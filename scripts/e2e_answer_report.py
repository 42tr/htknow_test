"""Self-contained answer reports with a case browser and readable evidence review."""
from __future__ import annotations

import html
import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple


CATEGORIES = {
    "facts": "事实问答", "cross_document": "跨文档问答", "abstention": "无答案处理",
    "multi_turn": "多轮对话", "wiki": "Wiki 问答", "safety": "安全与权限",
}
STATES = {"pass": ("ok", "通过"), "fail": ("bad", "答案问题"),
          "judge": ("warn", "评审未完成"), "error": ("bad", "执行异常")}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def pct(value: Any) -> str:
    return "未评估" if value is None else f"{value:.1f}%"


def elapsed(value: Any) -> str:
    if value is None:
        return "—"
    if value < 1000:
        return f"{value:.0f} ms"
    if value < 60000:
        return f"{value / 1000:.2f} s"
    minutes, seconds = divmod(round(value / 1000), 60)
    return f"{minutes} 分 {seconds:02d} 秒"


def badge(label: str, tone: str = "neutral") -> str:
    return f'<span class="ar-badge ar-{tone}">{esc(label)}</span>'


def case_state(row: Dict[str, Any]) -> str:
    """A judge failure is not evidence that the generated answer is wrong."""
    if row.get("passed"):
        return "pass"
    response = row.get("response") or {}
    if not response.get("complete") or response.get("error"):
        return "error"
    rules = row.get("rules") or {}
    if rules and not rules.get("passed"):
        return "fail"
    if (row.get("judge") or {}).get("status") != "scored":
        return "judge"
    return "fail"


def case_badge(row: Dict[str, Any]) -> str:
    state = case_state(row)
    tone, label = STATES[state]
    if state == "judge":
        label = "评审异常" if (row.get("judge") or {}).get("status") == "error" else "语义未评估"
    return badge(label, tone)


def source_button(sid: Any, sources: Dict[str, Any], quote: str = "") -> str:
    label = f"[{sid}]"
    if str(sid) not in sources:
        return f'<span class="ar-citation ar-invalid" title="本轮未返回此引用对应的证据">{esc(label)}</span>'
    return (f'<button type="button" class="ar-citation" data-source="{esc(sid)}" '
            f'data-quote="{esc(quote)}" aria-label="查看引用 {esc(sid)} 的原文证据">{esc(label)}</button>')


def prose(text: Any, sources: Optional[Dict[str, Any]] = None) -> str:
    """Render a small, escaped Markdown subset; model HTML and URLs stay inert."""
    link_citations = sources is not None
    sources = sources if sources is not None else {}

    def inline(value: str) -> str:
        parts = []
        for part in re.split(r"(`[^`\n]+`)", value):
            if part.startswith("`") and part.endswith("`"):
                parts.append(f"<code>{esc(part[1:-1])}</code>")
                continue
            safe = esc(part)
            safe = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", safe)
            if link_citations:
                safe = re.sub(r"\[(\d+)\]", lambda match: source_button(match.group(1), sources), safe)
            parts.append(safe)
        return "".join(parts)

    out: List[str] = []
    fenced = False
    code: List[str] = []
    listing = False
    for line in str(text or "").splitlines():
        if line.lstrip().startswith("```"):
            if listing:
                out.append("</ul>")
                listing = False
            if fenced:
                out.append(f'<pre class="ar-code">{esc(chr(10).join(code))}</pre>')
                code = []
            fenced = not fenced
            continue
        if fenced:
            code.append(line)
            continue
        item = re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*)", line)
        if item:
            if not listing:
                out.append("<ul>")
                listing = True
            out.append(f"<li>{inline(item.group(1))}</li>")
        else:
            if listing:
                out.append("</ul>")
                listing = False
            if line.strip():
                heading = re.match(r"^#{1,6}\s+(.*)", line)
                out.append(f'<p{ " class=ar-prose-heading" if heading else ""}>'
                           f'{inline(heading.group(1) if heading else line)}</p>')
    if listing:
        out.append("</ul>")
    if code:
        out.append(f'<pre class="ar-code">{esc(chr(10).join(code))}</pre>')
    return "".join(out) or '<p class="ar-muted">未生成回答</p>'


def marked_source(content: str, quotes: List[str]) -> str:
    spans: List[Tuple[int, int]] = []
    for quote in quotes:
        if not quote:
            continue
        start = content.find(quote)
        if start >= 0:
            spans.append((start, start + len(quote)))
    merged: List[Tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    output, offset = [], 0
    for start, end in merged:
        output += [esc(content[offset:start]), "<mark>", esc(content[start:end]), "</mark>"]
        offset = end
    output.append(esc(content[offset:]))
    return "".join(output)


def render_checks(row: Dict[str, Any], sources: Dict[str, Any]) -> str:
    judge, rules = row.get("judge") or {}, row.get("rules") or {}
    scored = judge.get("status") == "scored"
    accuracy = judge.get("accuracy") or {}
    facts = accuracy.get("facts") or []
    claims = (judge.get("grounding") or {}).get("claims") or []
    citations = [citation for claim in claims for citation in claim.get("citations", [])]
    checks = []
    facts_needed = bool(row.get("facts"))
    correct = sum(fact.get("label") == "correct" for fact in facts)
    checks.append(("参考事实", (len(facts) == len(row["facts"]) and correct == len(facts)) if facts_needed else None,
                   f'{correct} / {len(row.get("facts") or [])} 项正确' if facts_needed else "本题不涉及参考事实"))
    behavior_ok = accuracy.get("behavior_ok") and accuracy.get("task_ok")
    checks.append(("行为与任务", behavior_ok, "拒答、相关性与题目要求"))
    supported = sum(claim.get("verdict") == "supported" for claim in claims)
    checks.append(("证据支持", supported == len(claims) if claims else None, f"{supported} / {len(claims)} 条陈述有支持" if claims else "本题无可核验的事实陈述"))
    citations_ok = all(citation.get("supported") for citation in citations)
    missing = row.get("citation_required", True) and any(not claim.get("citations") for claim in claims)
    checks.append(("引用核验", citations_ok and not missing and not rules.get("invalid_citations") if citations or missing or rules.get("invalid_citations") else None,
                   f'{sum(bool(c.get("supported")) for c in citations)} / {len(citations)} 条引用有效' + (" · 存在未引用的陈述" if missing else "")))
    output = ['<section class="ar-checks-section"><div class="ar-section-heading"><h3>评审结论</h3>',
              badge("规则检查通过", "ok") if rules.get("passed") else badge("规则未通过", "bad") if rules else badge("规则未完成"),
              '</div><div class="ar-check-grid">']
    for label, passed, detail in checks:
        tone = "ok" if passed else "bad"
        state = "通过" if passed else "未通过"
        if passed is None:
            tone, state = "neutral", "不涉及"
        elif not scored:
            tone, state, detail = "neutral", "未评审", "尚无有效的语义评分"
        output.append(f'<div class="ar-check"><span>{label}</span>{badge(state, tone)}<p>{esc(detail)}</p></div>')
    output.append("</div>")
    if scored and accuracy.get("reason"):
        output.append(f'<details class="ar-explanation"><summary>查看行为与任务的评审依据</summary><p>{esc(accuracy["reason"])}</p></details>')
    if rules.get("violations"):
        output.append('<ul class="ar-rule-errors">' + "".join(f'<li>{esc(v)}</li>' for v in rules["violations"]) + '</ul>')
    output.append('</section><section class="ar-claims"><div class="ar-section-heading"><h3>陈述与证据核验</h3>'
                  f'<span class="ar-muted">{len(claims)} 条陈述</span></div>')
    for claim in claims:
        verdict = claim.get("verdict")
        label = {"supported": "证据支持", "unsupported": "缺少支持", "contradicted": "证据冲突"}.get(verdict, "未评审")
        output.append(f'<details class="ar-claim"{ " open" if verdict != "supported" else ""}><summary>'
                      f'{badge(label, "ok" if verdict == "supported" else "bad")}<span>{esc(claim.get("claim"))}</span></summary>')
        output.append(f'<div class="ar-claim-body"><p class="ar-small-label">回答中的原句</p><blockquote>{esc(claim.get("answer_quote"))}</blockquote>')
        for title, items in [("支持证据", claim.get("support") or []), ("回答所用引用", claim.get("citations") or [])]:
            output.append(f'<p class="ar-small-label">{title}</p>')
            if not items:
                output.append('<p class="ar-muted">未提供</p>')
            for item in items:
                supported_item = item.get("supported")
                output.append('<div class="ar-evidence-quote">' + source_button(item.get("source_id"), sources, item.get("quote") or "") +
                              (badge("引用不支持该陈述", "bad") if supported_item is False else "") +
                              f'<blockquote>{esc(item.get("quote") or "未提供原文引句")}</blockquote></div>')
        if claim.get("reason"):
            output.append(f'<details class="ar-explanation"><summary>评审说明</summary><p>{esc(claim["reason"])}</p></details>')
        output.append('</div></details>')
    if not claims:
        output.append('<p class="ar-empty-note">' + ("本题未提取出需要证据核验的事实陈述。" if scored else "本轮语义评审未完成，暂无法展示陈述与证据的对应关系。") + '</p>')
    output.append('</section>')
    return "".join(output)


def render_case(row: Dict[str, Any], key: str, active: bool) -> str:
    response, judge = row.get("response") or {}, row.get("judge") or {}
    sources = {str(source["id"]): source for source in response.get("sources", [])}
    fact_results = {fact["id"]: fact for fact in (judge.get("accuracy") or {}).get("facts", [])}
    state = case_state(row)
    category = CATEGORIES.get(row.get("category"), row.get("category", "其他"))
    out = [f'<article class="ar-case-detail" id="{key}" data-state="{state}" data-category="{esc(row.get("category"))}"'
           f'{"" if active else " hidden"} tabindex="-1" aria-label="{esc(row.get("case_id"))} 的评测详情">',
           '<div class="ar-case-heading"><div>',
           f'<p class="ar-small-label"><span class="ar-mono">{esc(row.get("case_id"))}</span> · {esc(category)} · '
           f'第 {esc(row.get("repeat"))} 次 / 第 {esc(row.get("turn"))} 轮</p>',
           f'<h3 class="ar-question">{esc(row.get("question"))}</h3></div>{case_badge(row)}</div>']
    note = "已生成回答，但评审未完成；本轮不能作为语义验收通过的依据。" if state == "judge" else ""
    out.append(f'<div class="ar-diagnosis ar-{STATES[state][0]}"><strong>{esc(row.get("diagnosis") or STATES[state][1])}</strong>'
               f'{"<p>" + esc(note) + "</p>" if note else ""}</div>')
    out.append('<div class="ar-comparison"><section class="ar-answer"><div class="ar-section-heading"><h3>模型回答</h3>'
               f'<span class="ar-muted">总耗时 {elapsed(response.get("elapsed_ms"))}</span></div>'
               f'<div class="ar-prose">{prose(response.get("answer"), sources)}</div>'
               f'<p class="ar-answer-meta">首段文本 {elapsed(response.get("first_text_ms"))} · {len(sources)} 个证据来源</p></section>')
    out.append('<section class="ar-reference"><div class="ar-section-heading"><h3>参考答案要点</h3>'
               f'<span class="ar-muted">{"应拒答 / 说明信息不足" if row.get("expected_behavior") == "abstain" else "应基于证据回答"}</span></div>')
    for fact in row.get("facts") or []:
        result = fact_results.get(fact["id"]) or {}
        label = {"correct": "正确", "incorrect": "错误", "missing": "遗漏"}.get(result.get("label"), "未评审")
        tone = "ok" if label == "正确" else "bad" if label in ("错误", "遗漏") else "neutral"
        out.append(f'<div class="ar-fact"><div class="ar-fact-heading">{badge(label, tone)}<span class="ar-mono">{esc(fact["id"])}</span></div>'
                   f'<p>{esc(fact.get("description"))}</p>')
        if result.get("answer_quote"):
            out.append(f'<p class="ar-small-label">回答对应原句</p><blockquote>{esc(result["answer_quote"])}</blockquote>')
        if result.get("reason"):
            out.append(f'<details class="ar-explanation"><summary>评分依据</summary><p>{esc(result["reason"])}</p></details>')
        out.append('</div>')
    if not row.get("facts"):
        out.append('<p class="ar-empty-note">' + ("本题没有可回答的参考事实。应明确说明无法从已有知识中获得答案，避免补充未经证实的信息。" if row.get("expected_behavior") == "abstain" else "本题主要检查行为、上下文或权限约束，请结合评审结论查看。") + '</p>')
    if row.get("rubric"):
        out.append(f'<p class="ar-rubric">题目要求：{esc(row["rubric"])}</p>')
    out.append('</section></div>')
    if row.get("history"):
        out.append(f'<details class="ar-history"><summary>本轮实际使用的对话历史 · {len(row["history"])} 条消息</summary>')
        for message in row["history"]:
            role = {"user": "用户", "assistant": "助手"}.get(message.get("role"), message.get("role"))
            out.append(f'<div class="ar-message"><strong>{esc(role)}</strong><div class="ar-prose">{prose(message.get("content"))}</div></div>')
        out.append('</details>')
    out.append(render_checks(row, sources))
    out.append('<section class="ar-sources"><div class="ar-section-heading"><h3>实际返回的证据</h3>'
               '<span class="ar-muted">点击回答中的引用编号，定位对应原文</span></div>')
    quotes: Dict[str, List[str]] = {}
    for claim in (judge.get("grounding") or {}).get("claims", []):
        for item in (claim.get("support") or []) + (claim.get("citations") or []):
            quotes.setdefault(str(item.get("source_id")), []).append(item.get("quote") or "")
    for sid, source in sources.items():
        result = source.get("result") or {}
        wiki = result.get("wiki") or {}
        title = ((wiki.get("page") or {}).get("title") or (result.get("file") or {}).get("filename") or f"证据 {sid}")
        content = str(result.get("content") or "")
        out.append(f'<details class="ar-source" id="{key}-source-{esc(sid)}"><summary><span class="ar-source-number">[{esc(sid)}]</span>'
                   f'<span>{esc(title)}</span>{badge("Wiki" if wiki else "原文")}</summary>'
                   f'<pre>{marked_source(content, quotes.get(sid, []))}</pre></details>')
    if not sources:
        out.append('<p class="ar-empty-note">本轮未返回可展示的证据来源。</p>')
    out.append('</section><details class="ar-raw"><summary>原始规则与评审记录</summary>')
    raw = {"参考事实": row.get("facts"), "规则检查": row.get("rules"), "独立模型评审": row.get("judge")}
    out.append(f'<pre>{esc(json.dumps(raw, ensure_ascii=False, indent=2))}</pre></details></article>')
    return "".join(out)


REPORT_CSS = r"""
.answer-report{--ar-bg:#f6f7f4;--ar-ink:#263129;--ar-muted:#6d796c;--ar-line:#e4e9e0;--ar-green:#28654b;--ar-good:#edf5ed;--ar-red:#ad493f;--ar-bad:#fbefeb;--ar-amber:#916920;--ar-warn:#fbf5e6;color:var(--ar-ink);font:14px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;text-align:left;min-width:0}
.answer-report{border:0;box-shadow:none;background:transparent;padding:0;margin:0;max-width:none}.answer-report.report-support{margin-top:24px}.answer-report section{margin:0;padding:0;border:0;box-shadow:none;border-radius:0;background:none}.answer-report h3{color:var(--ar-ink)}.answer-report details{border:0;border-radius:0;padding:0;margin:0;background:none}.answer-report details>summary{padding:0;border:0;background:none;display:list-item;list-style-position:inside}.answer-report details>summary::before{content:none}.answer-report *{box-sizing:border-box}.answer-report [hidden]{display:none!important}.answer-report h2,.answer-report h3,.answer-report p{margin:0}.answer-report button,.answer-report input,.answer-report select{font:inherit}.answer-report button{cursor:pointer}.answer-report button:disabled{opacity:.4;cursor:not-allowed}.answer-report button:focus-visible,.answer-report a:focus-visible,.answer-report summary:focus-visible{outline:3px solid #a4c9ad;outline-offset:3px}.answer-report button{border:1px solid var(--ar-line);border-radius:6px;background:#fff;color:var(--ar-ink);padding:6px 10px}.answer-report button:hover{background:#f0f5ed}.answer-report summary{cursor:pointer}.answer-report .ar-muted{color:var(--ar-muted);font-size:11px}.answer-report .ar-mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px}.answer-report .ar-small-label{color:var(--ar-muted);font-size:11px;font-weight:400;margin-bottom:7px;line-height:1.8}.answer-report .ar-badge{display:inline-flex;align-items:center;white-space:nowrap;border-radius:4px;font-size:10px;font-weight:500;line-height:1.5;padding:4px 7px;background:#f0f2ed;color:#74816f;vertical-align:middle}.answer-report .ar-ok{background:var(--ar-good);color:var(--ar-green)}.answer-report .ar-bad{background:var(--ar-bad);color:var(--ar-red)}.answer-report .ar-warn{background:var(--ar-warn);color:var(--ar-amber)}
.answer-report .ar-overview{padding:26px 30px;background:#fff;border:1px solid var(--ar-line);border-radius:12px;margin-bottom:22px}.answer-report .ar-overview-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:23px}.answer-report .ar-overview-heading h2{font-size:17px;font-weight:600;line-height:1.4}.answer-report .ar-overview-heading p{font-size:12px;color:var(--ar-muted);margin-top:7px}.answer-report .ar-coverage{text-align:right;white-space:nowrap;font-size:11px;color:var(--ar-muted)}.answer-report .ar-coverage strong{font-size:17px;font-weight:600;color:var(--ar-ink);margin-left:6px;font-variant-numeric:tabular-nums}.answer-report .ar-distribution{display:flex;gap:3px;height:6px;border-radius:4px;overflow:hidden;margin-bottom:12px}.answer-report .ar-distribution span{min-width:2px}.answer-report .ar-distribution .ar-segment-pass{background:#6b9675}.answer-report .ar-distribution .ar-segment-fail{background:#cd7d6c}.answer-report .ar-distribution .ar-segment-judge{background:#d5b472}.answer-report .ar-distribution .ar-segment-error{background:#95675a}.answer-report .ar-legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--ar-muted);font-size:11px;margin-bottom:24px}.answer-report .ar-legend span{display:inline-flex;align-items:center;gap:6px}.answer-report .ar-legend i{display:inline-block;width:6px;height:6px;border-radius:2px;background:#6b9675}.answer-report .ar-legend .ar-dot-fail{background:#cd7d6c}.answer-report .ar-legend .ar-dot-judge{background:#d5b472}.answer-report .ar-legend .ar-dot-error{background:#95675a}.answer-report .ar-metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:24px}.answer-report .ar-metric+.ar-metric{border-left:1px solid var(--ar-line);padding-left:24px}.answer-report .ar-metric-label{font-size:12px;color:var(--ar-muted);margin-bottom:5px}.answer-report .ar-metric-value{font-size:28px;font-weight:600;line-height:1.3;letter-spacing:-.7px;font-variant-numeric:tabular-nums}.answer-report .ar-metric-caption{font-size:10px;color:var(--ar-muted);margin-top:7px}.answer-report .ar-semantic-note{font-size:12px;line-height:1.9;border-radius:6px;padding:11px 13px;margin-top:22px;background:var(--ar-warn);color:var(--ar-amber)}.answer-report .ar-more-metrics{margin-top:22px;border-top:1px solid var(--ar-line);padding-top:14px}.answer-report .ar-more-metrics>summary{font-size:11px;color:var(--ar-muted)}.answer-report .ar-extra-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:20px;padding:18px 0 2px}.answer-report .ar-extra-metrics dt{font-size:11px;color:var(--ar-muted)}.answer-report .ar-extra-metrics dd{margin:4px 0 0;font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}.answer-report .ar-categories{border-top:1px solid var(--ar-line);margin-top:17px;padding-top:14px}.answer-report .ar-categories>summary{font-size:11px;color:var(--ar-muted)}.answer-report .ar-category-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:16px}.answer-report .ar-category{border:0;border-left:2px solid var(--ar-line);border-radius:0;padding:7px 12px;text-align:left;background:none;display:flex;justify-content:space-between;align-items:center;gap:10px}.answer-report .ar-category strong{font-size:11px;font-weight:500}.answer-report .ar-category span{font-size:11px;color:var(--ar-muted);font-variant-numeric:tabular-nums}.answer-report .ar-category[aria-pressed=true]{border-color:var(--ar-green);background:var(--ar-good)}
.answer-report .ar-review-heading{display:flex;align-items:center;justify-content:space-between;gap:15px;margin:28px 0 15px}.answer-report .ar-review-heading h2{font-size:17px;font-weight:600}.answer-report .ar-review-heading p{font-size:11px;color:var(--ar-muted);margin-top:4px}.answer-report .ar-review-navigation{display:flex;gap:7px;align-items:center}.answer-report .ar-review-navigation button{font-size:11px;padding:5px 10px}.answer-report .ar-position{font-size:11px;color:var(--ar-muted);font-variant-numeric:tabular-nums;white-space:nowrap}.answer-report .ar-filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:17px}.answer-report .ar-filter{font-size:11px;padding:6px 11px;background:transparent;color:var(--ar-muted)}.answer-report .ar-filter[aria-pressed=true]{background:#e7f0e5;color:var(--ar-green);border-color:#b9cdb7}.answer-report .ar-workbench{display:grid;grid-template-columns:264px minmax(0,1fr);gap:22px;align-items:start;scroll-margin-top:20px}.answer-report .ar-case-index{position:sticky;top:20px;min-width:0;border:1px solid var(--ar-line);border-radius:10px;background:#fff;overflow:hidden}.answer-report .ar-index-details>summary{padding:15px 16px;font-size:12px;font-weight:500;border-bottom:1px solid var(--ar-line);list-style-position:inside}.answer-report .ar-index-controls{padding:14px 14px 12px;display:grid;gap:9px}.answer-report .ar-index-controls input,.answer-report .ar-index-controls select{width:100%;min-width:0;border:1px solid var(--ar-line);border-radius:6px;padding:8px 9px;color:var(--ar-ink);background:#fff;font-size:12px}.answer-report .ar-index-controls input:focus,.answer-report .ar-index-controls select:focus{outline:2px solid #d5e5d2;border-color:#91b08e}.answer-report .ar-case-list{max-height:650px;overflow:auto;overscroll-behavior:contain;padding:0 7px 7px;scrollbar-width:thin}.answer-report .ar-case-link{display:block;text-decoration:none;color:var(--ar-ink);padding:13px 10px;border-radius:6px;border-left:2px solid transparent;border-bottom:1px solid #f0f3ec;line-height:1.8;scroll-margin:8px}.answer-report .ar-case-link:hover{background:#f7f9f3}.answer-report .ar-case-link[aria-current=true]{background:#eef5ea;border-left-color:#57845b;border-bottom-color:transparent}.answer-report .ar-case-link-top{display:flex;align-items:center;justify-content:space-between;gap:6px;margin-bottom:5px}.answer-report .ar-case-link p{font-size:12px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;line-height:1.7;color:#5d6c58}.answer-report .ar-case-link small{display:block;margin-top:6px;font-size:10px;color:#89947f}.answer-report .ar-case-detail{min-width:0;background:#fff;border:1px solid var(--ar-line);border-radius:10px;padding:26px 28px;scroll-margin-top:20px}.answer-report .ar-case-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:17px}.answer-report h3.ar-question{font-size:20px;font-weight:600;line-height:1.65;letter-spacing:-.2px;overflow-wrap:anywhere}.answer-report .ar-case-heading>.ar-badge{flex:none;margin-top:2px}.answer-report .ar-diagnosis{padding:11px 13px;border-radius:7px;margin-bottom:24px;overflow-wrap:anywhere;font-size:12px;line-height:1.8}.answer-report .ar-diagnosis strong{font-weight:500}.answer-report .ar-diagnosis p{font-size:11px;margin-top:5px;opacity:.85}.answer-report .ar-comparison{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,.9fr);gap:25px}.answer-report .ar-section-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:13px;flex-wrap:wrap}.answer-report .ar-section-heading h3{font-size:13px;font-weight:600;line-height:1.6;margin:0}.answer-report .ar-reference{padding-left:25px;border-left:1px solid var(--ar-line);min-width:0}.answer-report .ar-answer{min-width:0}.answer-report .ar-answer-meta{font-size:10px;color:var(--ar-muted);margin-top:16px}.answer-report .ar-prose{font-size:13px;line-height:2;overflow-wrap:anywhere}.answer-report .ar-prose p+p{margin-top:10px}.answer-report .ar-prose ul{padding-left:19px;margin:10px 0}.answer-report .ar-prose li+li{margin-top:6px}.answer-report .ar-prose .ar-prose-heading{font-weight:600;margin-top:15px}.answer-report code{background:#f2f4ed;padding:2px 4px;border-radius:3px;font-size:11px;font-family:ui-monospace,Menlo,monospace}.answer-report .ar-citation{display:inline;color:var(--ar-green);background:#edf5e9;border:0;border-radius:3px;padding:1px 4px;margin:0 1px;font-size:10px;line-height:1.6;vertical-align:1px;cursor:pointer;white-space:nowrap}.answer-report .ar-citation:hover{background:#d7e8d1}.answer-report .ar-invalid{color:var(--ar-red);background:var(--ar-bad);cursor:help}.answer-report .ar-fact+.ar-fact{border-top:1px solid var(--ar-line);padding-top:16px;margin-top:16px}.answer-report .ar-fact-heading{display:flex;align-items:center;gap:9px;margin-bottom:8px}.answer-report .ar-fact-heading .ar-mono{color:#97a18d;font-size:10px}.answer-report .ar-fact>p:not(.ar-small-label){font-size:12px;line-height:1.9;overflow-wrap:anywhere}.answer-report .ar-fact .ar-small-label{margin:13px 0 6px}.answer-report blockquote{padding:9px 12px;margin:7px 0 11px;background:#f7f8f3;border:0;border-left:2px solid #cddbc5;color:#65725d;font-size:12px;line-height:1.9;overflow-wrap:anywhere}.answer-report .ar-explanation{margin-top:10px;font-size:11px;color:var(--ar-muted)}.answer-report .ar-explanation summary{font-size:10px}.answer-report .ar-explanation p{padding:10px 0;line-height:1.9;overflow-wrap:anywhere}.answer-report .ar-rubric{font-size:11px;line-height:1.9;color:var(--ar-muted);margin-top:15px}.answer-report .ar-empty-note{font-size:12px;color:var(--ar-muted);line-height:1.9;background:#f8faf5;padding:12px;border-radius:6px}.answer-report .ar-checks-section,.answer-report .ar-claims,.answer-report .ar-sources{border-top:1px solid var(--ar-line);padding-top:23px;margin-top:25px}.answer-report .ar-check-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.answer-report .ar-check{background:#f8faf5;border-radius:6px;padding:12px}.answer-report .ar-check>span:first-child{font-size:11px;display:block;margin-bottom:8px;color:#586b52}.answer-report .ar-check p{font-size:10px;line-height:1.8;color:var(--ar-muted);margin-top:7px}.answer-report .ar-rule-errors{font-size:12px;color:var(--ar-red);padding-left:18px;margin:13px 0 0;overflow-wrap:anywhere}.answer-report .ar-claim{border-bottom:1px solid var(--ar-line);padding:13px 0}.answer-report .ar-claim:last-child{border-bottom:0}.answer-report .ar-claim>summary{display:flex;align-items:flex-start;gap:10px;list-style:none;font-size:12px;line-height:1.9}.answer-report .ar-claim>summary::-webkit-details-marker{display:none}.answer-report .ar-claim>summary>span:last-child{flex:1}.answer-report .ar-claim>summary::after{content:'展开';font-size:10px;color:#88957f;flex:none}.answer-report .ar-claim[open]>summary::after{content:'收起'}.answer-report .ar-claim-body{padding:16px 0 2px}.answer-report .ar-claim-body>.ar-small-label{margin-top:15px}.answer-report .ar-claim-body>.ar-small-label:first-child{margin-top:0}.answer-report .ar-evidence-quote{margin:7px 0 12px}.answer-report .ar-evidence-quote blockquote{margin-top:6px}.answer-report .ar-source{border:1px solid var(--ar-line);border-radius:6px;margin-top:10px;scroll-margin-top:22px;overflow:hidden}.answer-report .ar-source>summary{display:flex;align-items:center;gap:10px;list-style:none;padding:12px 14px;font-size:12px;overflow-wrap:anywhere}.answer-report .ar-source>summary>span:nth-child(2){flex:1;min-width:0}.answer-report .ar-source>summary::after{content:'查看原文';font-size:10px;color:var(--ar-muted);white-space:nowrap}.answer-report .ar-source[open]>summary{border-bottom:1px solid var(--ar-line)}.answer-report .ar-source[open]>summary::after{content:'收起'}.answer-report .ar-source-number{font-size:11px;color:var(--ar-green);font-family:ui-monospace,monospace;flex:none}.answer-report .ar-source pre,.answer-report .ar-code,.answer-report .ar-raw pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;margin:0;padding:16px;font:12px/1.9 ui-monospace,SFMono-Regular,Menlo,Consolas,'PingFang SC',monospace;background:#fafbf7;color:#64745b;max-height:480px;overflow:auto;border:0;border-radius:0;box-shadow:none}.answer-report mark{background:#e6eed0;color:#3c5828;padding:1px 0}.answer-report .ar-source.ar-source-selected{border-color:#75a16e;box-shadow:0 0 0 3px #edf4e7}.answer-report .ar-history,.answer-report .ar-raw{border-top:1px solid var(--ar-line);padding-top:17px;margin-top:24px;color:var(--ar-muted);font-size:11px}.answer-report .ar-history>summary,.answer-report .ar-raw>summary{font-size:11px}.answer-report .ar-raw pre{margin-top:13px;font-size:10px;max-height:420px}.answer-report .ar-message{padding:14px 0;border-bottom:1px solid var(--ar-line)}.answer-report .ar-message>strong{font-size:10px;color:var(--ar-muted)}.answer-report .ar-message .ar-prose{font-size:12px;margin-top:6px}.answer-report .ar-no-results{padding:35px;text-align:center;border:1px dashed #d3ddcc;border-radius:8px;color:var(--ar-muted);font-size:12px}.answer-report .ar-search-empty{padding:18px 9px;font-size:11px;color:var(--ar-muted)}.answer-report .ar-visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap}.answer-report .ar-announcement{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);max-width:90%;padding:10px 16px;border-radius:7px;color:#fff;background:#35482f;font-size:12px;z-index:20}.answer-report .ar-report-table{border-collapse:collapse;width:100%;font-size:12px}.answer-report .ar-report-table th,.answer-report .ar-report-table td{padding:11px 13px;border-bottom:1px solid var(--ar-line);text-align:left;vertical-align:middle;background:none}.answer-report .ar-report-table th{position:static;font-size:11px;font-weight:400;color:var(--ar-muted);background:#f7f9f3;white-space:nowrap}.answer-report .ar-report-table .ar-num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}.answer-report .ar-table-scroll{overflow:auto}.answer-report .ar-report-table{min-width:590px}.answer-report .ar-footer-section{background:#fff;border:1px solid var(--ar-line);border-radius:10px;margin-top:22px;padding:22px 28px}.answer-report .ar-footer-section>summary{font-size:14px;font-weight:500}.answer-report .ar-footer-section[open]>summary{margin-bottom:18px}.answer-report .ar-run-info{display:grid;grid-template-columns:140px minmax(0,1fr);font-size:12px;gap:10px 15px;margin-top:18px}.answer-report .ar-run-info dt{color:var(--ar-muted)}.answer-report .ar-run-info dd{margin:0;overflow-wrap:anywhere}.answer-report .ar-run-warnings{font-size:12px;line-height:1.9;color:var(--ar-amber);padding-left:18px;margin:20px 0 0}
@media(max-width:1200px){.answer-report .ar-workbench{grid-template-columns:235px minmax(0,1fr);gap:18px}.answer-report .ar-comparison{grid-template-columns:1fr}.answer-report .ar-reference{border-left:0;border-top:1px solid var(--ar-line);padding:20px 0 0;margin-top:0}.answer-report .ar-case-detail{padding:23px}.answer-report .ar-check-grid{grid-template-columns:1fr 1fr}.answer-report .ar-case-heading{flex-direction:column;gap:10px}.answer-report h3.ar-question{font-size:18px}}
@media(max-width:760px){.answer-report .ar-workbench{grid-template-columns:1fr}.answer-report .ar-case-index{position:static}.answer-report .ar-case-list{max-height:280px}.answer-report .ar-index-controls{grid-template-columns:1fr 1fr}.answer-report .ar-metric-grid{gap:14px}.answer-report .ar-metric+.ar-metric{padding-left:14px}.answer-report .ar-metric-value{font-size:24px}.answer-report .ar-metric-caption{font-size:9px}.answer-report .ar-overview{padding:23px}.answer-report .ar-category-grid{grid-template-columns:1fr 1fr}.answer-report .ar-review-heading{align-items:flex-start}.answer-report .ar-review-navigation{gap:5px}.answer-report .ar-review-navigation button{font-size:10px;padding:5px 7px}.answer-report .ar-footer-section{padding:20px}.answer-report .ar-case-heading{flex-direction:row}.answer-report h3.ar-question{font-size:17px}}
@media(max-width:480px){.answer-report .ar-overview{padding:21px 18px;border-radius:9px}.answer-report .ar-overview-heading{flex-direction:column;gap:12px;margin-bottom:18px}.answer-report .ar-coverage{font-size:10px}.answer-report .ar-coverage strong{font-size:15px}.answer-report .ar-legend{gap:8px 15px;font-size:10px}.answer-report .ar-metric-grid{grid-template-columns:1fr 1fr;gap:20px 18px}.answer-report .ar-metric+.ar-metric{border:0;padding:0}.answer-report .ar-metric:nth-child(even){border-left:1px solid var(--ar-line);padding-left:18px}.answer-report .ar-metric-caption{font-size:10px}.answer-report .ar-extra-metrics{grid-template-columns:1fr 1fr;gap:15px}.answer-report .ar-semantic-note{font-size:11px;padding:10px}.answer-report .ar-review-heading{display:block;margin-top:25px}.answer-report .ar-review-navigation{margin-top:12px;justify-content:flex-end}.answer-report .ar-filter{font-size:10px;padding:5px 9px}.answer-report .ar-filters{gap:6px}.answer-report .ar-case-detail{padding:20px 17px}.answer-report .ar-case-heading{flex-direction:column}.answer-report .ar-question{font-size:17px}.answer-report .ar-prose{font-size:13px;line-height:1.95}.answer-report .ar-section-heading{gap:6px}.answer-report .ar-section-heading .ar-muted{font-size:10px}.answer-report .ar-source>summary{padding:11px;font-size:11px;gap:7px}.answer-report .ar-source>summary::after{font-size:9px}.answer-report .ar-source pre{padding:12px;font-size:11px}.answer-report .ar-claim>summary{flex-wrap:wrap}.answer-report .ar-claim>summary>span:last-child{flex-basis:calc(100% - 85px)}.answer-report .ar-claim>summary::after{margin-left:auto}.answer-report .ar-check-grid{gap:8px}.answer-report .ar-check{padding:10px}.answer-report .ar-run-info{grid-template-columns:1fr;gap:3px}.answer-report .ar-run-info dd{margin-bottom:12px}.answer-report .ar-index-controls input,.answer-report .ar-index-controls select{font-size:16px}.answer-report .ar-case-link p{font-size:13px}}
@media print{.answer-report .ar-case-index,.answer-report .ar-filters,.answer-report .ar-review-navigation,.answer-report .ar-announcement,.answer-report .ar-raw{display:none!important}.answer-report .ar-case-detail[hidden]{display:block!important}.answer-report .ar-workbench{display:block}.answer-report .ar-case-detail{break-before:page;border:0;padding:20px 0}.answer-report .ar-no-results{display:none}.answer-report .ar-overview,.answer-report .ar-case-detail{box-shadow:none}.answer-report .ar-source pre{max-height:none}.answer-report .ar-comparison{grid-template-columns:1fr 1fr}.answer-report .ar-reference{padding:0 0 0 20px;border-left:1px solid #ddd;border-top:0}}
"""


REPORT_JS = r"""
(function(){
  'use strict';
  var root=document.getElementById('answer-review');
  if(!root)return;
  var links=Array.from(root.querySelectorAll('.ar-case-link'));
  var cases=Array.from(root.querySelectorAll('.ar-case-detail'));
  var search=root.querySelector('[data-case-search]'),category=root.querySelector('[data-category-filter]');
  var filter=root.dataset.initialFilter||'all',visible=[],active=root.dataset.active;
  var index=root.querySelector('.ar-index-details'),announcement=root.querySelector('.ar-announcement');
  function announce(text){announcement.textContent=text;announcement.hidden=false;clearTimeout(announcement._timer);announcement._timer=setTimeout(function(){announcement.hidden=true;},3500);}
  function select(id,scroll,updateHash){
    if(!visible.some(function(link){return link.dataset.case===id;}))return;
    active=id;
    cases.forEach(function(article){article.hidden=article.id!==id;});
    links.forEach(function(link){if(link.dataset.case===id)link.setAttribute('aria-current','true');else link.removeAttribute('aria-current');});
    var position=visible.findIndex(function(link){return link.dataset.case===id;});
    root.querySelector('.ar-position').textContent=(position+1)+' / '+visible.length;
    root.querySelector('[data-previous]').disabled=position<=0;
    root.querySelector('[data-next]').disabled=position>=visible.length-1;
    if(updateHash){try{history.replaceState(null,'','#'+id);}catch(e){}}
    if(scroll){if(window.innerWidth<=760)index.open=false;var article=document.getElementById(id);article.scrollIntoView({behavior:'smooth',block:'start'});article.focus({preventScroll:true});}
  }
  function applyFilters(){
    var needle=search.value.trim().toLowerCase(),cat=category.value;
    visible=links.filter(function(link){var ok=(!needle||link.dataset.search.toLowerCase().indexOf(needle)>=0)&&(!cat||link.dataset.category===cat)&&(filter==='all'||(filter==='issues'?link.dataset.state!=='pass':link.dataset.state===filter));link.hidden=!ok;return ok;});
    root.querySelector('.ar-index-count').textContent='用例目录 · '+visible.length+' 条结果';
    root.querySelector('.ar-search-empty').hidden=visible.length>0;
    root.querySelector('.ar-no-results').hidden=visible.length>0;
    root.querySelectorAll('[data-filter]').forEach(function(button){button.setAttribute('aria-pressed',String(button.dataset.filter===filter));});
    root.querySelectorAll('[data-category]').forEach(function(button){if(button.classList.contains('ar-category'))button.setAttribute('aria-pressed',String(button.dataset.category===cat));});
    if(visible.length)select(visible.some(function(link){return link.dataset.case===active;})?active:visible[0].dataset.case,false,false);
    else{cases.forEach(function(article){article.hidden=true;});root.querySelector('.ar-position').textContent='0 / 0';root.querySelector('[data-previous]').disabled=true;root.querySelector('[data-next]').disabled=true;}
  }
  links.forEach(function(link){link.addEventListener('click',function(event){event.preventDefault();select(link.dataset.case,true,true);});});
  root.querySelectorAll('[data-filter]').forEach(function(button){button.onclick=function(){filter=button.dataset.filter;applyFilters();};});
  root.querySelectorAll('.ar-category').forEach(function(button){button.onclick=function(){category.value=category.value===button.dataset.category?'':button.dataset.category;applyFilters();root.querySelector('.ar-review-heading').scrollIntoView({behavior:'smooth',block:'start'});};});
  search.addEventListener('input',applyFilters);category.addEventListener('change',applyFilters);
  root.querySelector('[data-previous]').onclick=function(){var i=visible.findIndex(function(link){return link.dataset.case===active;});if(i>0)select(visible[i-1].dataset.case,true,true);};
  root.querySelector('[data-next]').onclick=function(){var i=visible.findIndex(function(link){return link.dataset.case===active;});if(i>=0&&i<visible.length-1)select(visible[i+1].dataset.case,true,true);};
  root.addEventListener('click',function(event){var button=event.target.closest('button[data-source]');if(!button)return;var article=button.closest('.ar-case-detail');if(!article)return;var source=document.getElementById(article.id+'-source-'+button.dataset.source);if(!source){announce('本轮未返回该引用对应的证据');return;}root.querySelectorAll('.ar-source-selected').forEach(function(el){el.classList.remove('ar-source-selected');});source.open=true;source.classList.add('ar-source-selected');source.scrollIntoView({behavior:'smooth',block:'center'});source.querySelector('summary').focus({preventScroll:true});if(button.dataset.quote){var mark=Array.from(source.querySelectorAll('mark')).find(function(el){return el.textContent.indexOf(button.dataset.quote)>=0;});if(mark)mark.scrollIntoView({behavior:'smooth',block:'center'});}});
  function loadHash(){var id=location.hash.slice(1);if(cases.some(function(article){return article.id===id;})){filter='all';category.value='';search.value='';active=id;applyFilters();select(id,false,false);}}
  applyFilters();loadHash();window.addEventListener('hashchange',loadHash);
  if(window.innerWidth<=760)index.open=false;
})();
"""


def render_section(answers: Dict[str, Any]) -> str:
    metrics, rows = answers.get("metrics") or {}, answers.get("rows") or []
    counts = Counter(case_state(row) for row in rows)
    active_index = next((i for state in ("fail", "judge", "error", "pass")
                         for i, row in enumerate(rows) if case_state(row) == state), None)
    active = f"answer-case-{active_index}" if active_index is not None else ""
    initial_filter = "issues" if counts["pass"] < len(rows) else "all"
    visible_count = len(rows) - counts["pass"] if initial_filter == "issues" else len(rows)
    out = [f'<style>{REPORT_CSS}</style><section class="answer-report" id="answer-review" '
           f'data-active="{active}" data-initial-filter="{initial_filter}">',
           '<section class="ar-overview"><div class="ar-overview-heading"><div><h2>生成答案评估</h2>',
           f'<p>计划 {esc(metrics.get("planned_turns", len(rows)))} 轮 · 已完成 {esc(metrics.get("completed_turns", 0))} 轮 · '
           f'已评审 {esc(metrics.get("judged_turns", 0))} 轮</p></div>',
           f'<div class="ar-coverage">语义评审覆盖率<strong>{pct(metrics.get("judge_coverage"))}</strong></div></div>',
           '<div class="ar-distribution" role="img" aria-label="逐轮结果分布">']
    for state in STATES:
        if counts[state]:
            out.append(f'<span class="ar-segment-{state}" style="flex:{counts[state]}" title="{STATES[state][1]} {counts[state]} 轮"></span>')
    out.append('</div><div class="ar-legend">')
    for state in STATES:
        out.append(f'<span><i class="ar-dot-{state}" aria-hidden="true"></i>{STATES[state][1]} <strong>{counts[state]}</strong> 轮</span>')
    out.append('</div><div class="ar-metric-grid">')
    for label, key, description in [
        ("答案正确率", "correctness", "可回答题：事实、行为与任务均通过"),
        ("事实覆盖率", "fact_completeness", "正确覆盖的参考事实 / 全部参考事实"),
        ("证据忠实度", "faithfulness", "已评审陈述中，有实际证据支持的比例"),
        ("引用准确率", "citation_precision", "已评审引用中，支持对应陈述的比例"),
    ]:
        out.append(f'<div class="ar-metric"><div class="ar-metric-label">{label}</div>'
                   f'<div class="ar-metric-value">{pct(metrics.get(key))}</div><p class="ar-metric-caption">{description}</p></div>')
    out.append('</div>')
    if answers.get("semantic_status") != "complete":
        out.append(f'<div class="ar-semantic-note"><strong>语义评审尚未完整完成。</strong> '
                   f'{counts["judge"]} 轮评审未完成；未完成的评审会影响正确率等指标，不能直接认定为答案内容错误。'
                   '可筛选“评审未完成”查看原因。</div>')
    extra = [("引用覆盖率", pct(metrics.get("citation_coverage"))),
             ("无答案题正确处理率", pct(metrics.get("abstention_rate"))),
             ("过度拒答率", pct(metrics.get("over_refusal_rate"))),
             ("规则通过率", pct(metrics.get("rule_pass_rate"))),
             ("逐轮严格通过率", pct(100 * counts["pass"] / len(rows) if rows else None)),
             ("对话总耗时 P95", elapsed((metrics.get("latency") or {}).get("p95"))),
             ("首段文本 P95", elapsed((metrics.get("first_text_latency") or {}).get("p95"))),
             ("无效引用 / 泄漏轮次", f'{metrics.get("invalid_citations", 0)} / {metrics.get("leakage_turns", 0)}')]
    out.append('<details class="ar-more-metrics"><summary>更多质量与性能指标</summary><dl class="ar-extra-metrics">')
    for label, value in extra:
        out.append(f'<div><dt>{label}</dt><dd>{esc(value)}</dd></div>')
    out.append('</dl><p class="ar-muted">逐轮严格通过率要求规则与语义检查均通过。证据与引用指标仅统计已评审内容；首段文本耗时不等同于模型 token 延迟。</p></details>')
    categories = metrics.get("categories") or {}
    out.append('<details class="ar-categories"><summary>按题型查看表现 · 点击题型筛选用例</summary><div class="ar-category-grid">')
    for name, category in categories.items():
        out.append(f'<button type="button" class="ar-category" data-category="{esc(name)}" aria-pressed="false">'
                   f'<strong>{esc(CATEGORIES.get(name, name))}</strong><span>{esc(category.get("passed", 0))} / {esc(category.get("planned", 0))} 轮通过</span></button>')
    out.append('</div></details></section><div class="ar-review-heading" id="case-review"><div><h2>逐题复盘</h2>'
               '<p>先定位问题，再对照回答、参考要点与原文证据。</p></div><div class="ar-review-navigation">'
               '<span class="ar-position" aria-live="polite"></span><button type="button" data-previous>上一题</button>'
               '<button type="button" data-next>下一题</button></div></div><div class="ar-filters" role="group" aria-label="按结果筛选用例">')
    for state, label, count in [("all", "全部", len(rows)), ("issues", "未通过", len(rows) - counts["pass"]),
                                ("fail", "答案问题", counts["fail"]), ("judge", "评审未完成", counts["judge"]),
                                ("error", "执行异常", counts["error"])]:
        if state == "error" and not count:
            continue
        out.append(f'<button type="button" class="ar-filter" data-filter="{state}" aria-pressed="{str(state == initial_filter).lower()}">{label} {count}</button>')
    out.append('</div><div class="ar-workbench"><aside class="ar-case-index" aria-label="评测用例目录">'
               '<details class="ar-index-details" open><summary><span class="ar-index-count">'
               f'用例目录 · {visible_count} 条结果</span></summary><div class="ar-index-controls">'
               '<label class="ar-visually-hidden" for="ar-case-search">搜索问题、用例或诊断</label>'
               '<input id="ar-case-search" type="search" data-case-search placeholder="搜索问题、用例或诊断" autocomplete="off">'
               '<label class="ar-visually-hidden" for="ar-category-filter">按题型筛选</label>'
               '<select id="ar-category-filter" data-category-filter><option value="">全部题型</option>')
    for name in sorted({row.get("category", "") for row in rows}):
        out.append(f'<option value="{esc(name)}">{esc(CATEGORIES.get(name, name))}</option>')
    out.append('</select></div><nav class="ar-case-list" aria-label="用例列表">')
    for i, row in enumerate(rows):
        key, state = f"answer-case-{i}", case_state(row)
        search = " ".join(str(row.get(k) or "") for k in ("case_id", "question", "diagnosis", "category"))
        out.append(f'<a class="ar-case-link" href="#{key}" data-case="{key}" data-state="{state}" '
                   f'data-category="{esc(row.get("category"))}" data-search="{esc(search)}"'
                   f'{" aria-current=true" if i == active_index else ""}{" hidden" if initial_filter == "issues" and state == "pass" else ""}>'
                   f'<div class="ar-case-link-top"><span class="ar-mono">{esc(row.get("case_id"))}</span>{case_badge(row)}</div>'
                   f'<p>{esc(row.get("question"))}</p><small>{esc(CATEGORIES.get(row.get("category"), row.get("category")))} · '
                   f'第 {esc(row.get("repeat"))} 次 / 第 {esc(row.get("turn"))} 轮</small></a>')
    out.append('<p class="ar-search-empty" hidden>没有符合筛选条件的用例。</p></nav></details></aside><div class="ar-case-content">')
    for i, row in enumerate(rows):
        out.append(render_case(row, f"answer-case-{i}", i == active_index))
    out.append(f'<div class="ar-no-results"{"" if not rows else " hidden"}>没有符合条件的用例，请调整筛选条件。</div></div></div>'
               '<div class="ar-announcement" role="status" aria-live="polite" hidden></div>'
               '<noscript><style>.answer-report .ar-case-detail[hidden],.answer-report .ar-case-link[hidden]{display:block!important}</style>'
               '<p>启用 JavaScript 可使用筛选、逐题切换和引用定位。当前可向下阅读全部用例。</p></noscript>'
               f'<script>{REPORT_JS}</script></section>')
    return "".join(out)


def render_gates(gates: List[Dict[str, Any]]) -> str:
    out = ['<div class="ar-table-scroll"><table class="ar-report-table"><thead><tr><th>验收项</th>'
            '<th class="ar-num">实测值</th><th class="ar-num">验收要求</th><th>结果</th><th>说明</th></tr></thead><tbody>']
    for gate in gates:
        unit = gate.get("unit", "")
        def value(v: Any) -> str:
            return "无实测数据" if v is None else elapsed(v) if unit == "ms" else f"{v:.1f}{unit}"
        threshold = gate.get("threshold")
        state = badge("未启用") if threshold is None else badge("通过", "ok") if gate.get("passed") else badge("未通过", "bad")
        out.append(f'<tr><td>{esc(gate.get("name"))}</td><td class="ar-num">{esc(value(gate.get("actual")))}</td>'
                   f'<td class="ar-num">{esc("未启用" if threshold is None else str(gate.get("comparator", "")) + " " + value(threshold))}</td>'
                   f'<td>{state}</td><td class="ar-muted">{esc(gate.get("note"))}</td></tr>')
    out.append('</tbody></table></div>')
    return "".join(out)


def render_report(report: Dict[str, Any]) -> str:
    cfg, answers = report.get("config") or {}, report["answers"]
    gates = report.get("gates") or []
    failed = [gate for gate in gates if gate.get("critical") and gate.get("threshold") is not None and not gate.get("passed")]
    state = "通过验收" if report.get("overall_pass") else "未通过验收"
    shell_css = """
    html{scroll-behavior:smooth;scroll-padding-top:20px}body{margin:0;background:#f6f7f4;color:#263129;-webkit-font-smoothing:antialiased;font:14px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}.report-shell{max-width:1512px;margin:auto;padding:0 36px 35px}.report-brand{display:flex;align-items:center;justify-content:space-between;padding:22px 0;border-bottom:1px solid #e0e6db}.report-brand strong{font-size:20px;letter-spacing:-.7px}.report-brand span{font-size:11px;color:#839076;margin-left:14px;letter-spacing:.6px}.report-brand a{font-size:11px;color:#617b50;text-decoration:none}.report-brand button{font:inherit;font-size:11px;padding:5px 10px;background:transparent;border:1px solid #dce4d4;border-radius:5px;color:#617b50;cursor:pointer}.report-brand nav{display:flex;align-items:center;gap:22px}.report-title{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:31px 0 26px}.report-title h1{font-size:27px;font-weight:600;letter-spacing:-.5px;margin:0 0 8px}.report-title p{font-size:12px;color:#748369;margin:0;overflow-wrap:anywhere}.report-title .report-run{font-size:10px;font-family:ui-monospace,monospace;margin-top:7px;color:#929e86}.report-verdict{text-align:right;flex:none}.report-verdict strong{font-size:13px;display:inline-block;border-radius:6px;padding:6px 11px;background:#fbefeb;color:#ad493f;font-weight:500}.report-verdict.pass strong{background:#eaf3e7;color:#28654b}.report-verdict p{font-size:10px;margin-top:7px}.report-footer{font-size:10px;text-align:center;color:#929e86;padding:25px 0 0}.report-footer-section{margin-top:22px}.report-support{margin-top:24px}@media(max-width:900px){.report-shell{padding:0 20px 25px}.report-title h1{font-size:24px}}@media(max-width:480px){.report-shell{padding:0 13px 25px}.report-brand{padding:16px 4px}.report-brand strong{font-size:18px}.report-brand span{font-size:9px;margin-left:9px}.report-brand nav{gap:12px}.report-brand a{font-size:10px}.report-brand button{display:none}.report-title{padding:25px 4px 21px;align-items:flex-start}.report-title h1{font-size:22px}.report-title p{font-size:10px}.report-title .report-run{font-size:9px;max-width:230px}.report-verdict strong{font-size:10px;padding:5px 7px}.report-verdict p{font-size:9px}.report-footer{font-size:9px}}@media print{body{background:#fff}.report-shell{padding:0}.report-brand nav{display:none}.report-title{padding:20px 0}.report-verdict strong{border:1px solid #bbb}.report-brand{padding-top:0}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto!important}}
    """
    out = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           f'<title>HTKnow 生成答案测试报告 · {esc(cfg.get("run_id"))}</title><style>{shell_css}</style></head><body><main class="report-shell">',
           '<header class="report-brand"><div><strong>HTKnow</strong><span>EVALUATION REPORT</span></div><nav aria-label="报告导航">'
           '<a href="#case-review">逐题复盘</a><a href="#report-gates">验收门禁</a><button type="button" onclick="window.print()">打印报告</button></nav></header>',
           '<div class="report-title"><div><h1>' + ('对话答案复评报告' if cfg.get('evaluation_type') == 'rejudge' else '对话答案评测报告') + '</h1>',
           f'<p>{esc(cfg.get("target"))} · {esc(str(cfg.get("started_at", "")).replace("T", " ")[:16])} · 总用时 {elapsed(cfg.get("total_ms"))}</p>',
           ((f'<p>来源运行 {esc(cfg.get("source_run_id"))} · 重新评审 {esc((answers.get("rejudge") or {}).get("reviewed_turns", len(answers.get("rows", []))))} 轮'
             f' · 继承已完成评审 {esc((answers.get("rejudge") or {}).get("inherited_turns", 0))} 轮 · 沿用全部原始答案与证据。</p>')
            if cfg.get('evaluation_type') == 'rejudge' else '') +
           f'<p class="report-run">RUN {esc(cfg.get("run_id"))}</p></div><div class="report-verdict{ " pass" if report.get("overall_pass") else ""}">'
           f'<strong>{state}</strong><p>{len(failed)} 项关键门禁未达标</p></div></div>', render_section(answers),
           '<div class="answer-report report-support"><details class="ar-footer-section" id="report-gates"><summary>'
           f'验收门禁 · {len(failed)} 项关键门禁未达标</summary>{render_gates(gates)}</details>',
           '<details class="ar-footer-section"><summary>运行信息与文档解析</summary><dl class="ar-run-info">']
    for label, value in [("知识库", f'#{cfg.get("kb_id")} · {cfg.get("kb_name", "")}'),
                         ("知识库处置", cfg.get("kb_disposition")), ("独立评审模型", (answers.get("judge") or {}).get("model")),
                         ("评审版本", (answers.get("judge") or {}).get("prompt_version")), ("合成事实种子", answers.get("seed")),
                         ("题集 SHA-256", answers.get("manifest_sha256")), ("开始时间", cfg.get("started_at")), ("结束时间", cfg.get("finished_at"))]:
        out.append(f'<dt>{label}</dt><dd>{esc(value)}</dd>')
    for fixture in answers.get("fixture_dispositions") or []:
        out.append(f'<dt>对照知识库 #{esc(fixture.get("kb_id"))}</dt><dd>{esc(fixture.get("disposition"))}</dd>')
    out.append('</dl><div class="ar-table-scroll"><table class="ar-report-table"><thead><tr><th>文档</th><th>状态</th><th class="ar-num">切片数</th><th>说明</th></tr></thead><tbody>')
    stages = {"completed": "解析完成", "skipped": "跳过", "parse_failed": "解析失败", "parse_timeout": "解析超时",
              "upload_failed": "上传失败", "download_failed": "语料获取失败"}
    for doc in report.get("documents") or []:
        stage = doc.get("stage")
        out.append(f'<tr><td>{esc(doc.get("doc_id"))}</td><td>{badge(stages.get(stage, stage), "ok" if stage == "completed" else "warn")}</td>'
                   f'<td class="ar-num">{esc(doc.get("slices"))}</td><td>{esc(doc.get("error"))}</td></tr>')
    out.append('</tbody></table></div>')
    if report.get("warnings"):
        out.append('<ul class="ar-run-warnings">' + ''.join(f'<li>{esc(w)}</li>' for w in report["warnings"]) + '</ul>')
    out.append('</details></div><footer class="report-footer">HTKnow Evaluation · 回答、证据与评审依据均保留在本报告中</footer></main></body></html>')
    return "".join(out)

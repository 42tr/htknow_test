#!/usr/bin/env python3
"""HTKnow chat evaluation: fixture creation, SSE traces, independent judging.

Run directly for chat-only evaluation, or use e2e_retrieval_test.py --mode both.
"""
from __future__ import annotations

import codecs
import copy
import hashlib
import http.client
import json
import random
import re
import string
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from e2e_judge import Judge, JudgeError, citation_ids

DEFAULT_CASES = Path(__file__).with_name("e2e_answer_cases.json")
MAX_EVENT = 4 * 1024 * 1024
MAX_STREAM = 16 * 1024 * 1024


class AnswerError(Exception):
    pass


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sse_events(response: Any, deadline: float, raw_file: Any) -> Iterator[Any]:
    """Incremental UTF-8/SSE decoder; comments, multiline data and CRLF are valid."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer, event, data = "", "message", []
    total, event_size = 0, 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AnswerError("对话超过总超时")
        # Bound each socket read by the remaining wall-clock budget. HTTPResponse
        # exposes the socket through its buffered reader for HTTP and HTTPS.
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(min(30, remaining))
        chunk = response.read1(65536)
        if chunk:
            total += len(chunk)
            if total > MAX_STREAM:
                raise AnswerError("对话事件流超过 16 MiB")
            raw_file.write(chunk)
            raw_file.flush()
        buffer += decoder.decode(chunk, final=not chunk)
        while True:
            match = re.search(r"\r\n|\r|\n", buffer)
            if not match or (chunk and match.group() == "\r" and match.end() == len(buffer)):
                break
            line, buffer = buffer[:match.start()], buffer[match.end():]
            event_size += len(line)
            if event_size > MAX_EVENT:
                raise AnswerError("单个对话事件超过 4 MiB")
            if not line:
                if data:
                    try:
                        payload = json.loads("\n".join(data))
                    except ValueError:
                        raise AnswerError("对话事件包含无效 JSON") from None
                    if not isinstance(payload, dict):
                        raise AnswerError("对话事件必须包含 JSON 对象")
                    yield event, payload
                event, data, event_size = "message", [], 0
            elif not line.startswith(":"):
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    event = value
                elif field == "data":
                    data.append(value)
        if len(buffer) + event_size > MAX_EVENT:
            raise AnswerError("单个对话事件超过 4 MiB")
        if not chunk:
            # An unterminated event at EOF is not proof of normal completion.
            return


def stream_chat(client: Any, request: Dict[str, Any], timeout: float, directory: Path) -> Dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "request.json", request)
    result: Dict[str, Any] = {"answer": "", "sources": [], "events": [], "complete": False,
                              "error": None, "first_text_ms": None, "elapsed_ms": None,
                              "finish_reason": None}
    started, deadline = time.perf_counter(), time.monotonic() + timeout
    headers = dict(client.headers, **{"Content-Type": "application/json", "Accept": "text/event-stream"})
    req = urllib.request.Request(client.base_url + "/api/v1/knowledge/chat", method="POST",
                                 data=json.dumps(request, ensure_ascii=False).encode("utf-8"), headers=headers)
    client.request_count += 1
    source_map: Dict[int, Dict[str, Any]] = {}
    try:
        with urllib.request.urlopen(req, timeout=min(30, timeout), context=client._ctx) as response, \
                (directory / "stream.sse").open("wb") as raw, \
                (directory / "events.jsonl").open("w", encoding="utf-8") as trace:
            if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                raise AnswerError("对话接口未返回 text/event-stream")
            for event, payload in sse_events(response, deadline, raw):
                entry = {"event": event, "data": payload, "elapsed_ms": (time.perf_counter() - started) * 1000}
                result["events"].append(entry)
                trace.write(json.dumps(entry, ensure_ascii=False) + "\n")
                trace.flush()
                if event == "error":
                    raise AnswerError("服务返回 error 事件：" + str(payload.get("message", "生成失败"))[:300])
                if event == "sources":
                    sources = payload.get("sources")
                    if not isinstance(sources, list):
                        raise AnswerError("sources 事件缺少来源列表")
                    incoming = {}
                    for source in sources:
                        if not isinstance(source, dict) or type(source.get("id")) is not int or source["id"] < 1:
                            raise AnswerError("来源编号无效")
                        sid = source["id"]
                        item = source.get("result")
                        if sid in incoming or not isinstance(item, dict) or not isinstance(item.get("content"), str):
                            raise AnswerError("来源编号重复或来源正文无效")
                        previous = source_map.get(sid)
                        if previous:
                            old = previous["result"]
                            if any(old.get(key) != item.get(key) for key in ("id", "file_id", "content")) or bool(old.get("wiki")) != bool(item.get("wiki")):
                                raise AnswerError("同一轮检索覆盖了已有引用编号")
                        incoming[sid] = source
                    if set(source_map) - set(incoming):
                        raise AnswerError("累计来源列表丢失了之前的引用")
                    source_map = incoming
                    result["sources"] = sources
                elif event == "delta":
                    text = payload.get("text")
                    if not isinstance(text, str):
                        raise AnswerError("delta.text 必须是字符串")
                    if text:
                        if result["first_text_ms"] is None:
                            result["first_text_ms"] = entry["elapsed_ms"]
                        result["answer"] += text
                elif event == "done":
                    result["finish_reason"] = payload.get("finish_reason")
                    if result["finish_reason"] not in ("stop", "no_sources"):
                        raise AnswerError("对话未正常结束：" + str(result["finish_reason"]))
                    if not result["answer"].strip():
                        raise AnswerError("对话完成但回答为空")
                    result["complete"] = True
                    break
            if not result["complete"]:
                raise AnswerError("连接提前结束，缺少 done 事件")
    except urllib.error.HTTPError as exc:
        result["error"] = f"对话请求失败 HTTP {exc.code}"
    except (AnswerError, OSError, UnicodeError, http.client.HTTPException) as exc:
        result["error"] = str(exc)[:500]
    finally:
        result["elapsed_ms"] = (time.perf_counter() - started) * 1000
        client.total_ms += result["elapsed_ms"]
        write_json(directory / "response.json", result)
    return result


def _render(value: Any, values: Dict[str, str]) -> Any:
    if isinstance(value, str):
        return string.Template(value).substitute(values)
    if isinstance(value, list):
        return [_render(item, values) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, values) for key, item in value.items()}
    return value


def prepare_suite(args: Any, workdir: Path, run_id: str, harness: Any) -> Dict[str, Any]:
    try:
        raw = Path(args.answer_cases).read_bytes()
        manifest = json.loads(raw)
    except (OSError, ValueError):
        raise AnswerError("无法读取答案评测题集 JSON") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise AnswerError("答案题集 schema_version 必须为 1")
    seed = args.answer_seed or run_id
    rng = random.Random(seed)
    suffix = hashlib.sha256(seed.encode()).hexdigest()[:10].upper()
    budget, reserve = rng.randint(100, 800), rng.randint(10, 80)
    month, day = rng.randint(1, 12), rng.randint(1, 28)
    values = {
        "run_id": run_id, "project": "星桥-" + suffix, "project_code": "PRJ-" + suffix,
        "owner": "林岚" + suffix[:3], "buyer": "顾宁" + suffix[3:6], "ops_owner": "周衡" + suffix[6:9],
        "contact": "苏禾" + suffix[-3:], "region": rng.choice(["华东二区", "华北三区", "华南一区"]),
        "date_iso": f"2027-{month:02d}-{day:02d}", "date_cn": f"2027年{month}月{day}日",
        "old_date_cn": f"2026年{month}月{day}日", "budget": str(budget), "reserve": str(reserve),
        "total": str(budget + reserve), "sla": rng.choice(["99.91", "99.93", "99.97"]),
        "timeout": str(rng.randint(11, 39)), "replicas": str(rng.randint(3, 9)),
        "backup": str(rng.choice([3, 6, 8, 12])), "channel": "OPS-" + suffix,
        "ticket": "SUP-" + suffix, "injection": "E2E_INJECTED_" + suffix,
        "private_project": "密舟-" + suffix, "secret": "PRIVATE_CANARY_" + suffix,
        "private_budget": str(rng.randint(400001, 900000)), "wiki_project": "云帆-" + suffix,
        "wiki_owner": "沈澜" + suffix[:3], "wiki_capacity": str(rng.randint(121, 987)),
        "wiki_retention": str(rng.randint(31, 99)),
    }
    values["wiki_current_capacity"] = str(int(values["wiki_capacity"]) + 127)
    try:
        rendered = _render(manifest, values)
    except (KeyError, ValueError):
        raise AnswerError("答案题集包含未知模板变量") from None
    cases = rendered.get("cases")
    documents = rendered.get("documents")
    if not isinstance(cases, list) or not cases or not isinstance(documents, list):
        raise AnswerError("答案题集必须包含非空 cases 数组和 documents 数组")
    fixture_ids, seen = set(), set()
    for doc in documents:
        if not isinstance(doc, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(doc.get("id", ""))) or doc["id"] in fixture_ids:
            raise AnswerError("答案题集文档 ID 无效或重复")
        fixture_ids.add(doc["id"])
        if doc.get("scope", "main") not in ("main", "private", "wiki") or not isinstance(doc.get("content"), str) or not doc["content"].strip():
            raise AnswerError("答案题集文档的 scope 或 content 无效")
    for case in cases:
        if not isinstance(case, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(case.get("id", ""))) or case["id"] in seen:
            raise AnswerError("答案题集用例 ID 无效或重复")
        seen.add(case["id"])
        if case.get("scope", "main") not in ("main", "wiki", "denied") or not isinstance(case.get("category"), str) or not case["category"].strip():
            raise AnswerError("答案题集用例的 scope 或 category 无效")
        kinds = case.get("required_source_kinds", [])
        if not isinstance(kinds, list) or not all(kind in ("original", "wiki") for kind in kinds):
            raise AnswerError("required_source_kinds 只能包含 original 或 wiki")
        if case.get("required_source_kind") not in (None, "original", "wiki"):
            raise AnswerError("required_source_kind 只能为 original 或 wiki")
        required = case.get("required_docs", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise AnswerError("required_docs 必须是文档 ID 数组")
        forbidden = case.get("forbidden", [])
        if not isinstance(forbidden, list) or not all(isinstance(item, str) and item.strip() for item in forbidden):
            raise AnswerError("forbidden 必须是非空字符串数组")
        if set(required) - fixture_ids:
            raise AnswerError("答案题集引用了不存在的文档")
        turns = case.get("turns")
        if not isinstance(turns, list) or not turns or len(turns) > 7:
            raise AnswerError("每个答案用例需要 1 到 7 轮问题")
        for turn in turns:
            if not isinstance(turn, dict) or not isinstance(turn.get("question"), str) or not 1 <= len(turn["question"].strip()) <= 4000:
                raise AnswerError("答案题集存在空问题或超过 4000 字的问题")
            if turn.get("expected_behavior") not in ("answer", "abstain", "partial") or not isinstance(turn.get("facts"), list):
                raise AnswerError("答案题集缺少有效 expected_behavior 或 facts")
            ids = []
            for fact in turn["facts"]:
                if not isinstance(fact, dict) or not isinstance(fact.get("id"), str) or not fact["id"].strip() or not isinstance(fact.get("description"), str) or not fact["description"].strip():
                    raise AnswerError("答案题集事实必须包含 id 和 description")
                ids.append(fact["id"])
                aliases = fact.get("aliases", [])
                if not isinstance(aliases, list) or not all(isinstance(alias, str) and alias.strip() for alias in aliases):
                    raise AnswerError("事实 aliases 必须是非空字符串数组")
                if fact.get("exact") and not aliases:
                    raise AnswerError("精确事实必须包含 aliases")
            if len(set(ids)) != len(ids) or (turn["expected_behavior"] == "answer" and not ids):
                raise AnswerError("答案题集事实 ID 重复，或可回答题没有参考事实")
    pages = rendered.get("wiki_pages", [])
    if not isinstance(pages, list) or any(not isinstance(page, dict) or any(
            not isinstance(page.get(key), str) or not page[key].strip() for key in ("slug", "title", "content")) for page in pages):
        raise AnswerError("wiki_pages 必须包含有效 slug、title 和 content")
    selected = {s.strip() for s in (args.answer_categories or "").split(",") if s.strip()}
    available = {case["category"] for case in cases}
    if selected - available:
        raise AnswerError("未知答案用例分类：" + ", ".join(sorted(selected - available)))
    cases = [c for c in cases if not selected or c["category"] in selected]
    if args.answer_limit:
        cases = cases[:args.answer_limit]
    if not cases:
        raise AnswerError("筛选后没有答案用例")
    pairs = []
    for doc in documents:
        # Prefix every generated path ourselves; manifest filenames cannot escape
        # the run directory or overwrite the original corpus.
        path = workdir / "answer-fixtures" / (doc["id"] + ".txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc["content"], encoding="utf-8")
        spec = harness.DocSpec(id=doc["id"], filename=path.name, fmt="txt", description="答案评测合成事实文档")
        record = harness.DocResult(doc_id=doc["id"], filename=path.name, fmt="txt", language="zh", optional=False, stage="ready",
                                   source_url="本地生成", integrity="local", size_bytes=path.stat().st_size,
                                   local_path=str(path), sha256=harness.sha256_of(path))
        pairs.append({"scope": doc.get("scope", "main"), "spec": spec, "record": record})
    suite = {"cases": cases, "pairs": pairs, "wiki_pages": rendered.get("wiki_pages", []),
             "seed": seed, "manifest_sha256": hashlib.sha256(raw).hexdigest(),
             "schema_version": 1, "fixture_scopes": {}, "private_values": values}
    write_json(workdir / "answer-suite.json", {"cases": cases, "documents": documents,
               "wiki_pages": suite["wiki_pages"], "seed": seed, "manifest_sha256": suite["manifest_sha256"]})
    return suite


def setup_scopes(suite: Dict[str, Any], args: Any, client: Any, harness: Any,
                 workdir: Path, kb_id: int) -> List[Any]:
    scopes = suite["fixture_scopes"]
    scopes["main"] = {"kb_id": kb_id, "client": client, "error": None}
    cleanup = []
    if any(c.get("needs_private_fixture") or c.get("scope") == "denied" for c in suite["cases"]):
        owner = harness.Client(client.base_url, "e2e-owner-" + suite["private_values"]["project_code"],
                               "user", "e2e-private-owner", args.http_timeout, args.retries, args.insecure, False)
        reader = harness.Client(client.base_url, "e2e-reader-" + suite["private_values"]["project_code"],
                                "user", "e2e-private-reader", args.http_timeout, args.retries, args.insecure, False)
        scopes["denied"] = {"client": reader, "kb_id": None, "error": None}
        try:
            private_id, _ = harness.create_knowledge_base(owner, "E2E私有隔离-" + suite["seed"], "答案评估私有对照", 90, False)
            cleanup.append((owner, private_id))
            scopes["denied"]["kb_id"] = private_id
            pairs = [p for p in suite["pairs"] if p["scope"] == "private"]
            harness.upload_and_wait(owner, args, private_id, [p["spec"] for p in pairs], [p["record"] for p in pairs], workdir)
            if not pairs or not all(p["record"].stage == "completed" and p["record"].slices > 0 for p in pairs):
                raise AnswerError("私有对照文档未成功解析")
            # A negative permission test only means anything if the positive
            # control really can retrieve the canary as its owner.
            deadline = time.monotonic() + min(args.parse_timeout, 30)
            while True:
                found, _ = harness.run_search(owner, suite["private_values"]["secret"], private_id)
                if any(suite["private_values"]["secret"] in str(hit.get("content", "")) for hit in found):
                    break
                if time.monotonic() >= deadline:
                    raise AnswerError("私有对照的所有者无法检索机密标记")
                time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))
        except (harness.ApiFailure, harness.HarnessError, AnswerError) as exc:
            scopes["denied"]["error"] = "私有隔离对照初始化失败：" + str(exc)[:300]
    if any(c.get("scope") == "wiki" for c in suite["cases"]):
        scopes["wiki"] = {"client": client, "kb_id": None, "error": None}
        try:
            wiki_id, _ = harness.create_knowledge_base(client, "E2E-Wiki-" + suite["seed"], "手工 Wiki 证据使用评测", 90, False)
            cleanup.append((client, wiki_id))
            scopes["wiki"]["kb_id"] = wiki_id
            wiki_pairs = [p for p in suite["pairs"] if p["scope"] == "wiki"]
            if wiki_pairs:
                harness.upload_and_wait(client, args, wiki_id, [p["spec"] for p in wiki_pairs],
                                        [p["record"] for p in wiki_pairs], workdir)
                if not all(p["record"].stage == "completed" and p["record"].slices > 0 for p in wiki_pairs):
                    raise AnswerError("Wiki 对照原文未成功解析")
            if not suite["wiki_pages"]:
                raise AnswerError("Wiki 用例没有 Wiki 页面 fixture")
            for page in suite["wiki_pages"]:
                client.post_json("/api/v1/knowledge/wiki/page", dict(page, kb_id=wiki_id, status="published", page_type="concept"))
            deadline = time.monotonic() + min(args.parse_timeout, 30)
            while True:
                found, _ = harness.run_search(client, suite["private_values"]["wiki_project"], wiki_id)
                if any(hit.get("wiki") for hit in found):
                    break
                if time.monotonic() >= deadline:
                    raise AnswerError("Wiki 页面未能被混合检索召回，请检查 Wiki 搜索配置")
                time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))
        except (harness.ApiFailure, harness.HarnessError, AnswerError) as exc:
            scopes["wiki"]["error"] = "Wiki 对照初始化失败：" + str(exc)[:300]
    return cleanup


def conversation_history(history: List[Dict[str, str]]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    remaining = 16000
    for index in range(len(history) - 2, max(-1, len(history) - 14), -2):
        pair = history[index:index + 2]
        length = sum(len(message["content"]) for message in pair)
        if length > remaining:
            break
        remaining -= length
        result[0:0] = pair
    return copy.deepcopy(result)


def rule_checks(case: Dict[str, Any], turn: Dict[str, Any], response: Dict[str, Any], kb_id: int) -> Dict[str, Any]:
    answer, sources = response["answer"], response["sources"]
    citations = citation_ids(answer)
    valid = {source["id"] for source in sources}
    invalid = [sid for sid in citations if sid not in valid]
    violations = []
    if invalid:
        violations.append("引用了本轮不存在的编号：" + ", ".join(map(str, sorted(set(invalid)))))
    if turn.get("citation_required", True) and not citations:
        violations.append("应答事实没有引用")
    leaked = [word for word in case.get("forbidden", []) if word.lower() in answer.lower()]
    if leaked:
        violations.append("回答输出了禁止内容：" + ", ".join(leaked))
    out_of_scope = []
    for source in sources:
        item = source["result"]
        scope = (item.get("kb") or {}).get("id")
        if scope is None:
            scope = (item.get("file") or {}).get("kb_id")
        if scope is None and item.get("wiki"):
            scope = (item["wiki"].get("page") or {}).get("kb_id")
        if scope != kb_id:
            out_of_scope.append(source["id"])
    if out_of_scope:
        violations.append("返回了知识库范围之外或无法核验范围的来源")
    if case.get("require_empty_sources") and sources:
        violations.append("无访问权限的用户收到了来源")
    kinds = {"wiki" if s["result"].get("wiki") else "original" for s in sources}
    required_kinds = set(case.get("required_source_kinds", []))
    if case.get("required_source_kind"):
        required_kinds.add(case["required_source_kind"])
    if required_kinds - kinds:
        violations.append("未返回用例要求的全部原文/Wiki 证据类型")
    exact_checks = []
    for fact in turn.get("facts", []):
        if fact.get("exact"):
            matched = any(alias.casefold() in answer.casefold() for alias in fact["aliases"])
            exact_checks.append({"id": fact["id"], "matched": matched})
            if not matched:
                violations.append("未包含应答的精确事实：" + fact["id"])
    return {"passed": not violations, "violations": violations, "invalid_citations": invalid,
            "citation_count": len(citations), "leaked": leaked, "out_of_scope": out_of_scope,
            "unauthorized_sources": bool(case.get("require_empty_sources") and sources), "exact_facts": exact_checks}


def run_suite(suite: Dict[str, Any], args: Any, client: Any, harness: Any, workdir: Path,
              kb_id: int, docs: List[Any], judge: Judge, artifact_dir: Path) -> Dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    cleanup = setup_scopes(suite, args, client, harness, workdir, kb_id)
    parsed = {doc.doc_id for doc in docs if doc.stage == "completed" and doc.slices > 0}
    parsed.update(p["record"].doc_id for p in suite["pairs"] if p["record"].stage == "completed" and p["record"].slices > 0)
    rows = []
    for case in suite["cases"]:
        for repeat in range(1, args.answer_repeats + 1):
            history: List[Dict[str, str]] = []
            broken_history = False
            for index, turn in enumerate(case["turns"], 1):
                directory = artifact_dir / case["id"] / f"repeat-{repeat}" / f"turn-{index}"
                directory.mkdir(parents=True, exist_ok=True)
                row = {"case_id": case["id"], "category": case["category"], "repeat": repeat, "turn": index,
                       "question": turn["question"], "facts": turn.get("facts", []),
                       "expected_behavior": turn["expected_behavior"], "rubric": turn.get("rubric", ""),
                       "citation_required": turn.get("citation_required", True),
                       "history": conversation_history(history), "response": None, "rules": None,
                       "judge": {"status": "not_evaluated"}, "error": None, "passed": False,
                       "artifact_dir": str(directory)}
                scope = suite["fixture_scopes"][case.get("scope", "main")]
                reason = scope.get("error")
                missing = set(case.get("required_docs", [])) - parsed
                if missing:
                    reason = "依赖文档未成功解析：" + ", ".join(sorted(missing))
                if case.get("needs_private_fixture"):
                    reason = reason or suite["fixture_scopes"]["denied"].get("error")
                if broken_history:
                    reason = "前一轮未正常完成，不能构造真实对话历史"
                if reason:
                    row["error"] = reason
                    broken_history = True
                else:
                    response = stream_chat(scope["client"], {"question": turn["question"], "kb_id": scope["kb_id"],
                                           "messages": row["history"]}, args.answer_timeout, directory)
                    row["response"] = response
                    if response["error"] or not response["complete"]:
                        row["error"] = response["error"] or "对话没有正常完成"
                        broken_history = True
                    else:
                        history.extend([{"role": "user", "content": turn["question"]},
                                        {"role": "assistant", "content": response["answer"]}])
                        row["rules"] = rule_checks(case, turn, response, scope["kb_id"])
                        try:
                            row["judge"] = judge.evaluate(turn, response, directory)
                        except JudgeError as exc:
                            row["judge"] = {"status": "error", "error": str(exc)}
                            if exc.details:
                                row["judge"]["validation_details"] = exc.details
                            row["error"] = "模型评审失败：" + str(exc)
                        row["passed"] = row_passes(row)
                row["diagnosis"] = diagnose(row)
                rows.append(row)
                write_json(directory / "evaluation.json", row)
                print(f"  · 对话 {case['id']} / {repeat}.{index} "
                      f"{'通过' if row['passed'] else '未通过'}：{row['diagnosis']}", flush=True)
    result = summarize_answers(rows, suite, args, harness)
    result.update({"rows": rows, "judge": judge.metadata(), "seed": suite["seed"],
                   "manifest_sha256": suite["manifest_sha256"], "artifact_dir": str(artifact_dir),
                   "fixture_scopes": {key: {"kb_id": value["kb_id"], "error": value["error"]}
                                      for key, value in suite["fixture_scopes"].items()}})
    # Keep the clients out of JSON. The orchestrator disposes all KBs only after
    # retrieval AND generation gates have been evaluated.
    result["_cleanup"] = cleanup
    return result


def row_passes(row: Dict[str, Any]) -> bool:
    if row.get("error") or not (row.get("rules") or {}).get("passed") or row["judge"].get("status") != "scored":
        return False
    accuracy, claims = row["judge"]["accuracy"], row["judge"]["grounding"]["claims"]
    return bool(accuracy["behavior_ok"] and accuracy["task_ok"]
        and all(f["label"] == "correct" for f in accuracy["facts"])
        and all(c["verdict"] == "supported" for c in claims)
        and all(cit["supported"] for c in claims for cit in c["citations"])
        and all(any(cit["supported"] for cit in c["citations"]) for c in claims if row.get("citation_required", True)))


def rejudge_report(args: Any, harness: Any) -> int:
    """Re-evaluate every saved answer; preserve the original report and inputs."""
    import concurrent.futures
    import copy
    source_path = Path(args.rejudge_report).resolve()
    source_bytes = source_path.read_bytes()
    report = json.loads(source_bytes)
    original = report.get("answers") or {}
    if not original.get("rows"):
        raise AnswerError("来源报告没有对话答案，无法重新评审")
    html = Path(args.report or Path(__file__).parent / "reports" / f"rejudge-{time.time_ns()}.html").resolve()
    output = Path(args.json_report).resolve() if args.json_report else html.with_suffix(".json")
    if source_path in (output, html) or source_path.with_suffix(".html") in (output, html):
        raise AnswerError("重新评审必须使用新的报告路径，不能覆盖原报告")
    artifact_dir = output.parent / (output.stem + "-answers")
    started = time.perf_counter()
    started_at = harness.now_iso()
    rows = copy.deepcopy(original["rows"])
    errors_only = args.rejudge_errors_only
    reviewed = sum(bool(row.get("response") and row["response"].get("complete") and row.get("rules") and
                        (not errors_only or row["judge"].get("status") == "error")) for row in rows)
    print(f"重新评审 {reviewed}/{len(rows)} 轮已保存答案 · 来源 {source_path.parent.name}", flush=True)

    def evaluate(row: Dict[str, Any]) -> int:
        if errors_only and row["judge"].get("status") != "error":
            row.setdefault("inherited_judge", copy.deepcopy(original["judge"]))
            return 0
        response = row.get("response")
        if not response or not response.get("complete") or not row.get("rules"):
            return 0  # Generation failures remain in the planned denominator.
        directory = artifact_dir / row["case_id"] / f"repeat-{row['repeat']}" / f"turn-{row['turn']}"
        directory.mkdir(parents=True, exist_ok=True)
        row["source_artifact_dir"] = row.get("artifact_dir")
        row["artifact_dir"] = str(directory)
        judge = Judge(args.judge_url, args.judge_model, args.judge_timeout, args.judge_mode)
        row["error"] = None
        try:
            row["judge"] = judge.evaluate(row, response, directory)
        except JudgeError as exc:
            row["judge"] = {"status": "error", "error": str(exc), "validation_details": exc.details}
            row["error"] = "模型评审失败：" + str(exc)
        row["passed"] = row_passes(row)
        row["diagnosis"] = diagnose(row)
        write_json(directory / "evaluation.json", row)
        print(f"  · 复评 {row['case_id']} / {row['repeat']}.{row['turn']} "
              f"{'通过' if row['passed'] else '未通过'}：{row['diagnosis']}", flush=True)
        return judge.request_count

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        requests = sum(pool.map(evaluate, rows))
    answers = dict(original)
    answers.update(summarize_answers(rows, {}, args, harness))
    metadata = Judge(args.judge_url, args.judge_model, args.judge_timeout, args.judge_mode).metadata()
    metadata["requests"] = requests
    answers.update(rows=rows, judge=metadata, artifact_dir=str(artifact_dir))
    answers["rejudge"] = {"source_report": str(source_path), "source_run_id": source_path.parent.name,
                         "source_sha256": hashlib.sha256(source_bytes).hexdigest(), "all_turns": not errors_only,
                         "reviewed_turns": reviewed, "inherited_turns": len(rows) - reviewed,
                         "source_judge": original["judge"]}
    report["answers"] = answers
    report["gates"] = [g for g in report["gates"] if not g["metric"].startswith("answer_")] + answers["gates"]
    report["overall_pass"] = all(g["passed"] for g in report["gates"] if g["critical"])
    report["warnings"] = [w for w in report.get("warnings", []) if "答案语义评估未完整完成" not in w]
    if answers["semantic_status"] != "complete":
        report["warnings"].append("答案语义评估未完整完成，评审异常不能算作通过。")
    report["hints"] = list(dict.fromkeys(row["diagnosis"] for row in rows if not row["passed"]))[:12]
    report["config"].update(run_id=output.parent.name, evaluation_type="rejudge", source_run_id=source_path.parent.name,
        report_path=str(html), json_report_path=str(output), started_at=started_at, finished_at=harness.now_iso(),
        total_ms=(time.perf_counter() - started) * 1000, request_count=0)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, report)
    html.write_text(harness.render_html(report), encoding="utf-8")
    harness.console_summary(report)
    return 0 if report["overall_pass"] else 1


def diagnose(row: Dict[str, Any]) -> str:
    if row["error"]:
        return row["error"]
    if row.get("rules") and not row["rules"]["passed"]:
        return "；".join(row["rules"]["violations"])
    if row["judge"]["status"] != "scored":
        return "语义未评估，仅有规则检查结果"
    accuracy, claims = row["judge"]["accuracy"], row["judge"]["grounding"]["claims"]
    if not accuracy["behavior_ok"]:
        return "拒答或信息不足处理不符合预期"
    if not all(f["label"] == "correct" for f in accuracy["facts"]):
        return "答案事实错误或遗漏；请对照实际来源检查检索覆盖与生成"
    if any(c["verdict"] != "supported" for c in claims):
        return "答案包含实际证据不支持的陈述"
    if any(not all(cit["supported"] for cit in c["citations"]) for c in claims):
        return "引用来源不支持对应陈述"
    if row.get("citation_required", True) and any(not c["citations"] for c in claims):
        return "事实陈述缺少引用"
    if not accuracy["task_ok"]:
        return "相关性、语言或用例要求未满足"
    return "事实、证据与引用检查通过"


def summarize_answers(rows: List[Dict[str, Any]], suite: Dict[str, Any], args: Any, harness: Any) -> Dict[str, Any]:
    total = len(rows)
    scored = [r for r in rows if r["judge"]["status"] == "scored"]
    completed = [r for r in rows if r["response"] and r["response"]["complete"]]
    answers = [r for r in rows if r["expected_behavior"] != "abstain"]
    abstentions = [r for r in rows if r["expected_behavior"] == "abstain"]
    facts_total = sum(len(r["facts"]) for r in rows)
    correct_facts = sum(f["label"] == "correct" for r in scored for f in r["judge"]["accuracy"]["facts"])
    claims = [c for r in scored for c in r["judge"]["grounding"]["claims"]]
    citations = [c for claim in claims for c in claim["citations"]]
    citation_claims = [c for r in scored if r["citation_required"] for c in r["judge"]["grounding"]["claims"]]
    pct = lambda n, d: 100.0 * n / d if d else None
    good_answer = lambda r: (r["judge"]["status"] == "scored" and r["judge"]["accuracy"]["behavior_ok"]
        and r["judge"]["accuracy"]["task_ok"] and all(f["label"] == "correct" for f in r["judge"]["accuracy"]["facts"]))
    metrics = {"planned_turns": total, "completed_turns": len(completed), "judged_turns": len(scored),
        "completion_rate": pct(len(completed), total), "judge_coverage": pct(len(scored), total),
        "rule_pass_rate": pct(sum(bool(r["rules"] and r["rules"]["passed"]) for r in rows), total),
        "correctness": pct(sum(good_answer(r) for r in answers), len(answers)),
        "fact_completeness": pct(correct_facts, facts_total), "required_facts": facts_total, "correct_facts": correct_facts,
        "faithfulness": pct(sum(c["verdict"] == "supported" for c in claims), len(claims)),
        "claims": len(claims), "citation_precision": pct(sum(c["supported"] for c in citations), len(citations)),
        "citation_coverage": pct(sum(any(c["supported"] for c in claim["citations"]) for claim in citation_claims), len(citation_claims)),
        "abstention_rate": pct(sum(good_answer(r) for r in abstentions), len(abstentions)),
        "over_refusal_rate": pct(sum(r["judge"]["status"] == "scored" and r["judge"]["accuracy"]["behavior"] == "abstain" for r in answers), len(answers)),
        "invalid_citations": sum(len(r["rules"]["invalid_citations"]) for r in rows if r["rules"]),
        "leakage_turns": sum(bool(r["rules"]["leaked"] or r["rules"]["out_of_scope"] or r["rules"]["unauthorized_sources"]) for r in rows if r["rules"]),
        "latency": harness.aggregate(r["response"]["elapsed_ms"] for r in completed),
        "first_text_latency": harness.aggregate(r["response"]["first_text_ms"] for r in completed if r["response"]["first_text_ms"] is not None),
        "categories": {category: {"planned": len(group), "passed": sum(r["passed"] for r in group),
                                   "pass_rate": pct(sum(r["passed"] for r in group), len(group))}
                       for category in sorted({r["category"] for r in rows})
                       for group in [[r for r in rows if r["category"] == category]]}}
    gates = []
    def gate(name: str, key: str, threshold: float, applies: bool = True) -> None:
        value = metrics[key]
        gates.append({"name": name, "metric": "answer_" + key, "actual": value,
                      "threshold": threshold if applies else None, "comparator": ">=", "unit": "%",
                      "critical": applies, "passed": bool(applies and value is not None and value >= threshold),
                      "note": "缺少数据视为未通过；错误用例保留在计划分母内" if applies else "当前题集不涉及此项"})
    gate("对话完成率", "completion_rate", 100)
    gate("语义评审覆盖率", "judge_coverage", 100)
    gate("对话规则通过率", "rule_pass_rate", 100)
    gate("答案正确率", "correctness", args.min_answer_correctness, bool(answers))
    gate("参考事实覆盖率", "fact_completeness", args.min_answer_correctness, facts_total > 0)
    gate("答案证据忠实度", "faithfulness", args.min_answer_faithfulness, bool(answers))
    gate("引用准确率", "citation_precision", args.min_citation_precision, bool(answers))
    gate("引用覆盖率", "citation_coverage", args.min_citation_coverage, bool(answers))
    gate("无答案题正确处理率", "abstention_rate", args.min_abstention_rate, bool(abstentions))
    for label, key, limit in [("对话总耗时 P95", "latency", args.max_answer_p95_ms),
                              ("对话首段文本 P95", "first_text_latency", args.max_first_text_ms)]:
        value = metrics[key]["p95"]
        gates.append({"name": label, "metric": "answer_" + key + "_p95_ms", "actual": value,
                      "threshold": limit or None, "comparator": "<=", "unit": "ms", "critical": bool(limit),
                      "passed": bool(limit and value is not None and value <= limit), "note": "0 表示不设门禁"})
    return {"metrics": metrics, "gates": gates,
            "semantic_status": "complete" if len(scored) == total and total else ("not_evaluated" if args.judge_mode == "rules" else "incomplete"),
            "overall_pass": all(g["passed"] for g in gates if g["critical"])}


def render_section(answers: Dict[str, Any]) -> str:
    from e2e_answer_report import render_section as render
    return render(answers)


def render_report(report: Dict[str, Any], css: str = "") -> str:
    from e2e_answer_report import render_report as render
    return render(report)


if __name__ == "__main__":
    import e2e_retrieval_test as harness
    try:
        raise SystemExit(harness.main([*sys.argv[1:], "--mode", "chat"]))
    except (harness.HarnessError, harness.ApiFailure, AnswerError, JudgeError) as exc:
        print(f"\n✗ 测试中止：{exc}", file=sys.stderr)
        raise SystemExit(2)

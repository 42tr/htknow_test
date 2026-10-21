"""WeKnora REST/SSE adapter. No credentials are accepted as CLI arguments.

Protocol reference: Tencent/WeKnora e0c28bef3562de7d2d78d6c1fa480a045468a40f.
The common evaluator receives original reference bodies and normalized citation
markers; raw answers and wire events remain available in the run artifacts.
"""
from __future__ import annotations

import copy
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

REFERENCE = "e0c28bef3562de7d2d78d6c1fa480a045468a40f"
ROOT = "/api/v1"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A configured integration credential must never follow a redirect.
        return None


def endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.rstrip("/"))
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("WeKnora 地址须为不含内嵌凭据或查询参数的 HTTP(S) 根地址")
    path = parsed.path.rstrip("/")
    if path.endswith(ROOT):
        path = path[:-len(ROOT)]
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


class WeKnoraBackend:
    name = "weknora"

    def __init__(self, client: Any, args: Any, harness: Any, *, reader: bool = False):
        self.client, self.args, self.harness = client, args, harness
        self.session_id = None
        self.history: List[Dict[str, str]] = []
        self.sessions: List[str] = []
        self.template: Dict[str, Any] = {}
        self.wiki_pages: Dict[str, Dict[str, str]] = {}
        self.embedding = getattr(args, "weknora_embedding_model_id", "") or os.getenv("WEKNORA_EMBEDDING_MODEL_ID", "")
        self.chat_model = getattr(args, "weknora_chat_model_id", "") or os.getenv("WEKNORA_CHAT_MODEL_ID", "")
        self.rerank = getattr(args, "weknora_rerank_model_id", "") or os.getenv("WEKNORA_RERANK_MODEL_ID", "")
        self.template_id = getattr(args, "weknora_template_kb", "") or os.getenv("WEKNORA_TEMPLATE_KB", "")
        prefix = "WEKNORA_READER_" if reader else "WEKNORA_"
        key, token = os.getenv(prefix + "API_KEY", "").strip(), os.getenv(prefix + "TOKEN", "").strip()
        tenant = (os.getenv(prefix + "TENANT_ID", "") if reader else
                  getattr(args, "weknora_tenant_id", "") or os.getenv("WEKNORA_TENANT_ID", ""))
        if reader and not (key or token):
            raise harness.HarnessError("WeKnora 权限对照需要独立的 WEKNORA_READER_API_KEY 或 WEKNORA_READER_TOKEN")
        if key and token:
            raise harness.HarnessError(prefix + "API_KEY 和 TOKEN 只能配置一种")
        try:
            client.base_url = endpoint(client.base_url)
            allowed = os.getenv("WEKNORA_BASE_URL", "").strip()
            if (key or token) and (not allowed or endpoint(allowed) != client.base_url):
                raise ValueError("使用 WeKnora 凭据时，目标地址须与 WEKNORA_BASE_URL 一致")
        except ValueError as exc:
            raise harness.HarnessError(str(exc)) from None
        client.headers = {}
        if key:
            client.headers["X-API-Key"] = key
        if token:
            client.headers["Authorization"] = "Bearer " + token
        if tenant:
            client.headers["X-Tenant-ID"] = str(tenant)
        handlers = [NoRedirect()]
        if client._ctx:
            handlers.append(urllib.request.HTTPSHandler(context=client._ctx))
        client._opener = urllib.request.build_opener(*handlers)

    def data(self, payload: Any, kind: type = dict) -> Any:
        if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("data"), kind):
            raise self.harness.HarnessError("WeKnora 返回了无效的响应结构或业务失败")
        return payload["data"]

    def preflight(self) -> Dict[str, Any]:
        payload, elapsed = self.client.get(ROOT + "/knowledge-bases", {"page": 1, "page_size": 1})
        self.data(payload, list)
        total = payload.get("total")
        if self.template_id:
            payload, _ = self.client.get(ROOT + "/knowledge-bases/" + self.path(self.template_id))
            self.template = self.data(payload)
        self.embedding = self.embedding or self.template.get("embedding_model_id", "")
        self.chat_model = self.chat_model or self.template.get("summary_model_id", "")
        if not self.embedding:
            raise self.harness.HarnessError("WeKnora 测试建库需要 --weknora-template-kb 或 --weknora-embedding-model-id")
        return {"base_url": self.client.base_url, "backend": self.name, "reachable": True,
                "auth_ok": True, "kb_list_ms": elapsed, "existing_kbs": total,
                "openapi_title": "WeKnora", "openapi_version": None,
                "adapter_reference": REFERENCE, "models": {"embedding": self.embedding,
                "chat": self.chat_model or "workspace-default", "rerank": self.rerank or "workspace-default"},
                "chunking_config": {key: self.template.get("chunking_config", {}).get(key)
                                    for key in ("chunk_size", "chunk_overlap", "separators", "strategy")
                                    if key in (self.template.get("chunking_config") or {})}}

    @staticmethod
    def path(value: Any) -> str:
        return urllib.parse.quote(str(value), safe="")

    def create_kb(self, name: str, description: str, *, wiki: bool = False):
        config = {key: copy.deepcopy(self.template[key]) for key in (
            "chunking_config", "storage_provider_config", "storage_backend_id", "vector_store_id") if self.template.get(key)}
        config.update(name=name, description=description, type="document", embedding_model_id=self.embedding)
        if self.chat_model:
            config["summary_model_id"] = self.chat_model
        if wiki:
            config["indexing_strategy"] = {"vector_enabled": True, "keyword_enabled": True,
                                           "wiki_enabled": True, "graph_enabled": False}
        payload, elapsed = self.client.request("POST", ROOT + "/knowledge-bases", json_body=config, retryable=False)
        record = self.data(payload)
        if not isinstance(record.get("id"), str) or not record["id"]:
            raise self.harness.HarnessError("WeKnora 建库未返回有效 ID")
        return record["id"], elapsed

    def upload(self, kb_id: str, path: Path):
        payload, elapsed = self.client.upload(ROOT + "/knowledge-bases/" + self.path(kb_id) + "/knowledge/file",
                                            [], path.name, path.read_bytes(), "application/octet-stream")
        record = self.data(payload)
        if not isinstance(record.get("id"), str) or not record["id"]:
            raise self.harness.HarnessError("WeKnora 上传未返回有效知识 ID")
        return record["id"], elapsed

    def file_record(self, file_id: str):
        payload, elapsed = self.client.get(ROOT + "/knowledge/" + self.path(file_id))
        record = self.data(payload)
        status = {"pending": 0, "processing": 2, "completed": 1, "failed": -1}.get(record.get("parse_status"))
        return dict(record, status=status, backend_status=record.get("parse_status"),
                    log=record.get("error_message") or record.get("parse_error") or ""), elapsed

    def fetch_slices(self, file_id: str):
        page, count, chars = 1, 0, 0
        while True:
            payload, _ = self.client.get(ROOT + "/chunks/" + self.path(file_id), {"page": page, "page_size": 100})
            items = self.data(payload, list)
            count += len(items)
            chars += sum(len(str(item.get("content") or "")) for item in items if isinstance(item, dict))
            if not items or (type(payload.get("total")) is int and count >= payload["total"]) or len(items) < 100:
                return count, chars
            page += 1
            if page > 10000:
                raise self.harness.HarnessError("WeKnora 分块分页超过上限")

    def source(self, item: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise self.harness.HarnessError("WeKnora 来源缺少分块 ID")
        if not isinstance(item.get("content"), str) or not item["content"].strip():
            raise self.harness.HarnessError("WeKnora 来源没有返回可评审的正文")
        kb = item.get("knowledge_base_id")  # Never infer ownership from the requested KB.
        native_id = item.get("knowledge_id")
        kind = item.get("chunk_type") in ("wiki", "wiki_page") or item.get("knowledge_source") == "wiki"
        result = {"id": item["id"], "file_id": native_id, "content": item["content"], "score": item.get("score", 0),
                  "kb": {"id": kb}, "file": {"id": native_id, "kb_id": kb,
                  "filename": item.get("knowledge_filename") or item.get("knowledge_title") or ""},
                  "native": copy.deepcopy(item)}
        if kind:
            result["wiki"] = {"page": {"id": native_id, "kb_id": kb, "title": item.get("knowledge_title", "")}}
        return result

    def search(self, query: str, kb_id: str, file_ids=None):
        body = {"query": query, "match_count": self.args.top_k}
        if file_ids:
            body["knowledge_ids"] = list(file_ids)
        else:
            body["knowledge_base_ids"] = [kb_id]
        if self.rerank:
            body["rerank"] = {"model_id": self.rerank, "top_k": self.args.top_k}
        payload, elapsed = self.client.request("POST", ROOT + "/knowledge-search", json_body=body, retryable=False)
        return [self.source(item) for item in self.data(payload, list)], elapsed

    def delete_kb(self, kb_id):
        return self.client.delete(ROOT + "/knowledge-bases/" + self.path(kb_id))

    def create_wiki_page(self, kb_id, page):
        body = {key: page[key] for key in ("slug", "title", "content")}
        body.update(page_type="concept", status="published")
        payload, elapsed = self.client.request("POST", ROOT + "/knowledgebase/" + self.path(kb_id) + "/wiki/pages",
                                               json_body=body, retryable=False)
        if not isinstance(payload, dict) or not payload.get("slug"):
            raise self.harness.HarnessError("WeKnora Wiki 页面创建响应无效")
        self.wiki_pages[str(payload["slug"])] = {"kb_id": kb_id, "id": str(payload.get("id", ""))}
        return payload, elapsed

    def chat_request(self, request):
        history = request.get("messages") or []
        if not history:
            payload, _ = self.client.request("POST", ROOT + "/sessions", json_body={"title": "E2E answer evaluation"}, retryable=False)
            self.session_id = self.data(payload).get("id")
            if not isinstance(self.session_id, str) or not self.session_id:
                raise self.harness.HarnessError("WeKnora 创建会话未返回 ID")
            self.sessions.append(self.session_id)
            self.history = []
        elif not self.session_id or history != self.history[-len(history):]:
            raise self.harness.HarnessError("WeKnora 多轮测试必须复用同一真实会话，不能直接注入历史答案")
        body = {"query": request["question"], "knowledge_base_ids": [request["kb_id"]],
                "agent_enabled": False, "web_search_enabled": False, "disable_title": True}
        if self.chat_model:
            body["summary_model_id"] = self.chat_model
        return ROOT + "/knowledge-chat/" + self.path(self.session_id), body

    def commit_turn(self, request, answer):
        self.history.extend([{"role": "user", "content": request["question"]}, {"role": "assistant", "content": answer}])

    def cleanup_sessions(self):
        for session in self.sessions:
            self.client.delete(ROOT + "/sessions/" + self.path(session))
        self.sessions.clear()


    def stream_chat(self, request, timeout, directory):
        import http.client
        import urllib.error
        from e2e_answer_eval import AnswerError, sse_events, write_json
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "request.json", request)
        result = {"answer": "", "sources": [], "events": [], "complete": False,
                  "error": None, "first_text_ms": None, "elapsed_ms": None, "finish_reason": None,
                  "backend": self.name, "evidence_scope": "returned_references"}
        started, deadline = time.perf_counter(), time.monotonic() + timeout
        previous_ms = self.client.total_ms
        references = []
        by_id = {}
        try:
            original_timeout = self.client.timeout
            try:
                self.client.timeout = min(original_timeout, max(.001, deadline - time.monotonic()))
                path, body = self.chat_request(request)
            finally:
                self.client.timeout = original_timeout
            if time.monotonic() >= deadline:
                raise AnswerError("WeKnora 创建会话超过对话总超时")
            result["session_id"] = self.session_id
            write_json(directory / "backend-request.json", {"path": path, "body": body})
            headers = dict(self.client.headers, **{"Content-Type": "application/json", "Accept": "text/event-stream"})
            req = urllib.request.Request(self.client.base_url + path, method="POST",
                                         data=json.dumps(body, ensure_ascii=False).encode(), headers=headers)
            self.client.request_count += 1
            with self.client.open(req, min(30, max(.001, deadline - time.monotonic()))) as response, \
                    (directory / "stream.sse").open("wb") as raw, \
                    (directory / "events.jsonl").open("w", encoding="utf-8") as trace:
                if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                    raise AnswerError("WeKnora 对话接口未返回 text/event-stream")
                for event, payload in sse_events(response, deadline, raw):
                    entry = {"event": event, "data": payload, "elapsed_ms": (time.perf_counter() - started) * 1000}
                    result["events"].append(entry)
                    trace.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    trace.flush()
                    kind = payload.get("response_type")
                    if kind == "error":
                        raise AnswerError("WeKnora 返回 error：" + self.client.redact(str(payload.get("content", "生成失败")))[:300])
                    if kind == "references":
                        incoming = payload.get("knowledge_references")
                        if not isinstance(incoming, list):
                            raise AnswerError("WeKnora references 事件缺少来源列表")
                        for item in incoming:
                            source = self.source(item)
                            if item["id"] in by_id:
                                old = references[by_id[item["id"]] - 1]
                                if any(old.get(k) != item.get(k) for k in ("content", "knowledge_id", "knowledge_base_id", "chunk_type")):
                                    raise AnswerError("WeKnora 同一分块的来源正文或归属在本轮被覆盖")
                            else:
                                references.append(copy.deepcopy(item))
                                sid = len(references)
                                by_id[item["id"]] = sid
                                result["sources"].append({"id": sid, "result": source})
                    elif kind == "answer":
                        text = payload.get("content")
                        if not isinstance(text, str):
                            raise AnswerError("WeKnora answer.content 必须是字符串")
                        metadata = payload.get("data") or {}
                        if not isinstance(metadata, dict):
                            raise AnswerError("WeKnora answer.data 必须是对象")
                        if metadata.get("truncated"):
                            raise AnswerError("WeKnora 回答因输出上限被截断")
                        if text:
                            if result["first_text_ms"] is None:
                                result["first_text_ms"] = entry["elapsed_ms"]
                            result["answer"] += text
                    elif kind == "complete":
                        reason = payload.get("finish_reason")
                        if payload.get("done") is not True or reason not in (None, "", "stop", "complete"):
                            raise AnswerError("WeKnora complete 事件未正常结束")
                        if not result["answer"].strip():
                            raise AnswerError("WeKnora 对话完成但回答为空")
                        result["finish_reason"] = reason or "stop"
                        result["complete"] = True
                        break
                if not result["complete"]:
                    raise AnswerError("WeKnora 连接提前结束，缺少 complete 事件")
            result["raw_answer"] = result["answer"]
            result["answer"], result["citation_mapping"] = normalize_answer(result["answer"], references, self.wiki_pages)
            self.commit_turn(request, result["answer"])
        except (urllib.error.HTTPError, self.harness.ApiFailure) as exc:
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else exc.status
            result["error"] = f"WeKnora 对话准备或请求失败 HTTP {status}"
            result["http_status"] = status
            result["complete"] = False
        except (AnswerError, self.harness.HarnessError, OSError, UnicodeError, ValueError, http.client.HTTPException) as exc:
            result["error"] = self.client.redact(str(exc))[:500]
            result["complete"] = False
        finally:
            result.setdefault("raw_answer", result["answer"])
            result["elapsed_ms"] = (time.perf_counter() - started) * 1000
            self.client.total_ms = previous_ms + result["elapsed_ms"]
            write_json(directory / "response.json", result)
        return result


def normalize_answer(answer: str, references: List[Dict[str, Any]], wiki_pages=None):
    """Translate only native citation markers. Never create an absent citation."""
    by_id = {item["id"]: index for index, item in enumerate(references, 1)}
    changes = []

    def replace_wiki(match):
        slug = match[1].split("|", 1)[0].strip()
        page = (wiki_pages or {}).get(slug, {})
        candidates = []
        for index, item in enumerate(references, 1):
            if item.get("chunk_type") not in ("wiki", "wiki_page"):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            if (metadata.get("wiki_slug") == slug or metadata.get("wiki_page_slug") == slug
                    or page and item.get("knowledge_id") == page.get("id")
                    and item.get("knowledge_base_id") == page.get("kb_id")):
                candidates.append(index)
        if len(candidates) != 1:
            raise ValueError("WeKnora Wiki 引用缺少唯一的本轮正文来源")
        sid = candidates[0]
        changes.append({"original": match[0], "normalized": f"[{sid}]", "chunk_id": references[sid - 1]["id"]})
        return f"[{sid}]"

    def replace(match):
        attrs = dict((key, html.unescape(value)) for key, value in re.findall(r'([\w-]+)\s*=\s*"([^"]*)"', match[0]))
        chunk = attrs.get("chunk_id") or attrs.get("chunkId") or ""
        sid = by_id.get(chunk)
        if sid is None and chunk.isdigit() and 1 <= int(chunk) <= len(references):
            sid = int(chunk)
        if sid is None:
            raise ValueError("WeKnora 回答引用了未返回的或无法解析的分块")
        kb = attrs.get("kb_id") or attrs.get("kbId")
        if kb and kb != references[sid - 1].get("knowledge_base_id"):
            raise ValueError("WeKnora 引用标记与来源知识库不一致")
        changes.append({"original": match[0], "normalized": f"[{sid}]", "chunk_id": references[sid - 1]["id"]})
        return f"[{sid}]"

    # Do not interpret code examples as live citations.
    protected = re.compile(r"(`{3,}[\s\S]*?`{3,}|`[^`\n]*`)")
    pieces = protected.split(answer)
    for index in range(0, len(pieces), 2):
        pieces[index] = re.sub(r"<kb\b[^>]*?/?>", replace, pieces[index])
        pieces[index] = re.sub(r"\[\[([^\]]+)\]\]", replace_wiki, pieces[index])
    return "".join(pieces), changes

"""WeKnora adapter contract and complete offline HTTP/SSE workflows."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import e2e_answer_eval as answers
import e2e_retrieval_test as harness
import e2e_weknora as weknora
import e2e_web as web

KEY = "offline-weknora-secret"


def reference(kb="kb-uuid", chunk="chunk-uuid", **extra):
    return dict(id=chunk, knowledge_id="knowledge-uuid", knowledge_base_id=kb,
                content="项目负责人为林岚。副本数为3个。", knowledge_title="测试资料", chunk_type="text", score=.9, **extra)


class MockWeKnora:
    def __init__(self):
        self.kbs, self.files, self.sessions = {}, {}, {}
        self.requests, self.deleted = [], []
        self.mode = "normal"
        self.chunk_total = 1
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def send(self, value, status=200, content_type="application/json"):
                body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def authorized(self):
                if self.path.startswith("/judge/"):
                    return True
                outer.requests.append((self.command, self.path, dict(self.headers)))
                if self.headers.get("X-API-Key") != KEY:
                    self.send({"success": False}, 401)
                    return False
                if self.headers.get("x-role") or self.headers.get("x-user-id"):
                    self.send({"error": "HTKnow headers leaked"}, 400)
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                parsed = urlparse(self.path)
                path = parsed.path
                if outer.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", outer.url + "/redirected")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/api/v1/knowledge-bases":
                    return self.send({"success": True, "data": list(outer.kbs.values()), "total": len(outer.kbs)})
                if path == "/api/v1/knowledge-bases/template":
                    return self.send({"success": True, "data": {"id": "template", "embedding_model_id": "embed-1",
                        "summary_model_id": "chat-1", "chunking_config": {"chunk_size": 512}, "tenant_id": 99}})
                if path.startswith("/api/v1/knowledge/"):
                    fid = path.rsplit("/", 1)[-1]
                    record = outer.files[fid]
                    record["polls"] += 1
                    if outer.mode == "finalizing":
                        return self.send({"success": True, "data": {"id": fid,
                            "parse_status": "finalizing", "summary_status": "failed",
                            "pending_subtasks_count": 1,
                            "created_at": "2026-10-21T00:00:00Z", "updated_at": "2026-10-21T00:00:01Z"}})
                    return self.send({"success": True, "data": {"id": fid,
                        "parse_status": "processing" if record["polls"] == 1 else "completed",
                        "created_at": "2026-10-21T00:00:00Z", "updated_at": "2026-10-21T00:00:01Z"}})
                if path.startswith("/api/v1/chunks/"):
                    fid = path.rsplit("/", 1)[-1]
                    page = int(parse_qs(parsed.query).get("page", [1])[0])
                    start, end = (page - 1) * 100, min(page * 100, outer.chunk_total)
                    items = [{"id": f"chunk-{i}", "content": outer.files[fid]["content"]} for i in range(start, end)]
                    return self.send({"success": True, "data": items, "total": outer.chunk_total})
                return self.send({"error": "unknown endpoint"}, 404)

            def do_DELETE(self):
                if not self.authorized():
                    return
                outer.deleted.append(self.path)
                value = self.path.rsplit("/", 1)[-1]
                if "/knowledge-bases/" in self.path:
                    outer.kbs.pop(value, None)
                elif "/sessions/" in self.path:
                    outer.sessions.pop(value, None)
                return self.send({"success": True})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if not self.authorized():
                    return
                path = urlparse(self.path).path
                if path.endswith("/knowledge/file"):
                    msg = BytesParser(policy=default).parsebytes(
                        ("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
                    parts = {part.get_param("name", header="content-disposition"): part for part in msg.iter_parts()}
                    part = parts["file"]
                    fid = "knowledge-" + str(len(outer.files) + 1)
                    outer.files[fid] = {"id": fid, "kb_id": path.split("/")[4], "polls": 0,
                                        "filename": part.get_filename(), "content": part.get_payload(decode=True).decode()}
                    return self.send({"success": True, "data": {"id": fid}})
                data = json.loads(raw)
                if path == "/judge/chat/completions":
                    value = json.loads(data["messages"][1]["content"])
                    if "reference_facts" in value:
                        result = {"facts": [{"id": fact["id"], "label": "correct", "answer_quote": value["answer"]}
                                            for fact in value["reference_facts"]],
                                  "behavior": "answer", "behavior_ok": True, "task_ok": True}
                    else:
                        source = value["sources"][0]
                        result = {"claims": [{"claim": value["answer"], "answer_quote": value["answer"], "verdict": "supported",
                            "support": [{"source_id": source["id"], "quote": source["content"]}],
                            "citations": [{"source_id": source["id"], "supported": True, "quote": source["content"]}]}]}
                    return self.send({"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}, "finish_reason": "stop"}]})
                if path == "/api/v1/knowledge-bases":
                    kid = "kb-" + str(len(outer.kbs) + 1)
                    outer.kbs[kid] = dict(data, id=kid)
                    return self.send({"success": True, "data": {"id": kid}}, 201)
                if path == "/api/v1/knowledge-search":
                    kbs = data.get("knowledge_base_ids", [])
                    items = [dict(reference(file["kb_id"], "chunk-" + fid), knowledge_id=fid, content=file["content"])
                             for fid, file in outer.files.items() if file["kb_id"] in kbs]
                    return self.send({"success": True, "data": items})
                if path.endswith("/wiki/pages"):
                    return self.send(dict(data, id="wiki-page-uuid"), 201)
                if path == "/api/v1/sessions":
                    if outer.mode == "session_failure":
                        return self.send({"error": KEY}, 503)
                    sid = "session-" + str(len(outer.sessions) + 1)
                    outer.sessions[sid] = []
                    return self.send({"success": True, "data": {"id": sid}}, 201)
                if path.startswith("/api/v1/knowledge-chat/"):
                    sid = path.rsplit("/", 1)[-1]
                    outer.sessions[sid].append(data)
                    if outer.mode == "http_error":
                        return self.send({"error": KEY}, 500)
                    if outer.mode == "forbidden":
                        return self.send({"error": "denied"}, 403)
                    kid = data["knowledge_base_ids"][0]
                    file = next((f for f in outer.files.values() if f["kb_id"] == kid), None)
                    item = reference(kid)
                    if file:
                        item.update(knowledge_id=file["id"], content=file["content"])
                    if outer.mode == "missing_content":
                        item.pop("content")
                    if outer.mode == "wrong_kb":
                        item["knowledge_base_id"] = "other-kb"
                    def event(kind, **fields):
                        return b"event: message\ndata: " + json.dumps(dict(response_type=kind, **fields), ensure_ascii=False).encode() + b"\n\n"
                    answer = "副本数为3个" if "副本" in data["query"] else "负责人为林岚"
                    marker = '<kb doc="测试资料" kb_id="' + kid + '" chunk_id="chunk-uuid"/>'
                    if outer.mode == "bad_citation":
                        marker = marker.replace("chunk-uuid", "unknown-chunk")
                    body = event("thinking", content="internal plan must not enter the answer")
                    body += event("answer", content=answer + marker[:12], done=False)
                    body += event("references", knowledge_references=[item], done=True)
                    if outer.mode == "source_changed":
                        body += event("references", knowledge_references=[dict(item, content="changed")], done=True)
                    body += event("answer", content=marker[12:], done=True,
                                  data={"truncated": outer.mode == "truncated"})
                    if outer.mode not in ("missing_complete", "answer_done_only"):
                        body += event("complete", done=True, finish_reason="length" if outer.mode == "length" else "stop")
                    return self.send(body, content_type="text/event-stream")
                return self.send({"error": "unknown endpoint"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class WeKnoraTests(unittest.TestCase):
    def setUp(self):
        self.service = MockWeKnora()
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"WEKNORA_BASE_URL": self.service.url, "WEKNORA_API_KEY": KEY,
            "WEKNORA_TEMPLATE_KB": "template", "WEKNORA_TOKEN": "", "WEKNORA_READER_API_KEY": "",
            "WEKNORA_READER_TOKEN": "", "WEKNORA_EMBEDDING_MODEL_ID": "", "WEKNORA_CHAT_MODEL_ID": "",
            "WEKNORA_RERANK_MODEL_ID": ""})
        self.environment.start()
        self.args = harness.parse_args(["--backend", "weknora", "--target", self.service.url, "--quiet", "--retries", "0"])
        self.client = harness.make_client(self.args, self.service.url)
        harness.preflight(self.client)

    def tearDown(self):
        self.environment.stop()
        self.service.close()
        self.temp.cleanup()

    def chat(self, history=None):
        return answers.stream_chat(self.client, {"question": "负责人是谁？", "kb_id": "kb-uuid", "messages": history or []}, 3, self.directory)

    def test_real_sessions_and_late_references_preserve_raw_answer(self):
        first = self.chat()
        self.assertTrue(first["complete"], first)
        self.assertEqual(first["answer"], "负责人为林岚[1]")
        self.assertIn('<kb ', first["raw_answer"])
        self.assertNotIn("internal plan", first["answer"])
        second = self.chat([{"role": "user", "content": "负责人是谁？"}, {"role": "assistant", "content": first["answer"]}])
        self.assertTrue(second["complete"], second)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(len(self.service.sessions), 1)
        self.assertEqual(len(next(iter(self.service.sessions.values()))), 2)
        third = self.chat()
        self.assertNotEqual(third["session_id"], first["session_id"])
        self.assertIn(b'"response_type": "complete"', (self.directory / "stream.sse").read_bytes())
        self.assertNotIn(KEY, (self.directory / "backend-request.json").read_text())
        self.assertTrue(answers.rule_checks({}, {}, first, "kb-uuid")["passed"])

    def test_no_success_on_truncation_missing_completion_or_changed_evidence(self):
        for mode in ("truncated", "length", "missing_complete", "answer_done_only", "source_changed", "missing_content", "bad_citation"):
            with self.subTest(mode=mode):
                self.service.mode = mode
                response = self.chat()
                self.assertFalse(response["complete"], response)
                self.assertTrue(response["error"])

    def test_scope_is_taken_from_returned_evidence_not_request(self):
        self.service.mode = "wrong_kb"
        response = self.chat()
        self.assertFalse(response["complete"])
        self.assertIn("知识库不一致", response["error"])
        self.assertEqual(response["sources"][0]["result"]["kb"]["id"], "other-kb")

    def test_private_reader_requires_real_credentials(self):
        with self.assertRaisesRegex(harness.HarnessError, "独立"):
            harness.make_client(self.args, self.service.url, reader=True)
        self.service.mode = "forbidden"
        response = self.chat()
        self.assertEqual(response["http_status"], 403)
        self.assertFalse(response["complete"])
        self.assertEqual(response["sources"], [])

    def test_credentials_bound_to_target_and_redirects_not_followed(self):
        with self.assertRaisesRegex(harness.HarnessError, "一致"):
            harness.make_client(self.args, "http://example.invalid")
        self.service.mode = "redirect"
        with self.assertRaises(harness.ApiFailure):
            harness.preflight(self.client)
        self.assertFalse(any(path == "/redirected" for _, path, _ in self.service.requests))

    def test_http_errors_do_not_persist_credentials(self):
        self.service.mode = "http_error"
        response = self.chat()
        self.assertFalse(response["complete"])
        self.assertNotIn(KEY, json.dumps(response))
        for path in self.directory.iterdir():
            self.assertNotIn(KEY, path.read_text())

    def test_session_creation_failure_is_a_recorded_turn_error(self):
        self.service.mode = "session_failure"
        response = self.chat()
        self.assertFalse(response["complete"])
        self.assertEqual(response["http_status"], 503)
        self.assertEqual(response["raw_answer"], "")
        self.assertNotIn(KEY, json.dumps(response))
        self.assertTrue((self.directory / "response.json").exists())

    def test_upload_poll_paginate_chunks_and_preserve_native_ids(self):
        kb, _ = harness.create_knowledge_base(self.client, "test", "test", 90, False)
        path = self.directory / "中文.txt"
        path.write_text("负责人为林岚。")
        fid, _ = harness.upload_document(self.client, kb, path, "smart", [])
        record, _, polls = harness.wait_for_parse(self.client, fid, 3, .001, False)
        self.assertEqual(record["status"], 1)
        self.assertGreaterEqual(polls, 2)
        self.service.chunk_total = 101
        count, chars = harness.fetch_slices(self.client, fid)
        self.assertEqual(count, 101)
        self.assertEqual(chars, 101 * len(path.read_text()))
        found, _ = harness.run_search(self.client, "负责人", kb)
        hits = harness.normalize_hits(found, 10, [fid], ["林岚"])
        self.assertTrue(hits[0].doc_match)
        self.assertIsInstance(hits[0].slice_id, str)
        harness.delete_knowledge_base(self.client, kb)
        self.assertNotIn(kb, self.service.kbs)
        self.assertNotIn("template", self.service.kbs)

    def test_finalizing_file_with_chunks_is_usable_and_reports_summary_warning(self):
        self.service.mode = "finalizing"
        kb, _ = harness.create_knowledge_base(self.client, "finalizing", "test", 90, False)
        path = self.directory / "finalizing.txt"
        path.write_text("Wiki 对照正文。")
        fid, _ = harness.upload_document(self.client, kb, path, "smart", [])
        record, _, polls = harness.wait_for_parse(self.client, fid, 3, .001, False)
        self.assertEqual(record["status"], 1)
        self.assertEqual(record["backend_status"], "finalizing")
        self.assertIn("summary_status=failed", record["parse_warning"])
        self.assertEqual(polls, 1)
        self.assertEqual(harness.fetch_slices(self.client, fid)[0], 1)
        doc = harness.DocResult(doc_id="finalizing", filename=path.name, fmt="txt",
                                language="zh", optional=False, stage="ready", kb_file_id=fid)
        spec = harness.DocSpec(id="finalizing", filename=path.name, fmt="txt")
        harness.finalize_parse(self.client, self.args, spec, doc, record, 1, polls)
        _, _, warnings = harness.summarize(self.args, [doc], [], [], [])
        self.assertTrue(any("summary_status=failed" in warning for warning in warnings))

    def test_template_configuration_and_wiki_native_kind(self):
        kb, _ = harness.create_knowledge_base(self.client, "wiki", "test", 90, False, wiki=True)
        config = self.service.kbs[kb]
        self.assertEqual(config["embedding_model_id"], "embed-1")
        self.assertEqual(config["chunking_config"], {"chunk_size": 512})
        self.assertTrue(config["indexing_strategy"]["wiki_enabled"])
        self.assertNotIn("tenant_id", config)
        harness.create_wiki_page(self.client, kb, {"slug": "concept/test", "title": "test", "content": "test"})
        item = reference()
        item["chunk_type"] = "wiki_page"
        self.assertIn("wiki", self.client.backend.source(item))

    def test_cli_chat_complete_flow_with_repeats_and_cleanup(self):
        cases = self.directory / "cases.json"
        cases.write_text(json.dumps({"schema_version": 1, "documents": [{"id": "facts", "content": "项目负责人为林岚。副本数为3个。"}],
            "cases": [{"id": "conversation", "category": "multi_turn", "required_docs": ["facts"], "turns": [
                {"question": "负责人是谁？", "expected_behavior": "answer", "facts": [{"id": "owner", "description": "负责人林岚", "aliases": ["林岚"]}]},
                {"question": "副本是多少？", "expected_behavior": "answer", "facts": [{"id": "replicas", "description": "3个副本", "aliases": ["3"]}]}]}]}, ensure_ascii=False))
        report = self.directory / "report.json"
        cmd = [sys.executable, str(ROOT / "scripts/e2e_retrieval_test.py"), "--backend", "weknora", "--target", self.service.url,
               "--mode", "chat", "--answer-cases", str(cases), "--answer-repeats", "2", "--offline", "--poll-interval", ".001",
               "--judge-url", self.service.url + "/judge/", "--judge-model", "offline-model", "--corpus-cache", str(self.directory / "cache"),
               "--json-report", str(report), "--report", str(self.directory / "report.html"), "--cleanup", "delete", "--retries", "0"]
        env = dict(os.environ, E2E_JUDGE_API_KEY="None")
        run = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        value = json.loads(report.read_text())
        self.assertTrue(value["overall_pass"])
        self.assertEqual(value["config"]["backend"], "weknora")
        self.assertEqual(len(value["answers"]["rows"]), 4)
        self.assertTrue(all(row["passed"] for row in value["answers"]["rows"]))
        self.assertEqual(len(value["answers"]["session_dispositions"]), 2)
        self.assertEqual(len(self.service.sessions), 0)
        self.assertEqual(len(self.service.kbs), 0)
        self.assertIn("WeKnora", (self.directory / "report.html").read_text())
        self.assertNotIn(KEY, report.read_text())

    def test_cli_retrieval_complete_flow(self):
        report = self.directory / "retrieval.json"
        cmd = [sys.executable, str(ROOT / "scripts/e2e_retrieval_test.py"), "--backend", "weknora", "--target", self.service.url,
               "--docs", "needle_synthetic", "--offline", "--poll-interval", ".001", "--repeats", "1", "--think-time", "0",
               "--corpus-cache", str(self.directory / "cache"), "--json-report", str(report), "--report", str(self.directory / "retrieval.html"),
               "--cleanup", "delete", "--retries", "0"]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        value = json.loads(report.read_text())
        self.assertEqual(value["metrics"]["hit_rate"], 100)
        self.assertIsInstance(value["documents"][0]["kb_file_id"], str)
        self.assertIn("WeKnora 端到端", (self.directory / "retrieval.html").read_text())

    def test_web_parameters_choose_backend_without_secrets(self):
        params = web.sanitize_params({"backend": "weknora", "weknora_template_kb": "template", "api_key": KEY})
        argv = web.build_argv(self.service.url, params, self.directory)
        self.assertEqual(argv[argv.index("--backend") + 1], "weknora")
        self.assertIn("--weknora-template-kb", argv)
        self.assertNotIn(KEY, json.dumps(params) + str(argv))
        self.assertIn('id="f-backend"', web.UI_HTML)
        with self.assertRaises(ValueError):
            web.sanitize_params({"backend": "unknown"})

    def test_only_citation_markers_change_and_absent_citations_stay_absent(self):
        item = reference()
        raw = '负责人林岚<kb doc="test" chunk_id="chunk-uuid"/>；数值3。'
        normalized, mapping = weknora.normalize_answer(raw, [item])
        self.assertEqual(normalized, '负责人林岚[1]；数值3。')
        self.assertEqual(mapping[0]["chunk_id"], "chunk-uuid")
        self.assertEqual(weknora.normalize_answer("负责人林岚", [item])[0], "负责人林岚")
        code = '`<kb chunk_id="unknown"/>`'
        self.assertEqual(weknora.normalize_answer(code, [item])[0], code)
        with self.assertRaises(ValueError):
            weknora.normalize_answer('<kb chunk_id="unknown"/>', [item])

    def test_internal_thinking_is_removed_from_evaluated_answer_but_raw_answer_stays_available(self):
        item = reference()
        normalized, _ = weknora.normalize_answer('<think>未引用的内部推理事实</think>负责人林岚<kb chunk_id="chunk-uuid"/>', [item])
        self.assertEqual(normalized, "负责人林岚[1]")

    def test_wiki_links_require_actual_returned_page_body(self):
        item = reference()
        item.update(chunk_type="wiki_page", metadata={"wiki_slug": "concept/test"})
        answer, mapping = weknora.normalize_answer("负责人林岚[[concept/test|测试资料]]", [item])
        self.assertEqual(answer, "负责人林岚[1]")
        self.assertEqual(mapping[0]["chunk_id"], "chunk-uuid")
        with self.assertRaises(ValueError):
            weknora.normalize_answer("负责人林岚[[concept/test]]", [])
        with self.assertRaises(ValueError):
            weknora.normalize_answer("负责人林岚[[concept/test]]", [item, dict(item, id="second")])

    def test_history_cannot_be_injected_into_an_unrelated_server_session(self):
        response = self.chat([{"role": "user", "content": "foreign"}, {"role": "assistant", "content": "invented"}])
        self.assertFalse(response["complete"])
        self.assertIn("真实会话", response["error"])
        self.assertFalse(self.service.sessions)


if __name__ == "__main__":
    unittest.main()

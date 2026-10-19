"""Offline protocol, scoring and orchestration regressions (no paid models)."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import e2e_answer_eval as answers
import e2e_judge as judging
import e2e_retrieval_test as harness
import e2e_web as web


def source(sid=1, content="负责人为林岚。", kb=1):
    return {"id": sid, "result": {"id": sid + 10, "file_id": sid + 20, "content": content,
                                  "kb": {"id": kb}, "file": {"filename": "test.txt", "kb_id": kb}}}


def event(name, data, newline="\n"):
    return ("event: " + name + newline + "data: " + json.dumps(data, ensure_ascii=False) + newline * 2).encode()


class FragmentedResponse:
    def __init__(self, raw, step=1):
        self.raw, self.step = raw, step
    def read1(self, size):
        result, self.raw = self.raw[:self.step], self.raw[self.step:]
        return result


class DecoderTests(unittest.TestCase):
    def test_byte_fragmented_unicode_comments_crlf_and_multiline_json(self):
        raw = b': keepalive\r\n\r\nevent: delta\r\ndata: {\r\ndata: "text": "' + '中文答案'.encode() + b'"}\r\n\r\n'
        saved = io.BytesIO()
        decoded = list(answers.sse_events(FragmentedResponse(raw), time.monotonic() + 3, saved))
        self.assertEqual(decoded, [("delta", {"text": "中文答案"})])
        self.assertEqual(saved.getvalue(), raw)

    def test_eof_does_not_dispatch_unterminated_done(self):
        raw = event("delta", {"text": "partial"}) + b'event: done\ndata: {"finish_reason":"stop"}'
        decoded = list(answers.sse_events(FragmentedResponse(raw), time.monotonic() + 3, io.BytesIO()))
        self.assertEqual([name for name, _ in decoded], ["delta"])

    def test_malformed_json_and_expired_deadline_fail(self):
        for raw in [b'event: delta\ndata: invalid\n\n', b'data: []\n\n']:
            with self.assertRaises(answers.AnswerError):
                list(answers.sse_events(FragmentedResponse(raw), time.monotonic() + 3, io.BytesIO()))
        with self.assertRaises(answers.AnswerError):
            list(answers.sse_events(FragmentedResponse(b''), time.monotonic() - 1, io.BytesIO()))

    def test_keepalive_comments_do_not_extend_total_deadline(self):
        class Keepalive:
            def read1(self, size):
                time.sleep(0.005)
                return b": alive\n\n"
        with self.assertRaisesRegex(answers.AnswerError,"总超时"):
            list(answers.sse_events(Keepalive(),time.monotonic() + 0.02,io.BytesIO()))

    def test_history_matches_six_complete_pairs_and_character_budget(self):
        history = [message for i in range(9) for message in
                   [{"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": f"a{i}"}]]
        actual = answers.conversation_history(history)
        self.assertEqual(len(actual), 12)
        self.assertEqual(actual[0]["content"], "q3")
        actual[0]["content"] = "changed"
        self.assertEqual(history[6]["content"], "q3")
        self.assertEqual(answers.conversation_history([{"role":"user","content":"q"},
                                                       {"role":"assistant","content":"字" * 16000}]), [])


class JudgeValidationTests(unittest.TestCase):
    def test_local_settings_persist_with_environment_overrides_and_none_means_no_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "judge.json"
            path.write_text(json.dumps({"E2E_JUDGE_URL":"http://localhost:1234/v1/",
                                        "E2E_JUDGE_MODEL":"local-model","E2E_JUDGE_API_KEY":"None"}))
            with patch.dict(os.environ,{"E2E_JUDGE_CONFIG":str(path)},clear=True):
                judge = judging.Judge()
                self.assertEqual(judge.url,"http://localhost:1234/v1/chat/completions")
                self.assertEqual(judge.model,"local-model")
                self.assertEqual(judge.key,"")
                with patch.dict(os.environ,{"E2E_JUDGE_MODEL":"env-model","E2E_JUDGE_API_KEY":"env-key"}):
                    self.assertEqual(judging.Judge().model,"env-model")
                    self.assertEqual(judging.Judge().key,"env-key")

    def test_invalid_local_settings_fail_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "judge.json"
            for value in [[],{"E2E_JUDGE_MODEL":123},"not json"]:
                path.write_text(value if isinstance(value,str) else json.dumps(value))
                with patch.dict(os.environ,{"E2E_JUDGE_CONFIG":str(path)},clear=True):
                    with self.assertRaises(judging.JudgeError):
                        judging.Judge()

    def test_fact_ids_and_quotes_are_verified(self):
        facts = [{"id": "owner", "description": "负责人是林岚"}]
        correct = {"facts": [{"id": "owner", "label": "correct", "answer_quote": "林岚"}],
                   "behavior": "answer", "behavior_ok": True, "task_ok": True}
        judging.validate_accuracy(correct, facts, "负责人是林岚[1]。")
        for mutation in [dict(correct, facts=[]), dict(correct, behavior_ok="true"),
                         dict(correct, facts=[dict(correct["facts"][0], id=["owner"])]),
                         dict(correct, facts=[dict(correct["facts"][0], answer_quote="不存在的引句")])]:
            with self.assertRaises(judging.JudgeError):
                judging.validate_accuracy(mutation, facts, "负责人是林岚[1]。")

    def test_accuracy_quote_ignores_bold_and_anchors_back_to_actual_answer(self):
        # Regression from run 20261016-135034-cbaf5f, fact-02: the judge
        # correctly identified the owner but omitted Markdown bold markers.
        answer = ('根据知识库中的项目发布计划记录，**星桥-93E7010625** 的项目负责人是 **林岚93E** [2]。\n\n'
                  '此外，该项目的运维负责人为 **周衡062** [1]，财务相关的采购负责人为 **顾宁701** [3]。')
        payload = {"facts":[{"id":"owner","label":"correct",
                              "answer_quote":"星桥-93E7010625 的项目负责人是 林岚93E"}],
                   "behavior":"answer","behavior_ok":True,"task_ok":True}
        judging.validate_accuracy(payload,[{"id":"owner"}],answer)
        matched = payload["facts"][0]["answer_quote"]
        self.assertIn(matched,answer)
        self.assertIn("**林岚93E**",matched)

    def test_grounding_uses_original_spans_after_markdown_and_whitespace_matching(self):
        answer = "项目负责人是 **林岚93E**   [2]。"
        evidence = source(sid=2,content="负责人：**林岚93E**。\n\n项目编号：`PRJ_5`。")
        claim = {"claim":"项目负责人是林岚93E","answer_quote":"项目负责人是 林岚93E [2]。",
                 "verdict":"supported","support":[{"source_id":2,"quote":"负责人：林岚93E。"}],
                 "citations":[{"source_id":2,"supported":True,"quote":"负责人：林岚93E。"}]}
        judging.validate_grounding({"claims":[claim]},[evidence],answer,True)
        self.assertEqual(claim["answer_quote"],answer)
        self.assertEqual(claim["support"][0]["quote"],"负责人：**林岚93E**。")
        self.assertIn(claim["citations"][0]["quote"],evidence["result"]["content"])
        self.assertEqual(judging._quote("项目编号：PRJ_5。",evidence["result"]["content"],"source quote"),"项目编号：`PRJ_5`。")

    def test_quote_normalization_does_not_change_facts_or_join_separate_evidence(self):
        for quote,original in [
            ("负责人是林岚。","负责人是**顾宁**。"),
            ("预算为15万元。","预算为**150**万元。"),
            ("可用性为1.5%。","可用性为**15**%。"),
            ("答案 [2]。","**答案** [3]。"),
            ("owner_name","`ownername`"),
            ("PRJ123","`PRJ 123`"),
            ("编号12","编号**1 2**"),
            ("负责人是林岚。","负责人**不是**林岚。"),
            ("负责人是林岚[1]。","~~负责人是林岚~~[1]。"),
            ("上线日期2027年10月2日。发布阶段灰度。","上线日期2027年10月2日。\n部署区域华南。\n发布阶段灰度。"),
            ("代码foobar","代码`foo_bar`"),
        ]:
            with self.subTest(quote=quote,original=original):
                with self.assertRaises(judging.JudgeError):
                    judging._quote(quote,original,"source quote")

    def test_emphasis_and_code_content_are_handled_conservatively(self):
        for quote,original in [("项目负责人是林岚。","项目负责人是*林岚*。"),
                               ("项目负责人是 林岚。","项目负责人是 __林岚__。"),
                               ("负责人林岚。","负责人***林岚***。"),
                               ("代码__init__","代码`__init__`"),
                               ("值a*b*c","值`a*b*c`"),
                               ("负责人是林岚。 项目编号PRJ_5。","负责人是**林岚**。\n\n项目编号`PRJ_5`。")]:
            with self.subTest(original=original):
                matched=judging._quote(quote,original,"answer_quote")
                self.assertIn(matched,original)

    def test_markdown_matching_keeps_citation_validation_strict(self):
        answer="项目负责人是 **林岚** [2]。"
        claim={"claim":"负责人是林岚","answer_quote":"项目负责人是 林岚 [2]。","verdict":"supported",
               "support":[{"source_id":2,"quote":"林岚"}],
               "citations":[{"source_id":1,"supported":False,"quote":""}]}
        with self.assertRaisesRegex(judging.JudgeError,"引用评审未覆盖"):
            judging.validate_grounding({"claims":[claim]},[source(sid=2)],answer,True)

    def test_grounding_cannot_use_unseen_wiki_text_or_fabricated_evidence(self):
        s = source()
        s["result"]["wiki"] = {"page": {"content": "没有给模型看到的全文"}}
        claim = {"claim": "负责人是林岚", "answer_quote": "负责人是林岚[1]。", "verdict": "supported",
                 "support": [{"source_id": 1, "quote": "林岚"}],
                 "citations": [{"source_id": 1, "supported": True, "quote": "林岚"}]}
        judging.validate_grounding({"claims": [claim]}, [s], claim["answer_quote"], True)
        self.assertNotIn("page", judging.visible_sources([s])[0])
        bad = copy.deepcopy(claim)
        bad["support"][0]["quote"] = "没有给模型看到的全文"
        with self.assertRaises(judging.JudgeError):
            judging.validate_grounding({"claims": [bad]}, [s], claim["answer_quote"], True)
        with self.assertRaises(judging.JudgeError):
            judging.validate_grounding({"claims": []}, [s], claim["answer_quote"], True)
        bad = dict(claim, citations=[])
        with self.assertRaises(judging.JudgeError):
            judging.validate_grounding({"claims": [bad]}, [s], claim["answer_quote"], True)

    def test_rules_exclude_code_citations_and_detect_scope_and_canaries(self):
        self.assertEqual(judging.citation_ids('事实[1][2]。`[99]`\n```\n[98]\n```\n[3](https://x)'), [1, 2])
        result = answers.rule_checks({"forbidden": ["CANARY"]}, {"citation_required": True},
                                    {"answer":"CANARY 事实[99]", "sources":[source(kb=2)]}, 1)
        self.assertFalse(result["passed"])
        self.assertEqual(result["invalid_citations"], [99])
        self.assertEqual(result["out_of_scope"], [1])


class MockService:
    def __init__(self):
        self.kbs, self.files, self.pages = {}, {}, {}
        self.chat_requests = []
        self.mode = "normal"
        outer = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass
            def send(self, body, status=200, content_type="application/json"):
                if not isinstance(body, bytes):
                    body = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def owner(self):
                return self.headers.get("x-user-id")
            def allowed(self, kb):
                return kb in outer.kbs and (self.headers.get("x-role") == "admin" or outer.kbs[kb]["owner"] == self.owner())
            def sources(self, kb, query=""):
                if not self.allowed(kb):
                    return []
                if outer.mode == "complex_unsearchable" and outer.kbs[kb]["name"].startswith("E2E复杂推理-"):
                    return []
                result = []
                for fid, file in outer.files.items():
                    if file["kb_id"] == kb:
                        result.append({"id":fid * 10,"file_id":fid,"content":file["content"],"score":1,
                                       "file":{"id":fid,"filename":file["filename"],"kb_id":kb},"kb":{"id":kb}})
                for page in outer.pages.get(kb, []):
                    result.append({"id":900,"file_id":0,"content":page["content"],"score":1,
                                   "wiki":{"page":{"kb_id":kb,"title":page["title"],"slug":page["slug"]}},"kb":{"id":kb}})
                exact = [s for s in result if query and query in s["content"]]
                return exact or result
            def do_GET(self):
                path = urlparse(self.path)
                if path.path == "/api-docs/openapi.json":
                    return self.send({"info":{"title":"offline mock", "version":"1"}})
                if path.path.endswith("/knowledge_base/"):
                    return self.send({"total":len(outer.kbs)})
                if path.path.endswith("/search/"):
                    params = parse_qs(path.query)
                    return self.send({"results":self.sources(int(params["kb_id"][0]), params.get("query", [""])[0])})
                if "/files/" in path.path:
                    fid = int(path.path.split("/files/")[1].split("/")[0])
                    file = outer.files[fid]
                    if path.path.endswith("/slices"):
                        return self.send([{"id":fid * 10,"content":file["content"]}])
                    failed = outer.mode == "parse_failure" or (outer.mode == "complex_parse_failure" and file["filename"].startswith("complex_"))
                    return self.send({"id":fid,"status":-1 if failed else 1,
                                      "created_at":1,"updated_at":2,"log":"fixture parser"})
                return self.send({},404)
            def do_DELETE(self):
                kb = int(self.path.rstrip("/").rsplit("/",1)[1])
                outer.kbs.pop(kb, None)
                self.send({"deleted":kb})
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length",0)))
                path = urlparse(self.path).path
                if path.endswith("/files/"):
                    message = BytesParser(policy=default).parsebytes(("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
                    parts = {part.get_param("name",header="content-disposition"):part for part in message.iter_parts()}
                    fid = len(outer.files) + 1
                    outer.files[fid] = {"kb_id":int(parts["kb_id"].get_payload(decode=True)),
                                        "filename":parts["file"].get_filename(),
                                        "content":parts["file"].get_payload(decode=True).decode()}
                    return self.send({"id":fid})
                payload = json.loads(body)
                if path.endswith("/knowledge_base/"):
                    kb = max(outer.kbs.keys(),default=0) + 1
                    outer.kbs[kb] = {"owner":self.owner(),"name":payload["name"]}
                    return self.send({"id":kb})
                if path.endswith("/wiki/page"):
                    outer.pages.setdefault(payload["kb_id"],[]).append(payload)
                    return self.send({"page":payload})
                if path == "/judge/chat/completions":
                    data = json.loads(payload["messages"][1]["content"])
                    if outer.mode == "judge_failure" or (outer.mode == "owner_judge_failure" and "负责人" in data.get("question", "")):
                        return self.send({"choices":[{"message":{"content":"not JSON"}}]})
                    if "reference_facts" in data:
                        abstain = data["answer"].startswith("资料不足")
                        output = {"facts":[{"id":f["id"],"label":"missing" if abstain else "correct",
                                           "answer_quote":"" if abstain else data["answer"]} for f in data["reference_facts"]],
                                  "behavior":"abstain" if abstain else "answer",
                                  "behavior_ok":abstain == (data["expected_behavior"] == "abstain"),"task_ok":True}
                    else:
                        claim = {"claim":"mock answer claim","answer_quote":data["answer"],"verdict":"supported",
                                 "support":[{"source_id":data["sources"][0]["id"],"quote":data["sources"][0]["content"]}] if data["sources"] else [],
                                 "citations":[{"source_id":sid,"supported":sid == 1,"quote":data["sources"][0]["content"] if data["sources"] else ""}
                                              for sid in set(judging.citation_ids(data["answer"]))]}
                        output = {"claims":[] if data["answer"].startswith("资料不足") else [claim]}
                        if (output["claims"] and outer.mode in ("quote_repair", "unrepairable_quote")
                                and ("validation_feedback" not in data or outer.mode == "unrepairable_quote")):
                            output["claims"][0]["support"][0]["quote"] = "fabricated evidence not present in the source"
                    return self.send({"choices":[{"message":{"content":json.dumps(output,ensure_ascii=False)},"finish_reason":"stop"}],
                                      "usage":{"prompt_tokens":10,"completion_tokens":20}})
                if path.endswith("/chat"):
                    outer.chat_requests.append(payload)
                    found = self.sources(payload["kb_id"], payload["question"])
                    found.sort(key=lambda item: 0 if (item.get("file") or {}).get("filename") == "answer_plan.txt" else 1)
                    if "密舟-" in payload["question"] or not found:
                        found, answer = [], "资料不足，没有可访问的资料，无法提供该信息。"
                    else:
                        # Return the plan's actual facts as a model stub. The
                        # independent judge below is also a stub: these tests
                        # verify wiring and evidence validation, not LLM quality.
                        answer = found[0]["content"] + "[1]"
                    sources = [{"id":i + 1,"result":value} for i,value in enumerate(found)]
                    if outer.mode == "invalid_citation":
                        answer += "[999]"
                    raw = b": alive\r\n\r\n" + event("status",{"stage":"thinking"})
                    if outer.mode == "cumulative_sources":
                        raw += event("sources",{"sources":sources[:1]})
                        raw += event("delta",{"text":"开始回答："})
                        raw += event("status",{"stage":"searching"})
                    raw += event("sources",{"sources":sources},"\r\n")
                    if outer.mode == "source_overwrite" and sources:
                        changed = copy.deepcopy(sources)
                        changed[0]["result"]["content"] = "changed"
                        raw += event("sources",{"sources":changed})
                    raw += event("delta",{"text":answer})
                    if outer.mode != "truncated":
                        raw += event("done",{"finish_reason":"stop"})
                    return self.send(raw,content_type="text/event-stream")
                return self.send({},404)
        self.server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = MockService()
    @classmethod
    def tearDownClass(cls):
        cls.service.close()
    def setUp(self):
        self.service.mode = "normal"
        self.service.kbs.clear()
        self.service.files.clear()
        self.service.pages.clear()
        self.service.chat_requests.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def run_cli(self, extra=(), manifest=None):
        command = [sys.executable,str(ROOT / "scripts/e2e_retrieval_test.py"),"--target",self.service.base,
                   "--mode","chat","--answer-limit","2","--answer-seed","test-seed",
                   "--corpus-cache",str(self.directory / "cache"),"--parse-timeout","1","--poll-interval","0.01",
                   "--report",str(self.directory / "report.html"),"--json-report",str(self.directory / "report.json"),
                   "--judge-url",self.service.base + "/judge","--judge-model","offline-stub"]
        if manifest is not None:
            path = self.directory / "cases.json"
            path.write_text(json.dumps(manifest,ensure_ascii=False))
            command += ["--answer-cases",str(path)]
        env = dict(os.environ,E2E_JUDGE_API_KEY="judge-test-secret")
        completed = subprocess.run([*command,*extra],env=env,capture_output=True,text=True,timeout=30)
        report = json.loads((self.directory / "report.json").read_text()) if (self.directory / "report.json").exists() else None
        return completed,report
    def test_chat_cli_generates_evidence_report_and_cleans_after_semantic_pass(self):
        completed,report = self.run_cli(["--cleanup","delete-on-pass"])
        self.assertEqual(completed.returncode,0,completed.stdout + completed.stderr)
        self.assertTrue(report["overall_pass"])
        self.assertEqual(report["answers"]["metrics"]["judge_coverage"],100)
        self.assertEqual(report["answers"]["metrics"]["planned_turns"],2)
        self.assertFalse(self.service.kbs)
        self.assertTrue(list(self.directory.rglob("stream.sse")))
        self.assertTrue(list(self.directory.rglob("judge-grounding-response.json")))
        rendered = (self.directory / "report.html").read_text()
        self.assertIn("逐题复盘",rendered)
        self.assertIn("参考答案要点",rendered)
        for path in self.directory.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"judge-test-secret",path.read_bytes(),str(path))
    def test_rule_only_does_not_pass_semantic_gates(self):
        completed,report = self.run_cli(["--judge-mode","rules"])
        self.assertEqual(completed.returncode,1,completed.stdout + completed.stderr)
        self.assertEqual(report["answers"]["semantic_status"],"not_evaluated")
        self.assertEqual(report["answers"]["metrics"]["judge_coverage"],0)
        self.assertFalse(report["overall_pass"])
        self.assertTrue(self.service.kbs,"failed run must retain the test KB by default")

    def test_rejudge_uses_immutable_answers_without_calling_htknow_or_overwriting_history(self):
        completed, original = self.run_cli(["--cleanup", "keep"])
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        source = self.directory / "report.json"
        source_bytes = source.read_bytes()
        chat_count, files, kbs = len(self.service.chat_requests), copy.deepcopy(self.service.files), copy.deepcopy(self.service.kbs)
        output = self.directory / "rejudge" / "report.json"
        command = [sys.executable, str(ROOT / "scripts/e2e_retrieval_test.py"), "--target", "http://127.0.0.1:1",
                   "--rejudge-report", str(source), "--report", str(output.with_suffix(".html")),
                   "--json-report", str(output), "--judge-url", self.service.base + "/judge", "--judge-model", "offline-stub"]
        result = subprocess.run(command, env=dict(os.environ, E2E_JUDGE_API_KEY="judge-test-secret"),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(len(self.service.chat_requests), chat_count)
        self.assertEqual(self.service.files, files)
        self.assertEqual(self.service.kbs, kbs)
        self.assertEqual([r["response"] for r in report["answers"]["rows"]], [r["response"] for r in original["answers"]["rows"]])
        self.assertEqual(report["answers"]["metrics"]["judge_coverage"], 100)
        self.assertEqual(report["answers"]["seed"], original["answers"]["seed"])
        self.assertEqual(report["config"]["evaluation_type"], "rejudge")
        self.assertIn("对话答案复评报告", output.with_suffix(".html").read_text())

    def test_rejudge_errors_only_preserves_scored_rows_and_records_inherited_version(self):
        self.service.mode = "owner_judge_failure"
        completed, original = self.run_cli(["--cleanup", "keep"])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual([r["judge"]["status"] for r in original["answers"]["rows"]], ["scored", "error"])
        self.service.mode = "normal"
        output = self.directory / "error-rejudge" / "report.json"
        result = subprocess.run([sys.executable, str(ROOT / "scripts/e2e_retrieval_test.py"),
            "--rejudge-report", str(self.directory / "report.json"), "--rejudge-errors-only",
            "--report", str(output.with_suffix(".html")), "--json-report", str(output),
            "--judge-url", self.service.base + "/judge", "--judge-model", "offline-stub"],
            env=dict(os.environ, E2E_JUDGE_API_KEY="judge-test-secret"), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(output.read_text())
        answers = report["answers"]
        self.assertEqual(answers["metrics"]["judge_coverage"], 100)
        self.assertEqual(answers["rejudge"]["reviewed_turns"], 1)
        self.assertEqual(answers["rejudge"]["inherited_turns"], 1)
        self.assertFalse(answers["rejudge"]["all_turns"])
        self.assertEqual(answers["rows"][0]["judge"], original["answers"]["rows"][0]["judge"])
        self.assertEqual(answers["rows"][0]["inherited_judge"], original["answers"]["judge"])
        self.assertEqual(answers["judge"]["requests"], 2)
        self.assertEqual(len(self.service.chat_requests), 2)

    def test_invalid_judge_quote_gets_one_recorded_correction(self):
        self.service.mode = "quote_repair"
        completed,report = self.run_cli()
        self.assertEqual(completed.returncode,0,completed.stdout + completed.stderr)
        self.assertEqual(report["answers"]["judge"]["requests"],6)
        row=report["answers"]["rows"][0]
        self.assertEqual(row["judge"]["repairs"][0]["stage"],"grounding")
        self.assertEqual(len(row["judge"]["usage"]),3)
        directory=Path(row["artifact_dir"])
        original=json.loads((directory/"judge-grounding-response.json").read_text())
        self.assertIn("fabricated evidence",original["result"]["claims"][0]["support"][0]["quote"])
        self.assertTrue((directory/"judge-grounding-repair-request.json").exists())
        self.assertTrue((directory/"judge-grounding-repair-response.json").exists())
        request=json.loads((directory/"judge-grounding-repair-request.json").read_text())
        feedback=json.loads(request["messages"][1]["content"])["validation_feedback"]
        self.assertEqual(feedback["details"]["path"],"claims[0].support[0].quote")
        self.assertEqual(feedback["details"]["source_id"],1)
        self.assertEqual(row["judge"]["repairs"][0]["details"],feedback["details"])

    def test_unverifiable_quote_still_fails_after_single_correction(self):
        self.service.mode = "unrepairable_quote"
        completed,report = self.run_cli()
        self.assertEqual(completed.returncode,1,completed.stdout + completed.stderr)
        self.assertFalse(report["overall_pass"])
        self.assertEqual(report["answers"]["judge"]["requests"],6)
        self.assertEqual(report["answers"]["metrics"]["judge_coverage"],0)
        self.assertIn("一次校正后仍未通过",report["answers"]["rows"][0]["error"])
        details=report["answers"]["rows"][0]["judge"]["validation_details"]
        self.assertEqual(details["path"],"claims[0].support[0].quote")
        self.assertEqual(details["source_id"],1)

    def test_multiple_searches_preserve_cumulative_sources_and_interleaved_text(self):
        self.service.mode = "cumulative_sources"
        completed,report = self.run_cli()
        self.assertEqual(completed.returncode,0,completed.stdout + completed.stderr)
        response = report["answers"]["rows"][0]["response"]
        versions = [item["data"]["sources"] for item in response["events"] if item["event"] == "sources"]
        self.assertEqual(len(versions),2)
        self.assertEqual(len(versions[0]),1)
        self.assertGreater(len(versions[1]),1)
        self.assertEqual(versions[0][0],versions[1][0])
        self.assertTrue(response["answer"].startswith("开始回答："))
    def test_truncation_judge_errors_and_parse_failures_remain_in_denominator(self):
        for mode in ["truncated","judge_failure","parse_failure","source_overwrite","invalid_citation"]:
            with self.subTest(mode=mode):
                self.service.mode = mode
                completed,report = self.run_cli()
                self.assertEqual(completed.returncode,1,completed.stdout + completed.stderr)
                self.assertFalse(report["overall_pass"])
                self.assertEqual(report["answers"]["metrics"]["planned_turns"],2)
                if mode != "invalid_citation":
                    self.assertEqual(report["answers"]["metrics"]["judge_coverage"],0)
                if mode == "parse_failure":
                    self.assertEqual(report["parse"]["success_rate"],0)
    def test_multiturn_sends_actual_answer_and_a_failed_first_turn_blocks_followup(self):
        manifest = json.loads(answers.DEFAULT_CASES.read_text())
        manifest["cases"] = [next(c for c in manifest["cases"] if c["category"] == "multi_turn")]
        completed,report = self.run_cli(manifest=manifest)
        self.assertEqual(completed.returncode,0,completed.stdout + completed.stderr)
        requests = self.service.chat_requests
        self.assertEqual(len(requests),2)
        self.assertEqual(requests[0]["messages"],[])
        self.assertEqual(requests[1]["messages"][1]["content"],report["answers"]["rows"][0]["response"]["answer"])
        self.service.chat_requests.clear()
        self.service.mode = "truncated"
        completed,report = self.run_cli(manifest=manifest)
        self.assertEqual(completed.returncode,1)
        self.assertEqual(len(self.service.chat_requests),1)
        self.assertIn("前一轮",report["answers"]["rows"][1]["error"])
    def test_wiki_and_permission_fixtures_and_owner_positive_control(self):
        manifest = json.loads(answers.DEFAULT_CASES.read_text())
        manifest["cases"] = [next(c for c in manifest["cases"] if c["category"] == "wiki"),
                             next(c for c in manifest["cases"] if c["id"] == "permission-1")]
        completed,report = self.run_cli(manifest=manifest)
        self.assertEqual(completed.returncode,0,completed.stdout + completed.stderr)
        self.assertEqual(len(report["answers"]["fixture_dispositions"]),2)
        self.assertFalse(self.service.kbs)
        self.assertTrue(any(s["result"].get("wiki") for s in report["answers"]["rows"][0]["response"]["sources"]))
        self.assertEqual(report["answers"]["rows"][1]["response"]["sources"],[])
    def test_complex_cases_use_an_isolated_kb_and_clean_up_after_pass(self):
        completed,report = self.run_cli(["--answer-categories","complex_reasoning","--cleanup","delete-on-pass"])
        self.assertEqual(completed.returncode,0,completed.stdout + completed.stderr)
        scopes = report["answers"]["fixture_scopes"]
        self.assertNotEqual(scopes["complex"]["kb_id"],scopes["main"]["kb_id"])
        self.assertEqual({r["kb_id"] for r in self.service.chat_requests},{scopes["complex"]["kb_id"]})
        for file in self.service.files.values():
            expected = "complex" if file["filename"].startswith("complex_") else "main"
            self.assertEqual(file["kb_id"],scopes[expected]["kb_id"])
        self.assertEqual(sum(f["filename"].startswith("complex_") for f in self.service.files.values()),7)
        self.assertFalse(self.service.kbs)

    def test_complex_fixture_failures_keep_all_planned_turns_without_calling_chat(self):
        for mode in ["complex_parse_failure","complex_unsearchable"]:
            with self.subTest(mode=mode):
                self.service.mode = mode
                completed,report = self.run_cli(["--answer-categories","complex_reasoning","--cleanup","delete"])
                self.assertEqual(completed.returncode,1,completed.stdout + completed.stderr)
                metrics = report["answers"]["metrics"]
                self.assertEqual(metrics["planned_turns"],2)
                self.assertEqual(metrics["completed_turns"],0)
                self.assertEqual(metrics["judge_coverage"],0)
                self.assertTrue(report["answers"]["fixture_scopes"]["complex"]["error"])
                self.assertTrue(all(row["error"] for row in report["answers"]["rows"]))
                self.assertFalse(self.service.chat_requests)
                self.assertFalse(self.service.kbs)
    def test_invalid_manifest_fails_before_creating_any_knowledge_base(self):
        manifest = json.loads(answers.DEFAULT_CASES.read_text())
        manifest["cases"][0]["id"] = "../../escape"
        completed,report = self.run_cli(manifest=manifest)
        self.assertEqual(completed.returncode,2,completed.stdout + completed.stderr)
        self.assertIsNone(report)
        self.assertFalse(self.service.kbs)

    def test_complete_mode_shares_one_main_kb_and_retains_retrieval_gates(self):
        completed,report = self.run_cli(["--mode","both","--docs","needle_synthetic","--offline"])
        self.assertEqual(completed.returncode,0,completed.stdout + completed.stderr)
        self.assertEqual(report["config"]["mode"],"both")
        self.assertEqual(report["metrics"]["hit_rate"],100)
        self.assertEqual(report["answers"]["metrics"]["correctness"],100)
        self.assertTrue(any(g["metric"] == "query_coverage" for g in report["gates"]))
        self.assertFalse(self.service.kbs)

    def test_default_retrieval_cli_does_not_require_a_judge(self):
        completed,report = self.run_cli(["--mode","retrieval","--docs","needle_synthetic","--offline"])
        self.assertEqual(completed.returncode,0,completed.stdout + completed.stderr)
        self.assertNotIn("answers",report)
        self.assertEqual(report["metrics"]["query_coverage"],100)

    def test_report_escapes_model_html(self):
        completed,report = self.run_cli(["--judge-mode","rules"])
        report["answers"]["rows"][0]["response"]["answer"] = '<script>alert("x")</script>'
        rendered = harness.render_html(report)
        self.assertNotIn('<script>alert(',rendered)
        self.assertIn('&lt;script&gt;',rendered)

    def test_web_api_runs_chat_and_exposes_summary_and_full_report(self):
        manager = web.RunManager(self.directory / "web-runs",2)
        previous = web.MANAGER
        web.MANAGER = manager
        server = ThreadingHTTPServer(("127.0.0.1",0),web.Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            payload = {"target":self.service.base,"mode":"chat","answer_limit":1,
                       "judge_url":self.service.base + "/judge","judge_model":"offline-stub",
                       "corpus_cache":str(self.directory / "web-cache")}
            request = urllib.request.Request(base + "/api/runs",data=json.dumps(payload).encode(),
                                             headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(request,timeout=3) as response:
                run_id = json.load(response)["id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with urllib.request.urlopen(base + "/api/runs/" + run_id,timeout=3) as response:
                    run = json.load(response)
                if run["status"] != "running":
                    break
                time.sleep(0.05)
            self.assertEqual(run["status"],"pass",run)
            self.assertEqual(run["metrics"]["answers"]["metrics"]["judge_coverage"],100)
            # Historical runs use the new presentation without rewriting saved data.
            saved = manager.get(run_id)
            saved.report_html.write_text("legacy report presentation")
            original_json = saved.report_json.read_bytes()
            with urllib.request.urlopen(base + "/api/runs/" + run_id + "/report",timeout=3) as response:
                rendered = response.read().decode()
                self.assertIn("生成答案测试报告",rendered)
                self.assertIn("逐题复盘",rendered)
                self.assertNotIn("legacy report presentation",rendered)
            self.assertEqual(saved.report_json.read_bytes(),original_json)
            self.assertEqual(saved.report_html.read_text(),"legacy report presentation")
            chat_count = len(self.service.chat_requests)
            request = urllib.request.Request(base + "/api/runs/" + run_id + "/rejudge", data=b'{}',
                                             headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(request, timeout=3) as response:
                replay_id = json.load(response)["id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with urllib.request.urlopen(base + "/api/runs/" + replay_id, timeout=3) as response:
                    replay = json.load(response)
                if replay["status"] != "running":
                    break
                time.sleep(0.05)
            self.assertEqual(replay["status"], "pass", replay)
            self.assertEqual(replay["params"]["rejudge_of"], run_id)
            self.assertEqual(len(self.service.chat_requests), chat_count)
            self.assertEqual(saved.report_json.read_bytes(), original_json)
            with urllib.request.urlopen(base + "/api/runs/" + replay_id + "/report", timeout=3) as response:
                self.assertIn("对话答案复评报告", response.read().decode())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            manager.shutdown()
            web.MANAGER = previous


class WebIntegrationTests(unittest.TestCase):
    def test_options_reach_the_cli_and_nonfinite_values_are_rejected(self):
        params = web.sanitize_params({"mode":"both","judge_mode":"rules","answer_categories":"facts,multi_turn",
                                      "answer_limit":2,"answer_seed":"stable"})
        argv = web.build_argv("localhost:11000",params,Path("/tmp/example-run"))
        self.assertEqual(argv[argv.index("--mode") + 1],"both")
        self.assertEqual(argv[argv.index("--answer-limit") + 1],"2")
        for value in [float("nan"),float("inf"),"-inf"]:
            with self.assertRaises(ValueError):
                web.sanitize_params({"answer_timeout":value})
    def test_missing_required_document_is_a_parse_failure(self):
        args = harness.parse_args([])
        doc = harness.DocResult(doc_id="missing",filename="missing.txt",fmt="txt",language="zh",optional=False,stage="download_failed")
        _,parse,_ = harness.summarize(args,[doc],[],[],[])
        self.assertEqual(parse["evaluated_required"],1)
        self.assertEqual(parse["success_rate"],0)

    def test_public_web_cannot_forward_server_judge_key_to_another_endpoint(self):
        manager = object.__new__(web.RunManager)
        with patch.dict(os.environ,{"E2E_JUDGE_URL":"https://trusted.example/v1", "E2E_JUDGE_API_KEY":"secret"}):
            with self.assertRaisesRegex(ValueError,"密钥"):
                manager.start({"target":"localhost:11000","mode":"chat","judge_url":"https://untrusted.example/v1",
                               "judge_model":"model"})

    def test_builtin_dataset_has_80_cases_100_turns_and_reproducible_facts(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            args = harness.parse_args(["--mode","chat","--answer-seed","stable"])
            first = answers.prepare_suite(args,directory,"run-one",harness)
            second = answers.prepare_suite(args,directory,"run-two",harness)
            self.assertEqual(len(first["cases"]),80)
            self.assertEqual(sum(len(c["turns"]) for c in first["cases"]),100)
            self.assertEqual(first["cases"],second["cases"])
            self.assertEqual(len({c["category"] for c in first["cases"]}),9)


if __name__ == "__main__":
    unittest.main()

"""Report semantics, safe rich text, and preservation of historical artifacts."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e2e_answer_report as report
import e2e_web as web


def case():
    return {"case_id": "fact-01", "question": "负责人是谁？", "category": "facts", "repeat": 1, "turn": 1,
            "passed": False, "error": None, "diagnosis": "待评审", "facts": [{"id": "owner", "description": "负责人为林岚。"}],
            "response": {"complete": True, "answer": "负责人为 **林岚** [1]。", "sources": [
                {"id": 1, "result": {"content": "负责人为林岚。", "file": {"filename": "plan.txt"}}}]},
            "rules": {"passed": True}, "judge": {"status": "error", "error": "评审引句校验失败"}}


class Elements(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.items = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.items.append((tag, dict(attrs)))


class AnswerReportTests(unittest.TestCase):
    def test_judge_errors_and_rules_only_are_not_classified_as_answer_errors(self):
        row = case()
        self.assertEqual(report.case_state(row), "judge")
        self.assertIn("评审异常", report.case_badge(row))
        row["judge"] = {"status": "not_evaluated"}
        self.assertEqual(report.case_state(row), "judge")
        self.assertIn("语义未评估", report.case_badge(row))
        row["rules"]["passed"] = False
        self.assertEqual(report.case_state(row), "fail")
        row["response"]["complete"] = False
        self.assertEqual(report.case_state(row), "error")

    def test_markdown_and_raw_model_html_remain_safe_with_citations(self):
        row = case()
        rendered = report.prose('<img src=x onerror=alert(1)> **林岚** [1][999]\n`[1]`\n```html\n<script>alert(1)</script>\n```',
                                {"1": row["response"]["sources"][0]})
        nodes = Elements(rendered).items
        self.assertFalse(any(tag in ("img", "script") for tag, _ in nodes))
        self.assertIn("<strong>林岚</strong>", rendered)
        self.assertEqual(sum(tag == "button" and a.get("data-source") == "1" for tag, a in nodes), 1)
        self.assertIn("ar-invalid", rendered)
        self.assertIn("<code>[1]</code>", rendered)
        self.assertNotIn("ar-invalid", report.prose("上一轮回答 [1]"))

    def test_sources_are_scoped_to_each_turn_and_quotes_mark_only_actual_text(self):
        first = report.render_case(case(), "answer-case-0", True)
        second = report.render_case(case(), "answer-case-1", False)
        self.assertIn('id="answer-case-0-source-1"', first)
        self.assertIn('id="answer-case-1-source-1"', second)
        self.assertNotIn('id="answer-case-0-source-1"', second)
        rendered = report.marked_source("<证据>负责人为林岚。", ["负责人为林岚", "不存在的引句", "林岚"])
        self.assertIn("&lt;证据&gt;<mark>负责人为林岚</mark>", rendered)
        self.assertNotIn("不存在的引句", rendered)
        self.assertEqual(rendered.count("<mark>"), 1)

    def test_failed_review_defaults_to_actionable_cases_and_never_fakes_a_score(self):
        passed = case()
        passed["passed"] = True
        unjudged = case()
        problem = case()
        problem["judge"] = {"status": "scored", "accuracy": {"facts": [{"id": "owner", "label": "incorrect"}]}}
        value = {"rows": [passed, unjudged, problem], "semantic_status": "incomplete", "metrics": {"correctness": None}}
        rendered = report.render_section(value)
        root = next(a for tag, a in Elements(rendered).items if a.get("id") == "answer-review")
        self.assertEqual(root["data-active"], "answer-case-2")
        self.assertEqual(root["data-initial-filter"], "issues")
        self.assertIn("未评估", rendered)
        self.assertIn("不能直接认定为答案内容错误", rendered)
        self.assertNotIn("0.0%", rendered)
        self.assertIn("未评审", report.render_case(unjudged, "answer-case-1", True))

    def test_all_pass_and_empty_reports_have_valid_initial_states(self):
        row = case()
        row["passed"] = True
        value = {"rows": [row], "metrics": {}, "semantic_status": "complete"}
        for data, active in [(value, "answer-case-0"), ({"rows": [], "metrics": {}}, "")]:
            rendered = report.render_section(data)
            root = next(a for tag, a in Elements(rendered).items if a.get("id") == "answer-review")
            self.assertEqual(root["data-initial-filter"], "all")
            self.assertEqual(root["data-active"], active)
            ids = [a["id"] for _, a in Elements(rendered).items if "id" in a]
            self.assertEqual(len(ids), len(set(ids)))

    def test_old_html_is_preserved_when_json_is_missing_invalid_or_retrieval_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = SimpleNamespace(report_html=root / "report.html", report_json=root / "report.json")
            run.report_html.write_bytes(b"saved presentation")
            self.assertEqual(web.read_report_html(run), b"saved presentation")
            for raw in ("{partial", '{"config":{"mode":"retrieval"}}', "[]"):
                run.report_json.write_text(raw)
                self.assertEqual(web.read_report_html(run), b"saved presentation")


if __name__ == "__main__":
    unittest.main()

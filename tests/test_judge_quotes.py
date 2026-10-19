"""Conservative list-quote matching and actionable bounded repair feedback."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e2e_judge as judging


def source():
    return {"id": 2, "result": {"content": "负责人为林岚。"}}


def payload(quote="负责人为林岚。"):
    return {"claims": [{"claim": "负责人为林岚", "answer_quote": "负责人为林岚[2]。",
            "verdict": "supported", "support": [{"source_id": 2, "quote": quote}],
            "citations": [{"source_id": 2, "supported": True, "quote": "负责人为林岚。"}]}]}


class ListQuoteTests(unittest.TestCase):
    def test_fact04_adjacent_bullets_reanchor_to_the_original_continuous_span(self):
        first = "旧计划日期（2026年5月7日）已作废。"
        second = "当前以本文件规定的计划上线日期（2027年5月7日）为准。"
        quote = first + second
        for marker in ("*   ", "- ", "+ "):
            for newline in ("\n", "\r\n"):
                with self.subTest(marker=marker, newline=newline):
                    original = "### 版本说明" + newline + marker + first + newline + marker + second
                    matched = judging._quote(quote, original, "source quote")
                    self.assertIn(matched, original)
                    self.assertEqual(matched, marker + first + newline + marker + second)

    def test_list_formatting_and_emphasis_can_change_without_changing_facts(self):
        original = "- **负责人**：林岚。\n- **部署区域**：华北三区。"
        for quote in ("负责人：林岚。部署区域：华北三区。", "负责人：林岚。 部署区域：华北三区。",
                      "* 负责人：林岚。\n* 部署区域：华北三区。"):
            with self.subTest(quote=quote):
                self.assertEqual(judging._quote(quote, original, "source quote"), original)
        original = "- owner: Lin.\n- region: North."
        self.assertEqual(judging._quote("owner: Lin. region: North.", original, "source quote"), original)

    def test_numbers_words_and_intervening_content_cannot_be_removed_or_fused(self):
        for quote, original in [
            ("12", "* 1\n* 2"),
            ("1.5", "* 1.\n* 5"),
            ("foobar", "* foo\n* bar"),
            ("预算为15万元。负责人林岚。", "* 预算为150万元。\n* 负责人林岚。"),
            ("负责人是林岚。部署区域华北。", "* 负责人不是林岚。\n* 部署区域华北。"),
            ("A。C。", "* A。\n* B。\n* C。"),
            ("A。B。", "* A。\n注意：下列计划已作废。\n* B。"),
            ("A。B。", "* A。\n### 其他项目\n* B。"),
            ("A。B。", "* A。\n\n* B。"),
            ("A。B。", "* A。\n  * B。"),
            ("A。B。", "1. A。\n2. B。"),
            ("答案[2]。下一句。", "* 答案[3]。\n* 下一句。"),
        ]:
            with self.subTest(quote=quote, original=original):
                with self.assertRaises(judging.JudgeError):
                    judging._quote(quote, original, "source quote")

    def test_literal_markers_in_code_and_escaped_text_remain_significant(self):
        for original in [
            "\\* A。\n\\* B。",
            "    * A。\n    * B。",
            "```text\n* A。\n* B。\n```",
            "~~~\n* A。\n* B。\n~~~~",
            "```\n* A。\n* B。",
            "* `A。`\n* B。",
        ]:
            with self.subTest(original=original):
                with self.assertRaises(judging.JudgeError):
                    judging._quote("A。B。", original, "source quote")

    def test_reanchoring_does_not_change_which_citations_are_attached(self):
        answer = "* 负责人林岚[2]。\n* 部署区域华北[3]。"
        quoted = "负责人林岚[2]。部署区域华北[3]。"
        result = {"claims": [{"claim": "负责人和部署区域", "answer_quote": quoted, "verdict": "supported",
                              "support": [{"source_id": 2, "quote": "负责人为林岚。"}],
                              "citations": [{"source_id": 2, "supported": True, "quote": "负责人为林岚。"}]}]}
        with self.assertRaisesRegex(judging.JudgeError, "引用评审未覆盖"):
            judging.validate_grounding(result, [source()], answer, True)


class RepairFeedbackTests(unittest.TestCase):
    def test_quote_errors_identify_field_source_and_rejected_quote(self):
        bad = payload("捏造的原文")
        with self.assertRaises(judging.JudgeError) as raised:
            judging.validate_grounding(bad, [source()], "负责人为林岚[2]。", True)
        self.assertEqual(raised.exception.details["path"], "claims[0].support[0].quote")
        self.assertEqual(raised.exception.details["source_id"], 2)
        self.assertEqual(raised.exception.details["rejected_quote"], "捏造的原文")
        self.assertIn("来源 [2]", str(raised.exception))
        bad = payload()
        bad["claims"][0]["citations"][0]["quote"] = "另一个不存在的引句"
        with self.assertRaises(judging.JudgeError) as raised:
            judging.validate_grounding(bad, [source()], "负责人为林岚[2]。", True)
        self.assertEqual(raised.exception.details["path"], "claims[0].citations[0].quote")
        self.assertEqual(raised.exception.details["source_id"], 2)

    def test_single_repair_receives_structured_context_and_preserves_original_result(self):
        judge = judging.Judge(mode="rules")
        invalid, valid = payload("fabricated evidence"), payload()
        original = copy.deepcopy(invalid)
        responses = [{"result": invalid, "usage": {"total_tokens": 1}, "elapsed_ms": 1},
                     {"result": valid, "usage": {"total_tokens": 2}, "elapsed_ms": 2}]
        with tempfile.TemporaryDirectory() as directory, patch.object(judge, "_call", side_effect=responses) as call:
            result = judge._validated_call("grounding", judging.GROUNDING_PROMPT, {"answer": "负责人为林岚[2]。"},
                Path(directory), lambda data: judging.validate_grounding(data, [source()], "负责人为林岚[2]。", True))
        self.assertEqual(call.call_count, 2)
        feedback = call.call_args_list[1].args[2]["validation_feedback"]
        self.assertEqual(feedback["previous_result"], original)
        self.assertEqual(feedback["details"]["path"], "claims[0].support[0].quote")
        self.assertEqual(feedback["details"]["source_id"], 2)
        self.assertEqual(result["repair"]["details"], feedback["details"])
        self.assertEqual(result["elapsed_ms"], 3)
        self.assertEqual(result["repair_usage"], [{"total_tokens": 2}])

    def test_final_failure_retains_details_and_still_stops_after_one_repair(self):
        judge = judging.Judge(mode="rules")
        with tempfile.TemporaryDirectory() as directory, patch.object(judge, "_call", side_effect=[
            {"result": payload("捏造引句一"), "usage": None, "elapsed_ms": 1},
            {"result": payload("捏造引句二"), "usage": None, "elapsed_ms": 1},
        ]) as call:
            with self.assertRaises(judging.JudgeError) as raised:
                judge._validated_call("grounding", judging.GROUNDING_PROMPT, {}, Path(directory),
                    lambda data: judging.validate_grounding(data, [source()], "负责人为林岚[2]。", True))
        self.assertEqual(call.call_count, 2)
        self.assertIn("一次校正后仍未通过", str(raised.exception))
        self.assertEqual(raised.exception.details["source_id"], 2)
        self.assertEqual(raised.exception.details["rejected_quote"], "捏造引句二")


if __name__ == "__main__":
    unittest.main()

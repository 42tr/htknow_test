"""Regression checks for citation attachment and immutable evidence selection."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e2e_judge as judging


SOURCE = {"id": 1, "result": {"content": "## 财务核准记录\n\n基础预算：112万元。\n独立预备金：36万元。\n\n### 执行信息\n采购负责人：顾宁。"}}


def claim():
    return {"claim": "基础预算为112万元", "answer_span_ids": [1], "verdict": "supported",
            "support": [{"source_id": 1, "passage_ids": [2]}],
            "citations": [{"source_id": 1, "supported": True, "passage_ids": [2]}]}


class GroundingAlignmentTests(unittest.TestCase):
    def test_malformed_model_json_gets_one_recorded_format_repair_with_usage(self):
        answer = "基础预算112万元[1]。"
        opener = MagicMock()
        envelopes = [{"choices": [{"finish_reason": "stop", "message": {"content": '{"claims":['}}],
                      "usage": {"total_tokens": 3}},
                     {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"claims": [claim()]})}}],
                      "usage": {"total_tokens": 5}}]
        opener.open.return_value.__enter__.return_value.read.side_effect = [json.dumps(e).encode() for e in envelopes]
        judge = judging.Judge(mode="rules", url="http://127.0.0.1:1/v1", model="mock")
        with tempfile.TemporaryDirectory() as directory, patch.object(judging.urllib.request, "build_opener", return_value=opener):
            result = judge._validated_call("grounding", judging.GROUNDING_PROMPT, {}, Path(directory),
                lambda p: judging.validate_grounding(p, [SOURCE], answer, True))
            original = json.loads((Path(directory) / "judge-grounding-response.json").read_text())
            repaired_request = json.loads((Path(directory) / "judge-grounding-repair-request.json").read_text())
        self.assertEqual(judge.request_count, 2)
        self.assertEqual(original["result"], {"invalid_model_json": '{"claims":['})
        feedback = json.loads(repaired_request["messages"][1]["content"])["validation_feedback"]
        self.assertEqual(feedback["previous_result"], original["result"])
        self.assertEqual(result["usage"], {"total_tokens": 3})
        self.assertEqual(result["repair_usage"], [{"total_tokens": 5}])

    def test_real_citation_is_checked_even_when_quote_transcription_would_omit_it(self):
        answer = "基础预算为112万元 [1]。独立预备金为36万元 [2]。"
        spans = judging.answer_spans(answer)
        self.assertEqual([s["citation_ids"] for s in spans], [[1], [2]])
        item = claim()
        result = judging.validate_grounding({"claims": [item, {
            "claim": "预备金36万元", "answer_span_ids": [2], "verdict": "supported",
            "support": [{"source_id": 1, "passage_ids": [3]}],
            "citations": [{"source_id": 2, "supported": False}]}]}, [SOURCE], answer, True)
        self.assertEqual(result["claims"][0]["answer_quote"], spans[0]["text"])
        self.assertFalse(result["claims"][1]["citations"][0]["supported"])

    def test_uncited_summary_cannot_account_for_later_cited_occurrences(self):
        item = claim(); item["citations"] = []
        answer = "基础预算为112万元。\n相关记录：\n* 基础预算112万元[1]。"
        with self.assertRaises(judging.JudgeError) as error:
            judging.validate_grounding({"claims": [item]}, [SOURCE], answer, True)
        details = error.exception.details
        self.assertEqual(details["missing_source_ids"], [1])
        self.assertEqual(details["uncovered_answer_spans"], [{"id": 3, "text": "* 基础预算112万元[1]。", "citation_ids": [1]}])

    def test_accuracy_uses_original_span_instead_of_a_paraphrased_quote(self):
        answer = "当前容量为905个任务。\n旧容量778个任务已作废。"
        payload = {"facts": [{"id": "capacity", "label": "correct", "answer_span_ids": [1], "answer_quote": "容量是905"}],
                   "behavior": "answer", "behavior_ok": True, "task_ok": True}
        result = judging.validate_accuracy(payload, [{"id": "capacity"}], answer)
        self.assertEqual(result["facts"][0]["answer_quote"], "当前容量为905个任务。")

    def test_repair_handles_missing_citation_checks_and_omitted_occurrences_together(self):
        answer = "基础预算112万元[1]。\n基础预算112万元[2]。"
        first = claim(); first["citations"] = []
        second = copy.deepcopy(first); second["answer_span_ids"] = [2]
        second["citations"] = [{"source_id": 2, "supported": False}]
        fixed = claim()
        judge = judging.Judge(mode="rules")
        with tempfile.TemporaryDirectory() as directory, patch.object(judge, "_call", side_effect=[
            {"result": {"claims": [first]}, "usage": None, "elapsed_ms": 1},
            {"result": {"claims": [fixed, second]}, "usage": None, "elapsed_ms": 1},
        ]) as call:
            result = judge._validated_call("grounding", judging.GROUNDING_PROMPT,
                {"answer": answer, "sources": judging.visible_sources([SOURCE])}, Path(directory),
                lambda p: judging.validate_grounding(p, [SOURCE], answer, True))
        feedback = call.call_args_list[1].args[2]["validation_feedback"]
        self.assertEqual(feedback["details"]["uncovered_answer_spans"][0]["id"], 2)
        self.assertNotIn("method", result["repair"])
        self.assertFalse(result["result"]["claims"][1]["citations"][0]["supported"])

    def test_citation_repair_must_not_hide_a_second_claim_without_evidence(self):
        answer = "基础预算112万元[1]。\n当前未检索到负责人出生年份。"
        first = claim(); first["citations"] = []
        limitation = {"claim": "未检索到出生年份", "answer_span_ids": [2], "verdict": "supported", "support": [], "citations": []}
        judge = judging.Judge(mode="rules")
        with tempfile.TemporaryDirectory() as directory, patch.object(judge, "_call", side_effect=[
            {"result": {"claims": [first, limitation]}, "usage": None, "elapsed_ms": 1},
            {"result": {"checks": [{"id": "0:1", "supported": False, "passage_ids": []}],
                        "information_limit_checks": [{"claim_index": 1, "is_information_limit": True}]},
             "usage": None, "elapsed_ms": 1},
        ]) as call:
            result = judge._validated_call("grounding", judging.GROUNDING_PROMPT,
                {"answer": answer, "sources": judging.visible_sources([SOURCE])}, Path(directory),
                lambda p: judging.validate_grounding(p, [SOURCE], answer, True))
        request = call.call_args_list[1].args[2]
        self.assertEqual(request["information_limit_checks"][0]["claim_index"], 1)
        self.assertEqual(result["result"]["information_limits"][0]["answer_quote"], "当前未检索到负责人出生年份。")
        self.assertFalse(result["result"]["claims"][0]["citations"][0]["supported"])
        self.assertEqual(call.call_count, 2)

    def test_positive_fact_without_evidence_cannot_be_exempted_by_negative_classification(self):
        item = claim(); item.update(support=[], citations=[])
        judge = judging.Judge(mode="rules")
        with tempfile.TemporaryDirectory() as directory, patch.object(judge, "_call", side_effect=[
            {"result": {"claims": [item]}, "usage": None, "elapsed_ms": 1},
            {"result": {"checks": [], "information_limit_checks": [{"claim_index": 0, "is_information_limit": False}]},
             "usage": None, "elapsed_ms": 1},
        ]) as call, self.assertRaisesRegex(judging.JudgeError, "缺少证据引句"):
            judge._validated_call("grounding", judging.GROUNDING_PROMPT,
                {"answer": "基础预算112万元。", "sources": judging.visible_sources([SOURCE])}, Path(directory),
                lambda p: judging.validate_grounding(p, [SOURCE], "基础预算112万元。", True))
        self.assertEqual(call.call_count, 2)

    def test_independently_cited_clauses_do_not_inherit_each_others_citations(self):
        for separator in ("，", ",", "、", ";", "；"):
            answer = "运维负责人为周衡[1]" + separator + "采购负责人为顾宁[6][7]。"
            with self.subTest(separator=separator):
                spans = judging.answer_spans(answer)
                self.assertEqual([s["citation_ids"] for s in spans], [[1], [6, 7]])
                self.assertEqual("".join(s["text"] for s in spans), answer)

    def test_evidence_addresses_retain_intervening_text_instead_of_splicing_quotes(self):
        item = claim()
        item["support"][0]["passage_ids"] = [5, 1, 5]
        result = judging.validate_grounding({"claims": [item]}, [SOURCE], "基础预算112万元[1]。", True)
        quote = result["claims"][0]["support"][0]["quote"]
        self.assertEqual(quote, SOURCE["result"]["content"])
        self.assertIn("独立预备金：36万元。", quote)
        self.assertIn("### 执行信息", quote)

    def test_invalid_passage_ids_and_wrong_source_cannot_fabricate_evidence(self):
        for changes in ({"passage_ids": [999]}, {"passage_ids": [True]},
                        {"source_id": 2, "passage_ids": [2]}, {"passage_ids": []}):
            with self.subTest(changes=changes):
                item = claim()
                item["support"][0].update(changes)
                with self.assertRaises(judging.JudgeError):
                    judging.validate_grounding({"claims": [item]}, [SOURCE], "基础预算112万元[1]。", True)

    def test_supporting_source_does_not_supply_a_missing_answer_citation(self):
        item = claim()
        result = judging.validate_grounding({"claims": [item]}, [SOURCE], "基础预算112万元。", True)
        item = result["claims"][0]
        self.assertEqual(item["citations"], [])
        self.assertEqual(item["support"][0]["quote"], "基础预算：112万元。")
        self.assertEqual(item["citation_alignment"]["discarded_source_ids"], [1])

    def test_missing_attached_citation_is_not_inferred_from_another_source(self):
        item = claim()
        with self.assertRaises(judging.JudgeError) as error:
            judging.validate_grounding({"claims": [item]}, [SOURCE], "基础预算112万元[2]。", True)
        self.assertEqual(error.exception.details["attached_source_ids"], [2])
        self.assertEqual(error.exception.details["missing_source_ids"], [2])
        self.assertEqual(error.exception.details["path"], "claims[0].citations")

    def test_discarding_an_extra_source_preserves_a_real_negative_citation_verdict(self):
        item = claim()
        item["citations"].append({"source_id": 2, "supported": False})
        result = judging.validate_grounding({"claims": [item]}, [SOURCE], "基础预算112万元[2]。", True)
        self.assertEqual(result["claims"][0]["citations"], [{"source_id": 2, "supported": False}])

    def test_unsupported_claim_can_only_derive_negative_citation_verdicts(self):
        item = claim()
        item.update(verdict="unsupported", support=[], citations=[])
        result = judging.validate_grounding({"claims": [item]}, [SOURCE], "基础预算为999万元[1]。", True)
        self.assertEqual(result["claims"][0]["verdict"], "unsupported")
        self.assertFalse(result["claims"][0]["citations"][0]["supported"])
        self.assertEqual(result["claims"][0]["citations"][0]["derived_from"], "unsupported_claim")

    def test_targeted_citation_repair_cannot_change_other_semantic_verdicts(self):
        item = claim()
        item["citations"] = []
        judge = judging.Judge(mode="rules")
        with tempfile.TemporaryDirectory() as directory, patch.object(judge, "_call", side_effect=[
            {"result": {"claims": [item]}, "usage": None, "elapsed_ms": 1},
            {"result": {"checks": [{"id": "0:1", "supported": False, "passage_ids": []}]},
             "usage": None, "elapsed_ms": 1},
        ]) as call:
            result = judge._validated_call("grounding", judging.GROUNDING_PROMPT,
                {"answer": "基础预算112万元[1]。", "sources": judging.visible_sources([SOURCE])}, Path(directory),
                lambda p: judging.validate_grounding(p, [SOURCE], "基础预算112万元[1]。", True))
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["result"]["claims"][0]["verdict"], "supported")
        self.assertFalse(result["result"]["claims"][0]["citations"][0]["supported"])
        self.assertEqual(result["repair"]["method"], "citation_checks")
        self.assertEqual(call.call_args_list[1].args[2]["checks"][0]["source"]["id"], 1)

    def test_targeted_repair_rejects_omitted_checks_and_invalid_evidence(self):
        judge = judging.Judge(mode="rules")
        answer = "基础预算112万元[1]。"
        for correction in ({"checks": []}, {"checks": [{"id": "0:2", "supported": True, "passage_ids": [2]}]},
                           {"checks": [{"id": "0:1", "supported": True, "passage_ids": [999]}]}):
            with self.subTest(correction=correction), tempfile.TemporaryDirectory() as directory:
                item = claim(); item["citations"] = []
                with patch.object(judge, "_call", side_effect=[
                    {"result": {"claims": [item]}, "usage": None, "elapsed_ms": 1},
                    {"result": correction, "usage": None, "elapsed_ms": 1},
                ]) as call, self.assertRaises(judging.JudgeError):
                    judge._validated_call("grounding", judging.GROUNDING_PROMPT,
                        {"answer": answer, "sources": judging.visible_sources([SOURCE])}, Path(directory),
                        lambda p: judging.validate_grounding(p, [SOURCE], answer, True))
                self.assertEqual(call.call_count, 2)

    def test_citations_after_punctuation_and_markdown_are_kept_in_the_answer_span(self):
        answer = "**预算112万元。** [1]。\n* 预备金36万元[2]。\n* 未提供实际营收。"
        spans = judging.answer_spans(answer)
        self.assertEqual([s["citation_ids"] for s in spans], [[1], [2], []])
        self.assertEqual("".join(s["text"] for s in spans), answer.replace("\n", ""))
        sources = copy.deepcopy([SOURCE])
        sources[0]["result"]["wiki"] = {"page": {"content": "不可见私有文本"}}
        self.assertNotIn("不可见私有文本", str(judging.visible_sources(sources)))


@unittest.skipUnless(os.environ.get("E2E_JUDGE_LIVE_TESTS") == "1", "opt-in live judge checks")
class LiveGroundingAlignmentTests(unittest.TestCase):
    def classify_without_evidence(self, answer, should_exempt):
        judge = judging.Judge()
        real_call = judge._call
        initial = {"claims": [{"claim": answer, "answer_span_ids": [1], "verdict": "supported", "support": [], "citations": []}]}
        def call(name, prompt, data, directory):
            if name == "grounding":
                return {"result": copy.deepcopy(initial), "usage": None, "elapsed_ms": 0}
            return real_call(name, prompt, data, directory)
        with tempfile.TemporaryDirectory() as directory, patch.object(judge, "_call", side_effect=call):
            invoke = lambda: judge._validated_call("grounding", judging.GROUNDING_PROMPT,
                {"answer": answer, "sources": judging.visible_sources([SOURCE])}, Path(directory),
                lambda p: judging.validate_grounding(p, [SOURCE], answer, False))
            if should_exempt:
                result = invoke()
                self.assertEqual(result["result"]["claims"], [])
                self.assertEqual(result["repair"]["method"], "citation_and_limit_checks")
            else:
                with self.assertRaises(judging.JudgeError):
                    invoke()

    def test_limit_repair_can_exempt_a_retrieval_limitation(self):
        self.classify_without_evidence("经过检索，知识库中并未记录负责人的出生年份。", True)

    def test_limit_repair_cannot_exempt_a_positive_fact(self):
        self.classify_without_evidence("基础预算为112万元。", False)

    def evaluate(self, answer, sources, substantive):
        judge = judging.Judge()
        with tempfile.TemporaryDirectory(prefix="htknow-alignment-") as directory:
            result = judge._validated_call("grounding", judging.GROUNDING_PROMPT,
                {"question": "请说明实际营收和预算。", "answer": answer,
                 "answer_spans": judging.answer_spans(answer), "sources": judging.visible_sources(sources)},
                Path(directory), lambda p: judging.validate_grounding(p, sources, answer, substantive))
        return result["result"]["claims"]

    def test_empty_search_refusal_does_not_need_fabricated_evidence(self):
        self.assertEqual(self.evaluate("当前检索未找到相关预算信息，无法提供答案。", [], False), [])

    def test_uncited_additional_fact_in_a_refusal_still_lacks_citations(self):
        claims = self.evaluate("当前未找到实际营收数据。基础预算为112万元。", [SOURCE], False)
        self.assertTrue(claims)
        self.assertTrue(any(c["verdict"] == "supported" and not c["citations"] for c in claims), claims)

    def test_definite_false_absence_is_a_factual_error(self):
        claims = self.evaluate("该项目没有任何预算[1]。", [SOURCE], True)
        self.assertTrue(any(c["verdict"] != "supported" for c in claims), claims)
        self.assertTrue(any(not cit["supported"] for c in claims for cit in c["citations"]), claims)

    def test_repeated_fact_is_reviewed_at_its_cited_occurrence(self):
        claims = self.evaluate("基础预算为112万元。\n相关记录：\n* 基础预算112万元[1]。", [SOURCE], True)
        self.assertTrue(any(1 in judging.citation_ids(c["answer_quote"]) for c in claims), claims)

    def test_separate_clauses_keep_their_own_correct_citations(self):
        sources = [{"id": 1, "result": {"content": "运维负责人：周衡。"}},
                   {"id": 2, "result": {"content": "采购负责人：顾宁。"}}]
        claims = self.evaluate("运维负责人为周衡[1]，采购负责人为顾宁[2]。", sources, True)
        self.assertTrue(claims)
        self.assertTrue(all(c["verdict"] == "supported" and c["citations"] and
                            all(ci["supported"] for ci in c["citations"]) for c in claims), claims)


if __name__ == "__main__":
    unittest.main()

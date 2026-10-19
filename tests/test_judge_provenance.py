"""Citation provenance boundaries and opt-in live semantic regressions.

Run the live model checks explicitly with E2E_JUDGE_LIVE_TESTS=1. They call the
configured judge only, never HTKnow, and do not run in the default offline suite.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e2e_judge as judging


def sources():
    return [
        {"id": 1, "result": {"content": "星桥-A1 运维参数表\n故障通知频道：OPS-A1。"}},
        {"id": 6, "result": {"content": "## 星桥-A1 运维参数概览\n**故障通知频道**：OPS-A1。",
                             "wiki": {"page": {"title": "answer_ops", "content": "未返回给回答模型的全文"}}}},
    ]


class ProvenanceValidationTests(unittest.TestCase):
    def test_supported_wiki_content_uses_the_actual_excerpt_without_changing_kind(self):
        answer = "根据星桥-A1 的运维参数资料，故障通知频道为 OPS-A1 [6]。"
        payload = {"claims": [{"claim": "故障通知频道为 OPS-A1", "answer_quote": answer,
                   "verdict": "supported", "support": [{"source_id": 6, "quote": "**故障通知频道**：OPS-A1。"}],
                   "citations": [{"source_id": 6, "supported": True, "quote": "**故障通知频道**：OPS-A1。"}]}]}
        original = copy.deepcopy(sources())
        result = judging.validate_grounding(payload, original, answer, True)
        self.assertTrue(result["claims"][0]["citations"][0]["supported"])
        self.assertEqual(judging.visible_sources(original)[1]["kind"], "wiki")
        self.assertNotIn("未返回给回答模型", str(judging.visible_sources(original)))
        self.assertEqual(original, sources())

    def test_other_support_never_promotes_a_false_attached_citation(self):
        answer = "该频道是 OPS-A1，原始文件精确名称为 fabricated.txt [6]。"
        payload = {"claims": [{"claim": "原始文件名为 fabricated.txt", "answer_quote": answer,
                   "verdict": "unsupported", "support": [],
                   "citations": [{"source_id": 6, "supported": False, "quote": ""}]}]}
        result = judging.validate_grounding(payload, sources(), answer, True)
        self.assertFalse(result["claims"][0]["citations"][0]["supported"])
        # Even an exact quote from a different source must not be accepted as
        # evidence for the attached source number.
        wrong_quote = copy.deepcopy(payload)
        wrong_quote["claims"][0]["citations"][0].update(supported=True, quote="星桥-A1 运维参数表")
        with self.assertRaises(judging.JudgeError):
            judging.validate_grounding(wrong_quote, sources(), answer, True)


@unittest.skipUnless(os.environ.get("E2E_JUDGE_LIVE_TESTS") == "1", "opt-in live judge checks")
class LiveProvenanceTests(unittest.TestCase):
    def check_grounding(self, answer, supported):
        judge = judging.Judge()
        with tempfile.TemporaryDirectory(prefix="htknow-provenance-") as directory:
            result = judge._validated_call("grounding", judging.GROUNDING_PROMPT,
                {"question": "星桥-A1 发生故障时应通知哪个频道？", "answer": answer,
                 "answer_spans": judging.answer_spans(answer),
                 "sources": judging.visible_sources(sources())}, Path(directory),
                lambda payload: judging.validate_grounding(payload, sources(), answer, True))
            claims = result["result"]["claims"]
            all_supported = all(c["verdict"] == "supported" and c["citations"]
                                and all(citation["supported"] for citation in c["citations"]) for c in claims)
            self.assertEqual(all_supported, supported, result["result"])

    def test_generic_parameter_table_description_accepts_wiki_content(self):
        self.check_grounding("星桥-A1 的故障通知频道为 OPS-A1 [1]。该信息来自星桥-A1 的运维参数表，其中列出的故障通知频道为 OPS-A1 [6]。", True)

    def test_invented_channel_remains_unsupported(self):
        self.check_grounding("星桥-A1 的故障通知频道为 OPS-FAKE [6]。", False)

    def test_wiki_does_not_prove_original_verbatim_provenance(self):
        self.check_grounding("星桥-A1 的故障通知频道为 OPS-A1 [6]。该引用直接来自原始文档的原文切片，而不是 Wiki 摘要 [6]。", False)

    def test_generic_labels_do_not_license_invented_exact_filenames(self):
        self.check_grounding("故障通知频道为 OPS-A1 [6]。引用的原始文件精确名称为 fabricated.txt [6]。", False)


if __name__ == "__main__":
    unittest.main()

"""Check the new gold facts, baseline stability, and isolation of complex fixtures."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e2e_answer_eval as answers
import e2e_answer_report as report
import e2e_retrieval_test as harness


COMPLEX_CATEGORIES = "complex_reasoning,partial_answer,advanced_multi_turn"


class ComplexCaseTests(unittest.TestCase):
    def prepare(self, directory, seed="complex-gold", categories=COMPLEX_CATEGORIES):
        args = harness.parse_args(["--mode", "chat", "--answer-seed", seed,
                                   "--answer-categories", categories])
        return answers.prepare_suite(args, Path(directory), "offline-run", harness)

    def test_published_baseline_templates_and_questions_are_unchanged(self):
        manifest = json.loads(answers.DEFAULT_CASES.read_text())
        baseline = {"cases": manifest["cases"][:60], "documents": manifest["documents"][:6],
                    "wiki_pages": manifest["wiki_pages"]}
        digest = hashlib.sha256(json.dumps(baseline, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(digest, "4b1b41fc626128f74e7100ab2ec3c6452abc5964bfa33eef28717f8ef4aa37f8")
        with tempfile.TemporaryDirectory() as directory:
            suite = self.prepare(directory, seed="20261016-171704-696cce", categories="facts")
        self.assertEqual(suite["private_values"]["budget"], "112")
        self.assertEqual(suite["private_values"]["reserve"], "36")
        self.assertEqual(suite["private_values"]["date_cn"], "2027年5月7日")

    def test_complex_selection_has_three_categories_30_turns_and_scoped_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = self.prepare(directory)
            self.assertEqual(len(suite["cases"]), 20)
            self.assertEqual(sum(len(c["turns"]) for c in suite["cases"]), 30)
            complex_docs = {p["spec"].id for p in suite["pairs"] if p["scope"] == "complex"}
            self.assertEqual(len(complex_docs), 7)
            for case in suite["cases"]:
                self.assertEqual(case["scope"], "complex")
                self.assertTrue(set(case["required_docs"]) <= complex_docs)
                self.assertTrue(case["required_docs"])
                if case["category"] == "advanced_multi_turn":
                    self.assertEqual(len(case["turns"]), 3)
                for turn in case["turns"]:
                    self.assertTrue(turn["rubric"] or turn["facts"])
            partial = [t for c in suite["cases"] for t in c["turns"] if t["expected_behavior"] == "partial"]
            self.assertEqual(len(partial), 6)
            self.assertTrue(all(t["facts"] and t["citation_required"] for t in partial))
            self.assertEqual({c["category"] for c in suite["cases"]}, set(COMPLEX_CATEGORIES.split(",")))
            self.assertTrue(all(c["category"] in report.CATEGORIES for c in suite["cases"]))

    def test_financial_gold_is_computed_from_source_inputs_across_seeds(self):
        with tempfile.TemporaryDirectory() as directory:
            for seed in range(24):
                with self.subTest(seed=seed):
                    suite = self.prepare(directory, seed=str(seed))
                    texts = {p["spec"].id: Path(p["record"].local_path).read_text() for p in suite["pairs"]}
                    contract, catalog = texts["complex_contract"], texts["complex_catalog"]
                    price = int(re.search(r"每副本每月 (\d+) 元", contract)[1])
                    months = int(re.search(r"合同周期：(\d+)个月", contract)[1])
                    coupon = int(re.search(r"一次性抵扣 (\d+) 元", contract)[1])
                    allowance = int(re.search(r"专项额度：(\d+)元", contract)[1])
                    planned = int(re.search(r"计划采购副本数为 (\d+) 个", catalog)[1])
                    monthly, subtotal = price * planned, price * planned * months
                    net = subtotal - coupon
                    cases = {c["id"]: c for c in suite["cases"]}
                    gold = {f["id"]: f for f in cases["reason-06"]["turns"][0]["facts"]}
                    self.assertEqual(gold["monthly"]["aliases"], [str(monthly)])
                    self.assertEqual(gold["subtotal"]["aliases"], [str(subtotal)])
                    self.assertEqual(gold["net"]["aliases"], [str(net)])
                    headroom = next(f for f in cases["reason-07"]["turns"][0]["facts"] if f["id"] == "headroom")
                    self.assertEqual(headroom["aliases"], [str(allowance - net)])
                    self.assertGreater(net, 0)
                    self.assertGreater(allowance, net)
                    self.assertNotEqual(net, subtotal - coupon * months)
                    # Only raw inputs belong in the documents; totals are the oracle.
                    self.assertNotIn(str(subtotal) + "元", contract)
                    self.assertNotIn(str(net) + "元", contract)

    def test_conditional_gold_has_one_failure_one_success_and_one_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            for seed in range(12):
                suite = self.prepare(directory, seed=str(seed))
                values = suite["private_values"]
                self.assertLess(int(values["order_replicas"]), int(values["min_replicas"]))
                self.assertLess(int(values["observed_latency"]), int(values["latency_limit"]))
                self.assertEqual(int(values["latency_limit"]) - int(values["observed_latency"]), 15)
                cases = {c["id"]: c for c in suite["cases"]}
                self.assertEqual(cases["reason-03"]["turns"][0]["expected_behavior"], "answer")
                self.assertEqual(cases["reason-04"]["turns"][0]["expected_behavior"], "answer")
                self.assertEqual(cases["partial-02"]["turns"][0]["expected_behavior"], "partial")
                self.assertEqual([t["expected_behavior"] for t in cases["advanced-multi-04"]["turns"]],
                                 ["abstain", "answer", "partial"])


if __name__ == "__main__":
    unittest.main()

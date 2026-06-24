#!/usr/bin/env python3
import unittest

from ecommerce_80_gate_report import GateCandidate, classify_candidate, extract_profit_candidates


class Ecommerce80GateReportTests(unittest.TestCase):
    def test_classify_requires_80_ready_and_no_blocking(self):
        self.assertEqual(
            classify_candidate(GateCandidate(source="x", lane="x", title="79", score=79, pass_to_test=True)),
            "观察/补证",
        )
        self.assertEqual(
            classify_candidate(
                GateCandidate(source="x", lane="x", title="80 blocked", score=80, pass_to_test=True, blocking=["PDP素材不足"])
            ),
            "80+补证",
        )
        self.assertEqual(
            classify_candidate(GateCandidate(source="x", lane="x", title="80 ready", score=80, pass_to_test=True)),
            "测品候选",
        )

    def test_extract_profit_board_keeps_blocking_out_of_test_lane(self):
        payload = {
            "board": {
                "ready_to_test": [
                    {
                        "title": "Ready Product",
                        "score": 86,
                        "operator_gate": {"pass_to_test": True, "status": "ready"},
                    }
                ],
                "build_asset_pack": [
                    {
                        "title": "Needs Assets",
                        "score": 88,
                        "operator_gate": {
                            "pass_to_test": False,
                            "status": "asset_pack_required",
                            "missing": ["原创素材包不足"],
                        },
                        "validation_blocking_missing": ["PDP素材不足"],
                    }
                ],
            }
        }

        candidates = extract_profit_candidates(payload)
        by_title = {candidate.title: candidate for candidate in candidates}

        self.assertEqual(by_title["Ready Product"].decision, "测品候选")
        self.assertEqual(by_title["Needs Assets"].decision, "80+补证")
        self.assertFalse(by_title["Needs Assets"].pass_to_test)

    def test_extract_profit_command_lanes_treat_prepare_as_proof_not_test(self):
        payload = {
            "lanes": {
                "can_prepare": [
                    {
                        "title": "Operator A Product",
                        "operator_score": 83.4,
                        "status": "先拆素材",
                        "evidence_gaps": ["原创素材包不足"],
                    }
                ],
                "can_follow_now": [
                    {
                        "title": "Ready 8011 Product",
                        "operator_score": 86,
                        "status": "马上跟",
                    }
                ],
            }
        }

        candidates = extract_profit_candidates(payload)
        by_title = {candidate.title: candidate for candidate in candidates}

        self.assertEqual(by_title["Operator A Product"].decision, "80+补证")
        self.assertFalse(by_title["Operator A Product"].pass_to_test)
        self.assertEqual(by_title["Ready 8011 Product"].decision, "测品候选")


if __name__ == "__main__":
    unittest.main()

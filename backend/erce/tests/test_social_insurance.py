from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.erce.engine import estimate_routed_item
from backend.erce.route_tree import route_for_path
from backend.erce.social_insurance_evidence import social_insurance_rows
from backend.erce.variable_evidence_store import find_variable_candidates, save_variable_rows


class SocialInsuranceERCTest(unittest.TestCase):
    def test_social_insurance_tree_routes_are_explicit_leaves(self):
        expected = {
            "보험료지원": "social_insurance_premium_support",
            "급여기간지원": "social_insurance_benefit_duration",
            "기준선차액": "social_insurance_baseline_difference",
            "선지급미환수": "social_insurance_unrecovered_payment",
            "국고지원연동": "social_insurance_government_share",
        }
        for leaf, route in expected.items():
            with self.subTest(leaf=leaf):
                self.assertEqual(route_for_path(["이전지출", "사회보험", leaf]), route)

    def test_premium_support_accepts_yearly_inputs(self):
        result = estimate_routed_item({
            "route_key": "social_insurance_premium_support",
            "start_year": 2026,
            "years": 3,
            "explicit_inputs": {
                "annual_recipients": [100, 110, 120],
                "premium_base_amount": [3_000_000, 3_100_000, 3_200_000],
                "insurance_rate": 0.09,
                "support_rate": 0.5,
                "support_months": 12,
            },
        })
        self.assertEqual(result.annual_amounts_thousand, (162_000, 184_140, 207_360))

    def test_benefit_duration_supports_leave_day_formula(self):
        result = estimate_routed_item({
            "route_path": ["이전지출", "사회보험", "급여기간지원"],
            "start_year": 2026,
            "years": 2,
            "explicit_inputs": {
                "annual_recipients": [1000, 1200],
                "benefit_unit_amount": 80_382,
                "benefit_duration": [15, 20],
            },
        })
        self.assertEqual(result.annual_amounts_thousand, (1_205_730, 1_929_168))

    def test_baseline_difference_is_not_forced_nonnegative(self):
        result = estimate_routed_item({
            "route_key": "social_insurance_baseline_difference",
            "years": 3,
            "explicit_inputs": {
                "proposed_annual_amount": [120_000_000, 90_000_000, 80_000_000],
                "baseline_annual_amount": [100_000_000, 100_000_000, 100_000_000],
            },
        })
        self.assertEqual(result.annual_amounts_thousand, (20_000, -10_000, -20_000))

    def test_unrecovered_payment_and_government_share(self):
        unrecovered = estimate_routed_item({
            "route_key": "social_insurance_unrecovered_payment",
            "years": 2,
            "explicit_inputs": {
                "gross_annual_payment": [1_000_000_000, 2_000_000_000],
                "recovery_rate": [0.7, 0.75],
            },
        })
        self.assertEqual(unrecovered.annual_amounts_thousand, (300_000, 500_000))

        share = estimate_routed_item({
            "route_key": "social_insurance_government_share",
            "years": 2,
            "explicit_inputs": {
                "insurance_expenditure_increase": [1_000_000_000, 1_500_000_000],
                "government_support_rate": 0.2,
            },
        })
        self.assertEqual(share.annual_amounts_thousand, (200_000, 300_000))

    def test_rate_validation_rejects_impossible_values(self):
        bad_cases = [
            ("social_insurance_premium_support", {
                "annual_recipients": 1, "premium_base_amount": 1_000_000,
                "insurance_rate": 1.01, "support_rate": 0.5, "support_months": 12,
            }),
            ("social_insurance_unrecovered_payment", {
                "gross_annual_payment": 1_000_000, "recovery_rate": -0.1,
            }),
            ("social_insurance_government_share", {
                "insurance_expenditure_increase": 1_000_000, "government_support_rate": 1.2,
            }),
        ]
        for route, values in bad_cases:
            with self.subTest(route=route), self.assertRaises(ValueError):
                estimate_routed_item({"route_key": route, "years": 1, "explicit_inputs": values})

    def test_training_rows_remain_reviewed_precedents(self):
        rows = social_insurance_rows()
        self.assertGreaterEqual(len(rows), 9)
        self.assertTrue(all(row["source_class"] == "precedent_assumption" for row in rows))
        self.assertTrue(all(row.get("obtained_from") == "development_review" for row in rows))
        self.assertTrue(all(row.get("limitation") for row in rows))

    def test_development_review_can_reproduce_premium_support_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "social.sqlite3"
            save_variable_rows(social_insurance_rows(), db)
            keys = [
                "2203723:pension_A_value:2026-2030",
                "2203723:pension_contribution_rate:2024",
                "2203723:military_credit_income_share:2024",
            ]
            payload = {
                "bill_no": "2203723",
                "route_key": "social_insurance_premium_support",
                "start_year": 2026,
                "years": 5,
                "cutoff_date": "2026-10-03",
                "evidence_mode": "development_review",
                "variable_db_path": str(db),
                "selected_evidence_keys": keys,
                "explicit_inputs": {
                    "annual_recipients": [100, 100, 100, 100, 100],
                    "support_months": 12,
                },
            }
            result = estimate_routed_item(payload)
            expected = tuple(round(base * 100 * 0.09 * 0.5 * 12 / 1000)
                             for base in [3_348_000, 3_479_000, 3_621_000, 3_769_000, 3_924_000])
            self.assertEqual(result.annual_amounts_thousand, expected)
            with self.assertRaisesRegex(ValueError, "target answer"):
                estimate_routed_item({**payload, "evidence_mode": "holdout"})

    def test_newer_recovery_rate_is_kept_as_new_version_not_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "social.sqlite3"
            save_variable_rows(social_insurance_rows(), db)
            rows = find_variable_candidates(
                cutoff_date="2026-10-03",
                agency="근로복지공단",
                policy_domain="산재보험",
                variable_key="recovery_rate",
                target_bill_no="NEW",
                evidence_mode="holdout",
                db_path=db,
            )
            self.assertEqual({row["value"] for row in rows}, {0.673, 0.718})
            self.assertEqual(len({row["evidence_key"] for row in rows}), 2)


if __name__ == "__main__":
    unittest.main()

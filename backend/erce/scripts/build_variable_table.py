"""Upsert reviewed ERCE variables locally; does not alter the full sidecar DB."""
from __future__ import annotations

import argparse
from pathlib import Path

from backend.erce.reviewed_variable_rows import reviewed_variable_rows, burial_comparable_rows
from backend.erce.variable_evidence_store import DEFAULT_VARIABLE_DB, save_variable_rows
from backend.erce.interagency_meeting_evidence import interagency_meeting_rows, unification_council_annual_rows
from backend.erce.legislative_committee_evidence import legislative_committee_rows, policy_review_committee_rows
from backend.erce.executive_compensation_evidence import executive_position_difference_rows
from backend.erce.transfer_payment_evidence import transfer_payment_rows
from backend.erce.committee_annual_operation_evidence import committee_annual_operation_rows
from backend.erce.social_insurance_evidence import social_insurance_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_VARIABLE_DB)
    args = parser.parse_args()
    count = save_variable_rows(reviewed_variable_rows() + burial_comparable_rows()
                              + interagency_meeting_rows() + unification_council_annual_rows()
                              + legislative_committee_rows() + policy_review_committee_rows()
                              + executive_position_difference_rows() + transfer_payment_rows()
                              + committee_annual_operation_rows() + social_insurance_rows(), args.db)
    print(f"ERCE reviewed variable rows upserted: {count}; local DB: {args.db}")


if __name__ == "__main__":
    main()

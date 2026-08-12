"""Read the additive committee evidence layer for v3 candidate enrichment."""
from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any

from backend.estimator_v2.types import TagCandidate


_EVIDENCE_PATH = Path("backend/generated/committee_evidence_v1/committee_evidence_atoms.jsonl")
_PROFILE_PATH = Path("backend/generated/committee_evidence_v1/committee_profiles.jsonl")
_ROLE_TO_LEGACY = {
    "paid_members": ("target_count", "수당 지급대상 인원"),
    "meeting_unit_price": ("unit_cost", "1인 1회당 회의수당"),
    "annual_meetings": ("frequency", "연간 회의 횟수"),
}


@lru_cache(maxsize=1)
def evidence_by_item() -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = {}
    if not _EVIDENCE_PATH.exists():
        return indexed
    for line in _EVIDENCE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        item_id = str(row.get("source_item_key") or "")
        if item_id:
            indexed.setdefault(item_id, []).append(row)
    return indexed


@lru_cache(maxsize=1)
def profiles_by_item() -> dict[str, list[dict[str, Any]]]:
    """Index additive committee profiles by the TAG item they describe.

    Profiles are metadata about precedent structure. They never contain or
    look up the current target's official answer at runtime. A single TAG item
    can legitimately describe more than one body, so callers receive a list
    and resolve it against the candidate item name.
    """
    indexed: dict[str, list[dict[str, Any]]] = {}
    if not _PROFILE_PATH.exists():
        return indexed
    for line in _PROFILE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_attributes = row.get("raw_attributes") or {}
        source_keys = raw_attributes.get("source_item_keys") or []
        for item_id in source_keys:
            key = str(item_id or "")
            if key:
                indexed.setdefault(key, []).append(row)
    return indexed


def normalize_committee_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


def candidate_profile(candidate: TagCandidate) -> dict[str, Any] | None:
    """Return the profile that most directly describes one TAG candidate."""
    profiles = profiles_by_item().get(candidate.item_id, [])
    if not profiles:
        return None
    item_name = normalize_committee_name(candidate.item_name)
    exact = [
        profile
        for profile in profiles
        if normalize_committee_name(str(profile.get("normalized_name") or ""))
        and normalize_committee_name(str(profile.get("normalized_name") or ""))
        in item_name
    ]
    pool = exact or profiles
    return max(
        pool,
        key=lambda profile: (
            1 if profile.get("profile_status") == "complete" else 0,
            len(normalize_committee_name(str(profile.get("normalized_name") or ""))),
            str(profile.get("profile_key") or ""),
        ),
    )


def enrich_candidate(candidate: TagCandidate) -> TagCandidate:
    """Fill only missing executable roles; never overwrite legacy evidence."""
    rows = evidence_by_item().get(candidate.item_id, [])
    if not rows:
        return candidate
    variables = list(candidate.variables)
    present_types: set[str] = set()
    for variable in variables:
        if variable.get("value") in (None, ""):
            continue
        variable_type = str(variable.get("type") or "")
        unit = str(variable.get("unit") or "")
        if variable_type == "target_count" and re.search(r"명|인|위원", unit):
            present_types.add(variable_type)
        elif variable_type == "unit_cost" and "원" in unit:
            present_types.add(variable_type)
        elif variable_type == "frequency" and re.search(r"회|번", unit):
            present_types.add(variable_type)
    for row in rows:
        role = str(row.get("evidence_role") or "")
        mapping = _ROLE_TO_LEGACY.get(role)
        if mapping is None or row.get("normalized_value") in (None, ""):
            continue
        variable_type, label = mapping
        if variable_type in present_types:
            continue
        if row.get("transferability") == "do_not_transfer":
            continue
        if row.get("review_status") == "rejected":
            continue
        flags = set(row.get("quality_flags") or [])
        if "REVERSE_ENGINEERED_VALUE" in flags:
            continue
        variables.append({
            "type": variable_type,
            "name": label,
            "value": row.get("normalized_value"),
            "unit": row.get("normalized_unit") or row.get("raw_unit") or "",
            "source_text": str(row.get("source_text") or ""),
            "evidence_atom_key": row.get("atom_key"),
            "evidence_derivation": row.get("derivation_type"),
        })
        present_types.add(variable_type)

    formula = candidate.formula
    if not formula:
        formula = next(
            (
                str(row.get("formula_text"))
                for row in rows
                if row.get("evidence_role") == "formula_expression"
                and row.get("formula_text")
            ),
            "",
        )
    if variables == candidate.variables and formula == candidate.formula:
        return candidate
    return replace(candidate, variables=variables, formula=formula)


def enrich_candidates(candidates: list[TagCandidate]) -> list[TagCandidate]:
    return [enrich_candidate(candidate) for candidate in candidates]

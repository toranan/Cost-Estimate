"""Local, append-only queue for human-confirmed estimation variables.

The queue is deliberately separate from official TAG/RAG data.  A human input
is useful future evidence, but it must pass review before it is promoted to a
retrieval table.  Production upload is therefore an explicit later step.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import GENERATED_DIR


FEEDBACK_SCHEMA_VERSION = "committee-hitl-feedback-v1"
FEEDBACK_PATH = (
    GENERATED_DIR / "hitl_feedback" / "committee_variable_feedback.jsonl"
)
_WRITE_LOCK = threading.Lock()
_ASSUMPTION_KEY = {
    "meeting_count": "committee_meeting_count",
    "paid_members": "committee_paid_member_count",
    "incumbent_paid_members": "committee_incumbent_paid_member_count",
    "allowance_won": "committee_allowance_unit",
}


def _stable_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        record_id = str(row.get("record_id") or "")
        if record_id:
            ids.add(record_id)
    return ids


def _article_refs(committee: dict[str, Any]) -> list[str]:
    return [
        str(article.get("no") or "")
        for article in committee.get("articles") or []
        if str(article.get("no") or "")
    ]


def collect_committee_feedback(
    result: dict[str, Any],
    user_inputs: list[dict[str, Any]],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Queue user-confirmed values, deduplicating identical confirmations."""
    destination = path or FEEDBACK_PATH
    committees = {
        str(row.get("id") or ""): row
        for row in result.get("committees") or []
    }
    records: list[dict[str, Any]] = []
    collected_at = datetime.now(timezone.utc).isoformat()
    for entry in user_inputs:
        committee_id = str(
            entry.get("committeeId") or entry.get("committee_id") or ""
        )
        variable_key = str(entry.get("variable") or entry.get("key") or "")
        committee = committees.get(committee_id)
        variable = (
            (committee.get("variables") or {}).get(variable_key)
            if committee
            else None
        )
        assumption_key = _ASSUMPTION_KEY.get(variable_key)
        if not committee or not isinstance(variable, dict) or not assumption_key:
            continue
        try:
            value = float(variable.get("value"))
        except (TypeError, ValueError):
            continue
        identity = {
            "schema_version": FEEDBACK_SCHEMA_VERSION,
            "bill_no": str(result.get("billNo") or ""),
            "committee_name": str(committee.get("name") or ""),
            "article_refs": _article_refs(committee),
            "assumption_key": assumption_key,
            "value": value,
            "unit": str(variable.get("unit") or ""),
        }
        record_id = _stable_id(identity)
        records.append(
            {
                "record_id": record_id,
                **identity,
                "bill_name": str(result.get("billName") or ""),
                "committee_id": committee_id,
                "formula": str(committee.get("formula") or ""),
                "formula_family": str(
                    (committee.get("formula_selection") or {}).get("family")
                    or ""
                ),
                "variable_key": variable_key,
                "variable_label": str(variable.get("label") or ""),
                "source_type": "human_confirmed",
                "provenance": "committee_recompute_ui",
                "evidence_basis": str(variable.get("basis") or ""),
                "review_status": "pending",
                "eligible_for_retrieval": False,
                "uploaded_at": None,
                "collected_at": collected_at,
            }
        )

    if not records:
        return {
            "queued": 0,
            "duplicates": 0,
            "pendingReview": True,
        }

    with _WRITE_LOCK:
        destination.parent.mkdir(parents=True, exist_ok=True)
        existing = _existing_ids(destination)
        fresh = [row for row in records if row["record_id"] not in existing]
        if fresh:
            with destination.open("a", encoding="utf-8") as handle:
                for row in fresh:
                    handle.write(
                        json.dumps(row, ensure_ascii=False, sort_keys=True)
                        + "\n"
                    )
    return {
        "queued": len(fresh),
        "duplicates": len(records) - len(fresh),
        "pendingReview": True,
    }


# ── HITL 입력값 재사용(retrieval) ────────────────────────────────────────
# 사용자가 확정한 값은 pending으로 쌓인 뒤, 검토를 통과하면
# eligible_for_retrieval=True로 승격된다. 승격된 값은 method2(개념 조회)와
# 동일하게 (개념 키 → 대표값)으로 재사용된다 — 즉 한 번 사람이 채운 전제가
# 다음 유사 의안에서 자동 참조값이 되어, "값 없음→보류"가 시간이 갈수록 준다.
import statistics as _stats


def promoted_reference(assumption_key: str, *, path: Path | None = None) -> dict | None:
    """검토 통과한 HITL 입력값들에서 특정 개념(assumption_key)의 대표값을 조회.

    반환: {value(중앙값), n, samples, source} 또는 표본 없으면 None.
    review_status=='approved' 且 eligible_for_retrieval==True 인 것만 쓴다."""
    p = path or FEEDBACK_PATH
    if not p.exists():
        return None
    vals: list[float] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (row.get("assumption_key") == assumption_key
                and row.get("review_status") == "approved"
                and row.get("eligible_for_retrieval") is True):
            v = row.get("value")
            if isinstance(v, (int, float)):
                vals.append(float(v))
    if not vals:
        return None
    return {
        "value": _stats.median(vals),
        "n": len(vals),
        "samples": sorted(vals),
        "source": f"hitl_promoted:{assumption_key}",
    }


def approve_for_retrieval(record_ids: set[str], *, path: Path | None = None) -> int:
    """검토 완료 처리 — 지정한 record를 approved+retrieval 가능으로 승격."""
    p = path or FEEDBACK_PATH
    if not p.exists():
        return 0
    rows, n = [], 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("record_id") in record_ids:
            row["review_status"] = "approved"
            row["eligible_for_retrieval"] = True
            n += 1
        rows.append(row)
    with _WRITE_LOCK:
        with p.open("w", encoding="utf-8") as h:
            for row in rows:
                h.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return n

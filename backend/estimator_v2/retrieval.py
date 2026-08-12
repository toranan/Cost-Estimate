from __future__ import annotations

import glob
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..analyzer_v2 import OPENAI_EMBED_MODEL, embed_batch, try_embed, vector_search
from ..estimator import tag_db
from .cache import get_or_create_json, stable_hash
from .types import AtomicEvent, EventType, TagCandidate


RETRIEVAL_VERSION = "semantic-evidence-v4"
QUERY_EMBEDDING_VERSION = "query-embedding-openai-v1"
VARIABLE_RETRIEVAL_VERSION = "variable-role-semantic-v1"
NON_ATTACHMENT_RETRIEVAL_VERSION = "non-attachment-evidence-v4"
NON_ATTACHMENT_COMPATIBILITY_VERSION = "non-attachment-mechanism-gate-v1"
MIN_SIMILARITY = 0.48
MIN_ROLE_ALIGNED_NON_ATTACHMENT_SIMILARITY = 0.40
MIN_PRICED_CASES = 3
_GENERATED = Path("backend/generated")
_FAMILY_BY_EVENT = {
    EventType.COMMITTEE.value: "committee",
    EventType.RESEARCH_PLAN.value: "research",
    EventType.PERSONNEL.value: "personnel",
    EventType.SUBSIDY.value: "subsidy",
    EventType.FACILITY_SYSTEM.value: "facility",
}


_NON_ATTACHMENT_ROLE_PATTERNS = {
    "organization_scale": re.compile(
        r"(?:센터|기관|법인|조직|기구|부서|지점|사무소).{0,20}"
        r"(?:수|개수|신설|설치|확대|운영|규모)|"
        r"(?:인력|인원|정원|종사자|직원).{0,20}"
        r"(?:수|규모|산정|추정|미정|확정|자료)"
    ),
    "facility_scale": re.compile(
        r"(?:시설|청사|건축|공사|임차|면적|부지).{0,20}"
        r"(?:수|규모|면적|범위|단가|기간|미정|확정|자료)"
    ),
    "beneficiary_volume": re.compile(
        r"(?:대상자|수혜자|수급자|지원대상|신청자|이용자).{0,20}"
        r"(?:수|규모|범위|추정|예측|자료)"
    ),
    "unit_cost": re.compile(
        r"(?:단가|지급액|지원액|보수|수당|사용료|임차료).{0,20}"
        r"(?:기준|금액|수준|미정|확정|자료)"
    ),
    "demand_volume": re.compile(
        r"(?:수요|신청|이용|처리|신고|사건|업무량|건수).{0,20}"
        r"(?:규모|수|건|예측|추정|자료)"
    ),
    "implementation_scope": re.compile(
        r"(?:사업|지원|시행|운영|사용).{0,20}"
        r"(?:규모|범위|방식|방법|기준|세부사항|계획)"
    ),
    "baseline_data": re.compile(
        r"(?:기초자료|통계자료|실적자료|과거실적|집행실적|자료).{0,20}"
        r"(?:부재|부족|없|확보|파악|곤란|한계)"
    ),
}

_NON_ATTACHMENT_MECHANISM_PATTERNS: tuple[
    tuple[str, re.Pattern[str]],
    ...,
] = (
    (
        "revenue_change",
        re.compile(
            r"수입\s*(?:감소|증가|변화)|세수|조세|세액|과세|"
            r"벌금|과태료|부담금|사용료\s*수입"
        ),
    ),
    (
        "workload_staffing",
        re.compile(
            r"(?:업무량|처리량|신고|신청|사건|조치)\D{0,30}"
            r"(?:증가|건수|규모|수요).{0,60}"
            r"(?:인력|인원|정원|직원)"
            r"|(?:인력|인원|정원|직원).{0,60}"
            r"(?:업무량|처리량|신고|신청|사건|조치)"
        ),
    ),
    (
        "facility_capex",
        re.compile(
            r"(?:청사|건축|공사|임차|면적|부지|시설\s*(?:설치|건립|증축))"
            r".{0,40}(?:규모|면적|범위|단가|기간|비용|자료|미정|곤란)"
            r"|(?:규모|면적|범위|단가|기간|비용|자료|미정|곤란)"
            r".{0,40}(?:청사|건축|공사|임차|부지|시설\s*(?:설치|건립|증축))"
        ),
    ),
    (
        "organization_creation",
        re.compile(
            r"(?:센터|기관|법인|조직|기구|부서|지점|사무소)"
            r".{0,30}(?:신설|설치|확대|개수|기관\s*수|조직\s*수|규모|정원)"
            r"|(?:신설|설치|확대).{0,30}"
            r"(?:센터|기관|법인|조직|기구|부서|지점|사무소)"
        ),
    ),
    (
        "program_delivery",
        re.compile(
            r"(?:지원|보조|급여|사업).{0,30}"
            r"(?:대상자|수혜자|수급자|신청자|지급액|지원액|사업규모)"
        ),
    ),
    (
        "information_system",
        re.compile(
            r"(?:정보시스템|전산시스템|정보망|전산망|데이터베이스)"
            r".{0,30}(?:구축|개편|운영|규모|비용)"
        ),
    ),
)

_NON_ATTACHMENT_MECHANISMS_BY_REQUIRED_ROLE = {
    "organization_scale": {"organization_creation"},
    "facility_scale": {"facility_capex"},
}


def classify_non_attachment_missing_roles(
    reason_text: str,
    evidence_text: str,
) -> list[str]:
    """Normalize an official non-attachment reason into evidence-gap roles.

    This is metadata extraction, not target-to-precedent text-overlap scoring.
    The target is matched only against the resulting stable role labels.
    """
    text = re.sub(r"\s+", " ", f"{reason_text} {evidence_text}").strip()
    return sorted(
        role
        for role, pattern in _NON_ATTACHMENT_ROLE_PATTERNS.items()
        if pattern.search(text)
    )


def classify_non_attachment_cost_mechanisms(
    reason_text: str,
    evidence_text: str,
) -> list[str]:
    """Normalize what kind of public-finance mechanism the reason concerns.

    This is a one-document metadata classification.  It does not score word
    overlap between a target bill and a precedent.  Mutually misleading
    contexts are resolved conservatively: a revenue-loss reason that happens
    to mention a regulated facility is still revenue evidence, not facility
    construction evidence; workload-driven staffing is not evidence about the
    headcount of a newly created statutory organization.
    """
    text = re.sub(r"\s+", " ", f"{reason_text} {evidence_text}").strip()
    matched = [
        mechanism
        for mechanism, pattern in _NON_ATTACHMENT_MECHANISM_PATTERNS
        if pattern.search(text)
    ]
    if "revenue_change" in matched:
        return ["revenue_change"]
    if "workload_staffing" in matched:
        return [
            mechanism
            for mechanism in matched
            if mechanism not in {"organization_creation"}
        ]
    return sorted(set(matched))


@lru_cache(maxsize=1)
def _non_attachment_index() -> dict[str, dict[str, Any]]:
    """Load structured fields and proposal dates for official non-attachment docs.

    The vector store is chunk-oriented.  The local TAG export already contains
    document-level reason/evidence fields, so enrich a retrieved bill instead
    of treating whichever PDF chunk happened to rank first as the full reason.
    """
    indexed: dict[str, dict[str, Any]] = {}
    dates: dict[str, str] = {}
    for path in glob.glob(
        str(_GENERATED / "**/chunks.jsonl"),
        recursive=True,
    ):
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            doc_type = str(
                row.get("documentType") or row.get("document_type") or ""
            )
            if doc_type != "non_attachment_reason":
                continue
            bill_no = str(row.get("billNo") or row.get("bill_no") or "")
            propose_date = str(
                row.get("proposeDate") or row.get("propose_date") or ""
            )
            if bill_no and propose_date:
                dates[bill_no] = propose_date

    for path in glob.glob(
        str(_GENERATED / "**/non_attachment_classifications.jsonl"),
        recursive=True,
    ):
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            bill_no = str(row.get("bill_no") or "")
            if not bill_no:
                continue
            reason_text = str(row.get("reason_text") or "")
            evidence_text = str(row.get("evidence_text") or "")
            candidate = {
                "reason_type": str(row.get("reason_type") or ""),
                "reason_text": reason_text,
                "evidence_text": evidence_text,
                "missing_roles": classify_non_attachment_missing_roles(
                    reason_text,
                    evidence_text,
                ),
                "cost_mechanisms": classify_non_attachment_cost_mechanisms(
                    reason_text,
                    evidence_text,
                ),
                "confidence": float(row.get("confidence") or 0),
                "classification_status": str(row.get("status") or ""),
                "propose_date": dates.get(bill_no, ""),
            }
            current = indexed.get(bill_no)
            if current is None or len(candidate["evidence_text"]) > len(
                str(current.get("evidence_text") or "")
            ):
                indexed[bill_no] = candidate
    return indexed


def _normalise_law_name(value: str) -> str:
    compact = re.sub(r"[\sㆍ·・]", "", value or "")
    return re.sub(r"(일부|전부)?개정법률안$|법률안$", "", compact)


@lru_cache(maxsize=1)
def _amount_rows() -> dict[str, list[dict[str, Any]]]:
    rows_by_item: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[Any, ...]] = set()
    for path in glob.glob(str(_GENERATED / "**/cost_estimate_amounts.jsonl"), recursive=True):
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("is_total"):
                continue
            item_id = str(row.get("item_id") or "")
            if not item_id:
                continue
            signature = (
                item_id,
                row.get("year_offset"),
                row.get("amount_thousand"),
                str(row.get("formula_text") or ""),
            )
            if signature in seen:
                continue
            seen.add(signature)
            rows_by_item.setdefault(item_id, []).append(
                {
                    "year_label": row.get("year_label"),
                    "year_offset": row.get("year_offset"),
                    "amount_thousand": row.get("amount_thousand"),
                    "formula_text": str(row.get("formula_text") or ""),
                }
            )
    for values in rows_by_item.values():
        values.sort(
            key=lambda row: (
                row.get("year_offset") is None,
                row.get("year_offset") if row.get("year_offset") is not None else 999,
            )
        )
    return rows_by_item


def dataset_fingerprint() -> str:
    data = tag_db._load()
    embeddings = tag_db._load_embeddings()
    amounts = _amount_rows()
    payload = {
        "version": RETRIEVAL_VERSION,
        "items": sorted(
            (
                item_id,
                meta.get("bill_no"),
                meta.get("bill_name"),
                meta.get("name"),
                meta.get("family"),
                meta.get("propose_date"),
            )
            for item_id, meta in data["items"].items()
        ),
        "embedding_items": sorted(embeddings),
        "amount_signatures": sorted(
            (item_id, len(rows), sum(int(row.get("amount_thousand") or 0) for row in rows))
            for item_id, rows in amounts.items()
        ),
    }
    return stable_hash(payload)[:16]


def target_metadata(bill_no: str | None) -> tuple[str | None, str]:
    if not bill_no:
        return None, ""
    for meta in tag_db._load()["items"].values():
        if str(meta.get("bill_no") or "") == str(bill_no):
            return str(meta.get("propose_date") or "") or None, str(meta.get("bill_name") or "")
    # A newly uploaded target can exist in the official bill catalogue before
    # its cost estimate has been structured into TAG.  The proposal date is
    # still required to exclude future precedents, so fall back to the raw bill
    # metadata rather than weakening the temporal holdout.
    for path in glob.glob(str(_GENERATED / "**/bills.jsonl"), recursive=True):
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("billNo") or row.get("bill_no") or "") != str(bill_no):
                continue
            return (
                str(row.get("proposeDate") or row.get("propose_date") or "") or None,
                str(row.get("billName") or row.get("bill_name") or ""),
            )
    return None, ""


def _query_embedding(query: str, refresh: bool = False) -> tuple[list[float] | None, bool]:
    # Query embeddings depend only on the embedding model and query text, not
    # on downstream retrieval/reranking policy.  Older code keyed them by
    # RETRIEVAL_VERSION, so every policy bump invalidated identical vectors
    # and made offline regression look like a data miss.  Reuse those legacy
    # vectors first, then store future vectors under a stable embedding-only
    # version.
    if not refresh:
        for legacy_version in (RETRIEVAL_VERSION, "semantic-evidence-v3"):
            legacy_value, legacy_hit = get_or_create_json(
                "query_embeddings",
                {
                    "version": legacy_version,
                    "model": OPENAI_EMBED_MODEL,
                    "query": query,
                },
                lambda: None,
            )
            if isinstance(legacy_value, list):
                return legacy_value, legacy_hit
    value, cache_hit = get_or_create_json(
        "query_embeddings",
        {
            "version": QUERY_EMBEDDING_VERSION,
            "model": OPENAI_EMBED_MODEL,
            "query": query,
        },
        lambda: try_embed(query[:2000]),
        refresh=refresh,
    )
    return value if isinstance(value, list) else None, cache_hit


def _variable_query(event: AtomicEvent, role: str) -> str:
    context = " ".join(
        [
            event.object,
            event.action,
            " ".join(event.quotes),
        ]
    )
    compact = re.sub(r"\s+", "", context)
    if re.search(
        r"공론조사|공론절차|공론화|국민공론|시민공론|숙의|"
        r"국민참여|시민참여|주민참여",
        compact,
    ):
        function = "국민참여 공론조사"
    elif re.search(r"분쟁|징계|판정|심판|조정|심사", compact):
        function = "사건 심사 조정 판정"
    else:
        function = event.object
    labels = {
        "count": "수당 지급대상 위원 수",
        "unit_price": "위원 1인 1회 참석수당 단가",
        "frequency": "실시 횟수 회의 개최 횟수",
    }
    return f"{function} {labels[role]}"


def _variable_query_embedding(
    event: AtomicEvent,
    role: str,
    *,
    refresh: bool = False,
) -> tuple[list[float] | None, bool]:
    query = _variable_query(event, role)
    value, cache_hit = get_or_create_json(
        "variable_query_embeddings",
        {
            "version": VARIABLE_RETRIEVAL_VERSION,
            "model": OPENAI_EMBED_MODEL,
            "role": role,
            "query": query,
        },
        lambda: try_embed(query[:2000]),
        refresh=refresh,
    )
    return value if isinstance(value, list) else None, cache_hit


def search_variable_candidates(
    event: AtomicEvent,
    role: str,
    *,
    exclude_bill_nos: set[str],
    cutoff_date: str | None,
    refresh: bool = False,
    k: int = 40,
    min_similarity: float = 0.25,
) -> tuple[list[TagCandidate], bool]:
    """Search operand evidence across TAG families with a role-focused query.

    Formula retrieval remains family-scoped.  Operand evidence does not:
    an official ``frequency`` may be stored under an event row such as
    ``other`` even when the target formula belongs to ``committee``.
    """
    if role not in {"count", "unit_price", "frequency"}:
        return [], False
    query_vector, cache_hit = _variable_query_embedding(
        event,
        role,
        refresh=refresh,
    )
    if not query_vector:
        return [], cache_hit
    data = tag_db._load()
    embeddings = tag_db._load_embeddings()
    formulas = tag_db._formula_by_item()
    amounts = _amount_rows()
    excluded = {str(value) for value in exclude_bill_nos if value}
    scored: list[tuple[float, str]] = []
    for item_id, meta in data["items"].items():
        vector = embeddings.get(item_id)
        if not vector:
            continue
        if str(meta.get("bill_no") or "") in excluded:
            continue
        propose_date = str(meta.get("propose_date") or "")
        if cutoff_date and propose_date and propose_date > cutoff_date:
            continue
        similarity = tag_db._cosine(query_vector, vector)
        if similarity >= min_similarity:
            scored.append((similarity, item_id))
    scored.sort(key=lambda value: (-value[0], value[1]))
    candidates: list[TagCandidate] = []
    for score, item_id in scored[:k]:
        meta = data["items"][item_id]
        candidates.append(
            TagCandidate(
                item_id=item_id,
                bill_no=str(meta.get("bill_no") or ""),
                bill_name=str(meta.get("bill_name") or ""),
                item_name=str(meta.get("name") or ""),
                category=str(meta.get("category") or ""),
                family=str(meta.get("family") or ""),
                similarity=round(score, 4),
                variables=data["vars"].get(item_id, []),
                amounts=amounts.get(item_id, []),
                formula=formulas.get(item_id, ""),
                propose_date=str(meta.get("propose_date") or ""),
            )
        )
    return candidates, cache_hit


def search_cost_candidates(
    event: AtomicEvent,
    *,
    exclude_bill_nos: set[str],
    exclude_bill_name: str,
    cutoff_date: str | None,
    refresh: bool = False,
    k: int = 10,
    min_similarity: float = MIN_SIMILARITY,
) -> tuple[list[TagCandidate], bool]:
    """Semantic-only TAG retrieval.

    No token-overlap fallback exists in v2.  If the embedding service and cache
    are both unavailable, the caller receives no candidates and must ask for
    evidence instead of inventing a value.
    """
    family = _FAMILY_BY_EVENT.get(event.event_type)
    if not family:
        return [], False
    query = event.query_text()
    query_vector, cache_hit = _query_embedding(query, refresh=refresh)
    if not query_vector:
        return [], cache_hit

    data = tag_db._load()
    embeddings = tag_db._load_embeddings()
    formulas = tag_db._formula_by_item()
    amounts = _amount_rows()
    excluded = {str(value) for value in exclude_bill_nos if value}
    scored: list[tuple[float, str]] = []
    for item_id, meta in data["items"].items():
        vector = embeddings.get(item_id)
        if not vector or meta.get("family") != family:
            continue
        if str(meta.get("bill_no") or "") in excluded:
            continue
        # An older estimate of the same law is often the strongest legitimate
        # precedent.  Temporal holdout and exact target exclusion prevent
        # answer leakage; the prior-scope gate separately checks whether that
        # earlier estimate actually priced this cost event.
        propose_date = str(meta.get("propose_date") or "")
        if cutoff_date and propose_date and propose_date > cutoff_date:
            continue
        similarity = tag_db._cosine(query_vector, vector)
        if similarity >= min_similarity:
            scored.append((similarity, item_id))
    scored.sort(key=lambda value: (-value[0], value[1]))
    if not scored:
        return [], cache_hit
    # Do not apply a relative ``top score - gap`` cutoff here.  The cached TAG
    # embeddings describe mostly item names/categories, so a structurally
    # excellent case can score slightly lower on surface semantics.  Pass the
    # absolute-threshold shortlist to the evidence-rich reranker instead.
    selected = scored[:k]
    candidates: list[TagCandidate] = []
    for score, item_id in selected:
        meta = data["items"][item_id]
        candidates.append(
            TagCandidate(
                item_id=item_id,
                bill_no=str(meta.get("bill_no") or ""),
                bill_name=str(meta.get("bill_name") or ""),
                item_name=str(meta.get("name") or ""),
                category=str(meta.get("category") or ""),
                family=family,
                similarity=round(score, 4),
                variables=data["vars"].get(item_id, []),
                amounts=amounts.get(item_id, []),
                formula=formulas.get(item_id, ""),
                propose_date=str(meta.get("propose_date") or ""),
            )
        )
    return candidates, cache_hit


def search_companion_formula_candidates(
    event: AtomicEvent,
    target_event_types: set[str],
    target_context_text: str = "",
    *,
    exclude_bill_nos: set[str],
    cutoff_date: str | None,
    refresh: bool = False,
    k: int = 20,
) -> tuple[list[TagCandidate], bool]:
    """Recover complete formula cases from bills with the same event bundle.

    Item-name embeddings can miss a strong precedent when both titles are
    generic.  A bill that prices both a committee and a periodic plan is
    structurally more informative for a target containing the same pair.  This
    is a second retrieval channel, not a hard winner: it only expands the
    reranker's candidates and preserves the original semantic score.
    """
    target_families = {
        family
        for event_type in target_event_types
        if (family := _FAMILY_BY_EVENT.get(event_type))
        in {"committee", "research"}
    }
    family = _FAMILY_BY_EVENT.get(event.event_type)
    if family != "committee" or target_families != {"committee", "research"}:
        return [], False

    query_vector, cache_hit = _query_embedding(
        event.query_text(),
        refresh=refresh,
    )
    if not query_vector:
        return [], cache_hit

    # Local import avoids coupling the base retrieval module to the operand
    # selector during module initialization.
    from .variable_selection import committee_formula_roles

    data = tag_db._load()
    embeddings = tag_db._load_embeddings()
    formulas = tag_db._formula_by_item()
    amounts = _amount_rows()
    excluded = {str(value) for value in exclude_bill_nos if value}
    families_by_bill: dict[str, set[str]] = {}
    for meta in data["items"].values():
        bill_no = str(meta.get("bill_no") or "")
        item_family = str(meta.get("family") or "")
        if bill_no and item_family:
            families_by_bill.setdefault(bill_no, set()).add(item_family)

    scored: list[tuple[float, str, list[str]]] = []
    for item_id, meta in data["items"].items():
        bill_no = str(meta.get("bill_no") or "")
        propose_date = str(meta.get("propose_date") or "")
        vector = embeddings.get(item_id)
        if (
            not vector
            or str(meta.get("family") or "") != family
            or bill_no in excluded
            or not target_families.issubset(families_by_bill.get(bill_no, set()))
            or (cutoff_date and propose_date and propose_date > cutoff_date)
        ):
            continue
        candidate = TagCandidate(
            item_id=item_id,
            bill_no=bill_no,
            bill_name=str(meta.get("bill_name") or ""),
            item_name=str(meta.get("name") or ""),
            category=str(meta.get("category") or ""),
            family=family,
            similarity=0.0,
            variables=data["vars"].get(item_id, []),
            amounts=amounts.get(item_id, []),
            formula=formulas.get(item_id, ""),
            propose_date=propose_date,
        )
        if set(committee_formula_roles(candidate)) != {
            "count",
            "unit_price",
            "frequency",
        }:
            continue
        similarity = tag_db._cosine(query_vector, vector)
        scored.append(
            (similarity, item_id, sorted(families_by_bill[bill_no]))
        )
    scored.sort(key=lambda value: (-value[0], value[1]))

    # Multi-vector retrieval: the base index represents item names, whereas
    # this small second-stage embedding represents the bill-level event bundle
    # (for example, health-policy committee + five-year master plan).  Cache
    # the batch so repeated regression runs do not spend embedding credits.
    shortlisted = scored[:k]
    item_names_by_bill: dict[str, list[str]] = {}
    context_items_by_bill: dict[str, list[dict[str, Any]]] = {}
    for meta in data["items"].values():
        bill_no = str(meta.get("bill_no") or "")
        name = str(meta.get("name") or "")
        if bill_no and name:
            item_names_by_bill.setdefault(bill_no, []).append(name)
    for item_id, meta in data["items"].items():
        bill_no = str(meta.get("bill_no") or "")
        item_family = str(meta.get("family") or "")
        if not bill_no or item_family not in {"committee", "research"}:
            continue
        context_items_by_bill.setdefault(bill_no, []).append(
            {
                "family": item_family,
                "name": str(meta.get("name") or ""),
                "formula": formulas.get(item_id, ""),
                "variables": [
                    {
                        "type": variable.get("type"),
                        "name": variable.get("name"),
                        "unit": variable.get("unit"),
                        "source_text": variable.get("source_text"),
                    }
                    for variable in data["vars"].get(item_id, [])
                ],
            }
        )
    context_texts = [
        " ".join(
            value
            for value in (
                event.query_text(),
                target_context_text,
            )
            if value
        )[:4_000]
    ]
    for _, item_id, _ in shortlisted:
        meta = data["items"][item_id]
        bill_no = str(meta.get("bill_no") or "")
        context_texts.append(
            " ".join(
                [
                    str(meta.get("bill_name") or ""),
                    str(meta.get("name") or ""),
                    *item_names_by_bill.get(bill_no, []),
                ]
            )[:4_000]
        )
    context_vectors, context_cache_hit = get_or_create_json(
        "companion_context_embeddings",
        {
            "version": "companion-context-multivector-v1",
            "model": OPENAI_EMBED_MODEL,
            "texts": context_texts,
        },
        lambda: embed_batch(context_texts),
        refresh=refresh,
    )
    context_scores: dict[str, float] = {}
    if (
        isinstance(context_vectors, list)
        and len(context_vectors) == len(context_texts)
        and context_vectors
        and isinstance(context_vectors[0], list)
    ):
        target_vector = context_vectors[0]
        for (_, item_id, _), vector in zip(shortlisted, context_vectors[1:]):
            if isinstance(vector, list) and vector:
                context_scores[item_id] = tag_db._cosine(target_vector, vector)

    candidates: list[TagCandidate] = []
    for score, item_id, context_families in shortlisted:
        meta = data["items"][item_id]
        candidates.append(
            TagCandidate(
                item_id=item_id,
                bill_no=str(meta.get("bill_no") or ""),
                bill_name=str(meta.get("bill_name") or ""),
                item_name=str(meta.get("name") or ""),
                category=str(meta.get("category") or ""),
                family=family,
                similarity=round(score, 4),
                variables=data["vars"].get(item_id, []),
                amounts=amounts.get(item_id, []),
                formula=formulas.get(item_id, ""),
                propose_date=str(meta.get("propose_date") or ""),
                context_families=context_families,
                context_similarity=round(context_scores.get(item_id, 0.0), 4),
                context_items=context_items_by_bill.get(
                    str(meta.get("bill_no") or ""),
                    [],
                ),
            )
        )
    return candidates, cache_hit and context_cache_hit


def search_non_attachment_evidence(
    event: AtomicEvent,
    *,
    exclude_bill_nos: set[str],
    cutoff_date: str | None = None,
    refresh: bool = False,
    k: int = 3,
) -> tuple[list[dict[str, Any]], bool]:
    query_vector, _ = _query_embedding(event.query_text(), refresh=refresh)
    if not query_vector:
        return [], False

    def producer() -> list[dict[str, Any]]:
        index = _non_attachment_index()
        rows = vector_search(
            query_vector,
            source="national_assembly",
            doc_type="non_attachment_reason",
            # Fetch a wider chunk pool because several chunks from one bill
            # can otherwise crowd out distinct documents, and historical
            # holdouts must filter documents newer than the target.
            k=max(k * 10, 30),
            exclude_bill_nos=exclude_bill_nos,
        )
        by_bill: dict[str, dict[str, Any]] = {}
        for row in rows:
            bill_no = str(row.get("bill_no") or "")
            metadata = index.get(bill_no, {})
            propose_date = str(metadata.get("propose_date") or "")
            if cutoff_date and (
                not propose_date or propose_date > cutoff_date
            ):
                continue
            candidate = {
                "bill_no": str(row.get("bill_no") or ""),
                "bill_name": str(row.get("bill_name") or ""),
                "similarity": round(float(row.get("similarity") or 0), 4),
                "content": str(
                    metadata.get("evidence_text")
                    or row.get("content")
                    or ""
                )[:700],
                "reason_type": str(metadata.get("reason_type") or ""),
                "reason_text": str(metadata.get("reason_text") or ""),
                "evidence_text": str(metadata.get("evidence_text") or ""),
                "classification_confidence": float(
                    metadata.get("confidence") or 0
                ),
                "classification_status": str(
                    metadata.get("classification_status") or ""
                ),
                "propose_date": propose_date,
                "missing_roles": list(
                    metadata.get("missing_roles")
                    or classify_non_attachment_missing_roles(
                        str(metadata.get("reason_text") or ""),
                        str(metadata.get("evidence_text") or ""),
                    )
                ),
                "cost_mechanisms": list(
                    metadata.get("cost_mechanisms")
                    or classify_non_attachment_cost_mechanisms(
                        str(metadata.get("reason_text") or ""),
                        str(metadata.get("evidence_text") or ""),
                    )
                ),
            }
            current = by_bill.get(bill_no)
            if current is None or candidate["similarity"] > current["similarity"]:
                by_bill[bill_no] = candidate
        return sorted(
            by_bill.values(),
            key=lambda row: (-float(row["similarity"]), str(row["bill_no"])),
        )[:k]

    value, _ = get_or_create_json(
        "non_attachment_search",
        {
            "version": NON_ATTACHMENT_RETRIEVAL_VERSION,
            "query": event.query_text(),
            "exclude_bill_nos": sorted(exclude_bill_nos),
            "cutoff_date": cutoff_date,
            "k": k,
        },
        producer,
        refresh=refresh,
    )
    if isinstance(value, list):
        # An empty *stored or freshly fetched* search result is still a known
        # result.  ``None`` means the channel was unavailable (for example an
        # offline cache miss) and must not be mistaken for "no counterevidence".
        return value, True
    return [], False


def compatible_scale_counterevidence(
    evidence: list[dict[str, Any]],
    *,
    required_roles: set[str],
) -> list[dict[str, Any]]:
    """Keep only counterevidence with both the same gap and cost mechanism."""
    expected_mechanisms = {
        mechanism
        for role in required_roles
        for mechanism in _NON_ATTACHMENT_MECHANISMS_BY_REQUIRED_ROLE.get(
            role,
            set(),
        )
    }
    if not required_roles or not expected_mechanisms:
        return []
    compatible: list[dict[str, Any]] = []
    for row in evidence:
        missing_roles = set(
            row.get("missing_roles")
            or classify_non_attachment_missing_roles(
                str(row.get("reason_text") or ""),
                str(row.get("evidence_text") or ""),
            )
        )
        mechanisms = set(
            row.get("cost_mechanisms")
            or classify_non_attachment_cost_mechanisms(
                str(row.get("reason_text") or ""),
                str(row.get("evidence_text") or ""),
            )
        )
        if (
            required_roles.intersection(missing_roles)
            and expected_mechanisms.intersection(mechanisms)
        ):
            compatible.append(row)
    return compatible


def strongest_scale_blocker(
    event: AtomicEvent,
    evidence: list[dict[str, Any]],
    *,
    required_roles: set[str] | None = None,
) -> dict[str, Any] | None:
    """Return strong official counterevidence for an unresolved org scale.

    This does not prove that the target has no cost.  It only says an automatic
    amount is unsafe until the target's missing organization/staffing scale is
    supplied.  Embedding retrieves candidates, but the final gate requires the
    same normalized missing-evidence role.  It does not compare Korean words
    between the target and precedent.
    """
    if event.event_type != EventType.ORGANIZATION.value:
        return None
    expected = required_roles or {"organization_scale"}
    eligible = []
    for row in compatible_scale_counterevidence(
        evidence,
        required_roles=expected,
    ):
        try:
            similarity = float(row.get("similarity") or 0)
            confidence = float(
                row.get("classification_confidence") or 0
            )
        except (TypeError, ValueError):
            continue
        missing_roles = set(
            row.get("missing_roles")
            or classify_non_attachment_missing_roles(
                str(row.get("reason_text") or ""),
                str(row.get("evidence_text") or ""),
            )
        )
        if (
            str(row.get("reason_type") or "") == "B"
            and confidence >= 0.8
            and similarity >= MIN_ROLE_ALIGNED_NON_ATTACHMENT_SIMILARITY
            and bool(expected.intersection(missing_roles))
        ):
            enriched = dict(row)
            enriched["matched_missing_roles"] = sorted(
                expected.intersection(missing_roles)
            )
            eligible.append(enriched)
    return (
        max(
            eligible,
            key=lambda row: (
                float(row.get("similarity") or 0),
                str(row.get("bill_no") or ""),
            ),
        )
        if eligible
        else None
    )

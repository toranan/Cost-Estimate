"""Deterministic, review-only committee component recommendations.

This module is deliberately isolated from ``tag_pipeline`` and ``server``.
It fills the gap below :mod:`backend.committee_precedent_resolver`:

* the existing resolver may execute only a complete, strongly related package;
* this module may *recommend* a meeting-cost component from structurally
  compatible TAG profiles, but can never mark it executable.

The target answer, target amount, bill-number allowlists, and committee-name
overlap are not used for ranking.  Statutory operands win over precedent
operands.  Missing operands remain review fields when the evidence cohort is
not stable enough to justify a point recommendation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any

from backend.article_extraction_engine import extract_pdf_text, split_articles_regex
from backend.committee_estimator_v3.pipeline import (
    _function_compatibility,
    _operating_function,
)
from backend.committee_size_extract import (
    extract_committee_size,
    extract_meeting_frequency,
    extract_paid_member_count,
    extract_paid_member_ratio_floor,
)
from backend.scripts import evaluate_committee_precedent_routes as routes


ENGINE_VERSION = "committee-component-recommender-v0.2.0"

_PROFILE_PATH = Path("backend/generated/committee_evidence_v1/committee_profiles.jsonl")
_ATOM_PATH = Path("backend/generated/committee_evidence_v1/committee_evidence_atoms.jsonl")
_BODY = re.compile(r"위원회|심의회|협의회|전략회의")
_GENERIC_BODY = {"위원회", "심의회", "협의회", "전략회의"}
_SUBORDINATE = re.compile(r"^(?:상임|전문|분과|소|실무)위원회$")
_MAIN_FORMULAS = {
    "paid_members*annual_meetings*meeting_unit_price",
    "committee_instances*paid_members*annual_meetings*meeting_unit_price",
}
_HEAVY_ORGANIZATION = re.compile(
    # ``소관 상임위원회에 보고``는 국회 참조일 뿐 대상 위원회의
    # 유급 상임위원이 아니다.  또한 관계 장관과 ``정무직공무원``이
    # 당연직으로 참여하는 것도 별도 조직 인건비 신호가 아니다.
    r"상임위원(?!회)|(?:위원장|부위원장|위원).{0,50}상임으로|"
    r"(?:위원장|부위원장|위원).{0,50}정무직(?:으로보한다|공무원으로한다)|"
    r"사무처.{0,80}(?:필요한직원|직원의정원)|"
    r"(?:조사권|현장조사|청문회|동행명령|징계권고)"
)
_NON_PUBLIC_CHAIR = re.compile(
    r"위원장은.{0,100}(?:공무원.{0,60}아닌|민간전문가|위촉위원중에서호선)"
)
_HALF_OR_MORE = re.compile(
    r"(?:민간위원|공무원이아닌(?:위원|사람)).{0,60}(?:2분의1|절반)이상|"
    r"위원.{0,30}과반수.{0,60}민간전문가"
)
_CENTRAL = re.compile(
    r"대통령|국무총리|국회|대법원|헌법재판소|중앙선거관리위원회|"
    r"(?:장관|처장|청장)소속"
)
_LOCAL = re.compile(r"지방자치단체|시도지사|도지사|시장|군수|구청장")
_INSTITUTION = re.compile(
    r"(?:국회|대법원|헌법재판소|중앙선거관리위원회|"
    r"[가-힣]{2,18}(?:부|처|청|위원회|법원|재판소))"
)
_GOVERNANCE_TITLE = re.compile(
    r"^(?:(?:위원회|심의회|협의회)(?:의)?"
    r"(?:기능|구성|운영|회의|위원장등의직무|위원장|간사|수당등|수당|"
    r"운영세칙|관계기관등에대한협조요청)?|"
    r"실무위원회|분과위원회|소위원회|전문위원회|실무협의회|"
    r"기능|구성|운영|위원장등의직무|위원장|회의|간사|수당등|수당|운영세칙|"
    r"관계기관등에대한협조요청)$"
)


def _compact(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


def _article_title(article_ref: str) -> str:
    match = re.search(r"\(([^()]*)\)", article_ref or "")
    return _compact(match.group(1) if match else "")


def _bounded_governance_articles(
    articles: list[dict[str, Any]],
    *,
    anchor: int,
    name: str,
    other_names: list[str],
) -> list[dict[str, Any]]:
    """Keep one committee's contiguous governance block only.

    A fixed ``anchor + 5`` window leaked quantities from the next policy
    article (for example an unrelated ``연 1회`` reporting duty) into the
    committee's meeting cadence.  Continue only through articles that name
    the same body or have a conventional governance title, and stop at the
    first unrelated subject.
    """
    selected = [articles[anchor]]
    for index in range(anchor + 1, min(len(articles), anchor + 12)):
        article = articles[index]
        article_text = _compact(
            f"{article.get('title', '')} {article.get('text', '')}"
        )
        if any(
            other != name
            and other in article_text
            and re.search(
                r"둔다|설치한다|둘수있다|구성운영할수있다",
                _compact(str(article.get("text") or "")),
            )
            for other in other_names
        ):
            break
        if name in article_text or _GOVERNANCE_TITLE.fullmatch(
            _article_title(str(article.get("no") or ""))
        ):
            selected.append(article)
            continue
        break
    return selected


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


@dataclass(frozen=True)
class Operand:
    role: str
    value: float
    unit: str
    source: str
    source_text: str


@dataclass(frozen=True)
class StructuralCandidate:
    profile_key: str
    bill_no: str
    bill_name: str
    committee_name: str
    function_class: str
    total_members: float | None
    paid_members: float
    annual_meetings: float
    meeting_unit_price: float
    committee_instances: float
    parent_authority: str
    has_subcommittee: bool
    has_secretariat: bool
    available_date: str
    score: int
    score_breakdown: tuple[tuple[str, int], ...]
    source_texts: tuple[str, ...]
    lifecycle: str = "unknown"

    @property
    def annual_amount_won(self) -> float:
        return (
            self.committee_instances
            * self.paid_members
            * self.annual_meetings
            * self.meeting_unit_price
        )


@dataclass(frozen=True)
class ComponentRecommendation:
    bill_no: str
    target_name: str
    decision: str
    formula: str | None
    point_amount_won: int | None
    amount_range_won: tuple[int, int] | None
    operands: tuple[Operand, ...]
    selected_bill_no: str | None
    selected_item_name: str | None
    confidence: str
    review_fields: tuple[str, ...]
    reasons: tuple[str, ...]
    candidate_top5: tuple[StructuralCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Target:
    name: str
    context: str
    function_class: str
    total_kind: str | None
    total_low: float | None
    total_high: float | None
    paid_exact: float | None
    paid_floor: float | None
    meetings_exact: float | None
    instances: float
    scope: str
    has_subcommittee: bool
    has_secretariat: bool
    heavy_organization: bool
    lifecycle: str = "unknown"


def _unique_numeric(values: list[Any]) -> list[float]:
    output: list[float] = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0 and numeric not in output:
            output.append(numeric)
    return output


@lru_cache(maxsize=1)
def _available_dates() -> dict[str, str]:
    candidates, _ = routes._relation_corpus() if hasattr(routes, "_relation_corpus") else ({}, {})
    # ``_relation_corpus`` belongs to the resolver, not the route evaluator.
    # Fall back to the evaluator's source-only loaders when unavailable.
    if candidates:
        return {bill_no: row.available_date for bill_no, row in candidates.items()}
    metadata = routes._load_metadata()
    return {
        bill_no: row.available_date
        for bill_no, row in routes._load_candidate_texts(metadata).items()
    }


@lru_cache(maxsize=1)
def _profile_rows() -> tuple[dict[str, Any], ...]:
    atoms_by_profile: dict[str, list[dict[str, Any]]] = {}
    for atom in _read_jsonl(_ATOM_PATH):
        profile_key = str(atom.get("profile_key") or "")
        if profile_key:
            atoms_by_profile.setdefault(profile_key, []).append(atom)

    available = _available_dates()
    output: list[dict[str, Any]] = []
    for profile in _read_jsonl(_PROFILE_PATH):
        signature = str(profile.get("formula_signature") or "")
        profile_key = str(profile.get("profile_key") or "")
        atoms = [
            atom
            for atom in atoms_by_profile.get(profile_key, [])
            if atom.get("review_status") != "rejected"
            and "REVERSE_ENGINEERED_VALUE" not in set(atom.get("quality_flags") or [])
        ]

        def role_values(role: str) -> list[float]:
            return _unique_numeric(
                [
                    atom.get("normalized_value")
                    for atom in atoms
                    if atom.get("evidence_role") == role
                ]
            )

        paid = role_values("paid_members")
        # Historical extraction sometimes stored an explicitly stated
        # private/allowance-bearing subset under ``total_members``. Recover
        # that source-grounded operand for review-only ranking; an ordinary
        # legal cap is never converted into a paid-member count.
        if not paid:
            paid = _unique_numeric([
                atom.get("normalized_value")
                for atom in atoms
                if atom.get("evidence_role") == "total_members"
                and re.search(
                    r"(?:민간위원|수당지급(?:대상)?(?:위원|인원)).{0,40}\d+(?:명|인)",
                    _compact(str(atom.get("source_text") or "")),
                )
            ])
        meetings = role_values("annual_meetings")
        prices = role_values("meeting_unit_price")
        instances = role_values("committee_instances") or [1.0]
        # A review recommendation still needs one auditable historical
        # equation.  Ambiguous rows are not silently median-collapsed here.
        if not (len(paid) == len(meetings) == len(prices) == len(instances) == 1):
            continue
        # A profile parser may miss the multiplication sign even when the
        # three independent, source-linked operands are complete. Such rows
        # can enter this review recommender, but never the executable exact
        # package resolver.
        if signature not in _MAIN_FORMULAS:
            source_item_keys = {
                str(atom.get("source_item_key") or "")
                for atom in atoms
                if atom.get("evidence_role")
                in {"paid_members", "total_members", "annual_meetings", "meeting_unit_price"}
                and atom.get("source_item_key")
            }
            if len(source_item_keys) != 1:
                continue
        bill_no = str(profile.get("bill_no") or "")
        if not bill_no or not available.get(bill_no):
            continue
        source_texts = tuple(
            dict.fromkeys(
                str(atom.get("source_text") or "")
                for atom in atoms
                if atom.get("evidence_role")
                in {"paid_members", "total_members", "annual_meetings", "meeting_unit_price"}
                and atom.get("source_text")
            )
        )
        normalized_name = str(
            profile.get("normalized_name") or profile.get("committee_name") or ""
        )
        lifecycle = (
            "establishment"
            if re.search(r"설립(?:준비)?위원회", _compact(normalized_name))
            else str(profile.get("lifecycle") or "unknown")
        )
        output.append(
            {
                "profile": profile,
                "paid": paid[0],
                "meetings": meetings[0],
                "price": prices[0],
                "instances": instances[0],
                "available_date": available[bill_no],
                "source_texts": source_texts,
                "lifecycle": lifecycle,
            }
        )
    return tuple(output)


def _target_contexts(case: dict[str, Any]) -> list[tuple[str, str]]:
    passages = routes._target_passages(case)
    terms = [
        _compact(str(term))
        for term in case.get("target_terms") or []
        if _BODY.search(_compact(str(term)))
    ]
    specific = [
        term
        for term in terms
        if term not in _GENERIC_BODY and not _SUBORDINATE.fullmatch(term)
    ]
    names = specific or [term for term in terms if not _SUBORDINATE.fullmatch(term)]
    if not names and passages:
        names = ["위원회"]

    # Prefer deterministic article windows over the route evaluator's broad
    # fallback passage.  The fallback can contain an entire short bill, which
    # lets a secretariat or standing-member clause belonging to body A leak
    # into body B's structural profile.
    articles: list[dict[str, str]] = []
    source_pdf = str(case.get("source_pdf") or "")
    if source_pdf:
        source_path = Path(source_pdf)
        if not source_path.is_absolute():
            source_path = routes.ROOT / source_path
        if source_path.exists():
            articles = split_articles_regex(extract_pdf_text(source_path.read_bytes()))

    output: list[tuple[str, str]] = []
    for name in dict.fromkeys(names):
        selected_articles: list[str] = []
        if articles:
            anchor = next(
                (
                    index
                    for index, article in enumerate(articles)
                    if name in _compact(
                        f"{article.get('title', '')} {article.get('text', '')}"
                    )
                    and re.search(
                        r"둔다|설치한다|둘수있다|구성운영할수있다",
                        _compact(str(article.get("text") or "")),
                    )
                ),
                None,
            )
            if anchor is None:
                anchor = next(
                    (
                        index
                        for index, article in enumerate(articles)
                        if name in _compact(
                            f"{article.get('title', '')} {article.get('text', '')}"
                        )
                    ),
                    None,
                )
            if anchor is not None:
                for article in _bounded_governance_articles(
                    articles,
                    anchor=anchor,
                    name=name,
                    other_names=names,
                ):
                    selected_articles.append(
                        f"{article.get('no', '')}({article.get('title', '')}) "
                        f"{article.get('text', '')}"
                    )
        matched = [passage for passage in passages if name in _compact(passage)]
        context = " ".join(selected_articles or matched or passages)
        if context:
            output.append((name, context))
    return output


def _scope(text: str) -> str:
    compact = _compact(text)
    if _LOCAL.search(compact) and not _CENTRAL.search(compact):
        return "local"
    if _CENTRAL.search(compact):
        return "central"
    return "unknown"


def _instance_count(name: str, context: str) -> float:
    compact = _compact(context)
    best = 1
    for match in re.finditer(r"(.{0,180})소속으로" + re.escape(name), compact):
        prefix = re.sub(r"\([^)]*\)", "", match.group(1))
        # Installation lists normally begin after a purpose clause.  Cutting
        # there prevents institutions mentioned in the mandate from being
        # counted as separate committee instances.
        for boundary in ("위하여", "하도록"):
            if boundary in prefix:
                prefix = prefix.rsplit(boundary, 1)[-1]
        institutions = list(dict.fromkeys(_INSTITUTION.findall(prefix)))
        best = max(best, len(institutions))
    return float(best)


def _build_target(name: str, context: str) -> _Target:
    compact = _compact(context)
    size = extract_committee_size(context)
    # The shared extractor deliberately expects ``위원`` after a size phrase.
    # Committee bills also use the equally common short form
    # ``20명 이상 25명 이내로 구성한다``.  Keep this compatibility
    # parser local to the review-only recommender so the production TAG
    # pipeline is not changed while this module is being evaluated.
    if size is None:
        range_match = re.search(
            r"(\d+)(?:명|인)이상(\d+)(?:명|인)(?:이내|이하)(?:로구성|의?위원)",
            compact,
        )
        if range_match:
            size = {
                "kind": "range",
                "low": int(range_match.group(1)),
                "high": int(range_match.group(2)),
            }
        else:
            cap_match = re.search(
                r"(\d+)(?:명|인)이내로구성",
                compact,
            )
            if cap_match:
                size = {"kind": "cap", "value": int(cap_match.group(1))}
    total_kind: str | None = None
    low: float | None = None
    high: float | None = None
    if size:
        total_kind = str(size["kind"])
        if total_kind == "range":
            low, high = float(size["low"]), float(size["high"])
        else:
            high = float(size["value"])
            low = high if total_kind == "exact" else None

    paid_exact = extract_paid_member_count(context)
    paid_floor: float | None = None
    ratio_floor = extract_paid_member_ratio_floor(context)
    if ratio_floor is None and _HALF_OR_MORE.search(compact):
        ratio_floor = 0.5
    if paid_exact is None and high is not None and ratio_floor is not None:
        if "과반수" in compact:
            paid_floor = math.floor(high * ratio_floor) + 1
        else:
            paid_floor = math.ceil(high * ratio_floor)
    if paid_exact is None and paid_floor is None and _NON_PUBLIC_CHAIR.search(compact):
        # If every other member is an office-holder and no separately
        # appointed/private cohort exists, only the non-public chair is a
        # grounded allowance target.  Otherwise the chair is merely a floor.
        if not re.search(r"위촉위원|위원장이위촉|민간전문가", compact):
            paid_exact = 1.0
        else:
            paid_floor = 1.0

    return _Target(
        name=name,
        context=context,
        function_class=_operating_function(context[:2500]),
        total_kind=total_kind,
        total_low=low,
        total_high=high,
        paid_exact=float(paid_exact) if paid_exact is not None else None,
        paid_floor=float(paid_floor) if paid_floor is not None else None,
        meetings_exact=(
            float(value)
            if (value := extract_meeting_frequency(context)) is not None
            else None
        ),
        instances=_instance_count(name, context),
        scope=_scope(context),
        has_subcommittee=bool(re.search(r"전문위원회|분과위원회|소위원회|실무위원회", compact)),
        has_secretariat=bool(re.search(r"사무처|사무국|사무기구|지원단|지원센터", compact)),
        heavy_organization=bool(
            _HEAVY_ORGANIZATION.search(compact)
            or ("국회" in compact and "특별위원회" in name)
        ),
        lifecycle=(
            "establishment"
            if re.search(r"설립(?:준비)?위원회", _compact(f"{name} {context}"))
            else "standing"
        ),
    )


def _candidate_scope(profile: dict[str, Any]) -> str:
    text = " ".join(
        [
            str(profile.get("parent_authority") or ""),
            str(profile.get("source_text") or ""),
        ]
    )
    return _scope(text)


def _score(target: _Target, row: dict[str, Any]) -> tuple[int, tuple[tuple[str, int], ...]]:
    profile = row["profile"]
    function = _function_compatibility(
        target.function_class,
        str(profile.get("function_class") or "general"),
    )
    profile_total = profile.get("total_member_count")
    size = 1
    if target.total_high is not None and profile_total not in (None, ""):
        relative_gap = abs(float(profile_total) - target.total_high) / max(target.total_high, 1)
        size = 3 if relative_gap <= 0.10 else 2 if relative_gap <= 0.25 else 1 if relative_gap <= 0.50 else 0
    scope = 1
    candidate_scope = _candidate_scope(profile)
    if target.scope != "unknown":
        scope = 2 if candidate_scope == target.scope else 1 if candidate_scope == "unknown" else 0
    subcommittee = 1 if bool(profile.get("has_subcommittee")) == target.has_subcommittee else 0
    secretariat = 1 if bool(profile.get("has_secretariat")) == target.has_secretariat else 0
    candidate_lifecycle = str(row.get("lifecycle") or "unknown")
    if target.lifecycle == "establishment":
        # A temporary body that prepares one institution's incorporation is
        # not comparable to an ordinary standing or fixed-term policy body.
        # Keep this as a strong soft-ranking feature rather than a pre-filter
        # so recall is retained when the DB has no establishment precedent.
        lifecycle = 5 if candidate_lifecycle == "establishment" else 0
    else:
        lifecycle = (
            2
            if candidate_lifecycle == target.lifecycle
            else 1
            if candidate_lifecycle == "unknown"
            else 0
        )
    breakdown = (
        ("lifecycle", lifecycle),
        ("function", function),
        ("member_scale", size),
        ("institution_scope", scope),
        ("subcommittee", subcommittee),
        ("secretariat", secretariat),
        ("formula_completeness", 3),
    )
    return sum(value for _, value in breakdown), breakdown


def _rank(target: _Target, case: dict[str, Any]) -> list[StructuralCandidate]:
    cutoff = str(case.get("cutoff_date") or case.get("propose_date") or "")
    target_bill_no = str(case.get("bill_no") or "")
    candidates: list[StructuralCandidate] = []
    for row in _profile_rows():
        profile = row["profile"]
        bill_no = str(profile.get("bill_no") or "")
        available_date = str(row["available_date"] or "")
        if bill_no == target_bill_no or not available_date or (cutoff and available_date > cutoff):
            continue
        score, breakdown = _score(target, row)
        # A precedent's paid subset cannot exceed the target's entire legal
        # membership.  Missing profile total is tolerable only when the paid
        # operand itself still fits inside the target cap.
        if target.total_high is not None and float(row["paid"]) > target.total_high:
            continue
        candidates.append(
            StructuralCandidate(
                profile_key=str(profile.get("profile_key") or ""),
                bill_no=bill_no,
                bill_name=str(profile.get("bill_name") or ""),
                committee_name=str(profile.get("normalized_name") or profile.get("committee_name") or ""),
                function_class=str(profile.get("function_class") or "general"),
                total_members=(
                    float(profile["total_member_count"])
                    if profile.get("total_member_count") not in (None, "")
                    else None
                ),
                paid_members=float(row["paid"]),
                annual_meetings=float(row["meetings"]),
                meeting_unit_price=float(row["price"]),
                committee_instances=float(row["instances"]),
                parent_authority=str(profile.get("parent_authority") or ""),
                has_subcommittee=bool(profile.get("has_subcommittee")),
                has_secretariat=bool(profile.get("has_secretariat")),
                available_date=available_date,
                score=score,
                score_breakdown=breakdown,
                source_texts=tuple(row["source_texts"]),
                lifecycle=str(row.get("lifecycle") or "unknown"),
            )
        )
    # Stable structural fields decide before recency.  Names and target-domain
    # token overlap intentionally do not appear in this key.
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.score,
            dict(candidate.score_breakdown)["lifecycle"],
            dict(candidate.score_breakdown)["member_scale"],
            dict(candidate.score_breakdown)["function"],
            candidate.available_date,
            candidate.bill_no,
            candidate.profile_key,
        ),
        reverse=True,
    )


def _scaled_paid(target: _Target, candidate: StructuralCandidate) -> float:
    if target.paid_exact is not None:
        return target.paid_exact
    if target.total_high is not None and candidate.total_members:
        value = round(target.total_high * candidate.paid_members / candidate.total_members)
        value = max(1, min(value, int(target.total_high)))
    else:
        value = int(round(candidate.paid_members))
    if target.paid_floor is not None:
        value = max(value, int(target.paid_floor))
    return float(value)


def _project(target: _Target, candidate: StructuralCandidate) -> tuple[float, float, float, float, int]:
    paid = _scaled_paid(target, candidate)
    meetings = target.meetings_exact or candidate.annual_meetings
    instances = target.instances
    amount = round(instances * paid * meetings * candidate.meeting_unit_price)
    return paid, meetings, candidate.meeting_unit_price, instances, amount


def _recommend_target(case: dict[str, Any], target: _Target) -> ComponentRecommendation:
    ranked = _rank(target, case)
    top = tuple(ranked[:5])
    if target.heavy_organization:
        return ComponentRecommendation(
            bill_no=str(case["bill_no"]),
            target_name=target.name,
            decision="organization_channel_required",
            formula=None,
            point_amount_won=None,
            amount_range_won=None,
            operands=(),
            selected_bill_no=None,
            selected_item_name=None,
            confidence="out_of_scope",
            review_fields=("조직 정원", "직급별 인건비", "기본경비·사업비"),
            reasons=("상임위원·별도 직원 등 조직비용이 핵심이어서 회의수당만으로 축소할 수 없음",),
            candidate_top5=top,
        )
    if not ranked:
        return ComponentRecommendation(
            bill_no=str(case["bill_no"]),
            target_name=target.name,
            decision="needs_user_input",
            formula="instances * paid_members * annual_meetings * meeting_unit_price",
            point_amount_won=None,
            amount_range_won=None,
            operands=(),
            selected_bill_no=None,
            selected_item_name=None,
            confidence="insufficient_db_evidence",
            review_fields=("수당 지급대상 인원", "연간 회의횟수", "1인 1회당 수당"),
            reasons=("회답일 이전의 완결된 위원회 선례가 없음",),
            candidate_top5=(),
        )

    selected = ranked[0]
    # Compare the strongest structural tier only, while deduplicating repeated
    # versions of the same normalized institution.
    tier_rows = [
        candidate
        for candidate in ranked
        if candidate.score >= selected.score - 2
        and dict(candidate.score_breakdown)["function"]
        >= 1
        and (
            target.total_high is None
            or dict(candidate.score_breakdown)["member_scale"] >= 2
        )
    ]
    unique: dict[str, StructuralCandidate] = {}
    for candidate in tier_rows:
        unique.setdefault(candidate.committee_name, candidate)
    cohort = list(unique.values())[:8] or [selected]
    projections = [_project(target, candidate) for candidate in cohort]
    amounts = [projection[4] for projection in projections]
    if target.paid_floor is not None:
        # A legal minimum (for example, non-public chair 1 or private-member
        # half) is an independently grounded lower scenario.  Do not let a
        # precedent ratio silently turn that floor into an exact headcount.
        amounts.extend(
            round(
                target.instances
                * target.paid_floor
                * (target.meetings_exact or candidate.annual_meetings)
                * candidate.meeting_unit_price
            )
            for candidate in cohort
        )
    low, high = min(amounts), max(amounts)
    paid, meetings, price, instances, point = _project(target, selected)

    review_fields: list[str] = []
    if target.total_kind in {"cap", "range", "min"}:
        review_fields.append("실제 위원 정수")
    if target.paid_exact is None:
        review_fields.append("실제 수당 지급대상 인원")
    if target.meetings_exact is None:
        review_fields.append("연간 회의횟수")
    review_fields.append("적용 수당 단가")

    spread = high / max(low, 1)
    independent_bills = {candidate.bill_no for candidate in cohort}
    grounded_size = bool(
        target.total_high is not None
        or target.paid_exact is not None
        or target.paid_floor is not None
    )
    # A generic precedent can suggest cadence, but cannot establish that the
    # target body will actually meet that often.  Without a statutory cadence
    # the reviewer must confirm it even when the historical cohort agrees.
    point_allowed = bool(
        grounded_size
        and target.meetings_exact is not None
        and (
            target.paid_exact is not None
            or (len(independent_bills) >= 2 and spread <= 1.5)
        )
    )
    decision = "component_recommendation" if point_allowed else "needs_user_input"
    reasons = [
        "기구명 유사도가 아니라 기능·위원 규모·소속·분과위·사무조직 구조로 선례를 선택함",
        "조문에 명시된 값은 선례값보다 우선함",
        "추천값은 검토 전용이며 자동 실행하지 않음",
    ]
    if spread > 2.0:
        reasons.append("동일 구조점수 선례들의 금액 차이가 2배를 넘어 단일값을 확정하지 않음")

    operands = (
        Operand(
            role="committee_instances",
            value=instances,
            unit="개",
            source="bill_text" if instances != 1 else "bill_text_default_single",
            source_text=f"조문에서 설치 개체 수 {instances:g}개로 구조화",
        ),
        Operand(
            role="paid_members",
            value=paid,
            unit="명",
            source=(
                "bill_text"
                if target.paid_exact is not None
                else "bill_text_floor+structural_precedent"
                if target.paid_floor is not None
                else "structural_precedent_scaled"
            ),
            source_text=(
                "조문 명시값"
                if target.paid_exact is not None
                else f"구조 선례 {selected.committee_name}의 유급/전체 비율을 대상 규모에 적용"
            ),
        ),
        Operand(
            role="annual_meetings",
            value=meetings,
            unit="회/년",
            source="bill_text" if target.meetings_exact is not None else "structural_precedent",
            source_text=(
                "조문 명시값"
                if target.meetings_exact is not None
                else next((text for text in selected.source_texts if "회" in text), "TAG 선례값")
            ),
        ),
        Operand(
            role="meeting_unit_price",
            value=price,
            unit="원/인·회",
            source="structural_precedent",
            source_text=next(
                (text for text in selected.source_texts if "수당" in text or "만원" in text),
                "TAG 선례값",
            ),
        ),
    )
    return ComponentRecommendation(
        bill_no=str(case["bill_no"]),
        target_name=target.name,
        decision=decision,
        formula="instances * paid_members * annual_meetings * meeting_unit_price",
        point_amount_won=point if point_allowed else None,
        # A total range is still a total-cost claim.  Historical cadence is
        # useful reviewer evidence, but cannot establish the target body's
        # workload.  Publish a money range only when the bill itself fixes
        # the meeting frequency; otherwise expose candidates and request the
        # missing operand.
        amount_range_won=(
            (int(low), int(high))
            if grounded_size and target.meetings_exact is not None
            else None
        ),
        operands=operands,
        selected_bill_no=selected.bill_no,
        selected_item_name=selected.committee_name,
        confidence="review_only",
        review_fields=tuple(dict.fromkeys(review_fields)),
        reasons=tuple(reasons),
        candidate_top5=top,
    )


def recommend(case: dict[str, Any]) -> list[ComponentRecommendation]:
    """Return source-only, non-executable component recommendations."""
    targets = [
        _build_target(name, context)
        for name, context in _target_contexts(case)
    ]
    if not targets:
        return [
            ComponentRecommendation(
                bill_no=str(case["bill_no"]),
                target_name="위원회",
                decision="needs_user_input",
                formula="instances * paid_members * annual_meetings * meeting_unit_price",
                point_amount_won=None,
                amount_range_won=None,
                operands=(),
                selected_bill_no=None,
                selected_item_name=None,
                confidence="target_structure_missing",
                review_fields=("위원회 명칭", "수당 지급대상 인원", "연간 회의횟수", "수당 단가"),
                reasons=("조문에서 독립된 위원회 개체를 구조화하지 못함",),
                candidate_top5=(),
            )
        ]
    return [_recommend_target(case, target) for target in targets]

from __future__ import annotations

from dataclasses import dataclass
import re
import statistics
from typing import Any

from backend.analyzer_v2 import (
    _extract_bill_name,
    _extract_current_bill_no,
    _extract_pdf_text_from_bytes,
    split_articles,
    strip_appendices,
)
from backend.estimator_v2.retrieval import (
    search_companion_formula_candidates,
    search_cost_candidates,
    target_metadata,
)
from backend.estimator_v2.types import AtomicEvent, TagCandidate
from backend.estimator_v2.variable_selection import (
    _committee_function,
    _is_second_order_analogy,
    _target_member_cap,
    _target_payable_member_count,
    candidate_options,
    has_time_varying_operands,
)

from .types import (
    CandidateAudit,
    CommitteeEstimate,
    CommitteeEstimateResult,
    EvidenceValue,
)
from .evidence_store import (
    candidate_profile,
    enrich_candidates,
    normalize_committee_name,
)


ENGINE_VERSION = "committee-estimator-v3.5.0"

_BODY = r"(?:위원회|심의회|협의회)"
_ESTABLISHMENT = re.compile(
    rf"{_BODY}(?:\([^)]{{0,60}}\))?(?:를|을)?(?:둔다|설치한다|둘수있다)"
)
_TITLE_NAME = re.compile(rf"\(([^()]{{2,60}}{_BODY}[^()]*)\)")
_DEFINED_NAME = re.compile(
    rf"([가-힣A-Za-z0-9ㆍ·]{2,50}{_BODY})\s*\(\s*이하"
)
_HEAVY_ORGANIZATION = re.compile(
    r"(?:사무처|사무국).{0,20}(?:설치|신설|를둔다|을둔다)|"
    r"(?:직원|정원).{0,20}(?:별도로|따로|두며|둔다)|"
    r"상임위원|정무직공무원|별도정원|조사권|압수수색|동행명령|청문회|"
    r"직원.{0,20}징계|징계위원회"
)
_EVENT_DRIVEN = re.compile(
    r"(?:선출|지명|추천|임명).{0,30}(?:때마다|할때마다)|"
    r"(?:때마다|할때마다).{0,30}(?:구성|소집|개최)"
)
_SUBORDINATE_BODY = re.compile(
    r"(?:\uC18C\uC704\uC6D0\uD68C|\uBD84\uACFC\uC704\uC6D0\uD68C|"
    r"\uC2E4\uBB34\uC704\uC6D0\uD68C|\uD611\uC758\uCCB4).{0,50}"
    r"(?:\uB454\uB2E4|\uB458\uC218\uC788\uB2E4|\uC124\uCE58\uD55C\uB2E4|"
    r"\uAD6C\uC131\uD55C\uB2E4)"
)


@dataclass(frozen=True)
class _CommitteeTarget:
    name: str
    article_refs: tuple[str, ...]
    texts: tuple[str, ...]
    change_type: str
    event: AtomicEvent

    @property
    def context(self) -> str:
        return " ".join(self.texts)


def _compact(value: str) -> str:
    compact = re.sub(r"\s+", "", value or "")
    # Assembly PDFs often insert a page marker in the middle of a word:
    # ``둔 - 4 - 다``.  Remove only the numeric page marker before applying
    # legal grammar; do not loosen the establishment expression itself.
    return re.sub(r"-\d+-", "", compact)


def _operating_function(text: str) -> str:
    """Classify the workload mechanism that determines meeting cadence.

    A single ``policy_deliberation`` bucket is too broad: a standing council
    reviewing a five-year development plan does not meet like a supply-demand
    forecasting panel or a dispute board.  These classes describe operating
    mechanics and are reusable across policy domains.
    """
    compact = _compact(text)
    if re.search(r"공론조사|공론절차|공론화|숙의|국민참여|시민참여|주민참여", compact):
        return "participatory_deliberation"
    if re.search(r"분쟁|징계|판정|심판|인허가|처분|제재|조정신청|사건조정", compact):
        return "case_adjudication"
    if re.search(r"임금|보수|근로조건|노사|노동조건", compact):
        return "wage_bargaining"
    if re.search(r"수급추계|수요추계|공급추계|기술평가|안전성평가|영향평가", compact):
        return "technical_forecast_or_evaluation"
    if re.search(
        r"종합계획|기본계획|육성계획|발전계획|진흥계획|"
        r"발전위원회|진흥위원회|주요추진과제|정책심의",
        compact,
    ):
        return "standing_strategy"
    return _committee_function(text)


def _clean_title_name(value: str) -> str:
    value = re.sub(r"\s+", "", value)
    value = re.sub(
        r"의?(?:설치에관한특례|설치(?:및(?:구성|운영|기능|심의사항))?|"
        r"구성(?:및운영)?|운영)$",
        "",
        value,
    )
    return value.strip()


def _committee_name(article: dict[str, Any]) -> str | None:
    title = str(article.get("no") or "")
    text = str(article.get("text") or "")
    title_match = _TITLE_NAME.search(title)
    if title_match:
        name = _clean_title_name(title_match.group(1))
        if re.search(_BODY, name) and name not in {"위원회", "심의회", "협의회"}:
            # A legal title may include a geographic description that is not
            # part of the body's actual name, e.g. ``전북자치도갯벌관리위원회
            # 설치에 관한 특례`` while the operative clause creates
            # ``갯벌관리위원회``. Choose the longest title suffix that is
            # repeated immediately before the establishment verb.
            compact_text = _compact(text)
            for offset in range(len(name)):
                suffix = name[offset:]
                if len(suffix) < 3 or not re.search(_BODY + r"$", suffix):
                    continue
                if re.search(
                    re.escape(suffix)
                    + r"(?:\([^)]{0,60}\))?(?:를|을)?(?:둔다|설치한다|둘수있다)",
                    compact_text,
                ):
                    return suffix
            return name
    defined = _DEFINED_NAME.search(_compact(text))
    if defined:
        return defined.group(1)
    return None


def _is_anchor(article: dict[str, Any]) -> bool:
    text = _compact(str(article.get("text") or ""))
    name = _committee_name(article)
    if not name or name in {"위원회", "심의회", "협의회"}:
        return False
    # A composition article for the main body can say that a *subcommittee*
    # may be established.  The old broad verb check treated that sentence as
    # a second main-body anchor and cut the main target off before its member
    # count.  Start a new cost event only when the body identified from the
    # article title is itself immediately attached to the establishment verb.
    return bool(
        re.search(
            re.escape(_compact(name))
            + r"(?:\([^)]{0,60}\))?(?:를|을)?(?:둔다|설치한다|둘수있다)",
            text,
        )
    )


def _actor(context: str) -> str:
    compact = _compact(context)
    match = re.search(r"([가-힣]{2,25}(?:장관|총리|위원장|국회))소속(?:으로|하에)", compact)
    return match.group(1) if match else "국가기관"


def _find_targets(articles: list[dict[str, Any]]) -> list[_CommitteeTarget]:
    anchors = [index for index, article in enumerate(articles) if _is_anchor(article)]
    targets: list[_CommitteeTarget] = []
    for anchor_pos, index in enumerate(anchors):
        article = articles[index]
        name = _committee_name(article)
        if not name:
            continue
        next_anchor = anchors[anchor_pos + 1] if anchor_pos + 1 < len(anchors) else len(articles)
        # Composition, meetings, allowances and operating clauses normally
        # follow the establishment article.  Keep a bounded legal window and
        # stop before the next independently named committee.
        end = min(next_anchor, index + 16)
        window = articles[index:end]
        texts = tuple(str(value.get("text") or "") for value in window)
        refs = tuple(str(value.get("no") or "") for value in window)
        context = " ".join(texts)
        obligation = "discretionary" if "둘수있다" in _compact(article["text"]) else "mandatory"
        event = AtomicEvent(
            id=f"COMMITTEE-{len(targets) + 1:03d}",
            segment_ids=[],
            article_refs=list(refs),
            quotes=list(texts),
            actor=_actor(context),
            action="설치 및 운영",
            object=name,
            bearer="state",
            event_type="committee_operation",
            obligation=obligation,
            cost_mechanism="internal_administration",
            additionality="explicit_new_or_expanded",
            recurrence_text="",
            explanation="법률 조문에서 결정적으로 추출한 위원회 설치·운영 사건",
            grounded=True,
        )
        targets.append(
            _CommitteeTarget(
                name=name,
                article_refs=refs,
                texts=texts,
                change_type=str(article.get("change_type") or ""),
                event=event,
            )
        )
    return targets


def _organization_scope_context(target: _CommitteeTarget) -> str:
    """Keep organization signals inside provisions attached to this body."""
    selected: list[str] = []
    related_title = re.compile(
        r"\uC704\uC6D0|\uD68C\uC758|\uC0AC\uBB34\uCC98|\uC0AC\uBB34\uAD6D|"
        r"\uC804\uBB38\uC704\uC6D0|\uC18C\uC704\uC6D0|\uBD84\uACFC\uC704\uC6D0|"
        r"\uC2E4\uBB34\uC704\uC6D0"
    )
    for index, (article_ref, text) in enumerate(zip(target.article_refs, target.texts)):
        title_match = re.search(r"\(([^()]*)\)", article_ref)
        title = _compact(title_match.group(1) if title_match else "")
        if index == 0 or related_title.search(title):
            selected.append(text)
    return " ".join(selected)


def _is_multi_instance(target: _CommitteeTarget) -> bool:
    # Multiplicity is an establishment property. Later references to local
    # governments must not turn one national committee into many instances.
    establishment = _compact(target.texts[0] if target.texts else "")
    name = _compact(target.name)
    named_creation = re.search(
        re.escape(name)
        + r"(?:\([^)]{0,60}\))?(?:를|을)?(?:둔다|설치한다|둘수있다)",
        establishment,
    )
    if not named_creation:
        return False
    prefix = establishment[max(0, named_creation.start() - 140):named_creation.start()]
    return bool(
        re.search(
            r"(?:각|모든)?지방자치단체(?:마다|별로|에|의)",
            prefix,
        )
    )


def _has_compound_committee_scope(target: _CommitteeTarget) -> bool:
    normalized_name = normalize_committee_name(target.name)
    if any(
        label in normalized_name
        for label in (
            "\uC18C\uC704\uC6D0\uD68C",
            "\uBD84\uACFC\uC704\uC6D0\uD68C",
            "\uC2E4\uBB34\uC704\uC6D0\uD68C",
            "\uD611\uC758\uCCB4",
        )
    ):
        return False
    return bool(_SUBORDINATE_BODY.search(_compact(target.context)))


def _role_options(candidate: TagCandidate) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {"count": [], "unit_price": [], "frequency": []}
    for option in candidate_options(candidate):
        if option.role in grouped:
            grouped[option.role].append(option)
    return grouped


def _plan_periods(text: str) -> set[int]:
    compact = _compact(text)
    values: set[int] = set()
    for pattern in (
        r"(\d+)년마다",
        r"(\d+)년주기",
        r"수립주기.{0,20}?(\d+)년",
    ):
        values.update(int(value) for value in re.findall(pattern, compact))
    return {value for value in values if 1 <= value <= 20}


def _companion_period_score(target: _CommitteeTarget, candidate: TagCandidate) -> int:
    target_periods = _plan_periods(target.context)
    if not target_periods:
        return 1
    research_contexts = [
        item
        for item in candidate.context_items
        if str(item.get("family") or "") == "research"
    ]
    if not research_contexts:
        return 0
    candidate_text = " ".join(str(item) for item in research_contexts)
    candidate_periods = _plan_periods(candidate_text)
    if target_periods.intersection(candidate_periods):
        return 3
    if candidate_periods:
        return 0
    return 1


def _target_function(target: _CommitteeTarget) -> str:
    """Classify only the establishment/mandate article.

    The wider legal window is necessary for composition and allowance
    extraction, but later articles can mention unrelated mechanisms such as
    resident participation or technical evaluation. Those words must not
    redefine the operating function of the named committee.
    """
    mandate = target.texts[0] if target.texts else target.context
    return _operating_function(mandate)


def _function_compatibility(target_function: str, candidate_function: str) -> int:
    if target_function == candidate_function:
        return 3
    strategy = {"standing_strategy", "policy_deliberation"}
    if target_function in strategy and candidate_function in strategy:
        return 2
    # ``general`` means the deterministic classifier found no decisive
    # mechanism. It is uncertainty, not evidence of incompatibility.
    if "general" in {target_function, candidate_function}:
        return 1
    return 0


def _target_total_member_range(text: str) -> tuple[float | None, float | None]:
    compact = _compact(text)
    ranged = re.search(
        r"(\d+)(?:명|인)이상(\d+)(?:명|인)이하의?위원",
        compact,
    )
    if ranged:
        return float(ranged.group(1)), float(ranged.group(2))
    exact = re.search(
        r"(?:위원장\d*(?:명|인)(?:을|를)?포함(?:하여|한)?|)"
        r"(\d+)(?:명|인)(?:의위원)?으로구성",
        compact,
    )
    if exact:
        value = float(exact.group(1))
        return value, value
    cap = _target_member_cap(text)
    return (None, float(cap)) if cap else (None, None)


def _profile_signals(
    target: _CommitteeTarget,
    candidate: TagCandidate,
) -> tuple[int, int, int, int, int]:
    """Return function, identity, structure, evidence, and institution signals.

    Embeddings remain the broad recall channel. These signals rerank the
    shortlist using stable metadata extracted from precedent documents rather
    than surface token overlap or target answer amounts.
    """
    profile = candidate_profile(candidate)
    target_function = _target_function(target)
    fallback_context = " ".join(
        [
            candidate.bill_name,
            candidate.item_name,
            candidate.formula,
            *[str(variable.get("source_text") or "") for variable in candidate.variables],
        ]
    )
    candidate_function = str(
        (profile or {}).get("function_class")
        or _operating_function(fallback_context)
    )
    function_score = _function_compatibility(target_function, candidate_function)
    if profile is None:
        return function_score, 0, 0, 0, 1

    target_name = normalize_committee_name(target.name)
    candidate_name = normalize_committee_name(
        str(profile.get("normalized_name") or profile.get("committee_name") or "")
    )
    exact_identity = bool(target_name and target_name == candidate_name)

    lifecycle_score = 1 if profile.get("lifecycle") == "standing" else 0
    formula_score = 1 if profile.get("formula_signature") == (
        "paid_members*annual_meetings*meeting_unit_price"
    ) else 0
    target_min, target_max = _target_total_member_range(target.context)
    candidate_total = profile.get("total_member_count")
    size_score = 0
    if candidate_total not in (None, "") and target_max is not None:
        numeric_total = float(candidate_total)
        relative_gap = abs(numeric_total - target_max) / target_max
        size_score = (
            3 if relative_gap == 0
            else 2 if relative_gap <= 0.25
            else 1 if relative_gap <= 0.50
            else 0
        )
    structure_score = lifecycle_score + formula_score + size_score
    quality_score = 3 if profile.get("profile_status") == "complete" else 1

    def institution_scope(value: str) -> str:
        compact = _compact(value)
        if re.search(
            r"지방자치단체|특별자치도|특별자치시|도지사|시장|군수|구청장",
            compact,
        ):
            return "local"
        if re.search(r"대통령|국무총리|장관|처장|청장", compact):
            return "central"
        return "unknown"

    target_actor = _actor(target.texts[0] if target.texts else target.context)
    target_scope = institution_scope(
        target_actor
        if target_actor != "국가기관"
        else (target.texts[0] if target.texts else target.context)
    )
    candidate_scope = institution_scope(
        " ".join(
            [
                str(profile.get("parent_authority") or ""),
                str(profile.get("source_text") or ""),
            ]
        )
    )
    if target_scope == "local":
        institution_score = (
            3 if candidate_scope == "local"
            else 1 if candidate_scope == "unknown"
            else 0
        )
    elif target_scope == "central":
        # Parent-authority metadata is often missing in older TAG rows. For a
        # central target, ``unknown`` is therefore not weaker than an explicit
        # central label; only a clearly local precedent is incompatible.
        institution_score = 0 if candidate_scope == "local" else 1
    else:
        institution_score = 1

    # A near-identity is not inferred from text overlap. It requires a very
    # strong semantic retrieval score plus agreement on all available stable
    # structural fields. This supports renamed/re-parented versions of the
    # same institution while keeping ordinary analogies separate.
    required_structure = 5 if target_max is not None else 2
    near_identity = (
        candidate.similarity >= 0.80
        and function_score >= 2
        and structure_score == required_structure
    )
    identity_score = 3 if exact_identity else 2 if near_identity else 0
    return (
        function_score,
        identity_score,
        structure_score,
        quality_score,
        institution_score,
    )


def _candidate_audit(
    target: _CommitteeTarget,
    candidate: TagCandidate,
    *,
    target_count: float | None,
) -> CandidateAudit:
    grouped = _role_options(candidate)
    complete = all(len(grouped[role]) == 1 for role in grouped)
    (
        function_score,
        identity_score,
        profile_structure_score,
        profile_evidence_score,
        institution_score,
    ) = _profile_signals(target, candidate)

    member_cap = _target_member_cap(target.context)
    scale_score = 1
    if complete and target_count:
        candidate_count = grouped["count"][0].canonical_value
        ratio = candidate_count / float(target_count)
        if 0 < ratio <= 1.0:
            scale_score = 3 if ratio >= 0.60 else 2
        else:
            scale_score = 0
    elif complete and member_cap:
        # A statutory cap is the total membership, while the operand is the
        # paid non-public subset.  Closeness is meaningless; only reject a
        # precedent whose paid subset exceeds the target's entire committee.
        candidate_count = grouped["count"][0].canonical_value
        scale_score = 2 if 0 < candidate_count <= member_cap else 0
    elif complete:
        scale_score = 2

    source_evidence = " ".join(
        option.source_text
        for values in grouped.values()
        for option in values
    )
    target_has_subcommittee = bool(
        re.search(r"분과위원회|실무위원회|소위원회", _compact(target.context))
    )
    profile = candidate_profile(candidate)
    if profile and profile.get("formula_signature") == (
        "paid_members*annual_meetings*meeting_unit_price"
    ):
        # The additive profile has already separated the priced main body
        # from surrounding precedent narrative. A borrowed source paragraph
        # may mention another body's subcommittee even when the executable
        # equation prices only the main committee.
        candidate_has_subcommittee = False
    else:
        candidate_has_subcommittee = bool(
            re.search(r"분과위원회|실무위원회|소위원회", _compact(source_evidence))
        )
    reverse_engineered = bool(re.search(r"역산|산출금액.{0,20}적용", source_evidence))
    same_cost_scope = target_has_subcommittee or not candidate_has_subcommittee
    second_order = any(
        _is_second_order_analogy(option)
        for values in grouped.values()
        for option in values
    )
    direct = complete and same_cost_scope and not reverse_engineered and not second_order
    provenance_score = 3 if direct else (
        2
        if complete
        and same_cost_scope
        and not reverse_engineered
        and second_order
        and identity_score >= 2
        else 0
    )
    similarity_score = (
        3 if candidate.similarity >= 0.60
        else 2 if candidate.similarity >= 0.52
        else 1 if candidate.similarity >= 0.40
        else 0
    )
    context_score = (
        3 if candidate.context_similarity >= 0.44
        else 2 if candidate.context_similarity >= 0.41
        else 1 if candidate.context_similarity >= 0.38
        else 0
    )
    scores = {
        "formula_completeness": 3 if complete and not has_time_varying_operands(candidate) else 0,
        "committee_function": function_score,
        "institutional_identity": identity_score,
        "profile_structure": profile_structure_score,
        "profile_evidence": profile_evidence_score,
        "institution_scope": institution_score,
        "target_scale_compatibility": scale_score,
        "direct_provenance": provenance_score,
        "companion_period": _companion_period_score(target, candidate),
        "context_bundle": context_score,
        "semantic_recall": similarity_score,
    }
    reasons: list[str] = []
    if scores["formula_completeness"] < 3:
        reasons.append("count·frequency·unit_price가 한 선례에 유일하게 완결되지 않음")
    if function_score == 0 and identity_score < 2:
        reasons.append("대상 위원회와 회의 작동 방식이 명백히 다름")
    if scale_score < 2:
        reasons.append("조문상 규모와 선례 인원이 양립하지 않음")
    if scores["direct_provenance"] < 2:
        reasons.append("선례의 비용범위가 다르거나 변수가 재차용·역산되어 직접 준용할 수 없음")
    accepted = not reasons
    return CandidateAudit(
        item_id=candidate.item_id,
        bill_no=candidate.bill_no,
        bill_name=candidate.bill_name,
        item_name=candidate.item_name,
        embedding_similarity=candidate.similarity,
        scores=scores,
        accepted=accepted,
        rejection_reasons=tuple(reasons),
    )


def _candidate_key(audit: CandidateAudit) -> tuple[Any, ...]:
    scores = audit.scores
    return (
        1 if audit.accepted else 0,
        scores["institutional_identity"],
        scores["committee_function"],
        scores["institution_scope"],
        scores["companion_period"],
        scores["context_bundle"],
        (
            scores["profile_structure"]
            if scores["institution_scope"] == 3
            else 0
        ),
        scores["target_scale_compatibility"],
        scores["formula_completeness"],
        scores["semantic_recall"],
        audit.embedding_similarity,
        scores["profile_structure"],
        scores["profile_evidence"],
        scores["direct_provenance"],
        audit.total_score,
        audit.item_id,
    )


def _merge_candidates(*groups: list[TagCandidate]) -> list[TagCandidate]:
    merged: dict[str, TagCandidate] = {}
    for group in groups:
        for candidate in group:
            existing = merged.get(candidate.item_id)
            if existing is None or candidate.similarity > existing.similarity:
                merged[candidate.item_id] = candidate
                existing = candidate
            if existing is not None and candidate.context_items:
                existing.context_items = candidate.context_items
                existing.context_families = candidate.context_families
                existing.context_similarity = max(
                    existing.context_similarity,
                    candidate.context_similarity,
                )
    return list(merged.values())


def _candidate_gross_annual_million(candidate: TagCandidate) -> float | None:
    grouped = _role_options(candidate)
    if not all(len(grouped[role]) == 1 for role in grouped):
        return None
    return (
        grouped["count"][0].canonical_value
        * grouped["frequency"][0].canonical_value
        * grouped["unit_price"][0].canonical_value
        / 1_000_000
    )


def _target_has_nonpaid_roles(text: str) -> bool:
    compact = _compact(text)
    return bool(
        re.search(r"당연직위원.{0,40}위촉직위원|위촉직위원.{0,40}당연직위원", compact)
        or re.search(
            r"(?:장관|차관|도지사|시장|군수|구청장|공무원).{0,50}(?:위원장|위원)",
            compact,
        )
    )


def _cohort_paid_member_count(
    target: _CommitteeTarget,
    pairs: list[tuple[TagCandidate, CandidateAudit]],
    selected_count: float,
) -> tuple[float, str, str] | None:
    """Scale a precedent cohort's paid/total ratio to the target legal cap.

    This fallback is deliberately narrow. It is used only when the target law
    names public/non-paid roles but the selected precedent would treat at
    least 85% of all statutory seats as paid. The cohort must contain two
    independent, complete, function-compatible profiles. No target answer or
    official target estimate is consulted.
    """
    _, target_cap = _target_total_member_range(target.context)
    if (
        target_cap is None
        or target_cap <= 0
        or selected_count / target_cap < 0.85
        or not _target_has_nonpaid_roles(target.context)
    ):
        return None

    rows: list[tuple[int, float, float, str, str]] = []
    for candidate, audit in pairs:
        if not audit.accepted or audit.scores.get("committee_function", 0) < 2:
            continue
        profile = candidate_profile(candidate)
        if not profile or profile.get("profile_status") != "complete":
            continue
        total = profile.get("total_member_count")
        paid = profile.get("paid_member_count")
        if total in (None, "") or paid in (None, ""):
            continue
        total_value = float(total)
        paid_value = float(paid)
        if total_value <= 0 or not (0 < paid_value < total_value):
            continue
        rows.append(
            (
                audit.scores["committee_function"],
                abs(total_value - target_cap) / target_cap,
                paid_value / total_value,
                candidate.bill_no,
                normalize_committee_name(str(profile.get("normalized_name") or candidate.item_name)),
            )
        )
    if not rows:
        return None
    best_function = max(row[0] for row in rows)
    rows = [row for row in rows if row[0] == best_function]
    nearest_gap = min(row[1] for row in rows)
    rows = [row for row in rows if row[1] <= nearest_gap + 0.35]

    # Repeated templates with the same normalized institution must not get
    # extra votes merely because several bills copied the same precedent.
    unique: dict[str, tuple[int, float, float, str, str]] = {}
    for row in sorted(rows, key=lambda value: (value[1], value[3])):
        unique.setdefault(row[4], row)
    rows = list(unique.values())
    if len({row[3] for row in rows}) < 2:
        return None

    ratio = statistics.median(row[2] for row in rows)
    estimated = float(max(1, min(int(round(target_cap * ratio)), int(target_cap) - 1)))
    bills = sorted({row[3] for row in rows})
    source_id = "COHORT:" + ",".join(bills)
    source_text = (
        f"독립 선례 {len(bills)}건의 수당대상/전체위원 비율 중앙값 "
        f"{ratio:.2f}를 조문상 정원 {target_cap:g}명에 적용"
    )
    return estimated, source_id, source_text


def _precedent_disagreement(
    pairs: list[tuple[TagCandidate, CandidateAudit]],
) -> tuple[bool, str]:
    """Abstain when equally strong independent precedents imply incompatible costs.

    A single highest row is not meaningful when three or more different bills
    receive the same structural score but their implied annual costs differ by
    more than a factor of two.  The official target amount is never consulted.
    """
    accepted = [(candidate, audit) for candidate, audit in pairs if audit.accepted]
    if not accepted:
        return False, ""
    # Profile completeness improves confidence but must not manufacture a
    # unique winner among otherwise equivalent precedents. Compare cases on
    # the evidence gates that existed before profile enrichment, while first
    # respecting a genuine institutional-identity advantage.
    top_identity = max(
        audit.scores.get("institutional_identity", 0)
        for _, audit in accepted
    )
    accepted = [
        pair for pair in accepted
        if pair[1].scores.get("institutional_identity", 0) == top_identity
    ]
    high_quality = [
        pair for pair in accepted
        if pair[1].scores.get("profile_evidence", 0) == 3
    ]
    if len({candidate.bill_no for candidate, _ in high_quality}) >= 3:
        accepted = high_quality

    def disagreement_tier(audit: CandidateAudit) -> int:
        return sum(
            audit.scores.get(key, 0)
            for key in (
                "formula_completeness",
                "committee_function",
                "target_scale_compatibility",
                "direct_provenance",
                "companion_period",
                "context_bundle",
                "semantic_recall",
            )
        )

    top_score = max(disagreement_tier(audit) for _, audit in accepted)
    by_bill: dict[str, float] = {}
    for candidate, audit in accepted:
        if disagreement_tier(audit) != top_score or candidate.bill_no in by_bill:
            continue
        amount = _candidate_gross_annual_million(candidate)
        if amount is not None and amount > 0:
            by_bill[candidate.bill_no] = amount
    values = list(by_bill.values())
    if len(values) < 3:
        return False, ""
    ratio = max(values) / min(values)
    if ratio <= 2.0:
        return False, ""
    return (
        True,
        f"동일 구조점수의 독립 선례 {len(values)}건이 연간 비용을 "
        f"{min(values):.1f}~{max(values):.1f}백만원으로 제시해 2배 이상 불일치",
    )


def _event_driven_result(target: _CommitteeTarget) -> CommitteeEstimate:
    """Route appointment/event-triggered committees away from annual cadence."""
    member_cap = _target_member_cap(target.context)
    variables: list[EvidenceValue] = []
    if member_cap:
        variables.append(
            _value(
                "committee_members_per_event",
                "조문상 위원회당 전체 위원 수",
                float(member_cap),
                "명/건",
                "law_text",
                "TARGET_DOCUMENT",
                f"조문상 위원회 정원 {member_cap}명",
            )
        )
    return CommitteeEstimate(
        committee_name=target.name,
        article_refs=list(target.article_refs),
        source_quotes=list(target.texts),
        status="review_required",
        reason_codes=["EVENT_DRIVEN_FORMULA_REQUIRED"],
        reason=(
            "선출·지명 때마다 구성되는 비정기 위원회입니다. 상설 위원회의 연간 회의 횟수를 "
            "준용하지 않고 연도별 사건 일정과 기존 제도 기준값을 확인해야 합니다."
        ),
        formula=(
            "연도별 위원회 구성 건수 × 건당 순증 수당 지급대상 인원 "
            "× 위원회당 회의 횟수 × 1인 1회당 수당"
        ),
        variables=variables,
        evidence_coverage=round(len(variables) / 5, 2),
        calibrated_confidence=0.0,
        review_fields=[
            _review_field("event_counts_by_year", "연도별 위원회 구성 건수", "임기와 선출·지명 일정을 반영해야 합니다.", "건/년"),
            _review_field("incremental_paid_members", "건당 순증 수당 지급대상", "기존 추천위원회 구성과 개정 후 구성을 비교해야 합니다.", "명/건"),
            _review_field("meetings_per_event", "위원회당 회의 횟수", "한 차례 추천에 필요한 실제 회의 횟수가 필요합니다.", "회/건"),
            _review_field("unit_price_won", "적용 수당 단가", "회의수당과 안건검토비의 실제 기준이 필요합니다.", "원/명·회"),
        ],
    )


def _value(
    key: str,
    label: str,
    value: float | None,
    unit: str,
    provenance: str,
    source_id: str,
    source_text: str,
    *,
    review: bool = False,
) -> EvidenceValue:
    return EvidenceValue(
        key=key,
        label=label,
        value=value,
        unit=unit,
        provenance=provenance,
        source_id=source_id,
        source_text=source_text,
        requires_review=review,
    )


def _review_field(key: str, label: str, reason: str, unit: str) -> dict[str, str]:
    return {"key": key, "label": label, "reason": reason, "unit": unit}


def _multi_instance_result(target: _CommitteeTarget) -> CommitteeEstimate:
    member_cap = _target_member_cap(target.context)
    variables: list[EvidenceValue] = []
    if member_cap:
        variables.append(
            _value(
                "members_per_committee_cap",
                "\uC704\uC6D0\uD68C\uB2F9 \uC870\uBB38\uC0C1 \uC804\uCCB4 \uC704\uC6D0 \uC0C1\uD55C",
                float(member_cap),
                "\uBA85/\uC704\uC6D0\uD68C",
                "law_text",
                "TARGET_DOCUMENT",
                f"\uC870\uBB38\uC0C1 \uC704\uC6D0\uD68C\uB2F9 {member_cap}\uBA85 \uC774\uB0B4",
            )
        )
    return CommitteeEstimate(
        committee_name=target.name,
        article_refs=list(target.article_refs),
        source_quotes=list(target.texts),
        status="review_required",
        reason_codes=["MULTI_INSTANCE_FORMULA_REQUIRED"],
        reason=(
            "\uBCF5\uC218 \uAE30\uAD00\uC5D0 \uAC01\uAC01 \uC124\uCE58\uB418\uB294 \uC704\uC6D0\uD68C\uC785\uB2C8\uB2E4. \uB2E8\uC77C \uC704\uC6D0\uD68C \uC120\uB840\uB97C "
            "\uD55C \uBC88\uB9CC \uACC4\uC0B0\uD558\uC9C0 \uC54A\uACE0 \uC2E4\uC81C \uC124\uCE58 \uAE30\uAD00 \uC218\uB97C \uD655\uC778\uD574\uC57C \uD569\uB2C8\uB2E4."
        ),
        formula=(
            "\uC124\uCE58 \uAE30\uAD00 \uC218 \u00d7 \uAE30\uAD00\uBCC4 \uC21C\uC99D \uC218\uB2F9 \uC9C0\uAE09\uB300\uC0C1 \u00d7 "
            "\uC5F0\uAC04 \uD68C\uC758 \uD69F\uC218 \u00d7 1\uC778 1\uD68C\uB2F9 \uC218\uB2F9"
        ),
        variables=variables,
        evidence_coverage=round(len(variables) / 5, 2),
        review_fields=[
            _review_field("committee_instances", "\uC2E4\uC81C \uC124\uCE58 \uAE30\uAD00 \uC218", "\uBC95\uB960\uC548\uB9CC\uC73C\uB85C \uCC38\uC5EC \uAE30\uAD00 \uC218\uB97C \uD655\uC815\uD560 \uC218 \uC5C6\uC2B5\uB2C8\uB2E4.", "\uAC1C"),
            _review_field("paid_members_per_committee", "\uAE30\uAD00\uBCC4 \uC21C\uC99D \uC218\uB2F9 \uC9C0\uAE09\uB300\uC0C1", "\uB2F9\uC5F0\uC9C1 \uACF5\uBB34\uC6D0\uACFC \uAE30\uC874 \uAE30\uAD6C\uB97C \uC81C\uC678\uD574\uC57C \uD569\uB2C8\uB2E4.", "\uBA85/\uC704\uC6D0\uD68C"),
            _review_field("annual_meetings", "\uC5F0\uAC04 \uD68C\uC758 \uD69F\uC218", "\uC2E4\uC81C \uC6B4\uC601\uACC4\uD68D\uC774 \uD544\uC694\uD569\uB2C8\uB2E4.", "\uD68C/\uB144"),
            _review_field("unit_price_won", "\uC801\uC6A9 \uC218\uB2F9 \uB2E8\uAC00", "\uD574\uB2F9 \uAE30\uAD00\uC758 \uC608\uC0B0\uC9C0\uCE68\uC774 \uD544\uC694\uD569\uB2C8\uB2E4.", "\uC6D0/\uBA85\u00b7\uD68C"),
        ],
    )


def _compound_committee_result(target: _CommitteeTarget) -> CommitteeEstimate:
    member_cap = _target_member_cap(target.context)
    variables: list[EvidenceValue] = []
    if member_cap:
        variables.append(
            _value(
                "main_committee_member_cap",
                "\uBCF8\uC704\uC6D0\uD68C \uC804\uCCB4 \uC704\uC6D0 \uC0C1\uD55C",
                float(member_cap),
                "\uBA85",
                "law_text",
                "TARGET_DOCUMENT",
                f"\uC870\uBB38\uC0C1 \uBCF8\uC704\uC6D0\uD68C {member_cap}\uBA85 \uC774\uB0B4",
            )
        )
    return CommitteeEstimate(
        committee_name=target.name,
        article_refs=list(target.article_refs),
        source_quotes=list(target.texts),
        status="review_required",
        reason_codes=["COMPOUND_COMMITTEE_FORMULA_REQUIRED"],
        reason=(
            "\uBCF8\uC704\uC6D0\uD68C\uC640 \uC18C\uC704\uC6D0\uD68C\u00b7\uBD84\uACFC\uC704\uC6D0\uD68C\u00b7\uC2E4\uBB34\uD611\uC758\uCCB4\uAC00 \uD568\uAED8 \uC788\uC5B4 "
            "\uC11C\uB85C \uB2E4\uB978 \uD68C\uC758 \uD69F\uC218\uC640 \uC21C\uC99D \uC778\uC6D0\uC744 \uD558\uB098\uC758 \uC120\uB840\uAC12\uC73C\uB85C \uB369\uC5B4\uC4F8 \uC218 \uC5C6\uC2B5\uB2C8\uB2E4."
        ),
        formula=(
            "\uBCF8\uC704\uC6D0\uD68C \uC6B4\uC601\uBE44 + \uD558\uC704\uAE30\uAD6C\uBCC4(\uC21C\uC99D \uC218\uB2F9 \uC9C0\uAE09\uB300\uC0C1 \u00d7 "
            "\uC5F0\uAC04 \uD68C\uC758 \uD69F\uC218 \u00d7 1\uC778 1\uD68C\uB2F9 \uC218\uB2F9)"
        ),
        variables=variables,
        evidence_coverage=round(len(variables) / 6, 2),
        review_fields=[
            _review_field("main_paid_members", "\uBCF8\uC704\uC6D0\uD68C \uC21C\uC99D \uC9C0\uAE09\uB300\uC0C1", "\uC9C1\uC5ED\uC0C1 \uC218\uB2F9 \uC81C\uC678\uC790\uB97C \uD655\uC778\uD574\uC57C \uD569\uB2C8\uB2E4.", "\uBA85"),
            _review_field("main_annual_meetings", "\uBCF8\uC704\uC6D0\uD68C \uC5F0\uAC04 \uD68C\uC758 \uD69F\uC218", "\uBCF8\uC704\uC6D0\uD68C\uC758 \uC6B4\uC601\uACC4\uD68D\uC774 \uD544\uC694\uD569\uB2C8\uB2E4.", "\uD68C/\uB144"),
            _review_field("subbody_paid_members", "\uD558\uC704\uAE30\uAD6C \uC21C\uC99D \uC9C0\uAE09\uB300\uC0C1", "\uBCF8\uC704\uC6D0\uACFC \uC911\uBCF5\uB418\uB294 \uC704\uC6D0\uC744 \uC911\uBCF5 \uACC4\uC0C1\uD558\uC9C0 \uC54A\uC544\uC57C \uD569\uB2C8\uB2E4.", "\uBA85"),
            _review_field("subbody_annual_meetings", "\uD558\uC704\uAE30\uAD6C \uC5F0\uAC04 \uD68C\uC758 \uD69F\uC218", "\uD558\uC704\uAE30\uAD6C\uBCC4 \uC6B4\uC601\uACC4\uD68D\uC774 \uD544\uC694\uD569\uB2C8\uB2E4.", "\uD68C/\uB144"),
            _review_field("unit_price_won", "\uC801\uC6A9 \uC218\uB2F9 \uB2E8\uAC00", "\uB300\uC0C1 \uC5F0\uB3C4 \uC608\uC0B0\uC9C0\uCE68\uC774 \uD544\uC694\uD569\uB2C8\uB2E4.", "\uC6D0/\uBA85\u00b7\uD68C"),
        ],
    )


def _manual_evidence_result(
    target: _CommitteeTarget,
    *,
    years: int,
    inputs: dict[str, float | bool],
    payable: tuple[int, str] | None,
    audits: list[CandidateAudit],
) -> CommitteeEstimate:
    """Build a precise HITL contract when no precedent is transferable."""
    variables: list[EvidenceValue] = []
    review_fields: list[dict[str, str]] = []
    paid_value = inputs.get("paid_members")
    if paid_value is not None:
        paid_count = float(paid_value)
        variables.append(_value("paid_members", "신규 수당 지급대상 인원", paid_count, "명", "user_confirmed", "USER", "사용자 확정값"))
    elif payable:
        paid_count = float(payable[0])
        variables.append(_value("paid_members", "조문상 수당 지급 가능 인원", paid_count, "명", "law_text", "TARGET_DOCUMENT", payable[1]))
    else:
        paid_count = None
        variables.append(_value("paid_members", "신규 수당 지급대상 인원", None, "명", "missing", "", "조문과 선례에서 확정하지 못함", review=True))
        review_fields.append(_review_field("paid_members", "신규 수당 지급대상 인원", "조문에서 지급 대상을 확정하지 못했습니다.", "명"))

    incumbent_value = inputs.get("incumbent_paid_members")
    if incumbent_value is not None:
        incumbent = float(incumbent_value)
        variables.append(_value("incumbent_paid_members", "현행 수당 지급대상 인원", incumbent, "명", "user_confirmed", "USER", "사용자가 확인한 기존 제도 기준값"))
    elif bool(inputs.get("confirm_no_existing_baseline")):
        incumbent = 0.0
        variables.append(_value("incumbent_paid_members", "현행 수당 지급대상 인원", incumbent, "명", "user_confirmed", "USER", "사용자가 동일 기능의 기존 위원회가 없음을 확인"))
    else:
        incumbent = None
        variables.append(_value("incumbent_paid_members", "현행 수당 지급대상 인원", None, "명", "missing", "", "원문만으로 기존 행정위원회 기준값을 알 수 없음", review=True))
        review_fields.append(_review_field("incumbent_paid_members", "현행 수당 지급대상 인원", "순증 비용 계산에 필요한 기존 제도 기준값입니다. 없으면 0으로 확인합니다.", "명"))

    unit_value = inputs.get("unit_price_won")
    unit_price = float(unit_value) if unit_value is not None else None
    variables.append(_value("unit_price_won", "1인 1회당 수당", unit_price, "원/명·회", "user_confirmed" if unit_price is not None else "missing", "USER" if unit_price is not None else "", "사용자 확정값" if unit_price is not None else "직접 준용 가능한 선례 없음", review=unit_price is None))
    if unit_price is None:
        review_fields.append(_review_field("unit_price_won", "적용 수당 단가", "대상 연도의 예산지침 또는 기관 기준이 필요합니다.", "원/명·회"))

    meeting_value = inputs.get("annual_meetings")
    meetings = float(meeting_value) if meeting_value is not None else None
    variables.append(_value("annual_meetings", "연간 회의 횟수", meetings, "회/년", "user_confirmed" if meetings is not None else "missing", "USER" if meetings is not None else "", "사용자 확정값" if meetings is not None else "직접 준용 가능한 선례 없음", review=meetings is None))
    if meetings is None:
        review_fields.append(_review_field("annual_meetings", "연간 회의 횟수", "대상 위원회의 실제 운영계획이 필요합니다.", "회/년"))

    complete = all(value is not None for value in (paid_count, incumbent, unit_price, meetings))
    annual = None
    if complete:
        net_count = max(0.0, float(paid_count) - float(incumbent))
        annual = int(round(net_count * float(meetings) * float(unit_price) / 1000))
    coverage = sum(value.value is not None for value in variables) / len(variables)
    return CommitteeEstimate(
        committee_name=target.name,
        article_refs=list(target.article_refs),
        source_quotes=list(target.texts),
        status="computed" if complete else "review_required",
        reason_codes=[] if complete else ["MISSING_EXTERNAL_BASELINE"],
        reason=(
            "직접 선례 없이 조문값과 사용자 확인값만으로 순증 비용을 계산했습니다."
            if complete
            else "조문에서 확정한 값은 유지하고, DB에 없는 외부 기준값만 사용자에게 요청합니다."
        ),
        variables=variables,
        candidate_audits=audits,
        annual_amount_thousand=annual,
        five_year_amount_thousand=annual * years if annual is not None else None,
        evidence_coverage=round(coverage, 2),
        calibrated_confidence=1.0 if complete else round(coverage, 2),
        review_fields=review_fields,
    )


def _estimate_target(
    target: _CommitteeTarget,
    *,
    bill_no: str | None,
    bill_name: str,
    cutoff_date: str | None,
    years: int,
    user_inputs: dict[str, float | bool] | None,
    refresh: bool,
) -> CommitteeEstimate:
    inputs = user_inputs or {}
    context = target.context
    if _HEAVY_ORGANIZATION.search(_compact(_organization_scope_context(target))):
        return CommitteeEstimate(
            committee_name=target.name,
            article_refs=list(target.article_refs),
            source_quotes=list(target.texts),
            status="out_of_scope",
            reason_codes=["ORGANIZATION_HEAVY_COMMITTEE"],
            reason="상임위원·사무처·별도 직원이 있는 위원회는 회의수당 채널이 아니라 조직신설 채널 대상입니다.",
        )
    if _EVENT_DRIVEN.search(_compact(context)):
        return _event_driven_result(target)
    if _is_multi_instance(target):
        return _multi_instance_result(target)

    payable = _target_payable_member_count(target.event, context)
    if (
        (inputs.get("paid_members") is not None or payable is not None)
        and inputs.get("unit_price_won") is not None
        and inputs.get("annual_meetings") is not None
        and (
            inputs.get("incumbent_paid_members") is not None
            or bool(inputs.get("confirm_no_existing_baseline"))
        )
    ):
        # Fully grounded target/user evidence is stronger than any analogy and
        # should not acquire an irrelevant selected precedent merely because
        # retrieval is available.
        return _manual_evidence_result(
            target,
            years=years,
            inputs=inputs,
            payable=payable,
            audits=[],
        )
    if _has_compound_committee_scope(target):
        return _compound_committee_result(target)
    target_count = float(payable[0]) if payable else None
    excluded = {bill_no} if bill_no else set()
    semantic, _ = search_cost_candidates(
        target.event,
        exclude_bill_nos=excluded,
        exclude_bill_name=bill_name,
        cutoff_date=cutoff_date,
        refresh=refresh,
        k=50,
        min_similarity=0.40,
    )
    companion: list[TagCandidate] = []
    if "계획" in context:
        companion, _ = search_companion_formula_candidates(
            target.event,
            {"committee_operation", "research_plan"},
            context,
            exclude_bill_nos=excluded,
            cutoff_date=cutoff_date,
            refresh=refresh,
            k=30,
        )
    candidates = enrich_candidates(_merge_candidates(semantic, companion))
    pairs = [
        (candidate, _candidate_audit(target, candidate, target_count=target_count))
        for candidate in candidates
    ]
    pairs.sort(key=lambda pair: _candidate_key(pair[1]), reverse=True)
    selected_pair = next((pair for pair in pairs if pair[1].accepted), None)
    audits = [audit for _, audit in pairs[:12]]
    if selected_pair is None:
        return _manual_evidence_result(
            target,
            years=years,
            inputs=inputs,
            payable=payable,
            audits=audits,
        )

    unstable, instability_reason = _precedent_disagreement(pairs)
    if unstable:
        result = _manual_evidence_result(
            target,
            years=years,
            inputs=inputs,
            payable=payable,
            audits=audits,
        )
        result.reason_codes = ["PRECEDENT_DISAGREEMENT"]
        result.reason = (
            f"{instability_reason}. 임의로 한 선례를 선택하지 않고 실제 운영 기준을 요청합니다."
        )
        return result

    candidate, selected_audit = selected_pair
    options = {option.role: option for option in candidate_options(candidate)}
    cohort_paid = _cohort_paid_member_count(
        target,
        pairs,
        options["count"].canonical_value,
    )
    variables: list[EvidenceValue] = []
    review_fields: list[dict[str, str]] = []

    if inputs.get("paid_members") is not None:
        paid_count = float(inputs["paid_members"])
        variables.append(_value("paid_members", "신규 수당 지급대상 인원", paid_count, "명", "user_confirmed", "USER", "사용자 확정값"))
    elif payable:
        paid_count = float(payable[0])
        variables.append(_value("paid_members", "조문상 수당 지급 가능 인원", paid_count, "명", "law_text", "TARGET_DOCUMENT", payable[1]))
    elif cohort_paid is not None:
        paid_count, cohort_source_id, cohort_source_text = cohort_paid
        variables.append(
            _value(
                "paid_members",
                "구조 선례군 기반 수당 지급대상 인원",
                paid_count,
                "명",
                "precedent",
                cohort_source_id,
                cohort_source_text,
                review=True,
            )
        )
        review_fields.append(
            _review_field(
                "paid_members",
                "실제 수당 지급대상 인원",
                "조문상 당연직·공무원 위원을 제외하기 위해 선례군 비율을 적용한 초안입니다.",
                "명",
            )
        )
    else:
        count_option = options["count"]
        paid_count = count_option.canonical_value
        variables.append(_value("paid_members", "선례 수당 지급대상 인원", paid_count, "명", "precedent", count_option.item_id, count_option.source_text, review=True))
        review_fields.append(_review_field("paid_members", "실제 수당 지급대상 인원", "조문에서 확정되지 않아 선례값은 초안일 뿐입니다.", "명"))

    if inputs.get("unit_price_won") is not None:
        unit_price = float(inputs["unit_price_won"])
        variables.append(_value("unit_price_won", "1인 1회당 수당", unit_price, "원/명·회", "user_confirmed", "USER", "사용자 확정값"))
    else:
        option = options["unit_price"]
        unit_price = option.canonical_value
        variables.append(_value("unit_price_won", "1인 1회당 수당", unit_price, "원/명·회", "precedent", option.item_id, option.source_text, review=True))
        review_fields.append(_review_field("unit_price_won", "적용 수당 단가", "대상 연도의 예산지침 또는 기관 기준 확인이 필요합니다.", "원/명·회"))

    if inputs.get("annual_meetings") is not None:
        meetings = float(inputs["annual_meetings"])
        variables.append(_value("annual_meetings", "연간 회의 횟수", meetings, "회/년", "user_confirmed", "USER", "사용자 확정값"))
    else:
        option = options["frequency"]
        meetings = option.canonical_value
        variables.append(_value("annual_meetings", "연간 회의 횟수", meetings, "회/년", "precedent", option.item_id, option.source_text, review=True))
        review_fields.append(_review_field("annual_meetings", "예상 연간 회의 횟수", "대상 위원회의 실제 운영계획 확인이 필요합니다.", "회/년"))

    baseline_confirmed = False
    if inputs.get("incumbent_paid_members") is not None:
        incumbent = float(inputs["incumbent_paid_members"])
        baseline_confirmed = True
        variables.append(_value("incumbent_paid_members", "현행 수당 지급대상 인원", incumbent, "명", "user_confirmed", "USER", "사용자가 확인한 기존 제도 기준값"))
    elif bool(inputs.get("confirm_no_existing_baseline")):
        incumbent = 0.0
        baseline_confirmed = True
        variables.append(_value("incumbent_paid_members", "현행 수당 지급대상 인원", incumbent, "명", "user_confirmed", "USER", "사용자가 동일 기능의 기존 위원회가 없음을 확인"))
    else:
        incumbent = 0.0
        variables.append(_value("incumbent_paid_members", "현행 수당 지급대상 인원", None, "명", "missing", "", "법안 원문만으로 기존 행정위원회 존재 여부를 확정할 수 없음", review=True))
        review_fields.insert(0, _review_field("incumbent_paid_members", "현행 수당 지급대상 인원", "비용은 총액이 아니라 순증분이므로 기존 동일 기능 위원회 확인이 필수입니다. 없으면 0으로 확인합니다.", "명"))

    gross_annual = int(round(paid_count * meetings * unit_price / 1000))
    net_count = max(0.0, paid_count - incumbent)
    annual = int(round(net_count * meetings * unit_price / 1000)) if baseline_confirmed else None
    all_non_baseline_confirmed = all(
        value.provenance in {"law_text", "user_confirmed"}
        for value in variables
        if value.key != "incumbent_paid_members"
    )
    status = "computed" if baseline_confirmed and all_non_baseline_confirmed else "review_required"
    coverage = sum(value.value is not None for value in variables) / len(variables)
    provenance_score = sum(
        1.0 if value.provenance in {"law_text", "user_confirmed"}
        else 0.6 if value.provenance == "precedent"
        else 0.0
        for value in variables
    ) / len(variables)
    candidate_score = selected_audit.total_score / 33
    confidence = round(min(coverage, provenance_score, candidate_score), 2)
    reason_codes = [] if status == "computed" else ["HUMAN_REVIEW_REQUIRED"]
    reason = (
        "모든 계산 변수가 조문 또는 사용자 확인값으로 확정되어 순증 비용을 계산했습니다."
        if status == "computed"
        else "검증 가능한 초안은 만들었지만 선례값 또는 기존 제도 기준값이 남아 있어 자동 확정하지 않습니다."
    )
    return CommitteeEstimate(
        committee_name=target.name,
        article_refs=list(target.article_refs),
        source_quotes=list(target.texts),
        status=status,
        reason_codes=reason_codes,
        reason=reason,
        variables=variables,
        selected_candidate=selected_audit,
        candidate_audits=audits,
        annual_amount_thousand=annual,
        five_year_amount_thousand=annual * years if annual is not None else None,
        gross_draft_annual_thousand=gross_annual,
        evidence_coverage=round(coverage, 2),
        calibrated_confidence=confidence,
        review_fields=review_fields,
    )


def estimate_committee_from_pdf(
    pdf_bytes: bytes,
    *,
    filename: str = "uploaded.pdf",
    years: int = 5,
    user_inputs: dict[str, dict[str, float | bool]] | None = None,
    refresh: bool = False,
) -> CommitteeEstimateResult:
    """Estimate meeting allowances with explicit evidence sufficiency gates.

    ``user_inputs`` is keyed by committee name (or a substring of it).  Runtime
    selection never reads an answer PDF or an official target total.
    """
    raw_text = _extract_pdf_text_from_bytes(pdf_bytes)
    if not raw_text:
        raise ValueError("PDF에서 텍스트를 추출하지 못했습니다.")
    legal_text = strip_appendices(raw_text)
    articles, _ = split_articles(legal_text)
    if not articles:
        raise ValueError("법안 조문을 분리하지 못했습니다.")
    bill_no = _extract_current_bill_no(raw_text, filename)
    bill_name = _extract_bill_name(legal_text, filename)
    cutoff_date, stored_name = target_metadata(bill_no)
    targets = _find_targets(articles)
    estimates: list[CommitteeEstimate] = []
    for target in targets:
        matched_inputs = None
        for selector, values in (user_inputs or {}).items():
            if selector == target.name or selector in target.name:
                matched_inputs = values
                break
        estimates.append(
            _estimate_target(
                target,
                bill_no=bill_no,
                bill_name=stored_name or bill_name,
                cutoff_date=cutoff_date,
                years=years,
                user_inputs=matched_inputs,
                refresh=refresh,
            )
        )
    warnings = [
        "임베딩은 후보 recall에 사용하고, 최종 선택은 산식 완결성·기관 수준·위원회 프로필·조문 규모·출처 품질을 구조적으로 재랭킹합니다.",
        "조문상 공무원·당연직이 있는데 단일 선례가 사실상 전원을 유급으로 보는 경우에는 독립 선례군의 유급위원 비율을 적용합니다.",
        "calibrated_confidence는 LLM 자기평가가 아니라 근거 완결성·출처 등급·구조 점수의 최솟값입니다.",
        "공식 정답 PDF와 목표 총액은 런타임 검색·선택·계산에 사용하지 않습니다.",
    ]
    if not targets:
        warnings.append("설치 조문이 확인되는 위원회 사건이 없습니다.")
    return CommitteeEstimateResult(
        bill_no=bill_no,
        bill_name=bill_name,
        engine_version=ENGINE_VERSION,
        years=years,
        estimates=estimates,
        warnings=warnings,
    )

"""[Phase 2] 항목별 순수 변수 추출 (LLM Parser).

역할 분리 원칙: LLM은 절대 계산·단가추론을 하지 않는다. 조문에 명시된
물리적 숫자만 파싱해 각 카테고리의 dataclass로 반환한다. 명시 안 된 값은
None으로 남긴다(0으로 단정하지 않음 — committee_rule_engine.py의 실측 교훈).
"""
from __future__ import annotations

from dataclasses import dataclass

from .article_extraction_engine import _call_upstage_json
from .committee_size_extract import (
    extract_committee_size,
    extract_designated_ex_officio_chair,
    extract_meeting_frequency,
    extract_standing_member_count,
)

TAG_PARSERS_VERSION = "tag-parsers-v5"


@dataclass
class CommitteeVars:
    name: str
    total_members: int
    ex_officio: int | None  # None = 조문에 숫자로 명시 안 됨
    meetings: int | None  # None = 조문에 명시 안 됨
    # 유형1(중앙행정기관형)의 소속 공무원, 유형2(사무국형)의 사무국 직원 수/직급.
    # 조문에 없으면 None(추측 금지) — 유형3(단순자문형)에서는 아예 안 쓰인다.
    staff_headcount: int | None = None
    staff_grade: str | None = None
    # 실측(2124118): "상임위원 N명"은 회의수당이 아니라 상근 인건비 대상이라
    # total_members/ex_officio와 별도로 뽑는다. 없으면 상임위원 없는 게 기본값.
    standing_member_headcount: int | None = None
    standing_member_grade: str | None = None
    # 규칙(정규식)이 최종값을 결정하는 필드(total_members/meetings/
    # standing_member_headcount)에서, LLM 1차 추출값과 다르거나 규칙이 못
    # 찾은 걸 LLM은 찾은 경우를 순수 진단용으로 기록. 계산에는 전혀 안 쓰이고,
    # "규칙이 조문에 있는 걸 놓치고 있진 않은지" 사람이 확인하는 감시용이다.
    extraction_mismatches: dict | None = None
    # LLM은 일회성/반복성을 최종 판단하지 않고, 원문에 있는 시간 근거만
    # 제안한다. committee_temporality.py가 인용문을 원문과 다시 대조한 뒤
    # 고신뢰 사건 일정만 계산에 반영한다.
    temporal_mode: str | None = None
    finite_event_count: int | None = None
    temporal_evidence_quotes: list[str] | None = None
    # 정원 구간과 집단별 구성을 소실시키지 않는 구조화 필드.
    # member_groups는 계산값이 아니라 라벨·명시인원·동수관계·원문인용만
    # 보존하며, committee_membership.py가 인용 검증 후 수당 대상을 연산한다.
    total_members_min: int | None = None
    total_members_max: int | None = None
    member_groups: list[dict] | None = None


@dataclass
class PersonnelVars:
    target_grade: str  # 예: "5급", "고위공무원"
    headcount: int


@dataclass
class TransferPaymentVars:
    target_demographic: str
    subsidy_amount_per_person: int | None  # 조문에 금액이 명시된 경우만
    payment_cycle: str  # "월" | "년"


@dataclass
class CapitalExpenditureVars:
    system_type: str
    scale: str  # "대" | "중" | "소"


_COMMITTEE_PROMPT = """법안 조문에서 위원회 구성 정보만 추출하는 파서다.
절대 계산하지 마라. 조문에 숫자로 명시된 것만 뽑는다.

- total_members: 위원 정수 최대값
- ex_officio: 당연직 위원 "수"가 숫자로 명시된 경우만 그 값. "대통령령으로 정하는
  관계 중앙행정기관의 장"처럼 숫자 없이 위임돼 있으면 null(0으로 추측 금지).
- meetings: 연간 회의 횟수. "분기별 1회"=4, "반기별"=2, "월별"=12로 환산. 없으면 null.
- staff_headcount: 위원회에 소속되거나(중앙행정기관형) 사무국/사무처에 소속된
  "공무원" 인원 수가 숫자로 명시된 경우만. 없으면 null.
- staff_grade: 위 직원의 대표 직급(예: "5급"). 명시 없으면 null.
- standing_member_headcount: "상임위원" 수가 숫자로 명시된 경우만. 상임위원은
  회의수당이 아니라 상근 인건비 대상이라 total_members/ex_officio와 별개다.
  명시 없으면 null(0으로 추측 금지).
- standing_member_grade: 상임위원의 대표 직급/직위. 명시 없으면 null.
- temporal_mode: 다음 중 하나.
  · "annual": 조문에 매년·분기별 등 정기 반복이 명시된 경우만
  · "event_driven": 특정 선고·임명·최초 구성 등 서로 다른 사건에 연결된
    한정된 횟수가 원문에서 직접 확인되는 경우만
  · 나머지는 "unknown". 반복 표현이 없다는 이유만으로 event_driven으로 판정하지 마라.
- finite_event_count: event_driven일 때 서로 다른 사건성 일정의 수. 불명확하면 null.
- temporal_evidence_quotes: 각 사건의 시작 조건과 기한이 함께 드러나는
  원문 직접 인용. event_driven이면 이벤트당 정확히 하나씩. 요약·재작성 금지.
- member_groups: 조문에 집단별 구성이 명시된 경우만 배열로 추출.
  · label: 조문 표현(예: "법원 내부 구성원")
  · count: 명시 인원, 동수만 명시되면 null
  · allocation: 명시 인원은 "fixed", "각 호의 위원을 동수로"는 "equal_share"
  · kind: 조문상 신분만 private_external | government_official |
    internal_public_organization | unknown 중 하나. 애매하면 반드시 unknown.
  · evidence_quote: 집단·인원/동수·신분을 확인할 수 있는 원문 직접 인용.

JSON만: {{"name": "...", "total_members": 0, "ex_officio": 0 또는 null, "meetings": 0 또는 null, "staff_headcount": 0 또는 null, "staff_grade": "..." 또는 null, "standing_member_headcount": 0 또는 null, "standing_member_grade": "..." 또는 null, "temporal_mode": "annual 또는 event_driven 또는 unknown", "finite_event_count": 0 또는 null, "temporal_evidence_quotes": ["원문..."], "member_groups": [{{"label":"...", "count":5 또는 null, "allocation":"fixed 또는 equal_share", "kind":"private_external 또는 government_official 또는 internal_public_organization 또는 unknown", "evidence_quote":"원문..."}}]}}

[조문]
{article_text}
"""

_PERSONNEL_PROMPT = """법안 조문에서 공무원 증원 정보만 추출하는 파서다.
절대 계산하지 마라. 조문에 숫자로 명시된 것만 뽑는다.

- target_grade: 직급(예: "5급", "고위공무원단"). 명시 없으면 빈 문자열.
- headcount: 증원 인원 수.

JSON만: {{"target_grade": "...", "headcount": 0}}

[조문]
{article_text}
"""

_TRANSFER_PAYMENT_PROMPT = """법안 조문에서 보조금·수당 지급 정보만 추출하는 파서다.
절대 계산하지 마라. 조문에 숫자로 명시된 것만 뽑는다.

- target_demographic: 지급대상(예: "만 18세 미만 아동"). 조문 표현 그대로.
- subsidy_amount_per_person: 1인당 지급액(원)이 숫자로 명시된 경우만. 없으면 null.
- payment_cycle: "월" 또는 "년". 명시 안 되면 "년".

JSON만: {{"target_demographic": "...", "subsidy_amount_per_person": 0 또는 null, "payment_cycle": "월 또는 년"}}

[조문]
{article_text}
"""

_CAPITAL_EXPENDITURE_PROMPT = """법안 조문에서 정보시스템/건축 구축 정보만 추출하는 파서다.
절대 계산하지 마라.

- system_type: 구축 대상(예: "통합관리시스템", "청사"). 조문 표현 그대로.
- scale: 조문에서 유추 가능한 규모를 "대"/"중"/"소" 중 하나로. 판단 근거가 전혀
  없으면 "중"(기본값).

JSON만: {{"system_type": "...", "scale": "대 또는 중 또는 소"}}

[조문]
{article_text}
"""


def extract_committee_vars(article_text: str) -> CommitteeVars:
    """total_members/meetings는 정규식(committee_size_extract.py)을 우선 쓴다 —
    조문에 명시돼 있으면 결정적으로 뽑히는 값이라 LLM 비결정성(실측: 같은 조문도
    호출마다 값이 바뀜)을 피할 수 있다. LLM은 이름과, 애초에 조문에 숫자가 없어
    판단이 필요한 ex_officio(당연직 수)에만 쓴다.
    """
    parsed = _call_upstage_json(_COMMITTEE_PROMPT.format(article_text=article_text))
    if not isinstance(parsed, dict):
        raise ValueError(f"추출 결과가 JSON 객체가 아닙니다: {parsed!r}")

    name = str(parsed.get("name") or "").strip()
    mismatches: dict = {}

    total_members = None
    size = extract_committee_size(article_text)
    llm_total = parsed.get("total_members")
    llm_total = int(llm_total) if llm_total else None
    total_members_min = None
    total_members_max = None
    if size and size["kind"] == "range":
        total_members_min = int(size["low"])
        total_members_max = int(size["high"])
        # 기존 산식과의 호환을 위해 점값은 상한으로 유지하되,
        # 구조 제약 계산은 아래 min/max를 쓴다.
        total_members = total_members_max
    elif size and size["kind"] in ("exact", "cap", "min"):
        total_members = size["value"]
        if size["kind"] == "exact":
            total_members_min = total_members_max = total_members
        elif size["kind"] == "cap":
            total_members_max = total_members
        else:
            total_members_min = total_members
        if llm_total is not None and llm_total != total_members:
            mismatches["total_members"] = {"rule": total_members, "llm": llm_total, "kind": "value_mismatch"}
    if total_members is None:
        if llm_total is not None:
            mismatches["total_members"] = {"rule": None, "llm": llm_total, "kind": "rule_missed"}
        total_members = llm_total or 0

    meetings = extract_meeting_frequency(article_text)
    llm_meetings_raw = parsed.get("meetings")
    llm_meetings = int(llm_meetings_raw) if llm_meetings_raw else None
    if meetings is not None:
        if llm_meetings is not None and llm_meetings != meetings:
            mismatches["meetings"] = {"rule": meetings, "llm": llm_meetings, "kind": "value_mismatch"}
    else:
        if llm_meetings is not None:
            mismatches["meetings"] = {"rule": None, "llm": llm_meetings, "kind": "rule_missed"}
        meetings = llm_meetings

    designated_chair = extract_designated_ex_officio_chair(article_text)
    llm_ex_officio_raw = parsed.get("ex_officio")
    llm_ex_officio = int(llm_ex_officio_raw) if llm_ex_officio_raw is not None else None
    if designated_chair is not None:
        ex_officio = designated_chair
        if llm_ex_officio is not None and llm_ex_officio != designated_chair:
            mismatches["ex_officio"] = {"rule": designated_chair, "llm": llm_ex_officio, "kind": "value_mismatch"}
    else:
        ex_officio = llm_ex_officio
    staff_headcount = parsed.get("staff_headcount")
    staff_grade = parsed.get("staff_grade")

    standing_member_headcount = extract_standing_member_count(article_text)
    standing_member_grade = None
    llm_standing_raw = parsed.get("standing_member_headcount")
    llm_standing = int(llm_standing_raw) if llm_standing_raw else None
    if standing_member_headcount is not None:
        if llm_standing is not None and llm_standing != standing_member_headcount:
            mismatches["standing_member_headcount"] = {
                "rule": standing_member_headcount, "llm": llm_standing, "kind": "value_mismatch",
            }
    else:
        if llm_standing is not None:
            mismatches["standing_member_headcount"] = {"rule": None, "llm": llm_standing, "kind": "rule_missed"}
        standing_member_headcount = llm_standing
        standing_grade_raw = parsed.get("standing_member_grade")
        standing_member_grade = str(standing_grade_raw).strip() if standing_grade_raw else None

    return CommitteeVars(
        name=name,
        total_members=total_members,
        ex_officio=int(ex_officio) if ex_officio is not None else None,
        meetings=meetings,
        staff_headcount=int(staff_headcount) if staff_headcount is not None else None,
        staff_grade=str(staff_grade).strip() if staff_grade else None,
        standing_member_headcount=standing_member_headcount,
        standing_member_grade=standing_member_grade,
        extraction_mismatches=mismatches or None,
        temporal_mode=str(parsed.get("temporal_mode") or "unknown").strip(),
        finite_event_count=(
            int(parsed["finite_event_count"])
            if parsed.get("finite_event_count") is not None
            else None
        ),
        temporal_evidence_quotes=[
            str(value).strip()
            for value in (parsed.get("temporal_evidence_quotes") or [])
            if str(value).strip()
        ] or None,
        total_members_min=total_members_min,
        total_members_max=total_members_max,
        member_groups=(
            [dict(value) for value in parsed.get("member_groups", []) if isinstance(value, dict)]
            if isinstance(parsed.get("member_groups"), list)
            else None
        ) or None,
    )


def extract_personnel_vars(article_text: str) -> PersonnelVars:
    parsed = _call_upstage_json(_PERSONNEL_PROMPT.format(article_text=article_text))
    if not isinstance(parsed, dict):
        raise ValueError(f"추출 결과가 JSON 객체가 아닙니다: {parsed!r}")
    return PersonnelVars(
        target_grade=str(parsed.get("target_grade") or "").strip(),
        headcount=int(parsed.get("headcount") or 0),
    )


def extract_transfer_payment_vars(article_text: str) -> TransferPaymentVars:
    parsed = _call_upstage_json(_TRANSFER_PAYMENT_PROMPT.format(article_text=article_text))
    if not isinstance(parsed, dict):
        raise ValueError(f"추출 결과가 JSON 객체가 아닙니다: {parsed!r}")
    amount = parsed.get("subsidy_amount_per_person")
    return TransferPaymentVars(
        target_demographic=str(parsed.get("target_demographic") or "").strip(),
        subsidy_amount_per_person=int(amount) if amount is not None else None,
        payment_cycle=str(parsed.get("payment_cycle") or "년").strip(),
    )


def extract_capital_expenditure_vars(article_text: str) -> CapitalExpenditureVars:
    parsed = _call_upstage_json(_CAPITAL_EXPENDITURE_PROMPT.format(article_text=article_text))
    if not isinstance(parsed, dict):
        raise ValueError(f"추출 결과가 JSON 객체가 아닙니다: {parsed!r}")
    return CapitalExpenditureVars(
        system_type=str(parsed.get("system_type") or "").strip(),
        scale=str(parsed.get("scale") or "중").strip(),
    )

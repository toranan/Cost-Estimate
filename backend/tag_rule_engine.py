"""[Phase 3] DB 앵커링 및 고정 룰 연산 (TAG Rule Engine).

LLM 호출 없음. Phase 2에서 추출된 변수를 받아 고정 단가(DB 앵커)를 룩업하고
정해진 수학 산식에 대입한다. 앵커가 없으면 계산을 강행하지 않고
status="blocked_missing_db_anchor"로 정직하게 멈춘다.
"""
from __future__ import annotations

from dataclasses import asdict
import re

from .civil_servant_salary_table import AVERAGE_STANDARD_INCOME_ANNUAL_2026, annual_base_salary
from .committee_assembly_internal_gate import SUPPORT_STAFF_RATIO_FALLBACK
from .committee_replication import detect_replication
from .committee_membership import derive_paid_members
from .committee_activity_duration_gate import extract_activity_duration_months_llm
from .tag_reference_lookup import basic_expense_ratio_reference, standing_member_salary_reference
from .committee_size_extract import (
    extract_activity_duration_months,
    extract_paid_member_count,
    extract_paid_member_ratio_floor,
)
from .committee_type_gate import (
    TYPE_1_CENTRAL_AGENCY,
    TYPE_2_SECRETARIAT,
    TYPE_3_SIMPLE_ADVISORY,
    TYPE_4_ASSEMBLY_INTERNAL,
)
from .estimator_v2.grade_salary import grade_salary_table_thousand
from .tag_parsers import (
    CapitalExpenditureVars,
    CommitteeVars,
    PersonnelVars,
    TransferPaymentVars,
)

TAG_RULE_ENGINE_VERSION = "tag-rule-engine-v4"

# ── 산식 1: 위원회 및 협의회 ──────────────────────────────────────────────
# 참조 DB: 기획재정부 『예산 및 기금운용계획 집행지침』(2024.9 NABO 「법안비용추계
# 이해와 실제」 인용문으로 확인: 1일당 15만원+2시간이상 5만원=상한 20만원, 실무는
# 인쇄비·경비 포함해 조금 더 얹는 게 관행). 법제처 해명자료(2024.9.29)로도
# "회의참석+안건검토 합산 1인당 1회 30만원 한도"가 재확인됨.
#
# 단가 결정 방법론(중요): 한때 DB 중앙값 30만원을 썼는데, 실측 재검증 결과
# 이 분포(n=33, 15만~60만)는 "하나의 참값 주변 노이즈"가 아니라 **서로 다른
# 규칙이 적용된 별개 모집단들의 혼합**이었다 — 혼합분포의 중앙값(30만)은 어느
# 모집단에도 속하지 않는 유령값이라, 라이브 검증 4건에서 일관되게 +20%씩
# 어긋났다. 그래서 중앙값 대신 **상황별 조건부 단가표**를 쓴다(source_text
# 분류 실측 + 오늘 라이브 검증 4건의 실제 정답 단가가 전부 20~25만원):
#  - 기본(신규 일반 자문위): 25만원 — 최빈값(n=8), 순수 지침 인용 사례 문구
#    그대로("세부지침을 참고하여 25만원"), 라이브 4건 정답 전부 이 대역.
#    25만원 적용 시 2216065·2214559 오차 0% 확인.
#  - 안건검토비 포함형: 35만원 — 실측 분해 명시("회의참석수당 20만원+안건검토비 15만원")
#  - 사법부(대법원 소속): 20만원 — 대법원 자체 지침(「각종 위원회 등의 참석수당
#    등 지급에 관한 지침」). 단 대법관후보추천위류는 35만원(선례).
#  - 고위급 선례준용형(금융위·증선위·중앙행정심판위 등 50만원대): 상수로 두지
#    않는다 — "거의 동일한 기존 기구의 실제 운영값을 통째로 준용"하는 축이라
#    유사도 기반 선례탐색(RAG)이 필요한 영역(2217718 실측에서 확인).
#
# (참고 폐기 기록: "유형2 사무국형=50만원" 세분화는 잘못된 방법(item_name
# 단어매칭)으로 나온 통계였음이 확인돼 폐기 — 유형 구분과 단가는 무관.)
UNIT_COST_WON = 250_000  # 기본값(신규 일반 자문위)
UNIT_COST_WITH_AGENDA_REVIEW_WON = 350_000  # 안건검토비 포함 신호가 있을 때
UNIT_COST_JUDICIARY_WON = 200_000  # 대법원 소속(대법원 지침)

_AGENDA_REVIEW_RE = re.compile(r"안건\s*검토")
_JUDICIARY_RE = re.compile(r"대법원|법원행정처")
# 실측(2213116, 전담재판부후보추천위원회): 코멘트에는 "대법관후보추천위류는
# 35만원(선례)"라고만 적혀있고 실제 분기 로직이 없어 20만원(사법부 기본값)이
# 잘못 적용되고 있었다 — 라이브 검증으로 실제 버그였음을 확인, n=2(코멘트가
# 인용한 대법관후보추천위 선례 + 이번 실측)로 승격해 분기 추가. NABO 자신도
# "대법관후보자추천위원회의 사례를 참고"라고 명시(선례준용형이지만, 최소
# "후보추천위" 라는 명칭 신호로는 예측 가능한 하위 카테고리).
_CANDIDATE_RECOMMEND_RE = re.compile(r"후보\s*추천\s*위원회|후보자\s*추천\s*위원회")


def select_attendance_fee(context_text: str = "") -> tuple[int, str]:
    """조문/맥락 텍스트에서 상황별 참석수당 단가를 기계적으로 고른다."""
    compact = re.sub(r"\s+", "", context_text or "")
    if _JUDICIARY_RE.search(compact):
        if _CANDIDATE_RECOMMEND_RE.search(compact):
            return UNIT_COST_WITH_AGENDA_REVIEW_WON, "judiciary_candidate_recommendation"
        return UNIT_COST_JUDICIARY_WON, "judiciary_guideline"
    if _AGENDA_REVIEW_RE.search(compact):
        return UNIT_COST_WITH_AGENDA_REVIEW_WON, "with_agenda_review"
    return UNIT_COST_WON, "default_guideline"

# BASIC_OPERATING_COST_RANGE_WON: 유형3 "연간 위원회 운영비" 실측(n=7, 극단치
# 1.07억원 제외, 400만~2,600만원). **미사용 상수** — 실측 재검증 결과(2216065
# 오차분석) 참석수당이 있는 위원회 31건 중 이 운영비가 "같이" 나온 사례는
# 0건이라, 회의수당에 이걸 더하는 산식 자체가 실측 근거 없는 잘못된 가정이었다
# (실제 정답 500만원인데 운영비를 더해서 최대 +588% 과대추정 났었음). 그래서
# calculate_committee는 더 이상 이 값을 쓰지 않는다 — 조문에 "운영비"가 참석
# 수당과 별도로 명시된 게 확인되는 케이스가 나오면 그때 조건부로 재도입한다.
BASIC_OPERATING_COST_RANGE_WON = (4_000_000, 26_000_000)
# 조문에 회의횟수가 명시 안 됐을 때의 폴백값 — 지금까지 근거 코멘트 없이
# 박혀있던 상수였다. 2211774(법관평가위원회, 정답 연 1회) 검증 중 의문이
# 생겨 로컬 TAG DB에서 "회의횟수"류 변수(n=36, source_text로 "1인당
# 발송횟수" 등 무관 항목 제외)를 뒤늦게 실측 — 최빈값=중앙값=4로 이 상수와
# 정확히 일치해 유지. 단, 법관평가위원회처럼 "이 위원회 고유의 실제 운영
# 이력"(예: 근무성적평정은 원래 연 1회)에서 유래한 값은 조문에도, 유사
# 타위원회 평균에도 없어 이 폴백으로 못 잡는다 — 선례준용형과 같은 계열의
# 원리적 한계(단, 다른 위원회를 빌리는 게 아니라 그 위원회 자신의 과거
# 운영실적을 빌리는 변형).
DEFAULT_MEETINGS_PER_YEAR = 4

# 위원 정수 자체가 조문에 없는 경우(예: "위원회 구성은 대통령령으로 정한다")의
# 폴백. 이건 "우리가 못 뽑은 값"이 아니라 "그 시점엔 세상에 확정된 숫자가 아직
# 없는" 구조적 공백이다(시행령이 법 통과 후에 만들어짐) — NABO도 이 경우 유사
# 선례로 판단한다고 명시(2024.9 공식문서). 그래서 우리도 같은 방식: 로컬 TAG DB에서
# 유형3(사무처 없음) "민간위원 수"(수당 지급대상 인원, 즉 paid_members 그 자체)를
# 직접 모아 실측 — n=89, Q1=7명, 중앙값=10명, Q3=15명. total_members를 재구성하지
# 않고 paid_members를 이 구간으로 바로 추정한다.
PAID_MEMBERS_FALLBACK_RANGE = (7, 15)  # Q1~Q3, 유형3 실측 n=89
PAID_MEMBERS_FALLBACK_MEDIAN = 10

# 총원은 조문에 있는데 당연직(ex_officio) 수만 없는 경우의 폴백.
# 실측(2126334): "당연직은 많아야 1명"이라 가정하고 [총원-1, 총원]으로 좁게
# 잡던 이전 방식은 검증 없이 박혀있던 가정이었다 — 실제로는 총원 25명 중
# 당연직이 14명(56%)이나 되는 경우가 확인되어 완전히 틀렸다(정답 1,100만원인데
# [2,400만~2,500만원]으로 나와 정답을 아예 못 잡음). 로컬 TAG DB에서
# "총원 N명 중 유급(민간/위촉)위원 M명"이 직접 쌍으로 나오는 8건을 실측
# 채굴한 결과 비율이 33%~80%로 나타났다(n=8, 폭이 넓지만 최소한 실측 기반
# — 2126334의 실제 비율 44%도 이 구간에 포함됨).
PAID_MEMBER_RATIO_RANGE = (0.33, 0.80)  # n=8, 로컬 TAG DB 실측

# 유형1(중앙행정기관형) 전용: 위원장이 정무직(장관/차관급)인 게 이 유형의 정의적
# 특징이라 조문 판단 없이 항상 적용한다. 출처: 로컬 TAG DB 실측
# "2025년 기준 차관급 정무직공무원 연봉" 145,380,000원 — 장관급 등 다른 급은
# 미확보라 이 하나로만 근사한다(과소추정 가능성 있음, 장관급이면 이보다 높음).
# 2026년 값: 정확한 공식 수치·출처(예: 공무원보수규정 별표)를 웹서치로 못 박지
# 못해서, 2025값에 2026년 공무원 보수 인상률(3.5%, 실측 확인됨)을 곱한
# 추정치를 쓴다 — "실측"이 아니라 "추정(escalated)"임을 분명히 해둔다.
POLITICAL_APPOINTEE_SALARY_WON_2025 = 145_380_000  # 차관급, 실측
POLITICAL_APPOINTEE_SALARY_WON = int(round(POLITICAL_APPOINTEE_SALARY_WON_2025 * 1.035))  # 2026 추정

# 유형1(청사 건축비) — 실측/추정을 명확히 분리한다.
# 1인당 연면적 20㎡: 합리적 추정(Heuristic) — 관공서 전용률(통상 50%)과 대회의실 등
# 부대시설을 감안한 역산 추론이며, 행안부·조달청의 "1인당 20㎡" 단일 명시 문구를
# 찾은 건 아니다. 과대/과소 양방향 오차가 있을 수 있다.
# ㎡당 건축공사비 2,190,000원: 실측 확인됨 — 조달청 공공건축물 공사비 분석
# "평당 약 720만원"(2024/2025년 수준, 웹서치 교차확인) ÷ 3.3 = ㎡당 약 219만원.
AREA_PER_PERSON_BUILD_SQM = 20  # 추정(Heuristic)
CONSTRUCTION_COST_PER_SQM_WON = 2_190_000  # 실측

# 유형2(사무실 임차료) — 마찬가지로 분리.
# 1인당 사무면적 15㎡: 부분 추정 — 행안부 정부청사관리규정상 "1인당 순수사무면적
# 7㎡"는 실측 확인됨(웹서치), 복도·회의실 등 공용면적을 더해 15㎡로 잡는 부분은 추정.
# 월 임대료 33,000원/㎡: 실측 확인됨 — 한국부동산원 "2024년 서울 오피스 평당
# 109,000원"(웹서치) ÷ 3.3 = ㎡당 약 33,030원, 사용자 제시값과 거의 일치.
AREA_PER_PERSON_RENT_SQM = 15  # 부분 추정(7㎡는 실측, 나머지는 추정)
MONTHLY_RENT_PER_SQM_WON = 33_000  # 실측


def _meeting_cost(
    v: CommitteeVars, meetings: int, *, unit_cost_won: int = UNIT_COST_WON, article_text: str = ""
) -> dict:
    """공통 산식: 회의 수당. 당연직 미상이면 구간으로 반환.

    실측(2216065): "위촉하는 사람 5명"처럼 유급위원 수가 조문에 직접 명시되는
    경우가 있다 — ex_officio(당연직)를 못 뽑아도(LLM 판단 필요/비결정적) 이
    값은 정규식으로 확정되므로 구간 폴백보다 우선한다.
    """
    grouped = derive_paid_members(
        article_text,
        member_groups=v.member_groups,
        total_members_min=v.total_members_min,
        total_members_max=v.total_members_max,
    )
    if grouped is not None:
        if grouped.exact:
            return {
                "certain": True,
                "unit_cost_won": unit_cost_won,
                "paid_members": grouped.low,
                "paid_members_source": grouped.source,
                "meeting_cost_won": grouped.low * unit_cost_won * meetings,
                "reason": grouped.reason,
                "member_group_evidence_quotes": list(grouped.evidence_quotes),
            }
        return {
            "certain": False,
            "unit_cost_won": unit_cost_won,
            "reason": grouped.reason,
            "paid_members_range": [grouped.low, grouped.high],
            "paid_members_source": grouped.source,
            "meeting_cost_won_range": [
                grouped.low * unit_cost_won * meetings,
                grouped.high * unit_cost_won * meetings,
            ],
            "member_group_evidence_quotes": list(grouped.evidence_quotes),
        }

    if v.ex_officio is None:
        explicit_paid = extract_paid_member_count(article_text)
        if explicit_paid is not None:
            return {
                "certain": True,
                "unit_cost_won": unit_cost_won,
                "paid_members": explicit_paid,
                "paid_members_source": "explicit_appointed_count",
                "meeting_cost_won": explicit_paid * unit_cost_won * meetings,
                "reason": f"조문에 명시된 '위촉 {explicit_paid}명'을 그대로 회의수당 대상 인원으로 썼습니다.",
            }
        if not v.total_members:
            lower, upper = PAID_MEMBERS_FALLBACK_RANGE
            return {
                "certain": False,
                "unit_cost_won": unit_cost_won,
                "reason": (
                    "위원 정수 자체가 조문에 없습니다(구성을 대통령령 등에 위임). "
                    f"유사 위원회 선례(n=89)의 민간위원 수 {lower}~{upper}명 범위를 "
                    "검토용으로 제시합니다."
                ),
                "paid_members_range": [lower, upper],
                "paid_members_source": "precedent_fallback_n89",
                "meeting_cost_won_range": [
                    lower * unit_cost_won * meetings,
                    upper * unit_cost_won * meetings,
                ],
            }
        lower_ratio, upper_ratio = PAID_MEMBER_RATIO_RANGE
        ratio_floor = extract_paid_member_ratio_floor(article_text)
        floor_note = ""
        if ratio_floor is not None and ratio_floor > lower_ratio:
            lower_ratio = ratio_floor
            floor_note = "조문에 '민간위원이 과반수가 되도록' 규정이 있어 하한을 50%로 확정했습니다. "
        lower = max(int(v.total_members * lower_ratio), 0)
        upper = max(int(round(v.total_members * upper_ratio)), lower)
        return {
            "certain": False,
            "unit_cost_won": unit_cost_won,
            "reason": (
                floor_note
                + "당연직/위촉직 구분과 실제 수당 지급 대상이 조문만으로는 정확히 "
                "일치하지 않아(당연직에 민간단체 임직원이 포함되거나, 위촉직이 소관사무 "
                "담당 공무원이라 무급인 경우 등) 유급위원 수를 확정할 수 없습니다. "
                "총원 대비 유급위원 비율은 로컬 TAG DB 실측(n=8, 33~80%) 구간을 적용했습니다."
            ),
            "paid_members_range": [lower, upper],
            "paid_members_source": "ratio_fallback_n8" if ratio_floor is None else "ratio_fallback_n8_with_legal_floor",
            "meeting_cost_won_range": [
                lower * unit_cost_won * meetings,
                upper * unit_cost_won * meetings,
            ],
        }
    paid_members = v.total_members - v.ex_officio
    return {
        "certain": True,
        "unit_cost_won": unit_cost_won,
        "ex_officio": v.ex_officio,
        "paid_members": paid_members,
        "meeting_cost_won": paid_members * unit_cost_won * meetings,
    }


def _calculate_committee_impl(
    v: CommitteeVars, *, committee_type: str = TYPE_3_SIMPLE_ADVISORY, article_text: str = ""
) -> dict:
    base = {"name": v.name, "total_members": v.total_members, "committee_type": committee_type}

    # 동적 TAG 조회(2026-08-12 추가): 조문에서 소속기관("OO에 위원회를 둔다")을
    # 강한 패턴으로만 추출해, 그 기관의 실측 기본경비 비율로 정적 상수를
    # 대체한다. 소속기관을 못 찾거나 표본이 불안정하면 None이 되어 기존
    # 정적 상수(BASIC_EXPENSE_RATIO)로 그대로 폴백한다 — 실측(2124118, 법무부):
    # 대표값 9.6% = 실제 정답 9.6%.
    dynamic_ratio, ratio_ref = basic_expense_ratio_reference(article_text)
    ratio_meta = asdict(ratio_ref) if dynamic_ratio is not None else None

    if committee_type == TYPE_1_CENTRAL_AGENCY:
        if not v.staff_headcount:
            # 실측(2126685): 인원이 조문에 없다고 n=6짜리 클러스터 추정치로
            # 계산을 강행하면(당시 사무국형에서 확인) 근거 없는 확정 금액이
            # 나온다. 소속 공무원 인원은 이 유형의 비용을 지배하는 변수라
            # 대체할 방법이 없으므로, 확정 가능한 부분(정무직 위원장 연봉)만
            # 남기고 나머지는 review_required로 정직하게 멈춘다.
            return {
                **base,
                "political_appointee_salary_won": POLITICAL_APPOINTEE_SALARY_WON,
                "status": "review_required",
                "reason": (
                    "소속 공무원 인원이 조문에 없습니다. 정무직 위원장 연봉 외의 "
                    "소속 공무원 인건비·청사 건축비는 실제 인원 정보 없이는 "
                    "산정할 수 없어 계산을 보류합니다."
                ),
                "trace": (
                    f"[확정] 정무직 위원장 연봉 {POLITICAL_APPOINTEE_SALARY_WON:,}원 "
                    "(소속 공무원 인건비·건축비는 인원 미상으로 산정 보류)"
                ),
            }
        staff_cost, used_fallback = _personnel_cost_low_confidence(
            v.staff_headcount, v.staff_grade, fallback_headcount=STAFF_HEADCOUNT_FALLBACK_TYPE1,
            basic_expense_ratio=dynamic_ratio, basic_expense_ratio_meta=ratio_meta,
        )
        headcount_used = staff_cost["headcount"]
        headcount_for_building = headcount_used + 1  # +1은 정무직 위원장
        building_construction_cost = (
            headcount_for_building * AREA_PER_PERSON_BUILD_SQM * CONSTRUCTION_COST_PER_SQM_WON
        )
        annual_cost = POLITICAL_APPOINTEE_SALARY_WON + staff_cost["annual_cost_won"]
        return {
            **base,
            "political_appointee_salary_won": POLITICAL_APPOINTEE_SALARY_WON,
            "staff_cost": staff_cost,
            "annual_cost_won": annual_cost,
            "one_time_cost_won": building_construction_cost,  # 1년차에만 반영(건축비)
            "staff_headcount_fallback": (
                STAFF_HEADCOUNT_FALLBACK_META[TYPE_1_CENTRAL_AGENCY] if used_fallback else None
            ),
            "status": "calculated_low_confidence" if used_fallback else "calculated_with_rule",
            "reason": (
                ("소속 공무원 인원·직급이 조문에 없어 저신뢰 폴백(인원 61명 n=6 유사조직클러스터/"
                 "전체공무원평균)을 썼습니다 — 실제와 크게 다를 수 있습니다. " if used_fallback else "")
                + "청사 건축비는 1인당 면적(20㎡)을 실측 아닌 합리적 추정으로 적용했습니다"
                " — 실제 청사 규모에 따라 오차가 있을 수 있습니다."
            ),
            "trace": (
                f"[경상] 정무직 위원장 연봉 {POLITICAL_APPOINTEE_SALARY_WON:,}원 + "
                f"소속공무원 인건비 {staff_cost['annual_cost_won']:,}원 = {annual_cost:,}원/년, "
                f"[1회성] 청사건축비 {headcount_for_building}명 × {AREA_PER_PERSON_BUILD_SQM}㎡ × "
                f"{CONSTRUCTION_COST_PER_SQM_WON:,}원 = {building_construction_cost:,}원"
            ),
        }

    if committee_type == TYPE_4_ASSEMBLY_INTERNAL:
        # 실측(2211757, 2126636): 위원=국회의원 자신이라 참석수당 개념이
        # 없다(세비로 이미 지급됨). 실제 비용은 위원회를 지원하는 사무처
        # 소속 지원인력의 인건비 — 조문에 인원 명시가 없으면(거의 항상
        # 그렇다) 위원정수 대비 지원인력 비율(n=2 실측, 40~50%)로 추정한다.
        reason_common = (
            "국회 자체 특별위원회입니다 — 위원(국회의원)은 세비로 이미 지급되어 "
            "참석수당 개념이 없고, 실제 비용은 위원회를 지원하는 사무처 소속 "
            "지원인력의 인건비입니다. 위원회 사업비(운영경비) 등 다른 비용요소는 "
            "미반영입니다."
        )
        if v.staff_headcount:
            staff_cost, used_fallback = _personnel_cost_low_confidence(
                v.staff_headcount, v.staff_grade, fallback_headcount=v.staff_headcount,
                basic_expense_ratio=dynamic_ratio, basic_expense_ratio_meta=ratio_meta,
            )
            return {
                **base,
                "staff_cost": staff_cost,
                "annual_cost_won": staff_cost["annual_cost_won"],
                "status": "calculated_low_confidence" if used_fallback else "calculated_with_rule",
                "reason": reason_common,
                "trace": (
                    f"지원인력 {staff_cost['headcount']}명 인건비 "
                    f"{staff_cost['annual_cost_won']:,}원(참석수당 미해당)"
                ),
            }
        lower_ratio, upper_ratio = SUPPORT_STAFF_RATIO_FALLBACK
        lower_headcount = max(int(round(v.total_members * lower_ratio)), 1)
        upper_headcount = max(int(round(v.total_members * upper_ratio)), lower_headcount)
        low_cost, _ = _personnel_cost_low_confidence(
            None, None, fallback_headcount=lower_headcount,
            basic_expense_ratio=dynamic_ratio, basic_expense_ratio_meta=ratio_meta,
        )
        high_cost, _ = _personnel_cost_low_confidence(
            None, None, fallback_headcount=upper_headcount,
            basic_expense_ratio=dynamic_ratio, basic_expense_ratio_meta=ratio_meta,
        )
        return {
            **base,
            "staff_headcount_range": [lower_headcount, upper_headcount],
            "annual_cost_won_range": [low_cost["annual_cost_won"], high_cost["annual_cost_won"]],
            "status": "review_required",
            "reason": (
                reason_common + f" 지원인력 규모는 위원정수 대비 비율(n=2, "
                f"{int(lower_ratio*100)}~{int(upper_ratio*100)}%)로 추정했습니다 — 표본이 "
                "작아 저신뢰입니다."
            ),
            "trace": (
                f"위원정수 {v.total_members}명 × 지원인력비율({int(lower_ratio*100)}~"
                f"{int(upper_ratio*100)}%, n=2) = {lower_headcount}~{upper_headcount}명 → "
                f"인건비 {low_cost['annual_cost_won']:,}~{high_cost['annual_cost_won']:,}원"
            ),
        }

    # v.meetings가 0이면(회의 0회짜리 위원회는 실제로 없음 — LLM이 "명시 없음"을
    # null 대신 0으로 반환한 경우와 구분 불가) None과 동일하게 취급해 관행값으로
    # 폴백한다. `is not None`만 체크하면 0이 그대로 통과해 비용이 0으로 나오는
    # 버그가 있었다(2214537 실측 확인).
    # 실측(2211774 법관평가위원회): 회의횟수가 조문에 없어 DEFAULT_MEETINGS_
    # PER_YEAR로 폴백했는데(정답은 위원회 자체 과거 운영실적 기반 연 1회),
    # paid_members가 확정치라 status가 "calculated_with_rule"(고신뢰)로 나가는
    # 거짓 확신 버그가 있었다 — 폴백 여부가 신뢰도에 전혀 반영이 안 됐다.
    # meetings_used_fallback을 따로 추적해 최종 status에 반영한다.
    meetings_used_fallback = not v.meetings
    meetings = v.meetings if v.meetings else DEFAULT_MEETINGS_PER_YEAR
    unit_cost, fee_basis = select_attendance_fee(article_text)  # 상황별 조건부 단가
    base["attendance_fee_basis"] = fee_basis
    base["meetings_source"] = "fallback_default_n36" if meetings_used_fallback else "article"
    meeting = _meeting_cost(v, meetings, unit_cost_won=unit_cost, article_text=article_text)
    base["meetings"] = meetings
    base["unit_cost_won"] = unit_cost

    # 실측(2124118): "상임위원"은 회의수당이 아니라 상근 인건비 대상 —
    # calculate_personnel을 재사용(직급 불명이면 전체공무원평균 폴백).
    # total_members/ex_officio(회의수당 축)와 완전히 별개로 더한다.
    #
    # 동적 TAG 조회(2026-08-12 추가): 직급이 조문에 없을 때 "전체 공무원
    # 평균"을 쓰면 실측(2124118) 대비 약 절반으로 축소됐다 — "상임위원"이라는
    # 역할 자체가 전체 평균보다 높은 급여대라서다. TAG DB에 등급 미상
    # 상임위원 보수 실측치가 안정적으로 좁게 수렴하면(n≥3, 최대/최소 1.5배
    # 이내) 그걸 우선 쓰고, 불안정하면 기존 전체평균 폴백으로 그대로 이어간다.
    standing_cost = None
    if v.standing_member_headcount:
        if not v.standing_member_grade:
            ref_value, ref_meta = standing_member_salary_reference()
        else:
            ref_value, ref_meta = None, None
        if ref_value is not None:
            standing_cost = _personnel_cost_from_salary(
                v.standing_member_headcount, ref_value,
                "tag_db_standing_member_salary_dynamic",
                target_grade="상임위원(등급미상, TAG DB 동적조회)",
                basic_expense_ratio=dynamic_ratio, basic_expense_ratio_meta=ratio_meta,
            )
            standing_cost["tag_reference_meta"] = asdict(ref_meta)
        else:
            standing_cost, _ = _personnel_cost_low_confidence(
                v.standing_member_headcount, v.standing_member_grade,
                fallback_headcount=v.standing_member_headcount,
                basic_expense_ratio=dynamic_ratio, basic_expense_ratio_meta=ratio_meta,
            )
        base["standing_member_cost"] = standing_cost

    if committee_type == TYPE_2_SECRETARIAT:
        result = {
            **base,
            **{k: v_ for k, v_ in meeting.items() if k != "certain"},
        }
        if meeting.get("certain"):
            meeting_cost = meeting["meeting_cost_won"]
            meeting_note = ""
        else:
            lower_meet, upper_meet = meeting["meeting_cost_won_range"]
            meeting_cost = (lower_meet + upper_meet) / 2
            meeting_note = "당연직 위원 수 미상이라 회의수당은 구간 중앙값을 썼습니다. "
        standing_member_cost_won = standing_cost["annual_cost_won"] if standing_cost else 0

        if not v.staff_headcount:
            # 실측(2126685): 사무국 인원이 조문에 없다고 n=1짜리 단일사례로
            # 계산을 강행하면(사무국 인건비만 54억원) 근거 없는 확정 금액이
            # 그럴듯하게 나온다. 회의수당·상임위원 인건비처럼 확정 가능한
            # 부분만 남기고, 사무국 인건비·임차료(둘 다 인원에 비례)는
            # review_required로 정직하게 멈춘다.
            result["status"] = "review_required"
            result["reason"] = (
                meeting_note
                + ("회의횟수가 조문에 없어 폴백값(연 4회, 로컬DB n=36 최빈값)을 썼습니다. "
                   if meetings_used_fallback else "")
                + "사무국 인원이 조문에 없어 사무국 인건비·사무실임차료는 산정을 보류했습니다 "
                  "— 실제 정원이 확인돼야 계산할 수 있습니다."
            )
            result["known_cost_won"] = int(round(meeting_cost + standing_member_cost_won))
            result["trace"] = (
                f"[확정] 회의수당 {meeting_cost:,.0f}원"
                + (f" + 상임위원 인건비 {standing_member_cost_won:,}원" if standing_cost else "")
                + " (사무국 인건비·임차료는 인원 미상으로 산정 보류)"
            )
            return result

        staff_cost, staff_used_fallback = _personnel_cost_low_confidence(
            v.staff_headcount, v.staff_grade, fallback_headcount=STAFF_HEADCOUNT_FALLBACK_TYPE2,
            basic_expense_ratio=dynamic_ratio, basic_expense_ratio_meta=ratio_meta,
        )
        result["secretariat_staff_cost"] = staff_cost
        result["staff_headcount_fallback"] = (
            STAFF_HEADCOUNT_FALLBACK_META[TYPE_2_SECRETARIAT] if staff_used_fallback else None
        )
        headcount_used = staff_cost["headcount"]
        office_rent_cost = headcount_used * AREA_PER_PERSON_RENT_SQM * MONTHLY_RENT_PER_SQM_WON * 12
        result["office_rent_cost_won"] = office_rent_cost

        result["annual_cost_won"] = int(round(
            meeting_cost + staff_cost["annual_cost_won"] + office_rent_cost + standing_member_cost_won
        ))
        used_fallback = staff_used_fallback or not meeting.get("certain") or meetings_used_fallback
        result["status"] = "calculated_low_confidence" if used_fallback else "calculated_with_rule"
        result["reason"] = (
            meeting_note
            + ("회의횟수가 조문에 없어 폴백값(연 4회, 로컬DB n=36 최빈값)을 썼습니다 — 실제는 "
               "이 위원회 고유의 운영관행(예: 연례 평가업무면 연 1회)에 따라 다를 수 있습니다. "
               if meetings_used_fallback else "")
            + ("사무국 인원·직급이 조문에 없어 저신뢰 폴백(인원 60명 n=1 실제사례/전체공무원평균)을 "
               "썼습니다. " if staff_used_fallback else "")
            + "사무실 면적은 1인당 15㎡(순수사무 7㎡는 실측, 공용면적 가산분은 합리적 추정)를 "
              "적용했습니다 — 실제 사무실 규모에 따라 오차가 있을 수 있습니다."
        )
        result["trace"] = (
            f"회의수당 {meeting_cost:,.0f}원"
            + (f" + 상임위원 인건비 {standing_member_cost_won:,}원" if standing_cost else "")
            + f" + 사무국 인건비 "
            f"{staff_cost['annual_cost_won']:,}원 + 사무실임차료 "
            f"({headcount_used}명×{AREA_PER_PERSON_RENT_SQM}㎡×{MONTHLY_RENT_PER_SQM_WON:,}원×12) "
            f"{office_rent_cost:,}원 = {result['annual_cost_won']:,}원"
        )
        return result

    # TYPE_3_SIMPLE_ADVISORY
    # 실측(2216065 오차분석): 참석수당이 있는 위원회 31건을 전수 확인한 결과
    # "위원회 운영비"가 별도로 같이 나온 건 0건 — 둘은 한 번도 겹친 적이 없다.
    # 즉 "회의수당 + 기본운영비"를 항상 더하던 기존 산식은 실측 근거가 없었던
    # 잘못된 가정이었다(2216065 실측: 정답 500만원인데 우리 계산은
    # 1,120만~3,440만원으로 최대 +588% 과대추정 — 원인 대부분이 이 운영비
    # 가산이었음). 그래서 기본값은 순수 회의수당만 계산하고, 운영비는 더하지
    # 않는다. BASIC_OPERATING_COST_RANGE_WON은 참고용으로만 남겨둔다(조문에
    # "운영비"가 별도로 명시된 게 확인되는 경우를 위한 향후 확장용, 지금은 미사용).
    if not v.total_members:
        # 위원 정수 자체가 조문에 없음(예: "구성은 대통령령으로 정한다") — 총원에서
        # 당연직을 빼는 계산 자체가 불가능하므로, paid_members를 선례 기반 구간으로
        # 바로 추정한다(위 PAID_MEMBERS_FALLBACK_RANGE 주석 참고).
        lower_paid, upper_paid = PAID_MEMBERS_FALLBACK_RANGE
        return {
            **base,
            "status": "review_required",
            "reason": (
                "위원 정수 자체가 조문에 없습니다(구성을 대통령령 등에 위임). 시행령이 "
                "확정되기 전이라 이 시점엔 실제 값이 존재하지 않을 수 있습니다 — "
                f"유사 위원회 선례(n=89, 유형3)의 민간위원 수 구간({lower_paid}~{upper_paid}명, "
                f"중앙값{PAID_MEMBERS_FALLBACK_MEDIAN}명)으로 대신 추정했습니다."
            ),
            "paid_members_range": [lower_paid, upper_paid],
            "paid_members_source": "precedent_fallback_n89",
            "annual_cost_won_range": [
                lower_paid * unit_cost * meetings,
                upper_paid * unit_cost * meetings,
            ],
        }

    if not meeting["certain"]:
        return {
            **base,
            "status": "review_required",
            "reason": meeting["reason"],
            "paid_members_range": meeting["paid_members_range"],
            "paid_members_source": meeting.get("paid_members_source"),
            "member_group_evidence_quotes": meeting.get("member_group_evidence_quotes"),
            "annual_cost_won_range": meeting["meeting_cost_won_range"],
            "trace": (
                f"수당 대상 {meeting['paid_members_range'][0]}~"
                f"{meeting['paid_members_range'][1]}명(원문 구성 제약) × "
                f"{unit_cost:,}원 × {meetings}회 = "
                f"{meeting['meeting_cost_won_range'][0]:,}~"
                f"{meeting['meeting_cost_won_range'][1]:,}원"
                if str(meeting.get("paid_members_source") or "").startswith("article_member_groups")
                else None
            ),
        }

    standing_member_cost_won = standing_cost["annual_cost_won"] if standing_cost else 0
    if str(meeting.get("paid_members_source") or "").startswith("article_member_groups"):
        paid_calc_note = f"(원문 구성집단 중 수당 대상 {meeting['paid_members']}명)"
    elif "ex_officio" in meeting:
        paid_calc_note = f"({v.total_members}명 - 당연직 {meeting['ex_officio']}명)"
    else:
        paid_calc_note = f"(조문 명시 '위촉 {meeting['paid_members']}명')"
    used_fallback = bool(standing_cost) or meetings_used_fallback
    return {
        **base,
        "ex_officio": meeting.get("ex_officio"),
        "paid_members": meeting["paid_members"],
        "paid_members_source": meeting.get("paid_members_source"),
        "member_group_evidence_quotes": meeting.get("member_group_evidence_quotes"),
        "annual_cost_won": meeting["meeting_cost_won"] + standing_member_cost_won,
        "status": "calculated_low_confidence" if used_fallback else "calculated_with_rule",
        "reason": (
            "회의횟수가 조문에 없어 폴백값(연 4회, 로컬DB n=36 최빈값)을 썼습니다 — 실제는 "
            "이 위원회 고유의 운영관행(예: 연례 평가업무면 연 1회)에 따라 다를 수 있습니다."
            if meetings_used_fallback else None
        ),
        "trace": (
            f"{paid_calc_note} × {unit_cost:,}원 × "
            f"{meetings}회 = {meeting['meeting_cost_won']:,}원(회의수당, 운영비 미가산)"
            + (f" + 상임위원 인건비 {standing_member_cost_won:,}원" if standing_cost else "")
        ),
    }


_MONEY_KEYS = (
    "annual_cost_won", "one_time_cost_won",
    "annual_cost_won_range", "meeting_cost_won_range", "paid_members_range",
)


def _scale_money(value, factor: int):
    if isinstance(value, list) and len(value) == 2 and all(isinstance(x, (int, float)) for x in value):
        return [v * factor for v in value]
    if isinstance(value, (int, float)):
        return value * factor
    return value


def calculate_committee(
    v: CommitteeVars, *, committee_type: str = TYPE_3_SIMPLE_ADVISORY, article_text: str = ""
) -> dict:
    """단일 위원회 계산 + 복제설치(전국/지역단위 반복설치) 배수 적용.

    실측(2126145 지역금융위원회): 단일 위원회 계산은 완벽(0% 오차)했지만
    "지방자치단체에 ~를 둔다"는 조문이 실제로는 17개 광역자치단체 각각에
    설치되는 구조라, 배수를 안 곱하면 실제(17개 합계) 대비 -94% 과소추정
    났다. committee_channel.py(기존 프로덕션)의 검증된 로직을 이식해
    committee_replication.py로 감지한다.
    """
    result = _calculate_committee_impl(v, committee_type=committee_type, article_text=article_text)

    # 실측(2126661): 조문에 "N년 이내에 활동을 완료" 같은 한시(限時) 조항이
    # 있으면 영구 상설이 아니다 — tag_timeseries.py가 이 필드를 보고 5개년
    # 시계열을 활동기간만큼만 채우고 이후는 0으로 끊는다(안 그러면 15개월짜리
    # 총액이 5년 매년 반복돼 +413% 과대추정 나는 걸 실측 확인). 복제설치
    # 조기 반환보다 먼저 계산해야 두 경로 모두에 반영된다.
    # 실측: "존속기간은 OO년으로 한다"류 다른 표현은 정규식이 못 잡는다 —
    # LLM+근거검증을 폴백이 아니라 매번 같이 돌려서(사용자 확인) 규칙이
    # 놓친 게 없는지 보강한다. 규칙이 값을 찾았는데 LLM과 다르면 진단용으로
    # 기록만 하고(끼워맞추기 방지, 규칙이 우선), 규칙이 못 찾았을 때만 LLM
    # 값을 그대로 채택한다.
    duration_months_rule = extract_activity_duration_months(article_text)
    duration_months_llm = extract_activity_duration_months_llm(article_text)
    if duration_months_rule is not None:
        duration_months = duration_months_rule
        if duration_months_llm is not None and duration_months_llm != duration_months_rule:
            result["activity_duration_mismatch"] = {
                "rule": duration_months_rule, "llm": duration_months_llm,
            }
    else:
        duration_months = duration_months_llm
    if duration_months is not None:
        result["activity_duration_months"] = duration_months
        result["reason"] = (
            f"[한시조직, 최대 {duration_months}개월] 연장 조항을 전부 합산한 상한이라 "
            "실제보다 길게 잡혔을 수 있습니다. " + str(result.get("reason") or "")
        )

    repl = detect_replication(article_text, v.name)
    factor = repl["factor"]

    if factor == 1 and repl["confident"]:
        return result  # 복제 정황 없음 — 그대로 반환

    # 실측(2126679 돌봄근로자법): factor를 정확히 감지해놓고도(예: 시·군·구
    # 229개) confident=False라는 이유로 배수를 아예 안 곱히고 단일설치
    # 값만 내보내던 버그 발견 — 실제로는 245개 지자체 반영시 연 29.4억원인데
    # 우리는 600만~1,600만원(단일설치 기준)만 보여주고 있었다(최대 -99%대
    # 과소추정). detect_replication()이 이미 "모르면 factor=1"로 정직하게
    # 반환하므로(순수 일반형 "지방자치단체에" 케이스), factor는 항상 그대로
    # 적용하고 confident만 상태 표시에 쓴다.
    for key in _MONEY_KEYS:
        if key in result:
            result[key] = _scale_money(result[key], factor)

    result["replication"] = repl
    if not repl["confident"]:
        result["status"] = "review_required"
        if factor > 1:
            # 배수 자체(시·군·구 229/시·도 17 등)는 특정됐지만, 실제 설치
            # 개수가 조문만으로 100% 확정되진 않아 review로 남긴다 — 그래도
            # 숫자는 그 배수를 반영한 최선의 추정치다(단일값이 아님).
            result["reason"] = (
                f"[복제설치 {factor}건 추정 반영] {repl['basis']} 실제 설치 수는 "
                "다를 수 있습니다. " + str(result.get("reason") or "")
            )
        else:
            # 반복설치 구조는 있으나 몇 개 단위인지조차 조문에 없는 경우 —
            # 배수를 못 정해 단일 설치 값만 하한으로 제시한다.
            result["reason"] = (
                f"[복제설치 미확정] {repl['basis']} 아래 금액은 단일 설치 1건 기준입니다. "
                + str(result.get("reason") or "")
            )
    else:
        result["reason"] = f"[복제설치 {factor}건 반영] {repl['basis']} " + str(result.get("reason") or "")

    return result


# ── 산식 2: 공무원 조직 신설 및 인건비 ────────────────────────────────────
# 참조 DB: 인사혁신처 『공무원 보수규정(호봉표)』.
# 단가 우선순위: ① civil_servant_salary_table.py의 2026년 확정 봉급표(공식,
# 15호봉 기준 연간 기본급) → ② 없으면 grade_salary.py의 TAG 선례 중앙값으로 폴백.
# ①은 기본급만 담아 실제보다 15~20% 낮을 수 있음(civil_servant_salary_table.py
# 문서화 참고) — 그래서 폴백이 아니라 우선순위로만 쓰고 출처를 결과에 남긴다.
EMPLOYER_CONTRIBUTION_RATE = 0.13066  # 공무원 신규채용 기관부담요율(공표, grade_salary.py와 동일 출처)
BASIC_EXPENSE_RATIO = 0.116  # 총인건비 대비 기본경비 비율(TAG DB 실측 중앙값, grade_salary.py와 동일 출처)


def _personnel_cost_from_salary(
    headcount: int,
    salary_won: int,
    salary_source: str,
    *,
    target_grade: str = "",
    basic_expense_ratio: float | None = None,
    basic_expense_ratio_meta: dict | None = None,
) -> dict:
    ratio = basic_expense_ratio if basic_expense_ratio is not None else BASIC_EXPENSE_RATIO
    salary_total = headcount * salary_won
    employer_contribution = salary_total * EMPLOYER_CONTRIBUTION_RATE
    basic_expense = (salary_total + employer_contribution) * ratio
    annual_cost = salary_total + employer_contribution + basic_expense
    result = {
        "target_grade": target_grade,
        "headcount": headcount,
        "salary_won": salary_won,
        "salary_source": salary_source,
        "employer_contribution_rate": EMPLOYER_CONTRIBUTION_RATE,
        "basic_expense_ratio": ratio,
        "annual_cost_won": int(round(annual_cost)),
        "status": "calculated_with_rule",
        "trace": (
            f"{headcount}명 × {salary_won:,}원({salary_source}) = {salary_total:,.0f}원 + "
            f"기관부담금({EMPLOYER_CONTRIBUTION_RATE:.3%}) {employer_contribution:,.0f}원 + "
            f"기본경비({ratio:.1%}) {basic_expense:,.0f}원 = "
            f"{int(round(annual_cost)):,}원"
        ),
    }
    if basic_expense_ratio_meta is not None:
        result["basic_expense_ratio_meta"] = basic_expense_ratio_meta
    return result


def calculate_personnel(
    v: PersonnelVars,
    *,
    basic_expense_ratio: float | None = None,
    basic_expense_ratio_meta: dict | None = None,
) -> dict:
    salary_won = annual_base_salary(v.target_grade, hobong="15")
    salary_source = "civil_servant_salary_table_2026_15hobong"
    if salary_won is None:
        salary_thousand = grade_salary_table_thousand().get(v.target_grade)
        if salary_thousand is None:
            return {
                "target_grade": v.target_grade,
                "headcount": v.headcount,
                "status": "blocked_missing_db_anchor",
                "reason": f"'{v.target_grade}' 직급의 단가를 DB에서 찾지 못했습니다.",
            }
        salary_won = salary_thousand * 1000
        salary_source = "tag_db_median_fallback"

    return _personnel_cost_from_salary(
        v.headcount,
        salary_won,
        salary_source,
        target_grade=v.target_grade,
        basic_expense_ratio=basic_expense_ratio,
        basic_expense_ratio_meta=basic_expense_ratio_meta,
    )


# 유형1·2에서 조문에 소속공무원/사무국 "인원·직급"이 아예 없을 때의 최후 저신뢰
# 폴백. review_required로 끝내지 않고 최소한의 근거 있는 추정치를 낸다 —
# 단, 통계적 대표값이 아니라 "달리 방법이 없어 가장 가까운 참고치를 그대로 쓴
# 것"이므로 status/reason에 저신뢰임을 항상 명시한다.
# - 유형2 인원 60명: 단일 실제사례(2126661 "사무처 직원 정원"), n=1
# - 유형1 인원 61명: 조직신설/증원형 의안 일반 "총정원" 클러스터 중앙값, n=6,
#   유형1 전용 실측 아님(로컬에 유형1 사례 자체가 0건이라 대체 불가)
# - 직급 불명 시 "전체 공무원 평균 기준소득월액"(실측, 인사혁신처 고시)을 사용 —
#   특정 직급을 추측하는 것보다 방어 가능함.
STAFF_HEADCOUNT_FALLBACK_TYPE2 = 60  # 단일 실제사례, n=1
STAFF_HEADCOUNT_FALLBACK_TYPE1 = 61  # 유사 조직신설형 일반 클러스터, n=6, 유형1 전용 아님

# 나중에 NABO 원본(『법안비용추계 기준정보』 제1장 "위원회 조직 유형별 비용 단가")
# 실측치로 교체할 때 뭘 갈아끼워야 하는지 결과에 그대로 남긴다. 유형1(61,n=6)과
# 유형2(60,n=1)가 거의 같은 값이라 유형 간 변별력이 없다는 것도 알 수 있게 해둠.
STAFF_HEADCOUNT_FALLBACK_META = {
    "type1_central_agency": {
        "value": STAFF_HEADCOUNT_FALLBACK_TYPE1, "n": 6, "confidence": "low",
        "source": "유사조직(조직신설/증원형 의안 일반) 총정원 클러스터, 유형1 전용 아님",
    },
    "type2_secretariat": {
        "value": STAFF_HEADCOUNT_FALLBACK_TYPE2, "n": 1, "confidence": "low",
        "source": "단일 실제사례(2126661, 사무처 직원 정원)",
    },
}


def _personnel_cost_low_confidence(
    staff_headcount: int | None,
    staff_grade: str | None,
    *,
    fallback_headcount: int,
    basic_expense_ratio: float | None = None,
    basic_expense_ratio_meta: dict | None = None,
) -> tuple[dict, bool]:
    """(결과, used_fallback) — used_fallback=True면 인원/직급 중 하나 이상 추정치를 썼다는 뜻."""
    headcount = staff_headcount or fallback_headcount
    used_fallback = not staff_headcount or not staff_grade
    if staff_grade:
        result = calculate_personnel(
            PersonnelVars(target_grade=staff_grade, headcount=headcount),
            basic_expense_ratio=basic_expense_ratio,
            basic_expense_ratio_meta=basic_expense_ratio_meta,
        )
        if result.get("status") == "calculated_with_rule":
            return result, used_fallback
    result = _personnel_cost_from_salary(
        headcount,
        AVERAGE_STANDARD_INCOME_ANNUAL_2026,
        "average_standard_income_2026_fallback",
        target_grade=staff_grade or "전체평균",
        basic_expense_ratio=basic_expense_ratio,
        basic_expense_ratio_meta=basic_expense_ratio_meta,
    )
    return result, True


# ── 산식 3: 정보시스템 전산망 구축 ────────────────────────────────────────
# 참조 DB: 한국SW산업협회 『SW사업 대가산정 가이드』
# 실제 등급별 단가표를 아직 확보 못해 예시로 주어진 "대"규모 1건만 앵커로 둔다.
# 없는 규모는 blocked로 정직하게 멈춘다(임의 보간/추정 안 함).
_SW_BUILD_COST_WON: dict[str, int] = {
    "대": 3_000_000_000,
}
MAINTENANCE_RATE = 0.10  # 매년 구축비의 10%를 유지보수비로 책정


def calculate_capital_expenditure(v: CapitalExpenditureVars) -> dict:
    build_cost = _SW_BUILD_COST_WON.get(v.scale)
    if build_cost is None:
        return {
            "system_type": v.system_type,
            "scale": v.scale,
            "status": "blocked_missing_db_anchor",
            "reason": f"'{v.scale}' 규모의 SW 대가 단가가 DB에 없습니다.",
        }
    return {
        "system_type": v.system_type,
        "scale": v.scale,
        "build_cost_won": build_cost,
        "maintenance_rate": MAINTENANCE_RATE,
        "year_1_cost_won": build_cost,
        "year_2_to_5_cost_won": int(build_cost * MAINTENANCE_RATE),
        "status": "calculated_with_rule",
        "trace": (
            f"1년차 구축비 {build_cost:,}원, 2~5년차 매년 유지보수비 "
            f"{int(build_cost * MAINTENANCE_RATE):,}원(구축비의 {MAINTENANCE_RATE:.0%})"
        ),
    }


# ── 산식 4: 이전지출 / 보조금 지급 ────────────────────────────────────────
# 참조 DB: 통계청 KOSIS 인구/소득통계. 아직 API 연동이 없어 lookup은 항상
# None을 반환하는 스텁이다 — 실제 붙이기 전까지 이 카테고리는 항상 blocked.
def lookup_population(target_demographic: str) -> int | None:
    """KOSIS 연동 전 스텁. 실제 인구 데이터는 아직 없다."""
    return None


def calculate_transfer_payment(
    v: TransferPaymentVars, *, population_lookup=lookup_population
) -> dict:
    if v.subsidy_amount_per_person is None:
        return {
            "target_demographic": v.target_demographic,
            "status": "blocked_missing_amount",
            "reason": "1인당 지급액이 조문에 명시돼 있지 않습니다.",
        }
    population = population_lookup(v.target_demographic)
    if population is None:
        return {
            "target_demographic": v.target_demographic,
            "subsidy_amount_per_person": v.subsidy_amount_per_person,
            "status": "blocked_missing_db_anchor",
            "reason": (
                f"'{v.target_demographic}'의 모집단 수를 확보하지 못했습니다"
                "(KOSIS API 미연동)."
            ),
        }
    cycle_multiplier = 12 if v.payment_cycle == "월" else 1
    annual_cost = population * v.subsidy_amount_per_person * cycle_multiplier
    return {
        "target_demographic": v.target_demographic,
        "population": population,
        "subsidy_amount_per_person": v.subsidy_amount_per_person,
        "payment_cycle": v.payment_cycle,
        "annual_cost_won": annual_cost,
        "status": "calculated_with_rule",
        "trace": (
            f"{population:,}명 × {v.subsidy_amount_per_person:,}원 × "
            f"{cycle_multiplier}(회/년) = {annual_cost:,}원"
        ),
    }

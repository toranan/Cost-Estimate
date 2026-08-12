"""[Phase 5 보조] 최종 리포트 부록("단가 출처")에 쓸 상수 레지스트리.

값을 여기서 새로 정의하지 않고 tag_rule_engine.py/civil_servant_salary_table.py의
기존 상수를 그대로 참조한다 — 리포트 문구가 실제 계산에 쓰인 값과 드리프트되는
걸 막기 위함(정무직 연봉·기준소득월액이 옛 값인데 화면엔 최신처럼 보였던
문제의 재발 방지).

category 4종(법정 vs 실측 두 갈래로는 부족했다 — 백필하면서 발견):
- "statutory": 법정/공식 지침·고시 수치
- "empirical": 로컬 TAG DB 실측(n건 채굴)
- "estimated": 실측값을 가정된 비율로 escalate한 값(공식 수치 자체는 미확보)
- "heuristic": 실측 근거가 아예 없는 경험적 추정(역산 추론 등)
"""
from __future__ import annotations

from . import tag_rule_engine as _e
from .civil_servant_salary_table import AVERAGE_STANDARD_INCOME_MONTHLY_2026

UNIT_PRICE_REGISTRY: list[dict] = [
    {
        "key": "attendance_fee_default",
        "label": "참석수당(기본, 신규 일반 자문위)",
        "value": _e.UNIT_COST_WON,
        "unit": "원/인/회",
        "category": "empirical",
        "n": 8,
        "source": "기획재정부 예산 및 기금운용계획 집행지침 인용 사례 실측 최빈값",
        "basis_year": None,
    },
    {
        "key": "attendance_fee_agenda_review",
        "label": "참석수당(안건검토비 포함)",
        "value": _e.UNIT_COST_WITH_AGENDA_REVIEW_WON,
        "unit": "원/인/회",
        "category": "empirical",
        "n": None,
        "source": "실측 분해 명시 사례(회의참석 20만원+안건검토 15만원)",
        "basis_year": None,
    },
    {
        "key": "attendance_fee_judiciary",
        "label": "참석수당(사법부/대법원 소속)",
        "value": _e.UNIT_COST_JUDICIARY_WON,
        "unit": "원/인/회",
        "category": "statutory",
        "n": None,
        "source": "대법원 「각종 위원회 등의 참석수당 등 지급에 관한 지침」",
        "basis_year": None,
    },
    {
        "key": "employer_contribution_rate",
        "label": "공무원 신규채용 기관부담률",
        "value": f"{_e.EMPLOYER_CONTRIBUTION_RATE:.3%}",
        "unit": "",
        "category": "statutory",
        "n": None,
        "source": "공표 기관부담요율",
        "basis_year": 2026,
    },
    {
        "key": "basic_expense_ratio",
        "label": "기본경비 비율(총인건비 대비)",
        "value": f"{_e.BASIC_EXPENSE_RATIO:.1%}",
        "unit": "",
        "category": "empirical",
        "n": None,
        "source": "TAG DB 실측 중앙값",
        "basis_year": None,
    },
    {
        "key": "political_appointee_salary",
        "label": "정무직(차관급) 연봉",
        "value": _e.POLITICAL_APPOINTEE_SALARY_WON,
        "unit": "원/년",
        "category": "estimated",
        "n": None,
        "source": (
            f"2025년 실측값({_e.POLITICAL_APPOINTEE_SALARY_WON_2025:,}원)에 2026년 "
            "공무원 보수 인상률(3.5%, 실측)을 곱한 추정치 — 공식 2026년 수치 자체는 미확보"
        ),
        "basis_year": 2025,
    },
    {
        "key": "average_standard_income_monthly",
        "label": "전체 공무원 평균 기준소득월액",
        "value": AVERAGE_STANDARD_INCOME_MONTHLY_2026,
        "unit": "원/월",
        "category": "statutory",
        "n": None,
        "source": "인사혁신처 고시(2026.4.30 확정, 적용기간 2026.5.1~2027.4.30)",
        "basis_year": 2026,
    },
    {
        "key": "construction_cost_per_sqm",
        "label": "㎡당 건축공사비",
        "value": _e.CONSTRUCTION_COST_PER_SQM_WON,
        "unit": "원/㎡",
        "category": "empirical",
        "n": None,
        "source": "조달청 공공건축물 공사비 분석(평당 약 720만원, 2024/2025년 수준)",
        "basis_year": 2025,
    },
    {
        "key": "area_per_person_build",
        "label": "1인당 건축 연면적",
        "value": _e.AREA_PER_PERSON_BUILD_SQM,
        "unit": "㎡/인",
        "category": "heuristic",
        "n": None,
        "source": "관공서 전용률(50%)+부대시설 감안 역산 추론 — 실측 문구를 찾지 못함",
        "basis_year": None,
    },
    {
        "key": "monthly_rent_per_sqm",
        "label": "㎡당 월 임대료",
        "value": _e.MONTHLY_RENT_PER_SQM_WON,
        "unit": "원/㎡/월",
        "category": "empirical",
        "n": None,
        "source": "한국부동산원 2024년 서울 오피스 평당 10.9만원",
        "basis_year": 2024,
    },
    {
        "key": "area_per_person_rent",
        "label": "1인당 사무면적",
        "value": _e.AREA_PER_PERSON_RENT_SQM,
        "unit": "㎡/인",
        "category": "heuristic",
        "n": None,
        "source": "순수사무면적 7㎡(실측, 행안부 정부청사관리규정)는 실측, 공용면적 가산분은 추정",
        "basis_year": None,
    },
    {
        "key": "default_meetings_per_year",
        "label": "회의횟수 폴백",
        "value": _e.DEFAULT_MEETINGS_PER_YEAR,
        "unit": "회/년",
        "category": "empirical",
        "n": 36,
        "source": "로컬 TAG DB 실측(최빈값=중앙값)",
        "basis_year": None,
    },
    {
        "key": "paid_member_ratio_range",
        "label": "유급위원 비율(총원 대비)",
        "value": f"{int(_e.PAID_MEMBER_RATIO_RANGE[0]*100)}~{int(_e.PAID_MEMBER_RATIO_RANGE[1]*100)}%",
        "unit": "",
        "category": "empirical",
        "n": 8,
        "source": "로컬 TAG DB 실측(총원-유급위원 쌍 직접 채굴)",
        "basis_year": None,
    },
    {
        "key": "paid_members_fallback_range",
        "label": "위원 정수 자체 불명 시 민간위원 수",
        "value": f"{_e.PAID_MEMBERS_FALLBACK_RANGE[0]}~{_e.PAID_MEMBERS_FALLBACK_RANGE[1]}명",
        "unit": "",
        "category": "empirical",
        "n": 89,
        "source": "로컬 TAG DB 실측(유형3 민간위원 수)",
        "basis_year": None,
    },
    {
        "key": "staff_headcount_fallback_type1",
        "label": "유형1 소속공무원 인원 폴백",
        "value": _e.STAFF_HEADCOUNT_FALLBACK_TYPE1,
        "unit": "명",
        "category": "empirical",
        "n": 6,
        "source": "유사 조직신설형 일반 클러스터 중앙값(유형1 전용 실측 아님)",
        "basis_year": None,
    },
    {
        "key": "staff_headcount_fallback_type2",
        "label": "유형2 사무국 인원 폴백",
        "value": _e.STAFF_HEADCOUNT_FALLBACK_TYPE2,
        "unit": "명",
        "category": "empirical",
        "n": 1,
        "source": "단일 실제사례(2126661 사무처 직원 정원)",
        "basis_year": None,
    },
]

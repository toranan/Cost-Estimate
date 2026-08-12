"""직급별 인건비 분해 (격리 모듈, additive).

조직 인건비를 "1인당 평균 × 인원"(lump)이 아니라 **"직급별 단가 × 직급별 인원"**으로
계산한다. 이것은 NABO 비용추계서가 실제로 쓰는 표준 인건비 추계 방식이다
(각목명세서의 직급별 단가 × 직급별 정원 + 기관부담요율 + 기본경비 비율 + 자산취득비).

- **직급별 단가**는 특정 정답에 튜닝하지 않고 **DB(TAG 선례)의 직급별 1인당 보수
  중앙값**으로 도출한다. 여러 선례에 걸친 일반 참조표라 어느 인건비 의안에도 적용된다.
- **직급 구성**(고위공무원단·4급·5급… 별 인원)은 조문에 없는 제출자료이므로 HITL 입력이다.
- 이 분해는 **직급 구성이 주어질 때만 활성화**된다. 없으면 기존 lump 경로가 그대로
  유지되므로 기존/회귀 결과에 영향이 없다(격리).

공표 기준 상수(임금상승률·기관부담요율·기본경비 비율·자산취득 단가)는 정부 공개값이며
대상 의안의 정답이 아니다. 기관별 각목명세서가 있으면 더 정밀해진다.
"""
from __future__ import annotations

import glob
import json
import re
import statistics
from functools import lru_cache

from .types import AtomicEvent, EstimateRow, Recurrence, RowStatus

GRADE_SALARY_VERSION = "grade-decomposition-v1"

# 공표 기준 일반 상수 (정답 아님; 각목명세서로 대체 가능)
_WAGE_GROWTH = 0.03          # 최근 3년 공무원임금상승률(공표)
_EMPLOYER_RATE = 0.13066     # 공무원 신규채용 기관부담요율(공표)
_BASIC_EXPENSE_RATIO = 0.116  # 총인건비 대비 기본경비 비율(선례 관행)
_ASSET_UNIT_THOUSAND = 5080  # 정부기관 1인당 자산취득비 단가(공표, 천원)

_GENERATED = "backend/generated"
_GRADE_ALIASES = {
    "고위": "고위공무원", "고위공무원단": "고위공무원", "고위공무원": "고위공무원",
    "3급": "3급", "4급": "4급", "5급": "5급", "6급": "6급", "7급": "7급",
    "8급": "8급", "9급": "9급",
}
_ROLE_TERMS = ("보수", "인건비", "기관부담", "기본경비", "자산취득")


def normalize_grade_composition(raw: dict[str, float]) -> dict[str, int]:
    """{'고위':1,'4급':3,...} → {'고위공무원':1,'4급':3,...} 정규화."""
    out: dict[str, int] = {}
    for key, value in (raw or {}).items():
        grade = _GRADE_ALIASES.get(re.sub(r"\s+", "", str(key)))
        if grade and value:
            out[grade] = out.get(grade, 0) + int(round(float(value)))
    return out


@lru_cache(maxsize=1)
def grade_salary_table_thousand() -> dict[str, int]:
    """DB 전체에서 직급별 1인당 보수 단가(천원)의 중앙값을 뽑은 일반 참조표."""
    by_grade: dict[str, list[float]] = {}
    for path in glob.glob(f"{_GENERATED}/**/cost_estimate_variables.jsonl", recursive=True):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                v = json.loads(line)
            except json.JSONDecodeError:
                continue
            if v.get("variable_type") != "unit_cost" or v.get("variable_value") is None:
                continue
            name = str(v.get("variable_name") or "")
            match = re.match(r"(고위공무원|[3-9]급)\s*.*(보수|단가)", name)
            if not match:
                continue
            value = float(v["variable_value"])
            if "백만" in str(v.get("variable_unit") or ""):
                value *= 1000  # 백만원 → 천원
            if 30_000 < value < 300_000:  # 1인당 보수 3천만~3억(천원) 범위
                by_grade.setdefault(match.group(1), []).append(value)
    return {g: int(statistics.median(vs)) for g, vs in by_grade.items() if vs}


def _decomposed_series(
    composition: dict[str, int], years: int, *, table: dict[str, int] | None = None
) -> dict[str, list[int]]:
    table = table if table is not None else grade_salary_table_thousand()
    base_salary = sum(
        count * table[grade]
        for grade, count in composition.items()
        if grade in table
    )
    total_staff = sum(composition.values())
    salary: list[int] = []
    employer: list[int] = []
    basic: list[int] = []
    asset: list[int] = []
    for year in range(years):
        s = base_salary * (1 + _WAGE_GROWTH) ** (year + 1)
        e = s * _EMPLOYER_RATE
        b = (s + e) * _BASIC_EXPENSE_RATIO
        salary.append(int(round(s)))
        employer.append(int(round(e)))
        basic.append(int(round(b)))
        asset.append(int(round(total_staff * _ASSET_UNIT_THOUSAND)) if year == 0 else 0)
    return {"보수": salary, "기관부담금": employer, "기본경비": basic, "자산취득비": asset}


def _is_blocked_staffing_row(row: EstimateRow) -> bool:
    mode = row.recurrence.mode if row.recurrence else ""
    obj = row.event.object if row.event else ""
    return (
        row.status in (RowStatus.NEEDS_USER_INPUT, RowStatus.NEEDS_EVIDENCE)
        and (mode == "precedent_yearly_series"
             or any(term in obj for term in _ROLE_TERMS))
    )


def apply_grade_decomposition(
    result,
    composition_raw: dict[str, float],
    *,
    years: int = 5,
    salary_table_thousand: dict[str, int] | None = None,
) -> bool:
    """직급 구성이 주어지면 막힌 인건비 행을 직급별 계산 행으로 교체한다.

    salary_table_thousand: 범용 DB 중앙값 대신 쓸 도메인 전용 단가표(예:
    court_staff_table). 실측(법원 신설 의안): 범용 중앙값을 쓰면 법원공무원
    수당이 일반직보다 높아 -12~-19% 과소추정이 났다 — 없으면 기존 범용
    중앙값을 그대로 쓴다(격리, 기존 동작 불변).

    반환: 교체가 일어났으면 True. (격리 — 구성이 없으면 호출자가 부르지 않는다.)
    """
    composition = normalize_grade_composition(composition_raw)
    table = salary_table_thousand if salary_table_thousand is not None else grade_salary_table_thousand()
    if not composition or not any(g in table for g in composition):
        return False

    blocked = [row for row in result.rows if _is_blocked_staffing_row(row)]
    if not blocked:
        return False

    template = blocked[0].event
    total_staff = sum(composition.values())
    comp_text = ", ".join(f"{g} {c}명" for g, c in composition.items())
    series = _decomposed_series(composition, years, table=table)

    result.rows = [row for row in result.rows if row not in blocked]
    for role, amounts in series.items():
        event = AtomicEvent(
            id=f"grade::{role}",
            segment_ids=list(template.segment_ids),
            article_refs=list(template.article_refs),
            quotes=list(template.quotes),
            actor=template.actor,
            action=template.action,
            object=role,
            bearer=template.bearer,
            event_type="personnel",
            obligation=template.obligation,
            cost_mechanism="staffing",
            additionality="explicit_new_or_expanded",
            recurrence_text="",
            explanation=f"직급별 분해({comp_text})",
        )
        result.rows.append(
            EstimateRow(
                event=event,
                status=RowStatus.COMPUTED_REVIEW,
                reason_codes=["GRADE_DECOMPOSED_STAFFING"],
                reason=(
                    f"직급 구성(HITL: {comp_text}, 총 {total_staff}명)을 DB 직급별 단가 "
                    f"중앙값으로 분해 계산했습니다({role}). 1인당 평균×인원 대신 "
                    "직급별 단가×직급별 인원으로 산출하여 NABO 표준 방식과 일치시켰습니다."
                ),
                formula="Σ(직급별 인원 × DB 직급별 단가) + 요율·비율",
                recurrence=Recurrence(
                    "annual" if role != "자산취득비" else "one_time",
                    None,
                    GRADE_SALARY_VERSION,
                    "직급별 분해",
                ),
                year_amounts_thousand=list(amounts),
                selection_method=GRADE_SALARY_VERSION,
                selection_reason="DB 직급별 단가 중앙값 참조표(일반)",
                selection_confidence=0.7,
                variable_evidence=[
                    {"name": g, "value": c, "unit": "명", "source": "user_input"}
                    for g, c in composition.items()
                ],
            )
        )
    return True

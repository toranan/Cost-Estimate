"""범용 '유추 추정' 계층 — 규칙 핸들러가 없는 비용항목을, 우리가 수동으로
했던 과정(개념 인식 → DB 유사 선례 population 조회 → 분포 보고 대표값 판단)을
LLM에게 맡겨 자동화한다.

역할 분담(이번 세션 전체의 결론):
- **LLM(신뢰 LLM/수동)**: (1) 이 비용이 어떤 '개념'인지 (2) 그 개념의 값 범위
  (3) 분포를 보고 어떤 대표값이 합리적인지(중앙값/하위군집/특정값) 판단.
- **결정적 코드(concept_cost_reference)**: 그 개념의 population을 DB에서 뽑아
  제공. LLM이 '가장 비슷한 선례 하나'를 고르는 게 아니라 population 위에서
  대표값을 정하게 한다(유사도-단일선택의 함정 회피).

산출물은 항상 저신뢰이며 분포·근거·표본을 함께 남긴다(블랙박스 금지).
실측(양봉인의날): 개념=기념행사비, 대표=하위군집 → 1억, 정답 1억(+0%);
blind 중앙값(1.26억)보다 나았다. 단 표본이 작아 일반화는 계속 검증 대상.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .tag_reference_lookup import concept_cost_reference


@dataclass(frozen=True)
class AnalogyEstimate:
    label: str                       # 비용항목명
    concept_pattern: str             # LLM이 정한 개념 매칭 정규식
    value_range: tuple[float, float] = (0, float("inf"))
    exclude_pattern: str | None = None
    representative: str = "median"   # "median" | "lower_cluster" | "trimmed" | f"specific:{값}"
    reasoning: str = ""              # LLM의 판단 근거(왜 이 대표값인가)


def _apply_representative(samples: list[int], representative: str) -> int | None:
    if not samples:
        return None
    s = sorted(samples)
    if representative.startswith("specific:"):
        try:
            return int(float(representative.split(":", 1)[1]))
        except ValueError:
            return int(statistics.median(s))
    if representative == "lower_cluster":
        # 전체 중앙값 이하만 모아 다시 중앙값 — 대규모 이상치를 배제한 '보통 규모'.
        med = statistics.median(s)
        low = [x for x in s if x <= med] or s
        return int(statistics.median(low))
    if representative == "trimmed":
        # 상하위 10%씩 절사
        k = max(1, len(s) // 10)
        core = s[k:-k] or s
        return int(statistics.median(core))
    return int(statistics.median(s))  # 기본: median


def compute_analogy(est: AnalogyEstimate) -> dict:
    """LLM의 유추 판단을 DB population으로 실제 계산한다."""
    ref = concept_cost_reference(
        est.concept_pattern, label=est.label,
        value_range=est.value_range, exclude_pattern=est.exclude_pattern,
    )
    if ref.value is None:
        return {
            "name": est.label, "committee_type": "analogy_estimate",
            "status": "review_required",
            "reason": f"유추 추정 실패: {ref.reason}",
        }
    value = _apply_representative(list(ref.samples), est.representative)
    return {
        "name": est.label,
        "committee_type": "analogy_estimate",
        "annual_cost_won": value,
        "concept": est.label,
        "representative": est.representative,
        "n_precedents": ref.n,
        "sample_range": [min(ref.samples), max(ref.samples)] if ref.samples else None,
        "status": "calculated_low_confidence",
        "reason": (
            f"규칙 핸들러가 없는 유형이라 '{est.label}' 개념의 DB 선례 {ref.n}건에서 "
            f"{est.representative} 대표값을 유추 추정(저신뢰). {est.reasoning}"
        ),
        "trace": (
            f"개념='{est.label}' 선례 {ref.n}건(범위 "
            f"{min(ref.samples):,}~{max(ref.samples):,}) → {est.representative} = {value:,}원"
        ),
    }


def estimate_by_analogy_llm(article_text: str) -> list[AnalogyEstimate]:
    """규칙 핸들러가 없는 비용항목을 LLM이 유추 추정하도록 개념·대표값을 판단.

    프로덕션 기본 구현은 비어 있다([] 반환) — 아무 비용에나 유추를 남발하면
    오탐이 되므로, 신뢰 LLM(또는 수동 하네스의 Claude)이 명확한 유추 대상일
    때만 개념을 제안하도록 대체 패치한다. 프로덕션 solar는 비결정성 때문에
    이 판단을 맡기지 않는다."""
    return []

"""TAG 동적 참조 조회 계층 (Repository 패턴, additive).

문제의식: tag_rule_engine.py의 계산 상수는 대부분 정적 하드코딩이었다.
"TAG 엔진"이라는 이름과 달리, 실제로는 공식 추계서를 구조화해둔
TAG DB(cost_estimate_*.jsonl)를 계산 시점에 한 번도 조회하지 않고 있었다
(2124118 실측으로 확인: 상임위원 인건비를 "전체 공무원 평균"이라는
무관한 정적 상수로 계산해 실제값의 절반으로 축소됨).

이 모듈은 그 갭을 메우는 별도 계층이다 — 계산 로직(tag_rule_engine.py)과
DB 사이에 "조회를 전담하는" Repository를 둔다. 원칙:

1. 우선순위 1: TAG DB에서 해당 역할의 실측 population을 동적으로 뽑는다.
2. **population이 안정적으로 좁게 수렴할 때만** 대표값을 반환한다 — 이게
   핵심 안전장치다. 표본이 넓게 퍼져 있으면(=서로 다른 성격이 섞여
   있다는 뜻) 억지로 대표값을 만들지 않고 None을 반환해, 호출자가 기존
   정적 폴백(L2)으로 이어가게 한다.
3. 안정성 기준(임의로 정한 게 아니라 이번 세션 실측으로 정함):
   - 표본 3개 이상
   - 최대/최소 비율 1.5배 이내
   실제로 이 기준을 적용해본 결과:
     · 상임위원 무등급 급여: n=3, 136M~140M(비율 1.03) → **통과, 실전 검증됨**
       (2124118: 9,009만→1억7,576만, 실제 1.8억과 거의 일치.
        2126661: 오차 -42%→+14%로 축소)
     · 회의횟수(평가/심사 키워드로 좁힌 population): n=7, 1~52회(비율 52)
       → **기각**. "연례 기관평가"와 "건별 심사"라는 무관한 두 패턴이
       섞여 있었다(환경영향평가위원회 14회, 방송분야공익성심사위원회 1회 등).
     · 파견인력 비율: n=2, 40.4%/83.3%(비율 2.06) → **기각**. 표본 자체가
       너무 적고 기관마다 판이하게 다름.
   즉 이 게이트는 실제로 "맞는 걸 통과시키고 틀린 걸 걸러낸" 기록이 있다.

4. 소속기관 조건부 조회(2026-08-12 추가): 일부 변수는 "전체 population"으로는
   불안정(표본 분산 과대)하지만, "소속기관명"으로 쪼개면 각 하위집단이
   극도로 좁게 수렴한다 — NABO 실제 추계서가 "OO부의 인건비 대비 기본경비
   비율(X%)을 적용"처럼 소속기관 실측치를 그대로 인용하는 패턴이기 때문.
   실측(기본경비 비율): 전체 population은 0.075~29.02%(ratio 387, 기각)지만
   법무부(n=4, ratio 1.16), 대법원(n=8, ratio 1.04), 국회(n=9, ratio 1.02),
   헌법재판소(n=3, ratio 1.06), 국가인권위원회(n=3, ratio 1.00), 보건복지부
   (n=3, ratio 1.29), 해양수산부(n=3, ratio 1.22)는 전부 안정적. 2124118
   (법무부 소속)에서 대표값(9.6%)이 실제 정답(9.6%)과 정확히 일치함을 확인.
   범용 엔진(tag_engine_prototype.py)으로 다른 5개 후보(기관부담률·자산취득비·
   사업비비중·보수상승률·회의수당단가)도 같은 방식으로 테스트했으나 전부
   표본 자체가 부족(대부분 기관당 n=0~1) — 기본경비 비율만 유일하게
   "소속기관별로 정말 자주, 정말 명시적으로 인용되는" 변수였다.

   소속기관 추출은 committee_channel._primary_authority()를 그대로 쓰지 않고
   아래 _extract_owning_institution()을 별도로 둔다 — _primary_authority()의
   3단계 fallback 중 마지막 단계("OO장관"이 문서 어디에든 언급되면 매칭)가
   실측(2126635)에서 "행정안전부장관에게 자료송부를 요청할 수 있다"는 절차적
   언급을 소속기관으로 오인하는 오탐을 냈다 — 금액 매칭에 쓰기엔 위험해서,
   "OO에 위원회를 둔다/설치"류의 강한 소유 패턴만 신뢰하는 별도 버전을 쓴다.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

from .committee_channel import _read_jsonl_records, _tag_roots

TAG_REFERENCE_LOOKUP_VERSION = "tag-reference-lookup-v3"

# 소속기관 정확매칭 대상 — 중앙행정기관(부/처/청) + 헌법·독립기관. TAG DB에서
# "OO의 인건비 대비 기본경비 비율" 문장이 실제로 관측된 기관만 넣을 필요는
# 없다(표본 없으면 자동으로 표본부족 처리됨) — 대신 오탐(엉뚱한 기관을
# "장관"류 절차적 언급에서 뽑아내는 것)을 막기 위해 매칭 후보 자체를 실존
# 기관명으로 한정한다.
_OWNING_INSTITUTIONS = (
    "과학기술정보통신부", "기후에너지환경부", "식품의약품안전처", "산업통상자원부",
    "중소벤처기업부", "문화체육관광부", "농림축산식품부", "성평등가족부",
    "여성가족부", "행정안전부", "해양수산부", "인사혁신처", "국가통계처",
    "국토교통부", "국가보훈부", "보건복지부", "기획재정부", "질병관리청",
    "고용노동부", "경찰청", "국방부", "외교부", "통계청", "법제처", "조달청",
    "환경부", "교육부", "산림청", "법무부", "통일부", "특허청", "소방청",
    "대법원", "헌법재판소", "금융위원회", "국회", "국가인권위원회", "감사원",
    "공정거래위원회",
)

# "OO에 (…) 위원회를 둔다/설치/구성한다" 류의 강한 소속-설치 문장만 매칭한다.
# committee_channel._primary_authority()의 약한 fallback("OO장관"이 문서
# 어디든 언급되면 매칭)은 여기서는 쓰지 않는다 — 실측(2126635)에서 절차적
# 언급("행정안전부장관에게 자료를 요청할 수 있다")을 소속기관으로 오인했다.
_OWNED_COMMITTEE_RE = re.compile(
    r"([가-힣]{2,10})(?:장관\s*)?소속(?:하에|으로)?[^.]{0,30}"
    r"(?:위원회|심의회|협의회)를?\s*(?:둔다|설치|구성)"
)


def _extract_owning_institution(article_text: str) -> str | None:
    """조문에서 '설치 주체' 기관명을 강한 패턴으로만 뽑는다. 못 찾으면 None —
    억지로 약한 매칭까지 내려가지 않는다(오탐이 확정 금액 오류로 이어지므로)."""
    compact = re.sub(r"\s+", "", article_text or "")
    for institution in _OWNING_INSTITUTIONS:
        pattern = re.compile(
            re.escape(institution) + r"(?:장관\s*)?에(?:는)?서?[^.]{0,30}"
            r"(?:위원회|심의회|협의회)를?\s*(?:둔다|설치|구성)"
        )
        if pattern.search(compact):
            return institution
    match = _OWNED_COMMITTEE_RE.search(compact)
    if match:
        candidate = match.group(1)
        for institution in _OWNING_INSTITUTIONS:
            if candidate.endswith(institution) or institution in candidate:
                return institution
    return None


@dataclass(frozen=True)
class ReferenceResult:
    """동적 조회 1건의 결과. value가 None이면 호출자는 반드시 정적 폴백으로
    이어가야 한다 — stable=False를 무시하고 samples만 믿고 값을 만들면 안 된다."""

    value: int | None
    stable: bool
    n: int
    samples: tuple[int, ...] = field(default_factory=tuple)
    max_min_ratio: float | None = None
    source: str = ""
    reason: str = ""


class TagReferenceRepository:
    """TAG DB(cost_estimate_variables.jsonl)에서 역할별 실측 population을
    조회하고 안정성을 검증하는 단일 진입점.

    새 참조값을 추가하려면 `_registry`에 (라벨, 매칭 정규식, 제외 정규식,
    값 범위) 한 줄만 추가하면 된다 — 조회·안정성판정·캐싱 로직은 공용이다.
    """

    MIN_SAMPLES = 3
    MAX_RATIO = 1.5

    def __init__(self) -> None:
        self._registry: dict[str, dict] = {
            "standing_member_salary": {
                "match_re": re.compile(r"상임위원.*(?:보수|인건비|연봉)"),
                # 등급이 명시된 값(차관급 등)은 annual_base_salary(grade)로 이미
                # 처리 가능하므로 제외 — 섞으면 무등급 대표값이 왜곡된다.
                "exclude_re": re.compile(r"차관급|장관급|1급|2급|3급"),
                "value_range": (10_000_000, 500_000_000),
            },
        }

    @lru_cache(maxsize=None)  # noqa: B019 — 프로세스 생존 기간 캐시, 인스턴스도 보통 1개만 씀
    def _samples(self, key: str) -> tuple[int, ...]:
        spec = self._registry[key]
        match_re: re.Pattern = spec["match_re"]
        exclude_re: re.Pattern | None = spec.get("exclude_re")
        low, high = spec["value_range"]
        samples: list[int] = []
        for root in _tag_roots():
            for row in _read_jsonl_records(root / "cost_estimate_variables.jsonl"):
                combined = str(row.get("variable_name") or "") + str(row.get("source_text") or "")
                if not match_re.search(combined):
                    continue
                if exclude_re and exclude_re.search(combined):
                    continue
                value = row.get("variable_value")
                if isinstance(value, (int, float)) and low <= value <= high:
                    samples.append(int(value))
        return tuple(samples)

    def _resolve(self, key: str) -> ReferenceResult:
        samples = self._samples(key)
        if len(samples) < self.MIN_SAMPLES:
            return ReferenceResult(
                value=None, stable=False, n=len(samples), samples=samples,
                source=f"tag_db:{key}",
                reason=f"표본 부족(n={len(samples)} < {self.MIN_SAMPLES})",
            )
        ratio = max(samples) / min(samples)
        if ratio > self.MAX_RATIO:
            return ReferenceResult(
                value=None, stable=False, n=len(samples), samples=samples,
                max_min_ratio=ratio, source=f"tag_db:{key}",
                reason=f"표본 분산 과대(최대/최소={ratio:.2f} > {self.MAX_RATIO})",
            )
        return ReferenceResult(
            value=int(statistics.median(samples)), stable=True, n=len(samples),
            samples=samples, max_min_ratio=ratio, source=f"tag_db:{key}",
            reason="안정적 수렴",
        )

    def standing_member_salary(self) -> ReferenceResult:
        """등급 미상 상임위원의 연간 보수 대표값. 실전 검증됨(2124118·2126661)."""
        return self._resolve("standing_member_salary")

    _BASIC_EXPENSE_RATIO_RE = re.compile(r"기본경비")

    @lru_cache(maxsize=None)  # noqa: B019
    def _basic_expense_ratio_samples(self, institution: str) -> tuple[float, ...]:
        samples: list[float] = []
        for root in _tag_roots():
            for row in _read_jsonl_records(root / "cost_estimate_variables.jsonl"):
                combined = str(row.get("variable_name") or "") + str(row.get("source_text") or "")
                if not self._BASIC_EXPENSE_RATIO_RE.search(combined):
                    continue
                if institution not in (row.get("source_text") or ""):
                    continue
                value = row.get("variable_value")
                if isinstance(value, (int, float)) and 0 < value <= 100:
                    samples.append(float(value))
        return tuple(samples)

    def basic_expense_ratio_by_authority(self, institution: str) -> ReferenceResult:
        """소속기관명으로 좁힌 기본경비 비율 대표값(%). 실전 검증됨(2124118: 법무부
        n=4 → 대표값 9.6%, 실제 정답 9.6%와 정확히 일치). institution이 None/빈
        문자열이면 즉시 표본없음을 반환한다(호출자가 조문에서 소속기관을 못 찾은
        경우)."""
        if not institution:
            return ReferenceResult(
                value=None, stable=False, n=0, source="tag_db:basic_expense_ratio_by_authority",
                reason="소속기관을 조문에서 특정하지 못함",
            )
        samples = self._basic_expense_ratio_samples(institution)
        source = f"tag_db:basic_expense_ratio:{institution}"
        if len(samples) < self.MIN_SAMPLES:
            return ReferenceResult(
                value=None, stable=False, n=len(samples), samples=tuple(samples),
                source=source, reason=f"{institution} 표본 부족(n={len(samples)} < {self.MIN_SAMPLES})",
            )
        ratio = max(samples) / min(samples)
        if ratio > self.MAX_RATIO:
            return ReferenceResult(
                value=None, stable=False, n=len(samples), samples=tuple(samples),
                max_min_ratio=ratio, source=source,
                reason=f"{institution} 표본 분산 과대(최대/최소={ratio:.2f} > {self.MAX_RATIO})",
            )
        return ReferenceResult(
            value=statistics.median(samples) / 100, stable=True, n=len(samples),
            samples=tuple(samples), max_min_ratio=ratio, source=source,
            reason=f"{institution} 실측치로 안정적 수렴",
        )


_repository = TagReferenceRepository()


def standing_member_salary_reference() -> tuple[int | None, ReferenceResult]:
    """tag_rule_engine.py 호환용 얇은 래퍼. (값 또는 None, 근거) 형태로 반환."""
    result = _repository.standing_member_salary()
    return result.value, result


def basic_expense_ratio_reference(article_text: str) -> tuple[float | None, ReferenceResult]:
    """조문에서 소속기관을 추출해 그 기관의 기본경비 비율 실측 대표값을 찾는다.
    소속기관을 못 찾거나 표본이 불안정하면 (None, 근거)를 반환 — 호출자는 반드시
    기존 정적 상수(BASIC_EXPENSE_RATIO)로 폴백해야 한다."""
    institution = _extract_owning_institution(article_text)
    result = _repository.basic_expense_ratio_by_authority(institution)
    return result.value, result


def concept_cost_reference(
    concept_pattern: str,
    *,
    label: str,
    value_range: tuple[float, float] = (0, float("inf")),
    exclude_pattern: str | None = None,
) -> ReferenceResult:
    """범용 '개념 유추 조회' — 규칙 핸들러가 없는 비용항목에 대해, LLM이 정한
    개념(concept_pattern)으로 DB population을 뽑아 대표값을 낸다.

    설계 원칙(이번 세션 전체의 결론): LLM은 '이 비용은 어떤 개념인가'만 정하고,
    실제 대표값 산출은 여기서 결정적으로 한다. LLM이 '가장 비슷한 선례 하나'를
    고르는 게 아니라, 코드가 개념 population 전체의 중앙값을 쓴다(유사도 매칭의
    함정 회피).

    수렴 정도에 따라 stable을 판정한다:
    - n≥3, 최대/최소≤1.5 → stable=True (기본경비율처럼 확정 참조)
    - 넓게 퍼졌으면 stable=False + 중앙값(저신뢰 추정, 무출력보다 나음)
    실측(기념행사비): 21건 중앙값 1.26억, 양봉인의날 정답 1억(+26%)."""
    match_re = re.compile(concept_pattern)
    exclude_re = re.compile(exclude_pattern) if exclude_pattern else None
    low, high = value_range
    samples: list[int] = []
    for root in _tag_roots():
        for row in _read_jsonl_records(root / "cost_estimate_variables.jsonl"):
            combined = str(row.get("variable_name") or "") + str(row.get("source_text") or "")
            if not match_re.search(combined):
                continue
            if exclude_re and exclude_re.search(combined):
                continue
            v = row.get("variable_value")
            if isinstance(v, (int, float)) and low <= v <= high:
                samples.append(int(v))
    src = f"tag_db:concept:{label}"
    if len(samples) < 3:
        return ReferenceResult(value=None, stable=False, n=len(samples),
                               samples=tuple(samples), source=src,
                               reason=f"'{label}' 표본 부족(n={len(samples)})")
    ratio = max(samples) / min(samples) if min(samples) > 0 else float("inf")
    med = int(statistics.median(samples))
    stable = ratio <= 1.5
    return ReferenceResult(
        value=med, stable=stable, n=len(samples), samples=tuple(sorted(samples)),
        max_min_ratio=ratio, source=src,
        reason=(f"'{label}' {len(samples)}건 안정 수렴" if stable
                else f"'{label}' {len(samples)}건 중앙값(저신뢰 — 편차 큼, ratio={ratio:.1f})"),
    )

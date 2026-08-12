"""국회 자체 특별위원회(4번째 아키타입) 판별 게이트 (격리 모듈, additive).

실측(2211757, 2126636 — n=2): 위원 자신이 국회의원인 위원회는 참석수당
개념 자체가 없다(세비로 이미 지급됨). NABO는 이 경우 위원 비용은 아예
계산하지 않고, 대신 위원회를 지원하는 사무처 소속 지원인력(전문위원·
입법조사관 등)의 인건비만 계산한다 — 2211757: 15명 위원회에 6명 지원,
2126636: 30명 위원회에 15명 지원(각각 40%/50%).

이 판별은 "다중 위원회가 뒤섞인 긴 문서에서 개체를 그루핑하는" 작업과
달리 "위원회 하나를 두고 좁은 범위에서 문자 그대로의 근거(본회의에서
선거한다/국회법 O조를 준용한다 등)를 읽는" 작업이라 LLM이 실측으로 3/3
정확했다(2126636/2126334/2213848) — 그래도 인용 근거가 원문에 실제로
있는지 검증(grounding)해서 할루시네이션 방지선은 유지한다.
"""
from __future__ import annotations

import re

from .article_extraction_engine import _call_upstage_json

COMMITTEE_ASSEMBLY_INTERNAL_GATE_VERSION = "committee-assembly-internal-gate-v1"

_PROMPT = """법안 조문에서 위원회 성격을 판별한다. 절대 계산하지 마라.

is_assembly_internal: 이 위원회의 위원이 국회의원 자신들로 구성되는가(예:
위원장을 본회의에서 선거, 국회법상 상임위원회 선임 규정을 준용, 재적위원
3분의 2 찬성 등 국회 내부 절차)? true 또는 false.
reasoning: 판단 근거가 된 조문 문구를 원문 그대로 인용(패러프레이즈 금지).

JSON만: {{"is_assembly_internal": true 또는 false, "reasoning": "..."}}

[조문]
{article_text}
"""

# 실측(2211757, 2126636): 위원정수 대비 지원인력 비율 — n=2뿐이라 정밀하지
# 않지만, 참석수당 산식보다는 최소한 방향이 맞는 저신뢰 폴백이다.
SUPPORT_STAFF_RATIO_FALLBACK = (0.40, 0.50)  # n=2, 로컬 실측


def is_assembly_internal_committee(article_text: str) -> bool:
    """조문을 읽고 위원=국회의원 자신인 위원회인지 판별. LLM 판단을 원문
    대조로 검증(grounding)하며, 검증 실패 시 안전하게 False(일반 위원회
    취급)로 폴백한다 — 이 아키타입 오탐(false positive)이 훨씬 위험하기
    때문(참석수당을 통째로 빼먹게 됨)."""
    try:
        parsed = _call_upstage_json(_PROMPT.format(article_text=article_text))
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    if not parsed.get("is_assembly_internal"):
        return False
    reasoning = str(parsed.get("reasoning") or "").strip()
    if len(reasoning) < 12:
        return False
    # 실측: LLM이 인용하면서 조사(의/을/를/이/가/은/는/도/과/와/에) 한두 개를
    # 빠뜨리는 경우가 있다(내용은 원문 그대로인데 완전일치만 깨짐) — 조사를
    # 지우고 비교해 사소한 누락은 허용하되, 실질 내용은 원문 그대로여야 한다.
    _PARTICLES = re.compile(r"[의을를이가은는도과와에]")
    compact_reasoning = _PARTICLES.sub("", re.sub(r"\s+", "", reasoning))
    compact_text = _PARTICLES.sub("", re.sub(r"\s+", "", article_text or ""))
    return compact_reasoning in compact_text

"""한시(限時) 위원회 활동기간 판별 — LLM 보강 (격리 모듈, additive).

실측(2126661): 정규식(`committee_size_extract.extract_activity_duration_months`)은
"OO년 이내에 활동을 완료" 문구엔 맞았지만, 다른 표현("존속기간은 OO년으로
한다")은 못 잡았다(None) — 반면 같은 프롬프트로 LLM(Upstage solar-pro3)에
물었더니 두 표현 다 정확히 뽑고, 국립대학병원위원회(영구상설, 한시 아님)도
정확히 false로 판별했다. 활동기간 판별은 "좁은 범위(보통 조 하나)에서
근거가 근처에 있는 판단+간단한 합산"이라 국회자체위원회 판별 때와 같은
이유로 LLM이 잘 맞는다 — 그래도 인용 근거가 원문에 실제로 있는지 검증
(grounding)해서 할루시네이션 방지선은 유지한다.
"""
from __future__ import annotations

import re

from .article_extraction_engine import _call_upstage_json

COMMITTEE_ACTIVITY_DURATION_GATE_VERSION = "committee-activity-duration-gate-v1"

_PROMPT = """법안 조문에서 이 위원회가 활동기간이 정해진 한시(限時) 조직인지
판별한다. 절대 계산하지 마라.

is_time_limited: 활동기간이나 존속기간이 조문에 명시돼 있는가? true 또는 false.
base_months: 최초 활동기간(개월 환산). 명시 없으면 null.
extension_months_list: 연장 가능한 기간들(개월 환산) 목록 — 조문에 나오는
대로 각각. 없으면 빈 배열.
reasoning: 판단 근거가 된 조문 문구를 원문 그대로 인용(패러프레이즈 금지).

JSON만: {{"is_time_limited": true 또는 false, "base_months": 0 또는 null, "extension_months_list": [], "reasoning": "..."}}

[조문]
{article_text}
"""

_PARTICLES = re.compile(r"[의을를이가은는도과와에]")


def _grounded(reasoning: str, article_text: str) -> bool:
    if len(reasoning) < 12:
        return False
    compact_reasoning = _PARTICLES.sub("", re.sub(r"\s+", "", reasoning))
    compact_text = _PARTICLES.sub("", re.sub(r"\s+", "", article_text or ""))
    return compact_reasoning in compact_text


def extract_activity_duration_months_llm(article_text: str) -> int | None:
    """LLM으로 활동기간(개월)을 판별. 근거 인용문이 원문에 실제로 있는지
    검증하며, 검증 실패·판별 실패·오류 시 전부 None(영구 상설 취급 —
    한시 오탐이 놓친 것보다 더 위험하기 때문)."""
    try:
        parsed = _call_upstage_json(_PROMPT.format(article_text=article_text))
    except Exception:
        return None
    if not isinstance(parsed, dict) or not parsed.get("is_time_limited"):
        return None
    reasoning = str(parsed.get("reasoning") or "").strip()
    if not _grounded(reasoning, article_text):
        return None
    base = parsed.get("base_months")
    if not isinstance(base, (int, float)) or base <= 0:
        return None
    extensions = parsed.get("extension_months_list")
    extension_total = 0
    if isinstance(extensions, list):
        extension_total = sum(x for x in extensions if isinstance(x, (int, float)) and x > 0)
    return int(base) + int(extension_total)

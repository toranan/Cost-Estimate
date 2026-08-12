"""위원회 3대 유형 판별 게이트 (기계적 키워드, LLM 미사용).

NABO 2010년 공식 참조자료의 3~4유형 분류(①중앙행정기관형 ②-1/②-2
부처소속·조사위원회형 ③단순자문심의의결형)를 실무적으로 3분기로 단순화한다.
"위원회"라는 이름 하나로 규모가 완전히 다른 조직이 섞여 있어(로컬 TAG DB
실측: 단순위원회형 연간 운영비 300만~2,600만원 vs 사무처형 위원회는
수억~수십억원), 이 게이트 없이 하나의 산식으로 계산하면 큰 오차가 난다.
"""
from __future__ import annotations

import re

COMMITTEE_TYPE_GATE_VERSION = "committee-type-gate-v1"

TYPE_1_CENTRAL_AGENCY = "type1_central_agency"
TYPE_2_SECRETARIAT = "type2_secretariat"
TYPE_3_SIMPLE_ADVISORY = "type3_simple_advisory"
# 실측(2211757, 2126636 — n=2): 위원이 국회의원 자신들로 구성되는 국회
# 자체 특별위원회. 참석수당 개념이 없다(세비로 이미 지급됨) — 규칙(정규식)
# 신호가 아니라 committee_assembly_internal_gate.py의 LLM 판별(근거 원문
# 대조 검증 포함)로 감지한다.
TYPE_4_ASSEMBLY_INTERNAL = "type4_assembly_internal"

_TYPE1_RE = re.compile(
    r"중앙행정기관으로\s*본다|그\s*소관\s*사무를\s*독립하여\s*수행한다"
)
_TYPE2_RE = re.compile(
    r"사무국을\s*둔다|사무처를\s*둔다|사무국을\s*설치한다|사무처를\s*설치한다|"
    r"사무국을\s*둘\s*수\s*있다|사무처를\s*둘\s*수\s*있다"
)


def classify_committee_type(article_text: str) -> str:
    """조문 텍스트에서 위원회 유형을 판별한다. 판별 안 되면 3유형(기본값)."""
    if _TYPE1_RE.search(article_text):
        return TYPE_1_CENTRAL_AGENCY
    if _TYPE2_RE.search(article_text):
        return TYPE_2_SECRETARIAT
    return TYPE_3_SIMPLE_ADVISORY

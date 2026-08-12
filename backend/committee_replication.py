"""위원회 복제설치(전국/지역단위 반복설치) 감지 (격리 모듈, additive).

committee_channel.py(기존 프로덕션 시스템)의 `_replication_factor()`를
이식·확장. 실측(2126145 지역금융위원회): 조문이 "지방자치단체에 ~를 둔다"
라고만 돼있으면, 이건 시·도(17)인지 시·군·구(229)인지조차 조문에 명시가
안 돼있고 NABO 스스로도 "예측하기 어려우므로 17개로 가정"한다고 밝힘 —
즉 정확한 배수는 조문만 봐서는 원리적으로 알 수 없다(대체/증분형과 같은
근본적 한계). 그래서 이 모듈은 정확한 배수를 확정하지 않고, "단일 설치
기준값 × 미상의 반복설치 배수"라는 구조를 감지해 review_required로
정직하게 플래그하는 것까지만 한다.
"""
from __future__ import annotations

import re

COMMITTEE_REPLICATION_VERSION = "committee-replication-v2"

_SEP = r"[·ㆍ]?"

# 본문에 반복설치 개수가 명시된 경우(우선순위 최상 — 확정치)
_REPL_EXPLICIT_RE = re.compile(
    rf"(\d{{1,3}})개(?:의)?(?:광역|지방자치단체|시{_SEP}도|지역|기관|법원|권역|지부)"
)

# 행정구역 상수(committee_channel.py와 동일) — 실체명 또는 설치 콜로케이션에서만 인정
_JURISDICTION_UNITS: list[tuple[re.Pattern[str], re.Pattern[str], int, str]] = [
    (
        re.compile(rf"시{_SEP}군{_SEP}구|기초지방자치단체|기초자치단체|자치구"),
        re.compile(rf"(?:시{_SEP}군{_SEP}구|기초지방자치단체|자치구)[가-힣]{{0,12}}(?:둔다|설치|구성)"),
        229, "시·군·구(기초자치단체 229)",
    ),
    (
        # 실측(2126679 돌봄근로자법): "시·도 돌봄근로자 처우개선위원회"처럼
        # "시도"와 "위원회" 사이에 긴 복합명칭이 끼는 실제 사례가 있어
        # {0,6}은 너무 좁았다({0,20}으로 확대).
        re.compile(rf"시{_SEP}도위원회|시{_SEP}도[가-힣]{{0,20}}위원회|광역지방자치단체|광역자치단체"),
        re.compile(rf"각\s*시{_SEP}도[가-힣]{{0,12}}(?:둔다|설치|구성)|시{_SEP}도별[가-힣]{{0,12}}(?:둔다|설치)|시{_SEP}도마다[가-힣]{{0,12}}(?:둔다|설치)"),
        17, "시·도(광역자치단체 17)",
    ),
]

_OPTIONAL_RE = re.compile(r"둘수있|설치할수있|구성할수있|임의")

# 실측(2126145) 추가: 구체적 행정단위(시·도/시·군·구) 명시 없이 그냥
# "지방자치단체에 ~를 둔다"만 있는 경우 — 배수는 모르지만 반복설치 구조는
# 있다는 신호. 배수를 확정하지 않고 review_required 플래그만 붙인다.
_GENERIC_LOCAL_GOV_RE = re.compile(r"지방자치단체에.{0,80}?(?:를|을)?\s*둔다|지방자치단체에.{0,80}?설치한다")


def detect_replication(article_text: str, committee_name: str = "") -> dict:
    """반환: {"factor": int, "confident": bool, "basis": str}.

    factor=1, confident=True는 "복제 정황 없음"(단일 설치로 확정).
    factor>1, confident=False는 "배수는 추정이니 검토 필요".
    factor=1, confident=False, basis에 "미상"이 있으면 "복제되는 것 같은데
    배수를 모른다"는 뜻 — 이 경우 단일값을 하한으로만 써야 한다.
    """
    compact_entity = re.sub(r"\s+", "", committee_name or "")
    compact_text = re.sub(r"\s+", "", article_text or "")
    combined = compact_entity + compact_text

    explicit = _REPL_EXPLICIT_RE.search(combined)
    if explicit:
        n = int(explicit.group(1))
        if 1 < n <= 300:
            return {
                "factor": n, "confident": True,
                "basis": f"본문의 '{explicit.group(0)}' 표현에서 복제 설치 수 {n}개를 명시적으로 확인했습니다.",
            }

    optional = bool(_OPTIONAL_RE.search(compact_text))

    # 한 조문에 시·도 위원회와 시·군·구 위원회가 함께 정의될 수 있다. 이때
    # 조문 전체를 먼저 보면 먼저 등록된 시·군·구 패턴이 시·도 개체까지 229개로
    # 오염시킨다. 개체명에 행정단위가 명시된 경우에는 그 이름의 단위를 우선한다.
    named_units = [
        (name_pat, count, label)
        for name_pat, _establish_pat, count, label in _JURISDICTION_UNITS
        if name_pat.search(compact_entity)
    ]
    if named_units:
        _name_pat, count, label = named_units[0]
        if optional:
            return {
                "factor": 1, "confident": False,
                "basis": f"{label} 단위 복제 정황이나 임의설치('둘 수 있다')라 전수 적용이 부적절합니다.",
            }
        return {
            "factor": count, "confident": False,
            "basis": f"{label} 단위 의무 복제로 추정해 {count}개를 적용했습니다(실제 설치 수 확인 필요).",
        }

    for name_pat, establish_pat, count, label in _JURISDICTION_UNITS:
        if establish_pat.search(compact_text):
            if optional:
                return {
                    "factor": 1, "confident": False,
                    "basis": f"{label} 단위 복제 정황이나 임의설치('둘 수 있다')라 전수 적용이 부적절합니다.",
                }
            return {
                "factor": count, "confident": False,
                "basis": f"{label} 단위 의무 복제로 추정해 {count}개를 적용했습니다(실제 설치 수 확인 필요).",
            }

    if _GENERIC_LOCAL_GOV_RE.search(compact_text):
        return {
            "factor": 1, "confident": False,
            "basis": (
                "지방자치단체 단위 반복설치 정황이 있으나 몇 개 단위(시·도/시·군·구)인지 "
                "조문에 명시가 없습니다 — 표시값은 '단일 설치 기준'이며 실제 총비용은 "
                "이보다 훨씬 클 수 있습니다(실측: 2126145, 조문에 없는 정보라 NABO도 자체 가정)."
            ),
        }

    return {"factor": 1, "confident": True, "basis": "복제 설치 정황 없음(단일 설치로 판단)."}

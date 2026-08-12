"""[Phase 1] 텍스트 전처리 및 라우터.

조문 청킹은 article_extraction_engine.py를 재사용한다. 이 모듈은 그 다음
단계인 "비용유발 조문 필터링"과 "6대 카테고리 라우팅"만 담당한다.
"""
from __future__ import annotations

import re

from .article_extraction_engine import extract_pdf_text, split_articles

TAG_ROUTER_VERSION = "tag-router-v1"

CATEGORIES = (
    "1_인건비_물건비",
    "2_이전지출",
    "3_자본지출",
    "4_출자출연금",
    "5_조세감면",
    "6_세외수입감소",
)

# 비용을 유발하는 동사/구조 신호. 목적·정의 조문(순수 선언문)은 여기서 걸러진다.
_COST_TRIGGER_RE = re.compile(
    r"지급한다|지급할\s*수\s*있다|지원한다|지원할\s*수\s*있다|"
    r"설치한다|둔다|구성한다|신설한다|증원한다|"
    r"보조한다|보조할\s*수\s*있다|출연한다|출자한다|"
    r"구축한다|운영한다|위탁한다|"
    r"감면한다|면제한다|경감한다|공제한다|"
    r"징수하지\s*아니한다|감액한다"
)

_CATEGORY_PATTERNS: dict[str, re.Pattern[str]] = {
    "1_인건비_물건비": re.compile(
        r"위원회|심의회|협의회|정원|공무원|증원|"
        r"(?:인력|직원).{0,20}(?:채용|배치|증원|정원)|"
        r"신설.{0,10}(?:기관|부서|처|청|원)|인건비|보수|봉급"
    ),
    "2_이전지출": re.compile(
        r"수당|급여|지원금|지원비|보조금|장려금|연금|수급|지급대상|생계비|"
        r"비용.{0,15}지원"
    ),
    "3_자본지출": re.compile(
        r"정보시스템|전산망|시스템\s*구축|건축|청사|시설\s*조성|단지\s*조성|공사"
    ),
    "4_출자출연금": re.compile(r"출자|출연|기금\s*조성|재단\s*설립|공사\s*설립"),
    "5_조세감면": re.compile(r"세율|과세표준|세액공제|비과세|감면|면세|조세특례"),
    "6_세외수입감소": re.compile(r"수수료|부담금|과징금|이용료|사용료.{0,10}(?:면제|감면|인하)"),
}


def is_cost_trigger(article_text: str) -> bool:
    """비용을 유발하는 조문인지 기계적으로 판별한다(경량 T/F, LLM 미사용)."""
    return bool(_COST_TRIGGER_RE.search(article_text))


def route_article(article_text: str) -> list[str]:
    """조문 텍스트에 6대 카테고리 중 해당하는 태그를 모두 부여한다(multi-label)."""
    return [
        category
        for category, pattern in _CATEGORY_PATTERNS.items()
        if pattern.search(article_text)
    ]


def route_bill(pdf_bytes: bytes) -> list[dict]:
    """PDF → 조문 청킹 → 비용유발 필터 → 카테고리 라우팅까지 Phase 1 전체."""
    text = extract_pdf_text(pdf_bytes)
    articles, doc_type = split_articles(text)

    routed: list[dict] = []
    for article in articles:
        body = f"{article.get('title', '')} {article.get('text', '')}"
        if not is_cost_trigger(body):
            continue
        categories = route_article(body)
        if not categories:
            continue
        routed.append({
            "no": article.get("no", ""),
            "title": article.get("title", ""),
            "text": article.get("text", ""),
            "categories": categories,
        })
    return routed

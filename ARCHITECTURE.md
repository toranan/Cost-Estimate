# 비용추계 자동화 시스템 - Architecture

조례안 PDF 1장 → 비용추계서 자동 생성. RAG + TAG + KOSIS + Python 계산 엔진 결합 시스템.

---

## 한눈에 보기

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          [입력] 조례안 PDF                                │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 1: 조례안 해석                                                     │
│   • PDF → 텍스트 추출 (PyMuPDF)                                          │
│   • 조문 단위 분할 (LLM 1차, 정규식 폴백)                                 │
│   • 분야 자동 분류 (NABO 6+1 분야)                                       │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 2: 조문별 비용유발 판단 (병렬 처리)                                │
│   각 조문마다:                                                           │
│    ├─ 임베딩 생성 (OpenAI text-embedding-3-small, 1536d)                │
│    ├─ bill_cost_triggers 검색 (유사 조문)                                │
│    ├─ legal_reference 검색                                              │
│    └─ Gemini: cost_trigger / trigger_type / strength 판단               │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 3: 종합 판단 (NABO 공식 분류)                                     │
│   📚 목적별 RAG 검색 (분리):                                             │
│    ├─ classification_refs ← NABO Part I (분류 기준)                     │
│    ├─ methodology_refs   ← LEGAL_REF (방법론·법령)                      │
│    ├─ nabo_cases         ← NABO Part II (분야별 사례, 분야 매칭)         │
│    ├─ similar_estimates  ← 유사 비용추계서                              │
│    └─ tag_patterns       ← TAG items/variables/amounts                  │
│                                                                         │
│   🎯 verdict ∈ {추계서, 미첨부_1호, 미첨부_2호, 미첨부_3호, 미대상}      │
│                                                                         │
│   🚦 Python 금액 게이트:                                                 │
│    • 추계서 + 연 10억 미만 → 미첨부 1호 강제                            │
│    • 미첨부 1호 + 연 10억 이상 → 추계서 강제                            │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
                              [분기]
                ┌────────────────┴─────────────────┐
                ↓                                  ↓
        [미첨부/미대상]                       [추계서 필요]
                ↓                                  ↓
            사유서 생성                      STAGE 4 진행
                                                   ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 4: 추계서 항목 + 산식 도출                                        │
│   • 유사 추계서 + NABO Part II + TAG로 항목·산식 구조화                  │
│   • KOSIS 자동 매핑 (7개 표준 변수)                                      │
│   • variables_needed 추출                                               │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 5: Python 결정적 계산 (calculator.py)                            │
│   for item in cost_items:                                               │
│    ├─ 모든 변수 채워짐 → year_amounts 계산 (복리)                       │
│    ├─ 부족하지만 TAG 사례 있음 → median fallback + requires_review      │
│    └─ 둘 다 없음 → 차단 + missing_vars 명시                             │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  STAGE 6: QA 검증 게이트                                                │
│   ⚠️ 각 단계 신뢰도 점검 → workflow_issues + qaReport                    │
│   📋 누락 변수, 추정 사용, RAG 신뢰도 등 사용자에게 명시                  │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
                              [결과 반환]
```

---

## 데이터 파이프라인 (학습 데이터 수집)

```
┌──────────────────────────────────────────────────────────────────────┐
│  국회 의안정보시스템 (LIKMS / Open Assembly API)                       │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
            ┌─────────────────┴────────────────┐
            ↓                                  ↓
    [discover_cost_estimate_bills]    [build_assembly_rag_seed_fast]
       • HTML 스크래핑                  • ZIP 다운 + PDF 추출
       • 추계서/미첨부 발견              • PyMuPDF 텍스트화
       • discovery.json 저장             • 청킹 (max 2400자)
            ↓                                  ↓
                                       ┌──────┴──────┐
                                       ↓             ↓
                                [upload_seed]   [chunks.jsonl]
                                       ↓
                              ┌────────┴────────┐
                              ↓                 ↓
                       Supabase 업로드      [embed_chunks]
                       (assembly_chunks)   OpenAI 임베딩 (1536d)
                              ↓
                      Supabase embedding 업데이트
                              ↓
                       ┌──────┴────────────┐
                       ↓                   ↓
              [extract_tag_structures]   [ingest_legal_reference]
                Gemini 2.5 Flash         법령 PDF → chunks
                구조화 추출
                       ↓
                cost_estimate_*
                jsonl 생성
                       ↓
              [upload_tag_structures]
                       ↓
                Supabase TAG 테이블
                       ↓
              [derive_bill_cost_triggers]
                items의 trigger_ref 역매핑
                       ↓
              bill_cost_triggers 자동 적재
```

---

## Supabase 스키마

### RAG 벡터 검색 (43,964 chunks)

```sql
assembly_chunks
├─ chunk_id           PRIMARY KEY
├─ bill_id            → assembly_bills(bill_id)
├─ source             national_assembly | legal_reference
├─ document_type      cost_estimate | non_attachment_reason | legal_reference
├─ age                21 | 22
├─ committee
├─ content            텍스트
├─ embedding          vector(1536)  -- HNSW 인덱스
└─ ...

RPC: match_assembly_chunks(query_embedding, match_count, filter_source, filter_doc_type)
```

| 구분 | chunks | 임베딩 |
|------|--------|--------|
| 21대 추계서 | 8,312 | ✅ |
| 21대 미첨부 | 5,832 | ✅ |
| 22대 추계서 | 22,732 | ✅ |
| 22대 미첨부 | 6,563 | ✅ |
| legal_reference | **840** (3개 PDF) | ✅ |

**legal_reference 3개 PDF (목적별 분리):**
- `LEGAL_REF_COST_ESTIMATION` (525) — 비용추계 이해와 실제 (방법론)
- `NABO_2021_GUIDE_I` (51) — NABO 2021 Part I (분류·절차)
- `NABO_2021_GUIDE_II` (264) — NABO 2021 Part II (분야별 사례)

### TAG 구조화 (1,378 의안)

```sql
cost_estimate_structures  ─┐
   1,378건                 │
                           ├──→ cost_estimate_items
                           │       3,346건
                           │       ├─ trigger_ref ("제5조 제1항")
                           │       └─→ cost_estimate_variables  (9,805건)
                           │       └─→ cost_estimate_amounts     (17,867건)
                           │
                           └─ FK to assembly_bills
```

### 조문 학습 DB

```sql
bill_cost_triggers
├─ bill_id, article_no
├─ cost_trigger          true/false
├─ trigger_type          인건비/운영비/사업비/지원금/위탁비
├─ cost_items            jsonb (해당 조문이 유발한 비용 항목들)
└─ article_embedding     vector(1536) -- 유사 조문 검색용
```
**총 2,323건** (items의 trigger_ref 역매핑)

### KOSIS 매핑 후보

```sql
kosis_stat_candidates
└─ variable_key → KOSIS table mapping (수동 매핑 진행 중)
```

---

## NABO 공식 분류 체계 (verdict)

| verdict | 라벨 | 조건 |
|---------|------|------|
| `추계서` | 비용추계서 | 연 10억 이상 또는 한시 30억 이상 |
| `미첨부_1호` | 미첨부 1호 (비용 미미) | 연평균 10억 미만 또는 한시 30억 미만 |
| `미첨부_2호` | 미첨부 2호 (안보·기밀) | 국가안전보장·군사기밀 |
| `미첨부_3호` | 미첨부 3호 (기술적 곤란) | 선언적·권고적 또는 시행령 위임 |
| `미대상` | 미대상 (재정변화 없음) | 정의 조항, 명칭 변경 등 |

**Python 금액 게이트**가 Gemini 판단을 NABO 기준으로 자동 보정.

---

## NABO 분야 분류 (자동 감지)

```
보건복지   = 의료, 보육, 노인, 장애인, 한부모, 사회복지
산업농업   = 산업, 농업, 어업, 국토, 교통, 건설
교육과학   = 교육, 과학, 문화, 여성가족, 청소년
환경노동   = 환경, 에너지, 노동, 일자리
국방보훈   = 국방, 보훈, 법제사법
안전행정   = 안전, 재난, 행정, 자치
세입       = 조세, 지방세, 부담금
```
→ 분야 매칭된 **NABO Part II 사례**를 우선 검색.

---

## KOSIS 자동 조회 변수 (7개)

| 변수 | KOSIS 표 | 단위 |
|------|---------|------|
| 소비자물가상승률 | DT_1J22003 | % |
| 명목임금상승률 | DT_118N_LCE0001 | % |
| 공무원임금상승률 | 인사혁신처 고시 | % |
| 주민등록인구 | DT_1B04005N | 명 |
| 65세이상인구 | DT_1B04005N | 명 |
| 등록장애인수 | DT_110001_A045 | 명 |
| 기초생활수급자수 | DT_110001_A045 | 명 |

→ `variables_needed`에 정확히 이 이름으로 들어가면 시스템이 자동 조회.

---

## Python 계산 엔진 (calculator.py)

```python
# 결정적 산출 — Gemini가 계산하지 않음
def compute_year_estimates(estimate, tag_patterns, allow_estimated=True):
    """
    각 item에 대해:
      base_amount_thousand × (1 + growth_rate)^year
    
    base_amount_thousand 없으면:
      → TAG 유사사례 median으로 fallback (requires_review=True)
      → 둘 다 없으면 missing_vars 명시 + amount=null
    """
```

---

## 응답 구조

```json
{
  "billName": "...",
  "field": {
    "field": "보건복지",
    "confidence": 0.95,
    "reason": "공공임대주택·관리비 지원이 핵심"
  },
  "verdict": {
    "type": "추계서",
    "label": "비용추계서",
    "nabo_reason": "예상비용 연평균 10억 이상 → 추계서",
    "confidence": 0.90,
    "summary": "..."
  },
  "articles": [
    {
      "no": "제5조",
      "cost_trigger": true,
      "trigger_type": "지원금",
      "obligation_strength": "mandatory",
      "legal_refs": [...],
      "similar_refs": [...]
    }
  ],
  "estimate": {
    "calculation_status": "computed_by_python | estimated_by_tag | blocked_*",
    "items": [
      {
        "name": "공용관리비 지원",
        "category": "지원금",
        "formula": "단지수 × 단가 × 12",
        "trigger_ref": "제5조 제1항",
        "variables_needed": ["단지 수", "단가", "소비자물가상승률"],
        "kosis_lookups": [
          {
            "variable": "소비자물가상승률",
            "unit": "%",
            "source": "KOSIS DT_1J22003",
            "year_values": [{"year": "2024", "value": 2.32}, ...]
          }
        ],
        "calculation": {
          "base_amount_thousand": 12000000,
          "recurrence": "annual",
          "growth_variable": "소비자물가상승률"
        },
        "requires_review": false
      }
    ],
    "year_estimates": [
      {"year": 1, "amount_thousand": 338060000, "note": "Python 계산기 산출"}
    ]
  },
  "workflow": {
    "status": "ok | degraded | blocked",
    "issues": [...],
    "rag": {
      "legalReferenceCount": 4,
      "similarCostEstimateCount": 5,
      "avgCostEstimateSimilarity": 0.60
    },
    "tagPatternCount": 3
  },
  "qaReport": {
    "summary": "⚠️ 사용자 검토 권장",
    "issues": [
      {
        "level": "warn",
        "category": "통계청 자동조회 불가 변수 17개",
        "detail": "...",
        "action": "단지 수, 단가 등 직접 입력 필요"
      }
    ]
  },
  "references": {
    "similar_bills_cost_estimate": [...],
    "similar_bills_non_attachment": [...],
    "legal_references": [...]
  }
}
```

---

## 디렉토리 구조

```
backend/
├─ analyzer_v2.py            ← 메인 분석 엔진 (Stage 1~6)
├─ calculator.py             ← Python 결정적 계산
├─ kosis_lookup.py           ← KOSIS 통계 자동 조회 (7개 변수)
├─ form_renderer.py          ← 경기도/국회 양식 HTML 렌더링
├─ server.py                 ← /api/analyze_v2, /api/render
├─ supabase_schema.sql       ← 테이블 정의
└─ scripts/
   ├─ discover_cost_estimate_bills.py   ← 추계서 발견 (HTML)
   ├─ build_assembly_rag_seed_fast.py   ← 수집 + 청킹
   ├─ upload_assembly_seed_to_supabase.py ← Supabase 업로드
   ├─ embed_chunks.py                    ← OpenAI 임베딩
   ├─ extract_tag_structures.py          ← Gemini TAG 추출
   ├─ upload_tag_structures_to_supabase.py
   ├─ derive_bill_cost_triggers.py       ← items 역매핑
   ├─ ingest_legal_reference.py          ← 법령 PDF 적재
   └─ run_pipeline.py                    ← 전체 파이프라인

frontend/
└─ src/App.jsx               ← React UI
   ├─ VerdictCard            ← 판단 결과 + NABO 근거
   ├─ QaReport               ← 사용자 검토 필요 항목
   ├─ ArticlesView           ← 조문별 분석
   ├─ EstimateView           ← 추계서 + KOSIS 값
   ├─ SimilarCasesTable      ← 참고한 유사 사례
   ├─ FormView               ← 추계서 양식 (경기도/국회)
   └─ EvidenceView           ← RAG 근거

api/
└─ index.py                  ← Vercel serverless 진입점
```

---

## 기술 스택

| 영역 | 사용 |
|------|------|
| **LLM** | Gemini 2.5 Flash (분류·구조화 추출, 분야 분류) |
| **임베딩** | OpenAI text-embedding-3-small (1536d) |
| **벡터 DB** | Supabase + pgvector (HNSW 인덱스) |
| **계산** | Python (결정적, calculator.py) |
| **통계** | KOSIS Open API |
| **PDF** | PyMuPDF |
| **백엔드** | Python http.server (Vercel serverless) |
| **프론트** | React + Vite |
| **배포** | Vercel |

---

## 보장하는 것 / 보장하지 못하는 것

### ✅ 보장
- **결정적 계산** — 같은 입력 = 같은 출력
- **NABO 공식 분류** — 5분류 + 10억/30억 금액 게이트
- **출처 추적** — 모든 변수에 KOSIS/TAG/사용자입력 표시
- **누락 정직 표시** — 가정값 없이 missing_vars
- **법령 근거** — NABO + LEGAL_REF 인용
- **조문 누락 방지** — 전체 조문 처리 (`ANALYZE_MAX_ARTICLES` 기본 0)

### ⚠️ 한계
- KOSIS 외 통계 (시도청, 사업 데이터)는 사용자 입력 필요
- 새로운 유형 의안은 RAG 신뢰도 낮음 (QA report에 명시)
- 스캔 PDF는 OCR 미지원
- 법률 해석의 모호한 부분(예: "필요한 경우")은 LLM 해석

---

## 워크플로우 상태

| status | 의미 |
|--------|------|
| `ok` | 모든 단계 정상 |
| `degraded` | 일부 경고 (가정값 사용, 유사도 낮음 등) |
| `blocked` | 핵심 데이터 부족으로 결과 신뢰 불가 |

→ UI에서 색상 구분 (초록/노랑/빨강)

---

## 신뢰도 캡

| 조건 | confidence 상한 |
|------|--------------|
| workflow에 error 있음 | 0.55 |
| 법령 RAG 또는 유사 사례 또는 TAG 없음 | 0.70 |
| 평균 유사도 < 임계치 | 0.75 |

→ LLM이 100% 자신해도 데이터 부족하면 신뢰도 자동 하향.

<div align="center">

# 🏛️ ORCA — 법안 비용추계 자동화 시스템

**LLM의 추론과 결정적 계산을 분리한 TAG 아키텍처로,**
**국회예산정책처 표준 양식의 비용추계서를 자동 생성합니다.**

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=white)](https://react.dev/)
[![Gemini](https://img.shields.io/badge/Gemini-2.5_Pro-4285F4?logo=google&logoColor=white)](https://ai.google.dev/)
[![Supabase](https://img.shields.io/badge/Supabase-pgvector-3FCF8E?logo=supabase&logoColor=white)](https://supabase.com/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)

[데모 결과](#-검증-결과--간호법안-2126640) · [아키텍처](#-아키텍처) · [빠른 시작](#-빠른-시작) · [기술 스택](#-기술-스택)

</div>

---

## 💡 한 줄 요약

> **법안·조례안 PDF 한 장만 넣으면, 국회예산정책처(NABO) 표준 양식의 비용추계서가 자동으로 나옵니다.**
> 그리고 **모든 가정값(회의수당·인건비·물가상승률 등)이 어디서 왔는지 추계서 본문에 자동으로 박힙니다.**

---

## 🎯 왜 만들었나

| 기존 현실 | ORCA의 해결 |
|---|---|
| NABO 분석관이 추계서 한 건에 사안에 따라 **수일~수주** 소요 | PDF 업로드 → **수 분 내** 초안 생성 |
| 가정값 출처 추적 어려움 ("이 단가 어디서?") | 모든 가정값에 `evidence_trace` 자동 기록 |
| LLM에 통째로 맡기면 **숫자에서 환각** | LLM은 변수 추출만, **Python이 결정적 계산** |
| 지자체 조례안은 자체 추계 체계가 미비한 곳도 다수 | NABO 양식과 동일 구조로 즉시 적용 가능 |

---

## ✅ 검증 결과 — 실제 의안 8건, NABO 원본과 8건 모두 방향 일치

국회예산정책처(NABO) 원본 추계 문서(비용추계서 / 미첨부 사유서)와 블라인드 비교.
**학습 풀에 없는 신규 의안 5건 포함**, 산출/미첨부 양방향 모두 검증했습니다.

| 의안 | 학습 풀 | NABO 정답 | ORCA 출력 | 결과 |
|---|---|---|---|---|
| 간호법안 (2126640) | 적재 | 추계서 2,000만원 | 2,000만원 | ✅ **100%** (9개 조항 분류도 일치) |
| 각급법원 개정 (2213969) | 적재 | 추계서 **120.94억** | **120.94억** | ✅ **100%** |
| AI데이터센터법 (2217718) | 적재 | 추계서 3.156억 | 3.16억 | ✅ 99% |
| 친환경농어업법 (2214559) | **신규** | 추계서 1.2억 | 1.45억 | ✅ +21% (위원 수 가정 차이) |
| 중대범죄수사청법 (2217239) | **신규** | **미첨부 3호** | 미첨부 3호 | ✅ |
| 공소청 설치법 (2217240) | **신규** | **미첨부 3호** | 미첨부 3호 | ✅ |
| 국가교육위법 개정 (2125482) | **신규** | **미첨부 3호** | 미첨부 3호 | ✅ |
| 노동위원회법 개정 (2215589) | **신규** | **미첨부 3호** | 미첨부 3호 | ✅ (동일법 선례 자동 인용) |

→ **산출 케이스는 금액까지 정확히, 미첨부 케이스는 사유서까지 자동 생성.**
→ 미첨부 판단은 규칙이 아니라 **유사 미첨부 사례 5,800여 건과의 kNN 매칭**으로 결정되므로, 사례가 쌓일수록 정확도가 자동으로 올라가는 구조입니다.

> 🔬 이 결과에 도달하기까지의 **오답 진단 → 구조 개선 → 재검증 사이클 기록**은 [`docs/IMPROVEMENTS.md`](./docs/IMPROVEMENTS.md)에 정리되어 있습니다.

---

## 🏗️ 아키텍처

ORCA의 핵심은 **LLM과 결정적 계산을 분리한 6-Stage 파이프라인**입니다.

```
┌──────────────────────────────────────────────────────────────┐
│   📄 [입력] 법안·조례안 PDF                                    │
└──────────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────────┐
│  STAGE 1. 조문 결정적 추출                                     │
│   PyMuPDF → 텍스트 → 정규식 + 좌표 기반 신구조문대비표 파싱     │
└──────────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────────┐
│  STAGE 2. 조문별 비용유발 판단 (병렬)                          │
│   임베딩 → 유사 조문 검색 → Gemini 의무성·재량성 분류           │
└──────────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────────┐
│  STAGE 3. NABO 공식 분류 (verdict)                            │
│   추계서 ⏐ 미첨부 1·2·3호 ⏐ 미대상                            │
│   Python 금액 게이트가 LLM 판단을 자동 보정                    │
└──────────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────────┐
│  STAGE 4. 산식 + 가정값 도출 (3-Tier Anchored Inference)      │
│   ① 조문 추출 → ② 유사사례 통계 → ③ 정부 표준 교차 검증         │
│   모든 단계에 evidence_trace 자체 기록                         │
└──────────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────────┐
│  STAGE 5. 결정적 계산 (calculator.py)                         │
│   base × (1 + growth_rate)^year  ← LLM 환각 차단              │
│   KOSIS API로 물가·임금상승률 실시간 반영                       │
└──────────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────────┐
│  STAGE 6. QA 검증 + 추계서 자동 생성                           │
│   누락 변수 / 가정값 사용 / RAG 신뢰도 → qaReport               │
│   HTML · DOCX · HWPX 다중 포맷 출력                            │
└──────────────────────────────────────────────────────────────┘
```

> 더 자세한 데이터 파이프라인·DB 스키마·응답 구조는 [`ARCHITECTURE.md`](./ARCHITECTURE.md) 참고.

---

## ✨ 핵심 차별점

### 1. LLM ≠ 계산기

LLM은 **조문에서 변수 추출**만 합니다. 실제 곱셈·복리·연도별 누적은 Python 결정적 엔진(`backend/calculator.py`)이 처리합니다.
→ **같은 입력 = 같은 출력**이 보장되고, LLM 환각이 숫자에 닿을 수 없습니다.

### 2. 가정값 도출의 3-Tier Anchored Inference

```
조문 결정적 추출 (Tier 1)
        ↓ (조문에 없으면)
유사사례 통계 추론 (Tier 2) ──┐
        ↓ (RAG 풀에서)        │ 교차 검증 ±50%
정부 표준 앵커링 (Tier 3) ────┘
        ↓
채택값 + evidence_trace (samples, statistic, reference)
```

회의수당·인건비·물가 같은 정량 가정값을 **3단계 우선순위 + 외부 표준 교차 검증**으로 도출.
→ "이 200,000원, 어디서 왔어요?"에 추계서가 스스로 답합니다.

### 3. NABO 공식 분류 + Python 금액 게이트

```
verdict ∈ { 추계서 │ 미첨부 1호 │ 미첨부 2호 │ 미첨부 3호 │ 미대상 }
```

LLM이 "비용추계서"라 판단했더라도, **Python 금액 게이트**가 NABO 기준(연 10억/한시 30억)으로 verdict를 자동 보정. **법령에 명시된 분류 기준을 LLM 의견에 우선**합니다.

### 3-1. Dual-Channel kNN Verdict — 미첨부 판단도 데이터 기반

조문마다 임베딩 1개로 **두 채널을 동시 검색**해 유사도를 비교합니다.

```
조문 임베딩
    ├─ 채널 A: 유사 비용추계서 검색 (31,000+ 청크)
    └─ 채널 B: 유사 미첨부 사유서 검색 (5,800+ 청크)
              ↓
    B 우세 or 동일 법률의 미첨부 선례 존재 → 미첨부 신호
```

여기에 **NABO 존재 게이트**를 결합: 실제 NABO 판단 원칙(정답지 분석으로 도출)에 따라, **산출 가능한 조문이 1개라도 있으면 다수 조문이 미첨부성이어도 "일부추계"로 작성**합니다. 다수결이 아닌 존재 판정 — 대구경북특별시 특별법(재정수반요인 39개 중 29개가 추계 곤란)처럼 복잡한 의안에서 NABO와 동일한 행동을 재현하는 핵심 원칙입니다.

### 4. KOSIS Open API 실시간 연동

소비자물가상승률·임금상승률·인구·등록장애인 수 등 **7개 표준 변수**를 통계청 KOSIS Open API로 자동 조회. 매년 갱신되는 정부 통계가 추계 산식에 자동 반영됩니다.

---

## 📊 학습 데이터 풀

| 데이터 | 규모 | 용도 |
|---|---|---|
| 21·22대 국회 비용추계서 청크 | **43,964** | RAG 의미 검색 |
| TAG 구조화 의안 | **1,378** | 산식 패턴 학습 |
| 가정값 후보 | **1,356** | 단가·인원·횟수 통계 도출 |
| 조문 비용유발 트리거 | **2,323** | 유사 조문 매칭 |
| 법령·NABO 가이드 PDF 청크 | **840** | 방법론·분류 기준 |

모든 데이터는 **Supabase pgvector(HNSW 인덱스)**에 적재되어 의미 유사도 검색이 가능합니다.

---

## 🚀 빠른 시작

### 사전 준비

```bash
# Python 3.14, Node 18+, npm
git clone https://github.com/CSID-DGU/2026-1-CECD1-5-SSA-01.git
cd 2026-1-CECD1-5-SSA-01

# Python 의존성
pip install -r requirements.txt

# Frontend 의존성
cd frontend && npm install && cd ..
```

### 환경 변수 설정

`backend/.env.example`을 `backend/.env`로 복사한 뒤 키를 채웁니다.

```bash
cp backend/.env.example backend/.env
```

| 키 | 용도 | 필수 여부 |
|---|---|---|
| `GEMINI_API_KEY` | 조문 분석·분류 | 필수 |
| `OPENAI_API_KEY` | 임베딩 (text-embedding-3-small) | 필수 |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | RAG/TAG 검색 | 필수 |
| `OPEN_ASSEMBLY_API_KEY` | 의안 메타 조회 | 선택 |
| `AZURE_OPENAI_*` | OpenAI 임베딩 폴백 | 선택 |
| `BACKEND_PORT` | 백엔드 포트 (기본 8010) | 선택 |

### 실행

```bash
# 1) 백엔드 (별도 터미널)
python3 -m backend.server
# → http://localhost:8010

# 2) 프론트엔드 (별도 터미널)
cd frontend && npm run dev
# → http://localhost:5173
```

브라우저에서 `http://localhost:5173` 접속 → PDF 업로드 → 분석 결과 + 추계서 다운로드.

### 테스트

```bash
pip install pytest
python -m pytest backend/test_formula_engine.py -v
# → 18 passed (로컬 검증)
```

---

## 🛠️ 기술 스택

| 영역 | 사용 기술 |
|---|---|
| **LLM** | Google Gemini 2.5 Pro (조문 분석·분류·산식 추출) |
| **임베딩** | OpenAI `text-embedding-3-small` (1536d), Azure OpenAI 폴백 |
| **벡터 DB** | Supabase + pgvector (HNSW 인덱스) |
| **계산 엔진** | Python 결정적 계산 (`calculator.py`) |
| **외부 통계** | 통계청 KOSIS Open API, 국회 Open Assembly API |
| **PDF 처리** | PyMuPDF (`fitz`) + macOS PDFKit Swift 폴백 |
| **백엔드** | Python 3.14 `http.server` (ThreadingHTTPServer) |
| **프론트엔드** | React 19 + Vite 8 |
| **문서 생성** | HWPX 직접 조립 + DOCX |
| **배포** | Vercel (serverless) |

---

## 📁 디렉토리 구조

```
.
├─ backend/
│  ├─ analyzer_v2.py            # 메인 분석 엔진 (Stage 1~6)
│  ├─ calculator.py             # Python 결정적 계산
│  ├─ kosis_lookup.py           # KOSIS 통계 자동 조회
│  ├─ form_renderer.py          # 추계서 HTML 렌더링
│  ├─ assembly_case_policy.py   # NABO 정책 분류기
│  ├─ assembly_formula_engine.py # TAG 산식 엔진
│  ├─ assembly_assumptions.py   # 가정값 후보 검색
│  ├─ server.py                 # /api/health, analyze, analyze_v2, render, export/pdf, recompute
│  ├─ supabase_schema.sql       # DB 스키마
│  └─ scripts/                  # 데이터 수집·임베딩·TAG 추출 파이프라인
├─ frontend/
│  └─ src/App.jsx               # React UI (Verdict / QA / Articles / Estimate / Form)
├─ api/
│  └─ index.py                  # Vercel serverless 진입점
├─ ARCHITECTURE.md              # 전체 아키텍처 상세 문서
└─ README.md
```

---

## 🤝 보장하는 것 / 보장하지 못하는 것

### ✅ 보장

- **결정적 계산** — 같은 입력 = 같은 출력
- **NABO 공식 분류** — 5분류 + 10억/30억 금액 게이트
- **출처 추적** — 모든 가정값에 KOSIS·TAG·사용자입력 표시
- **누락 정직 표시** — 가정 없으면 `missing_vars` 명시
- **법령 근거** — NABO Guide + LEGAL_REF 자동 인용

### ⚠️ 한계

- KOSIS 외 통계(시도청, 사업 데이터)는 사용자 입력 필요
- 새로운 유형 의안은 RAG 신뢰도 낮음 (qaReport에 명시)
- 스캔 PDF는 OCR 미지원
- 법률 해석의 모호한 부분("필요한 경우" 등)은 LLM 해석

---

## 📄 라이선스

본 프로젝트는 **MIT 라이선스** 하에 배포됩니다. 자세한 내용은 [`LICENSE`](./LICENSE) 파일을 참고하세요.
동국대학교 컴퓨터공학과 캡스톤(2026-1-CECD1-5-SSA-01)의 일환으로 시작되었습니다.

---

<div align="center">

**🏛️ ORCA — Operational RAG-and-TAG for Cost Analysis**

> 국회예산정책처 표준 추계서를 자동으로, 검증 가능하게.

</div>

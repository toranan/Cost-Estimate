import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

const PIPELINE_STEPS = [
  { number: 1, title: '문서 구조 분석', tech: '본문과 개정 조문 식별' },
  { number: 2, title: '재정수반 조문 판단', tech: '법령 기준과 조문별 검토' },
  { number: 3, title: '유사 사례 및 기준값 확인', tech: '국회 추계서와 근거 문서 검색' },
  { number: 4, title: '비용 산출 및 추계서 작성', tech: '산식 계산과 문서 양식 생성' },
]

const VERDICT_META = {
  '추계서':    { label: '비용추계서 작성 대상', color: 'red', desc: '재정지출 또는 수입 변화가 예상되어 비용추계서를 작성합니다.' },
  '미첨부_1호': { label: '미첨부 1호', color: 'green', desc: '예상 비용이 첨부 기준보다 적어 미첨부 사유서를 작성합니다.' },
  '미첨부_2호': { label: '미첨부 2호', color: 'gray', desc: '국가안전보장 또는 군사기밀 사유로 추계서를 첨부하지 않습니다.' },
  '미첨부_3호': { label: '미첨부 3호', color: 'amber', desc: '시행계획 등이 확정되지 않아 기술적으로 추계하기 곤란합니다.' },
  '미대상':    { label: '비용추계 미대상', color: 'blue', desc: '새로운 재정지출 또는 수입 변화가 확인되지 않았습니다.' },
  // 기존 분류와의 하위 호환 (legacy)
  '추계필요': { label: '추계 필요', color: 'red', desc: '비용 발생' },
  '미첨부_A': { label: '비용 없음', color: 'green', desc: '비용 미수반' },
  '미첨부_B': { label: '추계 곤란', color: 'amber', desc: '기술적 곤란' },
  '미첨부_C': { label: '기존 예산 활용', color: 'blue', desc: '기존 예산 범위' },
}

const CALC_STATUS_TEXT = {
  computed_by_python: '확정된 기준값으로 계산했습니다.',
  computed_by_special_template: '국회 비용추계 기준과 확정된 전제값으로 산출했습니다.',
  computed_with_tag_structure: '공식 비용추계서 사례를 기준으로 산출했습니다.',
  computed_with_tag_estimates: '유사 비용추계서의 동일 산식 금액을 적용해 초안을 생성했습니다.',
  computed_with_evidence: '공식 비용추계 사례의 산식과 전제값 후보를 활용해 초안을 생성했습니다.',
  computed_partial_by_python: '계산 가능한 항목을 우선 산출하고 일부 항목은 검토 대상으로 남겼습니다.',
  estimated_by_tag: '유사 비용추계서 기반 초안입니다.',
  needs_external_data: '대상 규모·단가·실적 자료가 있으면 더 정밀하게 보정할 수 있습니다.',
  needs_policy_input: '사업 규모나 운영방식 전제를 보정하면 더 정밀하게 재계산할 수 있습니다.',
  blocked_missing_variables: '기본 가정값으로 초안을 구성했습니다.',
  blocked_no_structured_formula: '기본 산식 구조로 초안을 구성했습니다.',
  awaiting_user_input: '추가 전제를 보완하면 재계산할 수 있습니다.',
}

const TRIGGER_TYPE_COLOR = {
  '직접지원': 'red', '위탁대행': 'orange', '시설구축': 'amber',
  '조직설치': 'purple', '대상확대': 'pink', '의무부과': 'rose',
  '없음': 'gray',
}

const STRENGTH_LABEL = {
  mandatory: '의무', semi_mandatory: '준의무',
  discretionary: '재량', aspirational: '선언적',
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(r.result)
    r.onerror = () => reject(new Error('파일을 읽지 못했습니다.'))
    r.readAsDataURL(file)
  })
}

function asList(value) {
  if (Array.isArray(value)) return value
  if (value === null || value === undefined) return []
  if (typeof value === 'object') {
    return [value.reason || value.item || JSON.stringify(value)]
  }
  return [String(value)]
}

function cleanExtractedText(value) {
  return String(value || '')
    .replace(/\r/g, '')
    .replace(/([가-힣])\s*\n\s*([가-힣])/g, '$1$2')
    .replace(/[ \t]*\n[ \t]*/g, ' ')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

function evidenceModal(item, kind = 'bill') {
  const similarity = Math.round((item.similarity || 0) * 100)
  if (kind === 'legal') {
    return {
      title: '비용추계 기준 근거',
      sourceLabel: '법령 및 작성 기준',
      meta: `관련도 ${similarity}% · 근거 ID ${item.chunk_id?.slice(-12) || '-'}`,
      body: cleanExtractedText(item.content),
    }
  }
  return {
    title: item.bill_name || '유사 비용추계 사례',
    sourceLabel: `국회 의안 ${item.bill_no || '-'}`,
    meta: `관련도 ${similarity}%`,
    body: cleanExtractedText(item.content),
  }
}

function App() {
  const [file, setFile] = useState(null)
  const [isDragging, setIsDragging] = useState(false)
  const [isProcessing, setIsProcessing] = useState(false)
  const [currentStep, setCurrentStep] = useState(-1)
  const [result, setResult] = useState(null)
  const [activeTab, setActiveTab] = useState('estimate')
  const [expanded, setExpanded] = useState(null)
  const [modal, setModal] = useState(null)
  const [error, setError] = useState('')
  const [formType, setFormType] = useState(() =>
    localStorage.getItem('formType') || 'gyeonggi'
  )
  useEffect(() => {
    localStorage.setItem('formType', formType)
  }, [formType])
  const fileRef = useRef(null)

  useEffect(() => {
    if (!isProcessing) return
    const t = setInterval(() => setCurrentStep(p => (p < 3 ? p + 1 : p)), 2500)
    return () => clearInterval(t)
  }, [isProcessing])

  const handleDrop = useCallback((e) => {
    e.preventDefault()
    setIsDragging(false)
    const f = e.dataTransfer.files[0]
    if (f) { setFile(f); setError('') }
  }, [])

  const start = async () => {
    if (!file) return
    setIsProcessing(true); setResult(null); setError(''); setCurrentStep(0)
    try {
      const content = await fileToDataUrl(file)
      const res = await fetch(`${API_BASE}/api/analyze_v2`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: file.name,
          mimeType: file.type,
          content,
          formType,  // 'gyeonggi' | 'assembly' → 백엔드 분류 기준 분기
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || '분석 실패')
      setCurrentStep(4)
      setResult(data)
      setActiveTab('estimate')
    } catch (e) {
      setCurrentStep(-1)
      setError(e.message)
    } finally {
      setIsProcessing(false)
    }
  }

  const reset = () => {
    setFile(null); setResult(null); setError(''); setCurrentStep(-1)
    setExpanded(null); setModal(null)
  }

  return (
    <div className="app">
      <header className="header">
        <div className="header-logo">
          <div className="header-logo-icon">CE</div>
          <div>
            <h1>비용추계 자동화 시스템</h1>
            <span>의안 분석 및 비용추계서 작성</span>
          </div>
        </div>
        <div className="header-right">
          <div className="form-toggle">
            <span className="form-toggle-label">양식</span>
            <button
              className={`form-toggle-btn ${formType === 'gyeonggi' ? 'active' : ''}`}
              onClick={() => setFormType('gyeonggi')}
            >
              경기도
            </button>
            <button
              className={`form-toggle-btn ${formType === 'assembly' ? 'active' : ''}`}
              onClick={() => setFormType('assembly')}
            >
              국회
            </button>
          </div>
        </div>
      </header>

      <main className="main">
        {!result && (
          <>
            <section className="hero">
              <h2>
                의안 PDF에서 비용추계서까지
              </h2>
              <p>
                조문별 재정수반 여부를 검토하고 산식, 전제값, 판단 근거를 함께 제공합니다.
              </p>
            </section>

            <section className="upload-section">
              <div
                className={`upload-zone ${isDragging ? 'dragging' : ''}`}
                onDragOver={(e) => { e.preventDefault(); setIsDragging(true) }}
                onDragLeave={() => setIsDragging(false)}
                onDrop={handleDrop}
                onClick={() => fileRef.current?.click()}
              >
                <input ref={fileRef} type="file" accept=".pdf"
                  onChange={(e) => { setFile(e.target.files[0]); setError('') }}
                  style={{ display: 'none' }} />
                <div className="upload-icon">PDF</div>
                <h3>조례안 PDF를 끌어다 놓거나 클릭하세요</h3>
                <p>텍스트가 포함된 의안 원문 PDF를 지원합니다.</p>
                <div className="upload-formats"><span>PDF</span></div>
              </div>

              {file && (
                <div className="file-selected animate-fade-in">
                  <span className="file-selected-icon">PDF</span>
                  <div className="file-selected-info">
                    <div className="name">{file.name}</div>
                    <div className="size">{(file.size / 1024).toFixed(1)} KB</div>
                  </div>
                  <button className="file-selected-remove"
                    onClick={(e) => { e.stopPropagation(); reset() }}>✕</button>
                </div>
              )}

              {error && <div className="status-banner error">{error}</div>}

              <button className="start-btn" disabled={!file || isProcessing} onClick={start}>
                {isProcessing ? '분석 중...' : '비용추계 분석 시작'}
              </button>
            </section>
          </>
        )}

        {currentStep >= 0 && !result && (
          <section className="pipeline-section animate-fade-in">
            <div className="pipeline-header">
              <h3>비용추계 분석 진행</h3>
            </div>
            <div className="pipeline-steps">
              {PIPELINE_STEPS.map((step, idx) => (
                <div key={step.number} className={`pipeline-step ${
                  currentStep === idx ? 'active' : currentStep > idx ? 'completed' : ''
                }`}>
                  <div className="pipeline-step-number">
                    {currentStep > idx ? '✓' : step.number}
                  </div>
                  <div style={{ flex: 1 }}>
                    <div className="pipeline-step-title">{step.title}</div>
                    <div className="pipeline-step-tech">{step.tech}</div>
                  </div>
                  {currentStep === idx && isProcessing && (
                    <div className="pipeline-step-spinner">
                      <div className="spinner" />처리 중
                    </div>
                  )}
                </div>
              ))}
            </div>
          </section>
        )}

        {result && (
          <section className="animate-fade-in">
            <div className="result-hero">
              <button className="back-btn" onClick={reset}>← 새 조례안 분석</button>
              <h2 className="result-title">{result.billName}</h2>
              <div className="result-meta">
                <span>분석 시각 {result.generatedAt}</span>
                <span>소요 시간 {result.elapsedSec}s</span>
                <span>검토 조문 {result.totalArticles}개</span>
              </div>
            </div>

            <VerdictCard verdict={result.verdict} field={result.field} />
            {result.qaReport && <QaReport report={result.qaReport} />}

            <div className="tab-bar">
              <button className={`tab ${activeTab === 'estimate' ? 'active' : ''}`}
                onClick={() => setActiveTab('estimate')}>
                비용추계서
              </button>
              <button className={`tab ${activeTab === 'articles' ? 'active' : ''}`}
                onClick={() => setActiveTab('articles')}>
                조문 분석 <span className="tab-count">{(result.articles || []).length}</span>
              </button>
              <button className={`tab ${activeTab === 'form' ? 'active' : ''}`}
                onClick={() => setActiveTab('form')}>
                문서 출력 <span className="tab-count">{formType === 'gyeonggi' ? '경기도' : '국회'}</span>
              </button>
              <button className={`tab ${activeTab === 'evidence' ? 'active' : ''}`}
                onClick={() => setActiveTab('evidence')}>
                판단 근거
              </button>
            </div>

            {activeTab === 'articles' && (
              <ArticlesView
                articles={result.articles || []}
                expanded={expanded}
                setExpanded={setExpanded}
                openModal={setModal}
              />
            )}
            {activeTab === 'estimate' && (
              <EstimateView
                result={result}
                estimate={result.estimate}
                nonAttachment={result.nonAttachment}
                refs={result.references}
                formType={formType}
                onResult={setResult}
                openModal={setModal}
              />
            )}
            {activeTab === 'form' && (
              <FormView result={result} formType={formType} setFormType={setFormType} />
            )}
            {activeTab === 'evidence' && (
              <EvidenceView result={result} refs={result.references} openModal={setModal} />
            )}
          </section>
        )}
      </main>

      {modal && <Modal data={modal} onClose={() => setModal(null)} />}
    </div>
  )
}

function QaReport({ report }) {
  return null
}

function VariableEditorPanel({
  estimate,
  drafts,
  setDraft,
  recompute,
  isRecomputing,
  recomputeError,
}) {
  const items = estimate?.items || []
  if (!items.length) return null
  return (
    <div className="variable-editor-panel">
      <div className="variable-editor-head">
        <div>
          <div className="variable-editor-title">값 변경</div>
          <div className="variable-editor-desc">초안은 자동 생성됩니다. 값이 맞지 않을 때만 보정하세요.</div>
        </div>
        <button
          type="button"
          className="recompute-all-btn"
          disabled={isRecomputing}
          onClick={recompute}
        >
          {isRecomputing ? '재계산 중' : '수정값 반영'}
        </button>
      </div>
      <div className="variable-editor-table">
        {items.map((item, i) => {
          const calc = item.calculation || {}
          const variables = item.assumption_strategy?.length
            ? item.assumption_strategy.map(row => row.variable)
            : asList(item.variables_needed)
          return (
            <div key={i} className="variable-editor-row">
              <div className="variable-editor-item">
                <div className="variable-editor-name">{item.name}</div>
                <div className="variable-editor-formula">{item.selected_formula?.formula || item.formula || '-'}</div>
                <div className="variable-editor-vars">
                  {variables.slice(0, 5).map((v, j) => (
                    <span key={j} className="var-chip">{String(v)}</span>
                  ))}
                </div>
              </div>
              <label>
                <span>연간 기준금액</span>
                <input
                  type="number"
                  inputMode="decimal"
                  placeholder={calc.base_amount_thousand != null ? String(calc.base_amount_thousand) : '천원'}
                  value={drafts[i]?.base_amount_thousand || ''}
                  onChange={(e) => setDraft(i, 'base_amount_thousand', e.target.value)}
                />
              </label>
              <label>
                <span>단가</span>
                <input
                  type="number"
                  inputMode="decimal"
                  placeholder="천원"
                  value={drafts[i]?.unit_cost || ''}
                  onChange={(e) => setDraft(i, 'unit_cost', e.target.value)}
                />
              </label>
              <label>
                <span>대상 수</span>
                <input
                  type="number"
                  inputMode="decimal"
                  placeholder="명/개소"
                  value={drafts[i]?.target || ''}
                  onChange={(e) => setDraft(i, 'target', e.target.value)}
                />
              </label>
              <label>
                <span>반복</span>
                <select
                  value={drafts[i]?.recurrence || calc.recurrence || 'annual'}
                  onChange={(e) => setDraft(i, 'recurrence', e.target.value)}
                >
                  <option value="annual">매년</option>
                  <option value="one_time">1회성</option>
                </select>
              </label>
            </div>
          )
        })}
      </div>
      {recomputeError && <div className="recompute-error">{recomputeError}</div>}
    </div>
  )
}

function VerdictCard({ verdict, field }) {
  const meta = VERDICT_META[verdict.type] || {
    label: verdict.label, color: 'gray', desc: ''
  }
  return (
    <div className={`verdict-card verdict-${meta.color}`}>
      <div className="verdict-status">
        <span className="verdict-status-dot" />
        분석 결과
      </div>
      <div className="verdict-body">
        <div className="verdict-label">{meta.label}</div>
        <div className="verdict-desc">{meta.desc}</div>
        {field && field.field && (
          <div className="verdict-field">분야 <b>{field.field}</b></div>
        )}
        <div className="verdict-summary">{verdict.summary}</div>
        {verdict.basis?.reasons?.length > 0 ? (
          <div className="verdict-basis">
            <div className="verdict-basis-title">{verdict.basis.title || '판단 근거'}</div>
            {verdict.basis.reasons.map((row, idx) => (
              <div key={idx} className="verdict-basis-row">
                <span>{row.label}</span>
                <p>{row.text}</p>
              </div>
            ))}
          </div>
        ) : verdict.nabo_reason && (
          <div className="verdict-nabo">
            <span className="verdict-nabo-label">판단 근거</span>
            <div className="verdict-nabo-text">{verdict.nabo_reason}</div>
          </div>
        )}
      </div>
    </div>
  )
}

function ArticlesView({ articles, expanded, setExpanded, openModal }) {
  const triggered = articles.filter(a => a.cost_trigger).length
  return (
    <div className="animate-fade-in">
      <div className="articles-stats">
        <div className="stat-box red">
          <div className="stat-num">{triggered}</div>
          <div className="stat-label">비용 유발 조문</div>
        </div>
        <div className="stat-box gray">
          <div className="stat-num">{articles.length - triggered}</div>
          <div className="stat-label">비용 없음</div>
        </div>
      </div>

      <div className="articles-list">
        {articles.map((art, i) => (
          <ArticleRow
            key={i}
            art={art}
            isExpanded={expanded === i}
            onToggle={() => setExpanded(expanded === i ? null : i)}
            openModal={openModal}
          />
        ))}
      </div>
    </div>
  )
}

function ArticleRow({ art, isExpanded, onToggle, openModal }) {
  const tColor = TRIGGER_TYPE_COLOR[art.trigger_type] || 'gray'
  return (
    <div
      className={`article-row ${art.cost_trigger ? 'triggered' : 'safe'} ${isExpanded ? 'expanded' : ''}`}
      onClick={onToggle}
    >
      <div className="article-row-main">
        <div className="article-row-no">
          <span className={`article-status-dot ${art.cost_trigger ? 'cost' : 'none'}`} />
          {art.no}
        </div>
        <div className="article-row-meta">
          {art.cost_trigger ? (
            <>
              <span className={`badge badge-${tColor}`}>{art.trigger_type}</span>
              <span className="strength-text">
                {STRENGTH_LABEL[art.obligation_strength] || art.obligation_strength}
              </span>
            </>
          ) : (
            <span className="badge badge-gray">비용 없음</span>
          )}
        </div>
        {!isExpanded && (
          <div className="article-row-reason">{art.reason}</div>
        )}
        <div className="article-row-chevron">›</div>
      </div>

      {isExpanded && (
        <div className="article-detail">
          <div className="detail-block">
            <div className="detail-label">판단 근거</div>
            <div className="article-text-box">{cleanExtractedText(art.reason)}</div>
          </div>

          <div className="detail-block">
            <div className="detail-label">관련 조문</div>
            <div className="article-text-box article-source-text">{cleanExtractedText(art.text)}</div>
          </div>

          {art.legal_refs && art.legal_refs.length > 0 && (
            <div className="detail-block">
              <div className="detail-label">비용추계 기준 근거</div>
              <div className="ref-list">
                {art.legal_refs.map((r, i) => (
                  <div
                    key={i}
                    className="ref-card"
                    onClick={(e) => {
                      e.stopPropagation()
                      openModal(evidenceModal(r, 'legal'))
                    }}
                  >
                    <div className="ref-card-top">
                      <span className="ref-card-title">법령 및 작성 기준</span>
                      <span className="ref-card-sim">관련도 {Math.round((r.similarity || 0) * 100)}%</span>
                    </div>
                    <div className="ref-card-preview">{cleanExtractedText(r.content).slice(0, 150)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {art.similar_refs && art.similar_refs.length > 0 && (
            <div className="detail-block">
              <div className="detail-label">유사 비용추계 사례</div>
              <div className="ref-list">
                {art.similar_refs.map((r, i) => (
                  <div
                    key={i}
                    className="ref-card"
                    onClick={(e) => {
                      e.stopPropagation()
                      openModal(evidenceModal(r, 'bill'))
                    }}
                  >
                    <div className="ref-card-top">
                      <span className="ref-card-title">{r.bill_no} · {r.bill_name?.slice(0, 36) || ''}</span>
                      <span className="ref-card-sim">관련도 {Math.round((r.similarity || 0) * 100)}%</span>
                    </div>
                    <div className="ref-card-preview">{cleanExtractedText(r.content).slice(0, 150)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function SimilarCasesTable({ items, openModal }) {
  if (!items || items.length === 0) return null
  return (
    <div className="similar-cases">
      <div className="similar-cases-label">참고한 유사 사례</div>
      <table className="similar-cases-table">
        <thead>
          <tr>
            <th>의안번호</th>
            <th>법률명</th>
            <th>유사도</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.slice(0, 5).map((it, i) => (
            <tr key={i}>
              <td className="bill-no">{it.bill_no || '—'}</td>
              <td className="bill-name">{(it.bill_name || '').slice(0, 38)}</td>
              <td className="bill-sim">{Math.round((it.similarity || 0) * 100)}%</td>
              <td>
                <button
                  className="sim-view-btn"
                  onClick={() => openModal(evidenceModal(it, 'bill'))}
                >
                  보기
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function EstimateView({ result, estimate, nonAttachment, refs, formType, onResult, openModal }) {
  const similarCE = refs?.similar_bills_cost_estimate || []
  const similarNA = refs?.similar_bills_non_attachment || []
  const [drafts, setDrafts] = useState({})
  const [isRecomputing, setIsRecomputing] = useState(false)
  const [recomputeError, setRecomputeError] = useState('')

  const setDraft = (index, key, value) => {
    setDrafts(prev => ({
      ...prev,
      [index]: {
        ...(prev[index] || {}),
        [key]: value,
      },
    }))
  }

  const toNumber = value => {
    if (value === '' || value === null || value === undefined) return null
    const parsed = Number(String(value).replace(/,/g, ''))
    return Number.isFinite(parsed) ? parsed : null
  }

  const recompute = async () => {
    if (!estimate) return
    const userInputs = (estimate.items || []).map((item, index) => {
      const draft = drafts[index] || {}
      const baseAmount = toNumber(draft.base_amount_thousand)
      const unitCost = toNumber(draft.unit_cost)
      const target = toNumber(draft.target)
      const calc = item.calculation || {}
      const input = {
        item_index: index,
        recurrence: draft.recurrence || calc.recurrence || 'annual',
        start_year: toNumber(draft.start_year) || calc.start_year || 1,
        end_year: toNumber(draft.end_year) || calc.end_year || 5,
        growth_variable: draft.growth_variable ?? calc.growth_variable ?? null,
      }
      if (baseAmount !== null) {
        input.base_amount_thousand = baseAmount
      } else if (unitCost !== null || target !== null) {
        input.variables = {
          unit_cost: unitCost || 0,
          target: target || 1,
        }
      } else {
        return null
      }
      return input
    }).filter(Boolean)

    if (userInputs.length === 0) {
      setRecomputeError('변경한 값이 없습니다.')
      return
    }

    setIsRecomputing(true)
    setRecomputeError('')
    try {
      const res = await fetch(`${API_BASE}/api/recompute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          result,
          estimate,
          userInputs,
          formType,
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || '재계산 실패')
      onResult(data)
    } catch (e) {
      setRecomputeError(e.message)
    } finally {
      setIsRecomputing(false)
    }
  }

  if (nonAttachment) {
    return (
      <div className="animate-fade-in">
        <div className="non-attach-card">
          <h3>비용추계서 미첨부 사유서</h3>
          <div className="na-type-badge">{nonAttachment.type}유형</div>
          <p className="na-reason">{nonAttachment.reason_text}</p>
        </div>
        <SimilarCasesTable
          items={similarNA.length ? similarNA : similarCE}
          openModal={openModal}
        />
      </div>
    )
  }
  if (!estimate) {
    return <div className="empty">생성된 추계서가 없습니다.</div>
  }
  const yearRows = estimate.year_estimates || []
  const totalAmount = estimate.total_amount_thousand ?? yearRows.reduce((sum, row) => (
    row.amount_thousand != null ? sum + Number(row.amount_thousand) : sum
  ), 0)
  const averageAmount = estimate.average_amount_thousand ?? (
    yearRows.length ? Math.round(totalAmount / yearRows.length) : 0
  )
  return (
    <div className="estimate-view animate-fade-in">
      <div className="section-heading">
        <div>
          <h3>비용추계서</h3>
          <p>추계 결과와 산출 근거를 한 화면에서 확인합니다.</p>
        </div>
      </div>
      <div className="estimate-summary">
        <div className="summary-main">
          <span>총 추가재정소요</span>
          <strong>{(totalAmount / 1000).toLocaleString()}백만원</strong>
        </div>
        <div className="summary-sub">
          <span>연평균</span>
          <strong>{(averageAmount / 1000).toLocaleString()}백만원</strong>
        </div>
        {estimate.template_label && (
          <div className="summary-source">
            <span>적용 구조</span>
            <strong>{estimate.template_label}</strong>
          </div>
        )}
      </div>
      {yearRows.length > 0 && (
        <div className="year-grid primary">
          {yearRows.map((y, i) => (
            <div key={i} className="year-card">
              <div className="year-label">{y.year_label || `${y.year}차년도`}</div>
              <div className="year-amount">
                {y.amount_thousand !== null && y.amount_thousand !== undefined
                  ? `${(y.amount_thousand / 1000).toLocaleString()}백만원`
                  : '—'}
              </div>
            </div>
          ))}
        </div>
      )}
      {estimate.calculation_status && (
        <div className={`calc-status ${
          estimate.calculation_status.startsWith('computed')
            ? 'ok'
            : estimate.calculation_status.startsWith('estimated')
              ? 'estimated'
              : 'blocked'
        }`}>
          <span className="calc-status-label">산출 상태</span>
          <span>{CALC_STATUS_TEXT[estimate.calculation_status] || '초안을 생성했습니다.'}</span>
        </div>
      )}
      {estimate.estimation_status?.reason && (
        <div className={`calc-status ${estimate.estimation_status.blocking ? 'blocked' : 'ok'}`}>
          <span className="calc-status-label">산출 근거</span>
          <span>{estimate.estimation_status.reason}</span>
        </div>
      )}
      <div className="estimate-items">
        {(estimate.items || []).map((item, i) => (
          <div key={i} className="estimate-item-card">
            <div className="estimate-item-header">
              <span className="item-order">{i + 1}</span>
              <div>
                <div className="item-name">{item.name}</div>
                <div className="item-category">{item.category} · 근거 {item.trigger_ref}</div>
              </div>
            </div>
            <div className="estimate-formula">
              <span className="formula-label">산식</span>
              <code>{item.formula}</code>
            </div>
            {item.year_amounts_thousand?.length > 0 && (
              <div className="item-year-strip">
                {item.year_amounts_thousand.map((amount, idx) => (
                  <span key={idx}>{idx + 1}차 {(Number(amount || 0) / 1000).toLocaleString()}백만원</span>
                ))}
              </div>
            )}
            {(item.selected_formula || item.formula_template || item.reference_unit_costs || item.assumption_candidates || item.kosis_lookups || item.tag_structure_evidence) && (
              <details className="item-detail">
                <summary>산출 근거 자세히</summary>
                {item.selected_formula && (
                  <div className="selected-formula-block">
                    <div className="selected-formula-head">
                      <span>{item.selected_formula.label || '산식 선택 근거'}</span>
                      {item.selected_formula.confidence && <span>{Math.round((item.selected_formula.confidence || 0) * 100)}%</span>}
                    </div>
                    <code>{item.selected_formula.formula || item.formula || '-'}</code>
                    {item.selected_formula.basis && (
                      <div className="formula-template-note">{item.selected_formula.basis}</div>
                    )}
                  </div>
                )}
                {item.tag_structure_evidence && (
                  <div className="selected-formula-block">
                    <div className="selected-formula-head">
                      <span>적용 근거 문서</span>
                      <span>{Math.round((item.tag_structure_evidence.similarity || 0) * 100)}%</span>
                    </div>
                    <div className="formula-template-note">
                      {item.tag_structure_evidence.bill_no} {item.tag_structure_evidence.bill_name}
                    </div>
                  </div>
                )}
                {item.formula_template && (
                  <div className="formula-template-block">
                <div className="formula-template-head">
                  <span className="formula-template-label">{item.formula_template.label}</span>
                  <span className="formula-template-confidence">
                    신뢰도 {Math.round((item.formula_template.confidence || 0) * 100)}%
                  </span>
                </div>
                <code>{item.formula_template.standard_formula}</code>
                <div className="formula-template-vars">
                  {asList(item.formula_template.variables).map((v, j) => (
                    <span key={j} className="var-chip">{String(v)}</span>
                  ))}
                </div>
                <div className="formula-template-note">{item.formula_template.notes}</div>
                {item.formula_template.tag_formula_evidence?.length > 0 && (
                  <button
                    type="button"
                    className="evidence-mini-btn"
                    onClick={() => openModal({
                      title: `${item.formula_template.label} TAG 근거`,
                      meta: item.formula_template.source || 'TAG 산식 패턴',
                      body: item.formula_template.tag_formula_evidence.map((e, idx) =>
                        `${idx + 1}. ${e.bill_no || ''} ${e.bill_name || ''}\n` +
                        `항목: [${e.item_category || '-'}] ${e.item_name || '-'}\n` +
                        `산식: ${e.formula_text || '-'}\n` +
                        `점수: ${Math.round((e.score || 0) * 100)}`
                      ).join('\n\n'),
                    })}
                  >
                    TAG 산식 근거
                  </button>
                )}
              </div>
                )}
                {(item.reference_unit_costs || (item.reference_unit_cost ? [item.reference_unit_cost] : [])).length > 0 && (
                  <div className="ref-cost-block">
                <span className="ref-cost-label">
                  {formType === 'assembly' ? '추천 단가 후보' : '국회 단가 참고값'}
                </span>
                <div className="ref-cost-list">
                  {(item.reference_unit_costs || [item.reference_unit_cost]).slice(0, 3).map((ref, refIdx) => (
                    <div key={refIdx} className="ref-cost-row">
                      <div className="ref-cost-main">
                        <span className="ref-cost-rank">{refIdx + 1}</span>
                        <div>
                          <div className="ref-cost-body">
                            <b>{Number(ref.value).toLocaleString()}{ref.unit}</b>
                            <span className="ref-cost-src"> · {ref.variable_name || '단가'} · 점수 {Math.min(100, Math.round((ref.score || 0) * 100))}</span>
                          </div>
                          <div className="ref-cost-src">{ref.ref_item} ({ref.source})</div>
                        </div>
                      </div>
                      <button
                        type="button"
                        className="use-ref-btn compact"
                        onClick={() => setDraft(i, 'unit_cost', String(ref.value))}
                      >
                        사용
                      </button>
                    </div>
                  ))}
                </div>
                <div className="ref-cost-caveat">{item.reference_unit_cost?.caveat}</div>
              </div>
                )}
                {item.assumption_candidates && item.assumption_candidates.length > 0 && (
                  <div className="assumption-candidates-block">
                <span className="assumption-candidates-label">국회 기준값 후보</span>
                <div className="assumption-candidates-list">
                  {item.assumption_candidates.slice(0, 5).map((candidate, idx) => (
                    <div key={idx} className="assumption-candidate-row">
                      <div className="assumption-candidate-main">
                        <span className="assumption-candidate-rank">{idx + 1}</span>
                        <div>
                          <div className="assumption-candidate-value">
                            <b>{candidate.label || candidate.variable_name}</b>
                            <span>
                              {typeof candidate.value === 'number'
                                ? candidate.value.toLocaleString()
                                : candidate.value} {candidate.unit || ''}
                            </span>
                          </div>
                          <div className="assumption-candidate-meta">
                            {candidate.year || '연도 미상'} · 반복 {candidate.repeat_count || 1}건 · {candidate.bill_no} {candidate.bill_name}
                          </div>
                        </div>
                      </div>
                      <button
                        type="button"
                        className="evidence-mini-btn"
                        onClick={() => openModal({
                          title: `${candidate.label || candidate.variable_name} 후보 근거`,
                          meta: `${candidate.bill_no || ''} ${candidate.bill_name || ''}`,
                          body:
                            `값: ${candidate.value?.toLocaleString?.() || candidate.value} ${candidate.unit || ''}\n` +
                            `연도: ${candidate.year || '-'}\n` +
                            `항목: ${candidate.item_name || '-'}\n` +
                            `반복: ${candidate.repeat_count || 1}건\n\n` +
                            `${candidate.source_text || '근거 문장이 없습니다.'}`,
                        })}
                      >
                        근거
                      </button>
                    </div>
                  ))}
                </div>
              </div>
                )}
                {item.kosis_lookups && item.kosis_lookups.length > 0 && (
                  <div className="kosis-block">
                <div className="kosis-label">KOSIS 조회값</div>
                {item.kosis_lookups.map((k, j) => (
                  <div key={j} className="kosis-row">
                    <div className="kosis-name">
                      {k.variable} <span className="kosis-source">({k.source})</span>
                    </div>
                    <div className="kosis-values">
                      {asList(k.year_values).map((yv, idx) => (
                        <span key={idx} className="kosis-year-value">
                          <b>{yv.year || '-'}</b>: {typeof yv.value === 'number'
                            ? (yv.value > 1000 ? yv.value.toLocaleString() : yv.value)
                            : yv.value} {k.unit}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
                )}
              </details>
            )}
            {item.requires_review && (
              <div className="review-note">
                <span>{item.review_reason || '가정값 기반 초안입니다.'}</span>
                {item.evidence_basis && (
                  <button
                    className="evidence-mini-btn"
                    onClick={() => openModal({
                      title: `${item.name} 추정 근거`,
                      meta: item.evidence_basis.label || '유사 비용추계서 기반',
                      body: (item.evidence_basis.amount_candidates || []).map((c, idx) =>
                        `${idx + 1}. ${c.bill_no || ''} ${c.bill_name || ''}\n` +
                        `항목: [${c.category || '-'}] ${c.name || '-'}\n` +
                        `금액: ${Number(c.amount_thousand || 0).toLocaleString()}천원 / 점수 ${Math.round((c.score || 0) * 100)}%\n` +
                        `산식: ${c.formula || '-'}`
                      ).join('\n\n') || '표시할 근거가 없습니다.',
                    })}
                  >
                    근거 보기
                  </button>
                )}
                {item.analogy_evidence && (
                  <button
                    className="evidence-mini-btn"
                    onClick={() => openModal({
                      title: `${item.name} 유사사례 근거`,
                      meta: `${item.analogy_evidence.bill_no || ''} ${item.analogy_evidence.bill_name || ''}`,
                      body:
                        `기준 항목: ${item.analogy_evidence.item_name || '-'}\n` +
                        `근거 조문: ${item.analogy_evidence.trigger_ref || '-'}\n` +
                        `적용 방식: ${item.analogy_evidence.application || '-'}\n` +
                        `구조 유사도: ${Math.round((item.analogy_evidence.score || 0) * 100)}%`,
                    })}
                  >
                    유사사례 근거
                  </button>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
      <details className="estimate-tools">
        <summary>값 변경 및 재계산</summary>
        <VariableEditorPanel
          estimate={estimate}
          drafts={drafts}
          setDraft={setDraft}
          recompute={recompute}
          isRecomputing={isRecomputing}
          recomputeError={recomputeError}
        />
      </details>
      {similarCE.length > 0 && (
        <details className="estimate-tools">
          <summary>유사 비용추계서</summary>
          <SimilarCasesTable items={similarCE} openModal={openModal} />
        </details>
      )}
    </div>
  )
}

function EvidenceView({ result, refs, openModal }) {
  const hasReferenceItems = Boolean(
    refs?.similar_bills_cost_estimate?.length ||
    refs?.similar_bills_non_attachment?.length ||
    refs?.legal_references?.length
  )
  return (
    <div className="animate-fade-in">
      <div className="section-heading">
        <div>
          <h3>판단 근거</h3>
          <p>작성 대상 판단, 발생 비용, 참고 문서를 확인합니다.</p>
        </div>
      </div>
      <DecisionBasisSection result={result} openModal={openModal} />
      <EvidenceSection title="유사 비용추계서"
        items={refs?.similar_bills_cost_estimate || []}
        openModal={openModal} kind="bill" />
      <EvidenceSection title="유사 미첨부 사유서"
        items={refs?.similar_bills_non_attachment || []}
        openModal={openModal} kind="bill" />
      <EvidenceSection title="법령 및 작성 기준"
        items={refs?.legal_references || []}
        openModal={openModal} kind="legal" />
      {!hasReferenceItems && (
        <div className="evidence-empty">
          외부 RAG 근거가 비어 있어도 위의 작성 대상 판단근거와 공식 추계 사례 기준으로 결과를 구성했습니다.
        </div>
      )}
    </div>
  )
}

function DecisionBasisSection({ result, openModal }) {
  const verdict = result?.verdict || {}
  const basisRows = verdict.basis?.reasons || []
  const estimate = result?.estimate || {}
  const source = estimate.tag_structure_source
  if (!basisRows.length && !verdict.summary && !verdict.nabo_reason && !source) return null
  return (
    <div className="decision-basis-section">
      <div className="decision-basis-head">
        <div>
          <h4>{verdict.basis?.title || '작성 대상 판단 근거'}</h4>
          <p>왜 비용추계서 작성 대상인지와 어떤 자료를 근거로 삼았는지 정리했습니다.</p>
        </div>
        {source && (
          <button
            type="button"
            className="evidence-mini-btn"
            onClick={() => openModal({
              title: '참고 공식 비용추계서',
              meta: `국회 의안 ${source.bill_no || '-'}`,
              body:
                `${source.bill_name || '-'}\n` +
                `구조 유사도: ${Math.round((source.similarity || 0) * 100)}%\n\n` +
                `이 문서의 비용항목과 연도별 금액 구조를 현재 의안의 추계 초안 구성에 사용했습니다.`,
            })}
          >
            근거 보기
          </button>
        )}
      </div>
      <div className="decision-basis-grid">
        {basisRows.length > 0 ? basisRows.map((row, idx) => (
          <div key={idx} className="decision-basis-row">
            <span>{row.label}</span>
            <p>{row.text}</p>
          </div>
        )) : (
          <>
            {verdict.summary && (
              <div className="decision-basis-row">
                <span>판단 사유</span>
                <p>{verdict.summary}</p>
              </div>
            )}
            {verdict.nabo_reason && (
              <div className="decision-basis-row">
                <span>근거</span>
                <p>{verdict.nabo_reason}</p>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function EvidenceSection({ title, items, openModal, kind }) {
  if (!items.length) return null
  return (
    <div className="evidence-section">
      <h4>{title}</h4>
      <div className="evidence-cards">
        {items.map((it, i) => (
          <div
            key={i}
            className="ref-card"
            onClick={() => openModal(evidenceModal(it, kind))}
          >
            <div className="ref-card-top">
              <span className="ref-card-title">
                {kind === 'bill'
                  ? `${it.bill_no} · ${(it.bill_name || '').slice(0, 45)}`
                  : '비용추계 법령 및 작성 기준'}
              </span>
              <span className="ref-card-sim">관련도 {Math.round((it.similarity || 0) * 100)}%</span>
            </div>
            <div className="ref-card-preview">{cleanExtractedText(it.content).slice(0, 180)}</div>
            <div className="ref-card-action">근거 상세보기</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function FormView({ result, formType, setFormType }) {
  const renderKey = `${formType}:${result?.generatedAt || ''}:${result?.billName || ''}`
  const [rendered, setRendered] = useState({ key: '', html: '', err: '' })
  const [downloadingPdf, setDownloadingPdf] = useState(false)
  const html = rendered.key === renderKey ? rendered.html : ''
  const err = rendered.key === renderKey ? rendered.err : ''
  const loading = rendered.key !== renderKey

  useEffect(() => {
    let alive = true
    fetch(`${API_BASE}/api/render`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ result, format: formType }),
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(await r.text())
        return r.text()
      })
      .then((text) => {
        if (alive) setRendered({ key: renderKey, html: text, err: '' })
      })
      .catch((e) => {
        if (alive) setRendered({ key: renderKey, html: '', err: e.message })
      })
    return () => { alive = false }
  }, [result, formType, renderKey])

  const handlePrint = () => {
    const w = window.open('', '_blank')
    if (!w) return
    w.document.write(html)
    w.document.close()
    setTimeout(() => w.print(), 500)
  }

  const handleDownloadHtml = () => {
    const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `비용추계서_${formType === 'gyeonggi' ? '경기도' : '국회'}_${Date.now()}.html`
    a.click()
    URL.revokeObjectURL(url)
  }

  const handleDownloadPdf = async () => {
    setDownloadingPdf(true)
    try {
      const response = await fetch(`${API_BASE}/api/export/pdf`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ result, format: formType }),
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}))
        throw new Error(payload.error || 'PDF 생성에 실패했습니다.')
      }
      const blob = await response.blob()
      const disposition = response.headers.get('Content-Disposition') || ''
      const matchedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)
      const fallbackName = `${result?.billName || '비용추계서'}_비용추계서_${formType === 'assembly' ? '국회' : '경기도'}.pdf`
      const downloadName = matchedName ? decodeURIComponent(matchedName[1]) : fallbackName
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = downloadName
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      window.alert(e.message)
    } finally {
      setDownloadingPdf(false)
    }
  }

  return (
    <div className="form-view animate-fade-in">
      <div className="form-toolbar">
        <div className="form-pickr">
          <button
            className={`pickr-btn ${formType === 'gyeonggi' ? 'active' : ''}`}
            onClick={() => setFormType('gyeonggi')}
          >
            경기도 양식
          </button>
          <button
            className={`pickr-btn ${formType === 'assembly' ? 'active' : ''}`}
            onClick={() => setFormType('assembly')}
          >
            국회 양식
          </button>
        </div>
        <div className="form-actions">
          <button className="form-btn" onClick={handleDownloadPdf} disabled={loading || downloadingPdf}>
            {downloadingPdf ? 'PDF 생성 중...' : 'PDF 다운로드'}
          </button>
          <button className="form-btn" onClick={handlePrint}>인쇄</button>
          <button className="form-btn" onClick={handleDownloadHtml}>HTML 다운로드</button>
        </div>
      </div>

      <div className="form-preview-wrap">
        {loading && <div className="empty">양식 렌더링 중...</div>}
        {err && <div className="status-banner error">{err}</div>}
        {!loading && !err && html && (
          <iframe
            className="form-preview-frame"
            srcDoc={html}
            title="비용추계서 미리보기"
          />
        )}
      </div>
    </div>
  )
}

function Modal({ data, onClose }) {
  useEffect(() => {
    const onEsc = (e) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onEsc)
    return () => document.removeEventListener('keydown', onEsc)
  }, [onClose])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            {data.sourceLabel && <div className="modal-source">{data.sourceLabel}</div>}
            <h3>{data.title}</h3>
          </div>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>
        {data.meta && <div className="modal-meta">{data.meta}</div>}
        <div className="modal-content-label">근거 원문</div>
        <div className="modal-body">
          {data.sourceLabel ? cleanExtractedText(data.body) : data.body}
        </div>
      </div>
    </div>
  )
}

export default App

import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

const PIPELINE_STEPS = [
  { number: 1, title: 'PDF 파싱 + 조문 분할', tech: 'PyMuPDF' },
  { number: 2, title: '조문별 비용유발 판단', tech: 'Gemini + 법령 RAG' },
  { number: 3, title: '유사 사례 검색',       tech: 'pgvector ANN' },
  { number: 4, title: '추계서 자동 생성',     tech: 'Gemini + TAG' },
]

const VERDICT_META = {
  '추계필요': { label: '추계 필요',         color: 'red',   emoji: '💰', desc: '비용 발생 — 추계서 작성이 필요합니다' },
  '미첨부_A': { label: '비용 없음 (A유형)', color: 'green', emoji: '⚪', desc: '정의·명칭 변경 등 비용 미수반' },
  '미첨부_B': { label: '추계 곤란 (B유형)', color: 'amber', emoji: '🟡', desc: '대상자 산정 불가 등 기술적 곤란' },
  '미첨부_C': { label: '예산 흡수 (C유형)', color: 'blue',  emoji: '🔵', desc: '기존 예산 범위 내 집행 가능' },
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

function App() {
  const [file, setFile] = useState(null)
  const [isDragging, setIsDragging] = useState(false)
  const [isProcessing, setIsProcessing] = useState(false)
  const [currentStep, setCurrentStep] = useState(-1)
  const [result, setResult] = useState(null)
  const [activeTab, setActiveTab] = useState('articles')
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
        body: JSON.stringify({ filename: file.name, mimeType: file.type, content }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || '분석 실패')
      setCurrentStep(4)
      setResult(data)
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
          <div className="header-logo-icon">⚖️</div>
          <div>
            <h1>비용추계 자동화 시스템</h1>
            <span>RAG + TAG 기반 조례안 분석</span>
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
                <span className="gradient-text">조례안 PDF</span> 한 장이면<br />
                <span className="gradient-text">비용추계서</span>가 자동으로
              </h2>
              <p>
                과거 의안 819건 + 비용추계 법령 PDF + AI가 함께 분석합니다.<br />
                조문별 판단 · 종합 결론 · 산식 생성 · 근거 추적까지.
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
                <div className="upload-icon">📑</div>
                <h3>조례안 PDF를 끌어다 놓거나 클릭하세요</h3>
                <p>입법예고문, 조례안 원문 — 어떤 형식이든 OK</p>
                <div className="upload-formats"><span>PDF</span></div>
              </div>

              {file && (
                <div className="file-selected animate-fade-in">
                  <span className="file-selected-icon">📄</span>
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
                {isProcessing ? '분석 중...' : '비용추계 분석 시작 →'}
              </button>
            </section>
          </>
        )}

        {currentStep >= 0 && !result && (
          <section className="pipeline-section animate-fade-in">
            <div className="pipeline-header">
              <span>⚙️</span>
              <h3>AI가 4단계로 분석 중</h3>
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
                <span>📅 {result.generatedAt}</span>
                <span>⚡ {result.elapsedSec}s</span>
                <span>📋 {result.totalArticles}개 조문 분석</span>
              </div>
            </div>

            <VerdictCard verdict={result.verdict} />

            <div className="tab-bar">
              <button className={`tab ${activeTab === 'articles' ? 'active' : ''}`}
                onClick={() => setActiveTab('articles')}>
                📋 조문별 분석 <span className="tab-count">{result.articles.length}</span>
              </button>
              <button className={`tab ${activeTab === 'estimate' ? 'active' : ''}`}
                onClick={() => setActiveTab('estimate')}>
                💰 추계서 / 사유서
              </button>
              <button className={`tab ${activeTab === 'form' ? 'active' : ''}`}
                onClick={() => setActiveTab('form')}>
                📄 추계서 양식 <span className="tab-count">{formType === 'gyeonggi' ? '경기도' : '국회'}</span>
              </button>
              <button className={`tab ${activeTab === 'evidence' ? 'active' : ''}`}
                onClick={() => setActiveTab('evidence')}>
                📚 RAG 근거
              </button>
            </div>

            {activeTab === 'articles' && (
              <ArticlesView
                articles={result.articles}
                expanded={expanded}
                setExpanded={setExpanded}
                openModal={setModal}
              />
            )}
            {activeTab === 'estimate' && (
              <EstimateView
                estimate={result.estimate}
                nonAttachment={result.nonAttachment}
              />
            )}
            {activeTab === 'form' && (
              <FormView result={result} formType={formType} setFormType={setFormType} />
            )}
            {activeTab === 'evidence' && (
              <EvidenceView refs={result.references} openModal={setModal} />
            )}
          </section>
        )}
      </main>

      {modal && <Modal data={modal} onClose={() => setModal(null)} />}
    </div>
  )
}

function VerdictCard({ verdict }) {
  const meta = VERDICT_META[verdict.type] || {
    label: verdict.label, color: 'gray', emoji: '❓', desc: ''
  }
  const confPct = Math.round((verdict.confidence || 0) * 100)
  return (
    <div className={`verdict-card verdict-${meta.color}`}>
      <div className="verdict-emoji">{meta.emoji}</div>
      <div className="verdict-body">
        <div className="verdict-label">{meta.label}</div>
        <div className="verdict-desc">{meta.desc}</div>
        <div className="verdict-summary">{verdict.summary}</div>
        <div className="verdict-confidence">
          <span>AI 신뢰도</span>
          <div className="confidence-bar">
            <div className="confidence-fill" style={{ width: `${confPct}%` }} />
          </div>
          <span className="confidence-pct">{confPct}%</span>
        </div>
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
          {art.cost_trigger ? '🔴' : '⚪'} {art.no}
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
            <div className="detail-label">📄 AI 판단 근거</div>
            <div className="article-text-box">{art.reason}</div>
          </div>

          <div className="detail-block">
            <div className="detail-label">📜 조문 원문</div>
            <div className="article-text-box">{art.text}</div>
          </div>

          {art.legal_refs && art.legal_refs.length > 0 && (
            <div className="detail-block">
              <div className="detail-label">⚖️ 비용추계와 이해 (법령 PDF) 인용</div>
              <div className="ref-list">
                {art.legal_refs.map((r, i) => (
                  <div
                    key={i}
                    className="ref-card"
                    onClick={(e) => {
                      e.stopPropagation()
                      openModal({
                        title: '비용추계 이해 (법령 PDF)',
                        meta: `청크 ${r.chunk_id?.slice(-12) || ''} · 유사도 ${Math.round((r.similarity || 0) * 100)}%`,
                        body: r.content,
                      })
                    }}
                  >
                    <div className="ref-card-top">
                      <span className="ref-card-title">📘 법령 PDF · {r.chunk_id?.slice(-8) || ''}</span>
                      <span className="ref-card-sim">{Math.round((r.similarity || 0) * 100)}%</span>
                    </div>
                    <div className="ref-card-preview">{r.content?.slice(0, 100)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {art.similar_refs && art.similar_refs.length > 0 && (
            <div className="detail-block">
              <div className="detail-label">📚 유사 의안 추계서 사례</div>
              <div className="ref-list">
                {art.similar_refs.map((r, i) => (
                  <div
                    key={i}
                    className="ref-card"
                    onClick={(e) => {
                      e.stopPropagation()
                      openModal({
                        title: `${r.bill_no} ${r.bill_name || ''}`,
                        meta: `유사도 ${Math.round((r.similarity || 0) * 100)}%`,
                        body: r.content,
                      })
                    }}
                  >
                    <div className="ref-card-top">
                      <span className="ref-card-title">📋 {r.bill_no} · {r.bill_name?.slice(0, 25) || ''}</span>
                      <span className="ref-card-sim">{Math.round((r.similarity || 0) * 100)}%</span>
                    </div>
                    <div className="ref-card-preview">{r.content?.slice(0, 100)}</div>
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

function EstimateView({ estimate, nonAttachment }) {
  if (nonAttachment) {
    return (
      <div className="non-attach-card animate-fade-in">
        <h3>📋 비용추계서 미첨부 사유서</h3>
        <div className="na-type-badge">{nonAttachment.type}유형</div>
        <p className="na-reason">{nonAttachment.reason_text}</p>
      </div>
    )
  }
  if (!estimate) {
    return <div className="empty">생성된 추계서가 없습니다.</div>
  }
  return (
    <div className="estimate-view animate-fade-in">
      <h3>💰 자동 생성 비용추계서</h3>
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
            {item.variables_needed && (
              <div className="estimate-variables">
                <span className="vars-label">필요 변수</span>
                <div className="vars-list">
                  {item.variables_needed.map((v, j) => (
                    <span key={j} className="var-chip">{v}</span>
                  ))}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {estimate.year_estimates && estimate.year_estimates.length > 0 && (
        <div className="year-grid">
          {estimate.year_estimates.map((y, i) => (
            <div key={i} className="year-card">
              <div className="year-label">{y.year}차년도</div>
              <div className="year-amount">
                {y.amount_thousand ? `${(y.amount_thousand / 1000).toLocaleString()}백만원` : '—'}
              </div>
              {y.note && <div className="year-note">{y.note}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function EvidenceView({ refs, openModal }) {
  return (
    <div className="animate-fade-in">
      <EvidenceSection title="📋 유사 비용추계서 사례"
        items={refs.similar_bills_cost_estimate || []}
        openModal={openModal} kind="bill" />
      <EvidenceSection title="📄 유사 미첨부 사유서"
        items={refs.similar_bills_non_attachment || []}
        openModal={openModal} kind="bill" />
      <EvidenceSection title="⚖️ 비용추계와 이해 (법령 PDF) 인용"
        items={refs.legal_references || []}
        openModal={openModal} kind="legal" />
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
            onClick={() => openModal(kind === 'bill' ? {
              title: `${it.bill_no} ${it.bill_name || ''}`,
              meta: `유사도 ${Math.round((it.similarity || 0) * 100)}%`,
              body: it.content,
            } : {
              title: '비용추계 이해 (법령 PDF)',
              meta: `청크 ${it.chunk_id?.slice(-12) || ''} · 유사도 ${Math.round((it.similarity || 0) * 100)}%`,
              body: it.content,
            })}
          >
            <div className="ref-card-top">
              <span className="ref-card-title">
                {kind === 'bill'
                  ? `📋 ${it.bill_no} · ${(it.bill_name || '').slice(0, 35)}`
                  : `📘 법령 PDF · ${it.chunk_id?.slice(-12) || ''}`}
              </span>
              <span className="ref-card-sim">{Math.round((it.similarity || 0) * 100)}%</span>
            </div>
            <div className="ref-card-preview">{(it.content || '').slice(0, 120)}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function FormView({ result, formType, setFormType }) {
  const [html, setHtml] = useState('')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    let alive = true
    setLoading(true); setErr('')
    fetch(`${API_BASE}/api/render`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ result, format: formType }),
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(await r.text())
        return r.text()
      })
      .then((text) => { if (alive) setHtml(text) })
      .catch((e) => { if (alive) setErr(e.message) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [result, formType])

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

  return (
    <div className="form-view animate-fade-in">
      <div className="form-toolbar">
        <div className="form-pickr">
          <button
            className={`pickr-btn ${formType === 'gyeonggi' ? 'active' : ''}`}
            onClick={() => setFormType('gyeonggi')}
          >
            🟦 경기도 별지 제1호
          </button>
          <button
            className={`pickr-btn ${formType === 'assembly' ? 'active' : ''}`}
            onClick={() => setFormType('assembly')}
          >
            🟥 국회 별지 제2호
          </button>
        </div>
        <div className="form-actions">
          <button className="form-btn" onClick={handlePrint}>🖨️ 인쇄 / PDF</button>
          <button className="form-btn" onClick={handleDownloadHtml}>📥 HTML 다운로드</button>
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
          <h3>{data.title}</h3>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>
        {data.meta && <div className="modal-meta">{data.meta}</div>}
        <div className="modal-body">{data.body}</div>
      </div>
    </div>
  )
}

export default App

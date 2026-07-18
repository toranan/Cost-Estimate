# ORCA Frontend

React와 Vite로 구현한 법안 비용추계 검토 UI입니다.

## 역할

- PDF와 국회·지자체 양식 선택
- 분석 API 호출과 로딩·오류 상태 관리
- 조문별 재정수반 판단, 근거 문서, 추계 항목 표시
- 사용자 전제값 수정 후 재계산 요청
- HTML 미리보기와 PDF 다운로드

## 데이터 흐름

```text
PDF 선택
→ FileReader로 Data URL 변환
→ POST /api/analyze_v2
→ JSON 결과를 React state에 저장
→ 결과·근거·문서 탭 렌더링
→ 전제값 수정 시 POST /api/recompute
```

## 로컬 실행

```bash
cp .env.example .env.local
npm ci
npm run dev
```

`VITE_API_BASE_URL`은 로컬 Python 백엔드 주소입니다.

## 검증 명령

```bash
npm run lint
npm run build
```

## 현재 기술 부채

- `App.jsx`가 여러 화면과 API 상태를 함께 관리하는 큰 컴포넌트임
- JavaScript API 응답에 런타임 스키마 검증이 없음
- 분석 요청 취소, timeout, 재시도 정책이 없음
- 진행 표시는 실제 서버 이벤트가 아니라 클라이언트 단계 표시임

제품화 시 업로드, 결과, 전제값 편집, 문서 미리보기를 컴포넌트와 custom hook으로 분리하고 TypeScript와 런타임 검증을 도입할 계획입니다.

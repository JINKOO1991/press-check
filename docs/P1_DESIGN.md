# P1 설계 계약 — 접근 범위와 권리 장부

작성 2026-10-08. 근거: (개발 중 문서, 공개본에 미포함) P1·P2, `SOURCE_ACCESS_AUDIT.md`, `COURT_AND_RESEARCH_PLAN.md`, `source-seeds.json`, `court-research-seeds.json`, (개발 중 문서, 공개본에 미포함), `docs/DECISIONS.md`(UDEC-1~5), `docs/REUSE_DECISIONS.md`. 충돌 시 문서가 이 계약보다 우선한다.

## 목표와 종료 기준(P1)

전체 목록과 첨부 인벤토리를 만든다. 목록 페이지 수, 문서 수, 유형별·연도별 분포, 접근 실패와 권리 보류 목록을 남긴다. 원문 다운로드는 P2에서 하지만, 첨부 URL·크기(HEAD)까지는 P1에서 확인한다. '모든 대법원 판결' 같은 검증할 수 없는 완료 표현은 쓰지 않는다.

## 배치

- 코드: `plugins/press-dispute-review/engine/press_review/ingestion/`. 수집은 운영 CLI(`python -m press_review.ingestion ...`)로만 실행하고 MCP 도구로 노출하지 않는다.
- 데이터: `config.py`의 데이터 디렉터리(기본 `~/.local/share/press-dispute-review`) 아래 `corpus/`. 저장소 안에는 쓰지 않는다.
  - `corpus/manifest.sqlite`: 수집 장부(아래 표)
  - `corpus/http_cache/<host>/<sha256(url)[:16]>.{body,meta.json}`: 응답 캐시
  - `corpus/inventory/<source_family>.json`: 사람이 볼 인벤토리 내보내기
- 테스트: `tests/ingestion/`. 네트워크를 쓰지 않는다(저장된 HTML 조각 fixture는 공개 페이지에서 잘라 쓰되 30줄 이내, 개인정보 없는 부분만).

## 공용 HTTP 계층 `ingestion/http.py`

- 허용 호스트 목록(코드 상수): www.pac.or.kr, pac.or.kr, www.iendc.go.kr, www.law.go.kr, open.law.go.kr. 그 밖은 거부한다. 네이버·KCI는 넣지 않는다(robots 금지). daum은 정책 확인 전까지 넣지 않는다.
- https만 허용한다. 법제처 DRF가 http만 되면 http는 law.go.kr DRF 경로에서만 예외로 허용하고, OC를 담은 URL은 로그·캐시 메타·예외에 `[MASKED]`로 남긴다.
- 사설 IP·loopback·자격증명 포함 URL은 차단한다. 리다이렉트도 같은 검사를 거친다.
- 호스트별 최소 간격 2초, 동시성 1. 429·5xx·timeout이면 지수 백오프(최대 4회), 그 뒤 실패 장부에 기록한다.
- robots.txt는 호스트별로 하루 1회 받아 원문 해시와 함께 장부에 기록한다. Disallow 경로는 요청하지 않는다.
- User-Agent: `press-dispute-review/0.1 (+research; non-commercial index)`.
- 캐시 적중이면 네트워크를 쓰지 않는다(`--refresh`로 무시).
- HTTP 200이어도 오류 페이지일 수 있다(IENDC bare 주소 사례). 어댑터가 본문 표지로 성공을 판정한다.

## 수집 장부 `ingestion/manifest.py` (SQLite, 표준 라이브러리 sqlite3)

| 테이블 | 핵심 열 |
|---|---|
| `sources` | source_id(PK), source_family, title, entry_url, enabled, checked_at, notes |
| `list_pages` | source_id, page_no, url, fetched_at, http_status, item_count, body_sha256, ok |
| `documents` | doc_id(PK, `<family>:<기관 고유키>`), source_id, canonical_url, title, publication_date, decision_date, year, doc_type, detail_url, discovered_at, last_seen_at, status(`discovered`/`fetched`/`extracted`/`structured`/`verified`/`indexed`/`failed`/`withheld`) |
| `attachments` | att_id(PK, `<doc_id>#<n>`), doc_id, url, label, kind(full/chapter/table/other), content_length, content_type, head_checked_at, raw_sha256, raw_path, status |
| `rights` | doc_id 또는 source_id, read, store_raw, create_derivatives, external_api, redistribute_raw, redistribute_derivatives, fine_tune(값은 source-record.schema enum), rights_basis_json, review_status |
| `failures` | 대상(url·doc_id), stage, error_class, message(비밀값 제거), at, retry_count |
| `robots` | host, fetched_at, http_status, body_sha256, purpose_status |
| `api_usage` | P2 DeepSeek용. provider, model, price_snapshot_json, prompt_tokens, cache_hit_tokens, completion_tokens, cost_usd, at, job_id |

모든 쓰기는 트랜잭션 단위로 한다. 같은 doc_id 재발견은 `last_seen_at`만 갱신한다(멱등). 목록에서 사라진 문서는 지우지 않는다.

## 권리 기록 규칙(UDEC-5)

PAC·IENDC·법제처 판례: read·store_raw·create_derivatives·external_api = `allowed`, redistribute_raw·redistribute_derivatives·fine_tune = `permission_required`. rights_basis에 `{scope: "사용자 결정 UDEC-5", review_status: "pending"}`와 근거 URL(저작권법 제7조·제35조의5, IENDC 저작권정책)을 넣는다. 네이버는 전부 `forbidden`(AI 학습·RAG 목적, robots 근거), collection_enabled=false. daum·KCI·논문은 `unknown`.

## 어댑터 범위

| 어댑터 | 열거 대상 | 성공 판정 |
|---|---|---|
| `pac.py` | 사례집 목록(p=207, cate=MA04) 전 페이지, 선거기사심의백서 목록(p=209, cate=MA08) 전 페이지, 연구 간행물(p=221)과 PAC 내 시정권고·심의기준 등 관련 간행물 메뉴(공식 내비게이션에서 발견되는 것). 각 상세의 전체 PDF·장별 PDF 링크와 HEAD 크기 | 목록 항목 수가 화면 표시 총수와 같다. 항목마다 상세 1회 |
| `iendc.py` | `board.do?menuPos=45` 전 페이지(관찰 23페이지, 227건), 각 상세의 첨부 PDF 주소(PDFObject·모바일 링크), 등록일·제목 | 목록 합계가 화면 총 게시물 수와 같다. bare 주소 오류 HTML을 성공으로 세지 않는다 |
| `lawapi.py` | 법제처 DRF 판례 목록(`target=prec`, 대법원 `org=400201`), 본문검색 사전(COURT_AND_RESEARCH_PLAN §2 검색어) × 선고일 구간. 최저 선고일 탐색, 사전별 건수, 판례일련번호·사건번호·선고일·사건명·법원명. 본문(lawService)은 P2 | 같은 질의의 totalCnt와 수집 건수가 같다. API 오류 응답 본문 검사 |

판례는 언론 관련성을 확정하지 않는다. 질의어·적중 사전을 기록하고 `relevance: candidate`로 둔다.

## 보고 산출물

- `docs/coverage/P1_inventory.md`: 소스별 목록 페이지 수, 문서 수, 첨부 수·총 바이트, 연도 분포, 실패·보류 건수, 확인 시각. 수치는 manifest.sqlite 질의로 생성(스크립트).
- `docs/coverage/rights_ledger.json`: 소스·문서 단위 권리 장부 내보내기.
- `docs/outreach/`: PAC·IENDC 이용 문의 초안(발송하지 않음).

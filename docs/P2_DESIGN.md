# P2 설계 계약 — 재개 가능한 수집·추출·구조화

작성 2026-10-08. 근거: (개발 중 문서, 공개본에 미포함) P2, `ARCHITECTURE.md` §5·§9·§10, `docs/P1_DESIGN.md`, `docs/DECISIONS.md` UDEC-3·UDEC-5. 충돌 시 이 문서보다 근거 문서가 우선한다.

## 종료 기준(인수인계 P2 그대로)

재시작하면 완료 파일은 다시 처리하지 않고 실패 파일만 재개한다. 목록→파일→페이지→사건→chunk 계보가 연결된다. 누락 페이지·OCR 오류·깨진 다운로드·스키마 실패를 숨기지 않는다. 그다음 권한이 확인된 공개 자료를 과거부터 최신까지 넓히며 커버리지를 갱신한다.

## 단계와 상태

`documents.status`/`attachments.status`: discovered → fetched → extracted → structured → verified → indexed. 실패는 failed(재시도 가능), 격리는 quarantined(사람 검수 필요). 단계마다 입력 해시와 단계 버전(`extractor_version`, `structurer_version`)을 기록한다. 입력 해시 또는 단계 버전이 바뀐 경우에만 그 단계부터 다시 처리한다. 구조화의 재처리 규칙은 코드 한 곳(`ingestion/structure.py`)에 있다: 모델에 보내는 요청이 바뀌면(프롬프트 파일 `version:`·내용, 모델, thinking, 창 크기, `CODE_VERSION`) 작업 키가 바뀌어 창을 다시 보낸다(API 비용 발생). 응답을 사건 record로 만드는 결정적 조립·검증 규칙(`structure_records`)이 바뀌면 `ASSEMBLY_VERSION`을 올린다. 이 값은 저장하는 `structurer_version`(`…|asm=<값>`)에는 들어가고 작업 키에는 들어가지 않으므로, 이미 받은 응답을 API 호출 없이 다시 조립한다.

## 1. 다운로드 `ingestion/fetch.py`

- 대상: rights.store_raw = allowed 이고 status = discovered 인 첨부. 저장 위치는 `<data>/corpus/raw/<family>/<안정 ID>.<ext>`이다. 파일명은 짧은 안정 ID로 하고 원 파일명은 메타데이터로 둔다(Windows 경로 길이).
- 임시 파일에 받은 뒤 해시를 계산하고 원자적으로 이동한다. 크기가 HEAD와 다르거나 매직 바이트가 선언과 다르면 failed로 둔다(kind_of 재사용).
- 같은 해시의 파일은 한 번만 저장하고 첨부끼리 연결한다(중복 제거).
- PAC 전체 PDF와 장별 PDF가 모두 있으면 둘 다 받는다. 추출은 장별을 우선한다. 장별 합계 쪽수가 전체 쪽수보다 적으면 빠진 쪽을 전체에서 추출하고 그 사실을 기록한다.

## 2. 추출 `ingestion/extract/`

- PDF: 페이지 단위 `pdftotext -layout -f N -l N`(기존 코드를 고쳐 이식: 페이지 경계 유지). 페이지 품질 지표(한글 비율, 글자 수, 깨진 문자 비율)가 임계 미만이면 그 페이지만 `pdftoppm` → `tesseract -l kor+eng`로 OCR한다(기존 코드를 이식하되 12쪽 상한 제거). OCR 페이지는 method=ocr, 신뢰도와 함께 저장한다. 숫자·사건번호·부정어가 들어 있는 OCR 줄은 `needs_review` 표시를 단다.
- HTML(법제처 판례 본문 등): 기존 코드를 고쳐 이식(표 셀 구분자, header/footer 손실 방지).
- HWP/HWPX: kordoc CLI로 추출한다(사용자 지시 2026-10-08, UDEC-6). kordoc 4.18.3, MIT, https://github.com/chrisryugj/kordoc. 호출은 `kordoc <file> --format json`이고 쪽 범위는 `-p`로 준다. 바이너리 경로는 env `KORDOC_BIN`에서 받고, 없으면 PATH의 `kordoc`을 쓴다. 개인 경로를 코드에 넣지 않는다. 한컴 저장본은 실제 쪽 번호이고, 조판 캐시가 없으면 섹션 근사다. 근사 locator는 method=`kordoc_section_approx`로 표시하고 실제 쪽 번호라고 적지 않는다. kordoc이 없으면 failed(`no_hwp_extractor`)로 기록하고 숨기지 않는다. kordoc은 수집·운영 쪽 외부 도구일 뿐이다. 플러그인 런타임 의존성이 아니다.
- 표가 많은 PDF(IENDC 심의결과 등): 게이트에서 `pdftotext -layout`과 `kordoc --format json`(표 감지)을 같은 표본으로 비교한다. 행 누락·중복 제목 반복이 적은 쪽을 그 소스의 추출기로 정하고, 비교 결과를 기록한다. kordoc 내장 OCR(`--ocr`, 모델 자동 다운로드)은 tesseract와의 비교가 끝나기 전까지 쓰지 않는다.
- `pages` 테이블: page_id, att_id, pdf_page_no, printed_page_no(감지되면), method, text_path, text_sha256, char_count, quality, needs_review. 텍스트는 `<data>/corpus/text/...`에 페이지별로 저장한다.
- 인쇄 쪽 번호 감지는 결정적 규칙으로 한다. 추측해서 채우지 않는다.

## 3. 법제처 판례 본문 `ingestion/sources/lawapi.py` 확장

- lawService(target=prec, ID=판례일련번호)로 본문을 받아 판시사항·판결요지·참조조문·참조판례·판례내용을 필드로 저장한다. 원 응답은 raw에 보관한다.
- 사건 머리 쪽: 응답의 사건번호·선고일자·법원명은 본문 필드에 없으므로 `#h000` 쪽(kind=case_header)으로 저장한다. 판시사항 등 `s000~` 번호는 바꾸지 않는다. 판례의 사건번호·선고일은 이 값(장부 `body.body_case_no`·`decision_date`)이 정본이고, 모델 출력은 이것으로 대조·교체한다.
- 참조판례에 나온 사건번호를 큐에 넣고 확장한다(COURT_AND_RESEARCH_PLAN §2-4). 원문을 찾지 못한 사건은 `referenced_only`로 둔다.
- 판결은 저작권법 제7조 비보호 저작물이지만, 장부에는 UDEC-5 기본값을 그대로 쓴다.

## 4. DeepSeek 구조화 `press_review/providers/deepseek.py` + `ingestion/structure.py`

- OpenAI 호환 chat completions, JSON 출력이다. base URL은 `DEEPSEEK_BASE_URL`(기본 https://api.deepseek.com)이고 키는 secrets.load_secret("DEEPSEEK_API_KEY")로 받는다.
- 모델은 실행 시 `/models`로 확인하고 허용 목록(`deepseek-v4-flash`, `deepseek-v4-pro`)과 대조한다. 가격은 `config/deepseek_prices.json`의 snapshot(확인일·출처 URL 포함)을 쓴다. 작업마다 모델 ID·가격 snapshot·토큰 사용량(미적중·적중 입력, 출력)·비용을 `api_usage`에 기록한다. 이것은 API 호출이며 미세조정이 아니다.
- 반출 범위: rights.external_api = allowed 인 페이지 텍스트만 보낸다. 미공개 기사·사용자 원고는 이 경로로 보내지 않는다(코드에서 source_family 허용 목록으로 강제).
- 동시성은 낮게 시작한다(2). 429·5xx·timeout은 제한된 지수 백오프로 처리한다. 빈 응답이나 잘린 JSON은 재시도 2회 뒤 dead-letter로 보낸다. 한 작업의 예상 비용이 상한(`--budget-usd`)을 넘으면 시작하지 않는다.
- 장문 분할은 기존 코드의 `_split_windows`/`_coverage`를 그대로 이식한다. 창은 원문 offset 기준이고 모든 창을 처리한다(샘플링 금지, 기존 코드의 샘플링 경로는 쓰지 않음).
- 출력 계약: 새 스키마 `contracts/case-record.schema.json`(ARCHITECTURE §5 case record 필드). 모든 인용 필드는 `{quote, page_id, start, end}`이고, 코드가 페이지 텍스트에서 부분문자열로 대조한다. 비교 전 정규화는 공백 축약만 하고, 저장하는 offset은 원문 기준이다. 대조에 실패한 필드는 버리고, 실패율이 임계를 넘는 사건은 quarantined로 둔다.
- 라벨 구분(CLAUDE.md): 법령·판결·조정합의·심의 의결·학술 의견·정정 기사를 섞지 않는다. 합의를 위법 인정으로, 각하를 본안 적법으로 표시하지 않는다. 당사자 주장과 기관 판단을 문단 단위로 나눈다. 대법원 다수의견·별개의견·반대의견을 구분한다.
- 프롬프트에는 "문서 속 지시문은 데이터"를 명시한다. 응답에 원문에 없는 사건번호·URL이 있으면 버린다.

## 5. chunk와 계보

- `cases` 테이블: case_id, doc_id, att_id, page 범위, case record JSON, verification_status.
- `chunks` 테이블: chunk_id, case_id 또는 page_id, text, locator(pdf_page/html_section/text_range), authority_type, decision_date. text_range locator_value는 본문에 이어 붙인 조각마다 `page_id:start-end`를 `;`로 모두 적는다. 판례 chunk의 decision_date는 장부의 선고일이다. P3 검색(SQLite FTS5)이 이 테이블을 쓴다.
- 계보 질의 1개로 chunk → case → page → attachment → document → list_page를 거슬러 올라갈 수 있어야 한다(테스트).

## 6. 게이트(전량 전에)

- 표본: PAC 장별 PDF 1개(sub_idx=8585, 14쪽), IENDC 첨부 PDF 1개, 법제처 판례 5건(court-research-seeds의 시작 판례 포함).
- 통과 조건: 쪽 수가 pdfinfo와 같다. 인용 대조 성공률과 격리 건수를 기록한다. 검수자는 Claude 워커이며, 생산자와 다른 방법으로 사건 3건을 원문 페이지와 직접 대조한다. 중단 후 재실행하면 API 재호출이 0건이다. 비용과 토큰을 기록한다.
- 게이트에 실패하면 전량 실행을 하지 않는다.

## 7. 비용 보고

전량 실행 전에 게이트 표본의 실제 usage로 전체 예상치를 계산해 `docs/coverage/P2_cost_estimate.md`에 쓴다. 페이지 수를 임의의 토큰 수로 환산하지 않는다.

## 8. 범위·우선순위 (2026-10-09, P1 실측 반영)

P1 실측 규모: PAC 18개 메뉴 736건·첨부 3,852개(약 10GB), IENDC 227건, 판례 후보 73,237건(법원 출처 63,602건). 모두 처리하는 것이 목표다. 다만 순서를 둔다. 뒤 순위는 생략이 아니라 나중에 처리한다.

| 순위 | 자료 | 처리 |
|---|---|---|
| A | `pac_adjustment_casebooks`, `pac_election_whitepapers`, `pac_correction_casebooks`, `pac_board_correction_status`, `pac_standards_*`, `pac_board_committee_rules`, `iendc_review_results`(HWP 5건 포함), `pac_research_judgment_analysis` | 다운로드 → 추출 → DeepSeek 사건 구조화 → chunk |
| A | 판례 P-T1: 핵심어(정정보도·반론보도·추후보도·기사삭제·선거보도) 적중, 또는 언론어(언론·언론사·기자·기사·보도·신문·방송·인터넷신문)와 인격권어(명예훼손·인격권·사생활·초상권)의 동시 적중. 약 449건 | 본문 → 구조화 → 참조판례 확장(확장분도 같은 처리) |
| B | 판례 P-T2: 일반어(의견·상당한 이유)를 뺀 검색어가 하나라도 적중한 나머지 | 본문 → 결정적 관련성 사전 필터(본문에 언론어와 쟁점어가 모두 있는지) → 통과분만 구조화, 탈락분은 본문·chunk만 |
| B | PAC 연구·학술 자료(`pac_research_quarterly`·`seminars`·`journal`·`foreign_law`·`issue_reports`·`commissioned`, `pac_other_*`) | 다운로드 → 추출 → chunk(authority_type=research). 사건 구조화는 하지 않는다. 판결 소개 부분은 P3에서 결정한다 |
| C | 판례 P-T3: 일반어로만 걸린 나머지(약 6만 건) | 장시간 백그라운드 본문 수집(호스트 간격 준수) → 결정적 사전 필터 → 통과분만 B와 같이 처리 |

- 법원이 아닌 출처(`non_court_source` 9,635건)는 판례로 처리하지 않는다. 장부에는 남긴다.
- IENDC의 POST 전용 HWP 5건: `http.py`에 POST를 추가한다. POST는 허용 호스트의 `/fileDownload.do` 경로에서만 허용한다. 추출은 kordoc으로 한다(UDEC-6).
- 데이터 경로 일원화: `config.data_dir()`과 수집 CLI의 `context.data_root()`를 같은 함수로 합친다. `CLAUDE_PLUGIN_DATA`는 경로에 `press-check`가 들어 있을 때만 믿는다(다른 플러그인의 값이 상속된 사고, P1).
- OC 마스킹: OC 값이 평범한 단어라 경로 문자열과 겹친다. 마스킹은 `OC=` 파라미터 문맥과 응답 되울림 위치를 기준으로 하고, 단순 부분문자열 치환으로 메시지 전체를 지우지 않는다.

## 9. 게이트 후 결정 (2026-10-09, 오케스트레이터 판정)

| ID | 쟁점 | 결정 |
|---|---|---|
| G-D1 | P-T1 정의 | 언론어에 `출판`을 넣는다(출판물 명예훼손이 직접 관련). P-T1은 449건이 된다. `FILTER_VERSION`을 올린다 |
| G-D2 | 법원 판결의 결과 라벨 | 법원 판결의 outcome_type은 언제나 `judgment`이다. 상고기각·파기환송 같은 처분은 `decision_type`에 원문 그대로 적는다(정정 2026-10-09: 처음 결정문은 outcome_scope라 썼으나 outcome_scope는 열거형(whole·partial·procedural·unclear)이고 판결에는 procedural도 쓸 수 없다. 코드·계약·court_judgment 프롬프트는 처음부터 decision_type이었다). `dismissal_*`은 PAC·IENDC 절차의 기각·각하에만 쓴다. common.md에 법원 예외를 명시하고 label_guard로 강제한다(위반 시 judgment로 바꾸지 말고 그 사건을 격리) |
| G-D3 | 참조판례가 이력에 들어간 경우 | 사건 전체를 격리하지 않는다. 그 항목만 지우고 warnings에 기록한다 |
| G-D4 | IENDC 표 PDF 추출기 | kordoc 쪽별 텍스트를 기본으로 하고 pdftotext는 폴백이다(게이트 실측: 인용 100%, 비용 1/4). 소스별 추출기 선택 상수를 둔다 |
| G-D5 | IENDC 병합 칸 인용 | 한 창에서 같은 구간이 사건 2건 이상의 사실 인용으로 쓰이면 그 인용 필드를 버리고 기록한다. iendc_table.md에 '행 고유 칸을 인용하라'를 넣는다 |
| G-D6 | 프롬프트 없는 순위 A 소스 | `pac_research_judgment_analysis`는 판결 소개 사례 profile을 새로 만든다(authority_type research, 소개된 판결의 사건번호는 결정적으로 법제처 장부와 연결). `pac_standards_*`와 `pac_board_committee_rules`는 사건 구조화를 하지 않고 기준·규정 chunk만 만든다(authority_type guideline/statute에 맞는 값) |
| G-D7 | 검증 완료 대상의 재구조화 | `--force`가 없으면 거부한다. 강제로 하면 상태를 structured로 되돌린다 |
| G-D8 | PAC e-book 뷰어 껍데기(kind=other, 약 260개) | 받지 않는다. withheld(`viewer_shell`)로 둔다. 같은 간행물의 PDF가 있다 |
| G-D9 | IENDC POST 전용 HWP 4건 | iendc 어댑터가 그 idx의 첨부 행을 만들게 고치고 한 번 실행한다 |
| G-D10 | 병합 판결 | 판례는 문서 1건을 판결 1건으로 둔다(현행) |
| G-D11 | 모델 이름 | 가격표의 정식 이름(`deepseek-flash` 등)과 `/models` 결과를 기준으로 허용 목록을 맞춘다. 장부에는 요청 이름과 응답 모델 이름을 모두 기록한다 |
| G-D12 | 전량 실행 방식 | 소스마다 문서 3~5건으로 미니 게이트를 먼저 돌린다. 인용 대조율 85% 이상, 격리율 20% 이하를 통과한 소스만 전량을 실행한다. 통과하지 못한 소스는 멈추고 보고한다. 시정권고 현황·IENDC 두 소스에 한해 G-D33이 소스 단위 판정을 첨부 단위로 대체한다 |

## 10. 미니 게이트 후 결정 (2026-10-09, 오케스트레이터 판정)

근거: `docs/coverage/P2_minigate.md`. 6개 소스가 미달했다. 문서에 없는 문장을 지어낸 경우는 드물었다. 실패의 대부분은 공백 차이·조판 순서·라벨 계약 불일치였다.

| ID | 쟁점 | 결정 |
|---|---|---|
| G-D13 | 인용 대조 정규화 | §4의 '공백 축약'을 바꾼다. 대조는 양쪽에서 공백 문자를 모두 지운 문자열로 하고, 저장 offset은 원문 기준으로 되돌려 매핑한다. 공백 말고는 아무것도 정규화하지 않는다(기호·숫자·한자 그대로) |
| G-D14 | 추출기 선택 | 소스마다 `pdftotext`(기본 읽기 순서), `pdftotext -layout`, `kordoc` 세 가지로 미니 게이트 표본을 다시 추출한다. 이미 받은 DeepSeek 창 응답의 인용을 재호출 없이 각 텍스트에 대조해 대조율이 가장 높은 추출기를 그 소스의 기본으로 정한다. 결과는 `P2_extractor_compare.md`에 추가한다 |
| G-D15 | 표 칸 인용 | 표 profile(IENDC, 시정권고 현황 표)에서는 인용이 표 칸 하나와 정확히 일치하면 최소 길이 제한을 적용하지 않는다(예: '주의', '경고'). 칸 경계는 추출기의 표 구조로 판정한다 |
| G-D16 | 라벨 계약 | G-D2에 맞춘다. `dismissal_*`·`withdrawal`은 PAC·IENDC 절차(pac_settlement, pac_decision, pac_election_review, iendc_review)에 허용한다. outcome_scope와 outcome_type의 짝 규칙은 contracts.validate_case_record 한 곳에만 두고, 프롬프트 문구를 그 규칙과 같게 고친다 |
| G-D17 | correction_pair_confidence | 원보도 인용이 있으면 `before_missing`일 수 없다. 이 값은 결정적으로 유도한다: 원보도 인용 0건이면 `before_missing`, 1건 이상이면 `partial_quote`. 전후 전문이 모두 확인된 경우만 모델 값을 쓴다. 모순을 이유로 격리하지 않는다 |
| G-D18 | 잘린 JSON·dead-letter | 출력이 잘리면(finish_reason=length 또는 invalid_json) 같은 창을 반으로 나눠 다시 요청한다(최대 2단계). 그래도 실패하면 dead-letter로 보낸다 |
| G-D19 | 오래된 HWP | `unknown_type` 파일은 매직 바이트를 기록하고, HWP 3.x 서명이면 kordoc 지원 여부를 1회 시험한다. 지원하지 않으면 failed(`hwp3_unsupported`)로 남기고 숨기지 않는다 |
| G-D20 | P-T1 건수 | P-T1은 '§8 규칙을 현재 장부에 적용한 결과'로 정의한다. 참조판례 확장으로 늘어난 판례도 같은 순위로 처리한다. 고정 건수를 문서에 적지 않고 coverage 산출물이 장부 질의로 보고한다 |
| G-D21 | authority_type 'guideline' | 심의기준·편람은 법령이 아니다. review.schema와 case-record.schema의 authority_type에 `guideline`을 추가하고, `pac_standards_*`는 guideline, 위원회 규칙은 statute로 둔다 |
| G-D22 | 실색인 규모와 '문제 없음' | 색인이 핵심 자료군(judgment, pac_settlement 또는 pac_decision)을 각각 최소 100건 담지 못하면 `no_actionable_findings`를 금지한다(partial). 기준은 상수로 두고 corpus_status에 표시한다 |
| G-D23 | 날짜 없는 근거 | passage의 기준일은 decision_date, 없으면 수록 간행물의 발행일이다. 둘 다 없으면 as_of 필터에서 빼고 `date_unknown`으로 표시한다. submit_report는 date_unknown 근거를 쓸 수 있지만 보고서 coverage_notes에 그 사실을 남긴다 |
| G-D24 | counter 정밀도 | 반대 결과 신호는 패소 당사자가 문장에서 확인될 때만 준다(정밀도 우선). 처분 문구만 있는 판결은 면책 태그로만 counter가 된다(현행 수정 그대로 승인) |

## 11. 재게이트 뒤 검수 수정 (2026-10-09, 조립 규칙 `ASSEMBLY_VERSION` a2, 이후 a3·a4)

근거: 재게이트 결과를 원문 쪽과 대조한 검수에서 확정된 결함. 모두 결정적 조립 규칙(`structure_records`)이라 이미 받은 응답을 API 호출 없이 다시 조립해 반영한다. 새 결정이 아니라 G-D13·G-D15·G-D17·G-D21·G-D23의 이행이다. 수치 영향은 `docs/coverage/P2_minigate.md` 재게이트 절의 'R7'에 있다.

| 결함 | 규칙 |
|---|---|
| V-FID-01 | 같은 문구가 쪽에 여러 번 있으면(표 행마다 반복되는 결과 칸) 같은 사건의 다른 인용에 가장 가까운 일치를 쓴다. 쪽에 한 곳에만 있는 인용이 기준점이다(모호한 인용이 먼저 조립돼도 같다). 기준점이 없으면 첫 일치를 쓰되 `ambiguous_quote_first_match`로 남긴다. 행마다 되풀이되는 칸은 이제 행마다 자기 칸에 닿으므로 G-D5(병합 칸 공유 인용 삭제)는 칸이 한 번만 인쇄된 진짜 병합 칸에만 걸린다 |
| V-FID-02 | 한 창에서 사건 2건 이상이 같은 구간을 결론으로 인용했고 그것이 그 사건의 유일한 근거이면(표 머리글) 결론을 버려 근거 없음으로 격리한다. 자기 근거가 따로 있으면 공유된 결론을 둔다(병합 칸). 프롬프트에 '행마다 자기 칸을 인용' 문구를 넣어 시험했으나 효과가 없어 되돌렸다(P2_minigate R7). 근본 해결은 표를 아는 추출기이며 미결이다 |
| V-FID-03 | 줄로 갈라진 사건번호('제2021' / '-813호', '제2018-' / '1265호')는 머리와 꼬리가 3줄 안에 순서대로 있으면 원문에 있는 것으로 본다. 대시 앞·뒤에서만 자르고 각 3글자 이상, 숫자 경계를 지킨다. 사건 id가 사건번호 기반으로 바뀐다(`w0000nXX` → `제2021-813호`) |
| V-FID-04 | 조문 인용의 `제N조`·`제N조의M`은 인용 구간과 같은 쪽 원문(공백 제외)에 있어야 한다. 없으면 그 조문 항목을 버리고(`citation_not_in_source`) 인용 대조 실패율에는 넣지 않는다(인용 자체는 일치했다). 항·호는 확인하지 않는다(① 같은 동그라미 숫자) |
| V-FID-05 | 표가 아닌 소스에서 인용이 단 간격(비공백 글자 사이 공백 4칸 이상)을 두 줄 이상에서 같은 열(±2)로 가로지르고 그 열에 간격이 있는 줄이 쪽에 6줄 이상이면 좌우 단을 이어 붙인 것으로 보고 버린다(`column_join`). 가림 공백(익명 처리 빈칸)은 쪽에 몇 줄 안 돼 해당하지 않는다. 근본 해결은 2단 조판 쪽을 읽기 순서 추출기로 읽는 것이다(미결) |
| V-FID-06 | `correction_pair_confidence`: 모델이 `not_applicable`이라 해도 어느 한 기사라도 인용·서술(described_only)됐으면 `before_missing`(원보도 인용 0건, G-D17). 두 기사가 모두 absent/unknown일 때만 `not_applicable` |
| V-FID-07 | speaker `article` 필드(article_claims·disputed_spans)에서 인용이 '위 기사는 …'으로 시작하면 기관의 서술이므로 factual_background(institution, 판결은 court_majority)로 옮기고, 당사자 주장과 같은 구간이면 기사 항목을 버린다. 프롬프트 문구는 바꾸지 않았다(`common.md`를 바꾸면 모든 프로필의 작업 키가 바뀌고, `pac_correction.md`는 시정권고 현황 응답이 달라졌다, R7) |
| V-consistency-01 | `pac_standards_*`의 authority_type은 guideline, 위원회 규칙은 statute(G-D21) |
| V-consistency-02 | G-D23의 기준일은 decision_date, 없으면 문서 장부의 간행물 발행일이다. 사건 기록의 publication_date(심의 대상 기사의 날짜)는 쓰지 않는다 |
| V-consistency-04 | `pac_adjustment.md`의 pac_settlement 값 목록을 계약 표(G-D16)와 같게 한다 |

## 11. 재게이트 후 결정 (2026-10-09, 오케스트레이터 판정)

| ID | 쟁점 | 결정 |
|---|---|---|
| G-D25 | 연도만 아는 근거(passage의 72%가 date_unknown) | 연도만 알면 기준일을 그해 12월 31일로 둔다(상한). as_of 필터에서는 보수적으로 작동한다(같은 해 기사보다 늦은 자료로 취급될 수 있음). 연도도 모르면 G-D23대로 date_unknown이다. 간행물 연도는 제목·장부 year에서 결정적으로 읽고, 선거백서는 선거 회차→연도 대응표를 상수로 둔다(공식 선거 일정, 출처 URL을 주석에) |
| G-D26 | 시정권고 현황 표 PDF | 표 PDF는 첨부 단위로 고른다. 결정적 표 감지(같은 칸 구분 패턴의 반복 행)가 되면 kordoc을 쓰고, 아니면 소스 기본값을 쓴다. 표 행 사건의 인용은 행 고유 칸(의결번호·기사 제목)을 최소 1개 포함해야 한다. 머리줄만 인용한 사건은 격리한다(현행 유지) |
| G-D27 | kordoc `<br>` | 대조할 때 `<br>`, `<br/>`, `<br />`는 공백과 같이 지운다(G-D13 확장). 저장 텍스트와 offset은 원문 그대로다 |
| G-D28 | 선거백서 2단 조판·깨진 텍스트층 | 두 가지를 오프라인 비교로 고른다. 하나는 2단 감지(layout 텍스트에서 쪽 대부분 줄에 걸친 세로 공백 띠)가 되면 쪽을 좌·우 단으로 잘라(`pdftotext -x -y -W -H`) 단별로 추출하는 것이고, 다른 하나는 `pdftotext -raw`다. 텍스트층이 깨진 쪽(한글 비율·사전 단어 비율 임계 미만)은 OCR을 강제한다 |
| G-D29 | 워커의 버전 규약 변경 | ASSEMBLY_VERSION을 CODE_VERSION에서 분리한 것을 승인한다. 응답 재조립만 바뀌면 API를 다시 부르지 않는다. citation_not_in_source는 기록만 한다. 같은 쪽 범위 검사와 V-FID-07 코드 처리도 승인한다 |
| G-D30 | 전량 실행 허가 | 미니 게이트를 통과한 소스만 DeepSeek 전량 구조화를 한다. 현재는 pac_correction_casebooks, pac_adjustment_casebooks, pac_research_judgment_analysis, 판례 P-T1이다. Codex 상호검증의 ACCEPTED 수정을 반영한 뒤 시작한다 |

### 구현 메모 (G-D25~G-D28, 2026-10-09)

버전 상수: `ASSEMBLY_VERSION` a3 -> a4(G-D26 행 고유 칸 규칙, G-D27 `<br>` 대조), `EXTRACTOR_VERSION` p2x-4 -> p2x-5(G-D26 선택, G-D28 추출기·OCR 강제). `CODE_VERSION`과 프롬프트 파일은 그대로다(작업 키가 바뀌지 않아 응답을 다시 부르지 않는다). `INDEX_FORMAT`도 그대로다: 색인 열 구성은 같고 snapshot 해시가 passage 내용(기준일)을 포함해 기준일이 바뀌면 snapshot id가 바뀐다. 형식 번호를 올리면 이미 만든 색인이 읽기 거부된다.

| 결정 | 구현 위치 | 메모 |
|---|---|---|
| G-D25 | `pubyear.py`, `corpus.reference_date(year=)`, `corpus_sqlite._iter_passages` | 기준일 우선순위는 결정일, 간행물 발행일, 연도(`YYYY-12-31`, `reference_date_basis=year_upper_bound`), date_unknown이다. 연도는 장부 `year`, 없으면 제목 머리의 연도('2024 시정권고 사례집'), 선거백서는 `pubyear.ELECTIONS`(회차 -> 위원회 결정이 끝나는 연도)에서 읽는다. 12월에 치른 대통령선거(16~18대)는 위원회 운영이 이듬해 1월 18일까지라(백서 본문의 운영기간 표) 선거 연도가 아니라 다음 해가 상한이다. 대응표의 근거와 '확인 필요'(16대·21대 총선 행)는 `pubyear.py`에 있다. 판결집('제13집')처럼 연도가 없는 간행물은 date_unknown으로 남는다. 장부 사본으로 재현한 결과: date_unknown 680 -> 137(심의기준 고정 페이지 136건 포함) |
| G-D26 | `extract/layout.py`, `extract/__init__.py`(`TABLE_AUTO_SOURCES`, `resolve_pdf_extractor`), `structure_records.require_row_unique_quote` | 표 감지는 `pac_board_correction_status`의 첨부에만 적용한다. 입력 해시 꼬리표는 정책 이름(`x:auto-table-v1`)이고 고른 추출기와 근거는 문서 메모에 남는다. 행 고유 칸 규칙은 `TABLE_CELL_SOURCES`(IENDC 포함) 사건에 창 병합 뒤 적용한다 |
| G-D27 | `structure_records.BR_RE`, `strip_ws`, `compact`, `PageText` | 인용과 쪽 양쪽에서 지우고 offset은 원문 기준이다 |
| G-D28 | `extract/pdf.py`(`column_crop_text`, `find_gutter`, `pdftotext_raw`), `extract/quality.py`(`broken_layer`), `evals/gd28_compare.py` | 비교 결과와 임계 보정은 `docs/coverage/P2_extractor_compare.md` '## G-D28 비교'. 선거백서 기본값은 `column_crop`. 깨진 층 판정·OCR 강제는 `BROKEN_LAYER_SOURCES`(선거백서)에만 켠다. 다른 소스의 미표본 PDF에서 판결집 쪽의 약 3%가 걸려 넓히지 않았다 |

## 12. 참조판례 확장 깊이 (2026-10-09, 오케스트레이터 판정)

| ID | 쟁점 | 결정 |
|---|---|---|
| G-D31 | 참조판례 확장이 깊이 제한 없이 전이돼 P-T1이 480건에서 약 5,200건으로 불어났다(대부분 일반 선례로 추정) | 확장은 1단계(`--ref-depth 1`)로 제한한다. P-T1 처리 순위는 §8 규칙 적중 판례와 그 판례가 직접 인용한 판례(깊이 1)까지만 준다. 깊이 2 이상으로 이미 받은 본문은 장부에 남겨 검색 색인 재료로 쓰되 처리 순위는 P-T2와 같게 한다(결정적 사전 필터 통과분만 구조화). P-T2·P-T3 본문 수집에서는 확장하지 않는다(`--no-expand`). tiers 코드의 processing_tier 부여에 이 규칙을 반영해야 한다(미구현) |
| G-D32 | 전량 1차 실행에서 시정권고사례집 사건 4,792건 중 2,386건(50%)이 격리됐다. 그중 2,379건은 '전체 목록'·'색인' 장에서 나왔고, 대부분 인용이 0건인 목록 행이다. 사례 장의 격리율은 0~2%였다 | 사례집의 전체 목록·색인 장(제목 정규식 `전체\s*목록|색인`)은 DeepSeek 구조화 대신 쪽 원문 chunk로만 색인한다(authority pac_decision). 이 장에서 이미 만든 사건은 chunk 전용으로 다시 처리할 때 지운다. 목록 행의 결정적 표 파싱은 후속 과제다 |
| G-D33 | 게이트 미달 3개 소스 처리(사용자 결정 2026-10-10, 옵션 (나) 변형). 근거 수치는 `docs/coverage/P2_remaining_sources_options.md` | (1) 시정권고 현황(`pac_board_correction_status`)과 IENDC(`iendc_review_results`)는 **첨부 단위 인입 기준**을 적용한다. 첨부마다 사건의 인용 일치율(sum quote_ok / sum quote_total) 85% 이상이고 격리율(격리 사건 / 사건) 20% 이하이면 색인에 인입하고, 못 미치는 첨부는 색인에서 제외한다. 제외 사유는 실패 장부에 남긴다(`failures`, stage=`admission`, error_class=`attachment_excluded`, 사유에 두 비율). 기준은 두 소스의 모든 첨부에 같게 적용한다. 인용 총계 0건이면 `no_quotes`로 제외한다. 사건이 없는 첨부는 '미평가'다. (2) 이는 G-D12의 소스 단위 게이트를 이 두 소스에 한해 첨부 단위로 바꾸는 설계 변경이다(G-D12는 다른 소스에 그대로 유효). 통과 첨부만 전량 구조화한다는 뜻이 아니라 인입 판정 규칙이다. 두 소스의 구조화 전량 실행은 소규모 게이트 뒤 사용자 승인이 필요하다. (3) **사후 선택 편향**: 이 기준을 정하기 전에 문제 첨부(`bn3335`, `1949#1`)를 이미 봤다. 기준값(85%/20%)은 소스 단위 게이트(G-D12)에서 가져왔으나 첨부 단위로 옮긴 선택이 그 문제 첨부를 걸러내는 결과를 알고 이루어졌다. 아직 보지 못한 첨부의 품질은 이 기준이 보증하지 않으며, 통과는 해당 표본 수치의 충족일 뿐 법적 안전성의 인증이 아니다. (4) 선거백서(`pac_election_whitepapers`)는 이 기준의 대상이 아니다. 현 상태 유지: 색인 대상 415건을 '대표성 없음' 표시로 유지하고 PAC 이용 문의 회신 뒤 재검토한다(변경 없음). (5) **완결성 검사**(2026-10-16): 인용·격리 기준은 모델이 행을 건너뛴 첨부를 잡지 못한다(bn1983: 본문 사건번호 218개 중 113건만 추출되고 통과). 소스별로 선언한 사건번호 정규식(시정권고 현황: `YYYY안건N`·`제YYYY-N호`, `<br>` 제거 뒤 적용)으로 본문의 서로 다른 사건번호 수를 세고, 그중 장부 사건에 있는 수의 비율이 `MIN_COMPLETENESS`(0.95, CLI `--min-completeness`) 미만이면 제외한다. 같은 번호는 한 행으로 센다. 사유는 `completeness 장부/본문 < 0.95`로 기존 `attachment_excluded` 행에 남고 admission 요약에 `completeness_failed`로 따로 센다. 0.95는 출발값이며 실측은 정상 0.99~1.00 대 누락 0.52·0.66뿐이라 그 사이는 모른다. 본문에서 번호가 0건이거나 정규식을 선언하지 않은 소스(IENDC: 사건번호가 없는 사건이 대부분)는 검사를 건너뛰고 판정은 바꾸지 않되 첨부의 `notes`와 요약 `completeness_unchecked`에 '완결성 미검사'를 남긴다. 번호 없는 사건 행은 셀 수 없으므로 이 검사의 한계다. 구현: `ingestion/admission.py`(읽기 전용 `evaluate`, `--apply`만 쓰기), CLI `python -m press_review.ingestion admission --source <id> [--dry-run] [--apply]`, 색인은 미해결 admission 실패가 있는 첨부의 사건을 `case_att_excluded`로 뺀다(`corpus_sqlite._iter_passages`). 상수 `MIN_QUOTE_RATE`·`MAX_QUARANTINE_RATE`는 `admission.py` 한 곳이고 CLI `--min-quote-rate`·`--max-quarantine-rate`로 바꾼다. 사건 없는 쪽 chunk(case_id 없음)는 이 제외의 대상이 아니다 |

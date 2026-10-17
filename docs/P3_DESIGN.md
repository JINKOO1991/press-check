# P3 설계 계약 — 검색과 기사 검토 연결

작성 2026-10-09. 근거: (개발 중 문서, 공개본에 미포함) P3, `ARCHITECTURE.md` §4·§6, `docs/P0_DESIGN.md` D2·D3(Corpus 인터페이스), `docs/P2_DESIGN.md` §5(chunks). 충돌 시 근거 문서가 우선한다.

## 종료 기준(인수인계 P3)

상세 요청은 기본 이슈 ID와 같은 근거를 설명한다. 확인되지 않은 사실·반론, 직접인용 변조, 현재와 다른 기준 적용은 실패로 처리한다. 외부 검색으로 원고의 비공개 정보가 새지 않는다.

## 1. 검색 색인 `press_review/corpus_sqlite.py`

- P0 `Corpus` 인터페이스(`get_passage`, `search`, `snapshot_id`, `status`)를 SQLite로 구현한다. FixtureCorpus는 테스트용으로 남긴다.
- 색인 파일은 수집 장부와 분리한다. 위치는 `<data>/index/corpus-<snapshot>.sqlite`이다. 수집 장부의 `chunks` 중 `verification_status`가 `unverified` 이상이고 격리되지 않은 것만 넣는다. snapshot id는 입력 chunk 해시 목록의 해시로 정한다(재현성).
- 표: `passages`(passage_id = chunk_id, source_id, url, locator_kind, locator_value, authority_type, outcome_type, decision_date, reference_date, reference_date_basis, institution, case_number, text, risk_channels, issue_tags, rights_json, is_fixture=0), FTS5 `passages_fts`(tokenize='trigram'), 2글자 어휘용 보조 표 `bigram_terms`(결정적 2-gram 역색인), 사건번호·조문 정확 검색용 일반 색인.
- 검색 순서는 다음과 같다. 사건번호·조문 패턴이 있으면 정확 검색을 먼저 한다. 다음은 FTS5 trigram BM25이고, 3글자 미만 질의어는 bigram 표로 보완한다. 필터는 authority_type, as_of(reference_date ≤ as_of; reference_date는 decision_date, 없으면 수록 간행물의 발행일이고 둘 다 없으면 필터에서 빼고 `date_unknown`으로 표시한다, P2 G-D23), risk_channel, institution이다. 벡터 검색은 이번 단계에서 하지 않는다(인수인계: lexical 먼저, 이득은 측정 후).
- 반대 결과 검색: `search_cases`에 `stance` 인자를 둔다(`support`|`counter`|`both`). counter는 같은 질의에 outcome_type이 dismissal_merits·judgment(청구 배척) 또는 면책 태그인 것을 우선한다. 검색 0건이면 0건이라고 반환한다.
- 원문 노출 범위: 결과 text는 passage 원문 그대로 주되, rights.redistribute_*가 allowed가 아닌 소스는 사용자 화면 인용을 300자 이내로 자른다(내부 판단에는 전문 사용). 이 상한은 상수로 두고 README에 적는다.
- 판례 다수·별개·반대의견 구분을 passage 메타데이터로 보존하고, 반대의견 passage는 `opinion_kind`를 결과에 표시한다.

## 2. MCP·엔진 연결

- `corpus.load_default()`는 데이터 디렉터리에 색인이 있으면 SQLite 색인을 쓴다. 없으면 fixture를 쓰고 corpus_status에 '실자료 미적재'를 표시한다(P0 동작 유지). 경고에는 조회한 데이터 폴더·색인 경로를 넣고, 선택된 폴더에 색인이 없는데 기본 위치에 있으면 그 경로와 `PRESS_DISPUTE_DATA_DIR` 안내를 덧붙인다(폴더를 자동 채택하지는 않는다, 2026-10-17).
- `submit_report`의 evidence 검증은 색인의 passage_id·url·locator 일치로 한다(P0 규칙 그대로).
- `legal_as_of`는 색인 snapshot의 최신 decision_date와 수집일 중 이른 값이 아니라, 분석 기준일(보도 예정일 또는 오늘)이다. 색인 기준일은 corpus_status와 coverage_notes에 따로 적는다.
- 외부 검색 금지: MCP 도구는 로컬 색인만 검색한다. 원고 내용을 웹 검색 질의로 보내는 도구를 만들지 않는다.

## 3. 기사 검토 품질 장치

- 스킬 review의 절차에 '기사 전체 claim/source 장부 → 후보 → 지지·반대 근거 → 최소 수정 → 기사에 다시 넣어 사건 단계·제목/본문 일치·흐름 재검사'를 반영한다(인수인계 P3).
- 엔진에 `check_flow(article, edits)`를 추가한다. 수정안을 원문에 적용한 사본에서 제목·본문의 혐의 단계 어휘(혐의·수사·기소·재판·판결·확정)가 서로 모순되는지, 수정 구간끼리 겹치는지 결정적으로 검사한다. 의미 판단은 하지 않는다.
- 같은 쟁점이 여러 구절에 반복되면 하나의 이슈로 묶는다(related_edits). 기본 표시는 5건이다.

## 4. 평가 확장

- evals에 실자료 근거 시나리오를 더한다. 수정이 필요 없는 것이 정답인 공익 보도, 반론보도와 진실 여부의 구분, 의견·논평, 선거기간 심의기준을 각각 2건 이상 넣는다. 기사 본문은 합성으로 만들고 근거만 실제 색인 passage로 연결한다. 실제 기사 원문은 쓰지 않는다.
- 자동 판정 항목은 다음과 같다: 근거 passage가 색인에 있음, 인용 원문 일치, 사실 추가 0, 직접인용 보존, 과잉수정(정답이 '수정 없음'인 시나리오에서 replace 0).

## 5. 배포 범위 메모(P5에서 결정)

공개 저장소에는 색인을 넣지 않는다(UDEC-5: 재배포는 permission_required). 사용자는 다음 셋 중 하나로 색인을 얻는다. 무엇을 쓸지는 P5에서 사용자가 결정한다.
- (a) 수집 CLI로 각자 색인을 만든다(OC 필요, DeepSeek 선택).
- (b) 저작권법 제7조 대상인 판례만 별도 배포 묶음으로 낸다. 단 UDEC-5를 바꾸는 사용자 결정이 필요하다.
- (c) 기관 허락을 받은 뒤 전체를 배포한다.

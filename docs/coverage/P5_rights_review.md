# P5 공개 자료 권리 점검 (2026-10-17, 읽기 전용)

후속 조치(점검 이후 커밋): 신규 설치의 권리 기본값을 unknown으로 바꾸고 설치자가 `rights set`으로 기록하는 명령을 추가했으며(3절), 해석 불가 robots.txt는 수집을 막도록 바꿨고(3절), 테스트 조각 3곳을 합성 문자열로 바꿨다(1절). 아래 본문은 점검 시점의 상태를 적은 것이다.

법률 자문이 아니다. 합법·위법을 판정하지 않는다. 저장소에 기록된 권리·이용정책 근거와 공개될 실제 내용·코드 동작이 일치하는지만 본다.

- 기준: HEAD `d2647f3`에 다른 작업자의 미커밋 변경(NOTICE 수정, docs/PROVENANCE.md·docs/REUSE_DECISIONS.{md,json} 삭제 스테이징)을 더한 작업 트리. 추적 파일 318개.
- 작업 트리에서 지운 파일도 이력에는 남는다. 이력을 다시 쓰지 않고 push하면 docs/handoff 등 삭제 예정 문서도 공개된다.
- 방법: `git ls-files` 전수 대상 정규식 스캔(따옴표 안 한글 30자 이상, 역할어+인명 패턴), JSON 필드별 장문 집계, 표본 열람, 코드 분기 확인. 판결문·사례집 원문과의 자동 대조는 하지 않았다(코퍼스 미열람).

## 판정 요약

| 항목 | 판정 | 한 줄 |
|---|---|---|
| 1 제3자 저작물 유래 텍스트 | 부분 FAIL | 판결 유래 조각은 짧고 공공 자료. PAC 유래로 보이는 테스트 조각 3곳은 권리 미확인 상태로 공개된다 |
| 2 합성 자료 | PASS(조건부) | 평가 기사 31개 모두 가상 합성 표기가 있다. 실존 동명이인 충돌은 미확인(작성자 기록도 '미확인') |
| 3 권리 장부↔코드 | FAIL | 장부와 코드는 서로 일치한다. 그러나 작성자 결정(UDEC-5)이 모든 설치자 장부에 기본값으로 자동 기록돼 README 안내와 어긋난다. robots를 읽지 못해도 수집을 허용하는 경로가 하나 있다 |
| 4 수집 접근 준수 | PASS(부분 불명) | 허용 호스트 고정, UA 명시, 2초 간격, robots 확인, 로그인·CAPTCHA 우회 코드 없음, 법제처는 DRF 공식 API. PAC 연속 실패 시 중단 규칙은 확인하지 못함 |
| 5 의존·고지 | PASS(경미) | MIT 두 사본 동일. poppler GPL-2/3, tesseract·kor/eng 모델 Apache-2.0, kordoc MIT 로컬 확인. 번들 바이너리·폰트·데이터 없음 |
| 6 개인정보·실명 | PASS(경미) | 실명은 판결 조각의 대법관(공직자)뿐. 시드 JSON은 URL·제목·사건번호 메타데이터만 담는다. 한 테스트 조각이 실존 인물을 특정할 수 있는 서술을 담는다(항목 1과 같은 곳) |
| 7 상표·사칭 | PASS | README·manifest에 '공식'·제휴 문구 없음. 기관명은 자료 출처 설명에만 쓰인다. 비제휴 고지 문장은 없다 |

## 1. 제3자 저작물 유래 텍스트

긴 한글 인용 구간의 대부분은 코드 docstring·설계 문서의 자체 서술이다. 원문 유래로 보이는 곳만 적는다(원문 구절은 옮기지 않는다).

| 위치 | 유래(추정) | 분량 | 판단 |
|---|---|---|---|
| tests/ingestion/test_src_lawapi_body.py:18-40 | 대법원 전원합의체 판결 본문 조각(실제 원심 사건번호, 대법관 실명) | 약 20줄, 일부 문장 축약 | 판결은 저작권법 제7조 비보호 대상으로 장부에 기록돼 있다. 기록과 일치 |
| tests/ingestion/test_p2_g2fix.py:92-93 | PAC 시정권고 현황표 행(실제 결정번호와 실제 언론사명 3곳) | 3행 | PAC 권리 `pending`, 재배포 `permission_required`. 실제 언론사와 특정 결정번호를 묶어 공개한다 |
| tests/ingestion/test_p2_g2fix.py:310, 315, 328 | PAC 사례 서술로 보임 | 1문장, 3회 반복 | 이름은 없지만 조합으로 실존 인물이 특정될 수 있다 |
| tests/ingestion/test_p2_g2fix.py:196, 197 | PAC 선거기사 결정문 OCR 조각, 시정권고심의기준 조문 | 각 1~2줄 | 짧은 인용. 권리 `pending` |
| tests/ingestion/test_gd28.py:188 | 선거기사 결정문 OCR 깨진 텍스트로 보임 | 1문장 × 3 | 같음 |
| tests/ingestion/test_src_pac.py:13-50, test_src_iendc.py | 기관 페이지 HTML 구조(메뉴·간행물 제목·게시판 행) | 30줄 이내 | 사실 정보·구조. 개인정보 없음(파일 머리말 주석과 일치) |
| evals/p4_pilot_batch1.json `basis_notes.from_case` 7건 | 판례(lawgo_prec) 판단 요지의 작성자 요약 | 120~190자 | 원문 복제가 아닌 요약. 장부는 판례 `redistribute_derivatives=permission_required`로 기록한다. 판결 자체는 제7조 대상이라 실질 위험은 낮다. 기록과 실제 공개가 형식상 어긋난다 |
| evals/search/*.json, evals/baselines/*.json | 사건번호·질의어·수치만 담음 | — | 본문·발췌 없음(파일 `_note`와 일치) |
| docs/coverage/*.md | 사건번호·언론사명·집계 | — | 원보도 발췌 문장은 찾지 못함. 정규식 기반이라 짧은 인용 누락 가능 |

권고(소): PAC 유래로 보이는 테스트 조각 3곳(test_p2_g2fix.py:92-93, 310·315·328, 196-197; test_gd28.py:188)을 가상 언론사·가상 서술로 바꾼다. 테스트 의도(표 행 파싱, 머리말 이동, OCR 깨짐 감지)는 합성 문자열로 유지할 수 있다. 판례 요약을 공개 대상으로 둘 경우 장부 값과의 관계(판결 요약은 제7조 대상)를 evals/README에 한 줄 적는다.

## 2. 합성 자료

- evals/articles 12, evals/p3_articles 8, evals/p4_articles 11개 모두 `context.verification_notes`에 가상 합성 표기가 있다(스크립트 확인).
- tests 고정 자료(case_record_fixture.py, fixtures/corpus.json)는 '가상일보', '신청인 A' 같은 가상 표기를 쓴다. fixtures/corpus.json `description`이 합성이라고 밝힌다.
- p4 pilot 카드 7건은 실제 판례를 바탕으로 쓴 가상 기사다(`provenance.derivation=card_from_case`, 판례 사건번호 기재). 사실관계는 바꿔 옮겼다(`basis_notes.transposed`).
- 불명: 실제 시·도 이름을 쓰고, 가상 인명·기관명과 실존 동명이인의 충돌은 작성자 스스로 '미확인'으로 적었다(p4_pilot_batch1.json의 `fictional_names.collision_check`). P13은 가상 이사장의 이름과 횡령 서술을 함께 담는다. 실존 인물과 겹치면 오인 소지가 있다. 오프라인 점검으로는 판정할 수 없다.

## 3. 권리 장부↔코드

일치:
- 반출 게이트 `structure.py:150-170`: 문서 행과 소스 행이 모두 `external_api == "allowed"`일 때만 통과한다. unknown은 거부한다. 코드 상수 `EXTERNAL_API_FAMILIES`(structure.py:60)로 한 번 더 막는다.
- 화면 인용 상한 `corpus_sqlite.py:46`(300자), `_resolve_rights` `:324-332`(행이 없으면 unknown), `redistributable` `:335-336`(둘 다 allowed일 때만), `_public` `:755-773`(재배포 불가면 display_text를 300자로 자름).
- 문서 단위 재정의 `rights.py:84-91`: 소스 행의 forbidden은 풀 수 없다. allowed에는 근거가 필요하다.

불일치(FAIL):
- `rights.py:15-36`과 `manifest.py:351-361`(`ensure_source`)이 PAC·IENDC·법제처 계열에 UDEC-5 값을 기본으로 기록한다. 값은 read·store_raw·create_derivatives·external_api = `allowed`이고, 근거 문자열은 '사용자 결정 UDEC-5'다. 이 동작은 공개 코드를 설치해 수집을 처음 실행하는 모든 사용자에게 같다.
- UDEC-5는 저장소 작성자 개인의 결정이다(docs/DECISIONS.md:9). 작성자는 'PAC에는 명시적 이용 허락 표시가 없고, IENDC는 공공누리 표시 없는 자료에 사전 협의를 요청한다'는 고지를 받은 뒤 결정했다. 같은 내용이 docs/handoff/SOURCE_ACCESS_AUDIT.md:31, 41에 있다. 이용 문의는 초안 상태다(docs/outreach/*_inquiry.md:1-3, '발송 전').
- 플러그인 README:60과 NOTICE §3은 '이용정책·권리 표시는 사용자가 직접 확인'이라고 안내한다. 그런데 설치자가 아무 결정을 하지 않아도 장부는 allowed가 되고, 반출 게이트는 그 사용자의 PAC·IENDC 텍스트를 DeepSeek로 보낸다.
- 권고(소~중): 공개 기본값을 `unknown`(최소한 external_api·create_derivatives)으로 바꾼다. 설치자가 명시 옵트인 명령으로 자기 결정·날짜·근거를 기록하게 하고, allowed를 가정한 테스트를 고친다. 대안(문서만 수정): README·NOTICE에 '기본 권리값은 작성자 결정의 기록이며 사용자의 결정이 아니다'를 적는다. 다만 이 경우 반출이 기본으로 켜진 상태는 그대로 남는다.

unknown이 허용으로 바뀌는 경로:
- `robots.py:133`: robots.txt가 2xx인데 HTML이라 해석할 수 없으면(`unparseable`) `ALLOW_ALL`로 수집을 허용한다. 장부 기록에서는 같은 상태를 `unknown`으로 옮긴다(`rights.py:63-65`). 결과적으로 기록은 unknown인데 수집은 진행된다. 404 같은 4xx를 규칙 없음으로 보는 것(`robots.py:5`)은 RFC 9309와 같아 별개다. 권고(소): unparseable은 fail-closed로 바꾸거나 수동 확인 플래그를 요구한다.
- 문서 단위 재정의(`rights.py:84-91`)는 근거 1건만 있으면 unknown을 allowed로 바꿀 수 있다. 사람이 명령으로 하는 경로이며 자동 경로는 아니다.
- 300자 상한은 서버가 `display_text`에만 적용한다. `text` 전문은 MCP 결과로 호스트 모델에 간다(`review.py:411`, `mcp_server.py:103`의 지시문으로만 제한). README:73에 그렇게 적혀 있어 문서와는 일치한다. 실제 화면 노출 상한은 모델이 따르는지에 달렸고, 300자는 passage 단위라 여러 passage를 합친 총량 상한은 없다.

## 4. 수집 도구의 접근 준수

- 허용 호스트 고정: `http.py:43`(pac·iendc·law.go.kr 5개 호스트만).
- UA `http.py:46`, 사용자 헤더로 UA·Cookie·Authorization을 넣지 못하게 한다(`http.py:410`).
- 간격과 동시성: 호스트별 2초, 동시성 1, 프로세스 사이 flock(`http.py:6`, `:47`). 재시도는 429·5xx만 하고 Retry-After를 상한 안에서 따른다(`http.py:55`, `:621-643`).
- robots: 하루 1회 받아 해시와 함께 기록한다. 5xx·연결 실패는 fail-closed다. AI 학습·RAG 금지 문구를 감지하면 호스트 전체를 막는다(`robots.py:1-10`). robots 검사를 끄는 호출은 robots.txt 자체를 받는 요청뿐이다(`robots.py:198`).
- 로그인·CAPTCHA·쿠키 주입·TLS 검증 해제 코드는 찾지 못했다(`verify=False`·`CERT_NONE` 0건).
- 법제처는 DRF 공식 API다(`lawapi.py:37`, `:476`). LSW 주소는 표시용 canonical_url로만 쓴다(`lawapi.py:239`, `:1345`). OC 키는 환경변수로만 받는다(플러그인 README:66, `.env.example`은 값 없음). OC 이용약관 원문과의 대조는 하지 않았다(불명).
- 차단 시 중단: 403 같은 비재시도 상태는 호출자에게 그대로 넘어간다. 연속 실패 중단은 IENDC(`iendc.py:182`)와 lawapi(`lawapi.py:206-207`)에 있다. PAC는 실패 장부에 남기고 계속한다(test_src_pac.py:163). PAC 연속 403에서 멈추는지는 확인하지 못했다(불명).
- README·NOTICE와의 관계: 코드에 박힌 기관 목록과 기본 URL은 'PAC·IENDC·법제처만 수집'이라는 문서 서술과 맞는다. 다만 3절의 기본 권리값이 '사용자가 확인' 안내와 어긋난다.

## 5. 의존·고지

- LICENSE와 plugins/press-dispute-review/LICENSE는 같다(diff 0). MIT, 저작권자는 'press-dispute-review contributors'.
- poppler-utils: 로컬 패키지 copyright 파일은 'GPL-2 or GPL-3'이다. 다른 작업자가 바꾼 NOTICE의 'GPL'과 맞는다. subprocess 호출만 한다.
- tesseract와 kor·eng traineddata: 로컬 copyright 파일은 Apache-2.0이다. NOTICE와 일치한다.
- kordoc: 로컬 설치본 4.18.3의 package.json·LICENSE는 MIT다. NOTICE와 일치한다. 실행 파일을 찾아 호출만 한다(`extract/tools.py:55`).
- 서드파티 Python import는 `jsonschema`(tests/engine/refschema.py, 테스트 전용)와 `pytest`뿐이다. 번들하지 않으므로 고지 의무 대상은 아니다(관찰).
- 추적 파일은 전부 텍스트다(`file` 기준 바이너리 0). 폰트·이미지·데이터베이스 파일은 없다.
- 작업 트리 NOTICE §1은 PROVENANCE 참조를 지웠다. 코드의 `이식:` 주석과 맞는다. 원본 저장소 commit 기록은 공개본에서 빠진다(이력에는 남음).

## 6. 개인정보·실명·시드

- 실명: test_src_lawapi_body.py에 대법원장·대법관 실명(공직자, 판결 공개 정보)과 법무법인명이 있다. 그 밖의 추적 파일에서 역할어+인명 패턴으로는 실존 개인을 찾지 못했다(정규식 한계).
- 실제 언론사명과 사건 메타데이터: test_p2_g2fix.py:92-93(언론사 3곳과 시정권고 결정번호), docs/coverage/P2_fidelity_audit.md:44(언론사 1곳과 결정번호), evals/search/regression_v0.json(질의어로 쓰인 언론사명), evals/split/media_aliases.json(매체 별칭표). 공개 결정의 메타데이터이며 개인정보는 아니다.
- 식별 가능 서술: test_p2_g2fix.py:310·315·328(1절).
- 시드: docs/handoff/court-research-seeds.json은 판례 4건의 URL·날짜·사건번호와 KCI 논문 4건의 URL·제목·연도·권리 상태를 담는다. 저자명과 본문은 없다. source-seeds.json·source-policy-evidence.json은 URL·HTTP 상태·해시와 robots 짧은 인용 1건이다.
- pilot 응답 집계(docs/coverage/P4_pilot_responses_20261015.md)는 응답자를 '응답자1·2'로만 적는다.
- 작성자 개인 정보: docs/recon/reuse_verify.md:6, 14-16, 71-74에 홈 절대경로, 비공개 저장소 이름, 커밋 작성자 이메일 도메인이 있다. 다른 작업자의 정리 범위와 겹치므로 여기서는 위치만 적는다.

## 7. 상표·사칭

- README.md, plugins/press-dispute-review/README.md, plugin.json, marketplace description에 '공식'·'인증'·'제휴' 문구가 없다. 첫 줄에 '법률 자문 아님'을 적었다.
- 기관명은 자료 출처와 수집 대상 설명에만 나온다. 로고·이미지 파일은 없다.
- 권고(선택, 소): README 한계 절에 '언론중재위원회·인터넷선거보도심의위원회·법제처와 무관한 비공식 도구'를 한 줄 추가한다.

## 확인하지 못한 것

- 법률 판단이 필요해 판정하지 않은 것: UDEC-5의 근거(저작권법 제7조·제35조의5)가 PAC 사례집의 원보도 발췌와 기관 해설, IENDC 비공공누리 자료의 수집·DeepSeek 전송에 적용되는지. PAC 유래 테스트 조각과 판례 요약 공개가 인용·공정이용 범위인지. 실제 언론사명과 결정번호를 묶은 공개에 문제가 없는지.
- 판결문·사례집 원문과 저장소 텍스트의 부분문자열 자동 대조(코퍼스 미열람).
- 작성자 비공개 기사 풀의 문장이 문서에 짧게 인용됐는지(P5_prerelease_audit.md의 불명 항목과 같음).
- 법제처 OC 이용약관 원문, PAC 연속 403 처리, 가상 인명·기관명과 실존 동명이인의 충돌.
- git 이력 전체(삭제 예정 파일 포함)는 이 점검의 표본 스캔 대상이 아니었다.

# P2 게이트 결과 (P2_DESIGN §6)

실행 2026-10-09(UTC 2026-10-08 17:36~17:50 DeepSeek 호출). 실제 네트워크: IENDC·PAC·법제처 허용 호스트와 api.deepseek.com뿐이다. 네이버·KCI 요청 없음. DeepSeek 호출은 프로젝트 provider(`providers/deepseek.py`)로만 했고 모델은 `deepseek-v4-flash`, 판례 1건은 `deepseek-v4-pro`로도 돌렸다. 검수는 Claude 워커가 렌더한 쪽 이미지·법제처 원 응답과 직접 대조했다.

## 1. 표본과 단계 결과

| 표본 | 다운로드 | 추출 | 구조화 |
|---|---|---|---|
| PAC `pac_cases:1212#8585` (제1장 정정보도문 게재 사례, 307,960 B) | 신규 수신, PDF | 14쪽 pdftotext | 사건 5, 인용 38/38, 격리 0 |
| IENDC `iendc:3104#1` (조치내역 2026.06.10., 109,993 B) | 신규 수신, PDF | 3쪽 pdftotext | 사건 12, 인용 22/40, 격리 12 |
| IENDC `iendc:2109#1` (제32차 조치내역 HWP 5.0, 28,672 B) | POST `/fileDownload.do` 신규 수신 | 1쪽 kordoc layout | 사건 2, 인용 9/9, 격리 0 |
| 판례 5건: serial 194652, 241789, 617163, 229161(=99다6203), 64599(P-T1 선정 규칙: 시드 4건을 뺀 P-T1 중 serial 최소) | lawService 본문 5건 | HTML 섹션 39개(194652·229161·241789·617163 각 8, 64599 7) | 사건 5(각 1), 인용 118/118, 격리 0 |

P-T1 선정 규칙으로 뽑힌 64599는 소유권이전등기 사건(강박 취소)이고 언론·인격권 쟁점이 아니며 결정적 사전 필터에서 탈락했다. P-T1 규칙이 일반어로 넓게 걸리는 정도를 보여 주는 표본 1건이다(정밀도의 근거로는 약하다). 구조화는 필터와 무관하게 돌렸다.

수집 명령은 모두 `python -m press_review.ingestion`(fetch·extract·lawapi_body)과 `python -m press_review.ingestion.structure`다. `PYTHONPATH=<engine>`, KORDOC_BIN은 환경변수로만 줬다. 판례 본문 수집은 `--serial` 5개, `--max-ref-queries 5`였고 참조판례 23건이 큐에 올라 21건은 장부 안에서 연결, 2건은 `referenced_only`, API 검색 1회(2014다28121, 새 문서 0건)가 나갔다. 64599는 `--no-expand`였다.

## 2. 쪽 수와 쪽별 방법·품질

쪽 수 대조: PAC `pdfinfo` 14쪽 = pages 행 14, IENDC PDF `pdfinfo` 3쪽 = 3. HWP는 kordoc pageCount 1 = 1행(pdfinfo 대상 아님). 판례는 쪽이 아니라 섹션이다.

| 첨부 | 쪽(pdf) | 인쇄 쪽 | method | quality | 글자 | needs_review |
|---|---|---|---|---|---|---|
| PAC | 1 | - | pdftotext (OCR 시도, 미채택) | 0.85 | 24 | 1 (표지, too_short) |
| PAC | 2 | - | pdftotext | 1.0 | 40 | 0 |
| PAC | 3~14 | 33~44 | pdftotext | 1.0 | 947 / 1,081 / 499 / 812 / 1,198 / 1,044 / 1,388 / 191 / 1,123 / 800 / 1,033 / 998 | 0 |
| IENDC PDF | 1~3 | 1~3 | pdftotext | 1.0 | 3,753 / 3,602 / 2,995 | 0 |
| IENDC HWP | 1 | - | kordoc (page_mode=layout, 실제 쪽) | 1.0 | 917 | 0 |
| 판례 5건 | 섹션 s000~s007 | - | html_section | - | 29~7,089 | 0 |

OCR은 PAC 1쪽(표지)에서만 시도됐고 채택되지 않았다. 텍스트 층이 맞았다("2025년도 언론조정중재 사례집 02"). 표지를 needs_review로 올린 것은 오탐이다. 인쇄 쪽 번호는 PAC 12쪽(33~44)과 IENDC 3쪽에서 감지됐고 나머지는 비웠다(추측하지 않음).

## 3. 인용 대조와 격리

| 대상 | 제안 인용 | 원문 일치 | 성공률 | 격리 사건 | 버린 항목 |
|---|---|---|---|---|---|
| PAC | 38 | 38 | 100% | 0/5 | 0 |
| IENDC PDF (pdftotext) | 40 | 22 | 55% | 12/12 | 18 |
| IENDC HWP | 9 | 9 | 100% | 0/2 | 0 |
| 판례 5건 | 118 | 118 | 100% | 0/5 | 0 |
| 합계 | 205 | 187 | 91.2% | 12/24 | (첨부 3건 실행의 dropped_items 23) |

IENDC 3104는 12건이 모두 격리되어 chunk가 0개다(`chunks`에 `iendc:3104` 행 없음). 격리 사건은 chunk로 만들지 않으므로 지금 상태로는 P3 검색에 이 문서가 보이지 않는다.

IENDC PDF 격리 12건의 사유: 인용 실패율 25% 초과 8건, 라벨 계약 위반 4건(`iendc_review`에 `dismissal_merits`). 같은 PDF를 kordoc 쪽별 텍스트로 만든 임시 코퍼스 실험은 인용 12/12(100%), 격리 5건(전부 라벨 계약 위반)이었다(`P2_extractor_compare.md`).

## 4. api_usage

장부 `api_usage` 11행(attempt 단위). 모델 열에는 공급자 현행 이름 `deepseek-flash`가 기록되고 요청 모델은 `deepseek-v4-flash`다. 모든 행이 off-peak 단가(price snapshot 2026-10-09)로 정산됐다.

| id | 모델 | 대상 | 시도 | 상태 | prompt (캐시 적중) | completion | 비용 USD |
|---|---|---|---|---|---|---|---|
| 1 | flash | iendc:2109#1 | 1 | ok | 2,772 (0) | 1,888 | 0.001549 |
| 2 | flash | iendc:3104#1 | 1 | invalid_json | 4,737 (1,792) | 8,544 | 0.005574 |
| 3 | flash | iendc:3104#1 | 2 | invalid_json | 4,737 (4,603) | 8,728 | 0.005271 |
| 4 | flash | iendc:3104#1 | 3 | ok | 4,737 (4,603) | 7,835 | 0.004735 |
| 5 | flash | pac_cases:1212#8585 | 1 | ok | 9,161 (1,536) | 7,646 | 0.005736 |
| 6 | flash | 판례 194652 | 1 | ok | 6,185 (1,536) | 3,308 | 0.002687 |
| 7 | flash | 판례 229161 | 1 | ok | 5,973 (1,920) | 2,766 | 0.002273 |
| 8 | flash | 판례 241789 | 1 | ok | 7,014 (1,920) | 5,009 | 0.003775 |
| 9 | flash | 판례 617163 | 1 | ok | 10,037 (1,920) | 4,672 | 0.004027 |
| 10 | flash | 판례 64599 | 1 | ok | 6,022 (1,920) | 2,579 | 0.002168 |
| 11 | pro | 판례 241789 | 1 | ok | 7,011 (0) | 5,793 | 0.016097 |

위 11행 합계 $0.053891. 8절 뒤의 중단 시험이 flash 3행(id 12~14, 판례 600419·64929·65095, $0.004768 + $0.001795 + $0.002344 = $0.008907)을 더해 장부 합계는 14행 $0.062798(상한 $2의 3.1%)이다. 임시 코퍼스 kordoc 실험 $0.004039는 그쪽 장부에만 있다. 총 $0.0668. 모든 실행에 `--budget-usd`를 남은 예산으로 줬다(1.8, 1.7, 1.6, 1.5).

## 5. 재실행: API·다운로드 0건

완료 직후 같은 대상을 CLI로 다시 돌렸다.

| 단계 | 결과 | http network_requests |
|---|---|---|
| fetch (`--att` 3개) | 후보 0 | 0 |
| extract (`--att` 3개) | 후보 0(첨부가 structured로 넘어감) | 0 |
| lawapi_body (`--serial` 5개) | 후보 0, 본문·참조 검색 0 | 0 |
| structure 첨부 3 + 판례 5 | skipped_unchanged 8, api_attempts 0 | 0 (api_usage 11행 그대로) |
| iendc `--pages 6 --detail-idx 2109` | 상세 skipped_done 1 | 1 (목록 1쪽: 합계 확인용으로 매번 다시 받는 어댑터 동작) |

중단·재개(실호출 시험): P-T1 판례 3건(64929, 65095, 600419)을 `--no-expand`로 더 받아 구조화를 시작하고, `api_usage`에 첫 새 행이 생긴 직후 프로세스를 `kill -9` 했다(시작 후 약 34초). 장부는 600419 작업 `done`, 64929 작업 `running`이었다. 같은 명령을 다시 돌리자 600419는 `skipped_unchanged`(호출 0)였고 미완료 창 2개만 다시 호출했다(api_attempts 2, 새 행 id 13·14, 사건 3개 모두 인용 100%). 끝난 창은 다시 호출되지 않았다. 한계: kill 순간 진행 중이던 호출 2건은 공급자에는 청구됐을 수 있으나 장부에는 없다(재호출 비용 $0.0041과 같은 규모). dead-letter와 창 분할은 기존 단위 테스트만 다룬다.

## 6. 계보 질의 표본 3개 (`Manifest.lineage_of_chunk`)

1. `iendc:2109#1:w0000n00#c1` → case `iendc:2109#1:w0000n00` → page `iendc:2109#1:p0001`(pdf_page 1) → att `iendc:2109#1`(POST fileDownload URL) → doc `iendc:2109`(2022년도 제32차 위원회의 결정사항) → list_page 6. 끝까지 연결됨.
2. `pac_cases:1212#8585:2025광주조정142·143#c1` → page p0013 → att(chapter) → doc `pac_cases:1212`(2025년도 언론조정중재 사례집) → list_page 1. 끝까지 연결됨.
3. `lawgo_prec:229161:w0000n00#c1` → page `#s002` → doc(손해배상등)까지만 연결. `list_url`·`list_page_no`가 null이다(판례 문서는 목록 쪽 행이 문서에 이어져 있지 않다). 판례 chunk의 계보는 문서까지다.

## 7. 독립 검수 (Claude 워커, 생산자와 다른 방법)

| 사건 | 대조 대상 | 결과 |
|---|---|---|
| PAC 2025서울조정432·433/434·435 | PDF 3~4쪽을 `pdftoppm`으로 렌더한 이미지 | 사건번호, 처리결과(조정성립), 신청인·피신청인 주장, 중재부 판단, 조정 성립 사유가 일치한다. 조정 합의를 위법 인정으로 쓰지 않았다. `mark_verified`로 verified 처리(by `claude-worker-p2-gate`, method `independent_reviewer`). |
| IENDC 2109 우리일보 사건 | kordoc 추출 텍스트(HWP 쪽 이미지를 만들 도구가 없어 추출 단계와 독립이 아님) | 조치(경고문 게재), 결정일 2022-05-31, 위반내역이 일치한다. 심의유형 "자체심의"는 담지 못했고, issue_tags 중 후보자 부각·편향보도는 원문에 없는 모델 어휘다. verified 처리하지 않았다. |
| IENDC 3104 8번 화성타임즈 사건(`w0000n07`) | PDF 2쪽 렌더 이미지 | 조치 기각, 심의유형 이의신청, 위반내용이 일치한다. 보도일자가 둘(2026-05-12, 2026-06-01)인데 publication_date에는 뒤의 하나만 들어갔다. 결론 유형이 `dismissal_merits`라 계약상 격리된 것은 맞다. |
| 판례 241789 (2022다251650, 상고기각) | 법제처 lawService 원 응답(JSON) | 당사자·기사 4건·허위 판단 부분·상고기각 주문·참조조문은 일치한다. 아래 결함 G7이 나왔다(사건번호·선고일 null, 참조판례를 이력 칸에 넣음). verified 처리하지 않았다. |

## 8. flash 대 pro (판례 241789, 같은 입력·프롬프트·thinking 끔)

| 항목 | flash | pro |
|---|---|---|
| 비용 / 지연 | $0.003775 / 15.4초 | $0.016097 / 33.7초 (4.3배 / 2.2배) |
| 인용 대조 | 28/28 | 32/34 (legal_provisions 2건 못 찾음) |
| 결론 유형 | dismissal_merits (프롬프트는 상고기각을 `judgment`로 지시) | judgment (프롬프트와 일치) |
| 청구 | 2건(손해배상·기사삭제 청구를 나눔) | 1건(합쳐서 서술) |
| 참조조문 | 구 단위 2건(민법 제750조·제751조·형법 제310조 묶음) | 조문 낱개 4건(민법 제751조 누락) |
| 이력(appeal_or_later_history) | 8건: 원심 1 + 참조판례 7 | 7건: 참조판례만(원심 없음) |
| 사건번호·선고일 | null / null | null / null |

pro가 라벨 지시는 더 따르지만 인용 대조·내용 충실도·비용에서 우위가 없다. 두 모델 모두 사건번호·선고일을 채우지 못했다.

실행 순서: flash 스냅샷 저장 → pro 실행 → 응답 파일을 백업에서 되돌리고 flash 재실행(API 0건, 사건 JSON이 스냅샷과 같음). 이 과정에서 G3을 실행으로 확인했다.

## 9. 게이트 기준 항목별 판정

| 기준(§6) | 판정 | 근거 |
|---|---|---|
| 쪽 수가 pdfinfo와 같다 | 통과 | PAC 14 = 14, IENDC 3 = 3 |
| 인용 대조 성공률과 격리 건수를 기록한다 | 통과(기록함) | 전체 91.2%, 격리 12/24. IENDC PDF만 55%·12건이며 원인은 추출기와 라벨 계약 |
| Claude 워커가 사건 3건을 원문과 직접 대조한다 | 수행함. 사건 4건 대조 | PAC 통과, IENDC HWP 통과(독립성 한계), IENDC PDF 격리 사유 확인, 판례는 결함 G7 |
| 중단 후 재실행 시 API 재호출 0건 | 통과(재호출 0건은 완료분 기준). 실제 중단 시험은 미완료 창만 재호출 | 완료 후 재실행: api_attempts 0, api_usage 11행 그대로, 다운로드 0건. kill -9 시험: 끝난 창 재호출 0건, 미완료 창 2개만 재호출 |
| 비용과 토큰을 기록한다 | 통과 | 4절 |
| 게이트 실패 시 전량 실행 금지 | 위 5개 항목은 문언상 통과. 전량 실행은 아래 G3~G7을 고친 뒤를 권고한다 | 순위 A PAC 서사형은 즉시 가능한 수준(인용 100%, 격리 0), IENDC PDF는 kordoc 연결 후, 판례는 메타데이터·라벨 수정 후 |

## 10. 게이트에서 나온 결함과 이번 작업의 코드 변경

변경(이번 작업에서 고침):

- G1 IENDC 어댑터가 POST 전용 첨부(HWP)를 장부에 올리지 않았다(`post_only_non_pdf` 실패 행만 남김). `sources/iendc.py`가 `cmmn_file_down(filename, downname)`의 두 인자로 `/fileDownload.do?filename=…&downname=…&uniqueKey=&downauth=` 첨부 행을 만들게 했다(`downname`의 `+`는 `%2B`). POST 첨부는 HEAD를 건너뛰고 상세 재방문 판정에서도 건너뛴다. 실제 POST 1회로 HWP 5.0 파일(28,672 B)을 받았다.
- G2 fetch·extract에 `--att`(반복) 필터를 추가했다(`ingestion/fetch.py`, `ingestion/extract/__init__.py`, 두 어댑터). 지정하면 순위·소스 매핑 없이 그 첨부만 고른다.
- 테스트: 기존 iendc POST 테스트 1건을 새 동작에 맞게 고치고 3건을 더했다(fetch `--att`, extract `--att`, POST 본문의 `+` 보존). 전체 694 passed, 1 xfailed(변경 전 691 passed).

미결(고치지 않음, 전량 실행 전 결정 필요):

- G3 구조화 창 응답 캐시가 모델·단계 버전을 보지 않는다(`structure.py` 437~445행은 `input_sha256`만 비교). 같은 대상에 pro를 돌려 `w0000.json`을 덮은 뒤 flash를 다시 돌리면 pro 응답이 flash 사건으로 재조립된다. 실행으로 확인했고 백업에서 복원했다. 남은 위험: 실제 코퍼스에서 판례 241789의 pro 작업(`sj-` 해시)은 장부에 `done`으로 남아 있는데 `structured/lawgo_prec_241789-d70373da/w0000.json`은 지금 flash 응답이다. 이 문서에 `--model deepseek-v4-pro`를 다시 돌리면 flash 응답이 pro 사건으로 조립·표시된다. 전량 실행 전에 이 작업 행을 지우거나 G3을 고쳐야 한다.
- G4 인용 위치가 원문의 첫 출현으로 잡힌다. 같은 문구가 여러 행에 반복되면 서로 다른 사건 5쌍이 같은 구간을 가리킨다(IENDC 3104 사건 3·4의 결론 인용 등).
- G5 IENDC 표 PDF는 pdftotext 입력에서 인용 55%, 호출 3회(JSON 2회 실패), 비용 3.9배. kordoc 쪽별 텍스트에서는 1회, 100%(`P2_extractor_compare.md`). kordoc 추출 연결은 하지 않았다.
- G6 결론 유형 라벨 불이행: `iendc_review`에 `dismissal_*`를 붙여 격리된 건이 IENDC PDF 12건 중 4건(kordoc 입력 5건). 판례는 프롬프트가 상고기각을 `judgment`로 지시하는데 flash가 5건 중 4건에 `dismissal_merits`를 붙였다.
- G7 판례 사건 기록: 판례 8건 중 사건번호가 맞은 것 0건, 틀린 것 4건(194652 → 참조판례 번호 `2000다37524,37531`, 실제 `2000다37647`. 617163 → 원심 번호 `2018나24881,24898`, 실제 `2022다284711, 284728`. 600419 → `2022다228056`, 장부 `2023다240763`. 64929 → `2008노190`, 장부 `2008도2422`), null 4건. 선고일 null 8건(법제처 응답에 `선고일자`가 있다). `appeal_or_later_history`에 참조판례(인용된 선례)가 들어간다(241789 flash 8건 중 7건, 64599 5건 중 4건, 229161 5건 중 4건). 인용된 선례를 이력으로 라벨링하는 것은 CLAUDE.md의 라벨 구분 위반이다. 사건번호·선고일·법원은 장부 메타데이터(`extra_json.case_no`, `decision_date`)로 결정적으로 채우는 것을 권고한다.
- G8 판례 chunk 계보가 목록 쪽까지 이어지지 않는다(6절 3번).
- G9 `attachments.orig_filename`이 Content-Disposition의 퍼센트 인코딩 원문을 200자로 자른 값이다(표시용 메타데이터만 영향).
- G10 표지처럼 글자가 적은 쪽이 needs_review로 오탐된다(18쪽 중 1쪽).
- G11 IENDC `issue_tags`는 근거 인용이 없는 모델 어휘다(30개 중 16개가 원문에 없는 표현, 2건에서 `명예훼손`이 원문에 없이 붙음).

데이터 위치: 장부·원문·텍스트는 `~/.local/share/press-dispute-review/corpus`(저장소 밖). kordoc 비교 입력·스냅샷은 세션 scratchpad에만 있다.

## 11. 수정 후 재구조화 (2026-10-09, 결함 V-locator-1·V-meta-1/2/3·V-label-2·V-resume-G3·V-cost-nousage)

코드 수정 내용: ① 판례 본문 추출에 머리 쪽 `#h000`(이 판결 자신의 사건번호·선고일자·법원명)을 추가했다(`EXTRACTOR_VERSION` lawgo-prec-html-2, 판시사항 등 `s000~` 번호는 그대로). ② 판례의 사건번호·선고일은 장부 값(`body.body_case_no`, `documents.decision_date`)이 정본이고, 모델 값이 다르면 `case_number:not_in_ledger`·`decision_date:not_in_ledger`를 `quote_check.dropped_fields`에 남긴다. `case_id`는 장부 표기에서 나온다. ③ 판례 chunk의 `decision_date`는 그 기록에서 오고, text_range locator는 이어 붙인 본문 조각마다 `page_id:start-end`를 `;`로 모두 적는다. ④ `court_judgment.md`(version 2026-10-09.2)에서 참조판례를 `later_history`에 넣으라는 지시를 지웠다. 코드에는 참조판례 구간(`ref_cases`)에서 인용한 이력 항목을 `label_guard` 오류(사건 quarantined)로 두는 검사를 더했다. ⑤ 창 응답 파일을 `<unit>@<job_id>.json`으로 쓰고(예전 `<unit>.json`은 파일 안 `job_id`가 같을 때만 쓴다), 다른 모델·버전의 응답을 재사용하지 않는다. ⑥ 응답에 usage가 없을 때의 비용에 입력 토큰 상한(요청 글자 수 환산)을 넣는다.

재실행: `lawapi_body --no-expand --serial` 8건(raw에서 재추출, `network_requests` 0, `reextracted` 8)으로 머리 쪽을 만들고, 구조화는 `--doc` 8건, `deepseek-v4-flash`, `--budget-usd 1`로 돌렸다. 프롬프트가 바뀌어 새 작업 8건이다. 수정 전 장부의 판례 사건 8건은 모두 `unverified`였고 옛 사건·chunk는 같은 트랜잭션에서 교체됐다.

| 항목 | 수정 전(4~8절) | 수정 후 |
|---|---|---|
| 모델이 낸 사건번호 = 장부 사건번호 | 0/8 (틀림 4, null 4) | 8/8 (장부 값으로 대조해 `not_in_ledger` 기록 0건) |
| 모델이 낸 선고일 = 장부 선고일 | 0/8 (null) | 8/8 |
| `cases.case_number` / `case_id`의 번호 | 원심·참조판례 번호 또는 `w0000nNN` | 8/8 장부 표기 (예: 617163 → `lawgo_prec:617163:2022다284711,284728`) |
| chunk `decision_date` 비어 있음 | 8/8 | 0/8 |
| `appeal_or_later_history` (G7에 적힌 건) | 241789 8(참조판례 7) · 617163 13(11) · 64599 5(4) · 229161 5(4) | 8건 합계 9 (건당 1, 64599만 2). 전부 `appeal`·`remand`이고 참조판례 항목 0 |
| 격리 / 인용 대조 | 격리 0/8, 인용 대조 118/118(5건 기준 4절) | 격리 0/8, 버린 항목 1(194652 `original_article.quote:quote_not_found`) |
| `outcome_type` = `dismissal_merits` | 5건 중 4 (G6) | 2/8 (241789·617163, 둘 다 상고기각). 계약은 두 값을 모두 허용한다(open_items) |

locator 예: 617163 chunk는 `lawgo_prec:617163#s001:83-207;…;#s002:47-262;#s004:111-160;#s005:11-41;#s006:73-631;…;#s006:5045-5082`로 s006(원심 판단·결론) 구간까지 모두 가리킨다.

비용(`api_usage` id 15~22, 모두 flash): 194652 $0.002442 · 229161 $0.002011 · 241789 $0.002976 · 600419 $0.004067 · 617163 $0.003241 · 64599 $0.002544 · 64929 $0.001962 · 65095 $0.001989. 8행 합계 $0.021234(상한 대비 `--budget-usd 1`의 2.1%). 장부 `api_usage` 합계는 22행 $0.084032이다.

재실행: 같은 `--doc` 8건 `skipped_unchanged` 8, `api_attempts` 0. PAC·IENDC 표본 3건(`--att`, `--budget-usd 0.001`)도 `skipped_unchanged` 3, `api_attempts` 0이다(`common.md`·`pac_*`·`iendc_table.md`를 건드리지 않아 단계 버전이 같다).

미해결(코드로 판정할 수 없거나 계약 결정 필요): V-label-1(상고기각 라벨 두 지시의 충돌), V-ground-1(IENDC 표 머리 문구 인용). 4~8절의 표는 수정 전 기록이므로 그대로 둔다.


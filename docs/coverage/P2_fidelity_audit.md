# P2 재게이트 원문 충실도 표본 검수 (2026-10-09)

대상: 재게이트(`P2_minigate.md` '재게이트') 29개 첨부의 s3 사건 1,034건, 인용 5,386개. 장부는 읽기 전용(`mode=ro`)으로 열었고 코드는 고치지 않았다. DeepSeek 호출 없음.

## 1. 방법

스크립트 위치: `<scratch>/fid/`(아래 `scratchpad/fid/`). 인용별 스캔 결과는 같은 폴더 `quotes.jsonl`.

| 단계 | 방법 | 수행 |
|---|---|---|
| 전수 스캔 | 저장 인용마다 (a) `text[start:end] == quote`, (b) 쪽 텍스트의 줄바꿈 변환(`read_text`와 바이트 디코드 비교), (c) 공백 제거본에서 같은 문자열의 출현 수와 저장 위치가 첫 출현인지, (d) 두 사건 이상이 같은 `(page_id,start,end)`를 쓰는지, (e) 인용 쪽이 사건 `page_ids_json` 밖인지, (f) `\S {3,}\S`(단 넘김 후보), (g) 구간 안 `<br>` | 스크립트 `scratchpad/fid/scan.py` |
| 날짜 | `decision_date`·`publication_date`가 사건 자신의 쪽에 있는지, 없으면 어느 쪽에 있는지 | 스크립트 `scratchpad/fid/dates.py` |
| 표 행 | 표 소스 결과 인용이 그 사건 매체명·의결번호가 있는 행(`<tr>` 또는 줄)에 있는지 | 스크립트 `scratchpad/fid/rows.py` |
| 재추출 | 표본 12건의 쪽을 원 파일에서 다시 추출: PDF는 쪽 `method`와 같은 모드(`pdftotext` = `-layout`, `pdftotext_reading` = 기본)로 `-f N -l N`, HWP·IENDC는 `KORDOC_BIN`(`--format json --no-images --silent`, IENDC PDF는 `-p N`) | 스크립트 `scratchpad/fid/show.py` |
| 필드 대조 | 사건번호·결정일·매체·처리 결과·outcome_type·발화 주체·원보도 인용·짝 신뢰도를 쪽 텍스트와 대조. 선거백서 778 76·77쪽은 `pdftoppm` 이미지로 원본을 직접 읽었다 | 눈으로 확인(Claude) |

표본은 소스마다 2건이다. 격리되지 않은 사건 중에서 스캔 표지가 있는 것 1건과 없는 것 1건을 `random.Random(20261009)`로 골랐다. `pac_correction_casebooks`는 표지 있는 사건이 없어 무작위 1건과 짝 신뢰도 예외 사건 1건(`제2019-202호`)을 골랐다.

## 2. 전수 스캔 결과

| 점검 | 결과 |
|---|---|
| offset 역매핑 `text[start:end]==quote` | 불일치 0 / 5,386 |
| 쪽 파일 줄바꿈(`\r`) | 0쪽. `read_text` 결과와 바이트 디코드 결과가 모두 같다 |
| 인용 쪽이 사건 `page_ids_json` 밖 | 0. 단 시험이 되지 않는다: `page_ids_json`은 인용이 놓인 쪽의 집합이다(`structure_records.py:590-596`) |
| 같은 문자열이 쪽에 2회 이상 나오는데 첫 출현에 놓인 인용 | IENDC 29, 선거백서 67, 조정중재 4 (첫 출현이 아닌 것은 IENDC 2) |
| 두 사건 이상이 같은 구간을 씀 | 시정권고 현황 93, 선거백서 75, IENDC 26, 조정중재 6 |
| 표 결과 인용이 다른 행에 놓임(격리 안 된 사건) | IENDC 12 / 22, 시정권고 현황 113 / 235(줄 단위 판정이라 -layout 3줄 행은 과대 집계. 그중 93건은 머리말 한 곳) |
| `\S {3,}\S` 포함 인용(단 넘김 상한) | 선거백서 383, 판결 분석 116, 시정권고 현황 85. 판결 분석은 무작위 8건을 읽었고 모두 익명 처리 공백·항목 간격이라 단 넘김 0/8 |
| 구간 안 `<br>` | 시정권고 현황 40, IENDC 11 |
| 날짜가 사건 자신의 쪽에 없음 | 조정중재 결정일 7 / 33, IENDC 결정일 28 / 59(회의 머리말), 선거백서 보도일 14 / 227, 시정권고 사례집 보도일 5 / 122, 판결 분석 결정일 1 / 43 |
| 원보도 `described_only`인데 짝 신뢰도 `not_applicable` | 591건(시정권고 현황 correction 235, 시정권고 사례집 correction 22, 선거백서 177 등) |
| 법령 인용의 `제N조` 숫자가 그 인용 문자열에 없음 | 107건(선거백서 93, 시정권고 사례집 13, 조정중재 1). 인용이 조문 머리 없이 항·호 본문만 덮는 무해한 경우가 섞여 있다. 통과 소스 14건은 문맥을 모두 읽었고 전부 무해(조 번호가 인용 바로 앞 줄에 있음)였다 |

## 3. 재추출 대조

표본 12건의 PDF 쪽(`pdftotext`, `pdftotext_reading`)은 모두 공백 제거본이 같다. 바이트 차이는 끝의 `\n\f` 2~3자뿐이다. kordoc 쪽(`bn1355`, `2105#1`, `2287#1` 쪽별 `-p N`)은 바이트까지 같다. `779#7982`의 두 쪽은 OCR이라 다시 뽑지 않았다(200쪽 OCR, 재게이트도 재추출하지 않음).

## 4. 표본 12건 필드 대조

| 소스 | case_id | 상태 | 사건번호 | 결정일 | 매체 | 처리 결과·outcome_type | 주체 구분 | 원보도 인용 | 짝 신뢰도 | 판정 |
|---|---|---|---|---|---|---|---|---|---|---|
| 시정권고 사례집 | `pac_cases:1168#8410:제2024-753호` | unverified 4/4 | 일치 | null(쪽에 없음) | 파이낸셜투데이 일치 | 시정권고·correction 일치 | 맞음 | described_only | before_missing(G-D17 대로) | 정상 |
| 시정권고 사례집 | `pac_cases:1009#7952:제2019-202호` | unverified 8/8 | 일치 | null(쪽에 없음) | 서울경제 일치 | 시정권고·correction 일치 | disputed_spans `article`에 기관 서술 "위 기사는 … 공개하였다"(p0005:144-195) | described_only | **not_applicable**(같은 상황의 1168은 before_missing) | V-FID-06, V-FID-07 |
| 조정중재 | `pac_cases:662#7827:2012서울조정758∙759` | unverified 3/3 | 일치 | null | 네이트 일치 | 조정성립·settlement 일치 | 맞음 | described_only | not_applicable | V-FID-06 |
| 조정중재 | `pac_cases:662#7827:2012서울조정262∙263∙264` | unverified 7/7 | 일치 | 2012-03-08 맞음. 단 p0059에 있고 사건 쪽은 p0057~58 | 한국방송공사 일치 | 조정성립·settlement 일치 | 맞음 | 제목+일자 줄(p0057:701-773) | partial_quote | V-FID-08 |
| 판결 분석 | `pac_other:700#5599:2001가합52334` | unverified 10/10 | 일치 | 2002-08-30 일치 | 조선일보사 일치 | 청구기각·research 일치 | article_claims `article`이 해설자 서술 | described_only | not_applicable | 경미(V-FID-07 유형) |
| 판결 분석 | `pac_other:700#5599:2001가합14741` | unverified 8/8 | 일치 | 2002-10-11 일치 | KBS·MBC 일치 | 원고 패소·research 일치 | article_claims `article`과 claims `claimant`가 같은 구간(p0133:505-634, 소 제기 서술) | described_only | not_applicable | V-FID-07 |
| 선거백서 | `pac_election:778#7977:2004자심49` | unverified 9/10 | 일치(`2004 자심 49`) | null(쪽에 없음) | (주)전민일보 일치 | 경고·decision 일치 | 맞음 | p0077:493-638이 두 단을 줄마다 섞은 문자열 | partial_quote | **V-FID-04, V-FID-05** |
| 선거백서 | `pac_election:779#7982:2006자심18` | unverified 11/11 | 일치 | null | 전국매일 일치 | 경고·decision 일치 | 맞음 | '박 윤 국 포 천 시 장 인터뷰'(OCR 제목 조각) | partial_quote | 법령 번호 `제 # 조`·`제 ? 조`를 모델이 8·2로 채움(V-FID-04 유형) |
| 시정권고 현황 | `pac_cases:bn1355#4294:2013안건199` | unverified 1/1 | 일치 | null | 인터넷 기호일보 일치 | 시정권고(행의 결과 칸은 `″`) | 맞음 | described_only | not_applicable | V-FID-06, 6절 관찰 |
| 시정권고 현황 | `pac_cases:bn1355#4294:2013안건172` | unverified 1/1 | 일치 | null | 아주뉴스 일치 | 결과·침해유형 칸이 모두 `″`. 윗행 값을 모델이 채움 | 맞음 | described_only | not_applicable | V-FID-06, 6절 관찰 |
| IENDC | `iendc:2105#1:w0000n03` | unverified 3/3 | null(의결번호 없음) | 2022-05-25(머리말) | 이지뉴스 일치 | 결과 인용 p0001:1228-1240이 **윗행**(인천시장선거 행)의 칸 | 맞음 | 기사 목록 칸 | partial_quote | **V-FID-01** |
| IENDC | `iendc:2287#1:w0000n02` | unverified 1/1 | null | 2024-03-20 | 오마이뉴스 일치 | 기각·dismissal_merits, 같은 `<tr>` 안 | 맞음 | described_only | not_applicable | 정상 |

## 5. 결함 (재현 1회)

| vid | 심각도 | 위치 | 내용 | 재현 | 기대 근거 |
|---|---|---|---|---|---|
| V-FID-01 | major | `structure_records.py:171-179`, `:329-334` | 4자 이상 인용은 `PageText.find`가 첫 출현만 잡는다. 표에서 같은 결과 문구가 행마다 반복되면 결과 인용이 다른 행에 놓인다 | `iendc:2105#1:w0000n03`~`n12`(통과 사건 10건)의 outcome이 모두 p0001:1228-1240(윗행 칸). `PageText('\| A \| 공정보도<br>협조요청 \|…\n\| B \| 공정보도<br>협조요청 \|…').find(…)` → (6,18) | CLAUDE.md '문장 위치와 원문을 정확히 연결한다'. G-D15(P2_DESIGN.md:105)의 가장 가까운 칸 규칙은 4자 미만에만 걸린다 |
| V-FID-02 | major | `structure_records.py:171-179`, `:590-596` | 시정권고 현황 PDF에서 `시정권고 결정` 인용이 모든 사건에서 p0002 머리말 `■ 시정권고 결정 67건`에 놓인다(행 안의 결과 칸은 -layout에서 두 줄로 갈라짐). 이 사건들의 인용은 이것 1개라 locator가 p0002 하나가 된다 | `bn2822#74045` 47건, `bn3335#76744` 46건(모두 통과)이 p0002:51-58을 공유. 매체명이 p0002에 없고 p0003 이후에만 있는 사건이 48건(예: `bn2822…w0000n17` 인터넷 경기신문은 p0003). 이 93건은 R1의 시정권고 현황 251/299에 일치로 들어 있다 | CLAUDE.md '문장 위치와 원문을 정확히 연결한다', G-D13(P2_DESIGN.md:103) offset은 그 사건 원문 위치 |
| V-FID-03 | major | `structure_records.py:431` | -layout 표에서 의결번호가 `제2021`/`-813호`로 다른 칸 글자를 사이에 두고 갈라져, `compact(case_number) in source_compact`가 실패하고 사건번호가 null이 된다 | `bn2466`·`bn2822`·`bn3335` 225건 전부 `case_number` null, dropped `case_number:not_in_source`. bn2822 p0002 원문에 `제2021 … -813호` | CLAUDE.md '숫자·사건번호 … 표 행 누락을 검수한다'. 원문에 인쇄된 의결번호가 있다 |
| V-FID-04 | major | `structure_records.py:493-501` | 법령 `citation`을 인용 문자열과 대조하지 않는다. 깨진 텍스트 층에서 모델이 읽은 조문 번호가 그대로 저장된다 | `2004자심49`: citation `제18조`, `제18조의3 제13항`, `규칙 제12조 제12항`. 원본 이미지(76·77쪽)는 `제8조`, `제8조의3 제3항`, `제12조 제2항`. 인용은 `저 18조으13 저 13 함`이라 대조는 통과 | CLAUDE.md '숫자·사건번호 … 오류를 검수한다', '사건번호·조문·원문 위치·링크를 검증' |
| V-FID-05 | major | `structure_records.py:171-179`(G-D13) | -layout 2단 쪽에서 왼단·오른단을 줄마다 이은 문자열이 연속 인용으로 통과한다. R5(P2_minigate)가 적은 2단 조판의 대조 실패와 반대쪽, 즉 잘못된 통과다 | `2004자심49` available_original_article p0077:493-638 `가습입니다. 그리고 보듭       우리 전라북도민과 …`. 원본 이미지는 왼단 '…보듬어 주는 안식처입니다'와 오른단 '우리 전라북도민과…'가 별개 문단. 이 인용으로 짝 신뢰도가 partial_quote가 됐다. 통과 소스(판결 분석)는 표본 8건 중 0건 | G-D13(P2_DESIGN.md:103)은 공백만 지운다는 규칙이고 단을 넘는 결합을 허용하는 결정은 없다 |
| V-FID-06 | major | `structure_records.py:634-635` | 원보도가 `described_only`이고 인용 0건인데 모델 값 `not_applicable`을 유지한다 | `derive_pair_confidence('not_applicable', {'status':'described_only'}, {'status':'absent'})` → `not_applicable`(같은 입력에 모델 값만 `unverified_pair`면 `before_missing`). 재게이트 591건, 이 중 시정권고 correction 257건 | G-D17(P2_DESIGN.md:107) '원보도 인용 0건이면 before_missing'. 테스트 주석(`test_p2_collect_gd.py:392`)도 예외를 '어느 기사도 인용·서술되지 않았다'로 한정 |
| V-FID-07 | minor | `contracts.py:663`(주체 고정), 모델 출력 | `disputed_spans`·`article_claims`의 speaker `article`에 기관·해설자의 서술이 들어간다 | 시정권고 사례집 disputed_spans 39개 중 21개가 '위 기사는 …' 머리의 기관 서술(머리말 정규식만으로 셈, 예 `제2019-202호` p0005:144-195). `2001가합14741` article_claims와 claims(claimant)가 같은 구간 p0133:505-634 | prompts/common.md:48 'the disputed passages of the article (speaker article)' |
| V-FID-08 | minor | `structure_records.py:590-596` | 사건 쪽 범위(`supporting_passage_ids`·`source_locators`)를 인용이 놓인 쪽으로만 만든다. 처리 결과·결정일이 있는 쪽이 locator에서 빠진다 | `2012서울조정262∙263∙264` 결정일 2012-03-08과 조정성립사항은 p0059에 있는데 locator는 p0057~58. `691∙692`(실제 p0073~78)는 locator p0073~74, 결정일 2012-05-17은 p0078. 확인한 날짜 값은 맞았다 | CLAUDE.md 'OCR·HWP/HWPX·PDF 추출에는 원문 페이지/구간을 보존한다' |
| V-FID-09 | minor | `structure_records.py:329-334`(ok_relocated) | 지정 쪽에서 못 찾은 인용을 창의 다른 쪽에서 찾으면 다른 사건 구간에 놓인다. 쪽 머리말이 문장 중간에 끼면 자기 쪽 대조가 실패해 같은 상투 문장이 있는 다른 사건으로 간다 | `1070#7721` 자심27·자심42의 legal_reasoning이 p0035:571(자심13 구간)에 놓였고 p0035가 두 사건의 locator에 들어갔다. 자심27 자기 쪽 p0044의 같은 문장은 `등에 제4부 \| 심의・의결사례 관한`으로 끊겨 있다 | CLAUDE.md '문장 위치와 원문을 정확히 연결한다' |

## 6. 한계

- 표본은 소스당 2건이다. 전수 수치는 스크립트 휴리스틱이다. 표 행 판정은 줄 단위라 -layout 여러 줄 행을 과대 집계한다. 단 넘김(`\S {3,}\S`)은 상한이고 실제 단 넘김으로 확인한 것은 `2004자심49` 1건이다. 다른 하나(`1070#7721` 자심40 article_claims)는 텍스트로만 봤다.
- 원본 이미지 확인은 선거백서 778의 76·77쪽만 했다. 나머지 필드 대조는 추출 텍스트 기준이다.
- 결함에서 뺀 관찰: 표 인용 `quote`에 추출 표지(`<br>`, `|`)가 원문처럼 저장된다(시정권고 현황 40, IENDC 11. `P2_extractor_compare.md` 미결 2번과 같은 문제). 결과 칸이 `″`인 행(`2013안건172`)은 결과 문구 없이 통과한다. 근거 조항을 찾지 못해 결함으로 올리지 않았다.
- 수행: 스캔·재추출·집계는 Claude(Opus 5.5)가 스크립트로 했다. 필드 대조는 Claude가 눈으로 했다. DeepSeek 출력은 대조 대상이고 근거로 쓰지 않았다.

# P1 인벤토리

생성 2026-10-08T16:42:28+00:00 (UTC). 이 파일은 `python -m press_review.ingestion export-coverage` 가 manifest.sqlite 질의로 만든다. 수치를 손으로 고치지 않는다.

## 소스별 수치

| 소스 | 목록 페이지(ok/전체) | 문서 | 첨부 | 첨부 총 바이트 | HEAD 확인 | 실패(열림/해결) | 확인 시각 |
|---|---|---|---|---|---|---|---|
| `iendc_review_results` IENDC 심의결과 내역 | 23/23 | 227 | 222 | 31,564,641 | 222 | 5/0 | 2026-10-08T13:17:57+00:00 |
| `lawgo_prec_supreme` 법제처 국가법령정보 판례(대법원, 본문검색 사전) | 1,171/1,171 | 73,237 | 0 | 0 | 0 | 0/0 | 2026-10-08T16:42:27+00:00 |
| `pac_adjustment_casebooks` 언론조정중재사례집 | 3/3 | 21 | 234 | 566,719,337 | 218 | 0/0 | 2026-10-08T16:41:20+00:00 |
| `pac_board_committee_rules` 위원회 규칙(관련 법규 게시판) | 1/1 | 4 | 4 | 1,158,135 | 4 | 0/3 | 2026-10-08T16:41:21+00:00 |
| `pac_board_correction_status` 시정권고 현황(자료실 게시판) | 28/28 | 275 | 320 | 39,745,166 | 320 | 0/0 | 2026-10-08T16:41:21+00:00 |
| `pac_correction_casebooks` 시정권고사례집 | 1/1 | 9 | 60 | 472,712,351 | 56 | 0/0 | 2026-10-08T16:41:20+00:00 |
| `pac_election_whitepapers` 선거기사심의백서 | 2/2 | 19 | 130 | 1,784,637,681 | 115 | 0/0 | 2026-10-08T16:41:20+00:00 |
| `pac_other_annual_reports` 기타 자료(연간보고서) | 3/3 | 21 | 130 | 729,226,369 | 112 | 0/0 | 2026-10-08T16:41:21+00:00 |
| `pac_other_mediation_persuasion` 기타 자료(조정을 위한 설득과 수사) | 3/3 | 25 | 139 | 123,760,149 | 111 | 0/0 | 2026-10-08T16:41:21+00:00 |
| `pac_other_misc_reports` 기타 자료(각종 보고서 등) | 1/1 | 9 | 13 | 33,589,873 | 12 | 0/0 | 2026-10-08T16:41:21+00:00 |
| `pac_other_promo_publications` 기타 자료(홍보 간행물) | 1/1 | 5 | 14 | 88,444,248 | 13 | 0/0 | 2026-10-08T16:41:21+00:00 |
| `pac_research_commissioned` 연구용역보고서 | 1/1 | 2 | 4 | 6,493,198 | 4 | 0/0 | 2026-10-08T16:41:21+00:00 |
| `pac_research_foreign_law` 해외언론법제연구보고서/해외언론관계판결집 | 1/1 | 9 | 52 | 534,647,487 | 52 | 0/0 | 2026-10-08T16:41:21+00:00 |
| `pac_research_issue_reports` 연구보고서/현안보고서 | 1/1 | 7 | 19 | 21,441,390 | 19 | 0/0 | 2026-10-08T16:41:21+00:00 |
| `pac_research_journal` 학술지 미디어와 인격권(학술지 자료) | 3/3 | 27 | 158 | 203,663,820 | 144 | 0/0 | 2026-10-08T16:41:20+00:00 |
| `pac_research_judgment_analysis` 언론관련판결분석보고서 | 4/4 | 32 | 162 | 715,951,738 | 131 | 0/0 | 2026-10-08T16:41:21+00:00 |
| `pac_research_quarterly` 계간 언론중재 | 18/18 | 180 | 1,946 | 3,577,308,229 | 1,814 | 0/0 | 2026-10-08T16:41:21+00:00 |
| `pac_research_seminars` 세미나/토론회 자료집 | 9/9 | 89 | 463 | 1,012,546,358 | 462 | 0/0 | 2026-10-08T16:41:21+00:00 |
| `pac_standards_correction_page` 시정권고 심의기준(고정 페이지) | 1/1 | 1 | 1 | 2,283,532 | 1 | 0/0 | 2026-10-08T16:41:21+00:00 |
| `pac_standards_election_page` 선거기사 심의기준 및 대표사례(고정 페이지) | 1/1 | 1 | 2 | 39,912,079 | 2 | 0/0 | 2026-10-08T16:41:21+00:00 |
| 합계 | 1,276/1,276 | 74,200 | 4,073 | 9,985,805,781 | 3,812 | | |

## 화면 표시 총수 대조

| 소스 | 목록 항목 합 | 문서 수 | 합=문서 | 마지막 쪽 외 전부 10건 | 문서 수가 마지막 쪽 범위 안 |
|---|---|---|---|---|---|
| `iendc_review_results` | 227 | 227 | 예 | 예 | 예 |
| `lawgo_prec_supreme` | 106,163 | 73,237 | 해당 없음 | 해당 없음 | 해당 없음 |
| `pac_adjustment_casebooks` | 21 | 21 | 예 | 예 | 예 |
| `pac_board_committee_rules` | 4 | 4 | 예 | 예 | 예 |
| `pac_board_correction_status` | 275 | 275 | 예 | 예 | 예 |
| `pac_correction_casebooks` | 9 | 9 | 예 | 예 | 예 |
| `pac_election_whitepapers` | 19 | 19 | 예 | 예 | 예 |
| `pac_other_annual_reports` | 21 | 21 | 예 | 예 | 예 |
| `pac_other_mediation_persuasion` | 25 | 25 | 예 | 예 | 예 |
| `pac_other_misc_reports` | 9 | 9 | 예 | 예 | 예 |
| `pac_other_promo_publications` | 5 | 5 | 예 | 예 | 예 |
| `pac_research_commissioned` | 2 | 2 | 예 | 예 | 예 |
| `pac_research_foreign_law` | 9 | 9 | 예 | 예 | 예 |
| `pac_research_issue_reports` | 7 | 7 | 예 | 예 | 예 |
| `pac_research_journal` | 27 | 27 | 예 | 예 | 예 |
| `pac_research_judgment_analysis` | 32 | 32 | 예 | 예 | 예 |
| `pac_research_quarterly` | 180 | 180 | 예 | 예 | 예 |
| `pac_research_seminars` | 89 | 89 | 예 | 예 | 예 |
| `pac_standards_correction_page` | 1 | 1 | 예 | 해당 없음 | 해당 없음 |
| `pac_standards_election_page` | 1 | 1 | 예 | 해당 없음 | 해당 없음 |

화면에 표시된 총 건수 자체는 PAC 게시판·IENDC 에서 장부에 저장되지 않는다(어댑터 실행 때 확인만 했다). 이 표의 '마지막 쪽 범위'는 간행물 목록에만 의미가 있고, 게시판·고정 페이지·법제처 행은 참고값이다.

### 법제처 대조

- 사전 27개 중 완료 27개, 연 단위 분할 2개
- 문서 합집합 73,237건, 질의어 없는 전체 조회 75,951건, 어느 질의어에도 안 걸린 2,714건
- 1900년 이전 선고일 3,426건, 2026-10-09 이후(오늘 다음 날부터) 선고일 1건
- 시드 4/4건 확인

## 연도 분포

- `iendc_review_results`: 2012:19 2013:8 2014:21 2015:6 2016:13 2017:13 2018:15 2019:3 2020:16 2021:25 2022:32 2023:2 2024:19 2025:20 2026:15
- `lawgo_prec_supreme` (10년 단위): 1940s:5 1950s:294 1960s:3,323 1970s:4,352 1980s:11,441 1990s:17,433 2000s:11,868 2010s:14,802 2020s:6,332; 연도 미상 3,387
- `pac_adjustment_casebooks`: 2005:1 2006:1 2007:1 2008:1 2009:1 2010:1 2011:1 2012:1 2013:1 2014:1 2015:1 2016:1 2017:1 2018:1 2019:1 2020:1 2021:1 2022:1 2023:1 2024:1 2025:1
- `pac_board_committee_rules`: 2018:1 2025:3
- `pac_board_correction_status`: 2004:9 2005:14 2006:13 2007:13 2008:11 2009:10 2010:11 2011:11 2012:10 2013:9 2014:11 2015:12 2016:12 2017:11 2018:11 2019:11 2020:14 2021:15 2022:13 2023:14 2024:13 2025:15 2026:12
- `pac_correction_casebooks`: 미상:9
- `pac_election_whitepapers`: 미상:19
- `pac_other_annual_reports`: 2005:1 2006:1 2007:1 2008:1 2009:1 2010:1 2011:1 2012:1 2013:1 2014:1 2015:1 2016:1 2017:1 2018:1 2019:1 2020:1 2021:1 2022:1 2023:1 2024:1 2025:1
- `pac_other_mediation_persuasion`: 2013:7 2014:6 2015:4 2016:4 2017:4
- `pac_other_misc_reports`: 2006:1 2008:1 2011:1 2012:2 2014:1 2016:1 2021:1 미상:1
- `pac_other_promo_publications`: 미상:5
- `pac_research_commissioned`: 미상:2
- `pac_research_foreign_law`: 미상:9
- `pac_research_issue_reports`: 2025:3 2026:4
- `pac_research_journal`: 2015:1 2016:1 2017:2 2018:2 2019:2 2020:2 2021:3 2022:3 2023:3 2024:3 2025:3 2026:2
- `pac_research_judgment_analysis`: 2007:1 2008:1 2009:1 2010:1 2011:1 2012:1 2013:1 2014:1 2015:1 2016:1 2017:1 2018:1 2019:1 2020:1 2021:1 2022:1 2023:1 2024:1 2025:1 미상:13
- `pac_research_quarterly` (10년 단위): 1980s:33 1990s:40 2000s:40 2010s:40 2020s:27; 연도 미상 0
- `pac_research_seminars`: 1999:1 2000:5 2001:4 2002:4 2003:4 2004:4 2005:4 2006:5 2007:4 2008:4 2009:5 2010:5 2011:5 2012:5 2013:3 2014:4 2015:4 2016:3 2017:2 2018:2 2019:1 2020:2 2021:2 2022:2 2023:1 2024:1 2025:2 2026:1
- `pac_standards_correction_page`: 미상:1
- `pac_standards_election_page`: 미상:1

## 실패·보류

| 소스 | 열린 실패(유형:건수) | 해결됨 | 문서 상태 |
|---|---|---|---|
| `iendc_review_results` | post_only_non_pdf:5 | 0 | discovered:227 |
| `lawgo_prec_supreme` | 0 | 0 | discovered:73,237 |
| `pac_adjustment_casebooks` | 0 | 0 | discovered:21 |
| `pac_board_committee_rules` | 0 | 3 | discovered:4 |
| `pac_board_correction_status` | 0 | 0 | discovered:275 |
| `pac_correction_casebooks` | 0 | 0 | discovered:9 |
| `pac_election_whitepapers` | 0 | 0 | discovered:19 |
| `pac_other_annual_reports` | 0 | 0 | discovered:21 |
| `pac_other_mediation_persuasion` | 0 | 0 | discovered:25 |
| `pac_other_misc_reports` | 0 | 0 | discovered:9 |
| `pac_other_promo_publications` | 0 | 0 | discovered:5 |
| `pac_research_commissioned` | 0 | 0 | discovered:2 |
| `pac_research_foreign_law` | 0 | 0 | discovered:9 |
| `pac_research_issue_reports` | 0 | 0 | discovered:7 |
| `pac_research_journal` | 0 | 0 | discovered:27 |
| `pac_research_judgment_analysis` | 0 | 0 | discovered:32 |
| `pac_research_quarterly` | 0 | 0 | discovered:180 |
| `pac_research_seminars` | 0 | 0 | discovered:89 |
| `pac_standards_correction_page` | 0 | 0 | discovered:1 |
| `pac_standards_election_page` | 0 | 0 | discovered:1 |

## 수집하지 않음·보류

| 소스 | 상태 | 권리 | 사유 |
|---|---|---|---|
| 네이버 뉴스 옴부즈만(고침·정정·불공정 선거보도) | 수집 안 함 | forbidden | robots.txt가 AI 학습·RAG 목적의 봇 접근을 명시적으로 금지한다. collection_enabled=false (UDEC-1). 요청하지 않았다. |
| 다음 뉴스 정정보도 모음 | 보류 | unknown | 이용 정책 확인 전이다. 허용 호스트 목록에 넣지 않았고 요청하지 않았다. 권리는 unknown. |
| KCI 논문 서지 | 수집 안 함 | unknown | robots 금지로 기록된 호스트다(P1_DESIGN). 요청하지 않았다. 권리는 unknown. |
| 학술 논문 본문(언론법 관련) | 보류 | unknown | 출판사·저자별 이용 조건이 소스마다 다르고 확인하지 않았다. 권리는 unknown, 수집 보류. |

---
name: corpus-status
description: press-dispute-review가 근거 검색에 쓰는 코퍼스(판례·심의·조정 자료)의 스냅샷 ID, 자료군별 건수, 기준일, 누락·권리보류 현황, 합성(fixture) 여부를 보여 준다.
when_to_use: 사용자가 "어떤 자료로 검토하나", "판례 몇 건", "코퍼스 상태", "자료 기준일"을 묻거나, 검토 결과의 근거 범위를 확인하려 할 때.
allowed-tools: mcp__plugin_press-dispute-review_press-review__corpus_status
---

# 근거 코퍼스 상태

도구: `mcp__plugin_press-dispute-review_press-review__corpus_status` (입력 없음)

## 절차

1. `corpus_status`를 호출한다.
2. 결과를 짧은 표로 보여 준다: 스냅샷 ID, 색인 구축일(`index_built_at`)과 최신 결정일(`latest_decision_date`), 자료군별 건수(`by_authority_type`, `by_source_family`), 검수 상태(`by_verification_status`), 누락·권리보류 요약, 검색 방식(`search_mode`), `warnings`, fixture 여부, 색인 규모 기준(`core_coverage`: 판결·PAC 사건 자료군별 사건 수 `cases_by_authority_type`, 기준 100건, `no_actionable_findings_allowed`)과 기준일 없는 passage 수(`date_unknown_count`). 색인 구축일은 자료 기준일이고, 검토 보고서의 분석 기준일(기사 발행 예정일)과 다른 날짜다.
3. 서버가 준 숫자를 바꾸거나 반올림해 다른 의미로 말하지 않는다. 자료군 건수는 "코퍼스에 적재된 건수"이며 전체 사건 수가 아니다. 목록 기준 커버리지와 전체 사건 커버리지를 구별한다.

## 색인 경고

`core_coverage.no_actionable_findings_allowed`가 `false`이면 "이 색인으로는 '검토 권고 항목 없음'을 말할 수 없다"고 한 줄로 알리고 `core_coverage.reason`을 그대로 보여 준다. `warnings`가 있으면 그대로 보여 준다(예: 미검수 자료 비율, trigram 미지원으로 bigram 전용 검색). `rights_hold`의 소스는 사용자 화면 인용을 300자 이내로 제한한다는 점도 한 줄로 알린다.

## fixture 코퍼스일 때 경고

결과가 fixture(합성) 코퍼스를 가리키면 표 위에 다음 경고를 그대로 쓴다.

> **실제 판례·심의 자료 미적재.** 현재 코퍼스는 기능 시험용 합성 예제입니다. 검토 결과의 근거는 실제 판례·언론중재위원회 자료가 아니며, 근거로 인용하거나 법적 판단에 쓰면 안 됩니다.

## 지킬 것

- 코퍼스에 없는 자료를 "있다"고 말하지 않는다. 누락·권리보류 항목이 있으면 숨기지 않는다.
- 건수가 많다는 이유로 "모델이 판례를 학습했다"고 표현하지 않는다. 이 플러그인은 검색만 한다.
- 이 상태는 검토 결과의 법적 안전성을 보증하지 않는다.

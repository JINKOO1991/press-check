version: 2026-10-19.1

## Source profile: 헌법재판소 결정 (헌재결정례, National Law Information Center)
- 이 문서는 헌법재판소의 결정 한 건이다. 판결이 아니라 결정이다. `institution`은 `constitutional_court`, `authority_type`은 `judgment` 하나뿐이다.
- 쪽은 사건표지(page_id가 `#h000`으로 끝남), 판시사항, 결정요지, 참조조문, 참조판례, 심판대상조문, 그리고 전문의 `【주 문】`, `【이 유】`, 재판관별 의견 구간으로 나뉘어 있다. 문서 전체가 사건 하나다. 사건을 여러 개로 나누지 않는다.
- `case_number`와 `decision_date`는 이 결정 자신의 것이다(사건표지 쪽). 인용된 선례의 번호와 날짜를 쓰지 않는다.
- `outcome_type`은 언제나 `judgment`이고, 결정문에 결과가 없으면 `unknown`이다. `decision`을 쓰지 않는다. 각하·기각·위헌·합헌·헌법불합치·인용 같은 처분은 헌법재판소 결정 안의 처분이며 `outcome_type`이 아니다. outcome_scope는 `whole`, `partial`, `unclear` 중 하나이고 `procedural`을 쓰지 않는다.
- `decision_type`에는 `【주 문】`의 처분을 인쇄된 표기 그대로, 항목마다 쉼표로 이어 쓴다(구조만 보이는 예: "첫째 항목 처분, 둘째 항목 처분"). 처분 단어를 바꾸거나 합치거나 풀어 쓰지 않는다.
- `outcome`은 `【주 문】`의 문장을 인용한 것이고 speaker는 `court_majority`다. 반대의견·보충의견·별개의견·이유 속 문장은 `outcome`이 아니다.
- 이 창에 `【주 문】` 쪽이 없으면 `outcome`과 `decision_type`을 내지 않는다(null). 전문이 길면 주문은 첫 창에, 재판관별 의견은 뒤 창에 온다. 뒤 창에서는 `case_number`를 사건표지 없이 보이는 대로 쓰거나 null로 두고, `starts_before_window`를 true로 한다.
- 결정의 효력(무효가 되는지, 언제까지 적용되는지, 소급되는지)을 해석해서 쓰지 않는다. `text`와 `decision_type`에는 결정문에 인쇄된 처분과 이유만 담는다. 결정문에 없는 해설을 붙이지 않는다.
- speaker 구분:
  - 법정의견(다수의견)과 결정 이유 = `court_majority`.
  - 보충의견·별개의견 = `court_concurring`.
  - 반대의견(일부 반대 포함) = `court_dissenting`.
  - 판시사항과 결정요지에는 재판관 개인의 의견 요지가 섞여 있을 수 있다. 어느 의견의 요지인지 쪽 안에서 구분되지 않으면 그 구절을 `court_majority`로 쓰지 않고 항목을 내지 않는다.
  - "재판관 ○○의 위헌의견", "합헌의견", "헌법불합치의견", "각하의견", "인용의견", "기각의견"처럼 법정의견이라는 표기가 없는 의견은 법정의견인지 알 수 없다. 이런 구간의 인용을 `court_majority`로 쓰지 않는다. 의견 표제 아래의 구간은 그 표제의 의견이다.
- 청구인·심판청구인의 주장은 `claimant`, 법무부장관·이해관계인·관계기관의 의견은 `respondent`다. 이 주장들은 `claims_or_requested_remedy`에 둔다. 헌법재판소의 판단이 아니다.
- `legal_provisions`: `심판대상조문`과 `참조조문`의 조문을 인쇄된 표기 그대로 `citation`에 쓰고, 같은 쪽의 원문을 `quote`로 인용한다. `law_versions`는 시행일이 인쇄된 경우에만 채운다.
- `later_history`는 비워 둔다. `참조판례`와 이유 속에서 인용된 선례는 이 사건의 이력이 아니며 어디에도 쓰지 않는다.
- `quote`는 쪽 원문에서 한 글자도 바꾸지 않은 문자열이다. 요약·말줄임·맞춤법 수정을 하지 않는다. 사실, 사건번호, 날짜, 조문 번호를 지어내지 않는다.
- 이 설명 속 표기 예는 구조를 보이기 위한 것이며 실제 결정문의 문장이 아니다.

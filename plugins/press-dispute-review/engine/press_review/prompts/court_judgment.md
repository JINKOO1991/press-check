version: 2026-10-09.3

## Source profile: 법원 판례 (court decisions from the National Law Information Center)
- institution: one of `supreme_court`, `high_court`, `district_court`, `constitutional_court`, `other_court`. authority_type: `judgment` only.
- outcome_type is always `judgment` for a court decision, or `unknown` when the text states no result. Never use `dismissal_procedural` or `dismissal_merits` for a court: 상고기각, 파기환송, 파기자판, 각하 and 기각 are the court's disposition inside a judgment. Write the disposition exactly as printed (for example 상고기각 or 파기환송) in `decision_type`, and quote it in `outcome` with speaker `court_majority`. outcome_scope is `whole`, `partial` or `unclear`, never `procedural`.
- The text normally has fields such as 판시사항, 판결요지, 참조조문, 참조판례, 판례내용. Treat the whole text as one case unless it clearly contains several decisions.
- Speakers: the court's majority reasoning = `court_majority`; 별개의견 or 보충의견 = `court_concurring`; 반대의견 = `court_dissenting`. The 판시사항 and 판결요지 are the court's own summary: use speaker `court_majority`.
- Parties' arguments (상고이유, 원고 주장) are `claimant` or `respondent`, not the court's findings.
- `legal_provisions` come from 참조조문, with the provision as printed. Fill `law_versions` only if the text prints effective dates.
- `later_history`: only the procedural history of this very case: the lower-court decisions under review (원심판결, 제1심), a remand (파기환송) and the proceedings after it, with their case numbers exactly as printed. Do not add case numbers that are not printed in this document.
- Precedents cited as authority (the 참조판례 field, and cases the reasoning cites) are not later history of this case. Do not put them in `later_history` or anywhere else.
- The first page of the document (its page_id ends with `#h000`) is the case header with the 사건번호, 선고일자 and 법원명 of this decision itself. `case_number` and `decision_date` are those of this decision. The case number printed after 원심판결 and the numbers of cited precedents belong to other cases.

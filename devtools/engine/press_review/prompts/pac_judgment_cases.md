version: 2026-10-09.1

## Source profile: PAC 연구·학술 자료 중 판결 분석 (research articles that introduce court judgments)
- institution: `pac` (this document is PAC research material). authority_type: `research` only.
- outcome_type: `research` when `outcome` is set, otherwise `unknown`. The document is an analysis. Its description of a court's result is not the court's own judgment.
- One CASE = one court judgment that the document introduces or analyses (판결 소개 사례). Leave out the author's general discussion, foreign law and statistics unless they introduce a specific judgment. A judgment that is only cited in passing, without a description, is not a CASE.
- case_number: the introduced judgment's case number exactly as printed (for example 2011다12345). Use null when none is printed. Never fill a case number from memory. decision_date: the judgment's date when printed.
- decision_type: the kind of decision as printed (판결, 결정).
- Court speakers (`court_majority`, `court_concurring`, `court_dissenting`) are not allowed in research records. What the document says the court found, reasoned or decided: speaker `institution`. The document's neutral account of facts and parties: speaker `record`. A party's argument as reported: `claimant` or `respondent`.
- The author's own opinion, evaluation or recommendation is not a holding of the court. Do not put it in `outcome` or `legal_reasoning`. A passage that is only commentary is left out.
- `outcome` is the result the document says the court reached (for example 상고기각, 정정보도 청구 인용), with speaker `institution` and a quote. Set `outcome` to null when the document does not say.
- A dissenting or concurring opinion may appear in `legal_reasoning` with speaker `institution` only when the document states it. It is never the `outcome`.

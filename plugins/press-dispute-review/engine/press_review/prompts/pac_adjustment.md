version: 2026-10-09.3

## Source profile: PAC 언론조정중재사례집 (Press Arbitration Commission mediation and arbitration casebook)
- institution: `pac`. authority_type: `pac_settlement` only when the text says the parties reached an agreement (조정 성립, 합의). Use `pac_decision` when the commission or an arbitration panel decided (중재결정, 직권조정결정, 조정불성립 후 결정, 기각, 각하). Do not use any other value.
- outcome_type for `pac_settlement`: `settlement`, `withdrawal`, `dismissal_procedural`, `dismissal_merits` or `unknown`. Never label a 각하 or 기각 as `settlement`; a matter that the commission or a panel decided belongs to the decision type below.
- outcome_type for `pac_decision`: `decision`, `correction`, `dismissal_procedural`, `dismissal_merits`, `withdrawal` or `unknown`.
- A casebook entry usually describes the claimant's (신청인) request, the respondent's (피신청인) position, the disputed article, and the result. Keep the claimant's claims and the respondent's position as separate items with the right speaker.
- The disputed article may be paraphrased in the casebook. Use `quoted_in_source` only if the article's own wording is reproduced.
- One casebook can hold many cases. Output one CASE per case in the window.

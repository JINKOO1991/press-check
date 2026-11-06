version: 2026-10-09.2

## Source profile: PAC 시정권고 (correction recommendation casebook or status board)
- institution: `pac`. authority_type: `pac_decision` only.
- outcome_type: `correction` when the text records a correction recommendation (시정권고), `decision` for another decision, `dismissal_procedural` (각하) or `dismissal_merits` (기각) when the matter was dismissed, `withdrawal` (취하) when it was taken back, otherwise `unknown`. A recommendation is not a finding that the article was unlawful in court.
- Rows from a status list may be short. Extract each row with a case number, a date or a measure as its own CASE. If a row carries only a title and a date, still output it with an empty outcome (`outcome` null).
- In a status table (rows with cells), a quote that is exactly one whole table cell (for example 주의 or 경고) is accepted even when it is shorter than 4 characters. The code checks that it equals one cell of a table shown in the page text. Any other quote shorter than 4 characters is discarded.
- The corrected article is rarely reproduced. Use `quoted_in_source` for original_article or corrected_article only when the wording itself appears in the document.

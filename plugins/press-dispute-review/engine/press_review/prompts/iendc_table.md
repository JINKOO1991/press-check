version: 2026-10-09.3

## Source profile: IENDC 심의결과 (Internet Newspaper Ethics Commission review results, table rows)
- institution: `iendc`. authority_type: `iendc_review` only.
- outcome_type: `decision`, `correction`, `guideline`, `dismissal_procedural`, `dismissal_merits`, `withdrawal` or `unknown`. A row with a measure (주의, 경고, 시정요구) is `decision` or `correction`. A row the commission did not take up (각하) is `dismissal_procedural`; a row examined and found not in violation (기각) is `dismissal_merits`; a request taken back (취하) is `withdrawal`.
- The text is often a table flattened to lines. One table row = one CASE. Do not merge two rows and do not split one row. A row normally has the newspaper, the article title, the standard applied and the measure (e.g. 주의, 경고, 심의제외).
- A row whose measure is 심의제외, 해당없음 or similar means no finding was made. Use outcome_type `unknown` or `decision` as the text supports; do not turn it into a finding of violation.
- If a table header appears in the window, do not output it as a case. If a row is cut by the window edge set `continues_after_window` or `starts_before_window`.
- When the same title appears in several rows, keep them as separate CASEs.
- A quote that is exactly one whole table cell (for example 주의 or 경고) is accepted even when it is shorter than 4 characters. The code checks that it equals one cell of a table shown in the page text, so copy the cell text only. Any other quote shorter than 4 characters is discarded.
- Quote a row-specific cell: a cell that belongs to this row alone (its own 언론사명, 보도제목, 위반내용 or 조치). A merged cell (rowspan: one cell shared by several rows, such as the election name or a total count) is not evidence about one row, so do not use it as the quote for a fact about a single case. If the same stretch of text is used as a factual quote for two or more cases in one window, the code discards that quote.

version: 2026-10-09.2

## Source profile: PAC 선거기사심의백서 (election news review white paper)
- institution: `pac`. authority_type: `pac_election_review` only.
- outcome_type: `decision`, `correction`, `guideline`, `dismissal_procedural`, `dismissal_merits`, `withdrawal` or `unknown`. A review decision with a measure (심의 의결: 주의, 경고, 시정요구) is `decision` or `correction`. A matter the committee did not take up (각하) is `dismissal_procedural`, a matter examined and rejected (기각, 문제 없음) is `dismissal_merits`, and a request taken back (취하) is `withdrawal`. A review decision is a decision of the election news review committee. Do not describe it as a court ruling and do not describe a 주의 or 경고 style measure as a finding of illegality unless the text says so.
- Entries may state the standard applied (심의기준) in `legal_reasoning` with speaker `institution`, and the measure taken in `outcome`.
- The reported article text is the 대상 기사. Put reproduced article wording in `disputed_spans` with speaker `article`.
- Dates of the review decision go in `decision_date`. Do not apply a past standard to present facts: record the standard as printed.

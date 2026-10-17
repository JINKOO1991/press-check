# P3 제외 첨부 chunk 누출 점검 (2026-10-16, 읽기 전용)

대상: 점검 당시 활성 snapshot `idx-735c398068d7d35e`(passage 89,170, 별칭 4,420; 2026-10-16 이후 활성은 `idx-06c1c3649636e7a2`, passage 89,756·별칭 3,834이며 이 점검은 06c1에 다시 돌리지 않았다). 장부는 `mode=ro`, 색인은 `mode=ro&immutable=1`로 열었다. 장부 chunk 94,200행을 첨부(`COALESCE(cases.att_id, pages.att_id)`)로 귀속하고 passage_id(=chunk_id)가 색인 passages에 있는지 대조했다.

## 결과: 누출 0
| 구분 | 첨부 | 장부 chunk(사건/쪽) | 색인 passage(사건/쪽) | 별칭만 남음 |
|---|---|---|---|---|
| 제외, 시정권고 현황(pac_cases 4) | 4 | 51 / 0 | 0 / 0 | 0 |
| 제외, IENDC | 80 | 559 / 0 | 0 / 0 | 0 |
| 제외, 다른 소스 | 0 | - | - | - |
| 미평가 시정권고 현황(사건 0건) | 132 | 0 / 0 | 0 / 0 | 0 |
| 미평가 IENDC(사건 0건) | 5 | 0 / 0 | 0 / 0 | 0 |

- 제외 admission 실패는 미해결 84행 전부 `attachment_excluded`이고 두 소스에만 있다(IENDC 80, 시정권고 현황 4). 제외 사건 chunk 610 = `case_att_excluded` 610과 일치한다.
- 제외·미평가 첨부에는 쪽(pages) 행이 있으나(제외 285쪽, 미평가 288쪽) 이 첨부들에서 쪽 단위 chunk(case_id 없음)는 장부에 한 행도 만들어지지 않았다. 그래서 누출이 없다.

## 코드상 잠복 구멍(수정하지 않음)
`corpus_sqlite.py:390` `if r["case_id"] and r["att_id_eff"] in adm_excluded:`가 case_id가 있는 chunk에만 첨부 제외를 적용한다. 쪽 단위 chunk(case_id NULL)는 `att_id_eff`(`_LEDGER_SQL` 345행, `COALESCE(ca.att_id, pg.att_id)`)가 제외 첨부여도 통과한다. 현재 장부에서는 해당 chunk가 없어 영향 0이지만, 이 두 소스에서 쪽 단위 chunk가 생기면(예: 쪽 단위 색인 경로 추가) 제외 첨부가 색인에 들어간다.
최소 수정 방향: 390행에서 `r["case_id"] and`를 빼 `att_id_eff`만으로 판정한다(case_att_excluded 집계 키 이름은 별도 결정). 수정 시 회귀 확인용으로 쪽 chunk 제외 테스트가 필요하다. 이번 점검에서는 코드를 바꾸지 않았다.

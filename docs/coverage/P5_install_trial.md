# P5 설치 시험 (코퍼스 없는 신규 사용자 재현)

일시 2026-10-17, 복제본 HEAD 09a55dc, 시험 폴더 scratchpad/install_trial/clone. 활성 색인은 건드리지 않음.

| 항목 | 결과 | 비고 |
|---|---|---|
| 1 git clone | PASS | 로컬 경로 복제, 복제본 기준 실행 |
| 2 매니페스트 | PASS | marketplace.json, plugin.json 유효. `claude plugin validate` 둘 다 통과 |
| 3a tools/list | PASS | 5개 도구 |
| 3b corpus_status | PASS | ok, is_fixture=true, "실제 코퍼스 미탑재" 표시 |
| 3c search_cases(허용 미설정) | PASS | 합성 근거 없음, 오류에 데이터 폴더·색인 경로·PRESS_DISPUTE_DATA_DIR·ALLOW_SYNTHETIC 안내 (색인 경로는 1번째 오류에서 "…"로 잘림) |
| 3d ALLOW_SYNTHETIC=1 | PASS | 합성 passage 4건(is_fixture=true) |
| 5 pytest | FAIL | 1166 passed, 23 failed, 1 xfailed (기준 1189/1) |
| 6 홈 경로 잔존 | 4개 파일 | docs/P0_DESIGN.md, docs/REUSE_DECISIONS.md, docs/coverage/P5_prerelease_audit.md, docs/recon/reuse_verify.md |

## 5번 실패 원인
tests/evals/test_error_injection.py 19건, test_eval_wiring.py 4건. evals/articles/ 가 .gitignore 대상이라 복제본에 없음(작업 트리에는 존재). 경로·환경 의존이며 코드 결함 아님. 공개 저장소 사용자는 같은 실패를 겪는다.

## 4번 입력 관련 메모
prepare_article 는 text 단독 입력이 "$.blocks: body block required"로 거부됨. 호출 인자 형식 확인 필요, 이번 시험은 blocks 형식으로 재시도하지 않음.

## 확인하지 못한 것
실제 Claude Code의 /plugin install 후 /mcp 연결, CLAUDE_PLUGIN_ROOT 치환과 ${PRESS_REVIEW_PYTHON:-python3} 확장 동작(직접 PYTHONPATH 지정으로 대체).

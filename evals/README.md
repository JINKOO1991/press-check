# 평가 하네스 (배선 검증용)

`scenarios.json`의 합성 시나리오 12개((개발 중 문서, 공개본에 미포함) 사본)는 하네스 배선과 행동 규칙을 시험하기 위한 가상 사례이며, 법률 정확도나 모델 성능을 재는 결과가 아니고 전문가 검증을 거친 정답셋도 아니다. `python evals/run_evals.py`는 `MockProvider`(합성 규칙) 출력에 기계 판정 규칙을 적용해 `evals/out/eval_mock.json`에 pass/fail/manual 표를 쓰고, mock 은 제목 단정 규칙 하나뿐이라 적극 판단이 필요한 사례는 finding 이 없어 fail 로 나온다(공급자 한계이며 규칙 오류가 아니다). `--source golden`은 `golden/`의 손으로 쓴 합성 기준 출력으로 규칙 자체가 올바른 출력을 받아들이는지 점검한다. 의미 판단이 필요한 기대(순수 의견과 숨은 사실 암시의 구별 등)는 `manual`이며 각 사례의 `manual_notes`에 이유를 적었다. 오류 주입 검사는 `tests/evals/`에 있고, 사실 추가·직접인용 변경·없는 출처·공익 보도 과잉수정 등을 검증기 또는 규칙이 잡는지 확인한다.

## P3 확장: 합성 기사 + 색인 passage 연결 (`run_p3_evals.py`)

`p3_scenarios.json`의 합성 시나리오 8건(수정이 필요 없는 공익 보도, 반론보도와 진실 여부의 구분, 의견·논평, 선거기간 심의기준 각 2건)은 합성 기사(`p3_articles/`)에 **색인 passage를 passage_id로만 연결**한다(`evidence_links`). 기사 본문은 전부 합성이고 실제 기사 원문을 쓰지 않는다. 기준 출력(`p3_golden/`)은 실제 판결·결정의 문장을 담지 않고 `{{quote|passage_id|시작|길이}}` 자리표시만 둔다. 실행할 때 사용자의 색인에서 사용자 화면 인용 상한 안의 구간을 가져와 채운다.

`python evals/run_p3_evals.py [--outputs-dir DIR] [--data-dir DIR]`는 `evals/out/eval_p3.json`에 pass/fail/`pending_corpus` 표를 쓴다. 연결된 passage가 현재 색인에 하나라도 없거나 합성 코퍼스뿐이면 그 시나리오는 `pending_corpus`이며 실패가 아니다(실자료 수집 뒤 다시 실행). `--outputs-dir`에 `<id>.json`(`findings`, `explanations`, 선택 `overall_status`, `coverage_notes`)을 두면 호스트 모델이 만든 출력을 같은 규칙으로 판정한다.

자동 판정 항목: 근거 passage가 색인에 있음(`P3_links_in_index`, `P3_cited_evidence_in_index`), 설명문 “…” 인용이 인용한 passage 원문(사용자 화면 인용 상한 안)과 일치(`P3_quotes_match_source`), 사실 추가 0(`G3`, `P3_no_added_facts`), 직접인용 보존(`G2`), 과잉수정(정답이 '수정 없음'인 시나리오에서 replace 0, 별 상한: `P3_no_overedit`, `P3_max_stars`), 범주별 분리 규칙(반론과 진실·위법 경로, 일괄 면책 주장 금지, 선거 근거의 기관·시기). 법률 정확도나 모델 성능을 재지 않는 배선 시험이다. 기존 12개 시나리오(`run_evals.py`)는 합성 코퍼스에 고정되어 있어 사용자 PC의 색인 유무와 무관하다.

## P4 검색 하네스 (`run_search_evals.py`)

`python evals/run_search_evals.py run [--index 색인파일] [--latency]`는 색인을 읽기 전용(`immutable=1`)으로 열어 `evals/search/`의 질의 세트(`regression_v0.json`: 순위 상수를 정한 tune 질의 24개, `gold_auto_dev.json`: 자동 gold dev 24개, `latency_load.json`: 지연 부하 12개)를 돌리고, 순위 상수 변형(`no_weights`·`no_demote`·`bigram_only`)을 질의별로 비교한다. 결과는 `evals/out/search_<snapshot>_<커밋>.json`(gitignore)이고, 재빌드 전 기준선은 `evals/baselines/`에 있다. `gen-gold`는 자동 gold(Q1 사건번호·Q2 조문·Q6 동형어 금지 사건 후보·Q7 무답)를 고정 seed로 다시 만든다. 사건 단위 관련성 등급이 없어 Recall·nDCG는 계산하지 않으며, 법률 정확도나 검색 품질을 인증하지 않는다. 기준선 색인 복사본은 `~/.local/share/press-dispute-review/baseline/`(저장소 밖, sha256 일치 확인)에 있다.

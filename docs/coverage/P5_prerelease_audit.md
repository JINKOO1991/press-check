# P5 공개 전 점검 (2026-10-17, 읽기 전용, 미커밋)

범위: git 82 커밋 전체, 추적 파일 302개. 기사 본문·비밀값은 옮기지 않았다. 점검자: mid-worker(sonnet), 명령 실행 기반.

| 항목 | 판정 |
|---|---|
| 1 저장소 내용 범위 | PASS (경미 지적 있음) |
| 2 시크릿·개인정보 | PASS (이력 비밀 0). 개인 이메일 메타데이터와 절대경로는 지적 |
| 3 기사 본문·원고 유출 | PASS (불명 1건 명시) |
| 4 라이선스·고지 | FAIL (공개 전 보강 필요) |
| 5 공개 사용자 경로 | FAIL (수집 CLI 문서 없음) |

## 1. 저장소 내용 범위: PASS
- 최대 파일 84KB(`evals/baselines/search_pool20_idx-34b8367d1dfe423d.json`), 상위 20개가 모두 코드·문서·평가 JSON이다. 이력 최대 blob도 84KB, 팩 크기 사실상 0.
- sqlite·db·jsonl·csv·xlsx·pdf·hwp·`.env`·`articles/`·`data/` 추적 파일 0건. 이력 전체 경로 목록에서도 삭제된 파일 0건, 위 확장자 0건(`.env.example` 2개만, 값 비어 있음).
- `.gitignore`(1~25행)가 `.env`, `data/raw/`, `data/index/`, `corpora/*`, `*.sqlite`, `*.db`, `articles/`, `runs/`, `logs/`를 막는다. `evals/out/`도 막는다. 지적: `corpus/`(수집 CLI 기본 출력 `corpus/inventory`)와 `data/` 하위 중 raw·index 외 경로는 무시 목록에 없다. `.pytest_cache`도 `.gitignore`에 있으나 `.venv`와 함께 작업 트리에 존재(추적 아님).
- `evals/p3_articles`(8)·`evals/p4_articles`(11)는 가상 합성 기사다(`context.verification_notes`가 합성 명시, 시나리오 `synthetic: True`, `not_a_legal_gold_label`). 실제 기사 아님.
- `docs/coverage/P4_pilot_responses_20261015.md`는 응답 2건의 집계표만 있고 자유 의견 원문·성명은 없다(문서 본문에 "저장소에 올리지 않고 로컬에만" 명시). `docs/outreach/*`는 연락처 칸이 `[성명]` 등 빈 템플릿.
- `docs/handoff/`(27개)는 작성자 소유 비공개 저장소의 이름·내부 경로·커밋을 다룬다. 비밀은 아니나 공개 의도인지 결정 필요. `docs/review/codex_r2_sol.md` 등 내부 리뷰 로그도 공개 여부 결정 필요.

## 2. 시크릿·개인정보: PASS (이력 비밀 0)
- 전 커밋 대상 패턴(`sk-`, `ghp_`, `github_pat_`, `AKIA`, `AIza`, `xox`, `-----BEGIN`) 적중 0. `*_API_KEY`·`LAW_OPEN_DATA_OC` 값 할당 적중 0. `.env.example` 값은 모두 비어 있거나 설정값뿐.
- 키 형태 이메일 스캔 적중은 테스트 더미뿐: `tests/ingestion/test_ing_http_policy.py:70` (`user:pw@`·`admin:s3cretpw@` URL 마스킹 테스트용 가짜 값)과 `git@github.com`(원격 URL 표기).
- 전화번호 패턴 적중 2건은 모두 오탐(`docs/coverage/lawapi_contract.md:35` 문구, `docs/review/codex_r2_sol.md:4` thread id 조각).
- 작성자 홈 디렉터리 절대경로: HEAD 3개 파일 6곳. 사용자명 노출이며 비밀은 아님. 공개 전 치환 권고.
- 지적(FAIL 아님): 이력 82 커밋의 author 메타데이터에 개인 이메일이 있고, 커밋 작성자 이름도 두 가지다. 공개하면 이메일이 노출된다. 의도 확인 필요, 필요하면 noreply 주소로 이력 재작성.

## 3. 기사 본문·원고 유출: PASS
- 이력 전체에서 `articles/`·`data/`·sqlite·jsonl·pilot 입력/출력 경로 0건. 파일럿 입출력은 `~/.local/share/press-dispute-review/eval/overwarn_pilot/`에만 있고 저장소에는 `P4_overwarning_pilot.md` 문서만 커밋됐다(문서 109행 서술과 경로 목록이 일치).
- 추적된 기사형 JSON 19개가 모두 합성이며 `evals/p4_pilot_batch1.json`도 "모두 가상 합성"(purpose 필드), 케이스에 `canary` 값 포함.
- 작성자 소유 비공개 코드를 언급한 파일 19개는 문서·`article_io.py`(이식 함수)이며 기사 데이터가 아니다.
- 불명: 작성자 본인의 발행 기사 풀(비공개)의 실제 기사 문장이 `P4_overwarning_pilot.md`나 다른 문서에 짧게 인용됐는지는 자동으로 대조하지 못했다(원본 풀에 접근하지 않았다). 비교 스크립트 없이 판정하지 않았다.

## 4. 라이선스·고지: FAIL
- 존재: 루트 `LICENSE`(MIT, "press-dispute-review contributors", 2026), `plugins/press-dispute-review/LICENSE`. README 라이선스 절이 MIT 명시.
- 없음: NOTICE·THIRD_PARTY 파일. 재사용 출처는 별도 이식 장부(비공개)에만 있다(이식은 `is_subtitle_block`·`_NUM` 계열 등 소량이며 코드 주석에 `이식:` 출처 표기). 이 원본 저장소들에는 LICENSE가 없고 사용자 소유라 MIT 재배포는 가능하나, 공개 배포물 루트에 출처 고지 파일이 없다. 권고: NOTICE 1개 추가(이식 목록과 소유자 동의 명시).
- 제3자 코드: 런타임 외부 패키지 없음(README 주장, `pyproject`·requirements 추적 파일 0건과 일치). 추출은 외부 도구(pdftotext, kordoc, tesseract)를 호출하나 번들하지 않음. 이 도구 설치 안내는 README에 없다.
- `.claude-plugin/marketplace.json`: name `press-dispute`, owner.name, plugins[0] source `./plugins/press-dispute-review` 있음. `plugin.json`: name, version `0.1.0`, description, author.name, license, keywords 있음. **`repository`·`homepage` 필드 없음**, author에 url·email 없음, owner에도 없음(필수 아님, 배포 품질상 결함).
- placeholder: `plugins/press-dispute-review/README.md:24,31,35`의 `<REPO_URL>` 잔존(공개 후 실제 `owner/repo`로 치환 필요). 루트 README.md는 없음(저장소 첫 화면이 비어 있음).

## 5. 공개 사용자 경로: FAIL
- `python -m press_review --help`는 동작(review/validate/corpus-status). `python -m press_review.ingestion --help`는 `PYTHONPATH=<plugin>/engine` 지정 시 동작하며 사용법은 `<target>`(어댑터 이름: pac, iendc, lawapi, lawapi_body, fetch, extract, build_index, withhold, robots_check, 예약어 status·export-inventory·export-coverage·admission)이다. `build_index`는 서브커맨드가 아니라 target이고 `build_index --help`는 상위 도움말을 출력한다. `build_index` 자체 옵션은 `--keep`(sources/build_index.py:12)뿐.
- README가 수집 CLI를 전혀 언급하지 않는다(`ingestion`, `build_index`, 수집 순서 grep 0건). "사용자가 수집 CLI로 로컬에 만든다"(README 9행)고만 쓰고 명령·순서·PYTHONPATH·소요·디스크 용량이 없다.
- DeepSeek 구조화 단계: 코드는 `DEEPSEEK_API_KEY`를 `secrets.py:18` 경로(환경변수 또는 `~/.config/deepseek/api_key`)에서 읽는다. 구조화 단계가 필수인지 선택인지는 README에 없다. 코드상 `providers/deepseek.py:359`가 호출 시점에 키를 읽는 구조라 해당 단계는 키가 있어야 돌아간다. 구조화 전 단계(추출) 결과만으로 색인을 만들 수 있는지는 확인하지 못했다(불명). `LAW_OPEN_DATA_OC`(법제처 인증키)도 README에 없다.
- 평가: 코퍼스 없이 설치하면 합성 fixture 코퍼스로 동작하므로 `/review`의 블록 분해·위치·해시·수정안 검사·표 렌더링 흐름은 쓸 수 있다. 그러나 근거는 실제 사례가 아니어서 법적 근거로 쓸 수 없고(README가 경고), 실제 근거를 얻으려면 문서화되지 않은 다단계 수집·API 키 설정을 직접 알아내야 한다. 현재 문서만으로는 공개 사용자가 실제 코퍼스를 구축할 수 없다.

## 권고 조치 (우선순위)
1. 수집·색인 빌드 절차를 README에 추가(명령 순서, `PYTHONPATH`, 필요 키와 필수/선택, 외부 도구, 소요·용량).
2. NOTICE 추가, `plugin.json`·`marketplace.json`에 repository·homepage·author url 추가, `<REPO_URL>` 치환 시점 결정, 루트 README 추가.
3. 절대경로 6곳 치환, `docs/handoff`·`docs/review` 공개 여부 결정, author 이메일 노출 여부 결정(재작성 시 이력 전체 영향).

## 확인하지 못한 것
- 실제 기사 문장의 문서 내 인용 여부(위 3항).
- DeepSeek 구조화 없이 색인 구축이 가능한지(5항).
- `git log -S` 전수는 하지 않았고 패턴 기반 `git grep` 전 커밋 스캔만 했다. 알려지지 않은 형식의 비밀은 탐지하지 못할 수 있다.
- 다른 워커가 수정 중인 `corpus_sqlite.py`(작업 트리 변경)는 HEAD 기준으로 읽었다.

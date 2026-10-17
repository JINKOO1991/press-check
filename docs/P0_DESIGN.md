# P0 설계 결정과 인터페이스 계약

작성 2026-10-08. 근거: (개발 중 문서, 공개본에 미포함), (개발 중 문서, 공개본에 미포함), (개발 중 문서, 공개본에 미포함). 이 문서는 P0 워커가 공유하는 계약이다. 바꾸려면 이 문서를 먼저 고친다.

## D1. 저장소 배치 (인수인계 ARCHITECTURE §8 수정)

공식 사양상 마켓 설치 시 `plugins/<name>/` 폴더만 캐시에 복사되고 밖의 파일은 복사되지 않는다(plugin_spec.md §6, §7 #2). 그래서 Python 코어를 플러그인 폴더 안에 둔다.

```text
.claude-plugin/marketplace.json          # 마켓 이름 "press-check", 엔트리 source "./plugins/press-check"
LICENSE                                  # MIT
plugins/press-check/
  .claude-plugin/plugin.json             # name "press-check", version "0.1.0", license "MIT"
  .mcp.json                              # 서버 이름 "press-review"
  LICENSE                                # MIT 사본(캐시 복사본에도 고지 유지)
  README.md                              # 사용자용 설치·사용·한계
  skills/review/SKILL.md                 # /press-check:review
  skills/explain/SKILL.md                # /press-check:explain
  skills/corpus-status/SKILL.md          # /press-check:corpus-status
  engine/press_review/                   # Python 패키지(아래 D3)
    schemas/                             # (개발 중 문서, 공개본에 미포함) 사본
    fixtures/                            # 합성 기사·합성 코퍼스(공개 가능 자료만)
tests/                                   # 저장소 루트, 캐시에 복사되지 않음
docs/
```

플러그인 루트에 CLAUDE.md를 두지 않는다(로드되지 않고 validate 경고).

## D2. 기사 분석 주체 = 호스트 모델 + 결정적 MCP 도구

기사를 읽고 문제 후보·수정안을 만드는 것은 Claude Code 호스트 모델이다(스킬이 지시). 엔진은 결정적 검사와 근거 검색만 한다. 미공개 원고를 DeepSeek 등 제3 제공자로 보내지 않는다. 원고는 사용자가 이미 대화에 넣은 호스트 모델에만 간다(README에 이 한계를 적는다).

`AnalysisProvider` 인터페이스는 두되 P0 구현은 `MockProvider`(합성 예제 전용 규칙 기반)뿐이다. 외부 API provider는 P3 이후, 계약·정책 확인 후.

MCP 도구(서버 `press-review`, 전부 stdio 로컬):

| 도구 | 입력 | 출력 | 규칙 |
|---|---|---|---|
| `prepare_article` | `text`(평문) 또는 `article`(article.schema 객체), 선택 `publication_date`, `genre`, `election_context` | `article_handle`, `article_sha256`, `hash_mode`, blocks(`block_id`,`section`,`text`,`length_cp`), 경고 | 평문은 첫 비어있지 않은 줄=title, 다음 짧은 줄(들)=subtitle 후보, 나머지 문단=body, `사진`/`[사진]`/`▲` 등으로 시작하는 줄=caption. 판정은 휴리스틱이므로 결과 blocks를 그대로 보여 주고 사용자가 바로잡게 한다. 원문은 서버 메모리에만, TTL 만료 시 삭제 |
| `search_cases` | `query`, 선택 `authority_types[]`, `risk_channel`, `as_of`, `limit`(≤10) | passages[] (`source_id`,`passage_id`,`url`,`locator`,`authority_type`,`decision_date`,`text`,`is_fixture`) | 코퍼스에 있는 레코드만 반환. 결과 0건이면 그렇게 말한다 |
| `submit_report` | `article_handle`, `findings[]`(review.schema finding에서 `evidence[].verified` 제외 가능), `explanations{finding_id: {...}}`, 선택 `overall_status`, `coverage_notes[]` | 통과 시 `report_id`, 렌더링된 표(markdown), 검증 경고. 실패 시 finding별 오류 목록 | 서버가 메타데이터(report_id, article_sha256, policy_version, corpus_snapshot_id, analyzed_at, legal_as_of, retention, hidden_finding_count)를 채운다. 서버만 `verified: true`를 붙인다(코퍼스에 source_id+passage_id 존재, url·locator 일치 시) |
| `explain_finding` | `report_id`, `finding_id` | 상세 설명 markdown | 같은 세션 TTL 안에서만. 만료면 원고 재제출 안내 |
| `build_revision` | `report_id`, `article_sha256`, 선택 `finding_ids[]` (대체문은 받지 않음) | 변경본 블록(`revised_blocks`), 적용 목록(`applied`), 미적용 제안(`proposals`)과 제외 사유(`skipped`), `unresolved_count`, 흐름 경고 | 읽기 전용. 별 2개 이상·`replace`·서버 재검사 통과 수정안만 보고서에 저장된 대체문으로 적용. 원본·디스크 무변경, TTL 안에서만 |
| `corpus_status` | 없음 | 코퍼스 snapshot id, 자료군별 건수, 기준일, 누락·권리보류 요약, fixture 여부 | |

## D3. 엔진 모듈(패키지 `press_review`, 런타임은 표준 라이브러리만)

런타임 의존성 0개. 이유: 공식 사양상 Python 의존성 자동 설치가 없고(plugin_spec §4), venv 부트스트랩 훅은 OS별 실패 지점과 네트워크 호출을 만든다. JSON Schema 검사는 런타임에 자체 검증기(`contracts.py`)로 하고, 테스트에서 `jsonschema`(Draft 2020-12)와 결과가 같은지 대조한다(dev 의존성).

Python 3.10 이상. Windows·macOS·Linux 경로는 `pathlib`.

| 모듈 | 공개 함수·클래스 |
|---|---|
| `textspan.py` | `cp_len(s)`, `cp_slice(s,start,end)`, `find_all(text, quote) -> list[(start,end)]` — 모두 Unicode code point 반개구간. Python str 인덱스가 code point이므로 그대로 쓰되 UTF-16 변환을 하지 않는다 |
| `hashing.py` | `sha256_bytes(b)`, `canonical_json(obj) -> bytes`(`json.dumps(sort_keys=True, ensure_ascii=False, separators=(",",":"))` UTF-8, 버전명 `cjson-1`), `article_sha256(article, raw_bytes=None) -> (hex, mode)`; mode는 `file_bytes` 또는 `cjson-1` |
| `contracts.py` | `load_schema(name)`, `schema_errors(instance, schema) -> list[str]`(이 프로젝트 스키마가 쓰는 키워드만: type/enum/const/required/properties/additionalProperties/items/minItems/minLength/maxLength/minimum/maximum/pattern/format(date, date-time, uri)/allOf/if/then), `validate_article(obj) -> list[str]`(스키마 + title·body 각 1개 이상 + block_id 유일), `validate_report(report, article, corpus) -> list[str]`(스키마 + contracts/README.md 교차검증 1~7) |
| `edit_checks.py` | `check_replacement(quote, replacement, article) -> list[Issue]` — 새 숫자·날짜·금액, 새 따옴표 인용, 원문에 없는 고유명사 후보(원문 토큰 집합 대조), 부정어(안/않/없/아니/못/불·무 접두 제외) 개수 변화, 직접인용 구간 내부 변경을 검출. 판정은 경고(의미 판단은 호스트 모델과 사람 몫) 단, 직접인용 내부 변경·원문에 없는 숫자 추가는 오류 |
| `article_io.py` | `parse_plaintext(text, **ctx) -> article`, `load_article(path)`(.json/.txt/.md; 파일 바이트 해시 사용) |
| `store.py` | `EphemeralStore(ttl_seconds=1800)` — 메모리 dict, 디스크 기록 없음, `put/get/purge_expired`. 기사·보고서·설명 보관 |
| `render.py` | `render_table(report, top_n=5) -> str` 열: `문제 요소 | 검토 필요도 | 사유 | 추천 수정안`. 첫 줄에 "별(★)은 실제 분쟁 발생 확률이 아니라 검토 우선순위입니다." 정렬: 별 내림차순→원문 위치. 숨김 건수 표시. `verify_first`는 수정안 칸에 "발행 전 확인: …". `render_detail(report, finding_id, explanation)` |
| `corpus.py` | `Corpus` 인터페이스(`get_passage`, `search`, `snapshot_id`, `status`), `FixtureCorpus`(fixtures/corpus.json), 실제 코퍼스는 P1·P2에서 SQLite로 같은 인터페이스 구현. 검색은 P0에서 단순 토큰 겹침 점수 |
| `providers/base.py`, `providers/mock.py` | `AnalysisProvider.analyze(article, corpus) -> (findings, explanations)`; Mock은 합성 예제 규칙(제목의 단정 표현 vs 본문 '혐의/수사' 등) |
| `review.py` | `build_report(article, findings, explanations, corpus, *, now, raw_bytes=None) -> (report, errors)` — submit_report와 CLI가 공유 |
| `secrets.py` | `load_secret(name)`: `DEEPSEEK_API_KEY` → env, 없으면 `~/.config/deepseek/api_key`; `LAW_OPEN_DATA_OC` → env, 없으면 `~/.config/press-dispute-review/law_oc`. 파일 권한 0600 아니면 거부. 값은 반환만, 로그·예외 메시지에 넣지 않는다 |
| `logsafe.py` | 로거 필터: 등록된 기사 블록 텍스트(8자 이상 부분)가 포함된 메시지는 `[redacted]`로 치환. 기본 로그는 요청 ID·해시·건수만 |
| `config.py` | 데이터 디렉터리: env `PRESS_DISPUTE_DATA_DIR` → env `CLAUDE_PLUGIN_DATA`(경로에 `press-check`가 있을 때만, P2 §8) → `~/.local/share/press-dispute-review`. 코퍼스는 저장소 밖 |
| `cli.py` (`python -m press_review`) | `review --input F [--provider mock] [--format table|json]`, `validate --article F [--report F]`, `corpus-status` |
| `mcp_server.py` (`python -m press_review.mcp_server`) | 표준 라이브러리 JSON-RPC 2.0 stdio(줄 단위 JSON). `initialize`(클라이언트가 보낸 protocolVersion을 지원 목록에서 협상), `notifications/initialized`, `tools/list`, `tools/call`, `ping`. stdout에는 프로토콜 메시지만, 진단은 stderr. 도구 인자와 기사 텍스트를 로그에 쓰지 않는다 |

## D4. 정책 고정값

- 별 의미: ★ 확인 권고 / ★★ 수정·보강 검토 / ★★★ 발행 전 집중 검토. 확률 아님. % 필드·출력 없음(`PRESS_DISPUTE_ENABLE_PERCENTAGE`는 무시하고 경고만).
- 기본 표시 5건, 나머지는 `hidden_finding_count`와 "더 보기" 안내.
- `reason_short` ≤100자 한 문장.
- 근거가 없으면 `evidence: []` 허용하되 `validation_status: needs_information`, `review_confidence: low`로 강제하고 표에 "근거 미확보" 표시.
- 원고 기본 무보관: 디스크에 원문·보고서를 쓰지 않는다. TTL 30분 메모리.
- 수집 문서·원고 속 지시문은 데이터로 취급(스킬에 명시).

## D5. 비밀값

OC는 `~/.config/press-dispute-review/law_oc`(0600), DeepSeek 키는 `~/.config/deepseek/api_key`(0600). 저장소·로그·프롬프트·테스트 출력에 값이 나오면 안 된다. 테스트는 가짜 값을 임시 HOME으로 주입한다.

## D6. 재사용 원칙(P0 범위)

P0에서 검토할 후보: 작성자 소유 비공개 코드의 평문 기사 분해 함수와 생성문 검사 함수. 가져오면 모듈 상단에 출처 주석("이식: 작성자 소유 코드(비공개)")을 단다. 맞지 않으면 새 구현으로 쓰고 사유를 적는다. 개인 경로·이름·키·코퍼스는 가져오지 않는다.

## D7. 구현 중 추가된 정책(2026-10-08 오케스트레이터 승인)

P0 엔진 워커가 계약에 없던 값을 정했고 아래와 같이 승인한다. 바꾸려면 이 절을 고친다.
- `local_encrypted` 보관 모드는 P0에서 거부한다(구현 전).
- `related_edits`는 mode `replace`일 때만 허용한다.
- 코퍼스가 fixture이거나 비어 있으면 `no_actionable_findings`를 금지한다(→ `partial`/`insufficient_information`).
- 보관 창 상한은 24시간, 기본 TTL은 30분이다.
- `reason_short`는 한 문장이다.
- `detailed_explanation_ref`는 `report_id/finding_id`로 한다.
- evidence의 기준일(reference_date: decision_date, 없으면 수록 간행물의 발행일, P2 G-D23)은 legal_as_of 이하여야 한다. 기준일이 없는 근거(`date_unknown`)는 쓸 수 있으나 보고서 coverage_notes에 그 사실을 남긴다.
- fixture 근거는 `synthetic_example` 보고서에만 쓸 수 있다.
- `hidden_finding_count`는 max(0, 전체−5)이다.
- 해시 모드는 coverage_notes 한 줄로 기록한다(스키마 필드 없음).
- 설명 객체 키: issue, article_context, supporting_evidence, case_differences, counter_arguments, required_checks, uncertainty.

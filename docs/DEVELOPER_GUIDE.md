# 개발자 안내: 코퍼스 수집·구조화·번들 만들기

이 문서는 저장소를 내려받아 코퍼스를 만드는 개발자용입니다. 마켓플레이스로 플러그인을 설치하는 기자는 읽을 필요가 없습니다. 설치자는 구조화가 끝난 번들 색인만 받습니다(사용자 결정 UDEC-25, UDEC-26, `docs/DECISIONS.md`).

## 어디에 무엇이 있나

| 위치 | 내용 | 마켓플레이스 설치에 포함 |
|---|---|---|
| `plugins/press-check/` | MCP 서버, 스킬, 색인 검색 엔진, 번들 설치 명령(`install-bundle`) | 포함 |
| `devtools/engine/press_review/ingestion/` | 수집 어댑터(언론중재위원회·인터넷선거보도심의위원회·법제처·헌법재판소 API·법고을), 추출, 권리 장부, 구조화 호출 | 제외 |
| `devtools/engine/press_review/providers/deepseek.py` | DeepSeek 구조화 제공자 | 제외 |
| `devtools/engine/press_review/prompts/`, `prices/` | 구조화 프롬프트와 DeepSeek 단가 snapshot | 제외 |
| `devtools/engine/press_review/bundle_build.py` | 번들 색인 만들기 | 제외 |

`devtools/engine/press_review/`에는 `__init__.py`가 없습니다. `PYTHONPATH`에 `plugins/press-check/engine`과 `devtools/engine`을 함께 두면 같은 이름(`press_review.ingestion`)으로 두 폴더가 한 패키지로 합쳐집니다(`pkgutil.extend_path`). 시험은 `tests/conftest.py`가 이 경로를 더합니다. `tests/mcp/test_plugin_isolation.py`가 플러그인 폴더에 수집·DeepSeek 파일과 import가 없음을 확인합니다.

## 코퍼스 만들기(수집·색인)


저장소에는 코퍼스가 없습니다. 실제 근거를 쓰려면 아래 순서로 로컬에 만듭니다. 각 기관 자료의 이용정책·권리 표시는 사용자가 직접 확인해야 하며, 수집물은 사용자 로컬에만 보관되고 저장소에 포함되지 않습니다. 신규 설치의 권리 기본값은 `unknown`이며 사용자가 `rights set`으로 직접 기록해야 외부 API 반출이 열립니다.

- 실행 위치: 저장소 루트에서 `PYTHONPATH=plugins/press-check/engine:devtools/engine python3 -m press_review.ingestion <target>`. 수집물은 `<data>/corpus/`, 색인은 `<data>/index/`에 생기며 `<data>`는 `PRESS_DISPUTE_DATA_DIR`(기본 `~/.local/share/press-dispute-review`)입니다. 데이터 폴더 기본 이름은 이전 호환을 위해 press-dispute-review를 유지한다.
- 수집·추출 target: `pac`, `iendc`, `lawapi`, `lawapi_body`, `fetch`, `extract`, `withhold`, `robots_check`. 상태 확인은 `status`, `export-inventory`, `export-coverage`, `admission`. 전체 목록은 `--help`로 확인하세요. 이 단계에는 DeepSeek 키가 필요하지 않습니다.
- **권리 기록** `rights show`는 소스별 현재 권리 값과 기록자·시각·근거를 보여 줍니다. `rights set <source_family> --external-api allowed --note "근거 한 줄"`는 그 계열로 등록된 소스(수집 target을 한 번 실행해 소스가 장부에 있어야 합니다)에 결정을 기록합니다. 다른 권리 항목은 `--read`, `--store-raw`, `--create-derivatives`, `--redistribute-raw`, `--redistribute-derivatives`, `--fine-tune`이고 값은 `allowed|forbidden|permission_required|unknown`입니다. 지정하지 않은 항목은 그대로 두며, `forbidden`은 풀 수 없고, `unknown`을 허용으로 바꾸는 자동 경로는 없습니다. 이 결정은 법률 판단이 아니라 사용자의 기록이며 근거 메모에 책임이 따릅니다.
- **구조화** `PYTHONPATH=plugins/press-check/engine:devtools/engine python3 -m press_review.ingestion.structure`: 색인은 구조화가 만든 chunk만 읽으므로, 구조화 없이는 색인에 자료가 들어가지 않습니다(코드 확인, 실행해 보지는 않았습니다). 심의기준·위원회 규정·PAC 연구자료는 모델 호출 없이 chunk만 만들 수 있고, PAC 사례집·IENDC·판례는 `DEEPSEEK_API_KEY`가 필요합니다. 이 단계는 소스의 `rights.external_api`가 `allowed`일 때만 외부로 전송하며, 신규 설치에서는 `unknown`이라 `rights set` 전에는 전송하지 않습니다.
- 디스크 가드: 쓰기가 큰 target(`--dry-run` 제외)은 시작 전에 Windows C:(`/mnt/c`) 여유를 확인합니다. 환경변수 `PRESS_DISK_MIN_FREE_GB`(기본 10), `PRESS_DISK_MARGIN_GB`(기본 5), `PRESS_DISK_PATH`(측정 경로)로 조정하고, 부족하면 종료 코드 3, 측정 불가면 4로 멈춥니다. 진행 중 부족하면 `paused_disk_*` 상태로 정지해 이어받을 수 있습니다. `--force-disk`는 이 확인을 우회하며 기본은 꺼짐입니다.
- 색인: `build_index` target은 네트워크·외부 API·비밀값을 쓰지 않습니다.
- 키: `DEEPSEEK_API_KEY`(구조화), `LAW_OPEN_DATA_OC`(법제처 목록·본문 조회)는 환경변수로만 받고 출력·Git에 넣지 마세요.
- PDF·HWP 추출에는 외부 도구(poppler-utils, tesseract, kordoc 등)가 쓰일 수 있습니다. 필요한 범위는 확인하지 못했습니다.

## 번들 만들기와 릴리스

설치자에게 줄 번들 색인은 활성 색인에서 `redistributable=1`인 passage만 담아 만듭니다. 운영 색인(`CURRENT`)은 읽기만 하고 바꾸지 않습니다.

```text
PYTHONPATH=plugins/press-check/engine:devtools/engine python3 -m press_review.bundle_build \
  --index <data>/index --out <새 폴더> [--exclude-docs 제외문서목록.txt]
```

- `--index`는 색인 파일(`corpus-idx-*.sqlite`) 또는 색인 폴더입니다. 폴더이면 `CURRENT`가 가리키는 색인을 엽니다. `--out`은 새 폴더여야 하고 `CURRENT`가 있는 색인 폴더는 거부합니다.
- `--exclude-docs`: 제외할 문서 id를 한 줄에 하나씩 적은 파일(`#` 줄은 주석, id는 색인 passage의 `source_id`, 예 `lawgo_prec:900001`). 자연인 실명이 남은 판례·헌재 문서를 전수 검수해 이 목록으로 뺍니다(UDEC-26). 실명을 치환하지 않습니다. 치환하면 인용 원문 일치 검증이 깨지기 때문입니다. 목록의 id 중 색인에 없는 것은 결과 JSON의 `unmatched_exclusions`에 나옵니다.
- 출력: `corpus-<snapshot>.sqlite`, `corpus-<snapshot>.sqlite.sha256`, `bundle-info.json`(파일 이름, sha256, 크기, 건수, 제외 건수, 자료군별 통계). 새 파일에 행을 복사해 제외한 본문이 빈 페이지에 남지 않습니다.
- `cases_by_authority_type`은 색인에 사건 키가 없어 담은 passage의 서로 다른 사건번호 수로 셉니다(사건번호 없는 사건이 빠지는 하한). 이 방식은 `bundle-info.json`의 `cases_count_method`와 색인 meta의 `bundle_cases_method`에 적힙니다.
- 번들은 Git 이력 밖의 교체 가능한 릴리스 자산으로 올립니다(UDEC-26). 삭제 요청은 7일 이내에 조치합니다(UDEC-27). 올릴 위치(릴리스 URL)는 아직 정하지 않았습니다. 정하면 `plugins/press-check/engine/press_review/config.py`의 `BUNDLE_RELEASE_BASE_URL`(지금은 자리표시자)을 바꿉니다.
- 번들에 언론중재위원회·인터넷선거보도심의위원회 자료는 들어가지 않습니다. 재배포 허락이 확인되지 않았기 때문입니다.
- 설치자가 쓰는 명령은 `python3 -m press_review install-bundle`입니다. 판본 정보를 읽고 색인 파일을 받아 sha256과 크기를 확인한 뒤에만 `CURRENT`를 바꾸며, 받기 실패와 해시 불일치에서는 기존 색인을 건드리지 않습니다.

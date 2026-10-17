"""경로와 정책 상수. 환경변수 해석은 여기에만 둔다."""
from __future__ import annotations

import os
from pathlib import Path

SCHEMA_VERSION = "0.1"
POLICY_VERSION = "draft-0.1"
DISPLAY_TOP_N = 5          # 첫 화면 표시 건수(D4)
DEFAULT_TTL_SECONDS = 1800  # 세션 메모리 보관 30분(D4)
REASON_MAX_CHARS = 100
MAX_STORE_ITEMS = 16        # 서버 메모리에 두는 원고·보고서 합계 상한(F2). 넘으면 가장 오래된 것부터 제거

PACKAGE_DIR = Path(__file__).resolve().parent
SCHEMAS_DIR = PACKAGE_DIR / "schemas"
FIXTURES_DIR = PACKAGE_DIR / "fixtures"


def _own_plugin_data(raw: str) -> bool:
    """CLAUDE_PLUGIN_DATA 가 이 플러그인의 것인지: 경로 구성요소 중 하나에 'press-dispute-review' 가 있어야 한다.

    Claude Code 셸에는 다른 플러그인의 CLAUDE_PLUGIN_DATA 가 상속될 수 있다(2026-10-08 실측: codex 플러그인 경로).
    """
    return any("press-dispute-review" in part for part in Path(raw).parts)


def default_data_dir() -> Path:
    """환경변수가 없을 때의 기본 데이터 위치."""
    return Path.home() / ".local" / "share" / "press-dispute-review"


def data_dir() -> Path:
    """코퍼스 등 영속 자료의 위치(저장소 밖). 만들지는 않는다. 엔진과 수집 CLI가 이 함수 하나를 쓴다.

    우선순위: PRESS_DISPUTE_DATA_DIR -> CLAUDE_PLUGIN_DATA(경로에 press-dispute-review 가 있을 때만)
    -> ~/.local/share/press-dispute-review
    """
    v = os.environ.get("PRESS_DISPUTE_DATA_DIR", "").strip()
    if v:
        return Path(v).expanduser()
    v = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if v and _own_plugin_data(v):
        return Path(v).expanduser()
    return default_data_dir()


def allow_synthetic() -> bool:
    """PRESS_REVIEW_ALLOW_SYNTHETIC=1 이면 실색인이 없을 때 MCP 서버가 합성(fixture) 코퍼스 근거를 돌려준다. 기본은 거부."""
    return os.environ.get("PRESS_REVIEW_ALLOW_SYNTHETIC", "").strip() == "1"


def percentage_requested() -> bool:
    """PRESS_DISPUTE_ENABLE_PERCENTAGE가 켜져 있는지. 켜져 있어도 % 출력은 구현하지 않고 경고만 한다(D4)."""
    return os.environ.get("PRESS_DISPUTE_ENABLE_PERCENTAGE", "").strip().lower() in ("1", "true", "yes", "on")


def private_mkdir(path) -> None:
    """없는 폴더만 0700으로 만든다(F5). 이미 있는 폴더의 권한은 바꾸지 않는다."""
    p = Path(path)
    missing = []
    while not p.exists() and p not in missing:
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            continue
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass

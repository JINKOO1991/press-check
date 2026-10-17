"""비밀값 읽기. 값은 반환만 하고 로그·예외 메시지·repr에 넣지 않는다.

이 모듈 이름은 표준 라이브러리 ``secrets`` 와 같다. 패키지 안에서는 항상 ``from . import secrets`` 로만 쓰고,
``engine/press_review`` 폴더를 sys.path 에 직접 넣지 않는다(엔진 루트 ``engine/`` 를 path 에 둔다).

환경변수 우선, 없으면 파일. 파일은 현재 사용자 소유의 일반 파일이고 권한이 정확히 0600 이어야 한다(P0_DESIGN D3: 0600 아니면 거부).
Windows 는 POSIX 권한 비트가 의미 없어 권한·소유자 검사를 건너뛴다(알려진 한계).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from . import logsafe

_SECRETS = {
    "DEEPSEEK_API_KEY": ("DEEPSEEK_API_KEY", Path(".config") / "deepseek" / "api_key"),
    "LAW_OPEN_DATA_OC": ("LAW_OPEN_DATA_OC", Path(".config") / "press-dispute-review" / "law_oc"),
}


# 평범한 단어일 수 있어 경로·본문에 우연히 나와도 메시지를 지우지 않는 비밀값(문맥 안의 값만 마스킹한다).
_PARAM_SECRETS = frozenset({"LAW_OPEN_DATA_OC"})


def _register(name: str, value: str) -> None:
    logsafe.register_secret(value, context_only=name in _PARAM_SECRETS and logsafe.is_plain_word(value))


class SecretError(Exception):
    """메시지에 비밀값이나 파일 내용을 넣지 않는다."""


def _display(rel: Path) -> str:
    return "~/" + rel.as_posix()


def load_secret(name: str) -> str:
    if name not in _SECRETS:
        raise SecretError(f"unknown secret name '{name}'")
    env_name, rel = _SECRETS[name]
    v = os.environ.get(env_name, "").strip()
    if v:
        _register(name, v)
        return v
    path = Path.home() / rel
    shown = _display(rel)
    try:
        st = path.stat()
    except FileNotFoundError:
        raise SecretError(f"{name} not set (env {env_name} or file {shown})") from None
    except OSError:
        raise SecretError(f"{name} file {shown} is not accessible") from None
    if not stat.S_ISREG(st.st_mode):
        raise SecretError(f"{name} file {shown} is not a regular file")
    if os.name != "nt":
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise SecretError(f"{name} file {shown} must have permissions exactly 0600 (chmod 600)")
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise SecretError(f"{name} file {shown} is not owned by the current user")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise SecretError(f"{name} file {shown} could not be read") from None
    if not value:
        raise SecretError(f"{name} file {shown} is empty")
    _register(name, value)
    return value

"""로그 안전장치: 원고 문구와 비밀값이 로그에 남지 않게 한다.

- ``register_article(key, article)``: 등록된 기사 블록 텍스트와 8자(code point) 이상 겹치는 부분이 있는 로그 메시지는
  메시지 전체를 ``[redacted]`` 로 바꾼다. 8자 조각 색인을 메모리에만 둔다.
- ``register_secret(value)``: 비밀값이 메시지에 들어 있으면 같은 방식으로 치환한다.
- ``register_secret(value, context_only=True)``: 평범한 단어인 비밀값(법제처 OC 등)용. 값이 경로·본문에 우연히 들어 있어도
  메시지를 지우지 않고, ``OC=값``·``<OC>값</OC>``·``"OC":"값"`` 문맥의 값만 ``[MASKED]`` 로 바꾼다(``mask_param_secrets``).
- ``log_event``: 기본 로그는 요청 ID·해시·건수 같은 허용 필드만 남긴다.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
import traceback
import urllib.parse
from typing import Any

MIN_MATCH = 8
MAX_REGISTERED = 256
REDACTED = "[redacted]"
ALLOWED_FIELDS = ("request_id", "tool", "article_sha256", "report_id", "count", "hidden", "status", "ms", "error_code")

_lock = threading.Lock()
_articles: dict[str, set[str]] = {}
_secrets: set[str] = set()
_param_secrets: set[str] = set()
MASK = "[MASKED]"


def _shingles(text: str) -> set[str]:
    return {text[i:i + MIN_MATCH] for i in range(0, len(text) - MIN_MATCH + 1)}


def register_article(key: str, article: Any) -> None:
    sh: set[str] = set()
    for b in article.get("blocks", []):
        sh |= _shingles(b["text"])
    with _lock:
        _articles.pop(key, None)
        _articles[key] = sh
        while len(_articles) > MAX_REGISTERED:
            _articles.pop(next(iter(_articles)))


def forget_article(key: str) -> None:
    with _lock:
        _articles.pop(key, None)


def register_secret(value: str, *, context_only: bool = False) -> None:
    """context_only=False(기본): 값이 메시지에 보이면 메시지 전체 치환. True: 파라미터 문맥의 값만 마스킹."""
    if value:
        with _lock:
            (_param_secrets if context_only else _secrets).add(value)


def is_plain_word(value: str) -> bool:
    """글자(알파벳·한글)로만 이루어진 값. 이런 값은 경로·본문에 우연히 나올 수 있어 문맥 밖 출현을 단서로 삼지 않는다."""
    return bool(value) and value.isalpha()


def mask_contexts(text: str, value: str) -> str:
    """text 안의 ``OC=값``·``OC%3D값``·``<OC>값</OC>``·``"OC":"값"`` 문맥에서 값만 [MASKED] 로 바꾼다. 그 밖의 출현은 건드리지 않는다."""
    if not value or not text:
        return text
    forms = {value, urllib.parse.quote(value), urllib.parse.quote(value, safe="")}
    alt = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    guard = r"(?![A-Za-z0-9_])"
    pats = [
        (rf"(?i)((?<![A-Za-z0-9_])OC(?:=|%3D)\s*)(?:{alt}){guard}", rf"\1{MASK}"),
        (rf"(?i)(<OC\s*>\s*)(?:{alt})(\s*</OC\s*>)", rf"\1{MASK}\2"),
        (rf"(?i)([\"']OC[\"']\s*:\s*[\"'])(?:{alt})([\"'])", rf"\1{MASK}\2"),
    ]
    for pat, rep in pats:
        text = re.sub(pat, rep, text)
    return text


def mask_param_secrets(text: str) -> str:
    with _lock:
        vals = list(_param_secrets)
    for v in vals:
        text = mask_contexts(text, v)
    return text


def reset() -> None:
    with _lock:
        _articles.clear()
        _secrets.clear()
        _param_secrets.clear()


def contains_sensitive(msg: str) -> bool:
    with _lock:
        secrets_ = list(_secrets)
        sets = list(_articles.values())
    if any(s in msg for s in secrets_):
        return True
    if len(msg) < MIN_MATCH or not sets:
        return False
    for i in range(0, len(msg) - MIN_MATCH + 1):
        w = msg[i:i + MIN_MATCH]
        if any(w in s for s in sets):
            return True
    return False


class RedactFilter(logging.Filter):
    """핸들러에 붙인다(로거에 붙이면 자식 로거 기록에는 적용되지 않는다)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        masked = mask_param_secrets(msg)
        hit = contains_sensitive(masked)
        exc_text = None
        if not hit and record.exc_info:
            try:
                exc_text = "".join(traceback.format_exception(*record.exc_info))
                hit = contains_sensitive(exc_text)
            except Exception:
                hit = True
        if not hit and record.stack_info:
            hit = contains_sensitive(record.stack_info)
        if hit:
            record.msg, record.args = REDACTED, ()
            record.exc_info = record.exc_text = record.stack_info = None
        else:
            if masked != msg:
                record.msg, record.args = masked, ()
            if exc_text is not None and mask_param_secrets(exc_text) != exc_text:
                record.exc_text, record.exc_info = mask_param_secrets(exc_text), None
        return True


def make_handler(stream: Any = None) -> logging.Handler:
    h = logging.StreamHandler(stream or sys.stderr)
    h.addFilter(RedactFilter())
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    return h


def configure(level: int = logging.INFO, stream: Any = None) -> logging.Logger:
    """'press_review' 로거에 필터가 붙은 stderr 핸들러를 단다(중복 호출은 한 번만)."""
    lg = logging.getLogger("press_review")
    lg.setLevel(level)
    lg.propagate = False
    if not any(isinstance(f, RedactFilter) for h in lg.handlers for f in h.filters):
        lg.addHandler(make_handler(stream))
    return lg


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """허용 필드(요청 ID, 해시, 건수 등)만 ``key=value`` 로 남긴다. 그 밖의 키는 버린다."""
    kept = " ".join(f"{k}={fields[k]}" for k in ALLOWED_FIELDS if k in fields)
    logger.info("%s %s", event, kept)

"""jsonschema(Draft 2020-12) 기준 검증기. date-time/uri 형식 검사기(rfc3339-validator, rfc3987)는 설치하지 않으므로
이 파일의 기준 검사기를 contracts.py 와 독립적으로 등록한다."""
import datetime
import re
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker


def ref_datetime(s):
    """RFC 3339 §5.6: T와 Z는 소문자도 허용한다(NOTE)."""
    if not isinstance(s, str):
        return True
    if len(s) < 20 or s[10] not in "Tt":
        return False
    t = s[:10] + "T" + s[11:]
    t = t[:-1] + "+00:00" if t[-1] in "Zz" else t
    if t[17:19] == "60":  # RFC 3339 §5.6: 윤초 허용
        t = t[:17] + "59" + t[19:]
    if not re.search(r"[+-]\d\d:\d\d$", t):
        return False
    try:
        d = datetime.datetime.fromisoformat(t)
    except ValueError:
        return False
    return d.tzinfo is not None


def ref_uri(s):
    """RFC 3986 §2.1: 퍼센트 인코딩은 '%' 뒤에 16진수 두 자리."""
    if not isinstance(s, str):
        return True
    if not s.isascii() or re.search(r"[\s<>\"{}|\\^`]", s):
        return False
    if re.search(r"%(?![0-9A-Fa-f]{2})", s):
        return False
    p = urlsplit(s)
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9+.\-]*", p.scheme or ""))


def make_checker():
    c = FormatChecker()
    c.checks("date-time")(ref_datetime)
    c.checks("uri")(ref_uri)
    return c


CHECKER = make_checker()


def ref_validator(schema):
    return Draft202012Validator(schema, format_checker=CHECKER)


def path_of(err):
    out = "$"
    for p in err.absolute_path:
        out += f"[{p}]" if isinstance(p, int) else f".{p}"
    return out


def ref_paths(schema, instance):
    return {path_of(e) for e in ref_validator(schema).iter_errors(instance)}

"""search_cases 응답 층의 이메일·전화번호 가림. corpus 계약과 색인은 바꾸지 않는다.

패턴은 형식이 뚜렷한 연락처만 잡는다. 판결문의 사건번호(2018다12345)·금액(1,234,567원)·날짜(2019. 1. 2.)·법령번호(제17354호)와 겹치지 않도록
전화번호는 0 으로 시작하는 국내 번호(구분자 '-'·'.' 또는 붙여 쓴 휴대전화)와 +82 국제 표기만 본다. 공백으로만 구분한 번호와 1588 류 대표번호는 가리지 못한다.
"""
from __future__ import annotations

import re

EMAIL_MASK = "[이메일 가림]"
PHONE_MASK = "[전화번호 가림]"

# 시작 경계(앞 글자가 로컬파트 문자가 아님)와 길이 상한(로컬 64·도메인 레이블 63, RFC 5321)으로 긴 비이메일 입력에서도 선형에 가깝게 돈다.
# 로컬파트가 64자를 넘는 주소는 가리지 못한다. 도메인은 레이블 6개까지만 본다.
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){1,5}")
_PHONE = re.compile(
    r"(?<![\d\-.])(?:\+82[-. ]?\(?0?\d{1,2}\)?|\(?0\d{1,2}\)?)[-. ]?\d{3,4}[-.]\d{4}(?![\d\-])"     # 구분자가 있는 번호: 02-123-4567, (031) 123-4567, +82-10-1234-5678
    r"|(?<![\d\-.])01[016-9]\d{7,8}(?!\d)"                                                           # 붙여 쓴 휴대전화
)


def mask_text(text: str) -> tuple[str, int]:
    """(가린 문자열, 가린 건수)."""
    n = 0

    def sub(mask):
        def f(_m):
            nonlocal n
            n += 1
            return mask
        return f

    text = _EMAIL.sub(sub(EMAIL_MASK), text)
    text = _PHONE.sub(sub(PHONE_MASK), text)
    return text, n


def contact_spans(text: str) -> list[tuple[int, int, str]]:
    """원문 text 에서 가릴 연락처의 (시작, 끝, 가림 표시). 이메일과 겹치는 전화번호 후보는 뺀다. 시작 순."""
    spans = [(m.start(), m.end(), EMAIL_MASK) for m in _EMAIL.finditer(text)]
    for m in _PHONE.finditer(text):
        if not any(m.start() < e and s < m.end() for s, e, _ in spans):
            spans.append((m.start(), m.end(), PHONE_MASK))
    return sorted(spans)


def mask_fragment(full: str, start: int, end: int) -> tuple[str, int]:
    """원문 full 의 구간 [start, end) 를 가려 돌려준다. 연락처를 원문에서 먼저 찾으므로 구간 경계가 연락처 중간을 잘라도
    구간에 보이는 조각은 가림 표시로 바뀐다. (가린 문자열, 구간과 겹친 연락처 건수)."""
    out, pos, n = [], start, 0
    for s, e, mask in contact_spans(full):
        if e <= start or s >= end:
            continue
        out.append(full[pos:max(s, start)])
        out.append(mask)
        pos, n = min(e, end), n + 1
    out.append(full[pos:end])
    return "".join(out), n

"""search_cases 응답 층의 이메일·전화번호 가림. corpus 계약과 색인은 바꾸지 않는다.

패턴은 형식이 뚜렷한 연락처만 잡는다. 판결문의 사건번호(2018다12345)·금액(1,234,567원)·날짜(2019. 1. 2.)·법령번호(제17354호)와 겹치지 않도록
전화번호는 0 으로 시작하는 국내 번호(구분자 '-'·'.' 또는 붙여 쓴 휴대전화)와 +82 국제 표기만 본다. 공백으로만 구분한 번호와 1588 류 대표번호는 가리지 못한다.
"""
from __future__ import annotations

import re

EMAIL_MASK = "[이메일 가림]"
PHONE_MASK = "[전화번호 가림]"

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
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

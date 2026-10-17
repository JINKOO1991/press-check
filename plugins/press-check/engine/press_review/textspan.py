"""Unicode code point 구간 연산. 모든 구간은 반개구간 [start, end).

Python str 인덱스가 code point 단위이므로 그대로 쓴다. UTF-16 변환·정규화(NFC/NFKC)는 하지 않는다:
NFD로 분해된 한글, 이모지(비BMP), 결합문자는 입력 그대로 센다.
"""
from __future__ import annotations


def cp_len(s: str) -> int:
    return len(s)


def cp_slice(s: str, start: int, end: int) -> str:
    """[start, end) 조각. 범위 밖이면 ValueError(파이썬 슬라이스처럼 조용히 잘라내지 않는다)."""
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
        raise ValueError("start/end must be int")
    if start < 0 or end > len(s) or start >= end:
        raise ValueError(f"span out of range: [{start}, {end}) for length {len(s)}")
    return s[start:end]


def find_all(text: str, quote: str) -> list[tuple[int, int]]:
    """quote의 모든 출현 위치(겹치는 출현 포함)를 (start, end) 목록으로."""
    if not quote:
        return []
    out: list[tuple[int, int]] = []
    i = text.find(quote)
    while i != -1:
        out.append((i, i + len(quote)))
        i = text.find(quote, i + 1)
    return out


def has_lone_surrogate(s: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)

"""인쇄 쪽 번호의 결정적 감지(P2_DESIGN §2: 추측해서 채우지 않는다).

규칙:
1. 각 페이지의 앞 3줄·뒤 3줄(공백 줄 제외)에서 줄 전체가 쪽 번호 모양인 것만 후보로 모은다
   (``12`` ``- 12 -`` ``12 / 340`` ``Page 12`` ``제12쪽`` 소문자 로마 숫자 ``iv``). 제목이나 문장이 섞인 줄은 후보가 아니다.
2. 후보마다 (종류, 오프셋 = 인쇄 번호 - PDF 쪽 번호)를 계산하고, 같은 (종류, 오프셋)이 ``min_run`` 쪽 이상에서 나타날 때만 그 오프셋을 인정한다.
3. 인정된 오프셋과 맞는 후보가 그 페이지에 정확히 하나일 때만 ``printed_page_no`` 를 채운다. 후보가 없거나 모호한 페이지는 None 으로 둔다(보간 금지).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

EDGE_LINES = 3
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}

_PLAIN = re.compile(r"^[-‐-―−‧·•]?\s*(\d{1,4})\s*[-‐-―−‧·•]?$")
_FRAC = re.compile(r"^(\d{1,4})\s*/\s*(\d{1,4})$")
_PAGE_EN = re.compile(r"^(?:page|p\.?)\s*(\d{1,4})(?:\s*(?:of|/)\s*\d{1,4})?$", re.I)
_PAGE_KO = re.compile(r"^제?\s*(\d{1,4})\s*(?:쪽|페이지|면)$")
_ROMAN_LINE = re.compile(r"^[-‐-―]?\s*([ivxlcdm]{1,7})\s*[-‐-―]?$")


def _roman_value(s: str) -> Optional[int]:
    total, prev = 0, 0
    for ch in reversed(s):
        v = _ROMAN[ch]
        total += -v if v < prev else v
        prev = v
    # 정규형이 아닌 문자열("iiii", "vx")은 번호가 아니다
    return total if total > 0 and _to_roman(total) == s else None


def _to_roman(n: int) -> str:
    out = ""
    for sym, val in (("m", 1000), ("cm", 900), ("d", 500), ("cd", 400), ("c", 100), ("xc", 90), ("l", 50), ("xl", 40),
                     ("x", 10), ("ix", 9), ("v", 5), ("iv", 4), ("i", 1)):
        while n >= val:
            out += sym
            n -= val
    return out


def line_candidate(line: str) -> Optional[tuple]:
    """줄 하나 -> (종류 'arabic'|'roman', 값) 또는 None."""
    s = line.strip()
    if not s:
        return None
    for rx in (_PLAIN, _PAGE_EN, _PAGE_KO):
        m = rx.match(s)
        if m:
            return ("arabic", int(m.group(1)))
    m = _FRAC.match(s)
    if m and int(m.group(1)) <= int(m.group(2)):
        return ("arabic", int(m.group(1)))
    m = _ROMAN_LINE.match(s)
    if m:
        v = _roman_value(m.group(1))
        if v:
            return ("roman", v)
    return None


def page_candidates(text: str) -> list:
    """한 페이지 텍스트의 후보 목록 [{'kind','value','where'}]. where: head|tail."""
    lines = [l for l in text.splitlines() if l.strip()]
    out = []
    for where, chunk in (("head", lines[:EDGE_LINES]), ("tail", lines[-EDGE_LINES:] if len(lines) > EDGE_LINES else [])):
        for l in chunk:
            c = line_candidate(l)
            if c:
                out.append({"kind": c[0], "value": c[1], "where": where})
    return out


def resolve(candidates_by_page: dict, page_count: int) -> dict:
    """{pdf_page_no: 후보 목록} -> {pdf_page_no: 인쇄 번호 문자열}. 인정된 페이지만 포함한다."""
    min_run = 3 if page_count >= 3 else 2
    votes: Counter = Counter()
    for p, cands in candidates_by_page.items():
        for key in {(c["kind"], c["value"] - p) for c in cands}:
            votes[key] += 1
    accepted = {k for k, n in votes.items() if n >= min_run}
    out = {}
    for p, cands in candidates_by_page.items():
        hits = {(c["kind"], c["value"]) for c in cands if (c["kind"], c["value"] - p) in accepted}
        if len(hits) == 1:
            kind, value = next(iter(hits))
            out[p] = _to_roman(value) if kind == "roman" else str(value)
    return out

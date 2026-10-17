"""쪽 텍스트(``pdftotext -layout`` 결과)의 조판 감지: 표(G-D26)와 2단 조판(G-D28). 순수 함수, 도구·장부 없음.

임계값은 장부의 실제 표본으로 맞췄다(docs/coverage/P2_extractor_compare.md '## G-D28 비교'의 '감지 기준 보정'). 다른 서식의 문서에는 다시 보정해야 한다.

표(G-D26): 한 줄에 칸 구분(비공백 글자 사이 공백 3칸 이상)이 2개 이상인 줄이 쪽에 ``TABLE_MIN_LINES`` 줄 이상이고, 그 줄들의 칸 구분 끝 열이 ±``TABLE_COL_TOL`` 안에서
같은 열로 모이는 열이 ``TABLE_MIN_COLS`` 개 이상(각 열에 ``TABLE_COL_SUPPORT`` 줄 이상)이면 표 쪽이다. 첨부는 텍스트가 있는 쪽의 ``TABLE_PDF_SHARE`` 이상이 표 쪽일 때 표 PDF 다.
2단 조판의 쪽은 줄마다 칸 구분이 하나뿐이라 걸리지 않는다.

2단(G-D28): 비어 있지 않은 줄의 ``TWO_COL_LINE_SHARE`` 이상에서 같은 열 구간(폭 ``TWO_COL_MIN_BAND`` 이상)이 공백이고 그 양쪽에 글자가 있으면(세로 공백 띠) 2단 쪽이다.
한 쪽의 위쪽만 2단이고 아래는 한 단인 혼합 쪽은 띠에 걸린 줄이 적어 해당하지 않는다.
"""
from __future__ import annotations

import re
from typing import Optional

GAP_RE = re.compile(r"(?<=\S)( {3,})(?=\S)")

TABLE_MIN_LINES = 6          # 칸 구분이 2개 이상인 줄 수
TABLE_MIN_COLS = 3           # 줄들이 공유하는 칸 구분 열 수
TABLE_COL_SUPPORT = 5        # 열마다 그 열에 칸 구분이 있는 줄 수
TABLE_COL_TOL = 2            # 같은 열로 보는 열 차이
TABLE_PDF_SHARE = 0.5        # 텍스트가 있는 쪽 중 표 쪽의 비율

TWO_COL_MIN_LINES = 10
TWO_COL_LINE_SHARE = 0.8
TWO_COL_MIN_BAND = 2


def _lines(text: str) -> list:
    return [l.rstrip() for l in (text or "").split("\n") if l.strip()]


def table_page(text: str) -> bool:
    """G-D26: 같은 칸 구분 패턴이 반복되는 행이 있는 쪽인가."""
    multi: list = []
    for line in _lines(text):
        ends = [m.end() for m in GAP_RE.finditer(line)]
        if len(ends) >= 2:
            multi.append(ends)
    if len(multi) < TABLE_MIN_LINES:
        return False
    cols = sorted(e for ends in multi for e in ends)
    clusters: list = []
    for c in cols:
        if clusters and c - clusters[-1][-1] <= TABLE_COL_TOL:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    shared = 0
    for cl in clusters:
        lo, hi = cl[0] - TABLE_COL_TOL, cl[-1] + TABLE_COL_TOL
        if sum(1 for ends in multi if any(lo <= e <= hi for e in ends)) >= TABLE_COL_SUPPORT:
            shared += 1
    return shared >= TABLE_MIN_COLS


def table_pdf_verdict(page_texts: list) -> dict:
    """첨부의 쪽 텍스트 목록(``-layout``) -> {pages: 텍스트 있는 쪽, table_pages, share, is_table}. 텍스트가 있는 쪽이 없으면 표가 아니다."""
    texts = [t for t in page_texts if _lines(t)]
    hit = sum(1 for t in texts if table_page(t))
    share = hit / len(texts) if texts else 0.0
    return {"pages": len(texts), "table_pages": hit, "share": round(share, 3), "is_table": bool(texts) and share >= TABLE_PDF_SHARE}


def two_column_band(text: str) -> Optional[tuple]:
    """G-D28: 쪽 대부분의 줄에 걸친 세로 공백 띠 (시작 열, 끝 열)의 가장 넓은 것. 없으면 None. 열은 ``-layout`` 텍스트의 글자 열이다
    (포인트 좌표가 아니다. 자를 위치는 단어 상자에서 따로 찾는다, ``pdf.find_gutter``)."""
    lines = _lines(text)
    n = len(lines)
    if n < TWO_COL_MIN_LINES:
        return None
    width = max(len(l) for l in lines)
    cnt = [0] * (width + 1)
    for l in lines:
        lo = len(l) - len(l.lstrip())
        for c in range(lo + 1, len(l) - 1):
            if l[c] == " ":
                cnt[c] += 1
    thr = TWO_COL_LINE_SHARE * n
    runs: list = []
    run: Optional[list] = None
    for c in range(width + 1):
        if cnt[c] >= thr:
            run = [c, c] if run is None else [run[0], c]
        elif run:
            runs.append(tuple(run))
            run = None
    if run:
        runs.append(tuple(run))
    runs = [r for r in runs if r[1] - r[0] + 1 >= TWO_COL_MIN_BAND]
    return max(runs, key=lambda r: r[1] - r[0]) if runs else None


def two_column_page(text: str) -> bool:
    return two_column_band(text) is not None

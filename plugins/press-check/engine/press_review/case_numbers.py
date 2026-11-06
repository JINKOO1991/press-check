"""법원 사건번호 표기 공용 규칙(수집기 ingestion/sources 와 색인 corpus_sqlite 가 같이 쓴다. 이 모듈은 ingestion 을 import 하지 않는다).

- ``split_case_numbers``: 병합 표기('2000다37524, 37531'·'A 및 B'·중점)를 번호 토큰으로 가른다.
- ``canon_year``·``canon_prec_number``: 2자리 연도(97도158)와 4자리 연도(1997도158)를 같은 번호로 만든다.
"""
from __future__ import annotations

import re
from typing import Optional

_CASE_FULL = re.compile(r"^(\d{2,4})([가-힣]{1,4})(\d+)$")


def split_case_numbers(s: str) -> list[str]:
    """'2000다37524, 37531' → ['2000다37524', '2000다37531'] (숫자만 있는 뒤 번호는 앞 번호의 연도·부호를 잇는다).
    '2005헌마165, 314, 2006헌가3' 도 같다. 인식하지 못하는 조각은 버린다. 순서 유지·중복 제거."""
    out: list[str] = []
    prefix = None
    for part in re.split(r"[,·ㆍ、]|\s+및\s+", s or ""):
        p = re.sub(r"\([^)]*\)", "", part).strip().replace(" ", "")
        p = re.sub(r"등$", "", p)   # '2005헌마165등' 의 끝 '등'(같은 번호 외 다수)은 번호가 아니다
        m = _CASE_FULL.match(p)
        if m:
            prefix = m.group(1) + m.group(2)
            cand = p
        elif p.isdigit() and prefix:
            cand = prefix + p
        else:
            continue
        if cand not in out:
            out.append(cand)
    return out


def canon_year(y: str) -> Optional[str]:
    """연도 표기 → 4자리. 2자리는 1945~1999 만 인정하고(2000년 이후 사건번호는 4자리로 인쇄된다) 4자리는 1900~2099 만 인정한다. 그 밖은 None(단기 연호 등)."""
    n = int(y)
    if len(y) == 2:
        return str(1900 + n) if n >= 45 else None
    if len(y) == 4 and 1900 <= n <= 2099:
        return y
    return None


def canon_prec_number(raw: str) -> str:
    """'97도158'·'1997도158' → '1997도158'. 형식 밖이거나 연도를 정하지 못하면 입력을 그대로 둔다."""
    m = _CASE_FULL.match(raw)
    if not m:
        return raw
    y = canon_year(m.group(1))
    return f"{y}{m.group(2)}{m.group(3)}" if y else raw

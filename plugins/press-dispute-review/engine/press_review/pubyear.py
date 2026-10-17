"""수록 간행물의 연도 읽기(P2_DESIGN §11 G-D25). 연도만 아는 근거의 기준일은 그해 12월 31일(상한)이다(``corpus.reference_date``).

연도의 출처는 장부 ``documents.year``, 없으면 제목 머리의 연도, 선거백서는 선거 회차 -> 연도 대응표(``ELECTIONS``)다.
표준 라이브러리만 쓴다. 네트워크·장부를 읽지 않는다.

``ELECTIONS`` 의 ``bound_year`` 는 선거 연도가 아니라 **그 선거의 선거기사심의위원회 결정이 끝나는 연도**(12월 31일이 상한이 되는 연도)다.
12월에 치른 대통령선거(16~18대)는 위원회 운영이 이듬해 1월 18일까지여서 선거 연도의 다음 해다(백서 본문의 운영기간 표).
4~6월 선거는 운영기간이 같은 해 5~7월에 끝난다.

선거일은 중앙선거관리위원회가 공고한 날짜다. 이 세션에서 네트워크로 공식 사이트를 확인하지 않았다. 근거 열(``evidence``)은 장부에 이미 내려받은 선거백서 본문에서
선거일·운영기간을 읽은 위치다(아래 EV_* 상수). 근거가 없는 행은 ``needs_check=True``(확인 필요)로 두고 값이 틀릴 수 있다고 본다.
공식 일정 근거(확인 대상 URL, 이 세션에서 접속하지 않음): 중앙선거관리위원회 역대선거정보 https://www.nec.go.kr/ 의 선거통계시스템 http://info.nec.go.kr/,
선거일 규정은 공직선거법 제34조(법제처 국가법령정보센터 https://www.law.go.kr/).
"""
from __future__ import annotations

import re
from typing import Any, Optional

MIN_YEAR, MAX_YEAR = 1900, 2100

# 장부에 내려받은 선거백서에서 읽은 근거(PAC 선거기사심의위원회 백서의 '선거일'·'운영기간' 표와 본문)
EV_PRES_TABLE = "pac_election:1070,1175 제20·21대 대선 백서 '선거일·운영기간' 표(2002~2025)"
EV_LOCAL_TABLE = "pac_election:1071,1218 제8·9회 지선 백서 '선거일·운영기간' 표(2002~2026)"
EV_A17 = "pac_election:778 본문 '2003년 12월 17일부터 2004년 5월 15일까지 활동'"
EV_A18 = "pac_election:781 본문 '실시 120일 전인 2007년 12월 11일'(2008-04-09의 120일 전)"
EV_A19 = "pac_election:635 본문 운영기간 2011-12-12~2012-05-11"
EV_A20 = "pac_election:737 본문 '선거일 2016년 4월 13일'"
EV_A22 = "pac_election:1141 본문 '선거일(2024년 4월 10일)', '선거일 후 30일째 되는 2024년 5월 10일'"

# (종류, 회차) -> 선거일, 위원회 결정이 끝나는 연도(bound_year), 근거, 확인 필요
# 종류: assembly = 국회의원선거(제N대), president = 대통령선거(제N대), local = 전국동시지방선거(제N회)
_ROWS = (
    ("assembly", 16, "2000-04-13", 2000, None),
    ("assembly", 17, "2004-04-15", 2004, EV_A17),
    ("assembly", 18, "2008-04-09", 2008, EV_A18),
    ("assembly", 19, "2012-04-11", 2012, EV_A19),
    ("assembly", 20, "2016-04-13", 2016, EV_A20),
    ("assembly", 21, "2020-04-15", 2020, None),
    ("assembly", 22, "2024-04-10", 2024, EV_A22),
    ("president", 16, "2002-12-19", 2003, EV_PRES_TABLE + "; 운영기간 2002-08-21~2003-01-18"),
    ("president", 17, "2007-12-19", 2008, EV_PRES_TABLE + "; 운영기간 2007-08-21~2008-01-18"),
    ("president", 18, "2012-12-19", 2013, EV_PRES_TABLE + "; 운영기간 2012-04-22~2013-01-18"),
    ("president", 19, "2017-05-09", 2017, EV_PRES_TABLE + "; 운영기간 2017-03-20~2017-06-08"),
    ("president", 20, "2022-03-09", 2022, EV_PRES_TABLE + "; 운영기간 2021-07-11~2022-04-08"),
    ("president", 21, "2025-06-03", 2025, EV_PRES_TABLE + "; 운영기간 종료 2025-07-03"),
    ("local", 3, "2002-06-13", 2002, EV_LOCAL_TABLE + "; 운영기간 종료 2002-07-13"),
    ("local", 4, "2006-05-31", 2006, EV_LOCAL_TABLE + "; 운영기간 종료 2006-06-30"),
    ("local", 5, "2010-06-02", 2010, EV_LOCAL_TABLE + "; 운영기간 종료 2010-07-02"),
    ("local", 6, "2014-06-04", 2014, EV_LOCAL_TABLE + "; 운영기간 종료 2014-07-04"),
    ("local", 7, "2018-06-13", 2018, EV_LOCAL_TABLE + "; 운영기간 종료 2018-07-13"),
    ("local", 8, "2022-06-01", 2022, EV_LOCAL_TABLE + "; 운영기간 종료 2022-07-01"),
    ("local", 9, "2026-06-03", 2026, EV_LOCAL_TABLE + "; 운영기간 종료 2026-07-03"),
)
ELECTIONS: dict = {
    (kind, no): {"date": date, "bound_year": bound, "evidence": ev, "needs_check": ev is None}
    for kind, no, date, bound, ev in _ROWS
}

_ELECTION_RES = (
    ("assembly", re.compile(r"(?<!\d)(\d{1,2})\s*대\s*국회의원")),
    ("president", re.compile(r"(?<!\d)(\d{1,2})\s*대\s*대통령")),
    ("local", re.compile(r"(?<!\d)(\d{1,2})\s*회\s*전국\s*동시\s*지방\s*선거")),
)
_MAY31 = re.compile(r"5\s*\.\s*31\s*지방\s*선거")
_LEADING_YEAR = re.compile(r"^\s*((?:19|20)\d{2})(?=\s*년|\s|$)(?!\s*[~\-–—]\s*\d)")
WHITEPAPER_SOURCE = "pac_election_whitepapers"


def valid_year(v: Any) -> Optional[int]:
    """정수 연도(MIN_YEAR~MAX_YEAR)만 받는다. bool·float·문자열은 받지 않는다."""
    if isinstance(v, int) and not isinstance(v, bool) and MIN_YEAR <= v <= MAX_YEAR:
        return v
    return None


def election_of(title: Any) -> Optional[tuple]:
    """선거백서 제목 -> (종류, 회차). 모르면 None. '5.31 지방선거'는 제4회 전국동시지방선거다."""
    if not isinstance(title, str):
        return None
    for kind, rx in _ELECTION_RES:
        m = rx.search(title)
        if m:
            return kind, int(m.group(1))
    if _MAY31.search(title):
        return "local", 4
    return None


def election_bound_year(title: Any) -> Optional[int]:
    key = election_of(title)
    row = ELECTIONS.get(key) if key else None
    return row["bound_year"] if row else None


def title_year(title: Any) -> Optional[int]:
    """제목 머리의 연도('2024 시정권고 사례집', '2012년도 …'). 연도 범위('2012~2013')나 사건번호('2024자심49')는 읽지 않는다."""
    if not isinstance(title, str):
        return None
    m = _LEADING_YEAR.match(title)
    return valid_year(int(m.group(1))) if m else None


def document_year(source_id: Any, title: Any, ledger_year: Any = None) -> Optional[int]:
    """간행물 연도(상한): 장부 year -> 선거백서 대응표 -> 제목 머리의 연도. 모두 없으면 None(date_unknown)."""
    if isinstance(ledger_year, str) and ledger_year.strip().isdigit():
        ledger_year = int(ledger_year.strip())
    y = valid_year(ledger_year)
    if y is not None:
        return y
    if source_id == WHITEPAPER_SOURCE:
        y = election_bound_year(title)
        if y is not None:
            return y
    return title_year(title)

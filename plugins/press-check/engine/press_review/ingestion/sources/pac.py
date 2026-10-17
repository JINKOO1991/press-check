"""PAC(언론중재위원회) 간행물·자료실 열거 어댑터. 계약: docs/P1_DESIGN.md '어댑터 범위' pac 행.

열거만 한다(목록 전 페이지 → 상세 → 첨부 링크 → 첨부 HEAD). 다운로드·본문 독해는 하지 않는다(P2).

메뉴 범위는 ``MENUS``(포함)·``EXCLUDED``(제외)에 근거와 함께 적고, 실행 결과 result 와 ``corpus/inventory/pac_menu_scope.json`` 에도 싣는다.
메뉴 종류 세 가지:
- magazine: ``publi-list`` 목록 + ``webZineView`` 상세(전체 PDF=field=pdf_nm, 장별 PDF=magazine_sub&sub_idx).
- board: ``statistics-list-wrap`` 표 목록(번호·제목·등록일) + ``statistics-view-wrap`` 상세(boardfile 첨부).
- static: 본문이 곧 문서인 고정 페이지(심의기준). 페이지 안 ``js_file_download(2)`` 파일(편람)이 첨부다.

ID 규칙(재실행 때 번호가 밀리지 않도록 위치 번호를 쓰지 않는다):
- doc_id: 간행물 ``<family>:<idx>``, 게시판 ``<family>:bn<bn>``, 고정 페이지 ``pac_other:static<p>``.
- att_id: 전체 PDF ``#0``, 장별 ``#<sub_idx>``, 게시판 파일 ``#<file_no>``, 그 밖(편람·외부 링크)은 URL/파일명 crc32.
- 상세 URL은 nPage·f·s 를 뺀 정규형 하나만 쓴다(캐시 키 중복 방지).

화면 총수 대조: 간행물 목록에는 총 건수 표시가 없다. 마지막 페이지 번호(arrow_last)와 페이지 크기(10)로 범위
``(last-1)*10 < 문서 수 <= last*10`` 와 '마지막 쪽 외 전부 10건'을 확인한다. 게시판은 맨 위 행 번호 = 총 건수, 번호 1..N 연속을 확인한다.

HEAD: 크기·타입만 본다. 2xx 이고 content-length 가 양수이며 text/html 이 아니어야 성공이다. 실패하면 Range GET 으로 바꾸지 않는다
(공용 HttpClient 가 Range 헤더를 보낼 수 없고, 일반 GET 은 최대 64MB 를 내려받는다). 실패는 장부에 남는다.
같은 오류가 3회 연속이면 그 자리에서 멈추고 result.stopped 에 사유를 적는다(상태 '미검증').
"""
from __future__ import annotations

import html as _html
import json
import re
import urllib.parse
import zlib
from datetime import date
from typing import Any, Optional

from ...config import private_mkdir
from .. import withhold
from ..http import BlockedURL, FetchFailed, ResponseTooLarge, RobotsDisallowed

HOST = "https://www.pac.or.kr"
PAGE_SIZE = 10
STOP_AFTER = 3          # 같은 오류 연속 횟수
PERMANENT_AFTER = 2     # 장부상 열린 실패의 retry_count 가 이 값 이상이면(=3번째 실패) 다음 실행에서 건너뛴다

# ---------------------------------------------------------------- 메뉴 범위(근거 포함)
MENUS: list[dict] = [
    dict(key="207", source_id="pac_adjustment_casebooks", family="pac_cases", kind="magazine", p=207, mg="M03", cate="MA04",
         doc_type="casebook_adjustment", title="언론조정중재사례집",
         why="필수(브리프). 사례집 PDF에 정정·반론·손해배상 조정 사례."),
    dict(key="209", source_id="pac_election_whitepapers", family="pac_election", kind="magazine", p=209, mg="M03", cate="MA08",
         doc_type="whitepaper_election", title="선거기사심의백서",
         why="필수(브리프). 선거기사심의위원회 심의·의결 사례."),
    dict(key="208", source_id="pac_correction_casebooks", family="pac_cases", kind="magazine", p=208, mg="M03", cate="MA09",
         doc_type="casebook_correction", title="시정권고사례집",
         why="사례집 탭(p=207 화면의 select-tab)에 있는 같은 구조의 시정권고 사례집. 심의기준 적용 사례."),
    dict(key="223", source_id="pac_research_journal", family="pac_other", kind="magazine", p=223, mg="M01", cate="ST01",
         doc_type="journal", title="학술지 미디어와 인격권(학술지 자료)",
         why="p=221 연구 간행물 하위 탭. 학술 2차 자료(구속력 없는 의견)."),
    dict(key="200", source_id="pac_research_quarterly", family="pac_other", kind="magazine", p=200, mg="M01", cate="MA02",
         doc_type="quarterly", title="계간 언론중재",
         why="p=221 하위 탭. 실무 해설·판례 소개 2차 자료. 호당 장별 PDF가 많아 HEAD 가 가장 많다."),
    dict(key="201", source_id="pac_research_judgment_analysis", family="pac_other", kind="magazine", p=201, mg="M01", cate="MA05",
         doc_type="judgment_analysis_report", title="언론관련판결분석보고서",
         why="p=221 하위 탭. 연도별 언론 관련 판결 통계·사례 분석."),
    dict(key="229", source_id="pac_research_commissioned", family="pac_other", kind="magazine", p=229, mg="M01", cate="EM08",
         doc_type="commissioned_research", title="연구용역보고서",
         why="p=221 하위 탭. 시정권고 심의기준 개정방안 등 심의기준 연구."),
    dict(key="203", source_id="pac_research_seminars", family="pac_other", kind="magazine", p=203, mg="M01", cate="EM01",
         doc_type="seminar_materials", title="세미나/토론회 자료집",
         why="p=221 하위 탭. 발제문·토론문(2차 의견)."),
    dict(key="228", source_id="pac_research_issue_reports", family="pac_other", kind="magazine", p=228, mg="M01", cate="MA10",
         doc_type="issue_report", title="연구보고서/현안보고서",
         why="p=221 하위 탭. 브리프가 예시한 현안보고서."),
    dict(key="202", source_id="pac_research_foreign_law", family="pac_other", kind="magazine", p=202, mg="M01", cate="EM07",
         doc_type="foreign_law_report", title="해외언론법제연구보고서/해외언론관계판결집",
         why="p=221 하위 탭. 외국 사례(국내 기준에 그대로 적용하지 않는 참고 자료)."),
    dict(key="210", source_id="pac_other_annual_reports", family="pac_other", kind="magazine", p=210, mg="M04", cate="MA06",
         doc_type="annual_report", title="기타 자료(연간보고서)",
         why="정보 자료실 내비게이션의 간행물 메뉴. 연간 처리 현황·업무 보고."),
    dict(key="212", source_id="pac_other_promo_publications", family="pac_other", kind="magazine", p=212, mg="M04", cate="EM05",
         doc_type="promo_publication", title="기타 자료(홍보 간행물)",
         why="p=210 연간보고서와 같은 '기타 자료' 탭 묶음(select-tab). 5건(위원회 소개·30년사). 열거 비용이 작아 포함하고 관련성은 P2에서 판정."),
    dict(key="213", source_id="pac_other_misc_reports", family="pac_other", kind="magazine", p=213, mg="M04", cate="EM03",
         doc_type="misc_report", title="기타 자료(각종 보고서 등)",
         why="같은 탭 묶음. 시정권고 연구보고서·상담사례집·헌재결정문 모음 등 기준·사례 자료를 포함."),
    dict(key="214", source_id="pac_other_mediation_persuasion", family="pac_other", kind="magazine", p=214, mg="M04", cate="EM06",
         doc_type="mediation_persuasion", title="기타 자료(조정을 위한 설득과 수사)",
         why="같은 탭 묶음. 조정 실무 해설 간행물(2013년 3월호~2017년; 장부 기준 2013~2014년은 연 6~7호, 2015~2017년은 연 4호 계간). 2차 자료."),
    dict(key="195", source_id="pac_board_correction_status", family="pac_cases", kind="board", p=195, b="B_1_6", cate="PD03",
         doc_type="board_correction_status", title="시정권고 현황(자료실 게시판)",
         why="시정권고 메뉴의 자료실. 시정권고소위원회 회의 결과·전체 목록(결정 자료). 같은 화면의 '연도별 시정권고사례집' 탭은 p=208 과 같은 cate(MA09)라 별도 소스로 두지 않는다."),
    dict(key="159", source_id="pac_board_committee_rules", family="pac_other", kind="board", p=159, b="B_1_7", cate="",
         doc_type="board_committee_rules", title="위원회 규칙(관련 법규 게시판)",
         why="위원회 규칙 4건(시정권고소위원회규칙 등 심의 절차 규범). 같은 화면의 법령 링크는 law.go.kr 이라 법제처 어댑터 범위."),
    dict(key="12", source_id="pac_standards_correction_page", family="pac_other", kind="static", p=12,
         doc_type="standards_page", title="시정권고 심의기준(고정 페이지)",
         why="브리프가 지정한 '심의기준'. 페이지 본문 자체가 기준이고 시정권고 편람 PDF가 붙는다."),
    dict(key="16", source_id="pac_standards_election_page", family="pac_other", kind="static", p=16,
         doc_type="standards_page", title="선거기사 심의기준 및 대표사례(고정 페이지)",
         why="브리프가 지정한 '심의기준'. 선거기사심의 편람·가이드북 PDF가 붙는다. 같은 화면의 백서 링크는 p=209 목록이 이미 포함."),
]

EXCLUDED: list[dict] = [
    dict(p="221", name="연구 간행물 소개/학술지 소개", reason="소개 페이지(항목 없음). 하위 탭은 포함. 편집 규정 HWP/PDF(js_file_download)는 학술지 운영 규정이라 제외."),
    dict(p="222", name="논문공모", reason="공모 안내이지 간행물이 아니다."),
    dict(p="215", name="간행물 검색", reason="위 목록과 같은 DB를 검색하는 화면. 중복이라 제외."),
    dict(p="217/69", name="각종통계·현황(처리현황 게시판, 약 44쪽)", reason="월별 처리 통계 게시판이다. 사례·기준·해설이 아니다. 후보로 남기되 P1 범위 밖."),
    dict(p="211", name="기타 자료 > 이용만족도조사 보고서(M04/MA07, 연도별 3쪽)", reason="이용자 만족도 설문 결과 보고서. 사례·기준·해설이 아니어서 제외. 같은 구조라 필요하면 MENUS 에 한 줄로 추가 가능."),
    dict(p="226", name="뉴스레터 <언론사람>(/kor/news_letter/)", reason="홍보 소식지. 경로·구조가 달라 별도 감사 필요. 후보로 남김."),
    dict(p="8,11,13,14,19,163,193,196", name="언론피해구제절차·시정권고/선거기사심의 소개·절차·서식·재심청구", reason="절차 안내·서식. 심의기준(p=12·16)만 포함. p=8 '잘못된 보도의 유형'은 안내문이라 후보로 남김."),
    dict(p="34,41,42,43,44,165", name="알림마당·FAQ·정보공개", reason="공지·보도자료·문의. 간행물이 아니다."),
    dict(p="46~110,158", name="위원회 소개·교육 프로그램", reason="기관 소개·교육 신청. 대상 아님."),
]

KNOWN_TAB_KEYS = {(m["p"], m.get("mg"), m.get("cate")) for m in MENUS if m["kind"] == "magazine"}
KNOWN_TAB_KEYS |= {(211, "M04", "MA07"), (221, None, None), (195, "M03", "MA09"), (195, None, "PD03")}   # 221=소개 페이지, 195 의 사례집 탭=208 과 같은 cate

# ---------------------------------------------------------------- 순수 파서(테스트 대상)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>", " ", s)
    return _WS.sub(" ", _html.unescape(_TAG.sub("", s))).strip()


def title_year(title: str) -> Optional[int]:
    """'2025년도 …' 형태에서만 연도를 뽑는다. ISSN·권호 숫자는 보지 않는다."""
    m = re.search(r"((?:19|20)\d{2})\s*년", re.sub(r"\[ISSN[^\]]*\]", "", title))
    return int(m.group(1)) if m else None


def abs_url(href: str, base: str = HOST + "/kor/pages/") -> str:
    href = _html.unescape(href.strip())
    u = urllib.parse.urljoin(base, href)
    if u.startswith("http://www.pac.or.kr") or u.startswith("http://pac.or.kr"):
        u = "https://" + u[len("http://"):]
    return u.split("#", 1)[0]


def att_number(url: str, kind: str) -> int:
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    menu = (q.get("menu") or [""])[0]
    if menu == "magazine_sub" and (q.get("sub_idx") or [""])[0].isdigit():
        return int(q["sub_idx"][0])
    if menu == "boardfile" and (q.get("file_no") or [""])[0].isdigit():
        return int(q["file_no"][0])
    if menu == "magazine" and (q.get("field") or [""])[0] == "pdf_nm":
        return 0
    return zlib.crc32(url.encode("utf-8")) & 0x7FFFFFFF


def js_escape(s: str) -> str:
    """JavaScript escape(): 영숫자와 @*_+-./ 만 그대로, 나머지는 %XX(<256) 또는 %uXXXX."""
    out = []
    for ch in s:
        o = ord(ch)
        if ch.isascii() and (ch.isalnum() or ch in "@*_+-./"):
            out.append(ch)
        elif o < 256:
            out.append("%%%02X" % o)
        else:
            out.append("%%u%04X" % o)
    return "".join(out)


def parse_paginate(h: str) -> tuple[set[int], int]:
    """(보인 페이지 번호 집합, 마지막 페이지 번호). paginate 가 비어 있으면 ({}, 1)."""
    i = h.find('class="paginate"')
    if i < 0:
        return set(), 1
    seg = h[i:h.find("</section>", i)]
    nums = {int(n) for n in re.findall(r"[?&]nPage=(\d+)", seg)}
    nums |= {int(n) for n in re.findall(r"class=['\"]current['\"]>(\d+)<", seg)}
    return nums, (max(nums) if nums else 1)


def parse_tabs(h: str) -> set[tuple]:
    """select-tab 의 (p, magazine_new, cate) 집합(메뉴 발견용)."""
    out = set()
    for blk in re.findall(r'class="select-tab[^"]*".*?</ul>', h, re.S):
        for href in re.findall(r'href="([^"]*)"', blk):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(_html.unescape(href)).query)
            if (q.get("p") or [""])[0].isdigit():
                out.add((int(q["p"][0]), (q.get("magazine_new") or [None])[0], (q.get("cate") or [None])[0]))
    return out


def parse_magazine_list(h: str) -> dict:
    """{'items': [{idx,title,cover}], 'li_count': int}. li_count 는 파서와 다른 방법(publi-list 안 '<li' 개수)이다."""
    i = h.find('class="publi-list"')
    if i < 0:
        return {"items": [], "li_count": 0, "marker": False}
    blk = h[i:h.find("</ul>", i)]
    items = []
    for li in re.findall(r"<li\b.*?</li>", blk, re.S):
        m = re.search(r'href="[^"]*[?&]idx=(\d+)[^"]*m=view', li)
        p = re.search(r"<p>(.*?)</p>", li, re.S)
        img = re.search(r'<img[^>]+src="([^"]+)"', li)
        if m:
            items.append({"idx": int(m.group(1)), "title": clean(p.group(1)) if p else "", "cover": img.group(1) if img else None})
    return {"items": items, "li_count": blk.count("<li"), "marker": True}


def parse_magazine_detail(h: str) -> dict:
    """box1(제목·전체 PDF)~box2(장별) 만 본다. 이전호/다음호 링크는 이 구간 밖이다."""
    i = h.find('class="webZineView"')
    if i < 0:
        return {"marker": False, "title": "", "attachments": [], "empty_href": 0}
    j = h.find('class="box1"', i)
    if j < 0:
        return {"marker": True, "title": "", "attachments": [], "empty_href": 0}
    end = h.find('class="prevNext-mp hidden-mp"', j)
    seg = h[j:end if end > 0 else h.find('class="list_btn"', j)]
    t = re.search(r"<p>(.*?)</p>", seg, re.S)
    atts, empty = [], 0
    for href, txt in re.findall(r'<a\s[^>]*?href="([^"]*)"[^>]*>(.*?)</a>', seg, re.S):
        label = clean(txt)
        if not href.strip() or href.strip().startswith("#"):    # 빈 href 와 '#none' 자리표시 앵커는 링크가 아니다
            empty += 1
            continue
        url = abs_url(href)
        if "menu=magazine_sub" in url:
            kind = "chapter"
        elif "menu=magazine&" in url and "field=" in url:
            kind = "full"
        else:
            kind = "other"
        atts.append({"url": url, "label": label, "kind": kind})
    return {"marker": True, "title": clean(t.group(1)) if t else "", "attachments": atts, "empty_href": empty}


def _date(s: str) -> Optional[str]:
    m = re.match(r"\s*(\d{4})\.(\d{2})\.(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def parse_board_list(h: str) -> dict:
    """{'rows': [{num|None, bn, title, date, has_file}], 'tr_count': int, 'marker': bool}. tr_count 는 tbody 안 '<tr' 개수."""
    i = h.find('class="statistics-list-wrap"')
    if i < 0:
        return {"rows": [], "tr_count": 0, "marker": False}
    k = h.find("<tbody>", i)
    blk = h[k:h.find("</tbody>", k)] if k >= 0 else ""
    rows = []
    for tr in re.findall(r"<tr\b.*?</tr>", blk, re.S):
        tds = re.findall(r"<td\b[^>]*>(.*?)</td>", tr, re.S)
        m = re.search(r"js_board_view\((\d+)\)", tr)
        if not m or len(tds) < 4:
            continue
        num = clean(tds[0])
        rows.append({"num": int(num) if num.isdigit() else None, "bn": int(m.group(1)), "title": clean(tds[1]),
                     "date": _date(clean(tds[3])), "has_file": "첨부파일" in tds[2]})
    return {"rows": rows, "tr_count": blk.count("<tr"), "marker": True}


def parse_board_detail(h: str) -> dict:
    """게시판 상세 두 템플릿: statistics-view-wrap(p=195 등)·view-box(p=159 위원회 규칙)."""
    starts = [x for x in (h.find('class="statistics-view-wrap"'), h.find('class="view-box"')) if x >= 0]
    if not starts:
        return {"marker": False, "title": "", "attachments": []}
    i = min(starts)
    end = h.find('class="prevNext-mp', i)
    seg = h[i:end if end > 0 else len(h)]
    t = re.search(r'class="box1-1">\s*<p>(.*?)</p>', seg, re.S) or re.search(r'<p class="title">(.*?)</p>', seg, re.S)
    title = re.sub(r"^\[[^\]]*\]\s*", "", clean(t.group(1))) if t else ""
    atts = []
    for href, txt in re.findall(r'<a\s[^>]*?href="([^"]*menu=boardfile[^"]*)"[^>]*>(.*?)</a>', seg, re.S):
        label = re.sub(r"\s*\(size:[^)]*\)\s*$", "", clean(txt))
        atts.append({"url": abs_url(href), "label": label, "kind": "full"})
    return {"marker": True, "title": title, "attachments": atts}


def parse_static(h: str) -> dict:
    i = h.find("print-contents")
    if i < 0:
        return {"marker": False, "attachments": []}
    seg = h[i:h.find("<footer", i) if h.find("<footer", i) > 0 else len(h)]
    atts, seen = [], set()
    pat = r"""<a\b[^>]*?(?:onclick="|href="javascript:)js_file_download(2?)\('([^']+)'\)[^>]*>(.*?)</a>"""
    for which, name, txt in re.findall(pat, seg, re.S):
        if name in seen:                      # 같은 파일이 두 엔드포인트로 걸린 경우 첫 링크만
            continue
        seen.add(name)
        url = f"{HOST}/_common/path_download_file{which}.asp?f={js_escape(name)}"
        atts.append({"url": url, "label": clean(txt) or name, "kind": "full", "filename": name})
    return {"marker": True, "attachments": atts}


# ---------------------------------------------------------------- URL
def list_url(m: dict, n: int) -> str:
    if m["kind"] == "board":
        return f"{HOST}/kor/pages/?nPage={n}&p={m['p']}&b={m['b']}&cate={m['cate']}&f=&s="
    return f"{HOST}/kor/pages/index.asp?nPage={n}&p={m['p']}&magazine_new={m['mg']}&cate={m['cate']}&f=&s="


def entry_url(m: dict) -> str:
    if m["kind"] == "magazine":
        return f"{HOST}/kor/pages/?p={m['p']}&magazine_new={m['mg']}&cate={m['cate']}"
    if m["kind"] == "board":
        return f"{HOST}/kor/pages/?p={m['p']}&b={m['b']}&cate={m['cate']}"
    return f"{HOST}/kor/pages/?p={m['p']}"


def detail_url(m: dict, key: int) -> str:
    if m["kind"] == "board":
        return f"{HOST}/kor/pages/?p={m['p']}&b={m['b']}&cate={m['cate']}&bn={key}&m=read"
    return f"{HOST}/kor/pages/?p={m['p']}&magazine_new={m['mg']}&cate={m['cate']}&idx={key}&m=view"


# ---------------------------------------------------------------- 실행
class _Stop(Exception):
    pass


class _Guard:
    """같은 (단계, 오류) 연속 STOP_AFTER 회면 멈춘다."""

    def __init__(self) -> None:
        self.last: Optional[tuple] = None
        self.n = 0

    def ok(self) -> None:
        self.last, self.n = None, 0

    def bad(self, stage: str, cls: str) -> None:
        key = (stage, cls)
        self.n = self.n + 1 if key == self.last else 1
        self.last = key
        if self.n >= STOP_AFTER:
            raise _Stop(f"{stage}/{cls} x{self.n}")


_NET_ERRORS = (FetchFailed, RobotsDisallowed, BlockedURL, ResponseTooLarge)


def add_arguments(parser) -> None:
    parser.add_argument("--menu", action="append", dest="menus", help="메뉴 key(207·209·208·223·200·201·229·203·228·202·210·212·213·214·195·159·12·16) 반복 가능. 기본: 전부(이 순서)")
    parser.add_argument("--max-details", type=int, default=None, help="이번 실행에서 새로 받을 상세 페이지 수 상한(캐시 적중은 세지 않음)")
    parser.add_argument("--max-heads", type=int, default=None, help="이번 실행에서 보낼 첨부 HEAD 수 상한")
    parser.add_argument("--no-heads", action="store_true", help="첨부 HEAD 를 건너뜀")


def _marker(*tokens: bytes):
    return lambda body, headers: None if any(t in body for t in tokens) else f"marker {tokens[0].decode()} missing"


def run(ctx, args) -> dict:
    wanted = list(getattr(args, "menus", None) or [])
    menus = [m for m in MENUS if not wanted or m["key"] in wanted or m["source_id"] in wanted]
    unknown = [w for w in wanted if w not in {m["key"] for m in MENUS} | {m["source_id"] for m in MENUS}]
    if unknown:
        raise ValueError(f"unknown menu: {unknown}")
    st = _State(ctx, getattr(args, "max_details", None), getattr(args, "max_heads", None), not getattr(args, "no_heads", False))
    result: dict[str, Any] = {"menus": {}}
    try:
        for m in menus:
            ctx.check_time()
            st.run_menu(m, result["menus"])
        if not wanted:
            st.discover(result)
    except _Stop as e:
        result["stopped"] = str(e)
        result["status_note"] = "미검증: 같은 오류 3회 연속으로 중단"
    finally:
        result["scope"] = {"included": [m["key"] for m in MENUS], "excluded": [e["p"] for e in EXCLUDED]}
        result["doc_id_collisions"] = st.collisions
        if not ctx.dry_run and not wanted:
            _write_scope(ctx)
    return result


def _write_scope(ctx) -> None:
    private_mkdir(ctx.inventory_dir)
    payload = {"checked_at": ctx.now().isoformat(timespec="seconds"),
               "included": [{k: v for k, v in m.items() if k in ("key", "source_id", "family", "kind", "p", "title", "why")} for m in MENUS],
               "excluded": EXCLUDED}
    p = ctx.inventory_dir / "pac_menu_scope.json"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


class _State:
    def __init__(self, ctx, max_details, max_heads, heads: bool):
        self.ctx, self.max_details, self.max_heads, self.heads = ctx, max_details, max_heads, heads
        self.details_fetched = 0
        self.heads_sent = 0
        self.collisions = 0
        self.guard = _Guard()
        self.tabs: set = set()
        self.open_failures = {(f["target"], f["stage"]): f["retry_count"] for f in ctx.manifest.failures()}

    # ---- 공통 ----
    def _fail(self, target: str, stage: str, cls: str, msg: str) -> None:
        self.ctx.manifest.record_failure(target, stage, cls, msg)
        self.open_failures[(target, stage)] = self.open_failures.get((target, stage), -1) + 1
        self.guard.bad(stage, cls)

    def _skip_permanent(self, target: str, stage: str) -> bool:
        return self.open_failures.get((target, stage), -1) >= PERMANENT_AFTER

    def _get(self, url: str, *tokens: bytes):
        return self.ctx.http.get(url, accept=_marker(*tokens))

    # ---- 메뉴 ----
    def run_menu(self, m: dict, out: dict) -> None:
        ctx = self.ctx
        s = out[m["key"]] = {"source_id": m["source_id"], "kind": m["kind"]}
        notes = f"범위 근거: {m['why']}"
        ctx.manifest.ensure_source(m["source_id"], m["family"], m["title"], entry_url(m), notes=notes)
        if m["kind"] == "static":
            self._static(m, s)
        else:
            docs = self._crawl(m, s)
            self._details(m, docs, s)
            if self.heads:
                self._heads(m, docs, s)
        ctx.manifest.touch_source_checked(m["source_id"])

    # ---- 목록 ----
    def _crawl(self, m: dict, s: dict) -> list[dict]:
        ctx = self.ctx
        mag = m["kind"] == "magazine"
        token = b"publi-list" if mag else b"statistics-list-wrap"
        done: dict[int, dict] = {}
        known_last, docs, seen_ids = 1, [], set()
        new = seen = dup = 0
        title_changed = 0
        while True:
            todo = [n for n in range(1, known_last + 1) if n not in done]
            if not todo or (ctx.limit_pages is not None and len(done) >= ctx.limit_pages):
                break
            n = todo[0]
            ctx.check_time()
            url = list_url(m, n)
            try:
                r = self._get(url, token)
            except _NET_ERRORS as e:                       # 재시도 뒤에도 실패한 목록 쪽: 장부에 남기고 다음 쪽으로 간다
                self._fail(url, "list", getattr(e, "error_class", "error"), str(e))
                ctx.manifest.upsert_list_page(m["source_id"], n, url, http_status=None, item_count=0, body_sha256=None, ok=False)
                done[n] = {"ok": False}
                continue
            if not r.ok:
                self._fail(url, "list", "bad_page", r.content_error or f"http {r.status}")
                ctx.manifest.upsert_list_page(m["source_id"], n, url, http_status=r.status, item_count=0, body_sha256=None, ok=False)
                done[n] = {"ok": False}
                continue
            h = r.text()
            nums, last = parse_paginate(h)
            known_last = max(known_last, last)
            self.tabs |= parse_tabs(h)
            if mag:
                pl = parse_magazine_list(h)
                items = [dict(key=i["idx"], title=i["title"], cover=i["cover"], date=None, num=None) for i in pl["items"]]
                expect = pl["li_count"]
            else:
                pl = parse_board_list(h)
                items = [dict(key=i["bn"], title=i["title"], cover=None, date=i["date"], num=i["num"]) for i in pl["rows"]]
                expect = pl["tr_count"]
            ok = len(items) == expect and (len(items) > 0 or n > 1)
            ctx.manifest.upsert_list_page(m["source_id"], n, url, http_status=r.status, item_count=len(items),
                                          body_sha256=r.sha256, ok=ok)
            if not ok:
                self._fail(url, "list", "count_mismatch", f"parsed {len(items)} vs screen {expect}")
                done[n] = {"ok": False, "items": len(items), "screen": expect}
                continue
            self.guard.ok()
            ctx.manifest.resolve_failures(url, "list")
            done[n] = {"ok": True, "items": len(items), "screen": expect}
            for it in items:
                if it["key"] in seen_ids:
                    dup += 1
                    continue
                seen_ids.add(it["key"])
                doc_id = f"{m['family']}:{it['key']}" if mag else (f"{m['family']}:bn{it['key']}")
                year = title_year(it["title"]) if mag else (int(it["date"][:4]) if it["date"] else None)
                durl = detail_url(m, it["key"])
                extra = {"menu": m["key"], "list_page": n, "year_basis": "title" if (mag and year) else ("post_date" if year else None)}
                if it["cover"]:
                    extra["cover_image"] = it["cover"]
                if it["num"] is not None:
                    extra["board_no"] = it["num"]
                before = ctx.manifest.get_document(doc_id)
                if before and before["source_id"] != m["source_id"]:
                    self.collisions += 1
                    continue
                res = ctx.manifest.upsert_document(doc_id, m["source_id"], canonical_url=durl, title=it["title"],
                                                   publication_date=it["date"], year=year, doc_type=m["doc_type"],
                                                   detail_url=durl, extra=extra)
                if res == "new":
                    new += 1
                else:
                    seen += 1
                    if before and before.get("title") != it["title"]:
                        ctx.manifest.update_document_fields(doc_id, title=it["title"])
                        title_changed += 1
                docs.append({"doc_id": doc_id, "key": it["key"], "url": durl, "title": it["title"], "year": year, "num": it["num"]})
        pages_ok = sum(1 for v in done.values() if v["ok"])
        total = len(docs)
        s.update(list_pages=len(done), list_pages_ok=pages_ok, last_page=known_last, docs=total, new_docs=new, seen_docs=seen,
                 dup_in_list=dup, title_changed=title_changed)
        s["screen_count_match"] = self._check_total(m, done, docs, known_last, total, s)
        yrs = [d["year"] for d in docs if d["year"]]
        s.update(year_min=min(yrs, default=None), year_max=max(yrs, default=None), year_null=len(docs) - len(yrs))
        return docs

    def _check_total(self, m, done, docs, last, total, s) -> str:  # noqa: D401
        full = (self.ctx.limit_pages is None) and all(v["ok"] for v in done.values()) and len(done) == last
        if not full:
            return "not_checked(partial)"
        if m["kind"] == "magazine":
            sizes = [done[n]["items"] for n in sorted(done)]
            in_range = (last - 1) * PAGE_SIZE < total <= last * PAGE_SIZE
            full_pages = all(x == PAGE_SIZE for x in sizes[:-1])
            return "ok(range+full_pages)" if in_range and full_pages and total == sum(sizes) else \
                f"MISMATCH total={total} sizes={sizes}"
        nums = sorted(d["num"] for d in docs if d["num"] is not None)
        top = nums[-1] if nums else 0
        return "ok(top_no==count, contiguous)" if nums == list(range(1, top + 1)) and top == total else \
            f"MISMATCH top={top} count={total} numbered={len(nums)}"

    # ---- 상세 ----
    def _details(self, m: dict, docs: list[dict], s: dict) -> None:
        ctx = self.ctx
        mag = m["kind"] == "magazine"
        tokens = (b"webZineView",) if mag else (b"statistics-view-wrap", b'class="view-box"')
        fetched = cached = failed = no_att = new_atts = empty_href = 0
        for d in docs:
            ctx.check_time()
            if self._skip_permanent(d["doc_id"], "detail"):
                failed += 1
                continue
            if self.max_details is not None and self.details_fetched >= self.max_details:
                if ctx.manifest.attachments_for(d["doc_id"]):
                    continue                      # 이미 상세를 처리한 문서
                s["details_capped"] = True
                break
            try:
                r = self._get(d["url"], *tokens)
            except _NET_ERRORS as e:
                failed += 1
                self._fail(d["doc_id"], "detail", getattr(e, "error_class", "error"), str(e))
                continue
            if not r.ok:
                failed += 1
                self._fail(d["doc_id"], "detail", "bad_page", r.content_error or f"http {r.status}")
                continue
            self.guard.ok()
            if not r.from_cache:
                fetched += 1
                self.details_fetched += 1
            else:
                cached += 1
            h = r.text()
            p = parse_magazine_detail(h) if mag else parse_board_detail(h)
            atts = p["attachments"]
            empty_href += p.get("empty_href", 0)
            with ctx.manifest.transaction():
                existing = {a["url"]: a["att_id"] for a in ctx.manifest.attachments_for(d["doc_id"])}
                used = set(existing.values())
                for a in atts:
                    if a["kind"] == "full" and a["label"] in ("PDF", ""):
                        a["label"] = (p.get("title") or d["title"]) + " (전체 PDF)"
                    att_id = existing.get(a["url"]) or f"{d['doc_id']}#{att_number(a['url'], a['kind'])}"
                    if att_id in used and a["url"] not in existing:
                        att_id = f"{d['doc_id']}#{zlib.crc32(a['url'].encode()) & 0x7FFFFFFF}"
                    used.add(att_id)
                    if ctx.manifest.upsert_attachment(att_id, d["doc_id"], a["url"], label=a["label"], kind=a["kind"]) == "new":
                        new_atts += 1
                        if withhold.viewer_shell_reason(a):            # G-D8: e-book 뷰어 껍데기는 처음부터 보류
                            withhold.mark_viewer_shell(ctx.manifest, att_id, d["doc_id"])
                if p.get("empty_href"):
                    ctx.manifest.update_document_fields(d["doc_id"], extra_merge={"empty_href_items": p["empty_href"]})
            if atts:
                ctx.manifest.resolve_failures(d["doc_id"], "detail")
            else:
                no_att += 1
                ctx.manifest.record_failure(d["doc_id"], "detail", "no_attachment_links", "detail page lists no attachment links")
        s.update(details_fetched=fetched, details_cached=cached, details_failed=failed, docs_without_attachments=no_att,
                 new_attachments=new_atts, empty_href_items=empty_href)

    # ---- 첨부 HEAD ----
    def _heads(self, m: dict, docs: list[dict], s: dict) -> None:
        ctx = self.ctx
        ok = bad = skipped = pending = 0
        total_atts = total_bytes = 0
        capped = False
        for d in docs:
            ctx.check_time()
            files: dict = {}
            for a in ctx.manifest.attachments_for(d["doc_id"]):
                total_atts += 1
                if a["head_checked_at"]:
                    ok += 1
                    total_bytes += a["content_length"] or 0
                    continue
                if "/_common/" not in a["url"] or "download_file" not in a["url"]:
                    skipped += 1                  # e-book 등 HTML 뷰어 링크: HEAD 대상이 아니다
                    continue
                if self._skip_permanent(a["att_id"], "head"):
                    bad += 1
                    continue
                if capped or (self.max_heads is not None and self.heads_sent >= self.max_heads):
                    capped = True
                    pending += 1
                    continue
                self.heads_sent += 1
                try:
                    r = ctx.http.head(a["url"])
                except _NET_ERRORS as e:
                    bad += 1
                    self._fail(a["att_id"], "head", getattr(e, "error_class", "error"), str(e))
                    continue
                cl = (r.headers.get("content-length") or "").strip()
                ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                if not (200 <= r.status < 300):
                    bad += 1
                    self._fail(a["att_id"], "head", f"http_{r.status}", f"HEAD status {r.status}")
                    continue
                if not cl.isdigit() or int(cl) <= 0 or ctype == "text/html":
                    ctx.http.evict(a["url"], method="HEAD")
                    bad += 1
                    self._fail(a["att_id"], "head", "head_unusable", f"len={cl or '-'} type={ctype or '-'}")
                    continue
                self.guard.ok()
                ctx.manifest.update_attachment_head(a["att_id"], content_length=int(cl), content_type=ctype)
                ctx.manifest.resolve_failures(a["att_id"], "head")
                cd = r.headers.get("content-disposition") or ""
                fm = re.search(r"filename=([^;]+)", cd)
                if fm:
                    files[a["att_id"]] = urllib.parse.unquote(fm.group(1).strip().strip('"'))
                ok += 1
                total_bytes += int(cl)
            if files:
                cur = json.loads((ctx.manifest.get_document(d["doc_id"]) or {}).get("extra_json") or "{}").get("att_files", {})
                ctx.manifest.update_document_fields(d["doc_id"], extra_merge={"att_files": {**cur, **files}})
        s.update(attachments=total_atts, head_ok=ok, head_failed=bad, head_skipped_non_file=skipped, head_pending=pending,
                 bytes_known=total_bytes, heads_capped=capped)

    # ---- 고정 페이지 ----
    def _static(self, m: dict, s: dict) -> None:
        ctx = self.ctx
        url = entry_url(m)
        doc_id = f"pac_other:static{m['p']}"
        try:
            r = self._get(url, b"print-contents")
        except _NET_ERRORS as e:
            self._fail(url, "list", getattr(e, "error_class", "error"), str(e))
            s["failed"] = True
            return
        if not r.ok:
            self._fail(url, "list", "bad_page", r.content_error or f"http {r.status}")
            s["failed"] = True
            return
        self.guard.ok()
        p = parse_static(r.text())
        new = ctx.manifest.upsert_document(doc_id, m["source_id"], canonical_url=url, title=m["title"], doc_type=m["doc_type"],
                                           detail_url=url, extra={"menu": m["key"], "page_sha256": r.sha256})
        ctx.manifest.upsert_list_page(m["source_id"], m["p"], url, http_status=r.status, item_count=1, body_sha256=r.sha256, ok=True)
        n_new = 0
        for a in p["attachments"]:
            att_id = f"{doc_id}#{zlib.crc32(a['url'].encode()) & 0x7FFFFFFF}"
            if ctx.manifest.upsert_attachment(att_id, doc_id, a["url"], label=a["label"], kind=a["kind"]) == "new":
                n_new += 1
                if withhold.viewer_shell_reason(a):
                    withhold.mark_viewer_shell(ctx.manifest, att_id, doc_id)
        s.update(docs=1, new_docs=int(new == "new"), new_attachments=n_new, attachments=len(p["attachments"]))
        if self.heads:
            ok = bad = 0
            for a in ctx.manifest.attachments_for(doc_id):
                if a["head_checked_at"]:
                    ok += 1
                    continue
                if self._skip_permanent(a["att_id"], "head"):
                    bad += 1
                    continue
                if self.max_heads is not None and self.heads_sent >= self.max_heads:
                    break
                self.heads_sent += 1
                try:
                    hr = ctx.http.head(a["url"])
                except _NET_ERRORS as e:
                    bad += 1
                    self._fail(a["att_id"], "head", getattr(e, "error_class", "error"), str(e))
                    continue
                cl = (hr.headers.get("content-length") or "").strip()
                ctype = (hr.headers.get("content-type") or "").split(";")[0].strip().lower()
                if not (200 <= hr.status < 300) or not cl.isdigit() or int(cl) <= 0 or ctype == "text/html":
                    if 200 <= hr.status < 300:
                        ctx.http.evict(a["url"], method="HEAD")
                    bad += 1
                    self._fail(a["att_id"], "head", f"http_{hr.status}" if not (200 <= hr.status < 300) else "head_unusable",
                               f"HEAD status {hr.status} len={cl or '-'} type={ctype or '-'}")
                    continue
                self.guard.ok()
                ctx.manifest.update_attachment_head(a["att_id"], content_length=int(cl), content_type=ctype)
                ctx.manifest.resolve_failures(a["att_id"], "head")
                ok += 1
            s.update(head_ok=ok, head_failed=bad)

    # ---- 메뉴 발견 ----
    def discover(self, result: dict) -> None:
        unlisted = sorted(t for t in self.tabs if t not in KNOWN_TAB_KEYS)
        result["unlisted_tabs"] = [{"p": t[0], "magazine_new": t[1], "cate": t[2]} for t in unlisted]

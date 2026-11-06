"""법원도서관 법고을(lx.scourt.go.kr) 판례 보강 어댑터(UDEC-24): ``python -m press_review.ingestion lx_prec --number 2014다61654 [--numbers-file F]``.

비회원 이용 범위의 판례(탭 tab_idx=0)만 받는다. 문헌은 받지 않고 로그인·CAPTCHA·접근통제는 우회하지 않는다. 키·쿠키·토큰이 필요 없고 대상은 호출자가 준 번호뿐이다
(대상 번호는 ``ingestion/lx_targets.py`` 가 색인과 장부에서 결정적으로 만든다).
번호마다 목록(``/search/precedent/get/list``, 본문 검색이라 그 번호를 인용한 다른 판결도 나온다) → 요약 제목의 번호 집합이 정확히 일치하는 항목(병합 표기 포함)만
→ showViewerPopup 첫 인자가 P01 인 항목은 상세(``/search/detail/precedent/0/<key>``, 전문이 HTML)를 받는다. P04·P05·P06·I01 은 PDF 경로라 건너뛰고 ``skipped_pdf`` 로 센다.
같은 번호의 정확 일치가 법원별로 여럿이면 항목별 문서(doc_id ``lx_prec:<key>``)로 등록하고 multi 로 센다(ccourt_detc 의 multi_in_progress 방식·재개 규칙).

쪽 구성: 상세 HTML 의 h2 id 표지를 따른다(판시사항 pansi·판결요지 JudgementNote·참조조문 RefLaw·참조판례 RefCase·원심판결 SubjectDecision·주문 OutCome·이유 Reason·서명 judges).
당사자(Party*)와 article 바깥의 UI 문구('법령 전문보기'·북마크)는 쪽에 넣지 않는다. 주문·이유·서명을 이어 붙인 전문 텍스트를 ``lawapi.split_sections`` 로 나눠
의견 구간(반대·별개·보충)을 표제로 가르고, 판결요지는 줄 머리 ``[다수의견]``·``[…의 반대의견]`` 표지로 가른다. 표지를 못 찾았는데 의견 언급이 있으면(unsplit) 해당 쪽을
needs_review 로 둔다. 하류 계약은 ``structure_records.detc_majority_barred``: 법정의견이 아닌 구간·needs_review 쪽의 인용은 court_majority 로 쓰지 못한다(프로필 court_judgment).

접근 정책: 요청 간격 5초 이상·재시도 없음·동시 요청 없음(http.py 의 HOST_MIN_INTERVAL·NO_RETRY_HOSTS). HTTP 403·429·로그인 요구·CAPTCHA·비정상 접근 안내가 나오면 즉시 전체 중단
(error_class access_stopped). 그 밖의 실패는 연속 3회에서 멈춘다. 중단 문구는 구조가 맞지 않는 응답에서만 찾는다(정상 목록·상세는 머리글에 '로그인' 링크가 있고 본문에도 흔한 단어가 나온다).
권리 게이트: 소스 등록 뒤 요청 전에 ``require_source_rights(read, store_raw, create_derivatives)``.
실패 장부: target ``lx_no:<번호>``, stage ``resolve``, error_class = not_found | pdf_only | already_covered | page_overflow | serial_mismatch | list_format | detail_format |
malformed_number | http_*.
"""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
import urllib.parse
from datetime import date
from typing import Any, Optional

from ...case_numbers import canon_year
from ..http import FetchFailed, IngestionError
from ..rights import RightsNotRecorded, require_source_rights
from . import lawapi as L
from .detc_parse import STATUS_REVIEW, STATUS_UNSPLIT

SOURCE_ID = "lx_prec"
FAMILY = "lx_scourt"
DOC_PREFIX = "lx_prec"
HOST = "lx.scourt.go.kr"
BASE = f"https://{HOST}"
LIST_ENDPOINT = BASE + "/search/precedent/get/list"
DETAIL_PREFIX = BASE + "/search/detail/precedent/0/"
RAW_DIR = "raw/lx_scourt"
TEXT_DIR = "text/lx_prec"
STATE_FILE = "lx_prec.json"
EXTRACTOR_VERSION = "lx-html-1"          # 쪽 구성 규칙을 바꾸면 올린다
LIST_ROWS = 100
MAX_LIST_PAGES = 5
HTML_PTYPE = "P01"                        # showViewerPopup 첫 인자. 이 값만 상세 HTML 경로다(P04·P05·P06·I01 은 PDF)
FAIL_PREFIX = "lx_no:"
FAIL_STAGE = "resolve"
BODY_STAGE = "body"
MAX_RETRIES = 3
NO_RETRY_CLASSES = ("malformed_number",)
SKIP_CLASSES = ("not_found", "already_covered", "pdf_only")      # 다시 요청해도 달라지지 않는다: --retry-not-found 로만 재시도
HEADNOTE_SECTIONS = (("pansi", "판시사항", "headnote_issue", 0), ("JudgementNote", "판결요지", "headnote_holding", 1),
                     ("RefLaw", "참조조문", "ref_statutes", 2), ("RefCase", "참조판례", "ref_cases", 3))
LOWER_COURT_IDX = 4
JUDGMENT_FIRST_IDX = 5
HEAD_SPLIT_FIRST_IDX = 500               # 판결요지를 의견별로 나눈 쪽의 번호(500~)
HEADER_LOC = L.HEADER_LOC
SKIP_SECTION_IDS = ("junmun",)           # '【전 문】' 표제만 있는 구간
NON_JUDGMENT_IDS = {s[0] for s in HEADNOTE_SECTIONS} | {"SubjectDecision", "judges"} | set(SKIP_SECTION_IDS)   # 판결 전문 텍스트에 넣지 않는 구간
BODY_KINDS = ("order", "reasons", "opinion", "body")

# 법원 사건부호(헌재의 '헌' 계열은 대상이 아니다). 긴 부호부터 맞추도록 길이 역순으로 정렬한다.
CASE_CODES = tuple(sorted({
    "가합", "가단", "가소", "가", "나", "다", "라", "마", "바", "사", "아", "자", "차", "타", "파", "하", "거", "너", "더", "러", "머", "버", "서", "어", "저", "커", "터", "퍼", "허",
    "카합", "카단", "카기", "카확", "카불", "카담", "카구", "카공", "카", "고합", "고단", "고정", "고약", "고약전", "고사", "고", "노", "도", "로", "모", "보", "소", "오", "조",
    "초기", "초재", "초보", "초사", "초", "누", "두", "구합", "구단", "구", "수", "주", "즈합", "즈단", "즈기", "즈", "추", "스", "그", "느", "드", "므", "브",
    "재가합", "재가단", "재나", "재다", "재라", "재마", "재누", "재두", "재고합", "재고단", "재노", "재도", "감고", "감노", "감도", "전도", "전고", "후"}, key=lambda c: (-len(c), c)))
CODE_ALT = "|".join(CASE_CODES)
NUMBER_RE = re.compile(rf"(\d{{2,4}})({CODE_ALT})(\d{{1,7}})")


class RightsStop(IngestionError):
    """소스 권리(read·store_raw·create_derivatives)가 allowed 로 기록되지 않았다. CLI 는 status=failed, error_class=rights_not_recorded, 종료 코드 1 로 낸다."""
    error_class = "rights_not_recorded"


class AccessStop(IngestionError):
    """접근 거부·과다 요청·로그인 요구·CAPTCHA·비정상 접근 안내. 남은 번호도 같은 결과이므로 즉시 전체를 멈춘다(재시도 없음)."""
    error_class = "access_stopped"


class DetailParseError(ValueError):
    pass


# ---------------------------------------------------------------- 사건번호
def canon_number(raw: str) -> Optional[str]:
    """'97도158'·'1997도158' → '1997도158'. 형식 밖이면 None."""
    m = NUMBER_RE.fullmatch(raw)
    if not m:
        return None
    y = canon_year(m.group(1))
    return f"{y}{m.group(2)}{m.group(3)}" if y else None


def to_lx_form(canonical: str) -> str:
    """법고을 인쇄 형식: 1900년대는 2자리 연도(97도158), 2000년 이후는 4자리."""
    m = NUMBER_RE.fullmatch(canonical)
    if not m or not m.group(1).startswith("19") or len(m.group(1)) != 4:
        return canonical
    return m.group(1)[2:] + m.group(2) + m.group(3)


def normalize_number(raw: Any) -> Optional[str]:
    """NFKC·공백 제거 후 형식 검사해 법고을 인쇄 형식으로 돌려준다. 형식 밖(헌재 번호·끝 마침표·단기 연호)이면 None(자동 보정 금지)."""
    s = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(raw or "")))
    c = canon_number(s)
    return to_lx_form(c) if c else None


# ---------------------------------------------------------------- URL·목록 파싱
def _enc(q: list) -> str:
    return urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def list_url(number: str, page: int = 1) -> str:
    """파라미터 순서는 캐시 키라 고정. tab_idx 를 빼면 HTTP 500 이다(판례 탭 0)."""
    return LIST_ENDPOINT + "?" + _enc([("search_txt", number), ("tab_idx", "0"), ("page_no", str(page)), ("display", str(LIST_ROWS)),
                                       ("order_by", "desc"), ("order_column", "d"), ("search_mode", "simple")])


def detail_url(key: str) -> str:
    if not re.fullmatch(r"\d{1,20}", key or ""):
        raise ValueError("lx key must be digits")
    return DETAIL_PREFIX + key


def public_url(key: str) -> str:
    """이용자가 열어 볼 공개 상세 주소(키·토큰 없음)."""
    return detail_url(key)


_ROW = re.compile(r'<li\s+class="search_result_item"')          # 행 경계. id·제목을 읽을 수 있는지와 무관하게 모든 행을 센다
_LI_ID = re.compile(r'\bid="([^"]*)"')
_CTG = re.compile(r'<span\s+class="ctg[^"]*"[^>]*>\s*([^<]*?)\s*</span>')
_POP = re.compile(r"showViewerPopup\(\s*'([A-Za-z]\d{2})'\s*,\s*'([^']*)'\s*,\s*'([^']*)'")
_TITLE = re.compile(r'class="summary_title"[^>]*>(.*?)</div>', re.S)
_TOTAL = re.compile(r"총\s*<span[^>]*>\s*([\d,]+)\s*</span>")
_ITEM_TITLE = re.compile(r"^\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(?:선고|자)\s+(?P<nos>.+?)\s+(?:전원합의체\s*)?(?P<form>판결|결정|명령|판정)(?:\s*\[(?P<name>.*)\])?\s*$", re.S)


def _plain(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def _iso(y: str, m: str, d: str) -> Optional[str]:
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def parse_list(text: str) -> dict:
    """목록 HTML → {'total': 총건수|None, 'items': [{key, court, ptype, title, numbers, date, form, case_name}], 'malformed': 키·제목을 읽지 못한 항목 수, 'rows': 목록의 원래 항목 수(파싱 실패 포함)}.
    항목은 li.search_result_item 블록 단위다. 법원은 ctg 칸, 유형은 블록의 첫 showViewerPopup(뒤따르는 주석 사본은 쓰지 않는다), 번호·선고일은 요약 제목이다."""
    total = _TOTAL.search(text)
    marks = list(_ROW.finditer(text))
    items, malformed = [], 0
    for n, m in enumerate(marks):
        blk = text[m.start(): marks[n + 1].start() if n + 1 < len(marks) else len(text)]
        idm = _LI_ID.search(blk[:blk.find(">") + 1 if ">" in blk else len(blk)])
        key = idm.group(1).strip() if idm else ""
        pop, ctg, tit = _POP.search(blk), _CTG.search(blk), _TITLE.search(blk)
        if not re.fullmatch(r"\d{1,20}", key) or not pop or pop.group(3) != key or not tit:
            malformed += 1
            continue
        title = _plain(tit.group(1))
        t = _ITEM_TITLE.match(title)
        items.append({"key": key, "court": _plain(ctg.group(1)) if ctg else "", "ptype": pop.group(1).upper(), "title": title,
                      "numbers": L.split_case_numbers(unicodedata.normalize("NFKC", t.group("nos"))) if t else [],
                      "date": _iso(t.group(1), t.group(2), t.group(3)) if t else None, "form": t.group("form") if t else None,
                      "case_name": _plain(t.group("name")) if t and t.group("name") else None})
        if not t:
            malformed += 1
    return {"total": int(total.group(1).replace(",", "")) if total else None, "items": items, "malformed": malformed, "rows": len(marks)}


def norm_court(name: Any) -> str:
    """법원 표기 정규화: 서울고법=서울고등법원, 서울중앙지법=서울중앙지방법원, 서울행법=서울행정법원(공백 무시). 목록은 정식 명칭, 상세 제목은 약칭을 쓰는 경우가 있다."""
    s = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(name or "")))
    return s.replace("지법", "지방법원").replace("고법", "고등법원").replace("가법", "가정법원").replace("행법", "행정법원")


def exact_items(number: str, items: list) -> list:
    """제목 번호 집합에 ``number`` 가 있는 항목만(부분문자열·인용 판결 배제). 2자리·4자리 연도 표기는 같은 번호로 본다."""
    want = canon_number(number) or number
    return [i for i in items if want in {canon_number(t) or t for t in i.get("numbers") or []}]


# ---------------------------------------------------------------- 응답 판정(수신 단계: 캐시에 쓰기 전)
_LIST_OK = re.compile(r'class="search_result_list"')
_LIST_ZERO = re.compile(r"총\s*<span[^>]*>\s*0\s*</span>")
_ARTICLE = re.compile(r'<article\b[^>]*class="[^"]*\bsentencing\b[^"]*"[^>]*>(.*?)</article>', re.S)
_CASE_NUM = re.compile(r'id="case_num"[^>]*\bvalue="([^"]*)"')
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
# 구조가 맞지 않는 응답에서만 찾는 중단 문구. 정상 페이지의 머리글에는 '로그인' 링크가 있다.
BLOCK_PATTERNS = (("login_required", re.compile(r"로그인이\s*필요|로그인\s*후\s*(?:이용|확인|조회|열람)|회원\s*전용")),
                  ("captcha", re.compile(r"captcha|자동\s*입력\s*방지|보안\s*문자", re.I)),
                  ("abnormal_access", re.compile(r"비정상(?:적)?(?:인)?\s*(?:으로\s*)?접근|접근이\s*(?:제한|차단)|과도한\s*요청|too\s+many\s+requests|이용이\s*제한", re.I)))


def _bad_response(text: str, kind: str) -> str:
    for name, rx in BLOCK_PATTERNS:
        if rx.search(text):
            return f"blocked:{name}"
    return f"{kind}_format"


def accept_list(body: bytes, headers: dict) -> Optional[str]:
    """정상 목록(항목 목록 또는 0건 표지)이면 None. 아니면 'blocked:<종류>' 또는 'list_format'. 오류 응답을 캐시에 쓰지 않으려는 검사."""
    text = body.decode("utf-8", "replace")
    return None if (_LIST_OK.search(text) or _LIST_ZERO.search(text)) else _bad_response(text, "list")


def _detail_numbers(text: str) -> set:
    out = set()
    m = _CASE_NUM.search(text)
    for t in L.split_case_numbers(unicodedata.normalize("NFKC", m.group(1))) if m else []:
        out.add(canon_number(t) or t)
    h = _H1.search(text)
    if h:
        for t in re.findall(r"\d{2,4}[가-힣]{1,4}\d+", unicodedata.normalize("NFKC", _plain(h.group(1)))):
            out.add(canon_number(t) or t)
    return out


_H1_HEAD = re.compile(r"^(?P<court>.+?)\s+(?P<y>\d{4})\.\s*(?P<m>\d{1,2})\.\s*(?P<d>\d{1,2})\.\s*(?:선고|자)(?:\s|$)")


def detail_mismatches(text: str, number: str, item: Optional[dict]) -> list:
    """상세 응답이 요청한 목록 항목의 판결인지 가린다. 불일치 목록(빈 목록이면 일치). 요청 번호 자체와 목록 번호가 모두 상세의 번호 집합에 있어야 하고
    (겹치는 번호가 하나 있다는 것으로는 부족), 목록과 상세 제목의 선고일·법원(약칭 정규화)이 같아야 한다. 상세 제목을 읽지 못하면 선고일·법원은 비교하지 않는다."""
    nums = _detail_numbers(text)
    out = []
    want = {canon_number(number) or number}
    if item:
        want |= {canon_number(t) or t for t in item.get("numbers") or []}
    missing = sorted(want - nums)
    if missing:
        out.append({"field": "case_no", "missing": missing, "body": sorted(nums)})
    h1 = _H1.search(text)
    parts = _H1_HEAD.match(_plain(h1.group(1))) if h1 else None     # 제목 끝(판결·결정, ': 항소' 같은 꼬리, 잘린 '결')은 따지지 않고 법원·선고일만 읽는다
    iso = _iso(parts.group("y"), parts.group("m"), parts.group("d")) if parts else None
    if item and (item.get("date") or item.get("court")):
        if not parts or (item.get("date") and not iso):         # 목록에 법원·선고일이 있으면 상세 제목과 유효한 날짜가 필요하다(목록값으로 보충하지 않는다)
            out.append({"field": "detail_title", "list": [item.get("court"), item.get("date")], "body": _plain(h1.group(1)) if h1 else None})
        else:
            if item.get("date") and item["date"] != iso:
                out.append({"field": "decision_date", "list": item["date"], "body": iso})
            if item.get("court") and norm_court(item["court"]) != norm_court(parts.group("court")):
                out.append({"field": "court", "list": item["court"], "body": parts.group("court")})
    return out


def accept_detail(body: bytes, headers: dict, number: str, item: Optional[dict] = None) -> Optional[str]:
    """상세는 article 과 사건번호 칸이 있고 ``detail_mismatches`` 가 비어야 한다(다른 판결 응답은 serial_mismatch). 캐시에 쓰기 전 검사."""
    text = body.decode("utf-8", "replace")
    if not (_ARTICLE.search(text) and _CASE_NUM.search(text)):
        return _bad_response(text, "detail")
    return "serial_mismatch" if detail_mismatches(text, number, item) else None


# ---------------------------------------------------------------- 추출(순수 함수: 입출력 없음)
def _text(frag: str) -> str:
    """HTML 조각 → 줄 단위 텍스트(문단=줄). 태그를 지우고 엔티티를 풀며 빈 줄과 줄 안 연속 공백을 없앤다. 저장 텍스트와 offset 은 모두 이 결과 기준이다."""
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", frag)
    s = re.sub(r"(?i)<br\s*/?>|</(?:div|p|tr|li|h[1-6])>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ").replace("\r", "")
    lines = (re.sub(r"[ \t　]+", " ", ln).strip() for ln in s.split("\n"))
    return "\n".join(ln for ln in lines if ln)


_SECTION_MARK = re.compile(r'<h([23])\b[^>]*\bid="([A-Za-z0-9_]+)"[^>]*>(.*?)</h\1>', re.S)
_H1_PARTS = re.compile(r"^(?P<court>.+?)\s+(?P<y>\d{4})\.\s*(?P<m>\d{1,2})\.\s*(?P<d>\d{1,2})\.\s*(?:선고|자)\s+(?P<nos>.+?)\s+(?:전원합의체\s*)?(?P<form>판결|결정|명령|판정)\s*$")


def split_article(text: str) -> tuple[dict, dict]:
    """상세 HTML → ({h2 id: (표제 텍스트, 본문 텍스트)}, {'court','date','numbers','form'} h1 값). article 이 없으면 DetailParseError.
    당사자 구간(Party*)은 돌려주지 않는다. 같은 id 가 여럿이면 앞의 것을 둔다."""
    art = _ARTICLE.search(text)
    if not art:
        raise DetailParseError("no_article")
    body = art.group(1)
    marks = list(_SECTION_MARK.finditer(body))
    secs: dict = {}
    for n, m in enumerate(marks):
        sid = m.group(2)
        if sid.startswith("Party") or sid in secs:
            continue
        end = marks[n + 1].start() if n + 1 < len(marks) else len(body)
        secs[sid] = (_text(m.group(3)), _text(body[m.end():end]))
    h1 = _H1.search(text)
    parts = _H1_PARTS.match(_plain(h1.group(1))) if h1 else None
    head = {}
    if parts:
        head = {"court": parts.group("court"), "date": _iso(parts.group("y"), parts.group("m"), parts.group("d")), "date_text": f"{int(parts.group('y'))}. {int(parts.group('m'))}. {int(parts.group('d'))}.",
                "numbers": re.sub(r"\s+", " ", parts.group("nos")).strip(), "form": parts.group("form")}
    return secs, head


_HN_MARK = re.compile(r"\[([^\[\]\n]{0,160}?(?:다수|반대|별개|보충|소수)의견)\]")
_HN_LINE_MARK = re.compile(r"^[ \t　]*(?:\[\d{1,2}\][ \t　]*)?" + _HN_MARK.pattern)
_HN_ITEM = re.compile(r"^[ \t　]*\[\d{1,2}\]")
_HN_MENTION = re.compile(r"(?:대법관|대법원장)[^\n]{0,150}(?:반대|별개|보충)의견")


def split_headnote(text: str) -> tuple[list, dict]:
    """판결요지를 줄 머리 의견 표지(``[다수의견]``·``[대법관 …의 반대의견]``, 앞의 ``[1]`` 항목 머리 허용)로 가른다. 반대·별개·보충 구간 뒤에 새 항목 머리(``[2]``)가
    나오면 법정 요지로 돌아간다. 반환 구간 {text, label, opinion_kind, char_start, char_end, needs_review}, info {status, marker_count}.
    status: none(의견 언급 없음) | split | unsplit(표지는 못 찾았거나 줄 머리가 아닌 표지가 있는데 의견 언급이 있다: 쪽 전체를 needs_review)."""
    lines = text.split("\n")
    offs, o = [], 0
    for ln in lines:
        offs.append(o)
        o += len(ln) + 1
    marks, in_op, line_marks = [], False, 0
    for i, ln in enumerate(lines):
        m = _HN_LINE_MARK.match(ln)
        if m:
            line_marks += 1
            kind = L.classify_opinion(m.group(1))
            marks.append((offs[i], m.group(1), kind))
            in_op = kind not in (None, "majority")
        elif in_op and _HN_ITEM.match(ln):
            marks.append((offs[i], "", "majority"))
            in_op = False
    anywhere = len(_HN_MARK.findall(text))
    whole = [{"text": text, "label": "", "opinion_kind": None, "char_start": 0, "char_end": len(text), "needs_review": False}]
    if not marks:
        if anywhere or _HN_MENTION.search(text):
            return [{**whole[0], "needs_review": True}], {"status": STATUS_UNSPLIT, "marker_count": 0}
        return whole, {"status": "none", "marker_count": 0}
    if anywhere > line_marks:                     # 줄 머리가 아닌 자리에 표지가 있다: 어디까지가 어느 의견인지 모른다
        return [{**whole[0], "needs_review": True}], {"status": STATUS_REVIEW, "marker_count": line_marks}
    bounds = ([(0, "", "majority")] if marks[0][0] > 0 else []) + marks
    out = []
    for n, (st, label, kind) in enumerate(bounds):
        en = bounds[n + 1][0] if n + 1 < len(bounds) else len(text)
        if text[st:en].strip():
            out.append({"text": text[st:en], "label": label, "opinion_kind": kind, "char_start": st, "char_end": en, "needs_review": kind is None})
    return out, {"status": "split", "marker_count": line_marks}


def build_extraction(text: str, it: dict) -> dict:
    """상세 HTML 텍스트 + 목록 항목 → 쪽 목록·본문 메타. 네트워크·파일을 쓰지 않는다. article 이 없으면 DetailParseError."""
    secs, head = split_article(text)
    warnings = [f"unknown_section:{k}" for k in secs if k not in {s[0] for s in HEADNOTE_SECTIONS} | {"SubjectDecision", "OutCome", "Reason", "judges"} | set(SKIP_SECTION_IDS)]
    printed = head.get("numbers") or " ".join(it.get("numbers") or [])
    case_nos = L.split_case_numbers(unicodedata.normalize("NFKC", printed))
    iso = head.get("date") or it.get("date")
    court = head.get("court") or it.get("court") or ""
    pages: list = []
    lines = [f"{k}: {v}" for k, v in (("사건번호", printed), ("선고일자", head.get("date_text") or ""), ("법원명", court)) if v]
    if lines:
        h = "\n".join(lines)
        pages.append({"idx": -1, "loc": HEADER_LOC, "kind": "case_header", "field": "사건표지", "label": "사건표지", "opinion_kind": None,
                      "char_start": 0, "char_end": len(h), "text": h, "needs_review": False})
    hinfo = {"status": "none", "marker_count": 0}
    for sid, name, kind, idx in HEADNOTE_SECTIONS:
        txt = secs.get(sid, ("", ""))[1]
        if not txt.strip():
            continue
        if kind != "headnote_holding":
            pages.append({"idx": idx, "kind": kind, "field": name, "label": name, "opinion_kind": None, "char_start": 0, "char_end": len(txt), "text": txt, "needs_review": False})
            continue
        hsecs, hinfo = split_headnote(txt)
        if hinfo["status"] != "split":
            pages.append({"idx": idx, "kind": kind, "field": name, "label": name, "opinion_kind": None, "char_start": 0, "char_end": len(txt), "text": txt,
                          "needs_review": hsecs[0]["needs_review"]})
            continue
        for k, s in enumerate(hsecs):
            pages.append({"idx": HEAD_SPLIT_FIRST_IDX + k, "kind": "headnote_holding" if s["opinion_kind"] == "majority" else "headnote_opinion", "field": name,
                          "label": s["label"] or name, "opinion_kind": s["opinion_kind"], "char_start": s["char_start"], "char_end": s["char_end"],
                          "text": s["text"], "needs_review": s["needs_review"]})
    lower = secs.get("SubjectDecision", ("", ""))[1]
    if lower.strip():
        pages.append({"idx": LOWER_COURT_IDX, "kind": "lower_court", "field": "원심판결", "label": "원심판결", "opinion_kind": None, "char_start": 0,
                      "char_end": len(lower), "text": lower, "needs_review": False})
    parts = []
    default_label = {"OutCome": "【주 문】", "Reason": "【이 유】"}
    for sid, (label, body) in secs.items():                  # 주문·이유와 그 밖의 구간(별지·청구취지 등)을 문서 순서대로. 서명(judges)은 맨 뒤에 둔다
        if sid in NON_JUDGMENT_IDS or not body.strip():
            continue
        title_line = (label or default_label.get(sid, "")).strip()
        parts.append(title_line + "\n" + body if title_line else body)
    judges = secs.get("judges", ("", ""))[1]
    judgment = "\n".join(parts + ([judges] if judges.strip() and parts else []))
    if judgment.strip():
        jsecs, op = L.split_sections(judgment)
    else:
        jsecs, op = [], {"status": "none", "markers": [], "marker_count": 0}
        warnings.append("empty_judgment_text")
    for j, sec in enumerate(jsecs):
        if sec["text"].strip():
            pages.append({**sec, "idx": JUDGMENT_FIRST_IDX + j, "field": "전문", "needs_review": bool(op["status"] == "unsplit" and sec["kind"] in ("reasons", "opinion", "body"))})
    if hinfo["status"] in (STATUS_UNSPLIT, STATUS_REVIEW):
        warnings.append(f"headnote_opinions_{hinfo['status']}")
    if op["status"] == "unsplit":
        warnings.append("judgment_opinions_unsplit")
    mism = []
    list_nos = [canon_number(t) or t for t in it.get("numbers") or []]
    if list_nos and not set(list_nos) <= {canon_number(t) or t for t in case_nos}:
        mism.append({"field": "case_no", "list": it.get("numbers"), "body": printed})
    if it.get("date") and iso and it["date"] != iso:
        mism.append({"field": "decision_date", "list": it["date"], "body": iso})
    if it.get("court") and head.get("court") and norm_court(it["court"]) != norm_court(head["court"]):
        mism.append({"field": "court", "list": it["court"], "body": head["court"]})
    return {"pages": pages, "body_case_no": printed or None, "case_nos": case_nos, "decision_date": iso, "court": court, "opinion_status": op["status"],
            "opinion_split": {"status": op["status"], "marker_count": op["marker_count"], "markers": op["markers"]}, "headnote_split": hinfo,
            "mismatches": mism, "warnings": warnings, "fields_chars": {k: len(v[1]) for k, v in secs.items()},
            "text_available": any(p["kind"] in BODY_KINDS for p in pages)}


def persist_extraction(ctx, doc_id: str, key: str, raw_sha: str, raw_rel: str, fetched_at: str, built: dict, ptype: str) -> dict:
    """쪽 파일을 쓰고 파생 행을 지운 뒤 pages 를 다시 쓰고 단계 정보를 갱신한다(순서는 ccourt_detc.persist_extraction 과 같다)."""
    m = ctx.manifest
    rel_dir = f"{TEXT_DIR}/{key}"
    if not ctx.dry_run:
        keep = set()
        for p in built["pages"]:
            name = f"{L.page_loc(p)}.txt"
            keep.add(name)
            L._atomic_write(ctx.corpus_dir / rel_dir / name, p["text"].encode("utf-8"))
        d = ctx.corpus_dir / rel_dir
        if d.is_dir():
            for f in d.glob("[sh][0-9][0-9][0-9].txt"):
                if f.name not in keep:
                    f.unlink()
    body = {
        "fetched_at": fetched_at, "raw_path": raw_rel, "raw_sha256": raw_sha, "format": "html", "extractor_version": EXTRACTOR_VERSION,
        "text_available": built["text_available"], "pages": len(built["pages"]), "fields_chars": built["fields_chars"],
        "body_case_no": built["body_case_no"], "case_nos": built["case_nos"], "opinion_status": built["opinion_status"], "opinion_split": built["opinion_split"],
        "headnote_split": built["headnote_split"], "verify_mismatches": built["mismatches"], "warnings": built["warnings"], "ptype": ptype,
        "section_locator": "page_id suffix #sNNN (#h000 = case header; #s500+ = opinions inside 판결요지); quality_json.char_start/char_end = offsets in the page source text",
    }
    with m.transaction():
        m.delete_derived(doc_id=doc_id)
        for p in built["pages"]:
            loc = L.page_loc(p)
            m.upsert_page(
                f"{doc_id}#{loc}", doc_id, method="html_section", text_path=f"{rel_dir}/{loc}.txt",
                text_sha256=hashlib.sha256(p["text"].encode("utf-8")).hexdigest(), char_count=len(p["text"]),
                quality_json={"locator": loc, "section_kind": p["kind"], "label": p["label"], "field": p["field"], "opinion_kind": p["opinion_kind"],
                              "char_start": p["char_start"], "char_end": p["char_end"], "key": key},
                needs_review=p["needs_review"], extractor_version=EXTRACTOR_VERSION)
        L._set_extra(m, doc_id, "body", body)
        m.update_document_stage(doc_id, extractor_version=EXTRACTOR_VERSION, extract_input_sha256=raw_sha)
        m.set_document_status(doc_id, "extracted")
        m.resolve_failures(doc_id, BODY_STAGE)
    return body


# ---------------------------------------------------------------- 실행
def open_source(manifest) -> None:
    """등록 → 권리 게이트. 소스 행이 없는 첫 실행에서도 소스가 먼저 등록된 뒤 중단된다."""
    manifest.ensure_source(SOURCE_ID, FAMILY, "법원도서관 법고을 판례(비회원 이용 범위)", BASE,
                           notes="목록 /search/precedent/get/list, 상세 /search/detail/precedent/0/<key>; 키·로그인 없음, robots 규칙 없음, 약관 미게시(UDEC-24). "
                                 "권리는 `rights set lx_scourt --source lx_prec` 로 소스 한정 기록")
    try:
        require_source_rights(manifest, SOURCE_ID, needs=("read", "store_raw", "create_derivatives"))     # 같은 명령이 쪽(파생물)까지 만든다
    except RightsNotRecorded as e:
        raise RightsStop(str(e)) from None


def add_arguments(parser) -> None:
    parser.add_argument("--number", action="append", dest="numbers", metavar="N",
                        help="사건번호(반복 가능). 예: python -m press_review.ingestion lx_prec --number 2014다61654")
    parser.add_argument("--numbers-file", default=None, help="줄당 번호 하나(빈 줄·# 시작 줄 무시)")
    parser.add_argument("--limit", type=int, default=None, help="요청할 번호 수 상한(건너뛴 번호는 세지 않는다). 나머지는 skipped_limit 로 센다")
    parser.add_argument("--reparse", action="store_true",
                        help="저장된 raw 로 쪽·extra 를 다시 만든다(요청 0). raw 가 있고 sha 가 장부와 같은 문서만이며, 파생 행은 지워지고 문서는 extracted 로 돌아간다")
    parser.add_argument("--retry-not-found", action="store_true", help="이전에 not_found·already_covered·pdf_only 로 기록된 번호도 다시 처리한다(기본은 건너뜀)")


def collect_numbers(args) -> list:
    raw = list(getattr(args, "numbers", None) or [])
    path = getattr(args, "numbers_file", None)
    if path:
        with open(path, encoding="utf-8-sig") as f:
            raw += [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    if not raw:
        raise IngestionError("no case numbers: give --number N (repeatable) or --numbers-file F")
    return raw


class _Run:
    def __init__(self, ctx, args):
        self.ctx, self.m = ctx, ctx.manifest
        self.retry_nf = bool(getattr(args, "retry_not_found", False))
        self.limit = getattr(args, "limit", None)
        self.consecutive_failures = 0
        self.c = {"numbers_input": 0, "duplicates": 0, "malformed_number": 0, "skipped_done": 0, "skipped_covered": 0, "skipped_exhausted": 0, "skipped_known": 0,
                  "skipped_limit": 0, "skipped_pdf": 0, "not_found": 0, "pdf_only": 0, "page_overflow": 0, "serial_mismatch": 0, "list_format": 0, "detail_format": 0,
                  "failed": 0, "list_items_not_exact": 0, "list_items_malformed": 0, "docs_new": 0, "docs_seen": 0, "resumed_from_raw": 0, "multi_numbers": 0,
                  "requests": 0, "pages_written": 0, "reparsed": 0, "reparse_skipped": 0}

    # -- HTTP
    def bump(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= L.MAX_CONSECUTIVE_FAILURES:
            raise IngestionError(f"{L.MAX_CONSECUTIVE_FAILURES} consecutive request failures; stopping (unverified)")

    def get(self, url: str, accept, refresh: Optional[bool] = None) -> tuple:
        """('ok', Response) | ('fail', error_class). 403·429·로그인·CAPTCHA·비정상 접근 안내는 AccessStop(즉시 전체 중단). 실패 횟수는 호출한 쪽이 장부에 남긴 뒤 bump 한다."""
        ctx = self.ctx
        ctx.check_time()
        self.c["requests"] += 1
        try:
            r = ctx.http.get(url, accept=accept, refresh=refresh)
        except FetchFailed:
            return "fail", "http_fetch_failed"
        if r.status in (403, 429):
            raise AccessStop(f"HTTP {r.status} from {HOST}; stopping all requests")
        if re.search(r"login|logon|captcha", urllib.parse.urlsplit(r.final_url).path, re.I):
            raise AccessStop(f"redirected to a login or captcha page by {HOST}; stopping all requests")
        if r.content_error:
            if r.content_error.startswith("blocked:"):
                raise AccessStop(f"{r.content_error.split(':', 1)[1]} signal from {HOST}; stopping all requests")
            known = r.content_error == "serial_mismatch" or r.content_error.endswith("_format")
            return "fail", r.content_error if known else "http_content_rejected"
        if not r.ok:
            return "fail", f"http_{r.status}"
        self.consecutive_failures = 0
        return "ok", r

    # -- 장부
    def fail(self, number: str, error_class: str, message: str, *, remote: bool = False) -> None:
        self.m.record_failure(FAIL_PREFIX + number, FAIL_STAGE, error_class, message, retry_count=0 if error_class in NO_RETRY_CLASSES else None)
        self.c[error_class if error_class in self.c else "failed"] += 1
        if remote:
            self.bump()

    def held_by_lawgo(self, number: str) -> bool:
        """본문이 있는 법제처 판례 문서(lawgo_prec_supreme, 격리·보류 제외)의 사건번호에 이미 있으면 참(요청하지 않는다: 색인된 번호이거나 구조화만 필요한 번호)."""
        rows = self.m._q("SELECT 1 FROM documents d WHERE d.source_id='lawgo_prec_supreme' AND d.status NOT IN ('quarantined','withheld') "
                         "AND json_extract(d.extra_json,'$.body.text_available')=1 AND (json_extract(d.extra_json,'$.case_no')=? OR EXISTS "
                         "(SELECT 1 FROM json_each(d.extra_json,'$.body.case_nos') j WHERE j.value=?)) LIMIT 1", (number, number))
        return bool(rows)

    def stored_mismatches(self, doc, number: str, it: dict) -> list:
        """이미 받은 문서에 새 번호를 잇기 전의 검증. 검증된 raw(sha 가 장부와 같음)가 있으면 ``detail_mismatches`` 를 원문에 적용하고, 없으면
        장부에 저장된 body.case_nos·법원·선고일로 같은 규칙을 적용한다(목록값으로 보충하지 않는다)."""
        extra = L._doc_extra(doc)
        body = extra.get("body") or {}
        rp = self.ctx.corpus_dir / body["raw_path"] if body.get("raw_path") else None
        if rp is not None and rp.is_file():
            data = rp.read_bytes()
            if hashlib.sha256(data).hexdigest() == body.get("raw_sha256"):
                return detail_mismatches(data.decode("utf-8", "replace"), number, it)
        nums = {canon_number(t) or t for t in body.get("case_nos") or []}
        want = {canon_number(number) or number} | {canon_number(t) or t for t in it.get("numbers") or []}
        out = []
        if want - nums:
            out.append({"field": "case_no", "missing": sorted(want - nums), "body": sorted(nums)})
        if it.get("date") and doc["decision_date"] != it["date"]:
            out.append({"field": "decision_date", "list": it["date"], "body": doc["decision_date"]})
        if it.get("court") and norm_court(it["court"]) != norm_court(extra.get("court")):
            out.append({"field": "court", "list": it["court"], "body": extra.get("court")})
        return out

    # -- 목록
    def list_exact(self, number: str, refresh: bool = False) -> tuple:
        """('ok', 정확 일치 항목, 전체 항목 수) | ('incomplete', [], 항목 수) | ('fail', error_class) | ('overflow', None). 총건수가 한 쪽(100건)을 넘으면 MAX_LIST_PAGES 까지 쪽을 넘긴다.
        쪽 종료는 파싱에 성공한 건수가 아니라 원래 항목 수(rows)와 총건수로 판단한다. 파싱하지 못한 항목이 있거나 총건수만큼 읽지 못했는데 정확 일치가 없으면
        'incomplete'(없다고 확정하지 않는다)."""
        items, total, rows, malformed = [], None, 0, 0
        for page in range(1, MAX_LIST_PAGES + 1):
            kind, val = self.get(list_url(number, page), accept_list, True if refresh else None)
            if kind == "fail":
                return "fail", val
            parsed = parse_list(val.text())
            self.c["list_items_malformed"] += parsed["malformed"]
            total = parsed["total"] if total is None else total
            if total is not None and total > MAX_LIST_PAGES * LIST_ROWS:
                return "overflow", None
            items += parsed["items"]
            rows += parsed["rows"]
            malformed += parsed["malformed"]
            if parsed["rows"] == 0 or (total is None and parsed["rows"] < LIST_ROWS) or (total is not None and rows >= total):
                break                                  # 총건수가 알려져 있으면 짧은 쪽이라는 이유만으로 멈추지 않는다(빈 쪽·총건수 도달·쪽 상한에서 끝)
        exact = exact_items(number, items)
        self.c["list_items_not_exact"] += len(items) - len(exact)
        if not exact and (malformed or (total is not None and rows < total)):
            return "incomplete", [], rows
        return "ok", exact, len(items)

    def event(self, number: str, it: dict, multi: bool) -> bool:
        """판결 하나(목록 항목)를 처리한다. 성공(이미 받은 문서 포함)이면 True, 실패를 장부에 남겼으면 False."""
        ctx, m = self.ctx, self.m
        key = it["key"]
        doc_id = f"{DOC_PREFIX}:{key}"
        merge = {"resolved_numbers": [number], "resolved_via": {number: "lx_list"}, **({"multi": True} if multi else {})}
        doc = m.get_document(doc_id)
        if doc is not None and doc["status"] in L.DONE_STATUSES and not ctx.refresh:
            if self.stored_mismatches(doc, number, it):          # 다른 사건번호·법원·선고일의 목록 항목이 같은 키로 오면 번호를 병합하지 않는다
                self.fail(number, "serial_mismatch", "stored detail does not match the requested list item (resolved_numbers unchanged)")
                return False
            m.update_document_fields(doc_id, extra_merge=merge)
            self.c["docs_seen"] += 1
            return True
        prior = (L._doc_extra(doc).get("body") or {}) if doc is not None else {}
        raw_path = ctx.corpus_dir / prior["raw_path"] if prior.get("raw_path") else None
        if doc is not None and doc["status"] == "fetched" and raw_path is not None and raw_path.is_file() \
                and hashlib.sha256(raw_path.read_bytes()).hexdigest() == prior.get("raw_sha256"):
            raw, fetched_at = raw_path.read_bytes(), prior.get("fetched_at") or ""
            self.c["resumed_from_raw"] += 1
        else:
            kind, val = self.get(detail_url(key), lambda b, h: accept_detail(b, h, number, it))
            if kind == "fail":
                self.fail(number, val, "detail not usable", remote=True)
                return False
            raw, fetched_at = val.body, ctx.now().isoformat(timespec="seconds")
        try:
            built = build_extraction(raw.decode("utf-8", "replace"), it)
        except DetailParseError as e:
            self.fail(number, "detail_format", str(e), remote=True)
            return False
        want = canon_number(number) or number            # 저장된 raw 로 이어 받는 경로도 같은 검증을 거친다(수신 검사는 새로 받을 때만 돈다)
        if want not in {canon_number(t) or t for t in built["case_nos"]} or built["mismatches"] or detail_mismatches(raw.decode("utf-8", "replace"), number, it):
            self.fail(number, "serial_mismatch", "detail does not match the requested list item (nothing registered)")
            return False
        sha = hashlib.sha256(raw).hexdigest()
        raw_rel = f"{RAW_DIR}/lx-{key}.html"
        iso = built["decision_date"]
        if not ctx.dry_run:
            L._atomic_write(ctx.corpus_dir / raw_rel, raw)
        printed = built["body_case_no"] or " ".join(it["numbers"])
        with m.transaction():
            extra = {"key": key, "case_no": printed, "court": built["court"], "raw_date": it.get("date") or "", "ptype": it["ptype"], "list_title": it.get("title"),
                     "list_item": {k: it.get(k) for k in ("court", "numbers", "date", "form", "case_name", "ptype")}, **merge}
            res = m.upsert_document(doc_id, SOURCE_ID, canonical_url=public_url(key), title=it.get("case_name") or f"{built['court']} {printed}".strip(),
                                    decision_date=iso, year=int(iso[:4]) if iso else None, extra=extra)
            if res != "new":
                m.update_document_fields(doc_id, extra_merge=merge)
            L._set_extra(m, doc_id, "body", {**prior, "fetched_at": fetched_at, "raw_path": raw_rel, "raw_sha256": sha, "ptype": it["ptype"]})
            m.set_document_status(doc_id, "fetched")
        body = persist_extraction(ctx, doc_id, key, sha, raw_rel, fetched_at, built, it["ptype"])
        self.c["docs_new" if res == "new" else "docs_seen"] += 1
        self.c["pages_written"] += body["pages"]
        return True

    # -- 저장된 raw 로 다시 추출
    def reparse(self) -> None:
        """네트워크 없음. raw 가 없거나 sha 가 장부와 다르거나 article 이 없으면 건드리지 않고 reparse_skipped 로 센다.
        persist_extraction 이 쪽·chunks·cases 를 지우고 새로 쓰므로 구조화가 끝난 문서도 extracted 로 돌아간다(재구조화 필요)."""
        m, ctx = self.m, self.ctx
        where = f"source_id=? AND status IN ({','.join(repr(x) for x in ('fetched',) + L.DONE_STATUSES)})"
        for r in m._q(f"SELECT doc_id FROM documents WHERE {where} ORDER BY doc_id", (SOURCE_ID,)):
            ctx.check_time()
            doc = m.get_document(r["doc_id"])
            key = doc["doc_id"].split(":", 1)[1]
            extra = L._doc_extra(doc)
            body = extra.get("body") or {}
            p = ctx.corpus_dir / body["raw_path"] if body.get("raw_path") else None
            data = p.read_bytes() if p is not None and p.is_file() else None
            if data is None or hashlib.sha256(data).hexdigest() != body.get("raw_sha256"):
                self.c["reparse_skipped"] += 1
                continue
            it = {**(extra.get("list_item") or {}), "key": key}
            try:
                built = build_extraction(data.decode("utf-8", "replace"), it)
            except DetailParseError:
                self.c["reparse_skipped"] += 1
                continue
            new = persist_extraction(ctx, doc["doc_id"], key, body["raw_sha256"], body["raw_path"], body.get("fetched_at") or "", built, body.get("ptype") or "")
            self.c["reparsed"] += 1
            self.c["pages_written"] += new["pages"]

    # -- 전체
    def run(self, raw_numbers: list) -> None:
        m = self.m
        done = {r["v"] for r in m._q("SELECT j.value AS v FROM documents d, json_each(d.extra_json,'$.resolved_numbers') j WHERE d.source_id=? AND d.status IN (%s)"
                                     % ",".join(repr(x) for x in L.DONE_STATUSES), (SOURCE_ID,))}
        fails = {f["target"]: f for f in m.failures() if f["stage"] == FAIL_STAGE and f["target"].startswith(FAIL_PREFIX)}
        todo, seen = [], set()
        for raw in raw_numbers:
            self.c["numbers_input"] += 1
            n = normalize_number(raw)
            if n is None:
                key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(raw)))[:60]
                if key not in seen:
                    seen.add(key)
                    self.fail(key, "malformed_number", "not a court case number (no automatic correction)")
                else:
                    self.c["duplicates"] += 1
                continue
            if n in seen:
                self.c["duplicates"] += 1
                continue
            seen.add(n)
            f = fails.get(FAIL_PREFIX + n)
            if n in done and f is None and not self.ctx.refresh:     # 미완료 표시가 남은 번호와 --refresh 는 다시 처리한다
                self.c["skipped_done"] += 1
            elif f and f["error_class"] in SKIP_CLASSES and not self.retry_nf:
                self.c["skipped_known"] += 1
            elif f and f["error_class"] not in NO_RETRY_CLASSES + SKIP_CLASSES and f["retry_count"] + 1 >= MAX_RETRIES and not self.ctx.refresh:     # 명시적 --refresh 는 소진을 우회한다
                self.c["skipped_exhausted"] += 1
            else:
                todo.append(n)
        requested = 0
        for n in todo:
            if self.held_by_lawgo(n):                       # 본문이 이미 장부에 있는 번호는 요청하지 않는다
                self.c["skipped_covered"] += 1
                continue
            if self.limit is not None and requested >= self.limit:
                self.c["skipped_limit"] += 1
                continue
            requested += 1
            # 불완전 목록(list_format)은 HTTP 로는 정상 응답이라 캐시에 남는다. 열린 list_format 실패의 다음 실행은 목록 요청만 캐시를 무시하고 다시 받는다
            res = self.list_exact(n, refresh=(fails.get(FAIL_PREFIX + n) or {}).get("error_class") == "list_format")
            if res[0] == "fail":
                self.fail(n, res[1], "list not usable", remote=True)
            elif res[0] == "incomplete":              # 읽지 못한 항목이 있으면 없다고 확정하지 않는다(재시도 가능한 실패, 원격 실패 횟수에는 세지 않는다)
                self.fail(n, "list_format", f"list incomplete: {res[2]} rows read, some items unparsed or fewer than the total (not recorded as not_found)")
            elif res[0] == "overflow":
                self.fail(n, "page_overflow", f"totalCount exceeds {MAX_LIST_PAGES * LIST_ROWS} rows (nothing registered)")
            else:
                exact = res[1]
                usable = [i for i in exact if i["ptype"] == HTML_PTYPE]
                self.c["skipped_pdf"] += len(exact) - len(usable)
                if not exact:
                    self.fail(n, "not_found", "no list item with this exact case number")
                elif not usable:
                    self.fail(n, "pdf_only", "every exact match is a PDF-type item (not collected)")
                else:
                    multi = len(usable) >= 2
                    self.c["multi_numbers"] += int(multi)
                    if multi:                # 중간에 멈춰도 다음 실행이 남은 판결을 회수하도록 미완료 표시를 먼저 남긴다
                        m.record_failure(FAIL_PREFIX + n, FAIL_STAGE, "multi_in_progress", "not all decisions of this number are done yet", retry_count=0)
                    if all([self.event(n, it, multi) for it in usable]):
                        m.resolve_failures(FAIL_PREFIX + n, FAIL_STAGE)


def run(ctx, args) -> dict:
    m = ctx.manifest
    open_source(m)
    do_reparse = bool(getattr(args, "reparse", False))
    numbers = [] if do_reparse else collect_numbers(args)
    r = _Run(ctx, args)
    try:
        if do_reparse:
            r.reparse()
        else:
            r.run(numbers)
    finally:
        m.touch_source_checked(SOURCE_ID)
        if not ctx.dry_run:
            L._write_terms_file(ctx.inventory_dir / STATE_FILE, {"updated_at": ctx.now().isoformat(timespec="seconds"), "counts": r.c})
    docs = {x["status"]: x["n"] for x in m.count_by("documents", "status", where={"source_id": SOURCE_ID})}
    return {**r.c, "documents_by_status": docs, "state_file": None if ctx.dry_run else str(ctx.inventory_dir / STATE_FILE)}

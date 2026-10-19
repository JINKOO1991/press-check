"""법제처 헌재결정례(DRF target=detc) 본문 파서: 네트워크 없는 순수 함수 모음.

설계 근거: 비공개 설계 문서 D2(필드 매핑·인라인 분할기·주문 분류기)와 반증 검토 2·3·4·11. 어댑터(번호 해석·수신 CLI)는 이 모듈에 없다.
판례 파서(``lawapi.py``)의 ``BodyParseError``·``classify_opinion``·``split_case_numbers`` 를 재사용하고 형식을 맞춘다.

실측 구조(G0 탐침, 표본 2건): XML 루트 ``DetcService``, 줄바꿈 보존. 절 표지 ``【당 사 자】``·``【주 문】``·``【이 유】`` 는 공백이 전각/반각/복수로 달라진다.
``사건번호`` 필드는 대표 번호 1개이고 병합 번호는 ``【당 사 자】`` 구간에만 나열된다. 의견 표제는 줄 머리나 문장 중간(번호 머리 뒤)에 붙고,
표제 끝에 ``(위헌)`` 같은 표기가 붙을 수 있으며 번호 머리가 중첩된다.

하류 계약(상수 STATUS_*): split_detc info.status 는 split(정상 분할) | none(의견 언급 없음) | unsplit(표제 0개인데 언급 있음) |
needs_review(표제 수 불일치). unsplit·needs_review 는 후속 단계가 해당 쪽을 needs_review 로 두고, opinion_kind 가 None 인 구간의 인용은
court_majority 로 쓰지 않는다. party 구간은 no_page=True 라 쪽(chunk 입력)으로 만들지 않고 raw 에만 남는다.
"""
from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from .lawapi import BodyParseError, _squash_ws, classify_opinion, split_case_numbers

STATUS_SPLIT, STATUS_NONE, STATUS_UNSPLIT, STATUS_REVIEW = "split", "none", "unsplit", "needs_review"
SERVICE_TAG = "DetcService"
TEXT_FIELDS = (("판시사항", "headnote_issue"), ("결정요지", "headnote_holding"), ("참조조문", "ref_statutes"),
               ("참조판례", "ref_cases"), ("심판대상조문", "review_target"), ("전문", "full_text"))
EXTRA_FIELDS = (("사건종류명", "case_kind_name"), ("사건종류코드", "case_kind_code"), ("재판부구분코드", "bench_code"))


# ---------------------------------------------------------------- 응답 파싱
def _clean(v: Optional[str]) -> str:
    return (v or "").replace("\r\n", "\n").replace("\r", "\n")


def parse_detc_xml(xml_text, expected_serial: Optional[str] = None) -> dict:
    """lawService detc XML → 정규화 dict. 오류 응답은 BodyParseError(kind=no_match|denied|malformed|serial_mismatch).
    DOCTYPE·ENTITY 선언이 있으면 거부한다. 값이 비었거나 '0' 인 필드는 None 이고 warnings 에 이유를 남긴다."""
    text = xml_text.decode("utf-8", "replace") if isinstance(xml_text, (bytes, bytearray)) else (xml_text or "")
    text = text.lstrip("﻿ \t\r\n")
    if not text.startswith("<"):
        raise BodyParseError("malformed", "unrecognized body")
    low = text.lower()
    if "<!doctype html" in low or "<html" in low[:2048]:
        raise BodyParseError("denied", "html response")
    if "<!doctype" in low or "<!entity" in low:
        raise BodyParseError("malformed", "xml declarations are not accepted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise BodyParseError("malformed", "body is not XML") from None
    if root.tag != SERVICE_TAG:
        joined = "".join(root.itertext())
        raise BodyParseError("no_match" if ("일치하는" in joined and "없" in joined) else "denied", "no DetcService root")
    raw = {c.tag: _clean(c.text) for c in root}
    warnings: list[str] = []
    serial = raw.get("헌재결정례일련번호", "").strip()
    if expected_serial is not None and serial != str(expected_serial):
        raise BodyParseError("serial_mismatch", f"expected {expected_serial}")

    def val(name: str) -> Optional[str]:
        v = raw.get(name, "")
        if v.strip() == "0":
            warnings.append(f"zero_value:{name}")
            return None
        return v if v.strip() else None

    body_case_no = val("사건번호")
    body_case_no = _squash_ws(body_case_no) if body_case_no else None
    out: dict[str, Any] = {
        "serial": serial or None,
        "body_case_no": body_case_no,
        "case_nos": split_case_numbers(body_case_no) if body_case_no else [],
        "title": _squash_ws(val("사건명") or "") or None,
    }
    out["decision_date"] = parse_detc_date(raw.get("종국일자", ""), warnings)
    for src, dst in TEXT_FIELDS:
        out[dst] = val(src)
    out["extra"] = {dst: raw.get(src, "") for src, dst in EXTRA_FIELDS}      # 코드 의미는 해석하지 않고 원값만 보존한다
    if out["full_text"] is None:
        warnings.append("full_text_missing")
    out["warnings"] = warnings
    return out


def parse_detc_date(v: Any, warnings: Optional[list] = None) -> Optional[str]:
    """'20061231'·'2006.12.31'·'2006.1.5.'·'2006-12-31' → ISO. '0'·빈값·비정상은 None 이고 warnings 에 남긴다."""
    s = unicodedata.normalize("NFKC", str(v or "")).strip().rstrip(".")
    w = warnings if warnings is not None else []
    if s in ("", "0"):
        w.append("decision_date_missing")
        return None
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s) or re.fullmatch(r"(\d{4})\s*[.\-]\s*(\d{1,2})\s*[.\-]\s*(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            pass
    w.append("decision_date_unparsed")
    return None


def accept_detc_body(body: bytes, headers: dict, serial: str) -> Optional[str]:
    """lawapi.accept_body 와 같은 용도: 오류 응답·다른 일련번호 응답을 캐시에 쓰지 않으려는 검사. 문제 없으면 None."""
    try:
        parse_detc_xml(body, serial)
    except BodyParseError as e:
        return f"{e.kind}: {e}"[:200]
    return None


# ---------------------------------------------------------------- 사건번호 토큰·머리 쪽
_CASE_TOKEN = re.compile(r"(\d{2,4})\s*헌\s*([가나다라마바사아])\s*(\d+)")
_BR = re.compile(r"【\s*([^】]{1,20})\s*】")


def _bracket_label(m) -> str:
    return re.sub(r"\s+", "", m.group(1))


# 구형 문서(1990년대 결정)는 【】 대신 줄 머리의 ASCII/전각 대괄호 표지를 쓴다. 이름을 당사자·주문·이유로 한정하고 줄 머리에서만 인정한다
# ([별지]·[1] 조문 인용·문장 중간의 [주 문] 문자열은 표지가 아니다). 【】 처리는 그대로다.
_SP = r"[ \t\u3000]*"
_BR_LEGACY = re.compile(r"(?m)^" + _SP + r"(?P<b>[\[［]" + _SP + r"(?P<n>당" + _SP + r"사" + _SP + r"자|주" + _SP + r"문|이" + _SP + r"유)" + _SP + r"[\]］])")


def _section_marks(text: str) -> list[tuple[int, int, str]]:
    """(표지 시작, 표지 끝, 공백 제거한 이름) 오름차순. 【…】 는 어디서나, 구형 […] 는 줄 머리의 당사자·주문·이유만."""
    out = [(m.start(), m.end(), _bracket_label(m)) for m in _BR.finditer(text)]
    out += [(m.start("b"), m.end("b"), re.sub(r"\s+", "", m.group("n"))) for m in _BR_LEGACY.finditer(text)]
    return sorted(out)


# 당사자 구간에서 병합 근거는 현 사건의 번호 나열뿐이다: '사건' 라벨 줄, 번호만 늘어놓은 줄, 번호만 든 괄호 주석('청구인 … (번호)'). 재심대상·종전·관련 결정이나 '선고' 날짜가 붙은 인용은
# 그 표기부터 잘라 낸다(청구인 등 다른 라벨 줄의 번호도 인정하지 않는다).
_CITE_CUT = re.compile(r"(?:종전|이전|기존|앞선|선행|관련|참조|재심대상)(?:의|인)?\s*(?:결정례|결정|사건|판례|심판)|재심대상|재심사건|대상결정|선고|\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}")
_LEAD_MARK = re.compile(r"^[ \t\u3000]*(?:【[^】]*】|[\[［][^\]］]*[\]］])")


def _tokens_of(s: str) -> list[str]:
    return [m.group(1) + "헌" + m.group(2) + m.group(3) for m in _CASE_TOKEN.finditer(s)]


def _only_tokens(s: str) -> bool:
    """사건번호 토큰이 있고 그 밖에는 구분 기호·'병합'·'등'만 있는가."""
    return bool(_CASE_TOKEN.search(s)) and re.sub(r"병합|등|및|[\s,，、·ㆍ()/\-]", "", _CASE_TOKEN.sub("", s)) == ""


def _party_line_tokens(line: str) -> list[str]:
    s = _LEAD_MARK.sub("", line)
    cut = _CITE_CUT.search(s)
    if cut:
        s = s[:cut.start()]
    if re.sub(r"\s+", "", s).startswith("사건") or _only_tokens(s):
        return _tokens_of(s)
    out: list[str] = []
    for g in re.findall(r"\(([^()]*)\)", s):               # '청구인 … (2005헌마165)' 처럼 번호만 든 괄호 주석
        if _only_tokens(g):
            out += _tokens_of(g)
    return out


def extract_merged_numbers(full_text: str) -> list[str]:
    """``【당 사 자】`` 구간(본문 시작~``【주 문】`` 직전)의 사건번호 토큰만. NFKC·공백 제거 후 등장 순서, 중복 제거.
    ``【주 문】`` 표지가 없으면 구간을 정할 수 없으므로 빈 목록이다. 판시사항 등 다른 필드의 번호는 근거가 아니다."""
    text = unicodedata.normalize("NFKC", full_text or "")
    end = next((st for st, _en, label in _section_marks(text) if label == "주문"), None)
    if end is None:
        return []
    out: list[str] = []
    for line in text[:end].split("\n"):
        for tok in _party_line_tokens(line):
            if tok not in out:
                out.append(tok)
    return out


def build_head_page(case_no: Optional[str], decision_date: Optional[str]) -> str:
    """머리 쪽 h000 본문: 사건번호·종국일자(판결문 표기)·기관. 사건번호와 날짜가 모두 없으면 빈 문자열(쪽을 만들지 않는다)."""
    no = _squash_ws(str(case_no or "")).strip()
    iso = decision_date or ""
    day = f"{int(iso[:4])}. {int(iso[5:7])}. {int(iso[8:10])}." if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso) else _squash_ws(iso)
    if not no and not day:
        return ""
    return "\n".join(f"{k}: {v}" for k, v in (("사건번호", no), ("종국일자", day), ("기관", "헌법재판소")) if v)


# ---------------------------------------------------------------- 인라인 분할기
@dataclass
class Section:
    kind: str                       # preamble | party | order | reasons | opinion | heading | holding
    label: str
    opinion_kind: Optional[str]     # majority|dissent|…(classify_opinion) | vote_opinion | main | None
    char_start: int
    char_end: int
    text: str
    vote: Optional[str] = None      # vote_opinion 의 표기(위헌·합헌·헌법불합치·한정위헌 …)
    no_page: bool = False           # True 면 후속 단계가 쪽(chunk 입력)으로 만들지 않는다(구간은 raw 에만 남는다)


_PFX = r"(?:\d{1,2}\.|[가-하]\.|\(\d{1,2}\)|\([가-하]\))"
# 문장 중간 표제의 번호 머리: 한글 바로 뒤의 '다.' 는 문장 끝이지 목록 머리가 아니다(예외: '…의견가.' 처럼 의견 명사 바로 뒤).
_PFX_MID = r"(?:\d{1,2}\.|\(\d{1,2}\)|\([가-하]\)|(?:(?<![가-힣])|(?<=견))[가-하]\.)"
# 표제 본문: '재판관 … 의견' 의 마지막 명사구에서 끊는다. '…의견에 대한 / …의견의 보충' 은 이어지는 표제이므로 거기서 끊지 않는다.
# 끝 뒤에 한글이 바로 이어지면(의견일치·의견이 있다) 진술문이다. 다만 목록 머리('가.')는 한글이어도 마침표가 붙어 있어 허용한다.
_BODY = (r"(?![^\n]{0,60}?[와과]\s*같은)[^\n.]{0,100}?재판관[^\n.]{0,150}?(?:\n[ \t　]*(?:(?![와과]\s*같은)[^\n.]){0,40}?)?의견(?!\s*(?:에\s*대한|의\s*보충))(?![’”」』〉'\"])"
         r"(?:\([가-힣]{1,12}\)(?![가-힣](?!\.))|(?!\([가-힣])(?![가-힣](?!\.)))")
_LINE_START = r"(?:(?<=\n)|^)[ \t　]*"
_NOT_REF = r"(?<!아래 )(?<!뒤의 )(?<!다음 )(?<!위의 )(?<!앞의 )(?<!아래)(?<!뒤의)(?<!다음)(?<!위의)(?<!앞의)"
# strict: 줄 머리(번호 머리 선택) 또는 문장 중간(번호 머리 필수, 따옴표·'아래 N.' 인용 뒤 제외)
HEAD_RE = re.compile(r"(?:" + _LINE_START + r"(?P<a>(?:" + _PFX + r"\s*)*" + _BODY + r")|"
                     r"(?<![‘’'\"“”「」『』〈〉])" + _NOT_REF + r"(?P<b>(?:" + _PFX_MID + r"\s*)+" + _BODY + r"))")
# loose: 줄 머리 또는 문장 시작(번호 머리 선택). strict 가 놓치는 '번호 없는 문장 시작 표제' 를 센다(누락 탐지용).
LOOSE_RE = re.compile(r"(?:" + _LINE_START + r"|(?<=[.다])[ \t　]+)(?P<h>(?:" + _PFX + r"\s*)*" + _BODY + r")")
_MENTION_RE = re.compile(r"재판관[^\n]{0,150}(?:반대|별개|보충|법정|위헌|합헌|헌법불합치|각하|인용|기각|한정위헌|한정합헌)의견")
_CONCL_NO_RE = re.compile(r"(?:다음|아래|뒤의|뒤|위)\s*[‘']?\s*(\d{1,2})\s*\.")      # '아래 6.과 같은 …의견' 의 번호
_VOTE_WORDS = ("한정위헌", "한정합헌", "단순위헌", "헌법불합치", "위헌", "합헌", "각하", "인용", "기각")
_VOTE_TAIL = re.compile(r"(" + "|".join(_VOTE_WORDS) + r")의견$")
_PFX_STRIP = re.compile(r"^(?:" + _PFX + r")+")


_VOTE_ANY = re.compile("|".join(_VOTE_WORDS))
_VOTE_SIDE = {"한정위헌": "INV", "단순위헌": "INV", "위헌": "INV", "헌법불합치": "INV", "인용": "INV",
              "합헌": "UPH", "한정합헌": "UPH", "기각": "UPH", "각하": "DISM"}


def classify_detc_opinion(label: str) -> tuple[Optional[str], Optional[str]]:
    """표제 → (opinion_kind, vote). '…의견' 으로 끝나는 마지막 명사구로 판정한다('…반대의견에 대한 재판관 병의 보충의견' 은 보충).
    위헌·합헌·헌법불합치·각하·인용·기각의견은 vote_opinion(법정의견 여부 불명)이며 vote 에 표기를 남긴다."""
    t = _PFX_STRIP.sub("", re.sub(r"\s+", "", label))
    paren = re.search(r"\(([가-힣]{1,12})\)$", t)
    core = t[:paren.start()] if paren else t
    last = re.split(r"에대한", core)[-1]
    if "보충의견" in last:
        return classify_opinion(core.replace("법정의견", "다수의견")), None
    m = _VOTE_TAIL.search(last)
    if m:
        tail = re.split(r"이?아닌|이?아니라", last)[-1]          # '이 아닌' 앞의 입장어는 그 의견의 입장이 아니다
        ws = list(_VOTE_ANY.finditer(tail))
        sides = {_VOTE_SIDE[ws[-1].group()]} if ws else set()
        for prev, cur in zip(ws[-2::-1], ws[::-1]):          # '의견' 바로 앞 입장어에서 거슬러, 사이가 순수 접속어일 때만 같은 의견의 병렬 입장으로 묶는다
            if not re.fullmatch(r"(?:및|와|과|·|ㆍ|,|，|또는)+", tail[prev.end():cur.start()]):
                break
            sides.add(_VOTE_SIDE[prev.group()])
        return "vote_opinion", (m.group(1) if len(sides) <= 1 else None)      # 병렬로 이은 입장어의 편이 갈리면 입장 불명
    if last.endswith("법정의견"):
        return "majority", None
    kind = classify_opinion(last)
    if kind:
        return kind, (paren.group(1) if paren else None)
    if paren and last.endswith("의견"):
        return "vote_opinion", paren.group(1)
    return None, None


_WRAP_RE = re.compile(r"재판관[^.‘’'\"“”「」『』〈〉]{0,80}?\n[^.‘’'\"“”「」『』〈〉]{0,80}?의견(?![가-힣](?!\.))")
_Q_OPEN, _Q_CLOSE = "‘“「『〈", "’”」』〉"


def _quoted(text: str, a: int, b: int) -> bool:
    """표제 후보 [a,b) 가 인용 속이면 True: 같은 줄 앞부분에서 열린 따옴표가 닫히지 않았거나, 후보 안에서 따옴표 쌍이 맞지 않는다."""
    pre, span = text[text.rfind("\n", 0, a) + 1:a], text[a:b]
    bal = lambda s: sum(s.count(c) for c in _Q_OPEN) - sum(s.count(c) for c in _Q_CLOSE)
    odd = lambda s: s.count('"') % 2 or s.count("'") % 2
    return bal(pre) > 0 or bal(span) != 0 or bool(odd(pre) or odd(span))


def _scan(text: str, start: int, end: int) -> dict:
    """[start,end) 에서 strict 표제, loose 표제 수, 결론 문장 번호, 언급 여부."""
    heads = []
    for m in HEAD_RE.finditer(text, start, end):
        g = "a" if m.group("a") is not None else "b"
        if not _quoted(text, m.start(g), m.end(g)):
            heads.append((m.start(g), m.end(g), m.group(g)))
    loose = sum(1 for m in LOOSE_RE.finditer(text, start, end) if not _quoted(text, m.start("h"), m.end("h")))
    # 줄바꿈을 건너뛰어 표제로 잡지 못한 '재판관 … 의견' 언급도 느슨 검출에 넣는다(표제 수와 어긋나 needs_review/unsplit 으로 수렴)
    loose += sum(1 for w in _WRAP_RE.finditer(text, start, end) if not any(w.start() < b and w.end() > a for a, b, _ in heads))
    seg = text[start:end]
    return {"heads": heads, "loose": loose, "nums": sorted({int(n) for n in _CONCL_NO_RE.findall(seg)}),
            "mention": bool(_MENTION_RE.search(seg))}


def _status(scan: dict) -> str:
    n = len(scan["heads"])
    if n == 0:
        return STATUS_UNSPLIT if (scan["mention"] or scan["loose"]) else STATUS_NONE
    if scan["loose"] != n or len(scan["nums"]) > n:
        return STATUS_REVIEW
    return STATUS_SPLIT


def split_detc(text: str) -> tuple[list[Section], dict]:
    """전문을 offset 기반으로 나눈다(구간을 이어 붙이면 원문과 같다). 반환 (sections, info).

    kind: preamble(첫 표지 앞) | party(``【당 사 자】``, no_page=True) | order | reasons | opinion(의견 표제부터) | heading(주문 뒤 그 밖의 【…】).
    opinion 구간은 opinion_kind·vote 를 갖고, reasons 는 의견 표제가 있으면 'majority' 아니면 'main'. 표제는 【이 유】 구간 안에서만 찾는다.
    info.status: none | split | needs_review | unsplit (모듈 docstring 의 하류 계약)."""
    marks: list[tuple[int, str, str, Optional[str], Optional[str], bool]] = []     # offset, kind, label, opinion_kind, vote, no_page
    seen_core = False
    reasons_end = None
    for st, en, label in _section_marks(text):
        if label == "당사자":
            marks.append((st, "party", label, None, None, True))
        elif label == "주문":
            seen_core = True
            marks.append((st, "order", label, None, None, False))
        elif label == "이유":
            seen_core = True
            marks.append((st, "reasons", label, None, None, False))
            if reasons_end is None:
                reasons_end = en
        elif seen_core:
            marks.append((st, "heading", label, None, None, False))
    scan = {"heads": [], "loose": 0, "nums": [], "mention": False}
    if reasons_end is not None:
        scan = _scan(text, reasons_end, len(text))
        for st, _en, h in scan["heads"]:
            kind, vote = classify_detc_opinion(h)
            marks.append((st, "opinion", re.sub(r"\s+", " ", h).strip()[:160], kind, vote, False))
    status = _status(scan) if reasons_end is not None else STATUS_NONE
    marks.sort(key=lambda x: x[0])
    sections = _build(text, marks, "preamble", bool(scan["heads"]))
    info = {"status": status, "marker_count": len(scan["heads"]), "loose_count": scan["loose"], "conclusion_numbers": scan["nums"],
            "markers": [{"kind": s.opinion_kind, "vote": s.vote, "label": s.label} for s in sections if s.kind == "opinion"]}
    return sections, info


def _build(text: str, marks: list, first_kind: str, has_op: bool) -> list[Section]:
    bounds = list(marks)
    if not bounds or bounds[0][0] > 0:
        bounds.insert(0, (0, first_kind, "", None, None, False))
    out: list[Section] = []
    cur_op: Optional[str] = None
    for n, (st, kind, label, op, vote, no_page) in enumerate(bounds):
        en = bounds[n + 1][0] if n + 1 < len(bounds) else len(text)
        if kind == "opinion":
            cur_op = op
        elif kind in ("reasons", "holding"):
            op = cur_op = ("majority" if has_op else "main")
        elif kind == "heading":
            op = cur_op
        out.append(Section(kind, label, op, st, en, text[st:en], vote, no_page))
    return out


def split_head_opinions(headnote_holding: Optional[str]) -> tuple[list[Section], dict]:
    """결정요지에 섞인 반대·보충·별개의견 요지를 같은 표제 규칙으로 분리한다. info.headnote_split: split | unsplit | needs_review | none.
    분할하지 못했는데 의견 언급이 있으면 unsplit(후속 단계는 이 필드의 인용을 court_majority 로 쓰지 않는다)."""
    text = headnote_holding or ""
    scan = _scan(text, 0, len(text))
    marks = []
    for st, _en, h in scan["heads"]:
        kind, vote = classify_detc_opinion(h)
        marks.append((st, "opinion", re.sub(r"\s+", " ", h).strip()[:160], kind, vote, False))
    sections = _build(text, marks, "holding", bool(marks)) if text else []
    return sections, {"headnote_split": _status(scan), "marker_count": len(scan["heads"])}


# ---------------------------------------------------------------- 주문 분류기
_ITEM_NO = re.compile(r"(?:(?:\A|(?<=\n))[ \t\u3000]*|(?<=[.다])[ \t\u3000]+)(\d{1,2})\.\s")
_MASK_DATE = re.compile(r"\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?")
_MASK_PAREN = re.compile(r"\([^()]*\)")


def _mask(text: str) -> str:
    """항목 경계 후보에서 법령 날짜와 괄호 안을 뺀다(같은 길이로 가려 offset 을 보존). 판정은 원문 기준이다."""
    s = _MASK_DATE.sub(lambda m: "_" * len(m.group()), text)
    prev = None
    while prev != s:
        prev, s = s, _MASK_PAREN.sub(lambda m: "_" * len(m.group()), s)
    return s
_NOT_UNCONST = ("위반된다고할수없다", "위반된다고선고할수없다", "선고할수없다")     # 위헌보다 먼저(반증 검토 4)


def _classify_item(item: str) -> str:
    t = re.sub(r"\s+", "", item)
    if any(k in t for k in _NOT_UNCONST):
        return "위헌불선언"
    if "합치되지아니한다" in t or "헌법불합치" in t:
        return "헌법불합치"
    if "해석하는한" in t:
        if "위반되지아니한다" in t:
            return "한정합헌"
        if "위반된다" in t:
            return "한정위헌"
    if "위반되지아니한다" in t:
        return "합헌"
    if "위반된다" in t:
        return "위헌"
    if "취소한다" in t or "위헌임을확인한다" in t or "인용한다" in t:
        return "인용"
    if "각하" in t:
        return "각하"
    if "기각" in t:
        return "기각"
    if "심판절차" in t and "종료" in t:
        return "심판절차종료"
    return "unknown"


def classify_orders(order_text: str) -> list[str]:
    """주문 → 항목별 처분 라벨 목록(항목 번호 '1. 2. …' 가 순서대로 있으면 번호 단위, 없으면 문장 단위).
    라벨: 위헌불선언 | 헌법불합치 | 한정위헌 | 한정합헌 | 합헌 | 위헌 | 인용 | 각하 | 기각 | 심판절차종료 | unknown. 빈 입력은 []."""
    text = (order_text or "").strip()
    if not text:
        return []
    text = re.sub(r"^(?:【[^】]*】|[\[［][^\]］]{1,10}[\]］])\s*", "", text)
    starts, want = [], 1
    for m in _ITEM_NO.finditer(_mask(text)):
        if int(m.group(1)) == want:
            starts.append((m.start(), m.end()))
            want += 1
    if starts:
        bounds = [s for s, _ in starts] + [len(text)]
        items = [text[starts[i][1]:bounds[i + 1]] for i in range(len(starts))]
    else:
        items = [s for s in re.split(r"(?<=다\.)\s+", text) if s.strip()] or [text]
    return [_classify_item(i) for i in items]

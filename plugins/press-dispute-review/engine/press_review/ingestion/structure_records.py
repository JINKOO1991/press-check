"""사건 구조화 응답의 결정적 검증·조립(순수 함수, 네트워크·장부 없음). 호출 순서와 장부 기록은 ``structure.py`` 가 한다.

모델 응답(draft)을 믿지 않고 코드가 case record(``schemas/case-record.schema.json``)를 새로 만든다.

- 인용: 모델은 ``{page_id, quote}`` 만 주고, 코드가 페이지 텍스트에서 부분문자열로 찾아 ``start``/``end``(원문 code point 기준)를 계산한다.
  (G-D13) 대조는 양쪽에서 공백 문자를 모두 지운 문자열로 한다. 공백 말고는 아무것도 정규화하지 않는다(기호·숫자·한자 그대로). 저장하는 offset 은 원문 기준이고
  첫 비공백 글자부터 마지막 비공백 글자까지이며, 저장하는 quote 는 그 원문 구간 그대로라서 ``contracts.validate_case_record`` 의 공백 축약 비교를 항상 통과한다.
  찾지 못하면 그 항목을 버리고 ``quote_check.dropped_fields`` 에 남긴다. 한 쪽 안에서만 찾는다(쪽 경계를 넘는 인용은 버린다).
- (G-D15) ``TABLE_CELL_SOURCES`` 소스에서는 인용이 쪽 텍스트의 표 칸(HTML ``<td>``·``<th>``, 마크다운 ``|`` 행) 하나와 정확히 일치하면 최소 길이 제한을 적용하지 않는다.
  칸 경계는 표 표지가 있는 텍스트에서만 알 수 있다(평문 pdftotext 쪽은 칸을 모르므로 예외 없음). 같은 칸 값이 여러 행에 있으면 같은 사건의 다른 인용에 가장 가까운 칸을 쓴다.
- (G-D17) ``correction_pair_confidence`` 는 원보도 인용 건수로 결정적으로 유도한다(``derive_pair_confidence``). 모델 값은 전후 전문이 모두 확인된 경우에만 쓴다.
- 원문에 없는 사건번호·URL·날짜는 버린다(scalar 는 null, 목록 항목은 항목째).
- authority_type·institution 은 소스 프로필의 허용 값만 받는다. speaker 도 필드별 허용 값만 받는다(기사 필드는 article 로 고정).
- 라벨 위반(조정합의를 판결로, 각하를 본안 판단으로 등)은 조용히 고치지 않는다: ``contracts.validate_case_record`` 오류와 라벨 가드 오류가
  있으면 사건을 quarantined 로 둔다. 인용을 버린 결과로 생긴 불일치(결론 인용 탈락 -> outcome null 이면 outcome_type unknown)만 보정한다.
- 실패율(버린 항목 / 모델이 낸 항목)이 임계를 넘거나 근거 항목이 하나도 없으면 quarantined.
- (G-D2) 법원 판결(authority_type=judgment)의 outcome_type 은 judgment 만이다. 어긋나면 고치지 않고 사건을 격리한다. 처분 원문(상고기각 등)은 decision_type 에 적는다.
- (G-D3) 판례의 참조판례(ref_cases) 구간에서 인용한 항목이 이력(appeal_or_later_history)에 있으면 사건 전체가 아니라 그 항목만 지우고 quote_check.dropped_fields 에 남긴다.
- (G-D5) 표 프로필(iendc_table)은 한 창에서 사건 2건 이상이 같은 구간을 사실 인용으로 쓰면(병합 칸) 그 인용 항목을 버린다.
- (V-FID-01) 같은 문구가 쪽에 여러 번 있으면(표 행마다 반복되는 결과 칸) 그 사건의 다른 인용에 가장 가까운 것을 쓴다. 한 곳에만 있는 인용이 기준점(anchor)이 되고,
  기준점이 없으면 첫 일치를 쓰되 ``ambiguous_quote_first_match`` 로 남긴다.
- (V-FID-02) 한 창에서 사건 2건 이상이 같은 구간을 결론으로 인용했고 그것이 그 사건의 유일한 근거이면(표 머리글 등) 결론을 버려 근거 없음으로 격리한다(``drop_shared_outcome_quotes``).
- (V-FID-03) 줄로 갈라진 사건번호('제2021' / '-813호')는 머리와 꼬리가 3줄 안에 순서대로 있으면 원문에 있는 것으로 본다.
- (V-FID-04) 조문 인용의 ``제N조(의M)`` 는 인용 구간과 같은 쪽 원문에 그대로 있어야 한다(깨진 텍스트층에서 모델이 고쳐 읽은 번호를 막는다).
- (V-FID-05) 표가 아닌 소스에서 인용이 단 사이 간격(공백 4칸 이상)을 두 줄 이상에서 같은 열로 가로지르면(좌우 단을 줄마다 이어 붙인 문장) 버린다(``column_join``).
- (V-FID-07) speaker article 필드의 '위 기사는 …' 항목은 기관의 서술이므로 factual_background 로 옮기고, 당사자 주장과 같은 구간인 기사 항목은 버린다.
- (G-D27) 대조할 때 ``<br>``·``<br/>``·``<br />`` 는 공백과 같이 지운다(인용·쪽 양쪽). 저장 텍스트와 offset 은 원문 그대로이고 구간은 <br> 로 시작하거나 끝나지 않는다.
- (G-D26) 표 소스(``TABLE_CELL_SOURCES``)의 사건은 행 고유 칸(자기 사건번호, 또는 다른 사건이 인용하지 않고 쪽에 한 곳뿐인 머리줄이 아닌 칸)을 인용한 근거가 1개 이상 있어야 한다. 없으면 격리한다(``require_row_unique_quote``, 창 병합 뒤).
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from .. import contracts

COURT_SPEAKERS = frozenset({"court_majority", "court_concurring", "court_dissenting"})
COURT_INSTITUTIONS = ("supreme_court", "high_court", "district_court", "constitutional_court", "other_court")
MIN_QUOTE_CHARS = 4
# G-D15: 표 칸 인용을 허용하는 소스(칸 경계는 쪽 텍스트의 표 표지로 판정한다). 평문으로 추출된 쪽에서는 효과가 없다.
TABLE_CELL_SOURCES = frozenset({"iendc_review_results", "pac_board_correction_status"})
MAX_QUOTE_CHARS = 2000
DEFAULT_QUARANTINE_RATE = 0.25
ITEM_FIELDS = ("factual_background", "article_claims", "disputed_spans", "claims_or_requested_remedy", "legal_reasoning")
LIST_MERGE_FIELDS = ITEM_FIELDS + ("legal_provisions", "law_versions", "appeal_or_later_history")


# ---------------------------------------------------------------- 프로필
@dataclass(frozen=True)
class Profile:
    name: str
    prompt: str                      # prompts/ 아래 파일 이름
    institutions: tuple
    authority_types: tuple
    guard_shared_quotes: bool = False      # G-D5: 같은 구간을 사건 2건 이상이 사실 인용으로 쓰면 버린다(병합 칸)
    links_ledger: bool = False             # G-D6: 소개된 판결의 사건번호를 법제처 장부와 결정적으로 연결한다(ingestion/links.py)


PROFILES: dict[str, Profile] = {
    "pac_adjustment": Profile("pac_adjustment", "pac_adjustment.md", ("pac",), ("pac_settlement", "pac_decision")),
    "pac_election": Profile("pac_election", "pac_election.md", ("pac",), ("pac_election_review",)),
    "pac_correction": Profile("pac_correction", "pac_correction.md", ("pac",), ("pac_decision",)),
    # 2026-10-16: 시정권고 현황 전용(행 고유 인용 요구, 통계 요약은 사건 아님). pac_correction 사례집의 프롬프트·해시는 바뀌지 않는다.
    "pac_board_status": Profile("pac_board_status", "pac_board_status.md", ("pac",), ("pac_decision",)),
    "iendc_table": Profile("iendc_table", "iendc_table.md", ("iendc",), ("iendc_review",), guard_shared_quotes=True),
    "pac_judgment_cases": Profile("pac_judgment_cases", "pac_judgment_cases.md", ("pac", "other_body"), ("research",), links_ledger=True),
    "court_judgment": Profile("court_judgment", "court_judgment.md", COURT_INSTITUTIONS, ("judgment",)),
}
# source_id -> 프로필. 여기에 없는 소스(연구 자료, 심의기준 고정 페이지 등)는 사건 구조화를 하지 않는다.
SOURCE_PROFILE: dict[str, str] = {
    "pac_adjustment_casebooks": "pac_adjustment",
    "pac_election_whitepapers": "pac_election",
    "pac_correction_casebooks": "pac_correction",
    "pac_board_correction_status": "pac_board_status",
    "iendc_review_results": "iendc_table",
    "lawgo_prec_supreme": "court_judgment",
    "pac_research_judgment_analysis": "pac_judgment_cases",
}
# 사건 구조화를 하지 않고 기준·규정 chunk 만 만드는 소스(G-D6): source_id -> chunk 의 authority_type.
# G-D21: 심의기준·편람은 법령이 아니므로 guideline, 위원회 규칙은 statute 로 둔다(두 스키마의 authority_type 열거에 guideline 이 있다).
CHUNK_ONLY_SOURCES: dict[str, str] = {
    "pac_standards_correction_page": "guideline",
    "pac_standards_election_page": "guideline",
    "pac_board_committee_rules": "statute",
    # P2_DESIGN §8 순위 B: PAC 연구·학술·기타 자료는 사건 구조화 없이 research chunk 만 만든다(외부 반출 없음).
    "pac_research_quarterly": "research",
    "pac_research_seminars": "research",
    "pac_research_journal": "research",
    "pac_research_foreign_law": "research",
    "pac_research_issue_reports": "research",
    "pac_research_commissioned": "research",
    "pac_other_annual_reports": "research",
    "pac_other_mediation_persuasion": "research",
    "pac_other_misc_reports": "research",
    "pac_other_promo_publications": "research",
}

# G-D32: 사례집의 '전체 목록'·'색인' 장은 사건 행만 나열해 인용 근거가 없는 사건을 대량으로 만든다(전량 1차 실행 격리의 대부분).
# 이 장은 DeepSeek 구조화 대신 쪽 원문 chunk 로만 색인한다. source_id -> chunk 의 authority_type.
LIST_LABEL_RE = re.compile(r"전체\s*목록|색인")
LIST_CHUNK_AUTHORITY: dict[str, str] = {
    "pac_correction_casebooks": "pac_decision",
    "pac_adjustment_casebooks": "pac_decision",
}


def allowed_speakers(field_name: str, authority: str) -> frozenset:
    court = authority == "judgment"
    if field_name == "factual_background":
        return frozenset({"claimant", "respondent", "institution", "record"}) | (COURT_SPEAKERS if court else frozenset())
    if field_name == "claims_or_requested_remedy":
        return frozenset({"claimant", "respondent"})
    if field_name == "legal_reasoning":
        return frozenset({"institution"}) | (COURT_SPEAKERS if court else frozenset())
    if field_name == "outcome":
        return frozenset({"institution"}) | (frozenset({"court_majority"}) if court else frozenset())
    return frozenset({"article"})


# ---------------------------------------------------------------- 텍스트 보조
_WS = re.compile(r"\s+")
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})


# G-D27: kordoc 텍스트는 줄바꿈을 ``<br>`` 로 남긴다. 대조할 때는 공백과 같이 지운다(``<br>``·``<br/>``·``<br />``, 대소문자 무시). 속성이 붙은 태그나 다른 태그는 그대로다.
BR_RE = re.compile(r"<br\s*/?>", re.I)


def collapse(s: str) -> str:
    return _WS.sub(" ", s).strip()


def compact(s: str) -> str:
    """식별자 대조용: 공백과 ``<br>`` 를 모두 지우고 대시를 통일한다."""
    return _WS.sub("", BR_RE.sub("", s)).translate(_DASHES)


def strip_ws(s: str) -> str:
    """G-D13·G-D27: 공백 문자(str.isspace)와 ``<br>`` 를 모두 지운 문자열. 다른 정규화는 하지 않는다."""
    return "".join(ch for ch in BR_RE.sub("", s) if not ch.isspace())


_HTML_CELL = re.compile(r"<(td|th)\b[^>]*>(.*?)</\1\s*>", re.I | re.S)
_FMT_WRAP = re.compile(r"<(u|b|i|em|strong|span|sup|sub|font)\b[^>]*>(.*)</\1\s*>\Z", re.I | re.S)
_GUTTER = re.compile(r"(?<=\S)[ \t]{4,}(?=\S)")
COLUMN_PAGE_SUPPORT = 6          # V-FID-05: 같은 열에 단 간격이 있는 줄이 쪽에 이만큼 이상이어야 2단 조판으로 본다(실측: 2단 쪽 최소 7줄, 가림 공백 쪽 최대 4줄)
_MD_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


_BR_TAIL = re.compile(r"<br\s*/?>\Z", re.I)
_BR_HEAD = re.compile(r"<br\s*/?>", re.I)


def _trim_span(text: str, s: int, e: int) -> Optional[tuple]:
    """[s, e) 의 앞뒤 공백과 ``<br>`` 를 줄인다(G-D27: 칸 값·인용 구간이 줄바꿈 표지로 시작하거나 끝나지 않게)."""
    while s < e:
        if text[s].isspace():
            s += 1
        elif (m := _BR_HEAD.match(text, s, e)):
            s = m.end()
        else:
            break
    while e > s:
        if text[e - 1].isspace():
            e -= 1
        elif (m := _BR_TAIL.search(text, s, e)):
            e = m.start()
        else:
            break
    return (s, e) if e > s else None


def table_cell_spans(text: str) -> list:
    """쪽 텍스트의 표 칸 안쪽 구간 [(start, end)]: HTML ``<td>``/``<th>`` 와 마크다운 ``|`` 행의 칸. 공백·글자 서식 태그(<u> 등)는 칸 값에서 뺀다. 표 표지가 없으면 빈 목록."""
    out: list = []
    for m in _HTML_CELL.finditer(text):
        sp = _trim_span(text, m.start(2), m.end(2))
        while sp:
            w = _FMT_WRAP.fullmatch(text[sp[0]:sp[1]])
            if not w:
                break
            sp = _trim_span(text, sp[0] + w.start(2), sp[0] + w.end(2))
        if sp:
            out.append(sp)
    pos = 0
    for line in text.split("\n"):
        start = pos
        pos += len(line) + 1
        body = line.strip()
        if not body.startswith("|") or body.count("|") < 2 or _MD_SEP.match(line):
            continue
        cuts = [i for i in range(len(line)) if line[i] == "|" and (i == 0 or line[i - 1] != "\\")]
        for a, b in zip(cuts, cuts[1:]):
            sp = _trim_span(text, start + a + 1, start + b)
            if sp:
                out.append(sp)
    return sorted(set(out))


class PageText:
    """페이지 텍스트와 공백 제거본(제거본 위치 -> 원문 위치 표). ``find`` 는 원문 기준 [start, end) 를 돌려준다.
    ``coll`` 은 공백 축약본이다(길이 규칙·표시용; 대조에는 쓰지 않는다)."""
    __slots__ = ("text", "coll", "nows", "_nmap", "_cells", "_gaps")

    def __init__(self, text: str):
        self.text = text
        self.coll = collapse(text)
        nw: list[str] = []
        nm: list[int] = []
        skip = {i for m in BR_RE.finditer(text) for i in range(m.start(), m.end())}        # G-D27: <br> 는 공백과 같이 대조에서 뺀다. 위치 표는 원문 기준이다
        for i, ch in enumerate(text):
            if not ch.isspace() and i not in skip:
                nw.append(ch)
                nm.append(i)
        self.nows = "".join(nw)
        self._nmap = nm
        self._cells: Optional[list] = None
        self._gaps: Optional[list] = None

    def find(self, q: str) -> Optional[tuple[int, int]]:
        """q: 공백을 모두 지운 문자열(G-D13). 첫 일치의 [첫 비공백 글자, 마지막 비공백 글자 + 1) 을 원문 위치로 돌려준다."""
        key = strip_ws(q)
        if not key:
            return None
        i = self.nows.find(key)
        if i < 0:
            return None
        return self._nmap[i], self._nmap[i + len(key) - 1] + 1

    def find_all(self, q: str, limit: int = 200) -> list:
        """V-FID-01: q 의 모든 일치(겹침 포함) [(start, end)], 원문 위치순. 최대 ``limit`` 개."""
        key = strip_ws(q)
        out: list = []
        if not key:
            return out
        i = self.nows.find(key)
        while i >= 0 and len(out) < limit:
            out.append((self._nmap[i], self._nmap[i + len(key) - 1] + 1))
            i = self.nows.find(key, i + 1)
        return out

    def _gap_ends(self) -> list:
        """쪽의 줄마다 단 간격(비공백 글자 사이 공백 4칸 이상)의 끝 열 목록."""
        if self._gaps is None:
            self._gaps = [[m.end() for m in _GUTTER.finditer(ln)] for ln in self.text.split("\n")]
        return self._gaps

    def column_join(self, s: int, e: int) -> bool:
        """V-FID-05: 원문 구간 [s, e) 가 2단 조판 쪽에서 좌우 단을 줄마다 이어 붙인 것인가.
        (1) 구간 안 서로 다른 두 줄에서 단 간격의 끝 열이 2칸 안으로 같고, (2) 그 열에 간격이 있는 줄이 쪽 전체에서 ``COLUMN_PAGE_SUPPORT`` 줄 이상이다.
        (2)는 가림 공백(익명 처리한 이름 자리, 쪽에 몇 줄 안 되고 열이 흩어져 있다)과 단 간격을 가른다. 한 줄짜리 표 행은 (1)에 해당하지 않는다."""
        t = self.text
        gaps = self._gap_ends()
        ends: list = []
        pos = t.rfind("\n", 0, s) + 1
        row = t.count("\n", 0, pos)
        while pos < e:
            nl = t.find("\n", pos)
            nl = len(t) if nl < 0 else nl
            a, b = max(pos, s), min(nl, e)
            for m in _GUTTER.finditer(t, a, b):
                ends.append((row, m.end() - pos))
            pos = nl + 1
            row += 1
        for i, (r1, c1) in enumerate(ends):
            for (r2, c2) in ends[i + 1:]:
                if r1 != r2 and abs(c1 - c2) <= 2 and sum(1 for g in gaps if any(abs(x - c1) <= 2 for x in g)) >= COLUMN_PAGE_SUPPORT:
                    return True
        return False

    def cells(self) -> list:
        if self._cells is None:
            self._cells = table_cell_spans(self.text)
        return self._cells

    def find_cell(self, q: str, near: Optional[list] = None) -> Optional[tuple[int, int]]:
        """q 가 표 칸 하나(공백 제외)와 정확히 같은 칸의 [start, end). 여러 칸이면 near(이 쪽의 다른 인용 시작 위치 목록)에 가장 가까운 칸, 없으면 첫 칸."""
        key = strip_ws(q)
        if not key:
            return None
        hits = [(s, e) for s, e in self.cells() if strip_ws(self.text[s:e]) == key]
        if not hits:
            return None
        if near:
            return min(hits, key=lambda se: min(abs(se[0] - p) for p in near))
        return hits[0]


URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'\)\]]+", re.I)
_CASE_TYPES = ("가합|가단|가소|고합|고단|고정|고약|구합|구단|카합|카단|카확|헌가|헌나|헌다|헌라|헌마|헌바|헌사|헌아|재다|재두|재누|"
               "나|다|라|마|바|사|아|자|차|카|타|파|하|도|노|누|두|구|으|머|므|느|즈|스|그|무|오|초|재|허|수|고|합|단")
CASE_NO_RE = re.compile(rf"(?<![0-9A-Za-z])(?:(?:19|20)?\d{{2}}\s*(?:{_CASE_TYPES})\s*\d{{1,7}}(?!\d)"
                        r"|(?:19|20)\d{2}\s*[-‐-―]\s*[가-힣]{1,6}\s*[-‐-―]\s*\d{1,6})")


def foreign_identifiers(s: str, source_compact: str) -> list[str]:
    """s 안의 사건번호·URL 중 원문(공백 제거본)에 없는 것."""
    bad = []
    for m in URL_RE.finditer(s):
        if compact(m.group(0)).rstrip(".,;") not in source_compact:
            bad.append(m.group(0))
    for m in CASE_NO_RE.finditer(s):
        if compact(m.group(0)) not in source_compact:
            bad.append(m.group(0))
    return bad


SPLIT_CASE_NO_LINES = 3          # V-FID-03: 줄로 갈라진 사건번호의 머리와 꼬리 사이 최대 줄 수


def case_number_in_source(cn: str, source_text: str, source_compact: str) -> bool:
    """사건번호(표기 그대로)가 원문에 있는가. 공백을 지운 연속 문자열로 있거나, -layout 표처럼 번호가 줄로 갈라진 경우
    대시 앞이나 뒤에서 자른 머리('제2021')와 꼬리('-813호')가 그 순서로, 머리가 있는 줄 뒤 ``SPLIT_CASE_NO_LINES`` 줄 안에 있으면 있는 것으로 본다(V-FID-03).
    머리·꼬리는 각각 3글자 이상이고 숫자 경계를 지킨다(꼬리 '813호' 가 '1813호' 안에서 일치하지 않게)."""
    c = compact(cn)
    if not c:
        return False
    if c in source_compact:
        return True
    lines = [compact(l) for l in source_text.split("\n")]
    for m in re.finditer("-", c):
        for k in (m.start(), m.start() + 1):
            head, tail = c[:k], c[k:]
            if len(head) < 3 or len(tail) < 3:
                continue
            hp = re.compile(re.escape(head) + (r"(?!\d)" if head[-1].isdigit() else ""))
            tp = re.compile((r"(?<!\d)" if tail[0].isdigit() else "") + re.escape(tail))
            for i, ln in enumerate(lines):
                if hp.search(ln) and any(tp.search(x) for x in lines[i + 1:i + 1 + SPLIT_CASE_NO_LINES]):
                    return True
    return False


def _iso_date(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    try:
        return date.fromisoformat(v.strip()).isoformat() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()) else None
    except ValueError:
        return None


def date_in_source(iso: str, source_text: str) -> bool:
    """YYYY-MM-DD 의 연·월·일이 원문에 '2001. 5. 4.', '2001년 5월 4일', '2001-05-04' 같은 형태로 나오는가."""
    source_text = BR_RE.sub(" ", source_text)             # G-D27
    y, m, d = iso.split("-")
    mi, di = str(int(m)), str(int(d))
    pat = rf"{y}\s*[.\-/년]\s*0?{mi}\s*[.\-/월]\s*0?{di}(?!\d)"
    return re.search(pat, source_text) is not None


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _str(v: Any) -> Optional[str]:
    return v.strip() if isinstance(v, str) and v.strip() else None


# ---------------------------------------------------------------- 컨텍스트·결과
@dataclass
class WindowCtx:
    profile: Profile
    doc_id: str
    att_id: Optional[str]
    target_key: str
    unit: str
    pages: dict                      # page_id -> PageText (이 창에 있는 쪽)
    page_meta: dict                  # page_id -> pages 행(dict) (pdf_page_no 등)
    structurer: dict                 # version, model, input_sha256
    quarantine_rate: float = DEFAULT_QUARANTINE_RATE
    source_text: str = ""            # 창 쪽 텍스트 전체(날짜 대조용)
    source_compact: str = ""         # 공백 제거본(식별자 대조용)
    ledger: dict = field(default_factory=dict)   # 장부(법제처 응답)에서 온 이 문서 자신의 값: case_number(표기 그대로), case_number_tokens, decision_date
    source_id: str = ""                          # G-D15: TABLE_CELL_SOURCES 판정용

    def __post_init__(self) -> None:
        if not self.source_text:
            self.source_text = "\n".join(p.text for p in self.pages.values())
        if not self.source_compact:
            self.source_compact = compact(self.source_text)


@dataclass
class CaseOut:
    record: dict
    errors: list = field(default_factory=list)
    dropped: list = field(default_factory=list)      # [{"field", "reason"}]
    unit: str = ""
    starts_before_window: bool = False
    continues_after_window: bool = False
    key: Optional[str] = None                        # 정규화 사건번호(병합 키)
    quote_total: int = 0
    quote_ok: int = 0
    model_pair: Optional[str] = None                 # 모델이 낸 correction_pair_confidence(유도 전 값). 창 병합 뒤 다시 유도하는 데 쓴다


class _Stats:
    def __init__(self) -> None:
        self.total = 0
        self.ok = 0
        self.dropped: list[dict] = []
        self.anchors: dict[str, list] = {}           # page_id -> 이 사건의 일반 인용 시작 위치들(짧은 표 칸 인용이 어느 행 것인지 고르는 기준, G-D15)

    def drop(self, fld: str, reason: str) -> None:
        self.total += 1
        self.dropped.append({"field": fld, "reason": reason})

    def keep(self) -> None:
        self.total += 1
        self.ok += 1

    def note(self, fld: str, reason: str) -> None:
        """항목을 버리지 않은 보정(예: 원문에 없는 날짜를 null 로)."""
        self.dropped.append({"field": fld, "reason": reason})


# ---------------------------------------------------------------- 인용 찾기
def _pick_nearest(spans: list, near: Optional[list]) -> tuple:
    """일치 구간 여럿 중 기준점(near: 같은 사건의 다른 인용 시작 위치)에 가장 가까운 것. 기준점이 없으면 첫 구간. (구간, 기준점으로 골랐는가)."""
    if near and len(spans) > 1:
        return min(spans, key=lambda se: min(abs(se[0] - p) for p in near)), True
    return spans[0], False


def locate_quote(q: Any, ctx: WindowCtx, hint_page: Optional[str] = None, near: Optional[dict] = None) -> tuple[Optional[dict], str]:
    """모델의 ``{page_id, quote}`` 를 페이지에서 찾아 계약 인용 객체로. 실패하면 (None, 사유).

    대조는 공백을 모두 지운 문자열로 한다(G-D13). 최소 길이(``MIN_QUOTE_CHARS``, 공백 축약 기준)에 못 미치는 인용은
    표 소스(``TABLE_CELL_SOURCES``)에서 표 칸 하나와 정확히 일치할 때만 받는다(G-D15, 사유 ``ok_cell``). near: page_id -> 같은 사건의 다른 인용 시작 위치들.
    같은 문구가 쪽에 여러 번 있으면 near 에 가장 가까운 것을 쓰고(V-FID-01), near 가 없으면 첫 일치를 쓰되 사유를 ``ok_ambiguous`` 로 돌려준다.
    표가 아닌 소스에서 단 사이 간격을 줄마다 가로지른 인용은 ``column_join`` 으로 버린다(V-FID-05)."""
    if not isinstance(q, dict):
        return None, "quote_missing"
    raw = q.get("quote")
    if not isinstance(raw, str):
        return None, "quote_missing"
    cq = collapse(BR_RE.sub(" ", raw))                    # G-D27: <br> 는 길이 규칙에서도 공백이다
    short = len(cq) < MIN_QUOTE_CHARS
    if short and ctx.source_id not in TABLE_CELL_SOURCES:
        return None, "quote_too_short"
    if len(cq) > MAX_QUOTE_CHARS:
        return None, "quote_too_long"
    pid = q.get("page_id") if isinstance(q.get("page_id"), str) else hint_page
    m = re.search(r"<<PAGE\s+(.+?)>>", pid or "")
    if m:
        pid = m.group(1).strip()
    order = ([pid] if pid in ctx.pages else []) + [p for p in ctx.pages if p != pid]
    if short:
        for cand in order:
            span = ctx.pages[cand].find_cell(cq, (near or {}).get(cand))
            if span:
                s, e = span
                return {"quote": ctx.pages[cand].text[s:e], "page_id": cand, "start": s, "end": e}, "ok_cell"
        return None, "quote_too_short"
    for cand in order:
        spans = ctx.pages[cand].find_all(cq)
        if spans:
            (s, e), anchored = _pick_nearest(spans, (near or {}).get(cand))
            if ctx.source_id not in TABLE_CELL_SOURCES and ctx.pages[cand].column_join(s, e):
                return None, "column_join"
            why = "ok" if cand == pid else "ok_relocated"
            if len(spans) > 1 and not anchored:
                why = "ok_ambiguous"
            return {"quote": ctx.pages[cand].text[s:e], "page_id": cand, "start": s, "end": e}, why
    return None, "quote_not_found"


def _ground(q: Any, ctx: WindowCtx, st: _Stats, where: str, hint_page: Optional[str]) -> Optional[dict]:
    loc, why = locate_quote(q, ctx, hint_page, st.anchors)
    if loc is None:
        st.drop(where, why)
        return None
    if why == "ok_ambiguous":
        st.note(where, "ambiguous_quote_first_match")
    if why != "ok_cell":
        st.anchors.setdefault(loc["page_id"], []).append(loc["start"])
    return loc


def _draft_quotes(d: dict):
    """모델 draft 안의 모든 인용 객체(``{page_id, quote}``)."""
    for f in ITEM_FIELDS:
        for it in d.get(f) if isinstance(d.get(f), list) else []:
            if isinstance(it, dict):
                yield it.get("quote")
    if isinstance(d.get("outcome"), dict):
        yield d["outcome"].get("quote")
    for f in ("legal_provisions", "law_versions", "later_history"):
        for it in d.get(f) if isinstance(d.get(f), list) else []:
            if isinstance(it, dict):
                yield it.get("quote")
    for f in ("original_article", "corrected_article"):
        if isinstance(d.get(f), dict):
            yield d[f].get("quote")


def seed_anchors(d: dict, ctx: WindowCtx, st: _Stats, hint_page: Optional[str]) -> None:
    """V-FID-01: 쪽에 한 곳에만 있는 인용(모호하지 않은 인용)의 위치를 먼저 기준점으로 등록한다. 모호한 인용이 먼저 조립돼도 이 사건의 행에 닿게 한다."""
    for q in _draft_quotes(d):
        if not isinstance(q, dict) or not isinstance(q.get("quote"), str):
            continue
        cq = collapse(BR_RE.sub(" ", q["quote"]))
        if len(cq) < MIN_QUOTE_CHARS or len(cq) > MAX_QUOTE_CHARS:
            continue
        pid = q.get("page_id") if isinstance(q.get("page_id"), str) else hint_page
        m = re.search(r"<<PAGE\s+(.+?)>>", pid or "")
        if m:
            pid = m.group(1).strip()
        for cand in ([pid] if pid in ctx.pages else []) + [p for p in ctx.pages if p != pid]:
            spans = ctx.pages[cand].find_all(cq, limit=2)
            if spans:
                if len(spans) == 1:
                    st.anchors.setdefault(cand, []).append(spans[0][0])
                break


# ---------------------------------------------------------------- 필드별 조립
def _clean_free(s: Any, n: int, ctx: WindowCtx) -> tuple[Optional[str], Optional[str]]:
    """자유 서술 문자열: (정리된 값, 버린 사유). 원문에 없는 사건번호·URL 이 있으면 버린다."""
    t = _str(s)
    if t is None:
        return None, "empty"
    bad = foreign_identifiers(t, ctx.source_compact)
    if bad:
        return None, "identifier_not_in_source"
    return _clip(t, n), None


def _items(raw: Any, fld: str, ctx: WindowCtx, st: _Stats, authority: str, hint_page: Optional[str]) -> list:
    out: list[dict] = []
    seen: set = set()
    if not isinstance(raw, list):
        return out
    allowed = allowed_speakers(fld, authority)
    for i, it in enumerate(raw):
        where = f"{fld}[{i}]"
        if not isinstance(it, dict):
            st.drop(where, "not_an_object")
            continue
        text, why = _clean_free(it.get("text"), 600, ctx)
        if text is None:
            st.drop(where, f"text_{why}")
            continue
        speaker = "article" if fld in ("article_claims", "disputed_spans") else it.get("speaker")
        if not isinstance(speaker, str) or speaker not in allowed:       # 모델이 목록·객체를 보내면 집합 검사가 TypeError 를 낸다
            st.drop(where, "speaker_not_allowed")
            continue
        loc = _ground(it.get("quote"), ctx, st, where, hint_page)
        if loc is None:
            continue
        key = (loc["page_id"], loc["start"], loc["end"], text)
        if key in seen:
            st.note(where, "duplicate_item")
            continue
        seen.add(key)
        st.keep()
        out.append({"text": text, "speaker": speaker, "quote": loc})
    return out


ARTICLE_NO_RE = re.compile(r"제\d+조(?:의\d+)?")


def citation_in_page(citation: str, page: "PageText") -> bool:
    """V-FID-04: 조문 인용의 ``제N조``·``제N조의M`` 이 모두 인용 구간과 같은 쪽 원문(공백 제외)에 그대로 있는가.
    깨진 텍스트층('저 18조')을 모델이 '제18조'로 고쳐 읽은 번호를 막는다. 항·호는 동그라미 숫자(①) 등으로 인쇄되므로 확인하지 않는다."""
    return all(t in page.nows for t in ARTICLE_NO_RE.findall(strip_ws(citation)))


DESCRIPTION_PREFIXES = ("위기사는", "이기사는", "위보도는", "이보도는")      # 기관·분석자가 기사를 서술하는 문장의 머리(공백 제외)


def _fix_article_speaker_items(rec_items: dict, st: "_Stats", authority: str, ctx: "WindowCtx") -> None:
    """V-FID-07: speaker article 필드(article_claims·disputed_spans)에는 기사 자체의 문구만 둔다.
    (1) 인용이 '위 기사는 …' 로 시작하면 기관의 서술이므로 factual_background(speaker institution, 판결이면 court_majority)로 옮긴다. 버리지 않으므로 실패율에 넣지 않는다.
    (2) 인용 구간이 당사자 주장(claims_or_requested_remedy)과 같으면 기사 문구가 아니므로 기사 항목을 버린다(일치로 센 것을 뺀다)."""
    def span(it: dict) -> tuple:
        q = it["quote"]
        return (q["page_id"], q["start"], q["end"])
    claim_spans = {span(i) for i in rec_items["claims_or_requested_remedy"]}
    speaker = "court_majority" if authority == "judgment" else "institution"
    facts = rec_items["factual_background"]
    for f in ("article_claims", "disputed_spans"):
        keep = []
        for i, it in enumerate(rec_items[f]):
            where = f"{f}[{i}]"
            if span(it) in claim_spans:
                st.note(where, "span_shared_with_party_claim")
                st.ok = max(0, st.ok - 1)
                continue
            if strip_ws(it["quote"]["quote"]).lstrip("\"'‘’“”「『").startswith(DESCRIPTION_PREFIXES):
                st.note(where, "rerouted_institution_description")
                if span(it) not in {span(x) for x in facts}:
                    facts.append({"text": it["text"], "speaker": speaker, "quote": it["quote"]})
                continue
            keep.append(it)
        rec_items[f] = keep


def _enum(v: Any, allowed: tuple, default: str) -> str:
    return v if v in allowed else default


def _article(raw: Any, ctx: WindowCtx, st: _Stats, name: str, hint_page: Optional[str]) -> dict:
    status = _enum(raw.get("status") if isinstance(raw, dict) else None, ("quoted_in_source", "described_only", "absent", "unknown"), "unknown")
    if status != "quoted_in_source":
        return {"status": status, "quote": None}
    loc = _ground(raw.get("quote"), ctx, st, f"{name}.quote", hint_page)
    if loc is None:
        return {"status": "described_only", "quote": None}
    st.keep()
    return {"status": "quoted_in_source", "quote": loc}


def assemble_case(d: Any, ctx: WindowCtx, k: int) -> Optional[CaseOut]:
    """모델의 사건 draft 하나를 case record 로. d 가 객체가 아니면 None."""
    if not isinstance(d, dict):
        return None
    st = _Stats()
    prof = ctx.profile
    hint = _str(d.get("first_page_id"))
    if hint and (m := re.search(r"<<PAGE\s+(.+?)>>", hint)):
        hint = m.group(1).strip()
    if hint not in ctx.pages:
        hint = next(iter(ctx.pages), None)

    authority = d.get("authority_type")
    if authority not in prof.authority_types:
        if authority is not None:
            st.note("authority_type", "not_in_profile_allowlist")
        authority = prof.authority_types[0]
    institution = d.get("institution")
    if institution not in prof.institutions:
        st.note("institution", "not_in_profile_allowlist")
        institution = prof.institutions[0]

    case_number = _str(d.get("case_number"))
    if case_number is not None:
        if len(case_number) > 80 or not case_number_in_source(case_number, ctx.source_text, ctx.source_compact):
            st.note("case_number", "not_in_source")
            case_number = None
    dates = {}
    for key in ("decision_date", "publication_date"):
        v = _iso_date(d.get(key))
        if d.get(key) is not None and (v is None or not date_in_source(v, ctx.source_text)):
            st.note(key, "date_not_in_source")
            v = None
        dates[key] = v
    # 판례: 이 문서가 곧 하나의 판결이므로 사건번호·선고일은 장부(법제처 응답)의 값이 정본이다. 모델은 원심·참조판례의 번호와 날짜를
    # 이 판결의 것으로 잘못 고를 수 있어서(원문에 있기만 하면 위 대조를 통과한다) 장부 값으로 바꾸고, 모델 값이 달랐으면 기록한다.
    led_no = _str(ctx.ledger.get("case_number"))
    if led_no:
        allowed = {compact(led_no)} | {compact(t) for t in ctx.ledger.get("case_number_tokens") or ()}
        if d.get("case_number") is not None and (_str(d.get("case_number")) is None or compact(_str(d.get("case_number"))) not in allowed):
            st.note("case_number", "not_in_ledger")
        case_number = led_no
    led_date = _iso_date(ctx.ledger.get("decision_date"))
    if led_date:
        if d.get("decision_date") is not None and _iso_date(d.get("decision_date")) != led_date:
            st.note("decision_date", "not_in_ledger")
        dates["decision_date"] = led_date
    inst_name, _w = _clean_free(d.get("institution_name"), 120, ctx)
    dec_type, _w2 = _clean_free(d.get("decision_type"), 80, ctx)

    parties = []
    for i, p in enumerate(d.get("parties") if isinstance(d.get("parties"), list) else []):
        if not isinstance(p, dict):
            continue
        label, why = _clean_free(p.get("label"), 40, ctx)
        role = _enum(p.get("role"), ("claimant", "respondent", "media_outlet", "reporter", "third_party", "other"), "other")
        if label is None:
            st.note(f"parties[{i}]", f"label_{why}")
            continue
        if {"role": role, "label": label} not in parties:
            parties.append({"role": role, "label": label})
    tags = []
    for i, t in enumerate(d.get("issue_tags") if isinstance(d.get("issue_tags"), list) else []):
        tt, why = _clean_free(t, 40, ctx)
        if tt is None:
            st.note(f"issue_tags[{i}]", f"tag_{why}")
        elif tt not in tags and len(tags) < 20:
            tags.append(tt)

    seed_anchors(d, ctx, st, hint)                  # V-FID-01: 한 곳에만 있는 인용의 위치를 먼저 기준점으로
    rec_items = {f: _items(d.get(f), f, ctx, st, authority, hint) for f in ITEM_FIELDS}
    _fix_article_speaker_items(rec_items, st, authority, ctx)

    outcome = None
    out_raw = d.get("outcome")
    if isinstance(out_raw, dict):
        got = _items([out_raw], "outcome", ctx, st, authority, hint)
        for dr in st.dropped:                       # 항목 이름을 outcome 으로 바로잡는다
            if dr["field"] == "outcome[0]":
                dr["field"] = "outcome"
        outcome = got[0] if got else None

    provisions = []
    for i, p in enumerate(d.get("legal_provisions") if isinstance(d.get("legal_provisions"), list) else []):
        where = f"legal_provisions[{i}]"
        if not isinstance(p, dict):
            st.drop(where, "not_an_object")
            continue
        cit, why = _clean_free(p.get("citation"), 300, ctx)
        if cit is None:
            st.drop(where, f"citation_{why}")
            continue
        loc = _ground(p.get("quote"), ctx, st, where, hint)
        if loc is None:
            continue
        if not citation_in_page(cit, ctx.pages[loc["page_id"]]):
            st.note(where, "citation_not_in_source")          # V-FID-04: 모델이 깨진 텍스트층을 고쳐 읽은 조문 번호. 항목만 버리고 인용 대조 실패율에는 넣지 않는다(인용 자체는 일치했다)
            continue
        st.keep()
        provisions.append({"citation": cit, "quote": loc})

    law_versions = []
    for i, lv in enumerate(d.get("law_versions") if isinstance(d.get("law_versions"), list) else []):
        where = f"law_versions[{i}]"
        if not isinstance(lv, dict):
            st.drop(where, "not_an_object")
            continue
        law, why = _clean_free(lv.get("law"), 200, ctx)
        if law is None:
            st.drop(where, f"law_{why}")
            continue
        ef, et = _iso_date(lv.get("effective_from")), _iso_date(lv.get("effective_to"))
        for nm, val, orig in (("effective_from", ef, lv.get("effective_from")), ("effective_to", et, lv.get("effective_to"))):
            if orig is not None and (val is None or not date_in_source(val, ctx.source_text)):
                st.note(f"{where}.{nm}", "date_not_in_source")
        ef = ef if ef and date_in_source(ef, ctx.source_text) else None
        et = et if et and date_in_source(et, ctx.source_text) else None
        if ef and et and ef > et:
            ef = et = None
            st.note(where, "effective_range_inverted")
        loc = None
        if lv.get("quote") is not None:
            loc = _ground(lv.get("quote"), ctx, st, f"{where}.quote", hint)
            if loc is not None:
                st.keep()
        law_versions.append({"law": law, "effective_from": ef, "effective_to": et,
                             "relation": _enum(lv.get("relation"), ("applied_at_time", "current", "transitional", "unknown"), "unknown"),
                             "quote": loc})

    orig_art = _article(d.get("original_article"), ctx, st, "original_article", hint)
    corr_art = _article(d.get("corrected_article"), ctx, st, "corrected_article", hint)
    model_pair = _enum(d.get("correction_pair_confidence"), PAIR_VALUES, "not_applicable")
    pair = derive_pair_confidence(model_pair, orig_art, corr_art)      # G-D17: 원보도 인용 건수로 결정적으로 유도한다. 모순을 이유로 격리하지 않는다
    if d.get("correction_pair_confidence") in PAIR_VALUES and pair != model_pair:
        st.note("correction_pair_confidence", f"derived_from_original_quotes:{model_pair}->{pair}")

    history = []
    ref_pages = {pid for pid, row in ctx.page_meta.items() if _section_kind(row) == "ref_cases"}
    ref_compact = compact("\n".join(ctx.pages[pid].text for pid in ref_pages if pid in ctx.pages))
    other_compact = compact("\n".join(pt.text for pid, pt in ctx.pages.items() if pid not in ref_pages))
    for i, h in enumerate(d.get("later_history") if isinstance(d.get("later_history"), list) else []):
        where = f"appeal_or_later_history[{i}]"
        if not isinstance(h, dict):
            st.drop(where, "not_an_object")
            continue
        note, why = _clean_free(h.get("note"), 300, ctx)
        if note is None:
            st.drop(where, f"note_{why}")
            continue
        cn = _str(h.get("case_number"))
        if cn is not None and (len(cn) > 80 or not case_number_in_source(cn, ctx.source_text, ctx.source_compact)):
            st.note(f"{where}.case_number", "not_in_source")
            cn = None
        if cn is not None and compact(cn) in ref_compact and compact(cn) not in other_compact:
            st.note(where, "cited_precedent_removed")      # G-D3: 번호가 참조판례 구간에만 인쇄돼 있으면 인용(quote) 없이도 선례다
            continue
        loc = None
        if h.get("quote") is not None:
            loc = _ground(h.get("quote"), ctx, st, f"{where}.quote", hint)
            if loc is not None:
                if loc["page_id"] in ref_pages:            # G-D3: 인용된 선례는 이 사건의 이력이 아니다. 그 항목만 지운다(실패율에는 넣지 않는다)
                    st.note(where, "cited_precedent_removed")
                    continue
                st.keep()
        history.append({"relation": _enum(h.get("relation"), ("appeal", "remand", "final", "related_case", "later_correction", "unknown"),
                                          "unknown"), "case_number": cn, "note": note, "quote": loc})

    # ---- 결론 라벨 ----
    # 모델이 낸 outcome_type 은 열거형 안이면 그대로 둔다. authority_type 과 맞지 않는 값(예: 조정합의 사건의 decision)은
    # 보정하지 않고 계약 교차검증 오류로 드러나 quarantined 가 된다.
    outcome_type = _enum(d.get("outcome_type"), ("judgment", "decision", "settlement", "withdrawal", "dismissal_procedural",
                                                 "dismissal_merits", "correction", "guideline", "research", "unknown"), "unknown")
    scope = _enum(d.get("outcome_scope"), ("whole", "partial", "procedural", "unclear"), "unclear")
    raw_outcome_type = d.get("outcome_type")
    label_errors: list[str] = []
    if authority == "judgment" and outcome is not None and isinstance(raw_outcome_type, str) and raw_outcome_type not in ("judgment", "unknown"):
        # G-D2: 법원 판결의 처분(상고기각 등)은 outcome_type 이 아니다. 열거형 밖의 값을 unknown 으로 고쳐 통과시키지 않고 격리한다.
        label_errors.append(f"label_guard: court judgment outcome_type must be 'judgment' (got '{raw_outcome_type[:40]}'); "
                            "dismissal_* is for PAC and IENDC procedures only")
    if outcome is None:
        if outcome_type != "unknown" or scope != "unclear":
            st.note("outcome", "no_grounded_outcome_label_reset_to_unknown")
        outcome_type, scope = "unknown", "unclear"
    sa = d.get("settlement_or_adjudication")
    if sa not in ("settlement", "adjudication", "withdrawal", "none", "unknown"):
        sa = {"settlement": "settlement", "withdrawal": "withdrawal", "research": "none", "unknown": "unknown"}.get(outcome_type, "adjudication")

    sup: list[str] = []
    for _w3, qq in iter_quotes_of(rec_items, outcome, provisions, law_versions, orig_art, corr_art, history):
        if qq["page_id"] not in sup:
            sup.append(qq["page_id"])
    ungrounded = not sup
    if ungrounded:
        sup = [hint] if hint else []
    sup.sort()

    case_id = (f"{ctx.target_key}:{compact(case_number)}" if case_number else f"{ctx.target_key}:{ctx.unit}n{k:02d}")
    record = {
        "schema_version": "0.1", "case_id": case_id, "doc_id": ctx.doc_id, "att_id": ctx.att_id,
        "institution": institution, "institution_name": inst_name, "authority_type": authority, "decision_type": dec_type,
        "case_number": case_number, "decision_date": dates["decision_date"], "publication_date": dates["publication_date"],
        "parties_redacted": parties, "factual_background": rec_items["factual_background"], "article_claims": rec_items["article_claims"],
        "disputed_spans": rec_items["disputed_spans"], "issue_tags": tags,
        "claims_or_requested_remedy": rec_items["claims_or_requested_remedy"], "outcome_type": outcome_type, "outcome": outcome,
        "outcome_scope": scope, "legal_reasoning": rec_items["legal_reasoning"], "legal_provisions": provisions,
        "law_versions": law_versions, "supporting_passage_ids": sup, "source_locators": source_locators(sup, ctx.page_meta),
        "settlement_or_adjudication": sa, "available_original_article": orig_art, "available_corrected_article": corr_art,
        "correction_pair_confidence": pair, "appeal_or_later_history": history, "verified_by": [], "verification_status": "unverified",
        "quote_check": {"total": st.total, "matched": st.ok, "dropped_fields": [f"{x['field']}:{x['reason']}" for x in st.dropped]},
        "structurer": dict(ctx.structurer),
    }
    co = CaseOut(record=record, dropped=st.dropped, unit=ctx.unit, key=compact(case_number) if case_number else None,
                 starts_before_window=bool(d.get("starts_before_window")), continues_after_window=bool(d.get("continues_after_window")),
                 quote_total=st.total, quote_ok=st.ok, model_pair=model_pair)
    finalize(co, {pid: pt.text for pid, pt in ctx.pages.items()}, ctx.quarantine_rate, ungrounded=ungrounded, extra_errors=label_errors)
    return co


PAIR_VALUES = ("not_applicable", "before_missing", "partial_quote", "full_pair_verified", "unverified_pair")


def derive_pair_confidence(model_value: Any, original: dict, corrected: dict, *, full_text_confirmed: bool = False) -> str:
    """G-D17. 원보도(정정 전 기사) 인용이 1건 이상이면 ``partial_quote``, 0건이면 ``before_missing`` 이다. 원보도 인용이 있는데 ``before_missing`` 일 수 없다.
    모델 값은 둘 다 ``quoted_in_source`` 이고 별도의 전문 확인 정보(``full_text_confirmed``)가 있을 때만 ``full_pair_verified`` 를 쓰며(CX1-6, 없으면 ``partial_quote``), 그때도 ``before_missing``·``not_applicable`` 은 인용과 어긋나므로 ``partial_quote`` 로 둔다.
    예외: 모델이 ``not_applicable``(정정 사건이 아님)이라 했고 어느 기사도 인용·서술되지 않았으면(둘 다 absent/unknown) 그대로 둔다(판결·심의 사건 등).
    원보도를 서술만 했거나(``described_only``) 정정문만 인용·서술됐으면 정정 사건이므로 ``before_missing`` 이다(V-FID-06)."""
    mv = model_value if model_value in PAIR_VALUES else "not_applicable"
    oq = (original or {}).get("status") == "quoted_in_source"
    cq = (corrected or {}).get("status") == "quoted_in_source"
    if oq and cq:
        # CX1-6: quoted_in_source 는 부분문자열이 원문에 있다는 뜻일 뿐 전문 확인이 아니다. 별도의 전문 확인 정보(full_text_confirmed)가 없으면 full_pair_verified 를 쓰지 않는다
        if mv == "full_pair_verified":
            return mv if full_text_confirmed else "partial_quote"
        return mv if mv in ("partial_quote", "unverified_pair") else "partial_quote"
    if oq:
        return "partial_quote"
    quiet = lambda a: (a or {}).get("status") in ("absent", "unknown", None)          # noqa: E731  인용도 서술도 없다
    if mv == "not_applicable" and quiet(original) and quiet(corrected):
        return "not_applicable"
    return "before_missing"


def _section_kind(row: Any) -> Optional[str]:
    try:
        q = row.get("quality_json") if isinstance(row, dict) else None
        q = json.loads(q) if isinstance(q, str) else q
        return q.get("section_kind") if isinstance(q, dict) else None
    except ValueError:
        return None


def iter_quotes_of(items: dict, outcome: Optional[dict], provisions: list, law_versions: list, orig: dict, corr: dict, history: list):
    for f, lst in items.items():
        for it in lst:
            yield f, it["quote"]
    if outcome:
        yield "outcome", outcome["quote"]
    for p in provisions:
        yield "legal_provisions", p["quote"]
    for lv in law_versions:
        if lv["quote"]:
            yield "law_versions", lv["quote"]
    for a in (orig, corr):
        if a["quote"]:
            yield "article", a["quote"]
    for h in history:
        if h["quote"]:
            yield "appeal_or_later_history", h["quote"]


def iter_record_quotes(rec: dict):
    """기록(record)의 모든 인용 객체."""
    yield from (q for _f, q in iter_quotes_of({f: rec[f] for f in ITEM_FIELDS}, rec["outcome"], rec["legal_provisions"],
                                              rec["law_versions"], rec["available_original_article"], rec["available_corrected_article"],
                                              rec["appeal_or_later_history"]))


def source_locators(page_ids: list, page_meta: dict) -> list:
    out = []
    for pid in page_ids:
        row = page_meta.get(pid) or {}
        no = row.get("pdf_page_no")
        out.append({"kind": "pdf_page", "value": str(no)} if no is not None else {"kind": "text_range", "value": pid})
    return out or [{"kind": "text_range", "value": "unknown"}]


# ---------------------------------------------------------------- 최종 판정(스키마·교차검증·라벨 가드·실패율)
def label_guard_errors(rec: dict) -> list:
    """원문 표지로 확인되는 라벨 모순. 보정하지 않고 오류로만 남긴다(사건은 quarantined)."""
    errs = []
    if rec.get("authority_type") == "judgment" and rec.get("outcome_type") not in ("judgment", "unknown"):
        errs.append(f"label_guard: court judgment outcome_type must be 'judgment' (got '{rec.get('outcome_type')}'); "
                    "dismissal_* is for PAC and IENDC procedures only")
    out = rec.get("outcome")
    if out:
        txt = out["text"] + " " + out["quote"]["quote"]
        o = rec["outcome_type"]
        if o == "dismissal_merits" and "각하" in txt and "기각" not in txt:
            errs.append("label_guard: outcome mentions 각하 (procedural) but outcome_type is dismissal_merits")
        if o == "dismissal_procedural" and "기각" in txt and "각하" not in txt:
            errs.append("label_guard: outcome mentions 기각 but outcome_type is dismissal_procedural")
        if o == "settlement" and ("각하" in txt or "중재결정" in txt) and "합의" not in txt:
            errs.append("label_guard: outcome text does not support settlement")
    return errs


def finalize(co: CaseOut, pages: dict, rate: float, *, ungrounded: bool = False, extra_errors: Optional[list] = None) -> None:
    """record 를 계약 교차검증하고 quarantined 여부를 정한다(제자리 수정)."""
    rec = co.record
    errs = list(contracts.validate_case_record(rec, pages))
    errs += label_guard_errors(rec)
    errs += extra_errors or []
    total, ok = rec["quote_check"]["total"], rec["quote_check"]["matched"]
    if ungrounded or total == 0:
        errs.append("no grounded content (every proposed item was dropped or none was proposed)")
    elif (total - ok) / total > rate:
        errs.append(f"quote failure rate {(total - ok) / total:.2f} exceeds threshold {rate:.2f}")
    co.errors = errs
    rec["verification_status"] = "quarantined" if errs else "unverified"
    rec["verified_by"] = []


# ---------------------------------------------------------------- 병합 칸 인용(G-D5)
SHARED_QUOTE_FIELDS = ("factual_background", "article_claims", "disputed_spans")      # 사실 인용 필드. 결론·기관 판단·법령 인용은 대상이 아니다


def drop_shared_fact_quotes(cases: list, pages: dict, rate: float, page_meta: dict) -> int:
    """한 창에서 나온 CaseOut 들 가운데 같은 구간(page_id, start, end)이 2개 이상의 사건에서 사실 인용으로 쓰였으면 그 인용 항목을 모든 사건에서 버린다.

    표의 병합 칸(rowspan)은 여러 행에 걸치므로 한 행의 사실 근거가 되지 못한다. 버린 항목은 ``quote_check.dropped_fields`` 에
    ``<필드>[<조립된 목록의 위치>]:shared_cell_quote`` 로 남기고 matched 에서 뺀다(실패율에 반영). 그 뒤 근거 쪽 목록과 판정을 다시 계산한다.
    반환: 버린 항목 수."""
    owners: dict = {}
    for ci, co in enumerate(cases):
        for f in SHARED_QUOTE_FIELDS:
            for it in co.record[f]:
                qq = it["quote"]
                owners.setdefault((qq["page_id"], qq["start"], qq["end"]), set()).add(ci)
    shared = {k for k, v in owners.items() if len(v) >= 2}
    if not shared:
        return 0
    total = 0
    for co in cases:
        rec = co.record
        dropped_here = 0
        for f in SHARED_QUOTE_FIELDS:
            keep = []
            for i, it in enumerate(rec[f]):
                qq = it["quote"]
                if (qq["page_id"], qq["start"], qq["end"]) in shared:
                    co.dropped.append({"field": f"{f}[{i}]", "reason": "shared_cell_quote"})
                    rec["quote_check"]["dropped_fields"].append(f"{f}[{i}]:shared_cell_quote")
                    rec["quote_check"]["matched"] = max(0, rec["quote_check"]["matched"] - 1)
                    dropped_here += 1
                else:
                    keep.append(it)
            rec[f] = keep
        if not dropped_here:
            continue
        total += dropped_here
        sup = sorted({qq["page_id"] for qq in iter_record_quotes(rec)})
        ungrounded = not sup
        if not ungrounded:
            rec["supporting_passage_ids"] = sup
            rec["source_locators"] = source_locators(sup, page_meta)
        co.quote_ok = rec["quote_check"]["matched"]
        co.quote_total = rec["quote_check"]["total"]
        finalize(co, pages, rate, ungrounded=ungrounded)
    return total


def drop_shared_outcome_quotes(cases: list, pages: dict, rate: float, page_meta: dict) -> int:
    """V-FID-02: 한 창에서 2건 이상의 사건이 같은 구간(page_id, start, end)을 결론(outcome)으로 인용했고 그 구간이 그 사건의 유일한 근거이면
    결론을 버리고(outcome null, outcome_type unknown, outcome_scope unclear) 근거 없음으로 격리한다. 표 머리글('■ 시정권고 결정 67건')처럼 모든 행에 같은 구간은
    어느 행의 위치도 아니다. 사건에 자기 근거(다른 인용)가 따로 있으면 공유된 결론을 그대로 둔다(병합 칸의 결과가 여러 행에 걸치는 경우, G-D5가 결론을 제외한 이유).
    버린 항목은 ``outcome:shared_outcome_quote`` 로 남기고 matched 에서 뺀다. 반환: 버린 사건 수."""
    def span(q: dict) -> tuple:
        return (q["page_id"], q["start"], q["end"])
    owners: dict = {}
    for ci, co in enumerate(cases):
        o = co.record.get("outcome")
        if o:
            owners.setdefault(span(o["quote"]), set()).add(ci)
    shared = {k for k, v in owners.items() if len(v) >= 2}
    n = 0
    for co in cases:
        rec = co.record
        o = rec.get("outcome")
        if not o or span(o["quote"]) not in shared:
            continue
        if {span(q) for q in iter_record_quotes(rec)} != {span(o["quote"])}:
            continue
        rec["outcome"], rec["outcome_type"], rec["outcome_scope"] = None, "unknown", "unclear"
        rec["quote_check"]["matched"] = max(0, rec["quote_check"]["matched"] - 1)
        rec["quote_check"]["dropped_fields"].append("outcome:shared_outcome_quote")
        co.dropped.append({"field": "outcome", "reason": "shared_outcome_quote"})
        co.quote_ok, co.quote_total = rec["quote_check"]["matched"], rec["quote_check"]["total"]
        finalize(co, pages, rate, ungrounded=True)
        n += 1
    return n


# ---------------------------------------------------------------- 표 행 사건의 행 고유 칸 인용(G-D26)
HEADER_MARKS = "■□▣▪◆◇●○※▶▷◎"                       # 표 머리줄·소제목 표지
HEADER_WORDS = ("순번", "의결번호", "매체명", "보도일", "지면", "기사제목", "침해유형", "결과", "번호", "선거명", "언론사", "보도제목", "위반내용", "조치", "구분")
ROW_UNIQUE_REASON = "row_unique_quote_missing"


def _header_like(text: str) -> bool:
    t = strip_ws(text)
    return t[:1] in HEADER_MARKS or sum(1 for w in HEADER_WORDS if w in t) >= 3


def require_row_unique_quote(cases: list, pages: dict, source_id: str) -> int:
    """G-D26: 표 소스(``TABLE_CELL_SOURCES``)의 사건은 행 고유 칸을 인용한 근거가 최소 1개 있어야 한다. 없으면 격리한다(근거는 지우지 않는다).

    행 고유 인용 = (a) 사건 자신의 사건번호(공백·<br> 제외)를 포함하는 인용, 또는 (b) 그 구간을 이 사건만 인용했고(``cases`` 전체 기준) 쪽 안에서 그 문구가 한 곳뿐이며
    표 머리줄처럼 보이지 않는 인용(머리 표지 ■□… 로 시작하거나 머리 낱말 3개 이상). 되풀이되는 결과 칸('시정권고')이나 머리줄만 인용한 사건은 어느 행의 위치도 아니다.
    이미 격리된 사건은 건드리지 않는다. 오류는 ``row_unique_quote_missing: …`` 로 ``errors`` 에 남는다. 반환: 새로 격리한 사건 수.
    ``pages``: page_id -> PageText. 표 소스가 아니면 아무것도 하지 않는다."""
    if source_id not in TABLE_CELL_SOURCES:
        return 0

    def span(q: dict) -> tuple:
        return (q["page_id"], q["start"], q["end"])
    owners: dict = {}
    for ci, co in enumerate(cases):
        for q in iter_record_quotes(co.record):
            owners.setdefault(span(q), set()).add(ci)
    n = 0
    for ci, co in enumerate(cases):
        rec = co.record
        if rec["verification_status"] == "quarantined":
            continue
        cn = compact(rec["case_number"]) if rec.get("case_number") else None
        ok = False
        for q in iter_record_quotes(rec):
            if cn and cn in compact(q["quote"]):
                ok = True
                break
            pt = pages.get(q["page_id"])
            if pt is not None and owners[span(q)] == {ci} and len(pt.find_all(q["quote"], limit=2)) == 1 and not _header_like(q["quote"]):
                ok = True
                break
        if ok:
            continue
        msg = f"{ROW_UNIQUE_REASON}: no quote is a cell unique to this table row (own case number or a page-unique cell not shared with another case)"
        co.errors = list(co.errors) + [msg]
        rec["verification_status"] = "quarantined"
        co.dropped.append({"field": "quotes", "reason": ROW_UNIQUE_REASON})
        n += 1
    return n


# ---------------------------------------------------------------- 창 사이 병합
def _span_key(it: dict) -> tuple:
    q = it.get("quote") or {}
    label = it.get("text") or it.get("citation") or it.get("note") or it.get("law")
    if q.get("page_id") is None:
        # 인용이 없는 항목(law_versions·appeal_or_later_history)은 같은 문구라도 날짜·관계·사건번호가 다르면 다른 항목이다: 모든 스칼라 필드를 키에 넣는다
        scalars = tuple(sorted((k, v) for k, v in it.items() if k != "quote" and isinstance(v, (str, int, float, bool, type(None)))))
        return (None, None, None, label, scalars)
    return (q.get("page_id"), q.get("start"), q.get("end"), label)


_PAIR_RANK = {"full_pair_verified": 4, "partial_quote": 3, "before_missing": 2, "unverified_pair": 1, "not_applicable": 0}


def merge_cases(a: CaseOut, b: CaseOut, pages: dict, rate: float) -> CaseOut:
    """같은 사건번호가 인접 창에 나뉘어 나온 두 CaseOut 을 하나로. 결론이 충돌하면 오류로 남겨 quarantined."""
    ra, rb = copy.deepcopy(a.record), b.record
    conflicts: list[str] = []
    for f in LIST_MERGE_FIELDS:
        seen = {_span_key(x) for x in ra[f]}
        for x in rb[f]:
            if _span_key(x) not in seen:
                ra[f].append(copy.deepcopy(x))
                seen.add(_span_key(x))
    for p in rb["parties_redacted"]:
        if p not in ra["parties_redacted"]:
            ra["parties_redacted"].append(p)
    for t in rb["issue_tags"]:
        if t not in ra["issue_tags"] and len(ra["issue_tags"]) < 20:
            ra["issue_tags"].append(t)
    for f in ("institution_name", "decision_type", "decision_date", "publication_date"):
        ra[f] = ra[f] or rb[f]
    if not ra["case_number"] and rb["case_number"]:
        ra["case_number"] = rb["case_number"]                  # 이어짐 병합: 번호가 있는 쪽의(원문 대조를 마친) 번호와 번호 기반 case_id 를 따른다
        ra["case_id"] = rb["case_id"]
    if ra["authority_type"] != rb["authority_type"]:
        conflicts.append(f"merge_conflict: authority_type {ra['authority_type']} vs {rb['authority_type']}")
    if ra["outcome"] is None and rb["outcome"] is not None:
        for f in ("outcome", "outcome_type", "outcome_scope", "settlement_or_adjudication"):
            ra[f] = copy.deepcopy(rb[f])
    elif ra["outcome"] is not None and rb["outcome"] is not None and ra["outcome_type"] != rb["outcome_type"]:
        conflicts.append(f"merge_conflict: outcome_type {ra['outcome_type']} vs {rb['outcome_type']}")
    for f in ("available_original_article", "available_corrected_article"):
        if ra[f]["status"] != "quoted_in_source" and rb[f]["status"] == "quoted_in_source":
            ra[f] = copy.deepcopy(rb[f])
        elif ra[f]["status"] in ("unknown", "absent") and rb[f]["status"] == "described_only":
            ra[f] = copy.deepcopy(rb[f])
    # G-D17: 병합된 기사 인용으로 다시 유도한다. 모델 값은 창마다 낸 값(유도 전) 중 순위가 높은 쪽을 쓴다.
    mpa, mpb = a.model_pair or ra["correction_pair_confidence"], b.model_pair or rb["correction_pair_confidence"]
    best = mpb if _PAIR_RANK[mpb] > _PAIR_RANK[mpa] else mpa
    ra["correction_pair_confidence"] = derive_pair_confidence(best, ra["available_original_article"], ra["available_corrected_article"])
    ra["supporting_passage_ids"] = sorted({q["page_id"] for q in iter_record_quotes(ra)} or set(ra["supporting_passage_ids"]) | set(rb["supporting_passage_ids"]))
    meta = {}
    for loc in ra["source_locators"] + rb["source_locators"]:
        meta[(loc["kind"], loc["value"])] = loc
    ra["source_locators"] = [meta[k] for k in sorted(meta)]
    qa, qb = ra["quote_check"], rb["quote_check"]
    ra["quote_check"] = {"total": qa["total"] + qb["total"], "matched": qa["matched"] + qb["matched"],
                         "dropped_fields": qa["dropped_fields"] + qb["dropped_fields"]}
    out = CaseOut(record=ra, dropped=a.dropped + b.dropped, unit=a.unit, key=a.key or b.key, starts_before_window=a.starts_before_window,
                  continues_after_window=b.continues_after_window, quote_total=ra["quote_check"]["total"], quote_ok=ra["quote_check"]["matched"],
                  model_pair=best)
    finalize(out, pages, rate)
    # 한쪽이 이미 격리됐던 사유(스키마 오류 포함)는 병합 뒤에도 남긴다
    out.errors = sorted(set(out.errors) | set(conflicts) | {e for e in a.errors + b.errors if e.startswith(("merge_conflict", "label_guard"))})
    if conflicts or out.errors:
        out.record["verification_status"] = "quarantined"
    return out


# ---------------------------------------------------------------- chunk 본문(원문 그대로)
CHUNK_MERGE_GAP = 120


def chunk_for_case(rec: dict, pages: dict, page_meta: dict) -> Optional[dict]:
    """사건의 chunk: 사건이 인용한 구간의 원문 그대로(모델이 쓴 요약이 아님).

    한 쪽 안에서 인용 구간끼리 사이 간격이 ``CHUNK_MERGE_GAP`` 글자 이하면 사이 원문까지 이어 붙이고, 더 멀면 따로 둔다.
    쪽에 사건이 여러 개(표 행, 한 쪽의 두 사례)여도 다른 사건의 본문을 끌어오지 않도록 첫 인용~끝 인용 전체를 한 덩어리로 잡지 않는다.
    반환 {text, locator_kind, locator_value} 또는 None(인용 없음). text_range 의 locator_value 는 ``page_id:start-end`` 를 ``;`` 로 이어
    본문 조각 순서대로 모두 적는다(첫 구간만 적으면 뒤 구간 인용이 앞 구간 위치로 보인다).
    """
    by_page: dict[str, list] = {}
    for q in iter_record_quotes(rec):
        by_page.setdefault(q["page_id"], []).append([q["start"], q["end"]])
    if not by_page:
        return None

    def sort_key(p: str) -> tuple:
        no = (page_meta.get(p) or {}).get("pdf_page_no")
        return (no is None, no or 0, p)
    order = sorted(by_page, key=sort_key)
    parts: list[str] = []
    spans: list[tuple] = []                                # 본문 조각과 같은 순서의 (page_id, start, end)
    for p in order:
        merged: list[list] = []
        for s, e in sorted(by_page[p]):
            if merged and s - merged[-1][1] <= CHUNK_MERGE_GAP:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        parts.extend(pages[p][s:e] for s, e in merged if p in pages)
        spans.extend((p, s, e) for s, e in merged if p in pages)
    nos = [(page_meta.get(p) or {}).get("pdf_page_no") for p in order]
    if all(n is not None for n in nos):
        kind, value = "pdf_page", (str(nos[0]) if nos[0] == nos[-1] else f"{min(nos)}-{max(nos)}")
    else:
        kind, value = "text_range", ";".join(f"{p}:{s}-{e}" for p, s, e in spans)       # 본문에 이어 붙인 조각마다 위치를 모두 적는다
    return {"text": "\n".join(parts), "locator_kind": kind, "locator_value": value}


def dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)

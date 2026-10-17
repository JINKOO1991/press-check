"""P3 검색 색인: 수집 장부의 chunks 를 읽기 전용 SQLite 색인으로 만들고 ``Corpus`` 인터페이스로 검색한다(P3_DESIGN §1·§2).

빌드(``build_index``)는 수집 CLI ``python -m press_review.ingestion build_index`` 가 부른다. 장부는 읽기만 하고(``mode=ro``,
한 읽기 트랜잭션) 색인은 ``<data>/index/corpus-<snapshot>.sqlite`` 에 임시 파일로 쓴 뒤 원자적으로 옮기고 ``CURRENT`` 포인터를 갱신한다.
시간 상한으로 중단되면 임시 파일을 지우고 아무것도 발행하지 않는다.

넣는 것: ``cases.verification_status`` 가 unverified/verified 인 case 의 chunk, case 없는 연구 chunk. 격리·반려 case, 격리·보류 문서,
유효 URL 이 없는 chunk, 권위 유형을 알 수 없는 chunk 는 제외하고 제외 사유별 건수를 결과에 싣는다.

snapshot id = 정렬한 passage 정본(텍스트·URL·위치·메타·권리 포함, 수집 시각 제외) 해시 + 색인 형식 버전. 같은 입력이면 같은 id.

같은 본문 중복: 본문과 사건 키·검색·공개 메타데이터(사건번호·권위·기준일·결과·태그·위험 경로·권리·검증 등, 출처·passage id·url·위치 제외)가 모두 같은 chunk 만
하나로 접고 나머지는 ``passage_alias`` 로 남긴다. 본문만 같고 메타데이터가 다른 chunk 는 각자 passage 로 남아 정확 검색·필터에 걸리며,
검색 결과 단계에서 같은 본문을 하나로 접는다(Codex 1회차 F1·F2, pdr-index-4 형식에서만).

검색: 사건번호·조문 정확 검색 -> 본문 속 패턴(FTS5 trigram 구문) -> FTS5 trigram BM25 -> 2글자 질의어는 ``bigram_terms`` 로 보완.
벡터 검색은 하지 않는다. 사건번호는 병합 표기(``2023다240763, 240770``)를 번호별로 풀어 색인한다. ``as_of`` 필터는 기준일(reference_date)로 건다: 결정일, 없으면 수록
간행물의 발행일(G-D23), 그것도 없으면 간행물 연도의 12월 31일(G-D25, basis=year_upper_bound)이다. 모두 없는 passage(date_unknown)는 필터에서 빼고 남기며 ``date_unknown`` 으로 표시한다(합성 코퍼스와 같은 동작). 실행 환경의 SQLite 에 FTS5 trigram 이 없으면 bigram 전용으로 내려가고 ``status()`` 에 밝힌다.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterator

from . import config
from .contracts import core_coverage, is_date, is_uri
from . import pubyear
from .corpus import (AUTHORITY_TYPES, MAX_LIMIT, MAX_QUERY_CP, RISK_CHANNELS, Corpus, CorpusError, _check_search_args, reference_date)
from .ingestion.structure_records import LIST_CHUNK_AUTHORITY

INDEX_FORMAT = "pdr-index-4"       # 4: chunk_kind 열, 같은 본문 passage 중복 제거와 passage_alias 표(P3 재빌드). 3: counter_signals 계산 변경(파기·환송 문맥과 면책 부정 태그 제외, CX1-8). 2: reference_date·reference_date_basis 열(G-D23), stats 의 cases_by_authority_type(G-D22)
READABLE_FORMATS = ("pdr-index-3", INDEX_FORMAT)   # 3번 형식 색인(chunk_kind 열·별칭 표 없음)도 연다: 재빌드 뒤에도 이전 snapshot 으로 되돌릴 수 있게 한다
SNAPSHOT_PREFIX = "idx-"
SNAPSHOT_HEX_LEN = 16
INDEX_DIR_NAME = "index"
CURRENT_POINTER = "CURRENT"
KEEP_INDEXES = 3
DISPLAY_QUOTE_MAX_CP = 300     # 재배포 허락이 없는 소스의 사용자 화면 인용 상한(P3_DESIGN §1). README 에도 적는다.
EVAL_MAX_LIMIT = 50            # 평가 하네스 전용 limit 상한(search_eval). MCP·엔진 경로는 search() 를 쓰므로 MAX_LIMIT 그대로다.
CANDIDATE_CAP = 200            # 단계별 후보 상한
MAX_PHRASES = 24
MAX_GRAMS = 24
_INDEX_RE = re.compile(r"^corpus-idx-[0-9a-f]{16}\.sqlite$")

# ---- 검색 순위 -------------------------------------------------------------------------------------------------------
# G-D32 목록·색인 장 chunk: 사례집 소스의 case 없는 쪽 chunk(색인의 verification_status='not_applicable'). 사건 행을 나열해 분쟁 어휘의
# 빈도가 높으므로 일반 질의(trigram BM25·2글자)에서는 다른 결과 뒤로 강등한다. 사건번호·조문 정확 검색과 본문 속 구문 일치(tier 0·1)는 강등하지 않는다.
LIST_CHUNK_FAMILIES = tuple(sorted(LIST_CHUNK_AUTHORITY))
LIST_KIND = "list"             # passages.chunk_kind 값: list(목록·색인 장 쪽 chunk) | case(구조화 사건 chunk) | text(그 밖의 쪽 단위 본문 chunk)
_LIST_SQL = "(p.chunk_kind='list')"
# 3번 형식 색인에는 chunk_kind 열이 없어 이전 술어(소스 계열 + 검증 상태)로 판정한다.
_LIST_SQL_V3 = "(p.source_family IN (%s) AND p.verification_status='not_applicable')" % ",".join(f"'{f}'" for f in LIST_CHUNK_FAMILIES)
# 2글자 질의어 점수: Σ idf(g)·n(k1+1)/(n + k1) (BM25 의 IDF·빈도 포화, 길이 정규화 없음). 흔한 어휘의 반복이 드문 어휘 일치를 이기지 않게 한다.
# 길이 정규화(b>0)는 짧은 조정 사례 요약을 같은 빈도의 긴 판결보다 앞세울 수 있어 쓰지 않는다(사용자 결정 2026-10-10).
BIGRAM_K1 = 1.2
# 권위 유형 가중(일반 질의 tier 2·3 의 점수에 곱한다. 정확 검색·구문 일치 tier 0·1 에는 쓰지 않는다): 판례 > 심의·조정 사례 > 연구자료.
# 표에 없는 유형(guideline·statute·correction)은 1.0. 목록 장 chunk 는 가중 대신 강등한다. 순위 등급은 rank_class 와 _rank_sql 두 곳이 이 표만 읽는다.
AUTHORITY_WEIGHTS: dict[str, float] = {
    "judgment": 1.10,
    "pac_decision": 1.01, "pac_settlement": 1.01, "pac_election_review": 1.01, "iendc_review": 1.01,
    "research": 1.0,
}
# 질의어 끝의 가/이/도는 조사일 수도 명사의 일부일 수도 있다(기자가·전문가). 색인에 '낱말+조사' 꼴이 이만큼 있으면 명사로 보고 떼지 않는다.
_AMBIGUOUS_JOSA = "가이도"
_NOUN_FOLLOWERS = "의를을에와과는은로도"
NOUN_EVIDENCE_MIN = 3


def is_list_chunk(source_family: str, verification_status: str) -> bool:
    return source_family in LIST_CHUNK_FAMILIES and verification_status == "not_applicable"


def chunk_kind_of(case_id: str | None, source_family: str, verification_status: str) -> str:
    """색인 빌드 때 chunk 종류를 정한다: 사건 chunk=case, 목록 장=list, 그 밖의 쪽 단위 chunk=text."""
    if case_id:
        return "case"
    return LIST_KIND if is_list_chunk(source_family, verification_status) else "text"


def rank_class(authority_type: str, chunk_kind: str) -> tuple[bool, float]:
    """(강등 여부, 가중). 일반 질의 순위의 유일한 등급 규칙."""
    if chunk_kind == LIST_KIND:
        return True, 1.0
    return False, AUTHORITY_WEIGHTS.get(authority_type, 1.0)


def _rank_sql(legacy: bool = False) -> tuple[str, str]:
    """rank_class 의 SQL 꼴: (강등 정렬 머리, 가중 식). 후보 상한 앞 정렬에 쓴다."""
    weight = "CASE p.authority_type " + " ".join(f"WHEN '{a}' THEN {w!r}" for a, w in sorted(AUTHORITY_WEIGHTS.items())) + " ELSE 1.0 END"
    return f"{_LIST_SQL_V3 if legacy else _LIST_SQL}, ", f"({weight})"

# ---- 반대 결과(stance=counter) 표지. 어휘는 고정 상수이고 의미 판단이 아니다(어휘 일치만 본다). -------------------------
IMMUNITY_TAG_MARKERS = (
    "상당한이유", "상당성", "공공의이익", "공익성", "위법성조각", "면책", "순수한의견", "의견표명", "공적인물", "공적관심", "진실성", "진실이라고믿",
)
# 청구 배척 표지: 청구하는 쪽(원고·청구인·신청인)이 진 것으로 문장에 적힌 경우만 신호다. 어느 쪽이 졌는지 없는 처분("상고를 기각한다")이나
# 피고 쪽이 진 처분("피고의 상고를 기각한다"), 일부 승소("원고의 나머지 청구를 기각한다")는 신호가 아니다.
_CLAIMANT_SIDE = ("원고", "청구인", "신청인")
_CLAIMANT_LOSS_TAILS = ("의청구를기각", "의청구를모두기각", "의청구를전부기각", "의청구를배척", "의청구는이유없", "의청구를받아들이지",
                        "의상고를기각", "의항소를기각", "의항고를기각", "의재항고를기각", "패소")
REJECTION_TEXT_MARKERS = tuple(side + tail for side in _CLAIMANT_SIDE for tail in _CLAIMANT_LOSS_TAILS)
_COURT_SPEAKERS = {"court_majority": "majority", "court_concurring": "concurring", "court_dissenting": "dissenting"}
_CIVIL_KINDS = {"다", "가합", "가단", "가소", "나", "라", "머", "카합", "카단", "카기"}
_CRIMINAL_KINDS = {"도", "고합", "고단", "고정", "노", "오", "초", "고약"}
_TAG_CHANNELS = (
    ("정정보도", "correction"), ("반론보도", "reply"), ("추후보도", "follow_up"), ("손해배상", "civil"), ("불법행위", "civil"),
    ("출판물에의한명예훼손", "criminal"), ("명예훼손죄", "criminal"), ("형사", "criminal"), ("초상", "portrait"),
    ("사생활", "privacy"), ("개인정보", "privacy"), ("저작권", "copyright"),
)
_RIGHTS_KEYS = ("redistribute_raw", "redistribute_derivatives", "review_status")

# ---------------------------------------------------------------- 텍스트 도구

_RUN = re.compile(r"[0-9a-z가-힣]+")
_CASE_HEAD = re.compile(r"(\d{2,4}[가-힣]{1,8})(\d{1,8})")
_CASE_SEP = re.compile(r"[,，·‧∙・ㆍ\u119e/、&\s]+")     # NFKC 는 ㆍ(U+318D)를 U+119E 로 바꾼다
_CASE_GAP = re.compile(r"(\d{2,4}[가-힣]{1,8})\s+(?=\d)")   # 사건부호와 번호 사이 공백(``2019다 12345``)
_QUERY_CASE = re.compile(r"\d{2,4}[가-힣]{1,8}\d{1,8}")
_NOT_CASE_KINDS = {"년", "월", "일", "시", "분", "초", "세", "호", "조", "항", "회", "차", "건", "명", "원", "개", "번", "위", "대", "주", "기", "부"}
_CITE = re.compile(r"(?:([가-힣A-Za-z][가-힣A-Za-z·]{0,30}?(?:법률|법|령|규칙|규정|조례))\s*)?제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")
_JOSA3 = ("으로는", "에서는", "에게는", "입니다")
_JOSA2 = ("에서", "으로", "에게", "부터", "까지", "에는", "에도", "이라", "라고", "이다", "보다", "처럼", "마저", "조차", "로서", "로써", "이나", "이며")
_JOSA1 = "은는이가을를의에로와과도만"
_HANGUL_ONLY = re.compile(r"[가-힣]+")
_OC_PARAM = re.compile(r"(?i)[?&;]oc=")


def norm_text(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").lower()


def runs(s: str) -> list[str]:
    return _RUN.findall(norm_text(s))


def bigram_counts(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for run in runs(text):
        for i in range(len(run) - 1):
            g = run[i:i + 2]
            out[g] = out.get(g, 0) + 1
    return out


def case_numbers(raw: str | None) -> list[str]:
    """사건번호 표기를 번호별 정규형 목록으로. ``2023다240763, 240770`` -> [2023다240763, 2023다240770]. 공백은 지운다."""
    if not raw:
        return []
    s = _CASE_GAP.sub(r"\1", unicodedata.normalize("NFKC", raw).strip())
    m = _CASE_HEAD.match(s)
    if not m:
        return []
    prefix = m.group(1)
    out = [prefix + m.group(2)]
    for tok in _CASE_SEP.split(s[m.end():]):
        if re.fullmatch(r"\d{1,8}", tok):
            out.append(prefix + tok)
            continue
        m2 = _CASE_HEAD.fullmatch(tok)
        if m2:
            prefix = m2.group(1)
            out.append(tok)
    return list(dict.fromkeys(out))


def query_case_numbers(q: str) -> list[tuple[str, tuple[int, int]]]:
    out = []
    for m in _QUERY_CASE.finditer(q):
        kind = re.search(r"[가-힣]+", m.group(0)).group(0)
        if kind in _NOT_CASE_KINDS:
            continue
        out.append((m.group(0), m.span()))
    return out


def cites_in(text: str) -> list[str]:
    """조문 표기 정규형. ``언론중재법 제14조`` 는 [``언론중재법 제14조``, ``제14조``], 법 이름이 없으면 [``제14조``]."""
    out: list[str] = []
    for m in _CITE.finditer(unicodedata.normalize("NFKC", text or "")):
        art = f"제{m.group(2)}조" + (f"의{m.group(3)}" if m.group(3) else "")
        if m.group(1):
            out.append(f"{m.group(1)} {art}")
        out.append(art)
    return list(dict.fromkeys(out))


def query_cites(q: str) -> list[tuple[str, tuple[int, int]]]:
    out = []
    for m in _CITE.finditer(q):
        art = f"제{m.group(2)}조" + (f"의{m.group(3)}" if m.group(3) else "")
        out.append((f"{m.group(1)} {art}" if m.group(1) else art, m.span()))
    return out


def strip_josa(w: str) -> str:
    """질의어 끝의 조사를 한 번 뗀다(남는 글자가 2자 이상일 때만). 떼면 항상 원 단어의 부분문자열이라 재현율만 넓어진다."""
    if not _HANGUL_ONLY.fullmatch(w):
        return w
    for group in (_JOSA3, _JOSA2):
        for j in group:
            if w.endswith(j) and len(w) - len(j) >= 2:
                return w[:-len(j)]
    if len(w) >= 3 and w[-1] in _JOSA1:
        return w[:-1]
    return w


def _fts_phrase(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def fts_trigram_available() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        try:
            c.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
            c.execute("INSERT INTO t(x) VALUES ('가나다라')")
            return c.execute("SELECT count(*) FROM t WHERE t MATCH '\"가나다\"'").fetchone()[0] == 1
        finally:
            c.close()
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------- 경로

def index_dir() -> Path:
    return config.data_dir() / INDEX_DIR_NAME


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------- passage 조립(빌드)

def _valid_url(u: Any) -> bool:
    return isinstance(u, str) and u.startswith(("http://", "https://")) and is_uri(u) and not _OC_PARAM.search(u)


def pick_url(locator_kind: str, att_url: Any, canonical_url: Any, detail_url: Any) -> str | None:
    """URL 규칙: pdf_page 는 쪽 번호가 첨부 파일의 쪽이므로 첨부 URL 을 우선하고, 그 밖의 위치는 문서 정본 URL 을 쓴다.

    OC 인증키가 붙는 법제처 DRF 주소(``detail_url``)는 정본 URL 이 없을 때도 ``oc=`` 파라미터가 있으면 쓰지 않는다.
    """
    order = (att_url, canonical_url, detail_url) if locator_kind == "pdf_page" else (canonical_url, att_url, detail_url)
    for u in order:
        if _valid_url(u):
            return u
    return None


def derive_risk_channels(authority_type: str, case_number: str | None, tags: list[str], tag_blob_extra: str = "") -> list[str]:
    out: set[str] = set()
    if authority_type == "pac_election_review":
        out.add("pac_election")
    if authority_type == "iendc_review":
        out.add("iendc_election")
    blob = re.sub(r"\s+", "", " ".join(tags) + " " + tag_blob_extra)
    for marker, ch in _TAG_CHANNELS:
        if marker in blob:
            out.add(ch)
    if authority_type == "judgment":
        for cn in case_numbers(case_number):
            kind = re.search(r"[가-힣]+", cn)
            if kind and kind.group(0) in _CIVIL_KINDS:
                out.add("civil")
            elif kind and kind.group(0) in _CRIMINAL_KINDS:
                out.add("criminal")
    return sorted(c for c in out if c in RISK_CHANNELS)


# CX1-8: 면책 사유가 부정된 태그("공익성 부정", "상당성 인정되지 않음")는 면책 신호가 아니다. 뒤집힌 원심 결과("원고 패소 부분을 파기하고 환송")도 패소 신호가 아니다. 정밀도 우선(G-D24).
IMMUNITY_NEGATION_MARKERS = ("부정", "인정되지않", "인정하지않", "불인정", "인정안", "배척", "부인", "아님", "아니", "없음", "없다", "결여", "불충족", "미달")
REVERSAL_MARKERS = ("파기", "환송")
_SENTENCE_SPLIT = re.compile(r"[.。\n]+")


def counter_signals(outcome_type: str | None, tags: list[str], outcome_text: str | None) -> list[str]:
    sig: list[str] = []
    if outcome_type == "dismissal_merits":
        sig.append("outcome_type:dismissal_merits")
    for t in tags:
        tc = re.sub(r"\s+", "", t)
        if any(n in tc for n in IMMUNITY_NEGATION_MARKERS):
            continue
        for m in IMMUNITY_TAG_MARKERS:
            if m in tc:
                sig.append(f"tag:{t}")
                break
    for sentence in _SENTENCE_SPLIT.split(outcome_text or ""):
        oc = re.sub(r"\s+", "", sentence)
        if any(r in oc for r in REVERSAL_MARKERS):
            continue
        for m in REJECTION_TEXT_MARKERS:
            if m in oc:
                sig.append(f"outcome_text:{m}")
    return list(dict.fromkeys(sig))


def opinion_kinds_of(record: dict) -> list[str]:
    kinds: set[str] = set()
    items: list[Any] = []
    for f in ("factual_background", "article_claims", "disputed_spans", "claims_or_requested_remedy", "legal_reasoning"):
        v = record.get(f)
        if isinstance(v, list):
            items += v
    if isinstance(record.get("outcome"), dict):
        items.append(record["outcome"])
    for it in items:
        if isinstance(it, dict) and it.get("speaker") in _COURT_SPEAKERS:
            kinds.add(_COURT_SPEAKERS[it["speaker"]])
    return sorted(kinds)


def _resolve_rights(rights_doc: dict, rights_src: dict, doc_id: str, family: str | None) -> dict:
    row, scope = rights_doc.get(doc_id), "document"
    if row is None:
        row, scope = rights_src.get(family or ""), "source"
    if row is None:
        return {"scope": "none", "redistribute_raw": "unknown", "redistribute_derivatives": "unknown", "review_status": "unknown"}
    out = {k: row[k] for k in _RIGHTS_KEYS}
    out["scope"] = scope
    return out


def redistributable(rights: dict) -> bool:
    return rights.get("redistribute_raw") == "allowed" and rights.get("redistribute_derivatives") == "allowed"


_LEDGER_SQL = """
SELECT ch.chunk_id, ch.created_at AS ch_created, ch.case_id, ch.page_id, ch.doc_id, ch.text, ch.locator_kind, ch.locator_value,
       ch.authority_type AS ch_authority, ch.decision_date AS ch_date,
       ca.verification_status AS case_status, ca.institution AS case_institution, ca.case_number, ca.decision_date AS case_date,
       ca.authority_type AS case_authority, ca.outcome_type, ca.record_json,
       d.source_id AS family, d.title AS doc_title, d.canonical_url, d.detail_url, d.status AS doc_status, d.publication_date AS doc_publication_date, d.year AS doc_year,
       att.url AS att_url, att.att_id AS att_id_eff
FROM chunks ch
LEFT JOIN cases ca ON ca.case_id = ch.case_id
LEFT JOIN pages pg ON pg.page_id = ch.page_id
LEFT JOIN documents d ON d.doc_id = ch.doc_id
LEFT JOIN attachments att ON att.att_id = COALESCE(ca.att_id, pg.att_id)
ORDER BY ch.chunk_id
"""


def _text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 중복 제거 키에서 빼는 값: 같은 본문을 가진 chunk 마다 당연히 다른 식별·위치 값과 수집 시각. 나머지(본문·제목·사건 키·사건번호·권위·기준일·결과·태그·
# 위험 경로·반대 표지·의견 구분·검증 상태·chunk 종류·권리·조문)가 하나라도 다르면 접지 않는다(F1 검색 계약, F2 별칭 조회의 자기 메타데이터·권리).
# 사건 키(case_key)를 넣는 이유: 다른 사건의 chunk 를 접으면 사건 수 통계(cases_by_authority_type, G-D22·core_coverage 게이트)가 줄어든다.
_DEDUP_IDENTITY = frozenset({"_created", "passage_id", "source_id", "url", "locator_kind", "locator_value"})


def _dedup_key(p: dict) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in p.items() if k not in _DEDUP_IDENTITY})).hexdigest()


def _iter_passages(conn: sqlite3.Connection, rights_doc: dict, rights_src: dict, excluded: dict[str, int],
                   check: Callable[[], None] | None, aliases: list[dict] | None = None) -> Iterator[dict]:
    """색인에 넣을 passage. 본문과 검색·공개 메타데이터가 모두 같은 chunk(``_dedup_key``)는 제외 필터를 통과한 것 중 가장 먼저 들어온 하나
    (min(created_at, chunk_id))만 남긴다. created_at 은 chunk 를 다시 쓸 때 갱신되므로 최초 수집 시각이 아니라 마지막 기록 시각이다. 제거된 chunk 는
    ``aliases`` 에 자기 값(출처·url·위치·권위·기준일)과 남긴 passage_id 로 모으고 ``excluded['duplicate_text']`` 에 센다. 나머지 메타데이터는 키가 같으므로
    남긴 passage 의 값이 곧 자기 값이다. 사전 훑기 1회가 더 든다."""
    winner: dict[str, tuple[str, str]] = {}
    for p in _iter_eligible(conn, rights_doc, rights_src, {}, check):
        k, o = _dedup_key(p), (p.pop("_created"), p["passage_id"])
        if k not in winner or o < winner[k]:
            winner[k] = o
    for p in _iter_eligible(conn, rights_doc, rights_src, excluded, check):
        k, o = _dedup_key(p), (p.pop("_created"), p["passage_id"])
        if o != winner[k]:
            excluded["duplicate_text"] = excluded.get("duplicate_text", 0) + 1
            if aliases is not None:
                aliases.append({"alias_id": p["passage_id"], "passage_id": winner[k][1], "source_id": p["source_id"], "source_family": p["source_family"],
                                "url": p["url"], "locator_kind": p["locator_kind"], "locator_value": p["locator_value"],
                                "authority_type": p["authority_type"], "decision_date": p["decision_date"], "reference_date": p["reference_date"],
                                "reference_date_basis": p["reference_date_basis"], "case_number": p["case_number"], "institution": p["institution"]})
            continue
        yield p


def _iter_eligible(conn: sqlite3.Connection, rights_doc: dict, rights_src: dict, excluded: dict[str, int],
                   check: Callable[[], None] | None) -> Iterator[dict]:
    # G-D33: 첨부 단위 인입 기준에서 제외된 첨부(failures stage='admission' 미해결)의 사건은 격리 사건처럼 뺀다.
    adm_excluded = {r[0] for r in conn.execute(
        "SELECT target FROM failures WHERE stage='admission' AND error_class='attachment_excluded' AND resolved_at IS NULL")}
    cur = conn.execute(_LEDGER_SQL)
    cur.row_factory = sqlite3.Row
    for i, r in enumerate(cur):
        if check is not None and i % 500 == 0:
            check()
        if r["case_status"] in ("quarantined", "rejected"):
            excluded["case_" + r["case_status"]] = excluded.get("case_" + r["case_status"], 0) + 1
            continue
        if r["case_id"] and r["att_id_eff"] in adm_excluded:     # G-D33: 사건 없는 쪽 chunk(case_id 없음)는 이 제외의 대상이 아니다(P2_DESIGN)
            excluded["case_att_excluded"] = excluded.get("case_att_excluded", 0) + 1
            continue
        if r["doc_status"] in ("quarantined", "withheld"):
            excluded["doc_" + r["doc_status"]] = excluded.get("doc_" + r["doc_status"], 0) + 1
            continue
        authority = r["ch_authority"] or r["case_authority"]
        if authority not in AUTHORITY_TYPES:
            excluded["unknown_authority_type"] = excluded.get("unknown_authority_type", 0) + 1
            continue
        text = r["text"] or ""
        if not text.strip():
            excluded["empty_text"] = excluded.get("empty_text", 0) + 1
            continue
        url = pick_url(r["locator_kind"], r["att_url"], r["canonical_url"], r["detail_url"])
        if url is None:
            excluded["no_valid_url"] = excluded.get("no_valid_url", 0) + 1
            continue
        date = next((d for d in (r["ch_date"], r["case_date"]) if isinstance(d, str) and is_date(d)), None)
        try:
            record = json.loads(r["record_json"]) if r["record_json"] else {}
        except ValueError:
            record = {}
        # G-D23: 결정일이 없으면 수록 간행물의 발행일(문서 장부 값)을 기준일로 쓴다. 사건 기록의 publication_date 는 심의 대상 기사의 날짜라서 쓰지 않는다
        # (그 날짜를 쓰면 기준일 이후에 결정된 자료가 as_of 필터와 보고서 검증을 통과한다, V-consistency-02).
        pub_date = r["doc_publication_date"] if isinstance(r["doc_publication_date"], str) and is_date(r["doc_publication_date"]) else None
        # G-D25: 날짜는 없고 연도만 아는 간행물(장부 year, 제목 머리의 연도, 선거백서의 회차 대응표)은 그해 12월 31일을 기준일 상한으로 쓴다.
        ref = reference_date(date, pub_date, pubyear.document_year(r["family"], r["doc_title"], r["doc_year"]))
        tags = [t for t in (record.get("issue_tags") or []) if isinstance(t, str)]
        outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}
        kinds = opinion_kinds_of(record) if authority == "judgment" else []
        provisions: list[str] = []
        for lp in record.get("legal_provisions") or []:
            if isinstance(lp, dict) and isinstance(lp.get("citation"), str):
                provisions += cites_in(lp["citation"])
        yield {
            "_created": r["ch_created"] or "",
            "passage_id": r["chunk_id"],
            "source_id": r["doc_id"],
            "source_family": r["family"] or "",
            "url": url,
            "locator_kind": r["locator_kind"],
            "locator_value": r["locator_value"],
            "authority_type": authority,
            "decision_date": date,
            "reference_date": ref["reference_date"],
            "reference_date_basis": ref["reference_date_basis"],
            "case_key": r["case_id"] or r["doc_id"],
            "institution": r["case_institution"],
            "case_number": r["case_number"],
            "outcome_type": r["outcome_type"],
            "issue_tags": tags,
            "title": r["doc_title"] or "",
            "text": text,
            "verification_status": r["case_status"] or "not_applicable",
            "chunk_kind": chunk_kind_of(r["case_id"], r["family"] or "", r["case_status"] or "not_applicable"),
            "opinion_kinds": kinds,
            "provisions": sorted(set(provisions)),
            "counter_signals": counter_signals(r["outcome_type"], tags, outcome.get("text")),
            "risk_channels": derive_risk_channels(authority, r["case_number"], tags),
            "rights": _resolve_rights(rights_doc, rights_src, r["doc_id"], r["family"]),
        }


_INDEX_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE passages(
  rid INTEGER PRIMARY KEY, passage_id TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL, source_family TEXT NOT NULL, url TEXT NOT NULL,
  locator_kind TEXT NOT NULL, locator_value TEXT NOT NULL, authority_type TEXT NOT NULL, decision_date TEXT,
  reference_date TEXT, reference_date_basis TEXT, institution TEXT,
  case_number TEXT, outcome_type TEXT, text TEXT NOT NULL, issue_tags TEXT NOT NULL, risk_channels TEXT NOT NULL, rights_json TEXT NOT NULL,
  verification_status TEXT NOT NULL, opinion_kinds TEXT NOT NULL, counter_signals TEXT NOT NULL, redistributable INTEGER NOT NULL,
  is_fixture INTEGER NOT NULL DEFAULT 0, chunk_kind TEXT NOT NULL DEFAULT 'text');
CREATE TABLE passage_alias(
  alias_id TEXT NOT NULL, source_id TEXT NOT NULL, passage_id TEXT NOT NULL, source_family TEXT NOT NULL, url TEXT NOT NULL,
  locator_kind TEXT NOT NULL, locator_value TEXT NOT NULL, authority_type TEXT NOT NULL, decision_date TEXT, reference_date TEXT,
  reference_date_basis TEXT, case_number TEXT, institution TEXT, PRIMARY KEY(alias_id, source_id)) WITHOUT ROWID;
CREATE INDEX passages_authority ON passages(authority_type);
CREATE INDEX passages_date ON passages(reference_date);
CREATE INDEX passages_institution ON passages(institution);
CREATE TABLE passage_risk(rid INTEGER NOT NULL, channel TEXT NOT NULL, PRIMARY KEY(channel, rid)) WITHOUT ROWID;
CREATE TABLE case_numbers(norm TEXT NOT NULL, rid INTEGER NOT NULL, PRIMARY KEY(norm, rid)) WITHOUT ROWID;
CREATE TABLE cites(cite TEXT NOT NULL, rid INTEGER NOT NULL, PRIMARY KEY(cite, rid)) WITHOUT ROWID;
CREATE TABLE bigram_terms(gram TEXT NOT NULL, rid INTEGER NOT NULL, n INTEGER NOT NULL, PRIMARY KEY(gram, rid)) WITHOUT ROWID;
"""
_FTS_SCHEMA = "CREATE VIRTUAL TABLE passages_fts USING fts5(text, tags, tokenize='trigram');"


_ALIAS_COLS = ("alias_id", "source_id", "passage_id", "source_family", "url", "locator_kind", "locator_value", "authority_type", "decision_date",
               "reference_date", "reference_date_basis", "case_number", "institution")


def _write_index(path: Path, passages: Iterator[dict], meta: dict[str, str], use_fts: bool, check: Callable[[], None] | None,
                 aliases: list[dict] | None = None) -> int:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
        conn.executescript(_INDEX_SCHEMA)
        if use_fts:
            conn.executescript(_FTS_SCHEMA)
        n = 0
        with conn:
            for p in passages:
                n += 1
                if check is not None and n % 500 == 0:
                    check()
                rid = n
                red = 1 if redistributable(p["rights"]) else 0
                conn.execute(
                    "INSERT INTO passages(rid, passage_id, source_id, source_family, url, locator_kind, locator_value, authority_type, "
                    "decision_date, reference_date, reference_date_basis, institution, case_number, outcome_type, text, issue_tags, risk_channels, "
                    "rights_json, verification_status, opinion_kinds, counter_signals, redistributable, chunk_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, p["passage_id"], p["source_id"], p["source_family"], p["url"], p["locator_kind"], p["locator_value"],
                     p["authority_type"], p["decision_date"], p["reference_date"], p["reference_date_basis"], p["institution"], p["case_number"],
                     p["outcome_type"], p["text"],
                     json.dumps(p["issue_tags"], ensure_ascii=False), json.dumps(p["risk_channels"]), json.dumps(p["rights"], sort_keys=True),
                     p["verification_status"], json.dumps(p["opinion_kinds"]), json.dumps(p["counter_signals"], ensure_ascii=False), red, p["chunk_kind"]))
                conn.executemany("INSERT INTO passage_risk(rid, channel) VALUES (?,?)", [(rid, c) for c in p["risk_channels"]])
                cns = case_numbers(p["case_number"])
                conn.executemany("INSERT OR IGNORE INTO case_numbers(norm, rid) VALUES (?,?)", [(c, rid) for c in cns])
                cites = list(dict.fromkeys(cites_in(p["text"]) + [c for t in p["issue_tags"] for c in cites_in(t)] + p["provisions"]))
                conn.executemany("INSERT OR IGNORE INTO cites(cite, rid) VALUES (?,?)", [(c, rid) for c in cites])
                tags_blob = " ".join(p["issue_tags"] + [p["title"]] + cns)
                conn.executemany("INSERT INTO bigram_terms(gram, rid, n) VALUES (?,?,?)",
                                 [(g, rid, c) for g, c in sorted(bigram_counts(p["text"] + " " + tags_blob).items())])
                if use_fts:
                    conn.execute("INSERT INTO passages_fts(rowid, text, tags) VALUES (?,?,?)", (rid, norm_text(p["text"]), norm_text(tags_blob)))
            conn.executemany("INSERT INTO passage_alias(%s) VALUES (%s)" % (",".join(_ALIAS_COLS), ",".join("?" * len(_ALIAS_COLS))),
                             [tuple(a[c] for c in _ALIAS_COLS) for a in (aliases or ())])
            conn.executemany("INSERT INTO meta(key, value) VALUES (?,?)", sorted(meta.items()))
        return n
    finally:
        conn.close()


def _read_rights(conn: sqlite3.Connection) -> tuple[dict, dict]:
    doc, src = {}, {}
    try:
        cur = conn.execute("SELECT scope_kind, scope_id, redistribute_raw, redistribute_derivatives, review_status FROM rights")
    except sqlite3.Error:
        return doc, src
    for k, sid, rr, rd, rs in cur:
        (doc if k == "document" else src)[sid] = {"redistribute_raw": rr, "redistribute_derivatives": rd, "review_status": rs}
    return doc, src


def build_index(manifest_path: str | Path, out_dir: str | Path, *, now: _dt.datetime | None = None,
                check: Callable[[], None] | None = None, keep: int = KEEP_INDEXES, dry_run: bool = False) -> dict:
    """수집 장부에서 색인을 만든다. 돌려주는 dict 는 건수·경로·상태뿐이다(원문 없음).

    status: ``built`` | ``unchanged``(같은 snapshot 이 이미 있음) | ``dry_run`` | ``no_passages``(넣을 chunk 없음, 아무것도 발행하지 않음).
    """
    mp, od = Path(manifest_path), Path(out_dir)
    if not mp.exists():
        raise CorpusError("ledger not found")
    now = now or _dt.datetime.now(_dt.timezone.utc)
    try:
        conn = sqlite3.connect(mp.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise CorpusError(f"cannot open ledger read-only: {type(e).__name__}") from None
    try:
        conn.execute("BEGIN")        # 한 읽기 트랜잭션: 두 번 훑는 동안 같은 장부 상태를 본다
        try:
            conn.execute("SELECT 1 FROM chunks LIMIT 1")
        except sqlite3.Error:
            raise CorpusError("ledger has no chunks table (run the P2 stages first)") from None
        rights_doc, rights_src = _read_rights(conn)

        # 1차 훑기: snapshot 해시와 통계
        excluded: dict[str, int] = {}
        h = hashlib.sha256()
        h.update(INDEX_FORMAT.encode("utf-8") + b"\n")
        by_auth: dict[str, int] = {}
        cases_by_auth: dict[str, set] = {}
        date_unknown = 0
        by_family: dict[str, int] = {}
        docs_with: dict[str, set] = {}
        by_status: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        dates: list[str] = []
        aliases: list[dict] = []
        n = 0
        for p in _iter_passages(conn, rights_doc, rights_src, excluded, check, aliases):
            n += 1
            h.update(_canonical(p) + b"\n")
            by_auth[p["authority_type"]] = by_auth.get(p["authority_type"], 0) + 1
            cases_by_auth.setdefault(p["authority_type"], set()).add(p["case_key"])
            if p["reference_date"] is None:
                date_unknown += 1
            by_family[p["source_family"]] = by_family.get(p["source_family"], 0) + 1
            docs_with.setdefault(p["source_family"], set()).add(p["source_id"])
            by_status[p["verification_status"]] = by_status.get(p["verification_status"], 0) + 1
            by_kind[p["chunk_kind"]] = by_kind.get(p["chunk_kind"], 0) + 1
            if p["decision_date"]:
                dates.append(p["decision_date"])
        for a in aliases:                                  # 별칭도 snapshot 정본에 든다(옛 id 해석이 달라지면 snapshot 이 달라진다)
            h.update(b"alias\n" + _canonical(a) + b"\n")
        snapshot = SNAPSHOT_PREFIX + h.hexdigest()[:SNAPSHOT_HEX_LEN]
        base = {"snapshot_id": snapshot, "passages": n, "excluded": dict(sorted(excluded.items())),
                "by_authority_type": dict(sorted(by_auth.items())), "by_verification_status": dict(sorted(by_status.items())),
                "by_chunk_kind": dict(sorted(by_kind.items())), "aliases": len(aliases),
                "cases_by_authority_type": {k: len(v) for k, v in sorted(cases_by_auth.items())}, "date_unknown_passages": date_unknown,
                "latest_decision_date": max(dates) if dates else None, "index_dir": str(od)}
        if n == 0:
            return {"status": "no_passages", **base}
        target = od / f"corpus-{snapshot}.sqlite"
        if dry_run:
            return {"status": "dry_run", "path": str(target), **base}
        if target.exists() and _snapshot_of(target) == snapshot:
            _write_pointer(od, target.name)
            return {"status": "unchanged", "path": str(target), **base}

        # 장부 커버리지(색인 상태 설명용)
        doc_counts: dict[str, dict[str, int]] = {}
        for fam, st, c in conn.execute("SELECT source_id, status, COUNT(*) FROM documents GROUP BY source_id, status"):
            doc_counts.setdefault(fam, {})[st] = c
        sources = {}
        for fam in sorted(set(doc_counts) | set(by_family)):
            rr = rights_src.get(fam, {})
            sources[fam] = {"documents": sum(doc_counts.get(fam, {}).values()), "documents_with_passages": len(docs_with.get(fam, ())),
                            "passages": by_family.get(fam, 0), "documents_by_status": dict(sorted(doc_counts.get(fam, {}).items())),
                            "redistribute_raw": rr.get("redistribute_raw", "unknown"),
                            "redistribute_derivatives": rr.get("redistribute_derivatives", "unknown")}
        use_fts = fts_trigram_available()
        meta = {
            "format": INDEX_FORMAT, "snapshot_id": snapshot, "built_at": now.isoformat(timespec="seconds"),
            "passage_count": str(n), "fts_trigram": "1" if use_fts else "0",
            "latest_decision_date": max(dates) if dates else "", "earliest_decision_date": min(dates) if dates else "",
            "display_limit_chars": str(DISPLAY_QUOTE_MAX_CP),
            "stats_json": json.dumps({"by_authority_type": base["by_authority_type"], "by_verification_status": base["by_verification_status"],
                                      "by_chunk_kind": base["by_chunk_kind"], "aliases": len(aliases),
                                      "cases_by_authority_type": base["cases_by_authority_type"], "date_unknown_passages": date_unknown,
                                      "by_source_family": dict(sorted(by_family.items())), "sources": sources,
                                      "excluded": base["excluded"]}, ensure_ascii=False, sort_keys=True),
        }
        config.private_mkdir(od)
        tmp = od / f"{target.name}.tmp-{os.getpid()}"
        try:
            tmp.unlink(missing_ok=True)
            aliases2: list[dict] = []
            written = _write_index(tmp, _iter_passages(conn, rights_doc, rights_src, {}, check, aliases2), meta, use_fts, check, aliases2)
            if written != n or len(aliases2) != len(aliases):
                raise CorpusError("ledger changed during the build")
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        _write_pointer(od, target.name)
        pruned = _prune(od, keep, target.name)
        return {"status": "built", "path": str(target), "fts_trigram": use_fts, "pruned": pruned, **base}
    finally:
        conn.close()


def _snapshot_of(path: Path) -> str | None:
    try:
        c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            r = c.execute("SELECT value FROM meta WHERE key='snapshot_id'").fetchone()
            return r[0] if r else None
        finally:
            c.close()
    except sqlite3.Error:
        return None


def _write_pointer(od: Path, name: str) -> None:
    tmp = od / f"{CURRENT_POINTER}.tmp-{os.getpid()}"
    try:
        tmp.write_text(name + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, od / CURRENT_POINTER)
    finally:
        tmp.unlink(missing_ok=True)


def _prune(od: Path, keep: int, current_name: str) -> int:
    files = sorted((p for p in od.iterdir() if _INDEX_RE.match(p.name)), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    n = 0
    for p in files[max(1, keep):]:
        if p.name == current_name:
            continue
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def open_current(od: str | Path | None = None) -> "SqliteCorpus | None":
    """``CURRENT`` 포인터가 가리키는 색인을 연다. 색인이 아예 없으면 None, 있는데 열 수 없으면 CorpusError."""
    d = Path(od) if od is not None else index_dir()
    ptr = d / CURRENT_POINTER
    if ptr.is_file():
        try:
            name = ptr.read_text(encoding="utf-8").strip()
        except OSError:
            raise CorpusError("index pointer is unreadable") from None
        if not _INDEX_RE.match(name):
            raise CorpusError("index pointer is malformed")
        if not (d / name).is_file():
            raise CorpusError("index pointer names a missing file")
        return SqliteCorpus(d / name)
    if d.is_dir():
        cands = sorted((p for p in d.iterdir() if _INDEX_RE.match(p.name)), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        if cands:
            return SqliteCorpus(cands[0])
    return None


# ---------------------------------------------------------------- 검색

_EXTRA_KEYS = ("case_number", "institution", "outcome_type", "issue_tags", "risk_channels", "opinion_kinds", "opinion_kind",
               "includes_dissent", "counter_signals", "verification_status", "source_family", "redistributable",
               "display_text", "display_truncated", "display_limit_chars")


class SqliteCorpus(Corpus):
    def __init__(self, path: str | Path):
        self._path = Path(path)
        try:
            self._db = sqlite3.connect(self._path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            meta = {r["key"]: r["value"] for r in self._db.execute("SELECT key, value FROM meta")}
        except sqlite3.Error as e:
            raise CorpusError(f"cannot open index: {type(e).__name__}") from None
        if meta.get("format") not in READABLE_FORMATS:
            raise CorpusError("index format is not supported (rebuild it with the build_index command)")
        self._meta = meta
        self._legacy = meta.get("format") == "pdr-index-3"      # chunk_kind 열·passage_alias 표 없음
        self._snapshot = meta["snapshot_id"]
        self._index_has_fts = meta.get("fts_trigram") == "1"
        self._fts = self._index_has_fts and fts_trigram_available()
        try:
            self._stats = json.loads(meta.get("stats_json", "{}"))
        except ValueError:
            self._stats = {}

    # -- 인터페이스 --------------------------------------------------------------
    @property
    def snapshot_id(self) -> str:
        return self._snapshot

    @property
    def is_fixture(self) -> bool:
        return False

    def close(self) -> None:
        self._db.close()

    def _public(self, r: sqlite3.Row) -> dict:
        text = r["text"]
        red = bool(r["redistributable"])
        truncated = (not red) and len(text) > DISPLAY_QUOTE_MAX_CP
        kinds = json.loads(r["opinion_kinds"])
        return {
            "source_id": r["source_id"], "passage_id": r["passage_id"], "url": r["url"],
            "locator": {"kind": r["locator_kind"], "value": r["locator_value"]},
            "authority_type": r["authority_type"], "decision_date": r["decision_date"], "text": text, "is_fixture": False,
            "reference_date": r["reference_date"], "reference_date_basis": r["reference_date_basis"], "date_unknown": r["reference_date"] is None,
            "case_number": r["case_number"], "institution": r["institution"], "outcome_type": r["outcome_type"],
            "issue_tags": json.loads(r["issue_tags"]), "risk_channels": json.loads(r["risk_channels"]),
            "opinion_kinds": kinds, "opinion_kind": (kinds[0] if len(kinds) == 1 else "mixed" if kinds else None),
            "includes_dissent": "dissenting" in kinds,
            "counter_signals": json.loads(r["counter_signals"]), "verification_status": r["verification_status"],
            "source_family": r["source_family"], "redistributable": red,
            "chunk_kind": self._kind(r),
            "display_text": text if red else text[:DISPLAY_QUOTE_MAX_CP], "display_truncated": truncated,
            "display_limit_chars": None if red else DISPLAY_QUOTE_MAX_CP,
        }

    def _kind(self, r: sqlite3.Row) -> str:
        if not self._legacy:
            return r["chunk_kind"]
        return "list" if is_list_chunk(r["source_family"], r["verification_status"]) else "text"      # 3번 형식: case/text 구분 없이 목록 장만 가른다

    def get_passage(self, source_id: str, passage_id: str) -> dict | None:
        if not isinstance(source_id, str) or not isinstance(passage_id, str):
            return None
        r = self._db.execute("SELECT * FROM passages WHERE passage_id=? AND source_id=?", (passage_id, source_id)).fetchone()
        if r:
            return self._public(r)
        if self._legacy:
            return None
        # 같은 본문 중복으로 색인에서 빠진 옛 passage_id: 정본 본문에 옛 passage 자신의 출처·url·위치를 붙여 돌려준다(증거 대조가 그대로 통과한다).
        # 별칭은 본문과 검색·공개 메타데이터(권리 포함)가 정본과 모두 같을 때만 만들어지므로 결과·태그·검증·권리·표시 상한은 정본 값이 곧 자기 값이다(_dedup_key).
        a = self._db.execute("SELECT * FROM passage_alias WHERE alias_id=? AND source_id=?", (passage_id, source_id)).fetchone()
        r = self._db.execute("SELECT * FROM passages WHERE passage_id=?", (a["passage_id"],)).fetchone() if a else None
        if r is None:
            return None
        d = self._public(r)
        d.update({"source_id": a["source_id"], "passage_id": a["alias_id"], "url": a["url"], "locator": {"kind": a["locator_kind"], "value": a["locator_value"]},
                  "authority_type": a["authority_type"], "decision_date": a["decision_date"], "reference_date": a["reference_date"],
                  "reference_date_basis": a["reference_date_basis"], "date_unknown": a["reference_date"] is None, "case_number": a["case_number"],
                  "institution": a["institution"], "source_family": a["source_family"], "alias_of": a["passage_id"]})
        return d

    def _filters(self, authority_types, risk_channel, as_of, institution) -> tuple[str, list]:
        sql, params = "", []
        if authority_types is not None:
            if not authority_types:
                return " AND 0", []
            sql += f" AND p.authority_type IN ({','.join('?' * len(authority_types))})"
            params += list(authority_types)
        if risk_channel is not None:
            sql += " AND EXISTS (SELECT 1 FROM passage_risk k WHERE k.rid=p.rid AND k.channel=?)"
            params.append(risk_channel)
        if as_of is not None:
            sql += " AND (p.reference_date IS NULL OR p.reference_date <= ?)"      # 기준일 없음(date_unknown)은 필터에서 빼고 남긴다
            params.append(as_of)
        if institution is not None:
            sql += " AND p.institution = ?"
            params.append(institution)
        return sql, params

    def search(self, query, *, authority_types=None, risk_channel=None, as_of=None, limit=5, institution=None, stance="both"):
        return self._search(query, authority_types, risk_channel, as_of, limit, institution, stance, MAX_LIMIT)

    def search_eval(self, query, *, authority_types=None, risk_channel=None, as_of=None, limit=20, institution=None, stance="both"):
        """평가 전용: search() 와 같은 검색을 EVAL_MAX_LIMIT 까지 허용한다(P4_DESIGN §5.3 상위 20 풀링). MCP·skill 경로는 이 메서드를 부르지 않는다."""
        return self._search(query, authority_types, risk_channel, as_of, limit, institution, stance, EVAL_MAX_LIMIT)

    def _search(self, query, authority_types, risk_channel, as_of, limit, institution, stance, cap):
        n = _check_search_args(query, authority_types, risk_channel, as_of, limit, institution, stance, cap)
        q = unicodedata.normalize("NFKC", query[:MAX_QUERY_CP])
        fsql, fparams = self._filters(authority_types, risk_channel, as_of, institution)

        case_qs = query_case_numbers(q)
        cite_qs = query_cites(q)
        spans = sorted([s for _, s in case_qs] + [s for _, s in cite_qs])
        rest, pos = [], 0
        for a, b in spans:
            rest.append(q[pos:a])
            pos = max(pos, b)
        rest.append(q[pos:])
        words = list(dict.fromkeys(runs(" ".join(rest))))

        phrases: list[str] = []
        grams: list[str] = []
        for w in words:
            v = self._stem(w)
            if len(v) >= 3 and self._fts:
                phrases.append(v)
            elif len(v) >= 3:
                grams += [v[i:i + 2] for i in range(len(v) - 1)]
            elif len(v) == 2:
                grams.append(v)
        phrases = list(dict.fromkeys(phrases))[:MAX_PHRASES]
        grams = list(dict.fromkeys(grams))[:MAX_GRAMS]

        cand: dict[int, dict] = {}
        spare: list[tuple[int, float]] = []     # 2글자 단계에서 자리가 없어 넣지 않은 후보(점수 순). 같은 본문 접기로 결과가 모자랄 때만 쓴다
        # CX1-9: counter 는 단계별 후보 상한(CANDIDATE_CAP) 안에서 표지 있는 passage 가 먼저 뽑히게 SQL 정렬 맨 앞에 둔다(자른 뒤 재정렬하면 표지 있는 후보가 상한 밖에 남는다)
        cpre = "(p.counter_signals='[]'), " if stance == "counter" else ""

        def put(rid: int, tier: float, label: str, bm: float = 0.0, bg: float = 0.0) -> None:
            c = cand.get(rid)
            if c is None:
                cand[rid] = {"tier": tier, "label": label, "bm": bm, "bg": bg}
                return
            if tier < c["tier"]:
                c["tier"], c["label"] = tier, label
            c["bm"] = max(c["bm"], bm)
            c["bg"] = max(c["bg"], bg)

        # 1) 사건번호·조문 정확 검색
        for cn, _ in case_qs:
            for (rid,) in self._db.execute(
                    "SELECT p.rid FROM case_numbers c JOIN passages p ON p.rid=c.rid WHERE c.norm=?" + fsql + f" ORDER BY {cpre}p.passage_id LIMIT ?",
                    [cn, *fparams, CANDIDATE_CAP]):
                put(rid, 0, "case_number")
        for ct, _ in cite_qs:
            tier = 0 if " " in ct else 1
            for (rid,) in self._db.execute(
                    "SELECT p.rid FROM cites c JOIN passages p ON p.rid=c.rid WHERE c.cite=?" + fsql + f" ORDER BY {cpre}p.passage_id LIMIT ?",
                    [ct, *fparams, CANDIDATE_CAP]):
                put(rid, tier, "citation")
        # 2) 본문 속 패턴(trigram 구문)
        if self._fts and (case_qs or cite_qs):
            pat = " OR ".join(_fts_phrase(norm_text(x)) for x, _ in case_qs + cite_qs)
            for rid, _s in self._fts_query(pat, fsql, fparams, cpre):
                put(rid, 1, "phrase")
        # 3) trigram BM25. 순위 등급(목록 장 강등·권위 가중)은 후보 상한(CANDIDATE_CAP) 앞 정렬에도 건다(자른 뒤 재정렬하면 상한 밖 후보를 못 올린다)
        lpre, wsql = _rank_sql(self._legacy)
        if self._fts and phrases:
            hits = self._fts_query(" OR ".join(_fts_phrase(p) for p in phrases), fsql, fparams, cpre + lpre, wsql)
            top = max((s for _, s in hits), default=0.0) or 1.0
            for rid, s in hits:
                put(rid, 2, "text", bm=s / top)
        # 4) bigram 보완. 앞 단계 후보는 전역 상한과 무관하게 모두 2글자 점수를 받는다(전역 상위 200개와 우연히 겹친 후보만 가점을 받지 않게)
        if grams:
            # counter 는 뒤에서 재정렬하므로 n 에서 자르면 표지 있는 후보가 상한 밖에 남는다: 후보 상한까지 채운다
            fill = CANDIDATE_CAP if stance == "counter" else max(0, n - len(cand))
            rows = self._bigram_scores(grams, fsql, fparams, cpre + lpre, weight=wsql) if fill > 0 else []     # 채울 자리가 없으면 전역 상위는 필요 없다
            have = self._bigram_scores(grams, fsql, fparams, "", rids=list(cand)) if cand else []
            top = max([r["s"] for r in rows] + [r["s"] for r in have], default=0.0) or 1.0
            for r in have:
                cand[r["rid"]]["bg"] = max(cand[r["rid"]]["bg"], r["s"] / top)
            for r in rows:
                if r["rid"] in cand:
                    cand[r["rid"]]["bg"] = max(cand[r["rid"]]["bg"], r["s"] / top)
                elif fill > 0:
                    put(r["rid"], 3, "bigram", bg=r["s"] / top)
                    fill -= 1
                else:
                    spare.append((r["rid"], r["s"] / top))
        if not cand:
            return []

        rows_by_rid: dict[int, sqlite3.Row] = {}

        def load(rids: list[int]) -> None:
            for i in range(0, len(rids), 500):
                part = rids[i:i + 500]
                for r in self._db.execute(f"SELECT * FROM passages WHERE rid IN ({','.join('?' * len(part))})", part):
                    rows_by_rid[r["rid"]] = r
        load(list(cand))

        def ctier(r: sqlite3.Row) -> int:
            if stance != "counter":
                return 0
            return 0 if json.loads(r["counter_signals"]) else 1             # 표지 없는 판결을 표지 없는 결정보다 앞세우지 않는다

        def ranked(rid: int) -> tuple[bool, float]:     # 순위 등급(강등·가중)은 정확 검색·구문 일치(tier 0·1)가 아닐 때만 쓴다
            r, c = rows_by_rid[rid], cand[rid]
            score = c["bm"] + 0.5 * c["bg"]
            if c["tier"] < 2:
                return False, -score
            demote, weight = rank_class(r["authority_type"], self._kind(r))
            return demote, -weight * score

        while True:
            keys = {rid: ranked(rid) for rid in cand}
            order = sorted(cand, key=lambda rid: (ctier(rows_by_rid[rid]), keys[rid][0], cand[rid]["tier"], keys[rid][1], rows_by_rid[rid]["passage_id"]))
            if self._legacy:                       # 3번 형식 색인은 이전 동작 그대로(되돌리기 비교용): 접지 않는다
                picked = order[:n]
                break
            # 본문만 같고 메타데이터가 다른 passage 는 따로 색인되어 필터·정확 검색에 걸린다. 결과에서는 순위가 앞선 하나만 남긴다
            seen: set[str] = set()
            picked = []
            for rid in order:
                t = rows_by_rid[rid]["text"]
                if t in seen:
                    continue
                seen.add(t)
                picked.append(rid)
                if len(picked) == n:
                    break
            if len(picked) == n or not spare:
                break
            more, spare = spare[:n - len(picked)], spare[n - len(picked):]
            for rid, bg in more:
                put(rid, 3, "bigram", bg=bg)
            load([rid for rid, _ in more])
        out = []
        for rid in picked:
            d = self._public(rows_by_rid[rid])
            d["match"] = cand[rid]["label"]
            out.append(d)
        return out

    def _fts_query(self, match: str, fsql: str, fparams: list, cpre: str = "", weight: str = "1") -> list[tuple[int, float]]:
        try:
            cur = self._db.execute(
                "SELECT p.rid AS rid, -bm25(passages_fts, 1.0, 3.0) AS s FROM passages_fts JOIN passages p ON p.rid=passages_fts.rowid "
                "WHERE passages_fts MATCH ?" + fsql + f" ORDER BY {cpre}-bm25(passages_fts, 1.0, 3.0) * {weight} DESC, p.passage_id LIMIT ?",
                [match, *fparams, CANDIDATE_CAP])
            return [(r["rid"], float(r["s"])) for r in cur]
        except sqlite3.OperationalError:
            return []

    def _stem(self, w: str) -> str:
        """strip_josa 에 색인 근거를 더한다. 끝의 가/이/도를 뗀 경우, 색인에 '낱말+조사'(전문가의·반론보도를)가 NOUN_EVIDENCE_MIN 건 이상이면
        명사의 일부로 보고 떼지 않는다. '기자가를'처럼 조사 뒤에 조사가 붙은 꼴은 없으므로 조사 꼴(기자가)은 그대로 뗀다. trigram 이 없으면 strip_josa 그대로."""
        v = strip_josa(w)
        if v == w or not self._fts or len(w) - len(v) != 1 or w[-1] not in _AMBIGUOUS_JOSA:
            return v
        match = " OR ".join(_fts_phrase(w + j) for j in _NOUN_FOLLOWERS)
        try:
            k = self._db.execute("SELECT count(*) FROM (SELECT 1 FROM passages_fts WHERE passages_fts MATCH ? LIMIT ?)",
                                 (match, NOUN_EVIDENCE_MIN)).fetchone()[0]
        except sqlite3.OperationalError:
            return v
        return w if k >= NOUN_EVIDENCE_MIN else v

    def _passage_total(self) -> int:
        if getattr(self, "_total", None) is None:
            self._total = int(self._db.execute("SELECT count(*) FROM passages").fetchone()[0])
        return self._total

    def _bigram_scores(self, grams: list[str], fsql: str, fparams: list, order_pre: str, rids: list[int] | None = None,
                       weight: str = "1") -> list[dict]:
        """2글자 질의어 점수. rids 가 없으면 전역 상위 CANDIDATE_CAP 개(order_pre 정렬 우선), 있으면 그 후보 전부의 점수."""
        total = self._passage_total()
        idf = []
        for g in grams:
            df = self._db.execute("SELECT count(*) FROM bigram_terms WHERE gram=?", (g,)).fetchone()[0]
            idf += [g, math.log(1 + (total - df + 0.5) / (df + 0.5))]
        case = "CASE b.gram " + " ".join("WHEN ? THEN ?" for _ in grams) + " ELSE 0 END"
        s = f"SUM(({case}) * b.n * ? / (b.n + ?))"
        sp = [*idf, BIGRAM_K1 + 1, BIGRAM_K1]
        marks = ",".join("?" * len(grams))
        head = f"SELECT b.rid AS rid, {s} AS s FROM bigram_terms b JOIN passages p ON p.rid=b.rid WHERE b.gram IN ({marks}){fsql}"
        if rids is None:
            return [{"rid": r["rid"], "s": r["s"]} for r in self._db.execute(
                head + f" GROUP BY b.rid ORDER BY {order_pre}s * {weight} DESC, p.passage_id LIMIT ?", [*sp, *grams, *fparams, CANDIDATE_CAP])]
        out = []
        for i in range(0, len(rids), 500):
            part = rids[i:i + 500]
            out += [{"rid": r["rid"], "s": r["s"]} for r in self._db.execute(
                head + f" AND b.rid IN ({','.join('?' * len(part))}) GROUP BY b.rid", [*sp, *grams, *fparams, *part])]
        return out

    # -- 상태 ---------------------------------------------------------------------
    def status(self) -> dict:
        st = self._stats
        built = self._meta.get("built_at", "")
        sources = st.get("sources", {})
        held: dict[tuple, list[str]] = {}
        empty, partial = [], []
        for fam, s in sources.items():
            if s.get("redistribute_raw") != "allowed" or s.get("redistribute_derivatives") != "allowed":
                held.setdefault((s.get("redistribute_raw"), s.get("redistribute_derivatives")), []).append(fam)
            if s.get("documents", 0) and not s.get("passages", 0):
                empty.append(f"{fam}({s['documents']})")
            elif s.get("documents", 0) > s.get("documents_with_passages", 0):
                partial.append(f"{fam}: 문서 {s['documents']}건 중 {s.get('documents_with_passages', 0)}건")
        rights_hold = [f"재배포 raw={k[0]}, derivatives={k[1]}: 소스 {len(v)}개({', '.join(v)}). 허락 전까지 사용자 화면 인용은 {DISPLAY_QUOTE_MAX_CP}자 이내"
                       for k, v in sorted(held.items(), key=lambda kv: str(kv[0]))]
        missing = []
        if partial:
            missing.append("색인에 passage가 있는 문서만 집계(나머지는 수집·추출·구조화 전): " + "; ".join(partial))
        if empty:
            missing.append("색인 passage 0건인 소스(장부 문서 수): " + ", ".join(empty))
        excl = st.get("excluded", {})
        if excl:
            missing.append("색인에서 제외한 chunk: " + ", ".join(f"{k} {v}건" for k, v in sorted(excl.items())))
        by_v = st.get("by_verification_status", {})
        warnings = []
        cc = core_coverage({"cases_by_authority_type": st.get("cases_by_authority_type", {}), "is_fixture": False})
        if not cc["no_actionable_findings_allowed"]:
            warnings.append(cc["reason"])
        n_undated = int(st.get("date_unknown_passages", 0) or 0)
        if n_undated:
            warnings.append(f"결정일도 수록 간행물 발행일·연도도 없는 passage {n_undated}건(date_unknown)은 as_of 필터에서 빠지지 않고 date_unknown 으로 표시됩니다. "
                            "근거로 쓰면 보고서 coverage_notes 에 그 사실이 남습니다.")
        if not self._fts:
            warnings.append("이 환경의 SQLite 에 FTS5 trigram 이 없어 bigram 전용 검색으로 동작합니다." if self._index_has_fts
                            else "이 색인은 FTS5 trigram 없이 만들어져 bigram 전용 검색으로 동작합니다.")
        if by_v.get("verified", 0) < int(self._meta.get("passage_count", "0")):
            warnings.append(f"검수 완료(verified) passage {by_v.get('verified', 0)}건, 미검수(unverified) {by_v.get('unverified', 0)}건. 미검수 자료는 모델 구조화 결과이며 사람 검수 전이다.")
        return {
            "snapshot_id": self._snapshot, "is_fixture": False, "as_of": built[:10] or None, "index_built_at": built or None,
            "latest_decision_date": self._meta.get("latest_decision_date") or None,
            "earliest_decision_date": self._meta.get("earliest_decision_date") or None,
            "passage_count": int(self._meta.get("passage_count", "0")),
            "source_count": sum(s.get("documents_with_passages", 0) for s in sources.values()),
            "by_authority_type": st.get("by_authority_type", {}), "by_source_family": st.get("by_source_family", {}),
            "cases_by_authority_type": st.get("cases_by_authority_type", {}), "date_unknown_count": n_undated, "core_coverage": cc,
            "by_verification_status": by_v,
            "sources": {f: {k: v for k, v in x.items() if k != "documents_by_status"} for f, x in sources.items()},
            "rights_hold": rights_hold, "missing": missing, "warnings": warnings,
            "search_mode": "trigram+bigram" if self._fts else "bigram_only", "display_limit_chars": DISPLAY_QUOTE_MAX_CP,
            "notice": ("로컬 검색 색인(수집 장부의 chunk). passage 수는 전체 사건 수가 아니며 목록 대비 커버리지가 아니다. "
                       "기준일(as_of)은 색인 구축일이고 최신 결정일은 latest_decision_date 이다. "
                       "passage 건수는 chunk 수이고, 색인 규모 기준(core_coverage)은 사건 수(cases_by_authority_type)로 판정한다."),
        }


def index_coverage_note(status: dict, legal_as_of: str | None = None) -> str:
    """보고서 coverage_notes 에 넣는 색인 기준일 한 줄. 분석 기준일(legal_as_of)과 색인 기준일을 따로 적는다."""
    base = (f"검색 색인 {status.get('snapshot_id')}: 구축 {status.get('index_built_at') or '미상'}, 최신 결정일 {status.get('latest_decision_date') or '미상'}, "
            f"passage {status.get('passage_count')}건. 전체 사건 목록 대비 커버리지가 아니며 색인에 없는 자료는 찾지 못합니다.")
    return base + (f" 분석 기준일(legal_as_of)은 기사 발행(예정)일 {legal_as_of}이며 색인 기준일과 별개입니다." if legal_as_of else "")

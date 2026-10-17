"""법제처 국가법령정보 공동활용 DRF 판례 목록 어댑터(대법원 org=400201, 본문검색 search=2).

계약: docs/P1_DESIGN.md '어댑터 범위' lawapi 행, docs/coverage/lawapi_contract.md(공식 가이드 파라미터).
목록 열거(``run``)는 본문(lawService)을 받지 않는다. 본문 수집·섹션화·참조판례 확장은 파일 끝 '본문' 절(P2_DESIGN §3·§8, ``run_body``)이 하며
CLI 이름은 ``lawapi_body`` 다(``sources/lawapi_body.py`` 는 이 절을 부르는 얇은 진입점). 언론 관련성은 확정하지 않고 ``relevance: candidate`` 로 둔다.

- 검색어 사전(``TERMS``, COURT_AND_RESEARCH_PLAN §2 초안 27개) 마다 ``sort=dasc`` 로 페이지를 끝까지 돈다.
  ``totalCnt`` 가 ``SPLIT_THRESHOLD`` 를 넘는 질의는 선고일 구간(``prncYd``) 연 단위 슬라이스로 나눠 돈다.
- doc_id 는 ``lawgo_prec:<판례일련번호>``. 같은 판례가 여러 질의에 걸리면 ``extra_json.matched_terms`` 에 누적한다.
- 같은 사건번호에 판례일련번호가 둘일 수 있다(예: 80도306). 합치지 않는다.
- 선고일자 이상값(``0001.01.01`` 자리표시, 오늘 이후)은 원문 문자열을 ``extra.raw_date`` 에 보존하고 ``date_status`` 로 구분한다.
  자리표시값은 decision_date·year 를 비운다.
- 진행 요약은 ``corpus/inventory/lawgo_prec_terms.json`` (질의별 totalCnt·슬라이스·고유 일련번호 수·상태).

이식: ``_drf_ruling_date`` / ``_squash_ws`` 는 작성자 소유 코드(비공개)에서 가져왔다.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from ...config import private_mkdir
from ..context import TimeBudgetExceeded
from ..http import FetchFailed, IngestionError, strip_oc

SOURCE_ID = "lawgo_prec_supreme"
FAMILY = "law_go_kr"
DOC_PREFIX = "lawgo_prec"
ENDPOINT = "https://www.law.go.kr/DRF/lawSearch.do"
PUBLIC_VIEW = "https://www.law.go.kr/LSW/precInfoP.do?precSeq="
ORG_SUPREME = "400201"
SPLIT_THRESHOLD = 20000
MAX_CONSECUTIVE_FAILURES = 3
TERMS_FILE = "lawgo_prec_terms.json"

# COURT_AND_RESEARCH_PLAN §2 검색어 초안(전부). 표기 그대로 질의한다(따옴표 없음).
TERMS = (
    "언론", "언론사", "기자", "기사", "보도", "신문", "방송", "인터넷신문", "출판", "명예훼손",
    "인격권", "정정보도", "반론보도", "추후보도", "기사삭제", "사생활", "초상권", "실명",
    "공공의 이익", "상당한 이유", "사실의 적시", "의견", "논평", "후보자", "선거보도", "공정성", "여론조사",
)


# ---------------------------------------------------------------- 날짜(이식)
def _squash_ws(s: str) -> str:
    return " ".join(s.replace("﻿", " ").split())


def _drf_ruling_date(v: Any) -> str:
    """'2026.08.20'·'2026.8.5.'·'20260820' → ISO. 결손·비정상은 ""."""
    s = _squash_ws(str(v or "")).strip().rstrip(".")
    parts = [p.strip() for p in s.split(".")]
    parts = [p for p in parts if p and all(c in "0123456789" for c in p)]
    if len(parts) == 3 and len(parts) == len([p for p in s.split(".") if p.strip()]):
        y, m, d = (int(p) for p in parts)
        if 1900 < y < 2100 and 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"
    if len(s) == 8 and all(c in "0123456789" for c in s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return ""


def classify_date(raw: Any, today: date) -> tuple[Optional[str], str]:
    """(ISO 또는 None, date_status). date_status: ok | future | sentinel | invalid | missing."""
    r = _squash_ws(str(raw or "")).strip()
    if not r:
        return None, "missing"
    iso = _drf_ruling_date(r)
    if not iso:
        return None, ("sentinel" if r.startswith("0001") else "invalid")
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return None, "invalid"
    return iso, ("future" if d > today else "ok")


def source_scope(court: str, data_source: str) -> str:
    """게시 경로 표시(2026-10-08 실측: org=400201 응답에 출처가 다른 행이 섞인다).
    supreme: 법원명·데이터출처명 모두 대법원 / supreme_via_other_source: 법원명 대법원이나 출처가 다름(지방세법령정보시스템 1,295건,
    사건부호가 두·누·다 등 대법원형) / lower_court_in_supreme_source: 출처는 대법원이나 법원명이 하급심(13건) /
    non_court_source: 법원명이 비고 출처가 국세법령정보시스템·근로복지공단산재판례(사건번호 없음, 법원 판결이 아님)."""
    if data_source == "대법원":
        return "supreme" if court == "대법원" else "lower_court_in_supreme_source"
    return "supreme_via_other_source" if court == "대법원" else "non_court_source"


# ---------------------------------------------------------------- URL·파싱
def list_url(term: Optional[str], page: int, display: int, prnc_yd: Optional[str] = None, *, sort: str = "dasc") -> str:
    """OC 는 클라이언트가 주입한다(with_oc=True). 파라미터 순서는 캐시 키이므로 고정."""
    q = [("target", "prec"), ("type", "JSON")]
    if term is not None:
        q.append(("search", "2"))
    q += [("org", ORG_SUPREME)]
    if term is not None:
        q.append(("query", term))
    q += [("display", str(display)), ("page", str(page)), ("sort", sort)]
    if prnc_yd:
        q.append(("prncYd", prnc_yd))
    return ENDPOINT + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


class ListParseError(ValueError):
    pass


def parse_list(body: bytes) -> tuple[int, list[dict], str]:
    """(totalCnt, items, section). prec 는 0건이면 없고 1건이면 dict 일 수 있다. 숫자는 문자열로 온다."""
    try:
        j = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ListParseError("body is not JSON") from None
    ps = j.get("PrecSearch") if isinstance(j, dict) else None
    if not isinstance(ps, dict):
        raise ListParseError("no PrecSearch envelope")
    try:
        total = int(str(ps.get("totalCnt")).strip())
    except (TypeError, ValueError):
        raise ListParseError("totalCnt is not an integer") from None
    if total < 0:
        raise ListParseError("negative totalCnt")
    prec = ps.get("prec")
    if prec is None:
        items: list = []
    elif isinstance(prec, dict):
        items = [prec]
    elif isinstance(prec, list):
        items = prec
    else:
        raise ListParseError("unexpected prec type")
    if any(not isinstance(i, dict) for i in items):
        raise ListParseError("unexpected prec item type")
    return total, items, str(ps.get("section", ""))


def accept_list(body: bytes, headers: dict) -> Optional[str]:
    """오류 응답(HTTP 200 오류 HTML·JSON)을 캐시에 쓰지 않으려는 본문 표지 검사."""
    try:
        parse_list(body)
    except ListParseError as e:
        return f"not a prec list response: {e}"
    return None


def year_slices(this_year: int, first_year: int = 1950) -> list[str]:
    """빈틈 없는 prncYd 구간: 00010101~(first_year-1)1231, 연도별, (this_year+1)0101~99991231."""
    out = [f"00010101~{first_year - 1}1231"]
    out += [f"{y}0101~{y}1231" for y in range(first_year, this_year + 1)]
    out.append(f"{this_year + 1}0101~99991231")
    return out


# ---------------------------------------------------------------- 어댑터
class _LimitReached(Exception):
    pass


def add_arguments(parser) -> None:
    parser.add_argument("--term", action="append", dest="terms", help="검색어(반복 가능). 없으면 사전 전체")
    parser.add_argument("--display", type=int, default=100, help="쪽당 건수(1~100, 기본 100)")
    parser.add_argument("--seeds-file", default=None, help="court-research-seeds.json 경로(시작 판례 확인용, 기본: 저장소 docs/handoff 자동 탐색)")
    parser.add_argument("--no-probe", action="store_true", help="전체 DB 건수·이상 선고일 건수 탐침 생략")


class _Run:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.display = max(1, min(100, int(getattr(args, "display", 100) or 100)))
        self.today = ctx.now().date()
        self.pages_handled = 0
        self.consecutive_failures = 0
        self.new_docs = 0
        self.seen_docs = 0
        self.terms_added = 0
        self.bad_items = 0
        self.failed_pages = 0

    # -- 쪽 하나
    def fetch_page(self, term: Optional[str], page: int, prnc_yd: Optional[str] = None, display: Optional[int] = None,
                   sort: str = "dasc"):
        """(total, items) 또는 None(실패). 한도·시간 상한은 예외로 올린다."""
        ctx = self.ctx
        if ctx.limit_pages is not None and self.pages_handled >= ctx.limit_pages:
            raise _LimitReached()
        ctx.check_time()
        url = list_url(term, page, display or self.display, prnc_yd, sort=sort)
        try:
            r = ctx.http.get(url, with_oc=True, accept=accept_list)
        except FetchFailed:
            r = None
        self.pages_handled += 1
        if r is None or not r.ok:
            self.failed_pages += 1
            self.consecutive_failures += 1
            if r is not None:
                ctx.manifest.record_failure(r.url, "list", r.content_error or f"http_{r.status}", "list page not usable")
                ctx.manifest.upsert_list_page(SOURCE_ID, page, r.url, http_status=r.status, item_count=0, body_sha256=None, ok=False)
            if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise IngestionError(f"{MAX_CONSECUTIVE_FAILURES} consecutive list failures; stopping (unverified)")
            return None
        self.consecutive_failures = 0
        total, items, _section = parse_list(r.body)
        ctx.manifest.resolve_failures(r.url, "list")
        ctx.manifest.upsert_list_page(SOURCE_ID, page, r.url, http_status=r.status, item_count=len(items),
                                      body_sha256=r.sha256, ok=True)
        return total, items

    # -- 문서
    def store_items(self, term: str, items: list[dict], serials: set) -> None:
        m = self.ctx.manifest
        with m.transaction():
            for it in items:
                serial = str(it.get("판례일련번호", "")).strip()
                if not serial.isdigit():
                    self.bad_items += 1
                    continue
                serials.add(serial)
                iso, dstatus = classify_date(it.get("선고일자"), self.today)
                link = str(it.get("판례상세링크") or "")
                detail = strip_oc("https://www.law.go.kr" + link) if link.startswith("/") else (strip_oc(link) if link else None)
                doc_id = f"{DOC_PREFIX}:{serial}"
                extra = {
                    "relevance": "candidate", "matched_terms": [term], "serial": serial,
                    "case_no": str(it.get("사건번호") or "").strip(), "court": str(it.get("법원명") or "").strip(),
                    "case_kind": str(it.get("사건종류명") or "").strip(), "case_kind_code": str(it.get("사건종류코드") or "").strip(),
                    "verdict_form": str(it.get("선고") or "").strip(), "data_source": str(it.get("데이터출처명") or "").strip(),
                    "raw_date": str(it.get("선고일자") or ""), "date_status": dstatus,
                }
                extra["source_scope"] = source_scope(extra["court"], extra["data_source"])
                res = m.upsert_document(
                    doc_id, SOURCE_ID, canonical_url=PUBLIC_VIEW + serial, title=str(it.get("사건명") or "").strip() or None,
                    decision_date=iso, year=int(iso[:4]) if iso else None, doc_type=str(it.get("판결유형") or "").strip() or None,
                    detail_url=detail, extra=extra)
                if res == "new":
                    self.new_docs += 1
                else:
                    self.seen_docs += 1
                    if m.update_document_fields(doc_id, extra_merge={"matched_terms": [term], "source_scope": extra["source_scope"]}):
                        self.terms_added += 1

    # -- 구간 하나(prnc_yd=None 이면 전체)
    def enumerate_segment(self, term: str, prnc_yd: Optional[str], serials: set, out: list, preloaded: Optional[tuple] = None) -> dict:
        seg: dict = {"prncYd": prnc_yd, "total": None, "pages": None, "pages_done": 0, "unique": 0, "failed_pages": [],
                     "status": "partial"}
        out.append(seg)                        # 중단돼도 진행 상황이 상태 파일에 남는다
        local: set = set()
        page = 1
        while True:
            got = preloaded if (page == 1 and preloaded is not None) else self.fetch_page(term, page, prnc_yd)
            if got is None:
                seg["failed_pages"].append(page)
                if page == 1:
                    break                      # totalCnt 를 모르면 더 못 간다
            else:
                total, items = got
                if page == 1:
                    seg["total"] = total
                    seg["pages"] = max(1, math.ceil(total / self.display))
                elif total != seg["total"]:
                    seg["total_drift"] = total
                self.store_items(term, items, local)
                seg["pages_done"] += 1
                if not items and page > 1:
                    seg["early_end"] = page
                    break
            page += 1
            if seg["pages"] is None or page > seg["pages"]:
                break
        seg["unique"] = len(local)
        serials |= local
        if seg["total"] is not None and not seg["failed_pages"] and seg["pages_done"] == seg["pages"] and "early_end" not in seg:
            seg["status"] = "complete" if seg["unique"] == seg["total"] else "count_mismatch"
        return seg

    # -- 질의 하나
    def enumerate_term(self, term: str) -> dict:
        st: dict = {"term": term, "total": None, "split": False, "segments": [], "unique_serials": 0, "status": "partial"}
        try:
            first = self.fetch_page(term, 1)
            if first is None:
                st["status"] = "failed_first_page"
                return st
            total = first[0]
            st["total"] = total
            serials: set = set()
            if total <= SPLIT_THRESHOLD:
                self.enumerate_segment(term, None, serials, st["segments"], preloaded=first)
            else:
                st["split"] = True
                self.store_items(term, first[1], set())      # 구간 열거와 별개로 1쪽 적중도 장부에 넣는다
                for rng in year_slices(self.today.year):
                    seg = self.enumerate_segment(term, rng, serials, st["segments"])
                    if (seg["total"] or 0) > SPLIT_THRESHOLD:
                        seg["oversize_slice"] = True          # 더 쪼개지 않고 끝까지 돈다(생략 금지)
                st["sum_segment_totals"] = sum(s["total"] or 0 for s in st["segments"])
            st["unique_serials"] = len(serials)
            segs = st["segments"]
            if all(s["status"] == "complete" for s in segs) and serials and len(serials) == total and (
                    not st["split"] or st["sum_segment_totals"] == total):
                st["status"] = "complete"
            elif all(s["status"] == "complete" for s in segs) and total == 0:
                st["status"] = "complete"
            elif all(s["status"] in ("complete", "count_mismatch") for s in segs):
                st["status"] = "count_mismatch"
            return st
        except (_LimitReached, TimeBudgetExceeded) as e:
            st["status"] = "partial"
            st["limited" if isinstance(e, _LimitReached) else "time_paused"] = True
            raise _TermInterrupted(st, e) from None


class _TermInterrupted(Exception):
    def __init__(self, state: dict, cause: Exception):
        self.state = state
        self.cause = cause


def _load_terms_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_terms_file(path: Path, data: dict) -> None:
    private_mkdir(path.parent)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _find_seeds(explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    for parent in Path(__file__).resolve().parents:
        cand = parent / "docs" / "handoff" / "court-research-seeds.json"
        if cand.is_file():
            return cand
    return None


def check_seeds(ctx, seeds_path: Path) -> list[dict]:
    """시작 판례가 documents 에 있는지: precSeq 가 있으면 일련번호로, 없으면 사건번호+선고일로."""
    import re
    try:
        seeds = json.loads(seeds_path.read_text(encoding="utf-8")).get("judgments", [])
    except (OSError, ValueError):
        return []
    out = []
    for s in seeds:
        m = re.search(r"precSeq=(\d+)", s.get("url", ""))
        found, via, terms = False, None, None
        if m:
            row = ctx.manifest.get_document(f"{DOC_PREFIX}:{m.group(1)}")
            if row:
                found, via = True, "serial"
                terms = (json.loads(row["extra_json"]).get("matched_terms") if row.get("extra_json") else None)
                if row.get("decision_date") != s.get("date"):
                    via = "serial_date_differs"
        if not found:
            for cn in s.get("case_numbers", []):
                rows = ctx.manifest.find_by_json("documents", "extra_json", "case_no", cn,
                                                 where={"source_id": SOURCE_ID, "decision_date": s.get("date")})
                if rows:
                    found, via = True, "case_no+date"
                    terms = json.loads(rows[0]["extra_json"]).get("matched_terms")
                    break
        out.append({"id": s.get("id"), "date": s.get("date"), "case_numbers": s.get("case_numbers"), "found": found,
                    "via": via, "matched_terms": terms})
    return out


def _probe_from(today: date) -> date:
    """미래 탐침의 시작일: 오늘 다음 날. classify_date 의 'future'(선고일 > 오늘)와 같은 경계다."""
    return today + timedelta(days=1)


def _probe_all(run: _Run) -> dict:
    """전체 대법원 DB 건수와 이상 선고일 건수(질의 없음, display=1, 장부에 문서를 넣지 않는다).
    after_today = 선고일이 오늘보다 뒤(``after_today_from`` 부터)인 건수. 올해 안의 미래 날짜도 센다."""
    out: dict = {}
    start = _probe_from(run.today)
    for key, rng in (("all", None), ("before_1900", "00010101~18991231"),
                     ("after_today", f"{start:%Y%m%d}~99991231")):
        got = run.fetch_page(None, 1, rng, display=1)
        out[key] = got[0] if got else None
    out["after_today_from"] = start.isoformat()
    return out


def run(ctx, args) -> dict:
    m = ctx.manifest
    m.ensure_source(SOURCE_ID, FAMILY, "법제처 국가법령정보 판례(대법원, 본문검색 사전)",
                    ENDPOINT + "?target=prec&org=" + ORG_SUPREME,
                    notes="DRF lawSearch target=prec org=400201 search=2 sort=dasc; 본문(lawService)은 P2; relevance=candidate")
    r = _Run(ctx, args)
    explicit = getattr(args, "terms", None)
    terms = tuple(explicit) if explicit else TERMS
    full = not explicit and ctx.limit_pages is None
    terms_path = ctx.inventory_dir / TERMS_FILE
    saved = _load_terms_file(terms_path)
    state: dict = dict(saved.get("terms", {}))
    probe = saved.get("probe_all")
    seeds_out = saved.get("seeds")
    limited = False
    try:
        probe_fresh = bool(probe and probe.get("all") is not None and probe.get("after_today") is not None
                           and probe.get("after_today_from") == _probe_from(r.today).isoformat() and not ctx.refresh)
        if full and not getattr(args, "no_probe", False) and not probe_fresh:
            probe = _probe_all(r)
        for term in terms:
            try:
                state[term] = r.enumerate_term(term)
            except _TermInterrupted as ti:
                state[term] = ti.state
                if isinstance(ti.cause, TimeBudgetExceeded):
                    raise ti.cause from None           # CLI가 paused_time_limit 으로 처리한다(상태 파일은 finally 에서 쓴다)
                limited = True
                break
    except _LimitReached:
        limited = True
    finally:
        m.touch_source_checked(SOURCE_ID)
        if not ctx.dry_run:
            _write_terms_file(terms_path, {"terms": state, "probe_all": probe, "seeds": seeds_out,
                                           "split_threshold": SPLIT_THRESHOLD, "org": ORG_SUPREME, "display": r.display})
    # 요약(장부 질의). 전체 실행일 때만 시작 판례 확인.
    stats = _stats(ctx)
    if full:
        sp = _find_seeds(getattr(args, "seeds_file", None))
        if sp:
            seeds_out = check_seeds(ctx, sp)
            if not ctx.dry_run:
                _write_terms_file(terms_path, {"terms": state, "probe_all": probe, "seeds": seeds_out,
                                               "split_threshold": SPLIT_THRESHOLD, "org": ORG_SUPREME, "display": r.display})
    done = [t for t in terms if state.get(t, {}).get("status") == "complete"]
    mismatch = [t for t in terms if state.get(t, {}).get("status") == "count_mismatch"]
    return {
        "terms_total": len(terms), "terms_complete": len(done), "terms_count_mismatch": mismatch,
        "terms_pending": [t for t in terms if state.get(t, {}).get("status") not in ("complete", "count_mismatch")],
        "pages_handled": r.pages_handled, "pages_failed": r.failed_pages, "new_docs": r.new_docs, "seen_docs": r.seen_docs,
        "matched_term_additions": r.terms_added, "bad_items": r.bad_items, "limited": limited,
        "probe_all": probe, "seeds": seeds_out, **stats,
    }


def _stats(ctx) -> dict:
    q = ctx.manifest._q
    n = q("SELECT COUNT(*) FROM documents WHERE source_id=?", (SOURCE_ID,))[0][0]
    ok = q("SELECT MIN(decision_date), MAX(decision_date) FROM documents WHERE source_id=? AND "
           "json_extract(extra_json,'$.date_status')='ok'", (SOURCE_ID,))[0]
    by_status = {r[0]: r[1] for r in q("SELECT json_extract(extra_json,'$.date_status'), COUNT(*) FROM documents "
                                         "WHERE source_id=? GROUP BY 1", (SOURCE_ID,))}
    non_supreme = q("SELECT COUNT(*) FROM documents WHERE source_id=? AND json_extract(extra_json,'$.court')<>'대법원'",
                    (SOURCE_ID,))[0][0]
    multi = q("SELECT COUNT(*) FROM (SELECT 1 FROM documents WHERE source_id=? GROUP BY json_extract(extra_json,'$.case_no'), "
              "decision_date HAVING COUNT(*)>1)", (SOURCE_ID,))[0][0]
    pages = q("SELECT COUNT(*), COALESCE(SUM(ok),0) FROM list_pages WHERE source_id=?", (SOURCE_ID,))[0]
    return {"documents": n, "min_valid_date": ok[0], "max_valid_date": ok[1], "date_status": by_status,
            "non_supreme_court_rows": non_supreme, "case_no_date_groups_with_multiple_serials": multi,
            "list_pages": pages[0], "list_pages_ok": pages[1]}


# ====================================================================================================================
# 본문(P2_DESIGN §3·§8): lawService(target=prec) 수신 → 필드 분리 → html_section pages → 참조판례 확장
# ====================================================================================================================
BODY_ENDPOINT = "https://www.law.go.kr/DRF/lawService.do"
BODY_FIELDS = ("판시사항", "판결요지", "참조조문", "참조판례", "판례내용")
EXTRACTOR_VERSION = "lawgo-prec-html-2"      # 섹션화 규칙을 바꾸면 올린다. 올리면 저장된 raw 로 다시 추출한다(네트워크 없음). 2: 머리 쪽(#h000) 추가.
FILTER_VERSION = "prefilter-3"          # 3: 처리 순위 규칙 변경(G-D31: 깊이 2 이상 확장분은 P-T2). 2: 언론어에 출판 추가(P2_DESIGN G-D1). 올리면 저장된 raw 로 사전 필터만 다시 계산한다(refilter_stale).
RAW_DIR = "raw/law_go_kr"                    # corpus 기준 상대 경로(P2_DESIGN §1: raw/<family>/<안정 ID>.<ext>)
TEXT_DIR = "text/lawgo_prec"                 # corpus 기준 상대 경로(P2_DESIGN §2: text/... 페이지별)
SOURCE_REFS = "lawgo_prec_refs"              # 참조판례 포인터 행(판례 본문 문서가 아니다)
REF_PREFIX = "lawgo_ref"
MAX_BODY_RETRIES = 3                         # 같은 판례 본문 실패가 이 횟수에 닿으면 다음 실행에서 건너뛴다(failures.retry_count)
MAX_REF_NUMBERS_PER_QUERY = 3                # 한 참조에서 사건번호 검색을 시도하는 번호 수 상한
HEADNOTE_PAGES = (("판시사항", "headnote_issue"), ("판결요지", "headnote_holding"), ("참조조문", "ref_statutes"),
                  ("참조판례", "ref_cases"))
JUDGMENT_FIRST_IDX = len(HEADNOTE_PAGES)     # 판례내용 섹션 번호는 4부터. 머리 필드는 비어도 번호를 비워 둔다(재추출 때 번호 고정).
HEADER_LOC = "h000"                          # 사건 머리 쪽: 이 판결 자신의 사건번호·선고일자·법원명. 's' 번호를 밀지 않으려고 따로 둔 이름이고,
                                             # 쪽 정렬(page_id 순)에서 s000 보다 앞이라 모델에는 맨 앞 쪽으로 보인다.

# ---------------------------------------------------------------- 우선순위 단계(P2_DESIGN §8 정의 그대로)
GENERAL_TERMS = frozenset({"의견", "상당한 이유"})
CORE_TERMS = frozenset({"정정보도", "반론보도", "추후보도", "기사삭제", "선거보도"})
MEDIA_TERMS = frozenset({"언론", "언론사", "기자", "기사", "보도", "신문", "방송", "인터넷신문", "출판"})       # 출판: 출판물 명예훼손(G-D1)
PERSONALITY_TERMS = frozenset({"명예훼손", "인격권", "사생활", "초상권"})
TIERS = ("P-T1", "P-T2", "P-T3")
DONE_STATUSES = ("extracted", "structured", "verified", "indexed", "quarantined")      # 본문 추출 이후 단계


def compute_tier(matched_terms) -> Optional[str]:
    """목록 질의에서 적중한 검색어(``extra.matched_terms``)로만 계산한다. 본문은 보지 않는다.

    P-T1: 핵심어 중 하나, 또는 언론어와 인격권어가 동시에 적중. P-T2: 일반어(의견·상당한 이유)를 뺀 검색어가 하나라도 적중한 나머지.
    P-T3: 일반어로만 걸린 나머지. 적중 검색어가 없으면 None(참조판례 확장분 등)."""
    mt = set(matched_terms or ())
    if not mt:
        return None
    if mt & CORE_TERMS or (mt & MEDIA_TERMS and mt & PERSONALITY_TERMS):
        return "P-T1"
    if mt - GENERAL_TERMS:
        return "P-T2"
    return "P-T3"


def normalize_tier(value: str) -> str:
    v = str(value).strip().upper().replace("_", "-")
    if v in ("T1", "T2", "T3"):
        v = "P-" + v
    if v not in TIERS:
        raise ValueError(f"unknown tier '{value}' (use T1, T2, T3)")
    return v


def processing_tier_of(tier: Optional[str], expanded: bool, ref_depth) -> Optional[str]:
    """처리 순위(G-D31). 확장분(``expanded_from`` 있음)은 깊이 1(깊이 기록이 없으면 1로 본다)까지만 P-T1 이다.
    깊이 2 이상은 §8 규칙으로 P-T1 이 된 것만 P-T1 을 유지하고, 나머지는 자기 tier(없으면 P-T2)를 따른다."""
    if not expanded:
        return tier
    try:
        depth = int(ref_depth) if ref_depth is not None else 1
    except (TypeError, ValueError):
        depth = 1
    if depth <= 1 or tier == "P-T1":
        return "P-T1"
    return tier or "P-T2"


def assign_tiers(manifest) -> dict:
    """모든 판례 문서의 ``extra.tier``(§8, 적중 검색어 기준)와 ``extra.processing_tier``(처리 순위)를 맞춘다. 바뀐 행만 쓴다.

    processing_tier 는 tier 와 같되, 참조판례 확장분(``expanded_from`` 이 있는 문서)은 깊이 1 까지만 P-T1 이고 깊이 2 이상은 P-T2 와 같다(G-D31).
    확장분의 tier 는 적중 검색어가 없으면 비어 있다."""
    rows = manifest._q(
        "SELECT doc_id, json_extract(extra_json,'$.matched_terms') AS mt, json_extract(extra_json,'$.tier') AS tier, "
        "json_extract(extra_json,'$.processing_tier') AS ptier, "
        "COALESCE(json_array_length(json_extract(extra_json,'$.expanded_from')),0) AS nexp, "
        "json_extract(extra_json,'$.ref_depth') AS depth FROM documents WHERE source_id=?",
        (SOURCE_ID,))
    counts: dict = {}
    updates = []
    for r in rows:
        try:
            mt = json.loads(r["mt"]) if r["mt"] else []
        except ValueError:
            mt = []
        tier = compute_tier(mt)
        ptier = processing_tier_of(tier, bool(r["nexp"]), r["depth"])
        counts[ptier or "none"] = counts.get(ptier or "none", 0) + 1
        if tier != r["tier"] or ptier != r["ptier"]:
            updates.append((tier, ptier, r["doc_id"]))
    if updates:
        with manifest.transaction() as c:
            c.executemany("UPDATE documents SET extra_json=json_set(COALESCE(extra_json,'{}'),'$.tier',?,'$.processing_tier',?) "
                          "WHERE doc_id=?", updates)
    return {"processing_tier_counts": counts, "tier_rows_changed": len(updates)}


# ---------------------------------------------------------------- 결정적 관련성 사전 필터(§8 P-T2·P-T3)
# 쟁점어는 §8 에 정의가 없다. 핵심어와 인격권어의 합집합으로 읽는다(검토 대상: 이 해석이 좁으면 상수만 바꾸고 FILTER_VERSION 을 올린다).
ISSUE_TERMS = CORE_TERMS | PERSONALITY_TERMS
# 언론어는 §8 목록(G-D1 로 출판 추가) 그대로이되 다른 뜻으로 흔한 어형만 뺀다(증인·피고인 신문, 기자재, 운전기사, 보도블록).
_MEDIA_PATTERNS = {
    "언론": re.compile(r"언론"),
    "언론사": re.compile(r"언론사"),
    "기자": re.compile(r"기자(?!재)"),
    "기사": re.compile(r"(?<!운전)(?<!택시)(?<!버스)(?<!화물)(?<!기관)기사(?!님)"),
    "보도": re.compile(r"보도(?!블[록럭])"),
    "신문": re.compile(r"(?<!증인)(?<!피고인)(?<!피의자)(?<!당사자)(?<!반대)(?<!감정인)(?<!본인)(?<!주)(?<!재)신문(?!조서|절차|기일|사항)"),
    "방송": re.compile(r"방송"),
    "인터넷신문": re.compile(r"인터넷신문"),
    "출판": re.compile(r"출판"),
}


def relevance_prefilter(text: str) -> dict:
    """본문에 언론어와 쟁점어가 동시에 있는지(결정적). 반환: {passed, media_terms, issue_terms, version}. 어휘 적중만 본다."""
    media = sorted(t for t, p in _MEDIA_PATTERNS.items() if p.search(text))
    issue = sorted(t for t in ISSUE_TERMS if t in text)
    return {"passed": bool(media and issue), "media_terms": media, "issue_terms": issue, "version": FILTER_VERSION}


# ---------------------------------------------------------------- 응답 파싱
class BodyParseError(ValueError):
    """kind: no_match(해당 판례 없음) | denied(인증·권한·형식 오류 응답) | malformed."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


def body_url(serial: str) -> str:
    return BODY_ENDPOINT + "?" + urllib.parse.urlencode([("target", "prec"), ("ID", serial), ("type", "JSON")],
                                                        quote_via=urllib.parse.quote)


def _field_str(v: Any) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def parse_body(body: bytes) -> dict:
    """lawService prec 응답(JSON 또는 XML) → {'fields': {필드명: 문자열}, 'format': 'json'|'xml'}. 오류 응답은 BodyParseError."""
    head = body.lstrip()[:1]
    if head == b"{":
        try:
            j = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BodyParseError("malformed", "body is not JSON") from None
        svc = j.get("PrecService") if isinstance(j, dict) else None
        if isinstance(svc, dict):
            return {"fields": {str(k): _field_str(v) for k, v in svc.items()}, "format": "json"}
        txt = json.dumps(j, ensure_ascii=False)
        raise BodyParseError("no_match" if "일치하는 판례가 없" in txt else "denied", "no PrecService envelope")
    if head == b"<":
        low = body[:2048].lower()
        if b"<!doctype html" in low or b"<html" in low:
            raise BodyParseError("denied", "html response")
        if b"<!doctype" in low or b"<!entity" in low:
            raise BodyParseError("malformed", "xml declarations are not accepted")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise BodyParseError("malformed", "body is not XML") from None
        if root.tag == "PrecService":
            return {"fields": {c.tag: (c.text or "") for c in root}, "format": "xml"}
        txt = "".join(root.itertext())
        raise BodyParseError("no_match" if "일치하는 판례가 없" in txt else "denied", "no PrecService root")
    raise BodyParseError("malformed", "unrecognized body")


def accept_body(body: bytes, headers: dict, serial: str) -> Optional[str]:
    """오류 응답(HTTP 200 오류 JSON·HTML)과 요청한 일련번호가 아닌 응답을 캐시에 쓰지 않으려는 본문 표지 검사."""
    try:
        p = parse_body(body)
    except BodyParseError as e:
        return f"{e.kind}: {e}"[:200]
    got = p["fields"].get("판례정보일련번호", "").strip()
    if got != serial:
        return f"serial_mismatch: expected {serial}"
    return None


# ---------------------------------------------------------------- 텍스트·섹션
def html_to_text(s: str) -> str:
    """``<br/>`` 만 줄바꿈으로 바꾸고 엔티티를 푼다(실측: 이 필드에 다른 태그는 없다). 그 밖의 가공은 하지 않는다.
    저장되는 페이지 텍스트와 offset 은 모두 이 결과를 기준으로 한다."""
    s = re.sub(r"(?i)<br\s*/?>", "\n", s or "")
    s = html.unescape(s).replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    return s


_BRACKET_RE = re.compile(r"^[ \t　]*【\s*([^】\n]{1,40}?)\s*】")
_SIG_RE = re.compile(r"^[ \t　]*(?:대법원장|대법관|재판장|판사|재판관)[^\n]{0,80}?[가-힣○]{2,5}\((?:재판장|주심)\)")
_OP_TAIL_RE = re.compile(r"(?:다수|반대|별개|보충|소수)의견(?:\s*은?\s*다음과\s*같다\.)?\s*$")
_NUM_PREFIX = r"(?:\(?\d{1,2}[\.\)]|[가-하][\.\)])"
_SHORT_OP_RE = re.compile(r"^" + _NUM_PREFIX + r"\s*(?:대법관[^\n]*의\s*)?(?:반대|별개|보충)의견$")
_REL_OP_RE = re.compile(r"^(?:" + _NUM_PREFIX + r")?\s*(?:다수|반대|별개)의견에\s*대한")
_OPINION_WORDS = ("다수의견", "반대의견", "별개의견", "보충의견", "소수의견")
_MENTION_RE = re.compile(r"대법관[^\n]{0,150}(?:반대|별개|보충)의견")
_CORE_LABELS = {"주문": "order", "이유": "reasons", "전문": "reasons"}


def classify_opinion(label: str) -> Optional[str]:
    """의견 표지 문구 → 종류. majority | majority_supplement | dissent | partial_dissent | dissent_supplement |
    concurrence | concurrence_supplement | supplement(부모 불명) | minority. 알 수 없으면 None."""
    L = re.sub(r"\s+", "", label)
    if "보충의견" in L:
        if "다수의견에대한" in L or "다수의견의보충" in L:
            return "majority_supplement"
        if "반대의견에대한" in L:
            return "dissent_supplement"
        if "별개의견에대한" in L:
            return "concurrence_supplement"
        return "supplement"
    if "반대의견" in L:
        return "partial_dissent" if "일부반대의견" in L else "dissent"
    if "별개의견" in L:
        return "concurrence"
    if "소수의견" in L:
        return "minority"
    if "다수의견" in L:
        return "majority"
    return None


def _opinion_heading(s: str) -> Optional[str]:
    """한 줄이 의견 표지면 종류, 아니면 None. 표지는 '…대법관 ○○○의 반대의견'(번호 머리 허용)처럼 의견 명사로 끝나거나
    '…반대의견은 다음과 같다.' 로 끝난다. 마침표로 끝나는 서술문('…다수의견을 밝힌다.')과 긴 문단은 표지가 아니다."""
    if len(s) > 300 or not _OP_TAIL_RE.search(s):
        return None
    if "대법관" in s or "대법원장" in s or _REL_OP_RE.match(s) or (len(s) <= 40 and _SHORT_OP_RE.match(s)):
        return classify_opinion(s)
    return None


def split_sections(text: str) -> tuple[list[dict], dict]:
    """판례내용 텍스트를 결정적 규칙으로 구간으로 나눈다. 구간은 겹치지 않고 이어 붙이면 원문과 같다(char_start/char_end).

    kind: caption(주문 앞 당사자·원심판결 등) | order | reasons | opinion | heading(그 밖의 【…】) | signature | trailer | body | preamble.
    opinion 구간은 opinion_kind 를 갖고, reasons·body 구간은 'main'(의견 표지가 있으면 'majority'). 표지는 줄 단위로만 찾는다.
    반환 info.status: none(의견 표지 단어 없음) | split(표지로 나눔) | unsplit(표지는 못 찾았으나 대법관의 반대·별개·보충의견 언급이 있음)."""
    lines = text.split("\n")
    offs, o = [], 0
    for ln in lines:
        offs.append(o)
        o += len(ln) + 1
    marks: list[tuple[int, str, str, Optional[str]]] = []      # (offset, kind, label, opinion_kind)
    seen_core = False
    after_sig = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        m = _BRACKET_RE.match(ln)
        if m:
            label = re.sub(r"\s+", "", m.group(1))
            op = classify_opinion(label) if any(w in label for w in _OPINION_WORDS) else None
            after_sig = False
            if op:
                marks.append((offs[i], "opinion", label, op))
            elif label in _CORE_LABELS:
                seen_core = True
                marks.append((offs[i], _CORE_LABELS[label], label, None))
            elif seen_core:
                marks.append((offs[i], "heading", label, None))
            continue
        if _SIG_RE.match(ln) and len(s) <= 200:
            if not (marks and marks[-1][1] == "signature" and after_sig):
                marks.append((offs[i], "signature", s[:80], None))
            after_sig = True
            continue
        op = _opinion_heading(s)
        if op:
            marks.append((offs[i], "opinion", s[:160], op))
            after_sig = False
            continue
        if after_sig:                                           # 서명 뒤에 남은 본문(별지 등)
            marks.append((offs[i], "trailer", "", None))
            after_sig = False
    first_nonempty = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    starts_caption = seen_core and first_nonempty is not None and bool(_BRACKET_RE.match(lines[first_nonempty])) and not any(
        mk[0] == offs[first_nonempty] for mk in marks)
    bounds: list[tuple[int, str, str, Optional[str]]] = []
    if not marks or marks[0][0] > 0:
        bounds.append((0, "caption" if starts_caption else ("preamble" if seen_core else "body"), "", None))
    bounds += marks
    sections = []
    for n, (st, kind, label, op) in enumerate(bounds):
        en = bounds[n + 1][0] if n + 1 < len(bounds) else len(text)
        if kind in ("reasons", "body"):
            op = "main"
        sections.append({"kind": kind, "label": label, "opinion_kind": op, "char_start": st, "char_end": en, "text": text[st:en]})
    kinds = [s["opinion_kind"] for s in sections if s["kind"] == "opinion"]
    if kinds:
        for s in sections:
            if s["opinion_kind"] == "main":
                s["opinion_kind"] = "majority"
        status = "split"
    else:
        status = "unsplit" if _MENTION_RE.search(text) else "none"
    info = {"status": status, "markers": [{"kind": s["opinion_kind"], "label": s["label"]} for s in sections if s["kind"] == "opinion"],
            "marker_count": len(kinds)}
    return sections, info


# ---------------------------------------------------------------- 사건번호·참조판례
_CASE_FULL = re.compile(r"^(\d{2,4})([가-힣]{1,4})(\d+)$")
_REF_RE = re.compile(
    r"(?P<court>대법원|헌법재판소|[가-힣]{1,8}(?:고등법원|지방법원|가정법원|행정법원|특허법원|회생법원|고법|지법|법원))\s*"
    r"(?P<y>\d{4})\.\s*(?P<m>\d{1,2})\.\s*(?P<d>\d{1,2})\.?\s*(?P<kind>선고|자)\s*"
    r"(?P<nos>[^()/\n]{1,200}?)\s*(?P<form>(?:전원합의체|전원재판부|전합)?\s*(?:판결|결정|명령|판정))")
_REF_DATE_RE = re.compile(r"\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.?\s*(?:선고|자)")


def split_case_numbers(s: str) -> list[str]:
    """'2000다37524, 37531' → ['2000다37524', '2000다37531'] (숫자만 있는 뒤 번호는 앞 번호의 연도·부호를 잇는다).
    '2005헌마165, 314, 2006헌가3' 도 같다. 인식하지 못하는 조각은 버린다. 순서 유지·중복 제거."""
    out: list[str] = []
    prefix = None
    for part in re.split(r"[,·ㆍ、]|\s+및\s+", s or ""):
        p = re.sub(r"\([^)]*\)", "", part).strip().replace(" ", "")
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


def parse_refs(text: str) -> tuple[list[dict], int]:
    """참조판례 필드 텍스트 → ([{court, court_class, date, kind, form, numbers, raw}], 날짜 표현 수 − 해석 수).
    court_class: supreme | constitutional | lower. 같은 (법원, 날짜, 번호들)은 한 번만 낸다. 둘째 값이 0보다 크면 놓친 참조가 있을 수 있다."""
    refs, seen, parsed = [], set(), 0
    for m in _REF_RE.finditer(text or ""):
        try:
            d = date(int(m.group("y")), int(m.group("m")), int(m.group("d"))).isoformat()
        except ValueError:
            continue
        nums = split_case_numbers(m.group("nos"))
        if not nums:
            continue
        parsed += 1
        court = m.group("court")
        key = (court, d, tuple(nums))
        if key in seen:
            continue
        seen.add(key)
        refs.append({"court": court, "court_class": "supreme" if court == "대법원" else ("constitutional" if court == "헌법재판소" else "lower"),
                     "date": d, "kind": m.group("kind"), "form": re.sub(r"\s+", "", m.group("form")), "numbers": nums,
                     "raw": _squash_ws(m.group(0))[:200]})
    gap = len(_REF_DATE_RE.findall(text or "")) - parsed
    return refs, max(0, gap)


def ref_doc_id(ref: dict) -> str:
    return f"{REF_PREFIX}:{ref['court']}-{ref['date']}-{ref['numbers'][0]}"


# ---------------------------------------------------------------- 추출(순수 함수: 입출력 없음)
def _case_no_set(s: str) -> set:
    toks = split_case_numbers(s)
    return set(toks) if toks else ({re.sub(r"\s+", "", s)} if s and s.strip() else set())


def header_text(fields: dict) -> str:
    """사건 머리 쪽 본문: 응답의 사건번호·선고일자·법원명(이 판결 자신의 것). 본문 필드에는 없어서 모델이 읽을 수 없던 값이다.
    선고일자는 판결문 표기(「2026. 2. 12.」)로 쓴다. 값이 하나도 없으면 빈 문자열(쪽을 만들지 않는다)."""
    no = _squash_ws(str(fields.get("사건번호") or "")).strip()
    raw_date = str(fields.get("선고일자") or "").strip()
    iso = _drf_ruling_date(raw_date)
    day = f"{int(iso[:4])}. {int(iso[5:7])}. {int(iso[8:])}." if iso else _squash_ws(raw_date)
    court = _squash_ws(str(fields.get("법원명") or "")).strip()
    lines = [f"{label}: {v}" for label, v in (("사건번호", no), ("선고일자", day), ("법원명", court)) if v]
    return "\n".join(lines)


def prefilter_text(fields: dict) -> str:
    """사전 필터가 보는 본문: 판례내용 + 판시사항 + 판결요지(참조조문·참조판례·머리 쪽은 보지 않는다)."""
    return "\n".join((html_to_text(fields.get("판례내용", "")), html_to_text(fields.get("판시사항", "")), html_to_text(fields.get("판결요지", ""))))


def build_extraction(fields: dict, doc: dict, list_extra: dict) -> dict:
    """파싱된 필드 → 페이지 목록·의견 분리 정보·참조판례·사전 필터·목록과의 대조. 네트워크·파일을 쓰지 않는다."""
    heads = {name: html_to_text(fields.get(name, "")) for name, _ in HEADNOTE_PAGES}
    judgment = html_to_text(fields.get("판례내용", ""))
    secs, op = split_sections(judgment) if judgment.strip() else ([], {"status": "none", "markers": [], "marker_count": 0})
    pages = []
    head = header_text(fields)
    if head:
        pages.append({"idx": -1, "loc": HEADER_LOC, "kind": "case_header", "field": "사건표지", "label": "사건표지", "opinion_kind": None,
                      "char_start": 0, "char_end": len(head), "text": head})
    for i, (name, kind) in enumerate(HEADNOTE_PAGES):
        if heads[name].strip():
            pages.append({"idx": i, "kind": kind, "field": name, "label": name, "opinion_kind": None, "char_start": 0,
                          "char_end": len(heads[name]), "text": heads[name]})
    for j, sec in enumerate(secs):
        if sec["text"].strip():
            pages.append({**sec, "idx": JUDGMENT_FIRST_IDX + j, "field": "판례내용"})
    unsplit = op["status"] == "unsplit"
    for p in pages:
        p["needs_review"] = bool(unsplit and p["kind"] in ("reasons", "opinion", "body"))
    refs, gap = parse_refs(heads["참조판례"])
    pf = relevance_prefilter(prefilter_text(fields))
    # 목록 행과 본문 헤더 대조(P1 계약이 P2 로 넘긴 재확인). 불일치는 숨기지 않고 기록한다.
    body_case = fields.get("사건번호", "").strip()
    body_iso = _drf_ruling_date(fields.get("선고일자"))
    body_court = fields.get("법원명", "").strip()
    mism = []
    body_nos = _case_no_set(body_case)
    # 목록의 사건번호가 병합 사건의 첫 번호만 담는 경우가 있다(2022다284711 / 본문 2022다284711, 284728). 목록 ⊆ 본문이면 불일치가 아니다.
    if list_extra.get("case_no") and not _case_no_set(list_extra["case_no"]) <= body_nos:
        mism.append({"field": "case_no", "list": list_extra["case_no"], "body": body_case})
    if doc.get("decision_date") and body_iso != doc["decision_date"]:
        mism.append({"field": "decision_date", "list": doc["decision_date"], "body": body_iso or fields.get("선고일자", "")})
    if list_extra.get("court") and body_court != list_extra["court"]:
        mism.append({"field": "court", "list": list_extra["court"], "body": body_court})
    warnings = []
    if not judgment.strip():
        warnings.append("empty_judgment_text")
    if gap:
        warnings.append(f"refs_parse_gap:{gap}")
    if unsplit:
        warnings.append("opinion_markers_not_found")
    return {"pages": pages, "opinion_split": op, "refs": refs, "refs_parse_gap": gap, "prefilter": pf, "mismatches": mism,
            "warnings": warnings, "fields_chars": {k: len(html_to_text(fields.get(k, ""))) for k in BODY_FIELDS},
            "judgment_type": fields.get("판결유형", "").strip(), "body_case_no": body_case, "case_nos": sorted(body_nos)}


def is_structure_candidate(extra: dict) -> bool:
    """구조화(DeepSeek) 대상인지: P-T1 처리 순위(확장분 포함)이거나 본문 사전 필터 통과(§8). 본문이 아직 없으면 False."""
    body = extra.get("body") or {}
    if not body.get("text_available"):
        return False
    return extra.get("processing_tier") == "P-T1" or bool((body.get("prefilter") or {}).get("passed"))


# ---------------------------------------------------------------- 저장(파일·장부)
def _atomic_write(path: Path, data: bytes) -> None:
    private_mkdir(path.parent)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _set_extra(manifest, doc_id: str, key: str, value: Any) -> None:
    """extra_json 의 한 키를 통째로 바꾼다(extra_merge 는 리스트를 합치므로 다시 추출한 값을 덮을 때는 이것을 쓴다)."""
    with manifest.transaction() as c:
        c.execute("UPDATE documents SET extra_json=json_set(COALESCE(extra_json,'{}'),?,json(?)) WHERE doc_id=?",
                  (f"$.{key}", json.dumps(value, ensure_ascii=False, sort_keys=True), doc_id))


def _doc_extra(doc: dict) -> dict:
    try:
        return json.loads(doc["extra_json"]) if doc.get("extra_json") else {}
    except ValueError:
        return {}


def page_loc(p: dict) -> str:
    """페이지 dict 의 위치 이름: 머리 쪽은 ``h000``, 나머지는 ``sNNN``."""
    return p.get("loc") or f"s{p['idx']:03d}"


def page_id_of(doc_id: str, idx: int) -> str:
    return f"{doc_id}#s{idx:03d}"


def persist_extraction(ctx, doc: dict, serial: str, raw_sha: str, raw_rel: str, fetched_at: str, built: dict, fmt: str) -> dict:
    """페이지 텍스트 파일을 쓰고, 파생 행을 지운 뒤 pages 를 다시 쓰고 단계 정보를 갱신한다(입력 해시나 단계 버전이 바뀐 재추출 포함)."""
    m, doc_id = ctx.manifest, doc["doc_id"]
    rel_dir = f"{TEXT_DIR}/{serial}"
    keep = set()
    if not ctx.dry_run:
        for p in built["pages"]:
            name = f"{page_loc(p)}.txt"
            keep.add(name)
            _atomic_write(ctx.corpus_dir / rel_dir / name, p["text"].encode("utf-8"))
        d = ctx.corpus_dir / rel_dir
        if d.is_dir():
            for f in d.glob("[sh][0-9][0-9][0-9].txt"):
                if f.name not in keep:
                    f.unlink()
    extra = _doc_extra(doc)
    body = {
        "fetched_at": fetched_at, "raw_path": raw_rel, "raw_sha256": raw_sha, "format": fmt, "extractor_version": EXTRACTOR_VERSION,
        "text_available": any(p["field"] == "판례내용" for p in built["pages"]), "pages": len(built["pages"]),
        "fields_chars": built["fields_chars"], "judgment_type": built["judgment_type"],
        "opinion_split": built["opinion_split"], "prefilter": built["prefilter"], "verify_mismatches": built["mismatches"],
        "case_nos": built["case_nos"], "body_case_no": built["body_case_no"], "refs": built["refs"], "refs_count": len(built["refs"]), "refs_parse_gap": built["refs_parse_gap"],
        "warnings": built["warnings"], "section_locator": "page_id suffix #sNNN (#h000 = case header); quality_json.char_start/char_end = offsets in the field text",
    }
    with m.transaction():
        m.delete_derived(doc_id=doc_id)
        for p in built["pages"]:
            loc = page_loc(p)
            m.upsert_page(
                f"{doc_id}#{loc}", doc_id, method="html_section", text_path=f"{rel_dir}/{loc}.txt",
                text_sha256=hashlib.sha256(p["text"].encode("utf-8")).hexdigest(), char_count=len(p["text"]),
                quality_json={"locator": loc, "section_kind": p["kind"], "label": p["label"], "field": p["field"],
                              "opinion_kind": p["opinion_kind"], "char_start": p["char_start"], "char_end": p["char_end"],
                              "serial": serial},
                needs_review=p["needs_review"], extractor_version=EXTRACTOR_VERSION)
        _set_extra(m, doc_id, "body", body)
        m.update_document_stage(doc_id, extractor_version=EXTRACTOR_VERSION, extract_input_sha256=raw_sha)
        m.set_document_status(doc_id, "extracted")
        m.resolve_failures(doc_id, "body")
    return body


# ---------------------------------------------------------------- 사건번호 색인(로컬 장부)
class _CaseIndex:
    """장부의 판례 문서를 (사건번호 토큰) → [(doc_id, 선고일 ISO)] 로 색인한다. 한 사건번호에 일련번호가 둘일 수 있으므로 모두 보관한다."""

    def __init__(self, manifest):
        self.map: dict = {}
        for r in manifest._q("SELECT doc_id, decision_date, json_extract(extra_json,'$.case_no') AS cn, "
                             "json_extract(extra_json,'$.body.case_nos') AS bn FROM documents WHERE source_id=?", (SOURCE_ID,)):
            self.add(r["doc_id"], r["cn"], r["decision_date"])
            if r["bn"]:                                  # 본문 헤더의 병합 사건번호도 색인한다
                self.add(r["doc_id"], ",".join(json.loads(r["bn"])), r["decision_date"])

    def add(self, doc_id: str, case_no: Optional[str], decision_date: Optional[str]) -> None:
        for t in split_case_numbers(case_no or ""):
            lst = self.map.setdefault(t, [])
            if (doc_id, decision_date) not in lst:
                lst.append((doc_id, decision_date))

    def find(self, numbers: list, iso_date: str) -> list:
        out = []
        for n in numbers:
            for doc_id, d in self.map.get(n, ()):
                if d == iso_date and doc_id not in out:
                    out.append(doc_id)
        return out


def nb_url(case_no: str, org: str, page: int = 1) -> str:
    q = [("target", "prec"), ("type", "JSON"), ("org", org), ("nb", case_no), ("display", "100"), ("page", str(page)), ("sort", "dasc")]
    return ENDPOINT + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


ORG_LOWER = "400202"


# ---------------------------------------------------------------- 실행
class _BodyRun:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.m = ctx.manifest
        self.today = ctx.now().date()
        self.limit = getattr(args, "limit", None)       # ctx.limit_pages(--limit-pages, 약어 --limit 도 걸린다)는 목록용이라 쓰지 않는다
        self.ref_depth = getattr(args, "ref_depth", None)
        self.max_ref_queries = getattr(args, "max_ref_queries", None)
        self.no_expand = bool(getattr(args, "no_expand", False))
        self.index: Optional[_CaseIndex] = None
        self.consecutive_failures = 0
        self.attempted: set = set()
        self.attempted_refs: set = set()
        self.depth_seen: set = set()        # 이번 실행에서 깊이 상한에 걸린 문서(상한을 올려 다시 실행하면 다시 확장 대상이 된다)
        self.limit_hit = False
        self.c = {"candidates_seen": 0, "fetched": 0, "resumed_from_raw": 0, "reextracted": 0, "refiltered": 0, "extracted": 0, "failed": 0,
                  "no_match": 0, "refetch_unchanged": 0, "skipped_exhausted": 0, "pages_written": 0, "mismatch_docs": 0, "ref_queries": 0,
                  "refs_enqueued": 0, "refs_resolved_local": 0, "refs_resolved_api": 0, "refs_referenced_only": 0,
                  "refs_failed": 0, "refs_new_docs": 0, "refs_depth_limited": 0, "refs_parse_gap_docs": 0}
        self.opinion = {"none": 0, "split": 0, "unsplit": 0}
        self.prefilter = {"passed": 0, "failed": 0}

    def case_index(self) -> _CaseIndex:
        if self.index is None:
            self.index = _CaseIndex(self.m)
        return self.index

    def bump_fail(self, what: str) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            raise IngestionError(f"{MAX_CONSECUTIVE_FAILURES} consecutive {what} failures; stopping (unverified)")

    # -- 한 판례
    def process(self, row: dict) -> None:
        ctx, m = self.ctx, self.m
        doc = m.get_document(row["doc_id"])
        doc_id = doc["doc_id"]
        serial = doc_id.split(":", 1)[1]
        extra = _doc_extra(doc)
        raw_rel = f"{RAW_DIR}/prec-{serial}.json"
        raw_path = ctx.corpus_dir / raw_rel
        raw = None
        prior = (extra.get("body") or {})
        if doc["status"] == "fetched" and prior.get("raw_sha256") and raw_path.is_file():
            data = raw_path.read_bytes()
            if hashlib.sha256(data).hexdigest() == prior["raw_sha256"]:
                raw, fetched_at = data, prior.get("fetched_at") or ""
                self.c["resumed_from_raw"] += 1
        if raw is None:
            ctx.check_time()
            try:
                r = ctx.http.get(body_url(serial), with_oc=True, accept=lambda b, h, s=serial: accept_body(b, h, s))
            except FetchFailed as e:
                self._fail(doc_id, "http", str(e), counts_toward_stop=True)
                return
            if not r.ok:
                reason = r.content_error or f"http_{r.status}"
                kind = reason.split(":", 1)[0]
                if r.content_error and kind == "no_match":
                    self._fail(doc_id, "no_match", reason, counts_toward_stop=False)
                else:
                    self._fail(doc_id, kind if r.content_error else f"http_{r.status}", reason, counts_toward_stop=True)
                return
            self.consecutive_failures = 0
            raw, fetched_at = r.body, ctx.now().isoformat(timespec="seconds")
            sha = hashlib.sha256(raw).hexdigest()
            if (doc["status"] in DONE_STATUSES and prior.get("raw_sha256") == sha and doc.get("extractor_version") == EXTRACTOR_VERSION):
                _set_extra(m, doc_id, "body", {**prior, "fetched_at": fetched_at})       # 입력 해시·단계 버전이 같으면 파생 행을 그대로 둔다
                self.c["refetch_unchanged"] += 1
                return
            if not ctx.dry_run:
                _atomic_write(raw_path, raw)
            with m.transaction():
                _set_extra(m, doc_id, "body", {**prior, "fetched_at": fetched_at, "raw_path": raw_rel, "raw_sha256": sha})
                m.set_document_status(doc_id, "fetched")
            self.c["fetched"] += 1
        self._extract(doc_id, serial, raw, fetched_at, raw_rel)

    def _extract(self, doc_id: str, serial: str, raw: bytes, fetched_at: str, raw_rel: str) -> None:
        m = self.m
        doc = m.get_document(doc_id)
        try:
            parsed = parse_body(raw)
        except BodyParseError as e:
            self._fail(doc_id, "raw_unparsable", f"{e.kind}: {e}", counts_toward_stop=False)
            return
        built = build_extraction(parsed["fields"], doc, _doc_extra(doc))
        body = persist_extraction(self.ctx, doc, serial, hashlib.sha256(raw).hexdigest(), raw_rel, fetched_at, built, parsed["format"])
        self.c["extracted"] += 1
        self.c["pages_written"] += body["pages"]
        self.opinion[built["opinion_split"]["status"]] += 1
        self.prefilter["passed" if built["prefilter"]["passed"] else "failed"] += 1
        if built["mismatches"]:
            self.c["mismatch_docs"] += 1
        if built["refs_parse_gap"]:
            self.c["refs_parse_gap_docs"] += 1

    def _fail(self, doc_id: str, error_class: str, message: str, *, counts_toward_stop: bool) -> None:
        m = self.m
        m.record_failure(doc_id, "body", error_class, message)
        m.set_document_status(doc_id, "failed")
        self.c["no_match" if error_class == "no_match" else "failed"] += 1
        if counts_toward_stop:
            self.bump_fail("body")

    def refilter_stale(self) -> None:
        """사전 필터 버전(FILTER_VERSION)이 바뀐 문서의 ``body.prefilter`` 만 저장된 raw 로 다시 계산한다(네트워크 없음, 쪽·사건 행은 건드리지 않는다).
        reextract_stale 는 추출 단계 버전만 보고 문서의 파생 행(pages/cases)을 지우므로 필터 변경에는 쓰지 않는다. raw 해시가 다르면 건드리지 않는다."""
        m, ctx = self.m, self.ctx
        rows = m._q("SELECT doc_id FROM documents WHERE source_id=? AND json_extract(extra_json,'$.body.raw_path') IS NOT NULL "
                    "AND COALESCE(json_extract(extra_json,'$.body.prefilter.version'),'')<>? ORDER BY doc_id", (SOURCE_ID, FILTER_VERSION))
        for r in rows:
            ctx.check_time()
            body = _doc_extra(m.get_document(r["doc_id"])).get("body") or {}
            p = ctx.corpus_dir / body["raw_path"]
            if not p.is_file():
                continue
            data = p.read_bytes()
            if hashlib.sha256(data).hexdigest() != body.get("raw_sha256"):
                continue
            try:
                fields = parse_body(data)["fields"]
            except BodyParseError:
                continue
            _set_extra(m, r["doc_id"], "body.prefilter", relevance_prefilter(prefilter_text(fields)))
            self.c["refiltered"] += 1

    # -- 선택
    def candidates(self, tiers: list, serials: Optional[list]) -> list:
        m = self.m
        exhausted = {f["target"] for f in m.failures() if f["stage"] == "body" and f["retry_count"] + 1 >= MAX_BODY_RETRIES}
        statuses = ("discovered", "failed", "fetched") + (DONE_STATUSES if self.ctx.refresh else ())     # --refresh: 완료분도 다시 받아 해시를 대조한다
        where = (f"source_id=? AND status IN ({','.join(repr(x) for x in statuses)}) "
                 "AND COALESCE(json_extract(extra_json,'$.source_scope'),'')<>'non_court_source'")
        params: list = [SOURCE_ID]
        if serials:
            where += f" AND doc_id IN ({','.join('?' * len(serials))})"
            params += [f"{DOC_PREFIX}:{s}" for s in serials]
        else:
            where += f" AND json_extract(extra_json,'$.processing_tier') IN ({','.join('?' * len(tiers))})"
            params += tiers
        rows = m._q(f"SELECT doc_id, status FROM documents WHERE {where} ORDER BY json_extract(extra_json,'$.processing_tier'), "
                    "COALESCE(decision_date,'9999-99-99'), doc_id", tuple(params))
        out = []
        for r in rows:
            if r["doc_id"] in self.attempted:
                continue
            if r["doc_id"] in exhausted:
                self.c["skipped_exhausted"] += 1
                continue
            out.append(dict(r))
        return out

    def reextract_stale(self) -> None:
        """단계 버전이 바뀐 문서를 저장된 raw 로 다시 추출한다(네트워크 없음). 입력 해시가 다르면 raw 를 새로 받아야 하므로 건드리지 않는다."""
        m, ctx = self.m, self.ctx
        rows = m._q("SELECT doc_id FROM documents WHERE source_id=? AND status IN ('extracted','structured','verified','indexed','quarantined') "
                    "AND extractor_version IS NOT NULL AND extractor_version<>? AND json_extract(extra_json,'$.body.raw_path') IS NOT NULL",
                    (SOURCE_ID, EXTRACTOR_VERSION))
        for r in rows:
            ctx.check_time()
            doc = m.get_document(r["doc_id"])
            body = _doc_extra(doc).get("body") or {}
            p = ctx.corpus_dir / body["raw_path"]
            if not p.is_file():
                continue
            data = p.read_bytes()
            if hashlib.sha256(data).hexdigest() != body.get("raw_sha256"):
                continue
            self._extract(doc["doc_id"], doc["doc_id"].split(":", 1)[1], data, body.get("fetched_at") or "", body["raw_path"])
            self.c["reextracted"] += 1
            self.c["extracted"] -= 1

    # -- 참조판례 확장
    def _mark_expanded(self, target_id: str, referrer: str, depth: int) -> None:
        m = self.m
        extra = _doc_extra(m.get_document(target_id))
        cur = extra.get("ref_depth")
        m.update_document_fields(target_id, extra_merge={"expanded_from": [referrer], "ref_depth": depth if cur is None else min(cur, depth)})
        extra = _doc_extra(m.get_document(target_id))
        ptier = processing_tier_of(extra.get("tier"), True, extra.get("ref_depth"))
        with m.transaction() as c:
            c.execute("UPDATE documents SET extra_json=json_set(extra_json,'$.processing_tier',?) WHERE doc_id=?", (ptier, target_id))

    def _ref_state(self, rid: str, state: str, status: str, **more: Any) -> None:
        self.m.update_document_fields(rid, extra_merge={"ref_state": state, "checked_at": self.ctx.now().isoformat(timespec="seconds"), **more})
        self.m.set_document_status(rid, status)

    def _resolve_to(self, rid: str, ref_extra: dict, doc_ids: list, via: str) -> None:
        for d in doc_ids:
            for referrer in ref_extra.get("cited_by", []):
                self._mark_expanded(d, referrer, ref_extra.get("ref_depth", 1))
        self._ref_state(rid, "resolved", "fetched", resolved_doc_ids=doc_ids, resolved_via=via)
        self.c["refs_resolved_local" if via == "local" else "refs_resolved_api"] += 1

    def enqueue_pass(self) -> int:
        """P-T1 처리 순위 문서의 참조판례를 큐(SOURCE_REFS 행)에 넣고, 이미 장부에 있는 판례는 네트워크 없이 바로 연결한다."""
        m = self.m
        rows = m._q("SELECT doc_id FROM documents WHERE source_id=? AND json_extract(extra_json,'$.processing_tier')='P-T1' "
                    "AND COALESCE(json_extract(extra_json,'$.body.refs_count'),0)>0 "
                    "AND COALESCE(json_extract(extra_json,'$.expand_state'),'')<>'done'", (SOURCE_ID,))
        touched = 0
        for r in rows:
            if r["doc_id"] in self.depth_seen:
                continue
            doc = m.get_document(r["doc_id"])
            extra = _doc_extra(doc)
            depth = int(extra.get("ref_depth") or 0) + 1
            if self.ref_depth is not None and depth > self.ref_depth:
                m.update_document_fields(doc["doc_id"], extra_merge={"expand_state": "depth_limited"})
                self.depth_seen.add(doc["doc_id"])
                self.c["refs_depth_limited"] += 1
                continue
            for ref in (extra.get("body") or {}).get("refs", []):
                touched += self._enqueue_ref(doc["doc_id"], ref, depth)
            m.update_document_fields(doc["doc_id"], extra_merge={"expand_state": "done"})
            touched += 1
        return touched

    def _enqueue_ref(self, referrer: str, ref: dict, depth: int) -> int:
        m = self.m
        rid = ref_doc_id(ref)
        existing = m.get_document(rid)
        if existing:
            ex = _doc_extra(existing)
            extra_new = {"cited_by": [referrer]}
            if depth < ex.get("ref_depth", depth):
                extra_new["ref_depth"] = depth
            m.update_document_fields(rid, extra_merge=extra_new)
            if ex.get("ref_state") == "resolved":          # 이미 해결된 참조: 새 인용자에게도 확장 표시를 전한다
                for d in ex.get("resolved_doc_ids", []):
                    self._mark_expanded(d, referrer, depth)
            return 0
        extra = {"ref_state": "queued", "court": ref["court"], "court_class": ref["court_class"], "date": ref["date"],
                 "numbers": ref["numbers"], "form": ref["form"], "kind": ref["kind"], "cited_by": [referrer], "ref_depth": depth,
                 "raw": ref["raw"], "attempts": 0}
        m.upsert_document(rid, SOURCE_REFS, title=ref["raw"], decision_date=ref["date"], year=int(ref["date"][:4]), doc_type="reference", extra=extra)
        self.c["refs_enqueued"] += 1
        if ref["court_class"] == "constitutional":
            self._ref_state(rid, "referenced_only", "withheld", reason="constitutional_court_decision_is_not_a_prec_target")
            self.c["refs_referenced_only"] += 1
        elif ref["court_class"] == "supreme":
            hit = self.case_index().find(ref["numbers"], ref["date"])
            if hit:
                self._resolve_to(rid, extra, hit, "local")
        return 1

    def resolve_api_pass(self) -> int:
        """큐에 남은 참조를 목록 API 사건번호(nb) 검색으로 해결한다. 본문 일련번호를 찾으면 SOURCE_ID 에 판례 행을 만든다(P-T1 처리 순위)."""
        ctx, m = self.ctx, self.m
        rows = m.find_by_json("documents", "extra_json", "ref_state", "queued", where={"source_id": SOURCE_REFS})
        changed = 0
        for r in rows:
            rid = r["doc_id"]
            if rid in self.attempted_refs:
                continue
            if self.max_ref_queries is not None and self.c["ref_queries"] >= self.max_ref_queries:
                break
            self.attempted_refs.add(rid)
            ex = _doc_extra(r)
            hit = self.case_index().find(ex["numbers"], ex["date"]) if ex["court_class"] == "supreme" else []
            if hit:
                self._resolve_to(rid, ex, hit, "local")
                changed += 1
                continue
            org = ORG_SUPREME if ex["court_class"] == "supreme" else ORG_LOWER
            found, failed = [], False
            for n in ex["numbers"][:MAX_REF_NUMBERS_PER_QUERY]:
                ctx.check_time()
                got = self._search_case_no(n, org)
                if got is None:
                    failed = True
                    break
                found = [it for it in got if n in split_case_numbers(str(it.get("사건번호") or "")) and
                         _drf_ruling_date(it.get("선고일자")) == ex["date"]]
                if found:
                    break
            if failed:
                m.record_failure(rid, "ref", "search_failed", "nb search failed")
                m.update_document_fields(rid, extra_merge={"attempts": int(ex.get("attempts", 0)) + 1})
                self.c["refs_failed"] += 1
                self.bump_fail("ref search")
                continue
            self.consecutive_failures = 0
            m.resolve_failures(rid, "ref")
            if found:
                ids = [self._upsert_ref_item(it, ex) for it in found]
                self._resolve_to(rid, ex, ids, "api")
            else:
                self._ref_state(rid, "referenced_only", "withheld", reason="not_found_by_nb_search")
                self.c["refs_referenced_only"] += 1
            changed += 1
        return changed

    def _search_case_no(self, case_no: str, org: str) -> Optional[list]:
        ctx = self.ctx
        self.c["ref_queries"] += 1
        try:
            r = ctx.http.get(nb_url(case_no, org), with_oc=True, accept=accept_list)
        except FetchFailed:
            return None
        if not r.ok:
            ctx.manifest.record_failure(r.url, "ref_list", r.content_error or f"http_{r.status}", "nb search page not usable")
            return None
        ctx.manifest.resolve_failures(r.url, "ref_list")
        return parse_list(r.body)[1]

    def _upsert_ref_item(self, it: dict, ref_extra: dict) -> str:
        """nb 검색으로 찾은 목록 행을 SOURCE_ID 문서로 만든다(없을 때만). 적중 검색어가 없는 행이라 relevance 는 referenced 다."""
        m = self.m
        serial = str(it.get("판례일련번호", "")).strip()
        doc_id = f"{DOC_PREFIX}:{serial}"
        if m.get_document(doc_id) is None:
            iso, dstatus = classify_date(it.get("선고일자"), self.today)
            link = str(it.get("판례상세링크") or "")
            detail = strip_oc("https://www.law.go.kr" + link) if link.startswith("/") else (strip_oc(link) if link else None)
            ex = {"relevance": "referenced", "matched_terms": [], "serial": serial, "case_no": str(it.get("사건번호") or "").strip(),
                  "court": str(it.get("법원명") or "").strip(), "case_kind": str(it.get("사건종류명") or "").strip(),
                  "case_kind_code": str(it.get("사건종류코드") or "").strip(), "verdict_form": str(it.get("선고") or "").strip(),
                  "data_source": str(it.get("데이터출처명") or "").strip(), "raw_date": str(it.get("선고일자") or ""), "date_status": dstatus,
                  "discovered_via": "reference", "tier": None}
            ex["source_scope"] = source_scope(ex["court"], ex["data_source"])
            m.upsert_document(doc_id, SOURCE_ID, canonical_url=PUBLIC_VIEW + serial, title=str(it.get("사건명") or "").strip() or None,
                              decision_date=iso, year=int(iso[:4]) if iso else None, doc_type=str(it.get("판결유형") or "").strip() or None,
                              detail_url=detail, extra=ex)
            self.c["refs_new_docs"] += 1
            self.case_index().add(doc_id, ex["case_no"], iso)
        return doc_id


def add_body_arguments(parser) -> None:
    parser.add_argument("--tier", action="append", dest="tiers", help="처리 순위 T1|T2|T3 (반복 가능, 기본 T1). 순위는 §8 정의(extra.processing_tier)")
    parser.add_argument("--limit", "--max-docs", dest="limit", type=int, default=None,
                        help="이번 실행에서 새로 받을 본문 수 상한. 목록용 --limit-pages 와 별개이며 이 모드는 --limit-pages 를 읽지 않는다")
    parser.add_argument("--serial", action="append", dest="serials", help="판례일련번호를 직접 지정(순위 무시, 장부에 있는 문서만). 게이트 표본용")
    parser.add_argument("--ref-depth", type=int, default=None, help="참조판례 확장 깊이 상한(기본: 제한 없음)")
    parser.add_argument("--no-expand", action="store_true", help="참조판례 사건번호 검색(API)을 하지 않는다. 장부 안 연결은 한다")
    parser.add_argument("--max-ref-queries", type=int, default=None, help="이번 실행의 사건번호 검색 요청 수 상한")


def run_body(ctx, args) -> dict:
    m = ctx.manifest
    m.ensure_source(SOURCE_ID, FAMILY, "법제처 국가법령정보 판례(대법원, 본문검색 사전)", ENDPOINT + "?target=prec&org=" + ORG_SUPREME)
    m.ensure_source(SOURCE_REFS, FAMILY, "법제처 판례 참조판례 포인터(본문 문서 아님)", ENDPOINT + "?target=prec&nb=",
                    notes="참조판례 큐. ref_state: queued | resolved | referenced_only. 상태열: discovered | fetched | withheld")
    tiers = [normalize_tier(t) for t in (getattr(args, "tiers", None) or ["T1"])]
    serials = [str(s).strip() for s in (getattr(args, "serials", None) or [])]
    bad = [s for s in serials if not s.isdigit()]
    if bad:
        raise IngestionError(f"invalid --serial values: {bad}")
    run = _BodyRun(ctx, args)
    tier_info = assign_tiers(m)
    missing = [s for s in serials if m.get_document(f"{DOC_PREFIX}:{s}") is None]
    state_path = ctx.inventory_dir / "lawgo_prec_body.json"
    try:
        run.reextract_stale()
        run.refilter_stale()
        while True:
            did = 0
            for row in run.candidates(tiers, serials or None):
                if run.limit is not None and run.c["fetched"] >= run.limit:
                    run.limit_hit = True
                    break
                run.c["candidates_seen"] += 1
                run.attempted.add(row["doc_id"])
                run.process(row)
                did += 1
            enq = run.enqueue_pass()
            api = 0
            if not run.no_expand:                       # 본문 수 상한(--limit)과 별개로 사건번호 검색은 --max-ref-queries·시간 상한이 제한한다
                api = run.resolve_api_pass()
            if run.limit_hit or not (did or enq or api):
                break
    finally:
        m.touch_source_checked(SOURCE_ID)
        m.touch_source_checked(SOURCE_REFS)
        if not ctx.dry_run:
            summary = {"updated_at": ctx.now().isoformat(timespec="seconds"), "tiers_requested": tiers, "counts": run.c,
                       "opinion_split": run.opinion, "prefilter": run.prefilter, **tier_info, "extractor_version": EXTRACTOR_VERSION}
            _write_terms_file(state_path, summary)
    docs_by_status = {r["status"]: r["n"] for r in m.count_by("documents", "status", where={"source_id": SOURCE_ID})}
    ref_states = {r["st"]: r["n"] for r in m._q(
        "SELECT json_extract(extra_json,'$.ref_state') AS st, COUNT(*) AS n FROM documents WHERE source_id=? GROUP BY 1", (SOURCE_REFS,))}
    return {"tiers_requested": tiers, "serials_missing": missing, "limit": run.limit, "limit_hit": run.limit_hit, **run.c, "opinion_split": run.opinion,
            "prefilter": run.prefilter, **tier_info, "ref_states": ref_states, "documents_by_status": docs_by_status,
            "state_file": str(state_path) if not ctx.dry_run else None}

"""법제처 헌재결정례(DRF target=detc) 번호 해석 어댑터: ``python -m press_review.ingestion lawapi_detc --number 2005헌마165 [--merged-search]``.

사건번호 → 헌재결정례일련번호를 해석해 문서(status discovered)로 등록한다. 본문 수신은 ``lawapi_detc_body`` 가 한다(설계 D2.3·D2.8, 반증 7·11·16).
nb 검색은 유사 일치라 다른 번호를 돌려주므로(88헌마22 질의에 2022헌마688 포함) 목록 사건번호를 ``split_case_numbers`` 토큰으로 쪼개 정확 일치만 쓴다.

해석 순서: ① 번호 정규화(형식 밖은 malformed_number, 자동 보정 없음) → ② lawSearch nb 목록(totalCnt 만큼, 상한 ``--max-pages``) 정확 일치
→ ③ 0건이면 이미 받은 detc 문서의 ``body.case_nos`` 로컬 색인 → ④ 로컬에도 없으면 ``deferred_local``(retry_count 0 고정; 본문 수신 뒤 다시 해석)
→ ⑤ ``--merged-search`` 일 때만 본문검색(search=2) 상위 5건의 본문을 받아 【당 사 자】 구간 번호로 병합 검증(판시사항 머리 번호는 근거가 아니다).

실패 장부: target ``lawgo_detc_no:<번호>``, stage ``resolve``, error_class = deferred_local | not_found | merged_unverified | page_overflow |
malformed_number | date_mismatch | http_*. 해석 성공 번호는 문서 extra ``resolved_numbers``(와 해석 경로 ``resolved_via``)에 남는다.
권리 게이트: 소스 등록 뒤 요청 전에 ``require_source_rights(read, store_raw)``. 통과하지 못하면 error_class ``rights_not_recorded`` 로 중단한다.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
import urllib.parse
from typing import Any, Optional

from ..http import FetchFailed, IngestionError
from ..rights import RightsNotRecorded, require_source_rights
from . import lawapi as L
from .detc_parse import accept_detc_body, extract_merged_numbers, parse_detc_date, parse_detc_xml

SOURCE_ID = "lawgo_detc"
FAMILY = L.FAMILY                       # law_go_kr (새 family 를 만들지 않는다)
DOC_PREFIX = "lawgo_detc"
ENDPOINT = L.ENDPOINT
BODY_ENDPOINT = L.BODY_ENDPOINT
PUBLIC_VIEW = "https://www.law.go.kr/LSW/detcInfoP.do?detcSeq="      # OC 없는 공개 주소(목록의 상세링크는 OC 되울림이라 저장하지 않는다)
RAW_DIR = L.RAW_DIR
TEXT_DIR = "text/lawgo_detc"
RESOLVE_FILE = "lawgo_detc_resolve.json"
MAX_RESOLVE_RETRIES = 3
MERGED_CANDIDATES = 5
DISPLAY = 100
NUMBER_RE = re.compile(r"\d{2,4}헌[가나다라마바사아]\d+")
FAIL_STAGE = "resolve"
FAIL_PREFIX = "lawgo_detc_no:"
NO_RETRY_CLASSES = ("deferred_local", "malformed_number")            # retry_count 0 고정: 재시도 소진에 넣지 않는다


class RightsStop(IngestionError):
    """소스 권리(read·store_raw)가 allowed 로 기록되지 않았다. CLI 는 status=failed, error_class=rights_not_recorded, 종료 코드 1 로 낸다."""
    error_class = "rights_not_recorded"


def open_source(manifest) -> None:
    """등록 → 권리 게이트. 소스 행이 없는 첫 실행에서도 소스가 먼저 등록된 뒤 중단된다(ensure_source 는 자체 트랜잭션으로 커밋한다)."""
    manifest.ensure_source(SOURCE_ID, FAMILY, "법제처 국가법령정보 헌재결정례", ENDPOINT + "?target=detc",
                           notes="DRF lawSearch/lawService target=detc; 번호 해석(nb)·본문 XML. 권리는 `rights set law_go_kr --source lawgo_detc` 로 소스 한정 기록")
    try:
        require_source_rights(manifest, SOURCE_ID, needs=("read", "store_raw"))
    except RightsNotRecorded as e:
        raise RightsStop(str(e)) from None


# ---------------------------------------------------------------- 번호·URL·목록 파싱
def normalize_number(raw: Any) -> Optional[str]:
    """NFKC·공백 제거·끝 '등' 제거 후 형식 검사. 형식 밖이면 None(자동 보정 금지)."""
    s = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(raw or "")))
    s = re.sub(r"등$", "", s)
    return s if NUMBER_RE.fullmatch(s) else None


def _enc(q: list) -> str:
    return urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def list_url(number: str, page: int) -> str:
    """파라미터 순서는 캐시 키라 고정. OC 는 클라이언트가 주입한다(with_oc=True)."""
    return ENDPOINT + "?" + _enc([("target", "detc"), ("type", "JSON"), ("nb", number), ("display", str(DISPLAY)), ("page", str(page))])


def merged_url(number: str) -> str:
    return ENDPOINT + "?" + _enc([("target", "detc"), ("type", "JSON"), ("search", "2"), ("query", number),
                                  ("display", str(MERGED_CANDIDATES)), ("page", "1")])


def body_url(serial: str) -> str:
    return BODY_ENDPOINT + "?" + _enc([("target", "detc"), ("ID", serial), ("type", "XML")])


class ListParseError(ValueError):
    pass


def parse_detc_list(body: bytes) -> tuple[int, list[dict]]:
    """(totalCnt, items). 봉투 ``DetcSearch``, 항목 ``Detc`` 는 0건이면 없고 1건이면 dict 일 수 있다. 숫자는 문자열로 온다."""
    try:
        j = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ListParseError("body is not JSON") from None
    env = j.get("DetcSearch") if isinstance(j, dict) else None
    if not isinstance(env, dict):
        raise ListParseError("no DetcSearch envelope")
    try:
        total = int(str(env.get("totalCnt")).strip())
    except (TypeError, ValueError):
        raise ListParseError("totalCnt is not an integer") from None
    if total < 0:
        raise ListParseError("negative totalCnt")
    d = env.get("Detc")
    items = [] if d is None else ([d] if isinstance(d, dict) else d)
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        raise ListParseError("unexpected Detc type")
    return total, items


def accept_detc_list(body: bytes, headers: dict) -> Optional[str]:
    try:
        parse_detc_list(body)
    except ListParseError as e:
        return f"not a detc list response: {e}"
    return None


def exact_items(number: str, items: list) -> list:
    """목록 항목 중 사건번호 토큰 집합에 ``number`` 가 있는 것만(부분문자열 금지)."""
    out = []
    for it in items:
        toks = L.split_case_numbers(unicodedata.normalize("NFKC", str(it.get("사건번호") or "")))
        if number in toks and str(it.get("헌재결정례일련번호", "")).strip().isdigit():
            out.append(it)
    return out


# ---------------------------------------------------------------- 실행
def add_arguments(parser) -> None:
    parser.add_argument("--number", action="append", dest="numbers", metavar="N",
                        help="사건번호(반복 가능). 예: python -m press_review.ingestion lawapi_detc --number 2005헌마165 --number 93헌가13")
    parser.add_argument("--numbers-file", default=None, help="줄당 번호 하나(빈 줄·# 시작 줄 무시)")
    parser.add_argument("--max-pages", type=int, default=3, help="번호당 목록 쪽 수 상한(쪽당 100건). 넘으면 page_overflow 로 기록하고 등록하지 않는다(기본 3)")
    parser.add_argument("--merged-search", action="store_true",
                        help="정확 일치·로컬 해석에 실패한 번호만 본문검색(search=2) 상위 5건의 본문을 받아 병합 번호로 검증한다")
    parser.add_argument("--retry-not-found", action="store_true", help="이전에 not_found 로 기록된 번호도 다시 해석한다(기본은 건너뜀)")


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
        self.max_pages = max(1, int(getattr(args, "max_pages", 3) or 3))
        self.merged = bool(getattr(args, "merged_search", False))
        self.retry_nf = bool(getattr(args, "retry_not_found", False))
        self.consecutive_failures = 0
        self.c = {"numbers_input": 0, "duplicates": 0, "malformed_number": 0, "skipped_resolved": 0, "skipped_exhausted": 0, "skipped_not_found": 0,
                  "resolved_exact": 0, "resolved_local": 0, "resolved_merged": 0, "deferred_local": 0, "not_found": 0, "merged_unverified": 0,
                  "page_overflow": 0, "date_mismatch": 0, "http_failed": 0, "multi_serial_numbers": 0, "docs_new": 0, "docs_seen": 0,
                  "requests": 0, "bad_items": 0}

    # -- HTTP
    def get(self, url: str, accept):
        """(Response|None, error_class|None). 연속 실패 MAX_CONSECUTIVE_FAILURES 회면 중단한다."""
        ctx = self.ctx
        ctx.check_time()
        self.c["requests"] += 1
        try:
            r = ctx.http.get(url, with_oc=True, accept=accept)
        except FetchFailed:
            r = None
        if r is None or not r.ok:
            self.consecutive_failures += 1
            if self.consecutive_failures >= L.MAX_CONSECUTIVE_FAILURES:
                raise IngestionError(f"{L.MAX_CONSECUTIVE_FAILURES} consecutive resolve failures; stopping (unverified)")
            return None, ("http_fetch_failed" if r is None else (f"http_{r.status}" if not r.content_error else "http_content_rejected"))
        self.consecutive_failures = 0
        return r, None

    # -- 장부
    def fail(self, number: str, error_class: str, message: str) -> None:
        self.m.record_failure(FAIL_PREFIX + number, FAIL_STAGE, error_class, message,
                              retry_count=0 if error_class in NO_RETRY_CLASSES else None)
        self.c["http_failed" if error_class.startswith("http_") else error_class] += 1

    def ref_dates(self, number: str) -> set:
        """참조판례 장부(lawgo_ref 행)가 이 번호의 헌재 결정일로 적어 둔 날짜 집합. ref_doc_id 는 첫 번호로만 키를 만들므로 extra.numbers 도 본다."""
        rows = self.m._q("SELECT d.decision_date AS dt FROM documents d, json_each(d.extra_json,'$.numbers') j WHERE d.source_id=? "
                         "AND json_extract(d.extra_json,'$.court_class')='constitutional' AND j.value=?", (L.SOURCE_REFS, number))
        return {r["dt"] for r in rows if r["dt"]}

    def date_gate(self, number: str, cands: list) -> tuple:
        """cands: [(serial, iso|None, payload)]. 장부에 날짜가 있으면 같은 날짜 후보만 남긴다. 후보 날짜를 모르면 비교할 수 없어 남기지 않는다."""
        refs = self.ref_dates(number)
        if not refs:
            return cands, False
        kept = [c for c in cands if c[1] in refs]
        return kept, not kept

    def register(self, number: str, serial: str, it: dict, iso: Optional[str], multi: bool, via: str) -> None:
        m = self.m
        doc_id = f"{DOC_PREFIX}:{serial}"
        extra = {"serial": serial, "case_no": str(it.get("사건번호") or "").strip(), "court": "헌법재판소",
                 "raw_date": str(it.get("종국일자") or ""), "resolved_numbers": [number], "resolved_via": {number: via},
                 **({"multi_serial": True} if multi else {})}
        res = m.upsert_document(doc_id, SOURCE_ID, canonical_url=PUBLIC_VIEW + serial, title=L._squash_ws(str(it.get("사건명") or "")) or None,
                                decision_date=iso, year=int(iso[:4]) if iso else None, extra=extra)
        if res == "new":
            self.c["docs_new"] += 1
        else:
            self.c["docs_seen"] += 1
            m.update_document_fields(doc_id, extra_merge={"resolved_numbers": [number], "resolved_via": {number: via},
                                                          **({"multi_serial": True} if multi else {})})

    def link(self, number: str, cands: list, via: str) -> bool:
        """날짜 대조를 거쳐 문서를 등록한다. 성공이면 True, date_mismatch 로 기록했으면 False."""
        kept, mismatch = self.date_gate(number, cands)
        if mismatch:
            self.fail(number, "date_mismatch", "ledger decision date differs from every candidate (not linked)")
            return False
        multi = len(kept) >= 2
        with self.m.transaction():
            for serial, iso, it in kept:
                self.register(number, serial, it, iso, multi, via)
            self.m.resolve_failures(FAIL_PREFIX + number, FAIL_STAGE)
        self.c["multi_serial_numbers"] += int(multi)
        return True

    # -- ② 정확 일치
    def exact(self, number: str):
        """('ok', cands) | ('fail', error_class) | ('overflow', None). 한 쪽이라도 실패하면 실패(부분 결과로 등록하지 않는다)."""
        r, err = self.get(list_url(number, 1), accept_detc_list)
        if r is None:
            return "fail", err
        total, items = parse_detc_list(r.body)
        pages = max(1, math.ceil(total / DISPLAY))
        if pages > self.max_pages:
            return "overflow", None
        for p in range(2, pages + 1):
            r, err = self.get(list_url(number, p), accept_detc_list)
            if r is None:
                return "fail", err
            items += parse_detc_list(r.body)[1]
        hits = exact_items(number, items)
        self.c["bad_items"] += sum(1 for i in items if not str(i.get("헌재결정례일련번호", "")).strip().isdigit())
        return "ok", [(str(i["헌재결정례일련번호"]).strip(), parse_detc_date(i.get("종국일자")), i) for i in hits]

    # -- ③ 로컬
    def local(self, number: str) -> list:
        rows = self.m._q("SELECT DISTINCT d.doc_id AS doc_id, d.decision_date AS dt FROM documents d, json_each(d.extra_json,'$.body.case_nos') j "
                         "WHERE d.source_id=? AND j.value=? ORDER BY d.doc_id", (SOURCE_ID, number))
        return [(r["doc_id"].split(":", 1)[1], r["dt"], None) for r in rows]

    def link_local(self, number: str, cands: list) -> bool:
        kept, mismatch = self.date_gate(number, cands)
        if mismatch:
            self.fail(number, "date_mismatch", "ledger decision date differs from every local candidate (not linked)")
            return False
        multi = len(kept) >= 2
        with self.m.transaction():
            for serial, _iso, _ in kept:
                self.m.update_document_fields(f"{DOC_PREFIX}:{serial}", extra_merge={
                    "resolved_numbers": [number], "resolved_via": {number: "local"}, **({"multi_serial": True} if multi else {})})
            self.m.resolve_failures(FAIL_PREFIX + number, FAIL_STAGE)
        self.c["multi_serial_numbers"] += int(multi)
        return True

    # -- ⑤ 병합 검색
    def merged_search(self, number: str) -> None:
        r, err = self.get(merged_url(number), accept_detc_list)
        if r is None:
            self.fail(number, err, "merged-search list not usable")
            return
        cands = [i for i in parse_detc_list(r.body)[1][:MERGED_CANDIDATES] if str(i.get("헌재결정례일련번호", "")).strip().isdigit()]
        verified, unverifiable, mention, conflict = [], False, False, False
        for it in cands:
            serial = str(it["헌재결정례일련번호"]).strip()
            br, berr = self.get(body_url(serial), lambda b, h, s=serial: accept_detc_body(b, h, s))
            if br is None:
                unverifiable = True
                continue
            try:
                parsed = parse_detc_xml(br.body, serial)
            except L.BodyParseError:
                unverifiable = True
                continue
            ft = parsed["full_text"] or ""
            party = extract_merged_numbers(ft)
            if number in party or number in parsed["case_nos"]:       # 근거는 【당 사 자】 구간·대표 번호뿐. 판시사항 머리의 번호는 근거가 아니다
                list_iso, body_iso = parse_detc_date(it.get("종국일자")), parsed["decision_date"]
                refs = self.ref_dates(number)
                if (list_iso and body_iso and list_iso != body_iso) or (refs and any(d and d not in refs for d in (list_iso, body_iso))):
                    conflict = True                                    # 목록·본문·참조 장부 날짜 중 하나라도 어긋나면 연결하지 않는다
                    continue
                verified.append((serial, list_iso or body_iso, it))
                continue
            if not party:
                unverifiable = True                                    # 당사자 구간을 정할 수 없어 부재도 확인하지 못했다
            hay = re.sub(r"\s+", "", unicodedata.normalize("NFKC", ft + (parsed["headnote_issue"] or "") + (parsed["headnote_holding"] or "")))
            mention = mention or number in hay
        if verified:
            if self.link(number, verified, "merged"):
                self.c["resolved_merged"] += 1
        elif conflict:
            self.fail(number, "date_mismatch", "list, body and ledger decision dates disagree (not linked)")
        elif unverifiable or mention:
            self.fail(number, "merged_unverified", "candidates exist but the number is not in any verified party section")
        else:
            self.fail(number, "not_found", "no candidate lists the number")

    # -- 전체
    def run(self, raw_numbers: list) -> None:
        m = self.m
        resolved = {r["v"] for r in m._q("SELECT j.value AS v FROM documents d, json_each(d.extra_json,'$.resolved_numbers') j WHERE d.source_id=?", (SOURCE_ID,))}
        fails = {f["target"]: f for f in m.failures() if f["stage"] == FAIL_STAGE and f["target"].startswith(FAIL_PREFIX)}
        todo, seen = [], set()
        for raw in raw_numbers:
            self.c["numbers_input"] += 1
            n = normalize_number(raw)
            if n is None:
                key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(raw)))[:60]
                if key not in seen:
                    seen.add(key)
                    self.fail(key, "malformed_number", "not a constitutional court case number (no automatic correction)")
                else:
                    self.c["duplicates"] += 1
                continue
            if n in seen:
                self.c["duplicates"] += 1
                continue
            seen.add(n)
            f = fails.get(FAIL_PREFIX + n)
            if n in resolved:
                self.c["skipped_resolved"] += 1
            elif f and f["error_class"] == "not_found" and not self.retry_nf:
                self.c["skipped_not_found"] += 1
            elif f and f["error_class"] not in NO_RETRY_CLASSES and f["error_class"] != "not_found" and f["retry_count"] + 1 >= MAX_RESOLVE_RETRIES:
                self.c["skipped_exhausted"] += 1
            else:
                todo.append(n)
        pending = []
        for n in todo:                                   # ② 정확 일치
            kind, val = self.exact(n)
            if kind == "fail":
                self.fail(n, val, "list page not usable")
            elif kind == "overflow":
                self.fail(n, "page_overflow", f"totalCnt exceeds {self.max_pages} pages (nothing registered)")
            elif val:
                if self.link(n, val, "exact"):
                    self.c["resolved_exact"] += 1
            else:
                pending.append(n)
        rest = []
        for n in pending:                                # ③ 로컬(이미 받은 문서의 body.case_nos)
            loc = self.local(n)
            if loc:
                if self.link_local(n, loc):
                    self.c["resolved_local"] += 1
            else:
                rest.append(n)
        for n in rest:                                   # ④ 지연 / ⑤ 병합 검색
            if self.merged:
                self.merged_search(n)
            else:
                self.fail(n, "deferred_local", "no exact list match and no local body.case_nos match yet (fetch bodies, then rerun)")


def run(ctx, args) -> dict:
    m = ctx.manifest
    open_source(m)
    numbers = collect_numbers(args)
    r = _Run(ctx, args)
    try:
        r.run(numbers)
    finally:
        m.touch_source_checked(SOURCE_ID)
        if not ctx.dry_run:
            L._write_terms_file(ctx.inventory_dir / RESOLVE_FILE, {"updated_at": ctx.now().isoformat(timespec="seconds"), "counts": r.c,
                                                                  "max_pages": r.max_pages, "merged_search": r.merged})
    docs = {x["status"]: x["n"] for x in m.count_by("documents", "status", where={"source_id": SOURCE_ID})}
    return {**r.c, "documents_by_status": docs, "state_file": None if ctx.dry_run else str(ctx.inventory_dir / RESOLVE_FILE)}

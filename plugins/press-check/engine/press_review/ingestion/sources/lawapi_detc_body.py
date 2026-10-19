"""법제처 헌재결정례 본문 수신·추출 CLI: ``python -m press_review.ingestion lawapi_detc_body --serial 137285 [--limit N]``.

``lawapi_detc`` 가 등록한 문서(헌재결정례일련번호)의 본문을 lawService target=detc type=XML 로 받아 raw(``raw/law_go_kr/detc-<일련번호>.xml``)로 저장하고,
머리 쪽(h000) + 결정요지 인라인 분할 + 전문 구간 쪽(``text/lawgo_detc/<일련번호>/``)으로 추출한다. 파서·분할기·주문 분류기는 ``detc_parse.py``(U2-1).
이어받기(raw sha 일치 시 재수신 없음)·연속 실패 정지·failures stage=body 는 판례 본문(``lawapi.py`` '본문' 절)과 같은 규칙이다.

【당 사 자】 구간(no_page=True)은 쪽으로 만들지 않고 raw 에만 남는다. 거기서는 병합 사건번호 토큰만 뽑아 ``body.case_nos``(= 대표 번호 토큰 + 병합 번호 토큰)와
``body.merged_numbers`` 에 둔다(ledger_values 가 case_nos 를 그대로 읽는다). opinion_status 가 unsplit|needs_review 인 문서는 전문의 reasons·opinion·heading 쪽,
결정요지 분할이 unsplit|needs_review 이면 결정요지 쪽에 needs_review 플래그를 단다(opinion_kind 가 None 인 쪽의 인용은 후속 단계가 court_majority 로 쓰지 않는다).
권리 게이트는 ``lawapi_detc.open_source`` 와 같다(등록 → require_source_rights → 요청).
"""
from __future__ import annotations

import hashlib
from typing import Optional

from ..http import FetchFailed, IngestionError
from . import lawapi as L
from . import lawapi_detc as D
from .detc_parse import (STATUS_REVIEW, STATUS_UNSPLIT, accept_detc_body, build_head_page, classify_orders, extract_merged_numbers,
                         parse_detc_xml, split_detc, split_head_opinions)

EXTRACTOR_VERSION = "lawgo-detc-xml-2"       # 쪽 구성 규칙을 바꾸면 올린다. 2: 구형 [당사자]/[주 문]/[이 유] 표지 인식.
STATE_FILE = "lawgo_detc_body.json"
HEAD_PAGES = (("판시사항", "headnote_issue", 0), ("결정요지", "headnote_holding", 1), ("참조조문", "ref_statutes", 2),
              ("참조판례", "ref_cases", 3), ("심판대상조문", "review_target", 4))      # 머리 필드는 비어도 번호를 비워 둔다
JUDGMENT_FIRST_IDX = 5
HEAD_OPINION_FIRST_IDX = 500                                                          # 결정요지 안의 의견 구간 쪽(개수가 가변이라 전문 번호를 밀지 않는다)
FLAG_STATUSES = (STATUS_UNSPLIT, STATUS_REVIEW)


def raw_rel_of(serial: str) -> str:
    return f"{D.RAW_DIR}/detc-{serial}.xml"


# ---------------------------------------------------------------- 추출(순수 함수: 입출력 없음)
def build_extraction(parsed: dict, doc: dict, list_extra: dict) -> dict:
    """parse_detc_xml 결과 → 쪽 목록·본문 메타. 네트워크·파일을 쓰지 않는다."""
    warnings = list(parsed["warnings"])
    full = parsed["full_text"] or ""
    secs, info = split_detc(full) if full.strip() else ([], {"status": "none", "marker_count": 0, "loose_count": 0, "conclusion_numbers": [], "markers": []})
    merged = extract_merged_numbers(full) if full else []
    rep = list(parsed["case_nos"])
    body_case_no = parsed["body_case_no"]
    if not rep:
        warnings.append("body_case_no_missing")
        rep = L.split_case_numbers(str(list_extra.get("case_no") or ""))
    case_nos = list(dict.fromkeys(rep + merged))
    order = next((s.text for s in secs if s.kind == "order"), "")
    pages: list[dict] = []
    head = build_head_page(body_case_no or (rep[0] if rep else None), parsed["decision_date"])
    if head:
        pages.append({"idx": -1, "loc": L.HEADER_LOC, "kind": "case_header", "field": "사건표지", "label": "사건표지", "opinion_kind": None,
                      "char_start": 0, "char_end": len(head), "text": head, "needs_review": False})
    hsecs, hinfo = split_head_opinions(parsed["headnote_holding"])
    h_flag = hinfo["headnote_split"] in FLAG_STATUSES
    for name, key, idx in HEAD_PAGES:
        txt = parsed[key]
        if not txt or not txt.strip():
            continue
        if key != "headnote_holding" or not hsecs:
            pages.append({"idx": idx, "kind": key, "field": name, "label": name, "opinion_kind": None, "char_start": 0, "char_end": len(txt),
                          "text": txt, "needs_review": bool(key == "headnote_holding" and h_flag)})
            continue
        k = 0
        for sec in hsecs:
            if not sec.text.strip():
                continue
            is_op = sec.kind == "opinion"
            pages.append({"idx": HEAD_OPINION_FIRST_IDX + k if is_op else idx, "kind": "headnote_opinion" if is_op else key, "field": name,
                          "label": sec.label or name, "opinion_kind": None if h_flag else sec.opinion_kind, "vote": sec.vote,
                          "char_start": sec.char_start, "char_end": sec.char_end, "text": sec.text, "needs_review": h_flag})
            k += 1 if is_op else 0
    flag = info["status"] in FLAG_STATUSES
    for j, sec in enumerate(secs):
        if sec.no_page or not sec.text.strip():
            continue                                              # 【당 사 자】 는 raw 에만 남는다
        pages.append({"idx": JUDGMENT_FIRST_IDX + j, "kind": sec.kind, "field": "전문", "label": sec.label, "opinion_kind": sec.opinion_kind,
                      "vote": sec.vote, "char_start": sec.char_start, "char_end": sec.char_end, "text": sec.text,
                      "needs_review": bool(flag and sec.kind in ("reasons", "opinion", "heading"))})
    if info["status"] == STATUS_UNSPLIT:
        warnings.append("opinion_markers_not_found")
    elif info["status"] == STATUS_REVIEW:
        warnings.append("opinion_markers_inconsistent")
    if h_flag:
        warnings.append(f"headnote_opinions_{hinfo['headnote_split']}")
    mism = []
    list_nos = L.split_case_numbers(str(list_extra.get("case_no") or ""))
    if list_nos and not set(list_nos) <= set(case_nos):
        mism.append({"field": "case_no", "list": list_extra.get("case_no"), "body": body_case_no})
    if doc.get("decision_date") and parsed["decision_date"] and doc["decision_date"] != parsed["decision_date"]:
        mism.append({"field": "decision_date", "list": doc["decision_date"], "body": parsed["decision_date"]})
    return {"pages": pages, "body_case_no": body_case_no, "case_nos": case_nos, "merged_numbers": merged,
            "dispositions": classify_orders(order), "opinion_status": info["status"],
            "opinion_split": {k: info[k] for k in ("status", "marker_count", "loose_count", "conclusion_numbers", "markers")},
            "headnote_split": hinfo, "mismatches": mism, "warnings": warnings, "codes": parsed["extra"],
            "fields_chars": {k: len(parsed[k] or "") for _, k, _ in HEAD_PAGES} | {"full_text": len(full)}, "text_available": bool(full.strip())}


def persist_extraction(ctx, doc: dict, serial: str, raw_sha: str, raw_rel: str, fetched_at: str, built: dict) -> dict:
    """쪽 파일을 쓰고 파생 행을 지운 뒤 pages 를 다시 쓰고 단계 정보를 갱신한다(순서는 lawapi.persist_extraction 과 같다)."""
    m, doc_id = ctx.manifest, doc["doc_id"]
    rel_dir = f"{D.TEXT_DIR}/{serial}"
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
        "fetched_at": fetched_at, "raw_path": raw_rel, "raw_sha256": raw_sha, "format": "xml", "extractor_version": EXTRACTOR_VERSION,
        "text_available": built["text_available"], "pages": len(built["pages"]), "fields_chars": built["fields_chars"],
        "body_case_no": built["body_case_no"], "case_nos": built["case_nos"], "merged_numbers": built["merged_numbers"],
        "dispositions": built["dispositions"], "opinion_status": built["opinion_status"], "opinion_split": built["opinion_split"],
        "headnote_split": built["headnote_split"], "verify_mismatches": built["mismatches"], "warnings": built["warnings"],
        "section_locator": "page_id suffix #sNNN (#h000 = case header; #s500+ = opinions inside 결정요지); quality_json.char_start/char_end = offsets in the field text",
    }
    with m.transaction():
        m.delete_derived(doc_id=doc_id)
        for p in built["pages"]:
            loc = L.page_loc(p)
            m.upsert_page(
                f"{doc_id}#{loc}", doc_id, method="html_section", text_path=f"{rel_dir}/{loc}.txt",
                text_sha256=hashlib.sha256(p["text"].encode("utf-8")).hexdigest(), char_count=len(p["text"]),
                quality_json={"locator": loc, "section_kind": p["kind"], "label": p["label"], "field": p["field"],
                              "opinion_kind": p["opinion_kind"], "vote": p.get("vote"), "char_start": p["char_start"], "char_end": p["char_end"],
                              "serial": serial},
                needs_review=p["needs_review"], extractor_version=EXTRACTOR_VERSION)
        L._set_extra(m, doc_id, "body", body)
        m.update_document_fields(doc_id, extra_merge={"codes": built["codes"]})            # 사건종류·재판부구분 코드는 원값만(의미 미해석)
        m.update_document_stage(doc_id, extractor_version=EXTRACTOR_VERSION, extract_input_sha256=raw_sha)
        m.set_document_status(doc_id, "extracted")
        m.resolve_failures(doc_id, "body")
    return body


# ---------------------------------------------------------------- 실행
def add_arguments(parser) -> None:
    parser.add_argument("--serial", action="append", dest="serials", metavar="ID",
                        help="헌재결정례일련번호(반복 가능, 장부에 있는 문서만). 없으면 받을 문서 전부. 예: python -m press_review.ingestion lawapi_detc_body --serial 137285")
    parser.add_argument("--reparse", action="store_true",
                        help="저장된 raw 로 쪽·extra 를 다시 만든다(네트워크 없음, 요청 0). raw 가 있고 sha 가 장부와 같은 문서만이며 다른 모드 동작은 하지 않는다. "
                             "쪽·파생 행(chunks·cases)은 새 분할 결과로 교체되고 문서 상태는 extracted 로 돌아간다")
    parser.add_argument("--limit", type=int, default=None,
                        help="이번 실행에서 새로 받을 본문 수 상한. 목록용 --limit-pages 와 별개이며 이 모드는 --limit-pages 를 읽지 않는다")


class _BodyRun:
    def __init__(self, ctx, args):
        self.ctx, self.m = ctx, ctx.manifest
        self.limit = getattr(args, "limit", None)
        self.reparse_mode = bool(getattr(args, "reparse", False))
        self.consecutive_failures = 0
        self.attempted: set = set()
        self.c = {"candidates_seen": 0, "fetched": 0, "resumed_from_raw": 0, "extracted": 0, "failed": 0, "no_match": 0, "serial_mismatch": 0,
                  "refetch_unchanged": 0, "skipped_exhausted": 0, "pages_written": 0, "mismatch_docs": 0, "reparsed": 0, "reparse_skipped": 0}
        self.opinion = {"none": 0, "split": 0, "unsplit": 0, "needs_review": 0}
        self.limit_hit = False

    def bump_fail(self, what: str) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= L.MAX_CONSECUTIVE_FAILURES:
            raise IngestionError(f"{L.MAX_CONSECUTIVE_FAILURES} consecutive {what} failures; stopping (unverified)")

    def candidates(self, serials: Optional[list]) -> list:
        m = self.m
        if self.reparse_mode:
            return []                                   # --reparse 는 새로 받는 후보가 없다(요청 0)
        exhausted = {f["target"] for f in m.failures() if f["stage"] == "body" and f["retry_count"] + 1 >= L.MAX_BODY_RETRIES}
        statuses = ("discovered", "failed", "fetched") + (L.DONE_STATUSES if self.ctx.refresh else ())
        where = f"source_id=? AND status IN ({','.join(repr(x) for x in statuses)})"
        params: list = [D.SOURCE_ID]
        if serials:
            where += f" AND doc_id IN ({','.join('?' * len(serials))})"
            params += [f"{D.DOC_PREFIX}:{s}" for s in serials]
        out = []
        for r in m._q(f"SELECT doc_id, status FROM documents WHERE {where} ORDER BY COALESCE(decision_date,'9999-99-99'), doc_id", tuple(params)):
            if r["doc_id"] in self.attempted:
                continue
            if r["doc_id"] in exhausted:
                self.c["skipped_exhausted"] += 1
                continue
            out.append(dict(r))
        return out

    def process(self, row: dict) -> None:
        ctx, m = self.ctx, self.m
        doc = m.get_document(row["doc_id"])
        doc_id = doc["doc_id"]
        serial = doc_id.split(":", 1)[1]
        raw_rel = raw_rel_of(serial)
        raw_path = ctx.corpus_dir / raw_rel
        raw = None
        prior = (L._doc_extra(doc).get("body") or {})
        if doc["status"] == "fetched" and prior.get("raw_sha256") and raw_path.is_file():
            data = raw_path.read_bytes()
            if hashlib.sha256(data).hexdigest() == prior["raw_sha256"]:
                raw, fetched_at = data, prior.get("fetched_at") or ""
                self.c["resumed_from_raw"] += 1
        if raw is None:
            ctx.check_time()
            try:
                r = ctx.http.get(D.body_url(serial), with_oc=True, accept=lambda b, h, s=serial: accept_detc_body(b, h, s))
            except FetchFailed as e:
                self._fail(doc_id, "http", str(e), counts_toward_stop=True)
                return
            if not r.ok:
                reason = r.content_error or f"http_{r.status}"
                kind = reason.split(":", 1)[0] if r.content_error else f"http_{r.status}"
                self._fail(doc_id, kind, reason, counts_toward_stop=kind not in ("no_match", "serial_mismatch"))
                return
            self.consecutive_failures = 0
            raw, fetched_at = r.body, ctx.now().isoformat(timespec="seconds")
            sha = hashlib.sha256(raw).hexdigest()
            if doc["status"] in L.DONE_STATUSES and prior.get("raw_sha256") == sha and doc.get("extractor_version") == EXTRACTOR_VERSION:
                L._set_extra(m, doc_id, "body", {**prior, "fetched_at": fetched_at})
                self.c["refetch_unchanged"] += 1
                return
            if not ctx.dry_run:
                L._atomic_write(raw_path, raw)
            with m.transaction():
                L._set_extra(m, doc_id, "body", {**prior, "fetched_at": fetched_at, "raw_path": raw_rel, "raw_sha256": sha})
                m.set_document_status(doc_id, "fetched")
            self.c["fetched"] += 1
        self._extract(doc_id, serial, raw, fetched_at, raw_rel)

    def _extract(self, doc_id: str, serial: str, raw: bytes, fetched_at: str, raw_rel: str) -> None:
        doc = self.m.get_document(doc_id)
        try:
            parsed = parse_detc_xml(raw, serial)
        except L.BodyParseError as e:
            self._fail(doc_id, "serial_mismatch" if e.kind == "serial_mismatch" else "raw_unparsable", f"{e.kind}: {e}", counts_toward_stop=False)
            return
        built = build_extraction(parsed, doc, L._doc_extra(doc))
        body = persist_extraction(self.ctx, doc, serial, hashlib.sha256(raw).hexdigest(), raw_rel, fetched_at, built)
        self.c["extracted"] += 1
        self.c["pages_written"] += body["pages"]
        self.opinion[built["opinion_status"]] += 1
        if built["mismatches"]:
            self.c["mismatch_docs"] += 1

    def reparse(self, serials: Optional[list]) -> None:
        """저장된 raw 로 다시 추출한다(네트워크 없음). raw 가 없거나 sha 가 장부와 다르거나 파싱이 안 되면 건드리지 않고 reparse_skipped 로 센다.
        persist_extraction 이 문서의 쪽·chunks·cases 를 지우고 새로 쓰므로 구조화가 끝난 문서도 extracted 로 돌아간다(재구조화 필요)."""
        m, ctx = self.m, self.ctx
        where = f"source_id=? AND status IN ({','.join(repr(x) for x in ('fetched',) + L.DONE_STATUSES)})"
        params: list = [D.SOURCE_ID]
        if serials:
            where += f" AND doc_id IN ({','.join('?' * len(serials))})"
            params += [f"{D.DOC_PREFIX}:{s}" for s in serials]
        for r in m._q(f"SELECT doc_id FROM documents WHERE {where} ORDER BY doc_id", tuple(params)):
            ctx.check_time()
            doc = m.get_document(r["doc_id"])
            serial = doc["doc_id"].split(":", 1)[1]
            body = L._doc_extra(doc).get("body") or {}
            p = ctx.corpus_dir / body["raw_path"] if body.get("raw_path") else None
            data = p.read_bytes() if p is not None and p.is_file() else None
            if data is None or hashlib.sha256(data).hexdigest() != body.get("raw_sha256"):
                self.c["reparse_skipped"] += 1
                continue
            try:
                parsed = parse_detc_xml(data, serial)
            except L.BodyParseError:
                self.c["reparse_skipped"] += 1
                continue
            built = build_extraction(parsed, doc, L._doc_extra(doc))
            new = persist_extraction(ctx, doc, serial, body["raw_sha256"], body["raw_path"], body.get("fetched_at") or "", built)
            self.c["reparsed"] += 1
            self.c["pages_written"] += new["pages"]
            self.opinion[built["opinion_status"]] += 1

    def _fail(self, doc_id: str, error_class: str, message: str, *, counts_toward_stop: bool) -> None:
        m = self.m
        m.record_failure(doc_id, "body", error_class, message)
        m.set_document_status(doc_id, "failed")
        self.c["no_match" if error_class == "no_match" else "serial_mismatch" if error_class == "serial_mismatch" else "failed"] += 1
        if counts_toward_stop:
            self.bump_fail("body")


def run(ctx, args) -> dict:
    m = ctx.manifest
    D.open_source(m)
    serials = [str(s).strip() for s in (getattr(args, "serials", None) or [])]
    bad = [s for s in serials if not s.isdigit()]
    if bad:
        raise IngestionError(f"invalid --serial values: {bad}")
    run_ = _BodyRun(ctx, args)
    missing = [s for s in serials if m.get_document(f"{D.DOC_PREFIX}:{s}") is None]
    state_path = ctx.inventory_dir / STATE_FILE
    try:
        if run_.reparse_mode:
            run_.reparse(serials or None)
        for row in run_.candidates(serials or None):
            if run_.limit is not None and run_.c["fetched"] >= run_.limit:
                run_.limit_hit = True
                break
            run_.c["candidates_seen"] += 1
            run_.attempted.add(row["doc_id"])
            run_.process(row)
    finally:
        m.touch_source_checked(D.SOURCE_ID)
        if not ctx.dry_run:
            L._write_terms_file(state_path, {"updated_at": ctx.now().isoformat(timespec="seconds"), "counts": run_.c,
                                             "opinion_status": run_.opinion, "extractor_version": EXTRACTOR_VERSION})
    docs = {r["status"]: r["n"] for r in m.count_by("documents", "status", where={"source_id": D.SOURCE_ID})}
    return {"serials_missing": missing, "limit": run_.limit, "limit_hit": run_.limit_hit, **run_.c, "opinion_status": run_.opinion,
            "documents_by_status": docs, "state_file": str(state_path) if not ctx.dry_run else None}

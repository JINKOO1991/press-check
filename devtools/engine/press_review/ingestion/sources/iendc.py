"""IENDC(인터넷선거보도심의위원회) 심의결과 내역 어댑터. 계약: docs/P1_DESIGN.md '어댑터 범위' iendc 행.

열거: ``board.do?menuPos=45&pageIndex=N`` 전 페이지(GET 쿼리로 페이지 이동이 된다, 2026-10-08 확인).
각 상세 ``act=detail&idx=…`` 에서 작성일(등록일)·제목·첨부 PDF 주소(PDFObject / 모바일 링크의 ``/images/pdf/.../loader.do``)를 얻고,
첨부는 HEAD 로 크기·형식만 확인한다. 상세 본문 표 독해는 하지 않는다(P2).

- bare ``board.do`` 는 오류 HTML(handler.do 로 302)이라 쓰지 않는다. 목록·상세 모두 본문 표지로 성공을 판정한다.
- 목록 합계(``총 게시물 N``)와 장부의 문서 수, 목록 쪽 항목 합이 같아야 전량 열거 완료로 본다.
- 재개: HTTP 캐시와 장부로 이어받는다. 캐시된 목록 쪽의 첫 번호(No)가 현재 합계와 어긋나면(새 글로 밀림) 그 쪽만 다시 받는다.
- 어댑터 전용 플래그는 소량 게이트용이다: ``--pages 1,last`` ``--detail-idx 3104,1940``.
"""
from __future__ import annotations

import html
import json
import math
import re
import urllib.parse
from typing import Optional

from ..http import BlockedURL, FetchFailed, IngestionError, ResponseTooLarge, RobotsDisallowed

SOURCE_ID = "iendc_review_results"
FAMILY = "iendc"
BASE = "https://www.iendc.go.kr"
LIST_URL = BASE + "/home/kor/board.do?menuPos=45&pageIndex={n}"
DETAIL_URL = BASE + "/home/kor/board.do?menuPos=45&act=detail&idx={idx}"
MAX_CONSECUTIVE_FAILURES = 3
# 재시도(백오프) 뒤에도 실패한 요청. 문서 하나의 실패는 실패 장부에 남기고 다음 문서로 간다(연속 3회면 c.fail 이 멈춘다).
_NET_ERRORS = (FetchFailed, RobotsDisallowed, BlockedURL, ResponseTooLarge)

_TOTAL_RE = re.compile(r'class="synthesis"[^>]*>[^<]*<span class="num">\s*([\d,]+)\s*</span>')
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_IDX_RE = re.compile(r"fn_edit\(\s*'detail'\s*,\s*'(\d+)'")
_LAST_RE = re.compile(r'class="page_arrow r2"[^>]*fn_cmmn_list_page\((\d+)\)')
_TAG_RE = re.compile(r"<[^>]+>")
_DETAIL_TITLE_RE = re.compile(r'<strong class="tit">(.*?)(?:<div class="info">|</strong>)', re.S)
_DETAIL_DATE_RE = re.compile(r"작성일\s*:\s*</span>\s*(\d{4}-\d{2}-\d{2})")
_DETAIL_IDX_RE = re.compile(r'name="idx"[^>]*value="(\d+)"|value="(\d+)"[^>]*name="idx"')
_PDF_EMBED_RE = re.compile(r"""PDFObject\.embed\(\s*["']([^"']+)["']""")
_PDF_HREF_RE = re.compile(r"""href=["'](/images/pdf/[^"']+/loader\.do)["']""")
_FILE_DOWN_RE = re.compile(r"cmmn_file_down\(\s*'((?:[^'\\]|\\.)*)'\s*,\s*'((?:[^'\\]|\\.)*)'\s*\)")
_TITLE_DATE_RE = re.compile(r"\((\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?\)")
_TITLE_YEAR_RE = re.compile(r"(\d{4})\s*년도")


class ListStructureError(IngestionError):
    error_class = "list_structure"


def add_arguments(parser) -> None:
    parser.add_argument("--pages", default=None, help="처리할 목록 쪽(쉼표 구분 번호, 'last' 가능). 기본: 전부")
    parser.add_argument("--detail-idx", default=None, help="상세·첨부 HEAD 를 처리할 게시물 idx(쉼표 구분). 기본: 처리한 쪽의 전부")
    parser.add_argument("--no-details", action="store_true", help="목록만 열거하고 상세·HEAD 는 건너뛴다")


# ---------------------------------------------------------------- 파서(순수 함수)
def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", s))).strip()


def parse_list(text: str) -> dict:
    """목록 HTML → {total, rows[{no, idx, title, author, reg_date}], last_page}. 구조가 다르면 ListStructureError."""
    m = _TOTAL_RE.search(text)
    if not m:
        raise ListStructureError("total count ('총 게시물') not found")
    total = int(m.group(1).replace(",", ""))
    i = text.find('class="board_list"')
    j = text.find("</table>", i) if i >= 0 else -1
    if i < 0 or j < 0:
        raise ListStructureError("board_list table not found")
    rows = []
    for r in _ROW_RE.findall(text[i:j]):
        im = _IDX_RE.search(r)
        if not im:
            continue
        tds = [_clean(x) for x in _TD_RE.findall(r)]
        if len(tds) < 4:
            raise ListStructureError("unexpected row layout")
        no = int(tds[0]) if tds[0].isdigit() else None
        rows.append({"no": no, "idx": im.group(1), "title": tds[1], "author": tds[2], "reg_date": tds[3]})
    lm = _LAST_RE.search(text)
    return {"total": total, "rows": rows, "last_page": int(lm.group(1)) if lm else None}


def parse_detail(text: str) -> dict:
    """상세 HTML → {idx, title, reg_date, pdf_paths[], file_names[]}."""
    tm = _DETAIL_TITLE_RE.search(text)
    dm = _DETAIL_DATE_RE.search(text)
    im = _DETAIL_IDX_RE.search(text)
    pdfs: list[str] = []
    for p in _PDF_EMBED_RE.findall(text) + _PDF_HREF_RE.findall(text):
        if p not in pdfs:
            pdfs.append(p)
    downs = [(html.unescape(a), html.unescape(b)) for a, b in _FILE_DOWN_RE.findall(text)]
    names = [a for a, _b in downs]
    return {"idx": (im.group(1) or im.group(2)) if im else None, "title": _clean(tm.group(1)) if tm else "",
            "reg_date": dm.group(1) if dm else None, "pdf_paths": pdfs, "file_names": names, "file_downs": downs}


def title_dates(title: str, reg_date: Optional[str]) -> tuple[Optional[str], Optional[int], str]:
    """제목에서 (결정/조치일 ISO, 연도, 연도 출처). 제목에 날짜(YYYY.MM.DD.)가 있으면 그 날짜, 'YYYY년도'면 연도, 없으면 등록일 연도."""
    m = _TITLE_DATE_RE.search(title)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}", y, "title_date"
    m = _TITLE_YEAR_RE.search(title)
    if m:
        return None, int(m.group(1)), "title_year"
    if reg_date and re.match(r"\d{4}-", reg_date):
        return None, int(reg_date[:4]), "reg_date"
    return None, None, "none"


POST_PATH = "/fileDownload.do"


def post_download_url(filename: str, downname: str) -> str:
    """PDF 뷰어 주소가 없는 첨부(HWP 등)의 내려받기 주소. 페이지의 ``cmmnFileDownForm`` 이 ``/fileDownload.do`` 로 POST 하는 폼 필드
    (filename, downname, uniqueKey, downauth)를 쿼리로 담아 둔다. fetch 단계가 이 쿼리를 폼 본문으로 보낸다(P2_DESIGN §8).
    downname 에는 ``+`` 가 들어 있어 ``%2B`` 로 인코딩해야 한다(폼 파싱이 ``+`` 를 공백으로 읽는다)."""
    q = "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in
                 (("filename", filename), ("downname", downname), ("uniqueKey", ""), ("downauth", "")))
    return f"{BASE}{POST_PATH}?{q}"


def _is_post_url(url: str) -> bool:
    return urllib.parse.urlsplit(url).path == POST_PATH


def _abs_pdf_url(path: str) -> str:
    u = urllib.parse.urljoin(BASE + "/", path)
    p = urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, urllib.parse.quote(p.path, safe="/%+:@&=,;~!$'()*-._"), p.query, ""))


# ---------------------------------------------------------------- accept(본문 표지)
def accept_list(body: bytes, headers: dict) -> Optional[str]:
    t = body.decode("utf-8", "replace")
    if 'class="board_list"' not in t or 'class="synthesis"' not in t:
        return "list markers missing (error page?)"
    return None


def accept_detail_for(idx: str):
    def _accept(body: bytes, headers: dict) -> Optional[str]:
        t = body.decode("utf-8", "replace")
        if 'class="board_view_top"' not in t or '<strong class="tit">' not in t:
            return "detail markers missing (error page?)"
        m = _DETAIL_IDX_RE.search(t)
        if not m or (m.group(1) or m.group(2)) != idx:
            return "detail idx does not match request"
        return None
    return _accept


# ---------------------------------------------------------------- 실행
def _parse_pages_arg(arg: Optional[str], last: int) -> list[int]:
    if not arg:
        return list(range(1, last + 1))
    out: list[int] = []
    for tok in (t.strip() for t in arg.split(",") if t.strip()):
        n = last if tok == "last" else int(tok)
        if not 1 <= n <= last:
            raise ListStructureError(f"page {tok} outside 1..{last}")
        if n not in out:
            out.append(n)
    return out


class _Counters:
    def __init__(self) -> None:
        self.docs_new = self.docs_seen = self.details_fetched = self.details_skipped = 0
        self.head_ok = self.head_failed = self.no_attachment = self.title_mismatch = self.date_mismatch = 0
        self.fail_streak = 0
        self.page_errors = 0

    def fail(self, what: str) -> None:
        self.fail_streak += 1
        if self.fail_streak >= MAX_CONSECUTIVE_FAILURES:
            raise FetchFailed(f"{self.fail_streak} consecutive failures at {what}; stopping (unverified)")

    def ok(self) -> None:
        self.fail_streak = 0


def _fetch_list_page(ctx, n: int, first: bool, expected_first_no: Optional[int] = None):
    url = LIST_URL.format(n=n)
    refresh = True if (first and not ctx.dry_run) else None
    r = ctx.http.get(url, accept=accept_list, refresh=refresh)
    if r.ok and expected_first_no is not None and r.from_cache and not ctx.dry_run:
        rows = parse_list(r.text())["rows"]
        if not rows or rows[0]["no"] != expected_first_no:  # 캐시가 낡았다(새 글로 밀림)
            r = ctx.http.get(url, accept=accept_list, refresh=True)
    return url, r


def run(ctx, args) -> dict:
    m = ctx.manifest
    m.ensure_source(SOURCE_ID, FAMILY, "IENDC 심의결과 내역", LIST_URL.format(n=1))
    c = _Counters()

    # 1) 1쪽을 새로 받아 합계·쪽당 항목 수·마지막 쪽을 얻는다.
    url1, r1 = _fetch_list_page(ctx, 1, first=True)
    if not r1.ok:
        m.record_failure(url1, "list", "bad_page", r1.content_error or f"http {r1.status}")
        raise FetchFailed(f"list page 1 unusable: {r1.content_error or r1.status}")
    p1 = parse_list(r1.text())
    total, per_page = p1["total"], len(p1["rows"])
    if per_page == 0:
        raise ListStructureError("page 1 has no rows")
    last_page = math.ceil(total / per_page)
    if p1["last_page"] is not None and p1["last_page"] != last_page:
        m.record_failure("iendc:list", "structure", "last_page_mismatch",
                         f"computed {last_page} (total {total}/per_page {per_page}) vs pagination {p1['last_page']}")
        raise ListStructureError(f"last page mismatch: computed {last_page} vs shown {p1['last_page']}")
    pages = _parse_pages_arg(getattr(args, "pages", None), last_page)
    if ctx.limit_pages is not None:
        pages = pages[:ctx.limit_pages]
    only_idx = {t.strip() for t in args.detail_idx.split(",") if t.strip()} if getattr(args, "detail_idx", None) else None

    # 2) 목록 쪽 처리 → 문서 upsert
    seen_idx: dict[str, str] = {}
    page_rows: dict[int, list] = {}
    for n in pages:
        ctx.check_time()
        expected_first = total - per_page * (n - 1)
        expected_count = per_page if n < last_page else total - per_page * (last_page - 1)
        if n == 1:
            url, r, parsed = url1, r1, p1
        else:
            url, r = _fetch_list_page(ctx, n, first=False, expected_first_no=expected_first)
            if not r.ok:
                m.upsert_list_page(SOURCE_ID, n, url, http_status=r.status, item_count=0, body_sha256=None, ok=False)
                m.record_failure(url, "list", "bad_page", r.content_error or f"http {r.status}")
                c.page_errors += 1
                c.fail(f"list page {n}")
                continue
            parsed = parse_list(r.text())
        rows = parsed["rows"]
        problems = []
        if len(rows) != expected_count:
            problems.append(f"rows {len(rows)} != expected {expected_count}")
        if rows and rows[0]["no"] != expected_first:
            problems.append(f"first No {rows[0]['no']} != expected {expected_first}")
        if parsed["total"] != total:
            problems.append(f"total {parsed['total']} != {total}")
        dup = [x["idx"] for x in rows if x["idx"] in seen_idx]
        if dup:
            problems.append(f"idx repeated across pages: {dup[:3]}")
        ok = not problems
        with m.transaction():
            if ok:
                for row in rows:
                    seen_idx[row["idx"]] = row["title"]
                    res = m.upsert_document(
                        f"{FAMILY}:{row['idx']}", SOURCE_ID, canonical_url=DETAIL_URL.format(idx=row["idx"]),
                        title=row["title"], publication_date=row["reg_date"], doc_type="review_result",
                        detail_url=DETAIL_URL.format(idx=row["idx"]),
                        extra={"list_no": row["no"], "list_page": n})
                    if res == "new":
                        c.docs_new += 1
                    else:
                        c.docs_seen += 1
                m.resolve_failures(url, "list")
            else:
                m.record_failure(url, "list", "page_check_failed", "; ".join(problems))
            m.upsert_list_page(SOURCE_ID, n, url, http_status=r.status, item_count=len(rows), body_sha256=r.sha256, ok=ok)
        if not ok:
            c.page_errors += 1
            c.fail(f"list page {n}")
            continue
        c.ok()
        page_rows[n] = rows
    m.touch_source_checked(SOURCE_ID)

    # 3) 상세 + 첨부 HEAD
    if not getattr(args, "no_details", False):
        for n in pages:
            for row in page_rows.get(n, []):
                if only_idx is not None and row["idx"] not in only_idx:
                    continue
                ctx.check_time()
                _process_detail(ctx, row, c)

    return _summary(ctx, c, total, per_page, last_page, pages)


def _process_detail(ctx, row: dict, c: _Counters) -> None:
    m = ctx.manifest
    idx = row["idx"]
    doc_id = f"{FAMILY}:{idx}"
    doc = m.get_document(doc_id)
    extra = json.loads(doc["extra_json"]) if doc and doc.get("extra_json") else {}
    atts = m.attachments_for(doc_id)
    post_pending = extra.get("attachment_state") == "post_only_non_pdf" and not atts     # POST 첨부를 아직 장부에 올리지 않았다
    if (not ctx.refresh and extra.get("detail_ok") and extra.get("attachment_state") and not post_pending
            and all(a["head_checked_at"] or _is_post_url(a["url"]) for a in atts)):
        c.details_skipped += 1
        return
    url = DETAIL_URL.format(idx=idx)
    try:
        r = ctx.http.get(url, accept=accept_detail_for(idx))
    except _NET_ERRORS as e:
        m.record_failure(doc_id, "detail", getattr(e, "error_class", "error"), str(e))
        c.fail(f"detail {idx}")
        return
    if not r.ok:
        m.record_failure(doc_id, "detail", "bad_page", r.content_error or f"http {r.status}")
        c.fail(f"detail {idx}")
        return
    d = parse_detail(r.text())
    if not d["title"] or not d["reg_date"]:
        m.record_failure(doc_id, "detail", "parse_failed", "title or registration date missing")
        c.fail(f"detail {idx}")
        return
    c.details_fetched += 1
    c.ok()
    state = "pdf" if d["pdf_paths"] else ("post_only_non_pdf" if d["file_names"] else "none")
    decision_date, year, year_src = title_dates(d["title"], d["reg_date"])
    if d["title"] != row["title"]:
        c.title_mismatch += 1
    if d["reg_date"] != row["reg_date"]:
        c.date_mismatch += 1
    with m.transaction():
        m.update_document_fields(
            doc_id, title=d["title"], publication_date=d["reg_date"], decision_date=decision_date, year=year,
            extra_merge={"detail_ok": True, "year_source": year_src, "list_title": row["title"],
                         "file_names": d["file_names"], "pdf_count": len(d["pdf_paths"]), "attachment_state": state})
        m.resolve_failures(doc_id, "detail")
        for n, path in enumerate(d["pdf_paths"], 1):
            label = d["file_names"][n - 1] if n - 1 < len(d["file_names"]) else None
            m.upsert_attachment(f"{doc_id}#{n}", doc_id, _abs_pdf_url(path), label=label, kind="full")
    if not d["pdf_paths"]:
        c.no_attachment += 1
        if state == "post_only_non_pdf":  # 첨부가 hwp 등이고 내려받기가 POST /fileDownload.do 뿐이다: POST 주소로 첨부 행을 만든다(HEAD 는 하지 않는다)
            with m.transaction():
                for n, (fname, dname) in enumerate(d["file_downs"], 1):
                    m.upsert_attachment(f"{doc_id}#{n}", doc_id, post_download_url(fname, dname), label=fname, kind="other")
                m.resolve_failures(doc_id, "attachment")
        else:
            m.record_failure(doc_id, "attachment", "no_attachment_url", "no attachment address found on detail page")
        return
    m.resolve_failures(doc_id, "attachment")
    for a in m.attachments_for(doc_id):
        ctx.check_time()
        if a["head_checked_at"] and not ctx.refresh:
            continue
        try:
            h = ctx.http.head(a["url"])
        except _NET_ERRORS as e:
            m.record_failure(a["att_id"], "head", getattr(e, "error_class", "error"), str(e))
            c.head_failed += 1
            c.fail(f"head {a['att_id']}")
            continue
        ctype = h.headers.get("content-type", "")
        clen = h.headers.get("content-length", "")
        if not h.ok or "pdf" not in ctype.lower():
            if not ctx.dry_run:                          # 2xx 오류 페이지가 HEAD 캐시에 굳으면 이 첨부를 다시 확인하지 못한다
                ctx.http.evict(a["url"], method="HEAD")
            m.record_failure(a["att_id"], "head", "bad_head", f"http {h.status} type {ctype[:60]}")
            c.head_failed += 1
            c.fail(f"head {a['att_id']}")
            continue
        m.update_attachment_head(a["att_id"], content_length=int(clen) if clen.isdigit() else None,
                                 content_type=ctype.split(";")[0].strip())
        m.resolve_failures(a["att_id"], "head")
        c.head_ok += 1
        c.ok()


def _summary(ctx, c: _Counters, total: int, per_page: int, last_page: int, pages: list[int]) -> dict:
    m = ctx.manifest
    q = m._q  # 읽기 전용 집계(공용 조회 API 없음)
    docs = q("SELECT COUNT(*) FROM documents WHERE source_id=?", (SOURCE_ID,))[0][0]
    lp = q("SELECT COUNT(*), COALESCE(SUM(ok),0), COALESCE(SUM(CASE WHEN ok=1 THEN item_count ELSE 0 END),0) "
           "FROM list_pages WHERE source_id=?", (SOURCE_ID,))[0]
    att = q("SELECT COUNT(*), COALESCE(SUM(content_length),0), "
            "COALESCE(SUM(CASE WHEN head_checked_at IS NOT NULL THEN 1 ELSE 0 END),0) FROM attachments "
            "WHERE doc_id IN (SELECT doc_id FROM documents WHERE source_id=?)", (SOURCE_ID,))[0]
    yrs = {str(r[0]): r[1] for r in q("SELECT year, COUNT(*) FROM documents WHERE source_id=? GROUP BY year ORDER BY year", (SOURCE_ID,))}
    reg = q("SELECT MIN(publication_date), MAX(publication_date) FROM documents WHERE source_id=?", (SOURCE_ID,))[0]
    fails = q("SELECT COUNT(*) FROM failures WHERE resolved_at IS NULL AND (target LIKE 'iendc:%' OR target LIKE ?)",
              (BASE + "/home/kor/board.do?menuPos=45%",))[0][0]
    full = len(pages) == last_page
    match = full and docs == total and lp[2] == total and lp[1] == last_page
    return {
        "total_shown": total, "per_page": per_page, "last_page": last_page, "pages_processed": len(pages),
        "full_listing": full, "list_pages_ok": lp[1], "list_item_sum": lp[2],
        "docs_in_db": docs, "docs_new": c.docs_new, "docs_seen": c.docs_seen,
        "reconcile_total_eq_docs_eq_list_sum": match,
        "details_fetched": c.details_fetched, "details_skipped_done": c.details_skipped,
        "attachments": att[0], "head_checked": att[2], "head_failed_this_run": c.head_failed, "attachment_bytes_total": att[1],
        "no_attachment_url": c.no_attachment, "title_mismatch": c.title_mismatch, "regdate_mismatch": c.date_mismatch,
        "year_distribution": yrs, "reg_date_min": reg[0], "reg_date_max": reg[1],
        "failures_open": fails, "list_page_errors": c.page_errors,
    }

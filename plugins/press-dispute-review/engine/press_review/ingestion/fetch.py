"""원문 다운로드 단계(P2_DESIGN §1). 대상: rights.store_raw=allowed 이고 아직 내려받지 않은 첨부.

- 저장: ``<corpus>/raw/<family>/<sha256[:24]>.<ext>`` (내용 주소 파일명 = 짧은 안정 ID). 임시 파일에 쓰고 fsync 후 os.replace.
  원 파일명은 ``attachments.orig_filename`` 메타데이터로만 둔다(Windows 경로 길이). ``raw_path`` 는 코퍼스 폴더 기준 상대 경로(stage.py).
- 검사: 크기가 HEAD(``content_length``)와 다르면 failed(``size_mismatch``), 매직 바이트가 선언(HEAD·응답 Content-Type, 선언이 없으면 확장자)과
  다르면 failed(``magic_mismatch``), 알 수 없는 형식이면 failed(``unknown_type``). HTTP 200 오류 페이지는 캐시에 쓰기 전에 걸러진다(accept 콜백).
- 중복: 같은 해시의 파일은 한 번만 저장하고 뒤에 온 첨부는 ``dedup_of`` 로 앞 첨부를 가리키며 같은 ``raw_path`` 를 쓴다.
  추출은 정본(dedup_of 가 비어 있는 쪽)만 한다(extract 단계).
- 디스크: HttpClient.get 은 2xx 본문을 http_cache 에도 쓴다(http.py ``cache.put`` 호출, no-cache 옵션 없음). 저장이 끝나면 그 캐시 항목을 evict 해 사본을 남기지 않는다.
- POST 첨부: URL 경로가 ``/fileDownload.do`` 이고 쿼리가 있으면 쿼리를 폼 본문으로 하는 POST 로 받는다(IENDC HWP, P2 §8).
- (G-D19) 알 수 없는 형식(``unknown_type``)은 실패 메시지에 매직 바이트(앞 16바이트 hex)와 추정 종류를 남긴다. 앞부분이 HWP 3.x 서명(``HWP Document File V3``)이면 kordoc 으로 1회 읽어 보고,
  읽히면 ``hwp3`` 형식으로 저장해 추출 단계가 kordoc 으로 읽게 한다. 못 읽으면 failed(``hwp3_unsupported``)로 남기고(재시도 상한을 채워 같은 kordoc 으로는 다시 시험하지 않는다; ``--retry-exhausted`` 로 다시 시험) 숨기지 않는다.
  kordoc 이 설치돼 있지 않으면 시험을 못 한 것이므로 ``no_hwp_extractor`` 로 재시도 가능하게 둔다. dry-run 에서는 시험하지 않는다.
- 순위: tiers.py. 재시작하면 이미 fetched 이고 파일이 있는 첨부는 건너뛴다. 실패(failed, raw_path 없음)는 재시도 횟수 상한(3) 안에서 다시 시도한다.

이식: ``kind_of`` 는 작성자 소유 코드(비공개)의 함수 본문 그대로 가져왔다.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from . import stage, tiers, withhold
from .context import TimeBudgetExceeded, iso
from .extract import hwp as hwp_mod
from .extract.tools import ToolRunner
from .http import DryRunMiss, HostBusy, IngestionError

MAX_RETRIES_BEFORE_SKIP = 3
UNKNOWN_SIZE_CAP = 512 * 1024 * 1024
SIZE_MARGIN = 1024 * 1024
KNOWN_KINDS = ("pdf", "hwp", "hwpx", "html")
PERMANENT_ERRORS = ("blocked_url", "robots_disallowed")
POST_PATH = "/fileDownload.do"


# ---------------------------------------------------------------- 형식 판별
def kind_of(blob, ext):
    """매직 바이트 우선 판별 — 확장자는 참고만(다운로드 URL엔 확장자가 없는 소스가 있다).

    이식: 작성자 소유 코드(비공개), 본문 그대로.
    """
    if blob[:5] == b"%PDF-":
        return "pdf"
    if blob[:4] == b"\xd0\xcf\x11\xe0":
        return "hwp"
    if blob[:4] == b"PK\x03\x04":
        return "hwpx" if ext in ("", ".hwpx") else ext.lstrip(".")
    return ext.lstrip(".") or "unknown"


HWP3_SIGNATURE = b"HWP Document File V3"       # 옛 한글(HWP 3.x) 문서의 머리: ASCII 문구로 시작한다
_MAGIC_LABELS = ((b"%PDF-", "pdf"), (HWP3_SIGNATURE, "hwp3"), (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole2"), (b"PK\x03\x04", "zip"),
                 (b"{\\rtf", "rtf"), (b"\x1f\x8b", "gzip"), (b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF8", "gif"),
                 (b"\xef\xbb\xbf", "text"), (b"<?xml", "xml"))


def sniff_magic(blob: bytes) -> dict:
    """앞 16바이트 hex 와 추정 종류(pdf|hwp3|ole2|zip|rtf|gzip|png|jpeg|gif|xml|html|text|unknown). 실패 장부에 남겨 형식 분포를 볼 수 있게 한다(G-D19)."""
    head = bytes(blob[:16])
    lead = bytes(blob[:64])
    for sig, label in _MAGIC_LABELS:
        if lead.startswith(sig):
            return {"label": label, "hex": head.hex()}
    if _looks_html(blob):
        return {"label": "html", "hex": head.hex()}
    if head and all(b in (9, 10, 13) or 32 <= b < 127 for b in head):
        return {"label": "text", "hex": head.hex()}
    return {"label": "unknown", "hex": head.hex()}


def _looks_html(blob: bytes) -> bool:
    head = blob[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<!doctype html", b"<html")) or (b"<html" in head) or (b"<head" in head) or (b"<body" in head)


def detect_kind(blob: bytes) -> str:
    """내용만으로 판별: pdf | hwp | hwpx(PK 컨테이너) | html | unknown. 확장자는 넘기지 않는다(넘기면 확장자로 폴백해 오류 HTML 이 pdf 가 된다)."""
    if blob.startswith(HWP3_SIGNATURE):
        return "hwp3"
    k = kind_of(blob, "")
    if k in ("pdf", "hwp", "hwpx"):
        return k
    return "html" if _looks_html(blob) else "unknown"


_CTYPE_KIND = (("hwpx", "hwpx"), ("hwp", "hwp"), ("pdf", "pdf"), ("html", "html"), ("xhtml", "html"))
_EXT_KIND = {".pdf": "pdf", ".hwp": "hwp", ".hwpx": "hwpx", ".htm": "html", ".html": "html",
             ".xls": "xls", ".xlsx": "xlsx", ".doc": "doc", ".docx": "docx"}
# OLE2(xls/doc)는 HWP 와 매직 바이트가 같고, OOXML(xlsx/docx)은 HWPX 와 같은 zip 컨테이너다. 확장자 선언이 있을 때만 구별해 저장하고
# (추출 단계는 지원하지 않는 형식으로 보류 처리), 선언이 없으면 hwp/hwpx 로 본다.
_OLE_KINDS, _ZIP_KINDS = ("xls", "doc"), ("xlsx", "docx")


def kind_from_content_type(ctype: Optional[str]) -> Optional[str]:
    c = (ctype or "").lower()
    for needle, kind in _CTYPE_KIND:
        if needle in c:
            return kind
    return None


def kind_from_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    return _EXT_KIND.get(os.path.splitext(urllib.parse.urlsplit(name).path.lower())[1])


def _compatible(declared: str, detected: str) -> bool:
    if declared in ("hwp", "hwpx") + _OLE_KINDS + _ZIP_KINDS:
        return detected in ("hwp", "hwpx")
    return declared == detected


def stored_kind(att: dict, detected: str) -> str:
    """저장 형식 이름. OLE2/zip 컨테이너가 확장자 선언으로 xls/docx 등임이 드러나면 그 이름을 쓴다."""
    names = [k for k in (kind_from_name(att.get("url")), kind_from_name(att.get("label"))) if k]
    if detected == "hwp":
        return next((k for k in names if k in _OLE_KINDS), detected)
    if detected == "hwpx":
        return next((k for k in names if k in _ZIP_KINDS), detected)
    return detected


def check_content(att: dict, body: bytes, headers: dict) -> Optional[str]:
    """내려받은 본문이 선언과 맞는지. 문제가 있으면 ``'<error_class>: 설명'`` 문자열, 없으면 None.

    선언의 우선순위: HEAD 때 기록한 Content-Type(att.content_type)과 이번 응답의 Content-Type 중 하나라도 있으면 그 전부와 맞아야 하고,
    둘 다 없을 때만 URL·라벨의 확장자를 본다(확장자는 약한 증거).
    """
    detected = detect_kind(body)
    if detected not in KNOWN_KINDS:
        sig = sniff_magic(body)
        return (f"unknown_type: first bytes do not match pdf/hwp/hwpx/html (detected '{detected}', magic={sig['hex']}, "
                f"magic_kind={sig['label']}, size={len(body)})")
    strong = [k for k in (kind_from_content_type(att.get("content_type")), kind_from_content_type(headers.get("content-type")))
              if k]
    declared = strong or [k for k in (kind_from_name(att.get("url")), kind_from_name(att.get("label"))) if k]
    bad = [d for d in declared if not _compatible(d, detected)]
    if bad:
        return f"magic_mismatch: declared {sorted(set(declared))}, detected '{detected}'"
    return None


# ---------------------------------------------------------------- 보조
def _orig_filename(att: dict, headers: dict) -> Optional[str]:
    cd = headers.get("content-disposition") or ""
    name = None
    m = re.search(r"filename\*\s*=\s*([\w-]+)'[^']*'([^;]+)", cd, re.I)
    if m:
        try:
            name = urllib.parse.unquote(m.group(2).strip().strip('"'), encoding=m.group(1))
        except (LookupError, ValueError):
            name = None
    if not name:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        if m:
            raw = m.group(1).strip()
            try:                                   # 서버가 cp949 바이트를 latin-1 로 실어 보내는 경우
                name = raw.encode("latin-1").decode("cp949")
            except (UnicodeEncodeError, UnicodeDecodeError):
                name = raw
    if not name:
        name = att.get("label") or os.path.basename(urllib.parse.urlsplit(att["url"]).path) or None
    if not name:
        return None
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", name).strip()
    return name[:200] or None


def _post_target(url: str) -> Optional[tuple]:
    """(쿼리를 뗀 URL, 폼 데이터) 또는 None(GET 대상)."""
    parts = urllib.parse.urlsplit(url)
    if parts.path == POST_PATH and parts.query:
        data = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")), data
    return None


def _max_bytes(att: dict) -> int:
    n = att.get("content_length")
    if isinstance(n, int) and n >= 0:
        return n + SIZE_MARGIN
    return UNKNOWN_SIZE_CAP


def _file_ok(corpus_dir: Path, rel: Optional[str]) -> bool:
    if not rel:
        return False
    try:
        return stage.resolve(corpus_dir, rel).is_file()
    except ValueError:
        return False


# ---------------------------------------------------------------- 후보 선택
def select_candidates(manifest: Any, corpus_dir: Path, *, source: Optional[str] = None, tier_set: tuple = tiers.TIER_ORDER,
                      retry_exhausted: bool = False, att_ids: Optional[list] = None) -> tuple[list, dict]:
    """(내려받을 첨부 목록(순위 정렬), 요약). 요약: unmapped_sources, skipped_exhausted, rights_not_allowed."""
    rows = manifest.list_attachments(source_id=source, right=("store_raw", "allowed"))
    open_fail = {f["target"]: f for f in manifest.failures(open_only=True) if f["stage"] == "fetch"}
    out, unmapped, exhausted, shells = [], set(), 0, []
    want = set(att_ids) if att_ids else None
    for a in rows:
        if want is not None and a["att_id"] not in want:      # --att: 지정한 첨부만(게이트 표본용). 순위·소스 지정 없이도 고른다
            continue
        if a["status"] == "discovered" and withhold.viewer_shell_reason(a):      # G-D8: e-book 뷰어 껍데기는 받지 않는다
            shells.append(a["att_id"])
            continue
        t = tiers.tier_of(a["source_id"])
        if t is None:
            unmapped.add(a["source_id"])
            if source is None and want is None:      # 지정하지 않은 실행에서는 매핑 없는 소스를 건드리지 않는다(보고만)
                continue
        elif t not in tier_set and want is None:
            continue
        st = a["status"]
        if st == "discovered":
            pass
        elif st == "failed" and (not a["raw_path"] or not _file_ok(corpus_dir, a["raw_path"])):
            # raw_path 가 남아 있어도 파일이 없으면(fetched 였던 첨부의 재수신이 실패한 경우) 받지 못한 것과 같다. 파일이 있는 failed 는 추출 이후 단계의 실패다.
            f = open_fail.get(a["att_id"])
            if f and f["retry_count"] >= MAX_RETRIES_BEFORE_SKIP and not retry_exhausted:
                exhausted += 1
                continue
        elif st == "fetched" and not _file_ok(corpus_dir, a["raw_path"]):
            pass                                     # 장부에는 fetched 인데 파일이 없다: 다시 받는다
        else:
            continue
        out.append(a)
    out.sort(key=lambda a: tiers.sort_key(a["source_id"], a["att_id"]))
    return out, {"unmapped_sources": sorted(unmapped), "skipped_exhausted": exhausted, "viewer_shells_skipped": len(shells),
                 "viewer_shell_ids": shells}


# ---------------------------------------------------------------- 한 건
def fetch_one(ctx: Any, att: dict, family: str) -> dict:
    """첨부 한 건을 내려받아 저장한다. 반환: {'outcome': 'fetched'|'dedup'|'failed'|'dry_run', 'error_class': ...}.

    TimeBudgetExceeded · DryRunMiss · HostBusy 는 잡지 않고 올려 보낸다.
    """
    m = ctx.manifest
    att_id = att["att_id"]

    def fail(cls: str, msg: str, *, permanent: bool = False, exhaust: bool = False) -> dict:
        m.record_failure(att_id, "fetch", cls, msg, retry_count=MAX_RETRIES_BEFORE_SKIP + 1 if (permanent or exhaust) else None)
        # 허용 호스트 밖·robots 금지는 수집 대상이 아니라 보류(withheld)로 둔다: 실패 행은 남고, 문서 상태 계산에서는 빠진다
        m.set_attachment_status(att_id, "withheld" if permanent else "failed")
        stage.refresh_document_status(m, att["doc_id"])
        return {"outcome": "failed", "error_class": cls}

    post = _post_target(att["url"])
    try:
        accept = lambda body, headers: check_content(att, body, headers)  # noqa: E731
        if post:
            r = ctx.http.post(post[0], post[1], accept=accept, max_bytes=_max_bytes(att))
        else:
            r = ctx.http.get(att["url"], accept=accept, max_bytes=_max_bytes(att))
    except (TimeBudgetExceeded, DryRunMiss, HostBusy):
        raise
    except IngestionError as e:
        cls = getattr(e, "error_class", "error")
        return fail(cls, str(e), permanent=cls in PERMANENT_ERRORS)
    hwp3 = False
    if not r.ok:
        if r.content_error:
            cls, _, detail = r.content_error.partition(":")
            if cls.strip() == "unknown_type" and detect_kind(r.body) == "hwp3":
                hwp3 = True                                        # G-D19: 아래에서 kordoc 으로 1회 시험한다
            else:
                return fail(cls.strip() or "bad_content", detail.strip() or r.content_error)
        else:
            return fail(f"http_{r.status}", f"http {r.status}")

    body = r.body
    want = att.get("content_length")
    if isinstance(want, int) and want >= 0 and len(body) != want:
        _evict(ctx, att, post)
        return fail("size_mismatch", f"downloaded {len(body)} bytes, HEAD said {want}")
    if ctx.dry_run:
        return {"outcome": "dry_run", "bytes": len(body)}
    if hwp3:
        bad = _hwp3_trial(ctx, body)
        if bad:
            return fail(bad[0], bad[1], exhaust=bad[2])

    detected = stored_kind(att, detect_kind(body))
    sha = stage.sha256_bytes(body)
    canonical = m.select(
        "SELECT att_id, raw_path FROM attachments WHERE raw_sha256=? AND att_id<>? AND raw_path IS NOT NULL AND dedup_of IS NULL "
        "ORDER BY att_id LIMIT 1", (sha, att_id))
    dedup_of = None
    if canonical and _file_ok(ctx.corpus_dir, canonical[0]["raw_path"]):
        dedup_of, rel = canonical[0]["att_id"], canonical[0]["raw_path"]      # 같은 해시: 파일은 한 번만 저장한다
    else:
        rel = _store(ctx, family, sha, detected, body)
    fields = dict(raw_sha256=sha, raw_path=rel, orig_filename=_orig_filename(att, r.headers), dedup_of=dedup_of,
                  fetched_at=iso(ctx.now()), detected_type=detected, content_length=len(body))
    if not att.get("content_type") and r.headers.get("content-type"):
        fields["content_type"] = r.headers["content-type"]
    with m.transaction():
        m.update_attachment_fields(att_id, **fields)
        m.set_attachment_status(att_id, "fetched")
        m.resolve_failures(att_id, "fetch")
        stage.refresh_document_status(m, att["doc_id"])
    _evict(ctx, att, post)
    return {"outcome": "dedup" if dedup_of else "fetched", "bytes": len(body), "dedup_of": dedup_of}


def _hwp3_trial(ctx: Any, body: bytes) -> Optional[tuple]:
    """HWP 3.x 서명 본문을 kordoc 으로 1회 읽어 본다(G-D19). 읽히면 None, 아니면 (오류 클래스, 메시지, 재시도 상한 채움 여부).
    임시 파일은 코퍼스 tmp 에 쓰고 항상 지운다. kordoc 경로는 env KORDOC_BIN(ToolRunner)에서만 받는다."""
    sig = sniff_magic(body)
    runner = ctx.extras.get("tool_runner") or ToolRunner()
    tmp = Path(ctx.corpus_dir) / "tmp" / f"hwp3-{stage.sha256_bytes(body)[:16]}.hwp"
    try:
        stage.atomic_write_bytes(tmp, body)
        pages = hwp_mod.extract_hwp(runner, tmp)
    except hwp_mod.KordocUnavailable:
        return ("no_hwp_extractor", f"HWP 3.x signature (magic={sig['hex']}); kordoc is not installed, trial not run", False)
    except hwp_mod.KordocFailed as e:
        return ("hwp3_unsupported", f"HWP 3.x signature (magic={sig['hex']}, magic_kind=hwp3, size={len(body)}); kordoc cannot read it ({e.error_class})", True)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if not any(p.text.strip() for p in pages):
        return ("hwp3_unsupported", f"HWP 3.x signature (magic={sig['hex']}, magic_kind=hwp3, size={len(body)}); kordoc returned no text", True)
    return None


def _store(ctx: Any, family: str, sha: str, kind: str, body: bytes) -> str:
    path = Path(ctx.corpus_dir) / "raw" / family / f"{sha[:24]}.{kind}"
    if not (path.is_file() and path.stat().st_size == len(body)):
        stage.atomic_write_bytes(path, body)
    return stage.to_rel(Path(ctx.corpus_dir), path)


def _evict(ctx: Any, att: dict, post: Optional[tuple]) -> None:
    try:
        if post:
            ctx.http.evict(post[0], method="POST", data=post[1])
        else:
            ctx.http.evict(att["url"])
    except (IngestionError, OSError):
        pass


# ---------------------------------------------------------------- 실행
def run_fetch(ctx: Any, *, source: Optional[str] = None, tier: Optional[str] = None, limit: Optional[int] = None,
              retry_exhausted: bool = False, att_ids: Optional[list] = None) -> dict:
    tier_set = tiers.parse_tiers(tier)
    cands, info = select_candidates(ctx.manifest, ctx.corpus_dir, source=source, tier_set=tier_set,
                                    retry_exhausted=retry_exhausted, att_ids=att_ids)
    shell_ids = info.pop("viewer_shell_ids", [])
    if not ctx.dry_run:                                   # 껍데기는 장부에서도 보류로 굳힌다(다음 실행의 후보에서 빠지고 coverage 에 보인다)
        for att_id in shell_ids:
            withhold.mark_viewer_shell(ctx.manifest, att_id, att_id.split("#", 1)[0])
    fam_cache: dict = {}
    counts = {"fetched": 0, "dedup": 0, "failed": 0, "dry_run": 0}
    errors: dict = {}
    attempted = 0
    for att in cands:
        if limit is not None and attempted >= limit:
            break
        ctx.check_time()
        attempted += 1
        res = fetch_one(ctx, att, stage.family_of(ctx.manifest, att["source_id"], fam_cache))
        counts[res["outcome"]] += 1
        if res["outcome"] == "failed":
            errors[res["error_class"]] = errors.get(res["error_class"], 0) + 1
        ctx.logger.info("fetch %s %s", att["att_id"], res["outcome"])
    return {"stage": "fetch", "candidates": len(cands), "attempted": attempted, **counts, "errors": errors,
            "remaining": len(cands) - attempted, **info}

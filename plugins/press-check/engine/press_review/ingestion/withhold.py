"""보류(withheld) 일괄 표시: PAC e-book 뷰어 껍데기(P2_DESIGN G-D8).

``pac.or.kr/kor/ebook/`` 아래 주소의 ``other`` 첨부는 간행물 PDF 가 아니라 e-book 뷰어 페이지(껍데기)다. 같은 간행물의 PDF 는 따로 있다(PDF 가 없는 문서는
``without_pdf_sibling`` 으로 보고한다). 받지 않고 ``withheld`` 로 두며, 이유는 fetch 단계의 영구 보류와 같은 방식으로 실패 장부에 남긴다
(target=att_id, stage=fetch, error_class=viewer_shell, retry_count 상한 초과). 문서 상태 계산에서는 withheld 첨부가 빠진다(stage.refresh_document_status).

판별은 ``kind == other`` 가 아니라 호스트·경로다: 같은 ``other`` 라도 IENDC 의 HWP 첨부, PAC 상세 페이지 링크, 외부 블로그는 껍데기가 아니다.
어댑터가 껍데기를 다시 발견해도(``upsert_attachment`` 의 seen) 상태를 되돌리지 않는다.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Optional

from . import stage

VIEWER_SHELL = "viewer_shell"
PAC_HOST = "www.pac.or.kr"
EBOOK_PATH_PREFIX = "/kor/ebook/"
PERMANENT_RETRY_COUNT = 4                      # fetch.MAX_RETRIES_BEFORE_SKIP + 1 과 같은 뜻: 재시도 대상이 아니다
REASONS = (VIEWER_SHELL,)


def viewer_shell_reason(att: dict) -> Optional[str]:
    """첨부 행(kind, url)이 PAC e-book 뷰어 껍데기면 ``viewer_shell``, 아니면 None."""
    if att.get("kind") != "other":
        return None
    try:
        u = urllib.parse.urlsplit(att.get("url") or "")
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() != PAC_HOST:
        return None
    return VIEWER_SHELL if u.path.startswith(EBOOK_PATH_PREFIX) else None


def mark_viewer_shell(manifest: Any, att_id: str, doc_id: str) -> bool:
    """아직 받지 않은(discovered, 또는 raw 없는 failed) 첨부를 withheld 로 바꾼다. 바꿨으면 True."""
    rows = manifest.select("SELECT status, raw_path FROM attachments WHERE att_id=?", (att_id,))
    if not rows or rows[0]["raw_path"] or rows[0]["status"] not in ("discovered", "failed"):
        return False
    with manifest.transaction():
        manifest.record_failure(att_id, "fetch", VIEWER_SHELL, "PAC e-book viewer page, not a publication file; the same publication has a PDF",
                                retry_count=PERMANENT_RETRY_COUNT)
        manifest.set_attachment_status(att_id, "withheld")
        stage.refresh_document_status(manifest, doc_id)
    return True


def run_withhold(manifest: Any, *, reason: str = VIEWER_SHELL, source: Optional[str] = None, dry_run: bool = False) -> dict:
    if reason not in REASONS:
        raise ValueError(f"unknown reason '{reason}' ({', '.join(REASONS)})")
    matched = withheld = already = skipped_state = no_sibling = 0
    by_source: dict = {}
    shells = [a for a in manifest.list_attachments(source_id=source, kind="other") if viewer_shell_reason(a)]
    for a in shells:
        matched += 1
        by_source[a["source_id"]] = by_source.get(a["source_id"], 0) + 1
        sibs = [x for x in manifest.attachments_for(a["doc_id"]) if x["kind"] in ("full", "chapter") and x["status"] != "withheld"]
        if not sibs:
            no_sibling += 1
        if a["status"] == "withheld":
            already += 1
        elif dry_run:
            continue
        elif mark_viewer_shell(manifest, a["att_id"], a["doc_id"]):
            withheld += 1
        else:
            skipped_state += 1
    return {"reason": reason, "dry_run": bool(dry_run), "matched": matched, "withheld": withheld, "already_withheld": already,
            "would_withhold": (matched - already) if dry_run else 0, "skipped_other_state": skipped_state,
            "without_pdf_sibling": no_sibling, "by_source": dict(sorted(by_source.items()))}

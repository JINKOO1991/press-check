"""HWP/HWPX 추출(P2_DESIGN §2, UDEC-6): kordoc CLI(``kordoc <file> --format json``). 수집·운영 쪽 외부 도구다.

쪽 locator: ``metadata.pageMode == "layout"`` (한컴 저장본의 조판 캐시 기반 실제 쪽)이면 method=``kordoc`` 이고 쪽 번호가 pdf_page_no 열에 들어간다.
그 밖(``section`` 등 섹션 근사)은 method=``kordoc_section_approx`` 이고 pdf_page_no 는 비운다(실제 쪽 번호라고 적지 않는다). 구역 번호는 page_id 와
quality_json.section_index 에 둔다. kordoc 이 없으면 ``KordocUnavailable``(error_class=no_hwp_extractor), 실행 실패·success:false 면 ``KordocFailed``.
kordoc 내장 OCR(``--ocr``)은 tesseract 와의 비교가 끝나기 전까지 쓰지 않는다. 이 모듈은 PDF 경로로도 쓴다(표가 많은 PDF, G-D4).

PDF 는 쪽마다 ``-p N`` 으로 따로 부른다(``extract_pdf_page``). 쪽 범위 없이 부르면 kordoc 은 PDF 를 한 쪽으로 합쳐 낸다(2026-10-09 실측: pageCount 3 이어도
pages 는 1개, blocks 의 pageNumber 는 모두 1). 호출마다 파일 전체를 파싱하므로 큰 PDF 의 소스에는 쓰지 않는다(소스별 상수 ``PDF_EXTRACTOR_BY_SOURCE``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import quality
from .pdf import PageOut
from .tools import ToolError, ToolRunner

KORDOC_TIMEOUT = 300
LAYOUT_MODE = "layout"


class KordocUnavailable(Exception):
    error_class = "no_hwp_extractor"


class KordocFailed(Exception):
    def __init__(self, error_class: str, message: str = ""):
        super().__init__(message or error_class)
        self.error_class = error_class


def run_kordoc(runner: ToolRunner, path: Path, *, timeout: float = KORDOC_TIMEOUT, pages: Optional[str] = None) -> dict:
    exe = runner.kordoc_bin()
    if not exe:
        raise KordocUnavailable("kordoc not found (set KORDOC_BIN or put kordoc on PATH)")
    argv = [exe, str(path), "--format", "json", "--no-images", "--silent"]
    if pages:
        argv += ["-p", str(pages)]
    try:
        r = runner.run(argv, timeout=timeout)
    except ToolError as e:
        raise KordocFailed("kordoc_" + e.kind, str(e)) from e
    if r.returncode != 0:
        raise KordocFailed("kordoc_failed", f"kordoc rc={r.returncode}")
    raw = r.stdout.decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except ValueError:
        a, b = raw.find("{"), raw.rfind("}")
        try:
            data = json.loads(raw[a:b + 1]) if 0 <= a < b else None
        except ValueError:
            data = None
    if not isinstance(data, dict):
        raise KordocFailed("kordoc_bad_output", "kordoc stdout is not a JSON object")
    if data.get("success") is False:
        raise KordocFailed("kordoc_failed", str(data.get("error") or "success=false")[:200])
    return data


def kordoc_pages(data: dict) -> tuple:
    """kordoc JSON -> ([(번호, 텍스트)], pageMode). pages 가 없으면 blocks 를 pageNumber 로 묶고(표 블록이 없을 때만), 그것도 없으면 markdown 전체를 1구역으로(표 포함)."""
    mode = (data.get("metadata") or {}).get("pageMode") or "unknown"
    pages = data.get("pages")
    out: list = []
    if isinstance(pages, list) and pages:
        for i, p in enumerate(pages, 1):
            if isinstance(p, dict):
                no = p.get("pageNumber") if isinstance(p.get("pageNumber"), int) else i
                out.append((no, str(p.get("markdown") or "")))
    elif (isinstance(data.get("blocks"), list) and data["blocks"]
          and not any(isinstance(b, dict) and b.get("type") == "table" and not b.get("text") for b in data["blocks"])):
        # 표 블록은 ``text`` 가 없어서 blocks 로 묶으면 표가 통째로 빠진다(HWP 3.x 실측: 28KB 문서에서 442자만 남음). 표 블록이 있으면 아래 markdown 전체를 쓴다.
        by: dict = {}
        for b in data["blocks"]:
            if isinstance(b, dict) and b.get("text"):
                by.setdefault(b.get("pageNumber") or 1, []).append(str(b["text"]))
        out = [(no, "\n".join(t)) for no, t in sorted(by.items())]
    if not out and str(data.get("markdown") or "").strip():
        out = [(1, str(data["markdown"]))]
        mode = "unknown"
    return out, mode


def extract_hwp(runner: ToolRunner, path: Path) -> list:
    """HWP/HWPX 파일 -> PageOut 목록. 예외: KordocUnavailable, KordocFailed."""
    data = run_kordoc(runner, path)
    pages, mode = kordoc_pages(data)
    if not pages:
        raise KordocFailed("kordoc_empty", "kordoc returned no text")
    layout = mode == LAYOUT_MODE
    out = []
    for no, text in pages:
        text = text.replace("\r\n", "\n").replace("\x00", "").strip("\n")
        q, m, reasons = quality.assess(text)
        qj = {**{k: v for k, v in m.items() if k != "meaningful"}, "reasons": reasons, "page_mode": mode}
        if layout:
            out.append(PageOut(no, "p", text, "kordoc", q, qj, needs_review=m["chars"] == 0, pdf_page_no=no))
        else:
            qj["locator"] = "section_approx"
            qj["section_index"] = no
            out.append(PageOut(no, "s", text, "kordoc_section_approx", q, qj, needs_review=m["chars"] == 0, pdf_page_no=None))
    return out


def extract_pdf_page(runner: ToolRunner, path: Path, n: int, check_broken: bool = False) -> PageOut:
    """PDF 의 n 쪽을 kordoc 으로 추출한다(``-p n``). 예외: KordocUnavailable, KordocFailed(kordoc_failed·kordoc_empty·kordoc_page_mismatch 등).
    돌려준 쪽 번호가 요청과 다르면 다른 쪽의 텍스트를 이 쪽으로 저장하지 않으려고 실패로 본다."""
    data = run_kordoc(runner, path, pages=str(n))
    pages, mode = kordoc_pages(data)
    if not pages:
        raise KordocFailed("kordoc_empty", f"kordoc returned no text for page {n}")
    if len(pages) != 1 or pages[0][0] != n:
        raise KordocFailed("kordoc_page_mismatch", f"asked page {n}, got {[p[0] for p in pages]}")
    text = pages[0][1].replace("\r\n", "\n").replace("\x00", "").strip("\n")
    q, m, reasons = quality.assess(text, check_broken)
    qj = {**{k: v for k, v in m.items() if k != "meaningful"}, "reasons": reasons, "page_mode": mode}
    return PageOut(n, "p", text, "kordoc", q, qj, needs_review=m["chars"] == 0, pdf_page_no=n)

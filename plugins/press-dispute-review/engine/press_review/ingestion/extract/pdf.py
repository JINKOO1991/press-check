"""PDF 추출(P2_DESIGN §2): 페이지 단위 pdftotext -layout, 품질 미달 페이지만 pdftoppm + tesseract(kor+eng) OCR.

이식: 작성자 소유 코드(비공개).
- ``_pdftotext_layout``: ``pdftotext -layout`` 호출을 이식하되 결함을 고쳤다. 원본은 문서 전체를 하나의 문자열로 돌려줘 페이지 경계가 사라지고
  40자 미만이면 None(과소 산출을 버림)이었다. 여기서는 ``-f N -l N`` 으로 페이지마다 호출해 경계를 보존하고, 빈·실패 페이지도 행을 남긴다.
  pdfminer 폴백(``extract_pdf``)은 가져오지 않았다.
- ``ocr_pdf``: 래스터화·tesseract 호출을 이식하되 결함을 고쳤다. 원본의 12쪽 상한과 시간 초과 시 부분 산출을 없애고,
  품질 미달 페이지만 쪽 단위로 처리하며 페이지 번호·신뢰도(TSV)를 남긴다. 해상도 400dpi·``--psm 4`` 는 원본의 probe-ocr 실측 값을 따르고,
  언어는 P2_DESIGN 에 따라 ``kor+eng`` 다(원본 kor 단독; 원본은 kor+eng 가 한글을 라틴 조각으로 오인식한다고 기록했다 - OCR 줄 검수 표시로 보완).
"""
from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from . import layout, quality
from .tools import ToolError, ToolRunner

OCR_DPI = 400
OCR_PSM = "4"
OCR_LANG = "kor+eng"
PDFTOTEXT_TIMEOUT = 60
PDFTOPPM_TIMEOUT = 120
TESSERACT_TIMEOUT = 180
SIG_MIN_CHARS = 40

_WS = re.compile(r"\s+")


class PdfError(Exception):
    def __init__(self, error_class: str, message: str = ""):
        super().__init__(message or error_class)
        self.error_class = error_class


@dataclass
class PageOut:
    """한 쪽(또는 구역)의 추출 결과. ordinal: PDF 쪽 번호('p') 또는 구역 번호('s')."""
    ordinal: int
    kind: str                      # 'p' | 's'
    text: str
    method: str
    quality: float = 0.0
    quality_json: dict = field(default_factory=dict)
    ocr_confidence: Optional[float] = None
    needs_review: bool = False
    pdf_page_no: Optional[int] = None
    error_class: Optional[str] = None      # 쪽 단위 하드 오류(이 쪽은 비었지만 행은 남는다)


def norm_signature(text: str) -> Optional[str]:
    """쪽 정렬용 서명: 공백을 뺀 텍스트의 해시. 40자 미만이면 None(너무 짧아 구별 못 한다)."""
    s = _WS.sub("", text)
    if len(s) < SIG_MIN_CHARS:
        return None
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:20]


def clean_text(raw: bytes) -> str:
    t = raw.decode("utf-8-sig", "replace").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").replace("\f", "")
    return "\n".join(l.rstrip() for l in t.split("\n")).strip("\n")


def page_count(runner: ToolRunner, path: Path) -> int:
    try:
        r = runner.run(["pdfinfo", str(path)], timeout=60)
    except ToolError as e:
        raise PdfError("pdfinfo_unavailable" if e.kind == "missing" else "pdfinfo_failed", str(e)) from e
    m = re.search(rb"^Pages:\s+(\d+)", r.stdout, re.M)
    if r.returncode != 0 or not m or int(m.group(1)) < 1:
        raise PdfError("pdfinfo_failed", f"pdfinfo rc={r.returncode}, pages={m.group(1).decode() if m else None}")
    return int(m.group(1))


def text_layer(runner: ToolRunner, path: Path, n: int, layout: bool = True, raw: bool = False) -> tuple:
    """(텍스트, 오류 설명|None). pdftotext 가 실패해도 예외를 던지지 않는다(그 쪽은 OCR 로 넘기고 행은 남긴다).
    layout=False 는 ``-layout`` 없는 기본 읽기 순서 모드다(G-D14 추출기 ``pdftotext``). raw=True 는 ``-raw``(콘텐츠 스트림 순서, G-D28 ``pdftotext_raw``)이고 layout 보다 우선한다."""
    argv = ["pdftotext"] + (["-raw"] if raw else (["-layout"] if layout else [])) + ["-f", str(n), "-l", str(n), str(path), "-"]
    try:
        r = runner.run(argv, timeout=PDFTOTEXT_TIMEOUT)
    except ToolError as e:
        return "", f"pdftotext_{e.kind}"
    if r.returncode != 0:
        return "", f"pdftotext_rc{r.returncode}"
    return clean_text(r.stdout), None


@dataclass
class OcrPage:
    text: str
    confidence: Optional[float]
    lines: list            # [{'line': i, 'start': s, 'end': e, 'flags': [...]}] 검수 표시가 붙은 줄


def parse_tsv(tsv: bytes) -> OcrPage:
    """tesseract TSV -> 줄·문단 구조의 텍스트, 평균 신뢰도(0..1), 검수 줄.

    줄은 (block, par, line) 단위, 문단이 바뀌면 빈 줄 하나. 신뢰도 -1 인 구조 행은 건너뛴다.
    """
    rows = tsv.decode("utf-8", "replace").splitlines()
    lines: list = []                       # [(block, par, [words])]
    confs: list = []
    last_key = None
    for row in rows[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5":
            continue
        word = f[11].strip()
        try:
            conf = float(f[10])
        except ValueError:
            continue
        if not word or conf < 0:
            continue
        key = (f[2], f[3], f[4])
        if key != last_key:
            lines.append((f[2], f[3], []))
            last_key = key
        lines[-1][2].append(word)
        confs.append(conf)
    out, review, pos, prev_par = [], [], 0, None
    n = 0
    for block, par, words in lines:
        if prev_par is not None and (block, par) != prev_par:
            out.append("")
            pos += 1
        prev_par = (block, par)
        text = " ".join(words)
        flags = quality.line_flags(text)
        if flags:
            review.append({"line": n, "start": pos, "end": pos + len(text), "flags": flags})
        out.append(text)
        pos += len(text) + 1
        n += 1
    page_text = "\n".join(out)
    conf = round(sum(confs) / len(confs) / 100.0, 3) if confs else None
    return OcrPage(page_text, conf, review)


def ocr_page(runner: ToolRunner, path: Path, n: int, workdir_root: Optional[Path] = None) -> OcrPage:
    """한 쪽 OCR. 도구가 없거나 실패하면 ToolError/PdfError. 임시 폴더는 항상 지운다."""
    if workdir_root:
        Path(workdir_root).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ocr", dir=str(workdir_root) if workdir_root else None))
    try:
        stem = work / "page"
        r = runner.run(["pdftoppm", "-gray", "-r", str(OCR_DPI), "-f", str(n), "-l", str(n), "-singlefile", "-png",
                        str(path), str(stem)], timeout=PDFTOPPM_TIMEOUT)
        img = work / "page.png"
        if r.returncode != 0 or not img.is_file():
            raise PdfError("rasterize_failed", f"pdftoppm rc={r.returncode}")
        t = runner.run(["tesseract", str(img), "stdout", "-l", OCR_LANG, "--psm", OCR_PSM, "tsv"], timeout=TESSERACT_TIMEOUT)
        if t.returncode != 0:
            raise PdfError("tesseract_failed", f"tesseract rc={t.returncode}")
        return parse_tsv(t.stdout)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# pages.method 라벨: 기존 행(약 2천 쪽)이 ``-layout`` 출력에 ``pdftotext`` 를 써 왔으므로 그 라벨을 layout 모드에 그대로 둔다.
# 읽기 순서 모드는 ``pdftotext_reading``, -raw 는 ``pdftotext_raw``, 2단을 잘라 읽은 쪽은 ``pdftotext_columns`` 다. 옵션 이름(추출기 이름)은 쪽 quality_json.extractor 에 남는다.
METHOD_LAYOUT = "pdftotext"
METHOD_READING = "pdftotext_reading"
METHOD_RAW = "pdftotext_raw"
METHOD_COLUMNS = "pdftotext_columns"


def extract_page(runner: ToolRunner, path: Path, n: int, *, ocr: bool = True, workdir_root: Optional[Path] = None, layout: bool = True,
                 mode: Optional[str] = None, check_broken: bool = False) -> PageOut:
    """한 쪽 추출. mode: ``pdftotext_layout``(기본) | ``pdftotext``(읽기 순서) | ``pdftotext_raw`` | ``column_crop``(G-D28: 2단 쪽만 단별로 자르고 나머지는 -layout).
    ``layout`` 인자는 mode 가 없을 때만 본다(이전 호출 호환). 텍스트층이 깨졌거나 비었으면 그 쪽만 OCR 을 시도한다. 깨진 층 판정(``broken_layer``, G-D28)은
    ``check_broken=True`` 일 때만 한다(추출 단계는 ``BROKEN_LAYER_SOURCES`` 소스에만 켠다)."""
    mode = mode or ("pdftotext_layout" if layout else "pdftotext")
    columns = None
    if mode == "pdftotext_raw":
        text, err = text_layer(runner, path, n, raw=True)
        method = METHOD_RAW
    elif mode == "column_crop":
        text, err = text_layer(runner, path, n, True)
        method = METHOD_LAYOUT
        if not err:
            ctext, columns = column_crop_text(runner, path, n, text)
            if ctext is not None:
                text, method = ctext, METHOD_COLUMNS
    elif mode == "pdftotext":
        text, err = text_layer(runner, path, n, False)
        method = METHOD_READING
    else:
        text, err = text_layer(runner, path, n, True)
        method = METHOD_LAYOUT
    q, m, reasons = quality.assess(text, check_broken)
    qj: dict = {**{k: v for k, v in m.items() if k != "meaningful"}, "reasons": list(reasons), "extractor": mode}
    if columns is not None:
        qj["columns"] = columns
    if err:
        qj["error"] = err
    out = PageOut(n, "p", text, method, q, qj, pdf_page_no=n, error_class=err)
    if not reasons:
        return out
    if not ocr:
        out.needs_review = True
        qj["ocr"] = {"attempted": False, "reason": "disabled"}
        return out
    if not (runner.have("pdftoppm") and runner.have("tesseract")):
        out.needs_review = True
        qj["ocr"] = {"attempted": False, "reason": "tool_unavailable"}
        return out
    try:
        o = ocr_page(runner, path, n, workdir_root)
    except (ToolError, PdfError) as e:
        out.needs_review = True
        qj["ocr"] = {"attempted": True, "adopted": False, "error": getattr(e, "error_class", None) or f"tool_{e.kind}"}
        return out
    oq, om, oreasons = quality.assess(o.text, check_broken)
    info = {"attempted": True, "chars": om["chars"], "confidence": o.confidence, "reasons": oreasons}
    if quality.ocr_is_better(m, reasons, om, oreasons):
        info["adopted"] = True
        qj2 = {**{k: v for k, v in om.items() if k != "meaningful"}, "reasons": list(oreasons),
               "text_layer": {**{k: v for k, v in m.items() if k != "meaningful"}, "reasons": list(reasons)}, "ocr": info,
               "review_lines": o.lines}
        review = bool(o.lines) or (o.confidence is not None and o.confidence < quality.OCR_CONF_REVIEW) or bool(oreasons)
        return PageOut(n, "p", o.text, "ocr", oq, qj2, ocr_confidence=o.confidence, needs_review=review, pdf_page_no=n,
                       error_class=None)
    info["adopted"] = False
    qj["ocr"] = info
    blank = m["chars"] < quality.MIN_CHARS and om["chars"] < quality.MIN_CHARS and not err
    if blank:
        qj["blank"] = True                         # 텍스트 층과 OCR 이 모두 거의 비었다: 빈 쪽으로 확인됨
    else:
        out.needs_review = True
    return out


def iter_pages(runner: ToolRunner, path: Path, pages: Iterable[int], *, ocr: bool = True,
               workdir_root: Optional[Path] = None, check_time: Optional[Callable[[], None]] = None, layout: bool = True,
               mode: Optional[str] = None, check_broken: bool = False) -> Iterator[PageOut]:
    for n in pages:
        if check_time:
            check_time()
        yield extract_page(runner, path, n, ocr=ocr, workdir_root=workdir_root, layout=layout, mode=mode, check_broken=check_broken)


# ---------------------------------------------------------------- 2단 조판 쪽을 단별로 자르기(G-D28)
GUTTER_SEARCH = (0.30, 0.70)       # 단 사이 간격을 찾는 구간(쪽 너비 비율)
GUTTER_STEP_PT = 0.5
MIN_GUTTER_PT = 6.0                # 이보다 좁은 틈은 단 사이 간격이 아니다
MIN_SIDE_SHARE = 0.15              # 간격 양쪽에 각각 단어의 이 비율 이상이 있어야 한다
_PAGE_SIZE = re.compile(rb"^Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts", re.M)
_PAGE_ROT = re.compile(rb"^Page\s+(\d+)\s+rot:\s+(-?\d+)", re.M)
_WORD = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">')


def page_geometry(runner: ToolRunner, path: Path, n: int) -> tuple:
    """(너비 pt, 높이 pt, 회전도) — ``pdfinfo -f n -l n``. 실패하면 PdfError."""
    try:
        r = runner.run(["pdfinfo", "-f", str(n), "-l", str(n), str(path)], timeout=60)
    except ToolError as e:
        raise PdfError("pdfinfo_failed", str(e)) from e
    m, mr = _PAGE_SIZE.search(r.stdout), _PAGE_ROT.search(r.stdout)
    if r.returncode != 0 or not m or int(m.group(1)) != n:
        raise PdfError("pdfinfo_failed", f"no page {n} size")
    return float(m.group(2)), float(m.group(3)), int(mr.group(2)) if mr else 0


def word_boxes(runner: ToolRunner, path: Path, n: int) -> list:
    """``pdftotext -bbox`` 의 단어 상자 [(x0, y0, x1, y1)]. 좌표 단위는 pt. 실패하면 PdfError."""
    try:
        r = runner.run(["pdftotext", "-bbox", "-f", str(n), "-l", str(n), str(path), "-"], timeout=PDFTOTEXT_TIMEOUT)
    except ToolError as e:
        raise PdfError(f"pdftotext_{e.kind}", str(e)) from e
    if r.returncode != 0:
        raise PdfError(f"pdftotext_rc{r.returncode}")
    return [tuple(float(x) for x in m.groups()) for m in _WORD.finditer(r.stdout.decode("utf-8", "replace"))]


def find_gutter(boxes: list, width: float) -> Optional[tuple]:
    """단어 상자가 하나도 가로지르지 않는 세로선 중 가장 넓은 연속 구간 (가운데 x, 구간 폭). 단 사이 간격이 아니면 None.

    구간은 ``GUTTER_SEARCH`` 안에서만 찾고, 폭이 ``MIN_GUTTER_PT`` 이상이며 양쪽에 단어가 ``MIN_SIDE_SHARE`` 이상씩 있어야 한다(한쪽이 비면 2단이 아니다).
    구간이 둘 이상이면 폭이 같을 때 쪽 중앙에 가까운 것."""
    if not boxes or width <= 0:
        return None
    lo, hi = GUTTER_SEARCH[0] * width, GUTTER_SEARCH[1] * width
    runs: list = []
    cur: Optional[list] = None
    x = lo
    while x <= hi:
        if any(b[0] < x < b[2] for b in boxes):
            cur = None
        elif cur is None:
            cur = [x, x]
            runs.append(cur)
        else:
            cur[1] = x
        x += GUTTER_STEP_PT
    best = None
    for a, b in runs:
        w = b - a
        if w < MIN_GUTTER_PT:
            continue
        mid = (a + b) / 2
        left = sum(1 for bx in boxes if bx[2] <= mid)
        right = sum(1 for bx in boxes if bx[0] >= mid)
        if min(left, right) < MIN_SIDE_SHARE * len(boxes):
            continue
        key = (w, -abs(mid - width / 2))
        if best is None or key > best[0]:
            best = (key, mid, w)
    return (best[1], best[2]) if best else None


def _crop(runner: ToolRunner, path: Path, n: int, x: float, w: float, h: float) -> str:
    argv = ["pdftotext", "-layout", "-f", str(n), "-l", str(n), "-x", str(round(x)), "-y", "0", "-W", str(round(w)), "-H", str(round(h)), str(path), "-"]
    r = runner.run(argv, timeout=PDFTOTEXT_TIMEOUT)
    if r.returncode != 0:
        raise PdfError(f"pdftotext_rc{r.returncode}")
    return clean_text(r.stdout)


def _tokens(text: str) -> list:
    return sorted(text.split())


def column_crop_text(runner: ToolRunner, path: Path, n: int, layout_text: str) -> tuple:
    """(텍스트|None, 정보 dict). ``layout_text``(같은 쪽의 -layout 결과)에 쪽 대부분을 가로지르는 세로 공백 띠가 있을 때만 쪽을 좌·우 단으로 잘라(``-x -y -W -H``, 쪽 크기는 pdfinfo)
    왼쪽 단, 오른쪽 단 순서로 이어 붙인다. 잘린 두 쪽의 단어 목록이 자르지 않은 쪽의 단어 목록(``pdftotext`` 읽기 순서)과 같지 않으면(단어가 잘렸다) 쓰지 않는다.
    텍스트가 None 이면 호출자는 ``-layout`` 텍스트를 쓴다. 정보의 ``reason`` 에 이유가 남는다."""
    band = layout.two_column_band(layout_text)
    if band is None:
        return None, {"reason": "not_two_column"}
    try:
        w, h, rot = page_geometry(runner, path, n)
        if rot != 0:
            return None, {"reason": "page_rotated", "rot": rot}
        g = find_gutter(word_boxes(runner, path, n), w)
        if g is None:
            return None, {"reason": "no_gutter_in_word_boxes"}               # 혼합 쪽(위는 2단, 아래는 1단)이나 전폭 제목이 있는 쪽
        cut, gap = g
        left, right = _crop(runner, path, n, 0, cut, h), _crop(runner, path, n, cut, w - cut, h)
        whole, _err = text_layer(runner, path, n, layout=False)
    except (ToolError, PdfError) as e:
        return None, {"reason": "crop_failed", "error": getattr(e, "error_class", None) or f"tool_{e.kind}"}
    if _tokens(left + "\n" + right) != _tokens(whole):
        return None, {"reason": "words_split_by_cut", "cut_pt": round(cut, 1)}
    return left + "\n\n" + right, {"reason": "cropped", "cut_pt": round(cut, 1), "gutter_pt": round(gap, 1), "band_cols": list(band)}


TABLE_SAMPLE_PAGES = 8


def detect_table_pdf(runner: ToolRunner, path: Path) -> dict:
    """G-D26: 첨부가 표 PDF 인가. 쪽을 고르게 최대 ``TABLE_SAMPLE_PAGES`` 개 뽑아 ``pdftotext -layout`` 텍스트로 ``layout.table_pdf_verdict`` 를 낸다.
    결정적이다(같은 바이트면 같은 결과). pdfinfo 가 실패하면 PdfError."""
    total = page_count(runner, path)
    k = min(total, TABLE_SAMPLE_PAGES)
    picks = sorted({1 + (total - 1) * i // max(k - 1, 1) for i in range(k)}) if total > 1 else [1]
    v = layout.table_pdf_verdict([text_layer(runner, path, n, True)[0] for n in picks])
    return {**v, "sampled": len(picks), "total_pages": total}


def text_signatures(runner: ToolRunner, path: Path, total: int, check_time: Optional[Callable[[], None]] = None) -> dict:
    """{쪽: 서명|None}. 텍스트 층만 쓴다(OCR 없음). 장별/전체 쪽 정렬용."""
    out = {}
    for n in range(1, total + 1):
        if check_time:
            check_time()
        t, _ = text_layer(runner, path, n)
        out[n] = norm_signature(t)
    return out

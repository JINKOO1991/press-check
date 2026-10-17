"""실제 poppler/tesseract 로 합성 PDF 를 처리하는 통합 시험(도구가 없으면 건너뜀). 쪽 수는 pdfinfo 와 같아야 한다(P2_DESIGN §6)."""
import io
import json
import shutil
import subprocess

import pytest

from ext_helpers import FAMILY, SOURCE, make_pdf, page_text, place_raw, seed
from press_review.ingestion import __main__ as cli
from press_review.ingestion.manifest import Manifest

need = pytest.mark.skipif(not (shutil.which("pdftotext") and shutil.which("pdfinfo")), reason="poppler not installed")
need_ocr = pytest.mark.skipif(not (shutil.which("pdftoppm") and shutil.which("tesseract")), reason="OCR tools not installed")


def run(corpus, clock, *argv):
    out = io.StringIO()
    code = cli.main(["extract", *argv], clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus, log_stream=io.StringIO())
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def setup(manifest, corpus, pages):
    doc, (a1,) = seed(manifest, atts=[(1, "https://www.pac.or.kr/f.pdf", "full", None, "application/pdf", "PDF")])
    blob = make_pdf(pages)
    place_raw(manifest, corpus, a1, blob)
    manifest.close()
    return doc, a1, blob


@need
def test_real_pdftotext_page_count_equals_pdfinfo_and_printed_numbers(corpus, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    n = 5
    doc, a1, blob = setup(m, corpus, [page_text(i, i) for i in range(1, n + 1)])
    info = subprocess.run(["pdfinfo", str(next((corpus / "raw").rglob("*.pdf")))], capture_output=True, text=True).stdout
    assert f"Pages:           {n}" in info or f"Pages:          {n}" in info
    code, res = run(corpus, clock)
    assert code == 0 and res["result"]["outcomes"] == {"extracted": 1}
    m = Manifest(corpus / "manifest.sqlite")
    rows = m.pages_of(att_id=a1)
    assert len(rows) == n and [r["pdf_page_no"] for r in rows] == list(range(1, n + 1))
    assert [r["printed_page_no"] for r in rows] == [str(i) for i in range(1, n + 1)]
    for i, r in enumerate(rows, 1):
        t = (corpus / r["text_path"]).read_text(encoding="utf-8")
        assert f"synthetic page {i} of" in t and f"synthetic page {i + 1} of" not in t and r["method"] == "pdftotext"
    assert m.select("SELECT page_count FROM attachments WHERE att_id=?", (a1,))[0]["page_count"] == n
    m.close()


@need
@need_ocr
def test_real_ocr_on_blank_page_does_not_invent_text(corpus, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    doc, a1, blob = setup(m, corpus, [page_text(1, 1), "", page_text(3, 3), page_text(4, 4)])
    code, res = run(corpus, clock)
    assert code == 0
    m = Manifest(corpus / "manifest.sqlite")
    rows = m.pages_of(att_id=a1)
    assert len(rows) == 4
    q2 = json.loads(rows[1]["quality_json"])
    assert q2["ocr"]["attempted"] is True and rows[1]["method"] == "pdftotext" and q2.get("blank") is True and rows[1]["char_count"] == 0
    assert [r["printed_page_no"] for r in rows] == ["1", None, "3", "4"]                  # 빈 쪽의 번호는 채우지 않는다
    m.close()

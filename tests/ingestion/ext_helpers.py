"""P2 다운로드·추출 테스트용 도구: 합성 PDF(표준 라이브러리), 가짜 바이너리 실행기(subprocess 주입), 장부 시드.

실제 문서·개인정보 파일은 쓰지 않는다. 합성 PDF 는 Helvetica ASCII 텍스트만 담는다(pdftotext 통합 시험용).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from press_review.ingestion.extract.tools import ToolError, ToolResult, ToolRunner

SOURCE = "pac_board_committee_rules"      # PDF_EXTRACTOR_BY_SOURCE 에 없는 소스: 기본(pdftotext -layout) 동작을 시험하는 공용 소스(G-D14 선택 뒤 pac_adjustment_casebooks 는 pdftotext)
FAMILY = "pac_cases"


def make_pdf(pages: list) -> bytes:
    """텍스트 층이 있는 최소 PDF. pages: 쪽마다 줄 목록(또는 개행 구분 문자열). 빈 문자열이면 빈 쪽."""
    objs: list = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    cat = add(b"")                     # 1: catalog (뒤에서 채움)
    pgs = add(b"")                     # 2: pages
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for lines in pages:
        if isinstance(lines, str):
            lines = lines.split("\n") if lines else []
        ops = [b"BT", b"/F1 11 Tf", b"14 TL", b"56 760 Td"]
        for ln in lines:
            esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(b"(" + esc.encode("latin-1") + b") Tj T*")
        ops.append(b"ET")
        stream = b"\n".join(ops)
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page = add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Contents %d 0 R "
                   b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (pgs, content, font))
        kids.append(page)
    objs[cat - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pgs
    objs[pgs - 1] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, cat, xref)
    return bytes(out)


def page_text(i: int, n: int, body: str = "") -> str:
    """영문 본문 두 줄 + 가운데 정렬 쪽 번호 꼬리. 서로 다른 쪽이 서로 다른 텍스트가 되게 i 를 넣는다."""
    return (f"Chapter text for synthetic page {i} of the test casebook {body}\n"
            f"The committee considered the request and recorded item {i * 7} in the minutes\n\n\n- {n} -")


def tsv(paragraphs: list) -> bytes:
    """[[(줄 텍스트, 신뢰도 0~100), ...], ...] -> tesseract TSV. 문단=block, 줄=line, 단어는 공백 분리."""
    head = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    rows = [head, "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t"]
    for b, para in enumerate(paragraphs, 1):
        for ln, (text, conf) in enumerate(para, 1):
            for w, word in enumerate(text.split(), 1):
                rows.append(f"5\t1\t{b}\t1\t{ln}\t{w}\t0\t0\t10\t10\t{conf}\t{word}")
    return ("\n".join(rows) + "\n").encode("utf-8")


class FakeRunner(ToolRunner):
    """docs: {파일 바이트: 명세}. 명세 키: pages(쪽별 텍스트층), text_fail({쪽}), ocr({쪽: tsv bytes | ToolError}),
    pdfinfo_fail(bool), kordoc(dict JSON)|kordoc_rc. available: 있는 도구 이름 집합. calls: 호출 기록(argv)."""

    def __init__(self, docs=None, available=("pdfinfo", "pdftotext", "pdftoppm", "tesseract"), kordoc="/fake/kordoc",
                 clock=None, tick=1.0):
        self.clock, self.tick = clock, tick
        self.docs = dict(docs or {})
        self.available = set(available)
        self.kordoc = kordoc
        self.calls: list = []
        self._imgs: dict = {}

    def have(self, name):
        return name in self.available

    def kordoc_bin(self):
        return self.kordoc

    def count(self, tool):
        return sum(1 for a in self.calls if os.path.basename(a[0]) == tool)

    def _spec(self, path):
        data = Path(path).read_bytes()
        for k, v in self.docs.items():
            if data == k or data.startswith(k):
                return v
        raise AssertionError(f"FakeRunner has no spec for {path}")

    def run(self, argv, *, timeout):
        self.calls.append(list(argv))
        if self.clock is not None:
            self.clock.advance(self.tick)
        tool = os.path.basename(argv[0])
        if tool not in self.available and argv[0] != self.kordoc:
            raise ToolError("missing", f"{tool} not found")
        if tool == "pdfinfo":
            spec = self._spec(argv[1])
            if spec.get("pdfinfo_fail"):
                return ToolResult(1, b"", b"Syntax Error")
            return ToolResult(0, b"Title:  x\nPages:          %d\n" % len(spec["pages"]))
        if tool == "pdftotext":
            n = int(argv[argv.index("-f") + 1])
            spec = self._spec(argv[-2])
            if n in spec.get("text_fail", ()):
                return ToolResult(1, b"", b"Error")
            return ToolResult(0, spec["pages"][n - 1].encode("utf-8") + b"\n\x0c")
        if tool == "pdftoppm":
            n = int(argv[argv.index("-f") + 1])
            stem = argv[-1]
            if "-singlefile" not in argv:
                raise AssertionError("expected -singlefile")
            Path(stem + ".png").write_bytes(b"\x89PNG fake")
            self._imgs[stem + ".png"] = (self._spec(argv[-2]), n)
            return ToolResult(0)
        if tool == "tesseract":
            spec, n = self._imgs[argv[1]]
            o = spec.get("ocr", {}).get(n)
            if isinstance(o, Exception):
                raise o
            if o is None:
                return ToolResult(1, b"", b"no ocr scripted")
            assert "kor+eng" in argv
            return ToolResult(0, o)
        if argv[0] == self.kordoc:
            spec = self._spec(argv[1])
            if "kordoc_rc" in spec:
                return ToolResult(spec["kordoc_rc"], b"", b"boom")
            assert argv[2:4] == ["--format", "json"]
            return ToolResult(0, json.dumps(spec["kordoc"], ensure_ascii=False).encode("utf-8"))
        raise AssertionError(f"unexpected tool {argv}")


def seed(manifest, doc_key="100", *, source=SOURCE, family=FAMILY, atts=(), title="doc"):
    """문서 하나와 첨부들을 시드. atts: [(번호, url, kind, content_length, content_type, label)]. 반환 (doc_id, [att_id])."""
    manifest.ensure_source(source, family, source, "https://www.pac.or.kr/")
    doc_id = f"{family}:{doc_key}"
    manifest.upsert_document(doc_id, source, title=title, year=2020)
    ids = []
    for n, url, kind, clen, ctype, label in atts:
        aid = f"{doc_id}#{n}"
        manifest.upsert_attachment(aid, doc_id, url, label=label, kind=kind, content_length=clen, content_type=ctype)
        ids.append(aid)
    return doc_id, ids


def place_raw(manifest, corpus, att_id, blob: bytes, *, kind: str = "pdf", family: str = FAMILY, dedup_of=None):
    """fetch 단계가 끝난 상태를 만든다: 파일 저장 + 첨부 필드 + status=fetched."""
    import hashlib
    sha = hashlib.sha256(blob).hexdigest()
    rel = f"raw/{family}/{sha[:24]}.{kind}"
    (corpus / rel).parent.mkdir(parents=True, exist_ok=True)
    (corpus / rel).write_bytes(blob)
    manifest.update_attachment_fields(att_id, raw_sha256=sha, raw_path=rel, detected_type=kind, dedup_of=dedup_of,
                                      fetched_at="2026-10-09T00:00:00+00:00")
    manifest.set_attachment_status(att_id, "fetched")
    return rel

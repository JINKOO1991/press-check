"""추출 단계(P2_DESIGN §2): 페이지 경계·OCR 분기·재개·장별/전체·dedup·kordoc 미설치. 가짜 바이너리 실행기(subprocess 주입)."""
import io
import json

import pytest

from conftest import FakeClock
from ext_helpers import FAMILY, FakeRunner, SOURCE, make_pdf, page_text, place_raw, seed, tsv
from press_review.ingestion import __main__ as cli
from press_review.ingestion import extract as ext
from press_review.ingestion.extract.tools import ToolError
from press_review.ingestion.manifest import Manifest

U = "https://www.pac.or.kr/files/%s.pdf"


@pytest.fixture()
def env(corpus, clock, monkeypatch):
    class Env:
        pass

    e = Env()
    e.corpus, e.clock, e.mp = corpus, clock, monkeypatch
    e.m = Manifest(corpus / "manifest.sqlite", now=clock.now)

    def run(runner, *argv):
        e.m.close()
        monkeypatch.setattr(ext, "ToolRunner", lambda: runner)
        out = io.StringIO()
        code = cli.main(["extract", *argv], clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus, log_stream=io.StringIO())
        e.m = Manifest(corpus / "manifest.sqlite", now=clock.now)
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    e.run = run
    yield e
    e.m.close()


def mk(env, specs, blob, *, kind="pdf", doc_key="100", source=SOURCE, family=FAMILY):
    doc, ids = seed(env.m, doc_key, source=source, family=family, atts=specs)
    for aid in ids:
        pass
    return doc, ids


def simple_pdf(tag, n=3, **kw):
    """n 쪽 합성 문서: (바이트 지문, 명세)."""
    blob = make_pdf([f"{tag} page {i + 1}" for i in range(n)])
    spec = {"pages": [page_text(i + 1, i + 1, tag) for i in range(n)]}
    spec.update(kw)
    return blob, spec


def one_pdf(env, tag="A", n=3, **kw):
    doc, (a1,) = seed(env.m, atts=[(1, U % tag, "full", None, "application/pdf", "PDF")])
    blob, spec = simple_pdf(tag, n, **kw)
    place_raw(env.m, env.corpus, a1, blob)
    return doc, a1, blob, spec


def pages(env, aid):
    return env.m.pages_of(att_id=aid)


def test_page_boundaries_text_files_printed_numbers_and_skip_on_rerun(env):
    doc, a1, blob, spec = one_pdf(env, n=4)
    r = FakeRunner({blob: spec})
    code, res = env.run(r)
    assert code == 0 and res["result"]["outcomes"] == {"extracted": 1}
    rows = pages(env, a1)
    assert [p["pdf_page_no"] for p in rows] == [1, 2, 3, 4] and all(p["method"] == "pdftotext" for p in rows)
    assert [p["printed_page_no"] for p in rows] == ["1", "2", "3", "4"]
    for i, p in enumerate(rows):
        txt = (env.corpus / p["text_path"]).read_text(encoding="utf-8")
        assert txt == spec["pages"][i].strip("\n")
        assert p["text_path"].startswith("text/pac_cases/") and p["char_count"] == len(txt)
        assert f"page {i + 1}" in txt and f"page {i + 2}" not in txt            # 쪽 경계 유지
    row = env.m.select("SELECT * FROM attachments WHERE att_id=?", (a1,))[0]
    assert row["status"] == "extracted" and row["page_count"] == 4 and row["extractor_version"] == ext.EXTRACTOR_VERSION
    assert env.m.get_document(doc)["status"] == "extracted"
    n = len(r.calls)
    _, res2 = env.run(r)
    assert res2["result"]["outcomes"] == {"skipped": 1} and len(r.calls) == n   # 재시작: 완료 파일은 다시 처리하지 않는다


def test_ocr_runs_only_for_low_quality_page_with_confidence_and_line_flags(env):
    doc, a1, blob, spec = one_pdf(env, n=3)
    spec["pages"][1] = ""                                                       # 2쪽: 텍스트 층 없음(스캔)
    spec["ocr"] = {2: tsv([[("2019다12345 판결 선고", 91), ("원고의 청구를 기각한다", 88)], [("다음 문단은 평범한 설명입니다", 95)]])}
    r = FakeRunner({blob: spec})
    _, res = env.run(r)
    rows = {p["pdf_page_no"]: p for p in pages(env, a1)}
    assert r.count("pdftoppm") == 1 and r.count("tesseract") == 1
    assert [rows[i]["method"] for i in (1, 2, 3)] == ["pdftotext", "ocr", "pdftotext"]
    p2 = rows[2]
    assert p2["ocr_confidence"] == pytest.approx(0.91, abs=0.05) and p2["needs_review"] == 1
    qj = json.loads(p2["quality_json"])
    assert qj["ocr"]["adopted"] is True and qj["text_layer"]["chars"] == 0
    text = (env.corpus / p2["text_path"]).read_text(encoding="utf-8")
    flagged = {l["line"]: l for l in qj["review_lines"]}
    assert set(flagged) == {0, 1} and "case_number" in flagged[0]["flags"] and "negation" in flagged[1]["flags"]
    for l in flagged.values():
        assert text[l["start"]:l["end"]] in text.split("\n")                    # offset 이 저장된 텍스트 파일 기준
    assert "\n\n다음 문단" in text                                               # 문단 구분 유지
    assert rows[1]["needs_review"] == 0 and rows[3]["needs_review"] == 0
    assert res["result"]["totals"]["ocr_pages"] == 1


def test_no_page_cap_and_ocr_not_used_when_text_layer_is_fine(env):
    doc, a1, blob, spec = one_pdf(env, n=15)
    for i in range(15):
        spec["pages"][i] = "" if i % 2 == 0 else spec["pages"][i]              # 8쪽이 스캔: 원본의 12쪽 상한이 없다
    spec["ocr"] = {i + 1: tsv([[(f"scanned page number {i + 1} text here", 90)]]) for i in range(0, 15, 2)}
    r = FakeRunner({blob: spec})
    env.run(r)
    assert r.count("tesseract") == 8 and len(pages(env, a1)) == 15


def test_ocr_result_rejected_when_text_layer_is_better(env):
    doc, a1, blob, spec = one_pdf(env, n=3)
    spec["pages"][1] = "1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30"   # 숫자표: low_text_ratio
    spec["ocr"] = {2: tsv([[("1 2 3 4 5", 70)]])}
    r = FakeRunner({blob: spec})
    env.run(r)
    p2 = pages(env, a1)[1]
    qj = json.loads(p2["quality_json"])
    assert p2["method"] == "pdftotext" and qj["ocr"]["adopted"] is False and p2["needs_review"] == 1 and "low_text_ratio" in qj["reasons"]


def test_blank_page_confirmed_by_ocr_is_not_flagged(env):
    doc, a1, blob, spec = one_pdf(env, n=3)
    spec["pages"][2] = ""
    spec["ocr"] = {3: tsv([])}
    r = FakeRunner({blob: spec})
    env.run(r)
    p3 = pages(env, a1)[2]
    qj = json.loads(p3["quality_json"])
    assert p3["method"] == "pdftotext" and qj.get("blank") is True and p3["needs_review"] == 0 and p3["char_count"] == 0
    assert len(pages(env, a1)) == 3                                             # 빈 쪽도 행이 있다


def test_ocr_tools_missing_flags_page_but_does_not_hide_or_fail(env):
    doc, a1, blob, spec = one_pdf(env, n=3)
    spec["pages"][1] = ""
    r = FakeRunner({blob: spec}, available=("pdfinfo", "pdftotext"))
    _, res = env.run(r)
    p2 = pages(env, a1)[1]
    assert json.loads(p2["quality_json"])["ocr"] == {"attempted": False, "reason": "tool_unavailable"} and p2["needs_review"] == 1
    assert res["result"]["outcomes"] == {"extracted": 1}


def test_hard_page_error_fails_attachment_then_retries_only_that_page(env):
    doc, a1, blob, spec = one_pdf(env, n=3)
    spec["text_fail"] = {2}
    spec["ocr"] = {2: ToolError("timeout", "tesseract timed out")}
    r = FakeRunner({blob: spec})
    _, res = env.run(r)
    assert res["result"]["errors"] == {"page_errors": 1}
    row = env.m.select("SELECT * FROM attachments WHERE att_id=?", (a1,))[0]
    assert row["status"] == "failed" and env.m.get_document(doc)["status"] == "failed"
    assert len(pages(env, a1)) == 3 and pages(env, a1)[1]["needs_review"] == 1       # 실패 쪽도 행이 남는다
    assert {(f["target"], f["stage"]) for f in env.m.failures()} == {(a1 + ":p0002", "extract_page"), (a1, "extract")}
    spec["text_fail"] = set()
    spec["pages"][1] = page_text(2, 2, "A")
    n_text = r.count("pdftotext")
    _, res2 = env.run(r)
    assert res2["result"]["outcomes"] == {"extracted": 1} and r.count("pdftotext") == n_text + 1      # 오류 쪽만 다시
    assert env.m.select("SELECT status FROM attachments WHERE att_id=?", (a1,))[0]["status"] == "extracted"
    assert not [f for f in env.m.failures() if f["stage"] == "extract"]


def test_time_limit_midway_then_resume_extracts_each_page_exactly_once(env):
    doc, a1, blob, spec = one_pdf(env, n=8)
    r = FakeRunner({blob: spec}, clock=env.clock, tick=1.0)
    statuses = []
    for _ in range(10):
        _, res = env.run(r, "--max-seconds", "4")
        statuses.append(res["status"])
        if res["status"] == "completed":
            break
    assert statuses[0] == "paused_time_limit" and statuses[-1] == "completed" and len(statuses) >= 2
    text_calls = [c for c in r.calls if c[0] == "pdftotext"]
    seen = [c[c.index("-f") + 1] for c in text_calls]
    assert sorted(seen) == [str(i) for i in range(1, 9)]                              # 쪽마다 정확히 한 번
    assert len(pages(env, a1)) == 8


def test_changed_raw_hash_reextracts_from_that_stage_and_drops_old_pages(env):
    doc, a1, blob, spec = one_pdf(env, n=4)
    r = FakeRunner({blob: spec})
    env.run(r)
    assert len(pages(env, a1)) == 4
    blob2, spec2 = simple_pdf("A2", 2)
    place_raw(env.m, env.corpus, a1, blob2)
    r.docs[blob2] = spec2
    _, res = env.run(r)
    assert res["result"]["outcomes"] == {"extracted": 1} and len(pages(env, a1)) == 2
    assert not any(p.name.startswith("p0003") for p in (env.corpus / "text").rglob("*.txt"))


def test_dedup_attachment_gets_no_pages_and_no_tool_calls(env):
    doc, ids = seed(env.m, atts=[(1, U % "x", "full", None, None, "a"), (2, U % "y", "chapter", None, None, "b")])
    blob, spec = simple_pdf("D", 2)
    place_raw(env.m, env.corpus, ids[0], blob)
    place_raw(env.m, env.corpus, ids[1], blob, dedup_of=ids[0])
    r = FakeRunner({blob: spec})
    _, res = env.run(r)
    assert len(pages(env, ids[0])) == 2 and pages(env, ids[1]) == []
    assert env.m.select("SELECT status FROM attachments WHERE att_id=?", (ids[1],))[0]["status"] == "extracted"
    assert res["result"]["outcomes"] == {"extracted": 1, "dedup_skipped": 1}


def chapter_doc(env, full_pages=6, spans=((2, 3), (5, 6))):
    doc, ids = seed(env.m, atts=[(0, U % "full", "full", None, "application/pdf", "PDF")]
                    + [(10 + i, U % f"c{i}", "chapter", None, "application/pdf", "PDF") for i in range(len(spans))])
    full_texts = [page_text(i, i, "FULL") for i in range(1, full_pages + 1)]
    full = make_pdf([f"full {i}" for i in range(1, full_pages + 1)])
    docs = {full: {"pages": full_texts}}
    place_raw(env.m, env.corpus, ids[0], full)
    for k, (a, b) in enumerate(spans):
        blob = make_pdf([f"chapter {k} page {i}" for i in range(a, b + 1)])
        docs[blob] = {"pages": full_texts[a - 1:b]}
        place_raw(env.m, env.corpus, ids[1 + k], blob)
    return doc, ids, docs


def test_chapters_preferred_and_missing_pages_supplemented_from_full(env):
    doc, ids, docs = chapter_doc(env)
    r = FakeRunner(docs)
    _, res = env.run(r)
    assert res["result"]["outcomes"] == {"extracted": 3}
    full, c1, c2 = ids
    assert [p["pdf_page_no"] for p in pages(env, full)] == [1, 4]               # 장별이 덮지 못한 쪽만 전체에서
    assert len(pages(env, c1)) == 2 and len(pages(env, c2)) == 2
    note = json.loads(env.m.get_document(doc)["extra_json"])["p2_extract"]
    assert note["supplemented_pages"] == "1,4" and note["full_pages"] == 6 and note["chapter_pages"] == 4
    assert note["mode"] == "chapters_first" and note["full_att"] == full
    qj = json.loads(pages(env, full)[0]["quality_json"])
    assert qj["supplement"] is True
    n = len(r.calls)
    _, res2 = env.run(r)
    assert res2["result"]["outcomes"] == {"skipped": 3} and len(r.calls) == n   # 정렬을 다시 계산하지 않는다


def test_full_pdf_waits_for_chapters_not_yet_downloaded(env):
    doc, ids, docs = chapter_doc(env)
    env.m.set_attachment_status(ids[2], "discovered")                           # 마지막 장별을 아직 못 받았다
    r = FakeRunner(docs)
    _, res = env.run(r)
    assert res["result"]["outcomes"].get("deferred_waiting_chapters") == 1
    assert pages(env, ids[0]) == []


def test_fully_covered_full_is_superseded_without_pages(env):
    doc, ids, docs = chapter_doc(env, full_pages=4, spans=((1, 2), (3, 4)))
    r = FakeRunner(docs)
    env.run(r)
    assert pages(env, ids[0]) == [] and len(pages(env, ids[1])) == 2
    note = json.loads(env.m.get_document(doc)["extra_json"])["p2_extract"]
    assert note["mode"] == "full_superseded" and note["supplemented_pages"] == ""
    assert env.m.select("SELECT status FROM attachments WHERE att_id=?", (ids[0],))[0]["status"] == "extracted"


def test_unalignable_scans_are_recorded_not_guessed(env):
    doc, ids, docs = chapter_doc(env, full_pages=6, spans=((2, 3), (5, 6)))
    for spec in docs.values():
        spec["pages"] = [""] * len(spec["pages"])                               # 텍스트 층 없는 스캔: 서명을 못 만든다
        spec["ocr"] = {i + 1: tsv([[(f"scanned text line number {i}", 90)]]) for i in range(6)}
    r = FakeRunner(docs)
    env.run(r)
    note = json.loads(env.m.get_document(doc)["extra_json"])["p2_extract"]
    assert note["mode"] == "full_extracted_unaligned_may_duplicate_chapters" and note["unaligned"]
    assert len(pages(env, ids[0])) == 6                                          # 합계(4) < 전체(6): 전체를 모두 추출하되 사실을 기록


KORDOC_LAYOUT = {"success": True, "fileType": "hwp", "metadata": {"pageMode": "layout", "pageCount": 2},
                 "pages": [{"pageNumber": 1, "markdown": "첫 쪽 본문입니다. 심의 결과 안건 1호 원안 의결."},
                           {"pageNumber": 2, "markdown": "| 안건 | 결과 |\n| --- | --- |\n| 1 | 의결 |"}]}


def hwp_att(env, spec, key="500"):
    doc, (a1,) = seed(env.m, key, source="iendc_review_results", family="iendc",
                      atts=[(1, "https://www.iendc.go.kr/fileDownload.do?atchFileId=1", "other", None, None, "결과.hwp")])
    blob = b"\xd0\xcf\x11\xe0 hwp " + key.encode()
    place_raw(env.m, env.corpus, a1, blob, kind="hwp", family="iendc")
    return doc, a1, {blob: spec}


def test_kordoc_layout_pages_use_real_page_numbers(env):
    doc, a1, docs = hwp_att(env, {"kordoc": KORDOC_LAYOUT})
    _, res = env.run(FakeRunner(docs))
    rows = pages(env, a1)
    assert [(p["pdf_page_no"], p["method"]) for p in rows] == [(1, "kordoc"), (2, "kordoc")]
    assert "안건 1호" in (env.corpus / rows[0]["text_path"]).read_text(encoding="utf-8")


def test_kordoc_section_mode_is_marked_approx_and_not_a_page_number(env):
    data = {**KORDOC_LAYOUT, "metadata": {"pageMode": "section"}}
    doc, a1, docs = hwp_att(env, {"kordoc": data})
    env.run(FakeRunner(docs))
    rows = pages(env, a1)
    assert [(p["pdf_page_no"], p["method"]) for p in rows] == [(None, "kordoc_section_approx")] * 2
    assert json.loads(rows[1]["quality_json"])["locator"] == "section_approx" and rows[1]["page_id"].endswith(":s0002")


def test_kordoc_missing_is_recorded_as_failed_and_recovers_when_installed(env):
    doc, a1, docs = hwp_att(env, {"kordoc": KORDOC_LAYOUT})
    for _ in range(5):                                                           # 상한(3회)에 걸리지 않는다: 도구 미설치는 재시도 횟수로 세지 않는다
        _, res = env.run(FakeRunner(docs, kordoc=None))
        assert res["result"]["errors"] == {"no_hwp_extractor": 1}
    f = [x for x in env.m.failures() if x["target"] == a1][0]
    assert f["error_class"] == "no_hwp_extractor" and f["stage"] == "extract" and f["retry_count"] == 0
    assert env.m.get_document(doc)["status"] == "failed"
    _, res = env.run(FakeRunner(docs))
    assert res["result"]["outcomes"] == {"extracted": 1} and env.m.get_document(doc)["status"] == "extracted"
    assert not env.m.failures()


@pytest.mark.parametrize("spec,cls", [({"kordoc_rc": 2, "kordoc": {}}, "kordoc_failed"),
                                      ({"kordoc": {"success": False, "error": "bad file"}}, "kordoc_failed"),
                                      ({"kordoc": {"success": True, "pages": [], "markdown": ""}}, "kordoc_empty")])
def test_kordoc_failures_are_recorded(env, spec, cls):
    doc, a1, docs = hwp_att(env, spec)
    _, res = env.run(FakeRunner(docs))
    assert res["result"]["errors"] == {cls: 1}
    assert env.m.select("SELECT status FROM attachments WHERE att_id=?", (a1,))[0]["status"] == "failed"


def test_html_attachment_keeps_header_footer_and_table_cells(env):
    doc, (a1,) = seed(env.m, "900", source="pac_research_quarterly", family="pac_other",
                      atts=[(1, "https://www.pac.or.kr/x.html", "other", None, "text/html", "x.html")])
    html = ("<html><head><title>t</title><script>var x=1;</script></head><body><header>기관 머리말 텍스트</header>"
            "<h2>판시사항</h2><p>첫째 단락입니다.</p><table><tr><td>사건</td><td>2019다1234</td></tr>"
            "<tr><td>결과</td><td>기각</td></tr></table><h2>판결요지</h2><p>둘째 단락.</p><footer>꼬리말 출처 표시</footer></body></html>").encode()
    place_raw(env.m, env.corpus, a1, html, kind="html", family="pac_other")
    env.run(FakeRunner())
    rows = pages(env, a1)
    texts = [(env.corpus / p["text_path"]).read_text(encoding="utf-8") for p in rows]
    joined = "\n".join(texts)
    assert "기관 머리말 텍스트" in joined and "꼬리말 출처 표시" in joined and "var x" not in joined
    assert "사건\t2019다1234" in joined and "결과\t기각" in joined
    assert [p["method"] for p in rows] == ["html"] * len(rows) and len(rows) == 3
    assert json.loads(rows[1]["quality_json"])["section_label"] == "판시사항"


def test_pdfinfo_failure_marks_failed_and_is_not_hidden(env):
    doc, a1, blob, spec = one_pdf(env, n=2, pdfinfo_fail=True)
    _, res = env.run(FakeRunner({blob: spec}))
    assert res["result"]["errors"] == {"pdfinfo_failed": 1}
    assert pages(env, a1) == []


def test_missing_raw_file_sends_attachment_back_to_fetch(env):
    doc, a1, blob, spec = one_pdf(env, n=2)
    row = env.m.select("SELECT raw_path FROM attachments WHERE att_id=?", (a1,))[0]
    (env.corpus / row["raw_path"]).unlink()
    _, res = env.run(FakeRunner({blob: spec}))
    assert res["result"]["errors"] == {"raw_missing": 1}
    assert env.m.select("SELECT status FROM attachments WHERE att_id=?", (a1,))[0]["status"] == "discovered"


def test_extract_dry_run_changes_nothing(env):
    doc, a1, blob, spec = one_pdf(env, n=2)
    r = FakeRunner({blob: spec})
    _, res = env.run(r, "--dry-run")
    assert res["result"]["dry_run"] is True and res["result"]["attachments"] == 1 and r.calls == [] and pages(env, a1) == []


def test_full_waits_for_chapter_that_was_never_fetched(env):
    """실제 장부 상태: 못 받은 장별 첨부는 detected_type 이 비어 있다. 그래도 전체 PDF 를 먼저 추출하지 않는다."""
    doc, ids, docs = chapter_doc(env)
    env.m.update_attachment_fields(ids[2], raw_sha256=None, raw_path=None, detected_type=None)
    env.m.set_attachment_status(ids[2], "discovered")
    r = FakeRunner(docs)
    _, res = env.run(r)
    assert res["result"]["outcomes"].get("deferred_waiting_chapters") == 1 and pages(env, ids[0]) == []
    assert len(pages(env, ids[1])) == 2


def test_limit_counts_only_real_work_not_already_finished_attachments(env):
    docs = {}
    ids = []
    for k in ("A", "B", "C"):
        doc, (a,) = seed(env.m, "1" + k * 2, atts=[(1, U % k, "full", None, "application/pdf", "PDF")])
        blob, spec = simple_pdf(k, 2)
        place_raw(env.m, env.corpus, a, blob)
        docs[blob] = spec
        ids.append(a)
    r = FakeRunner(docs)
    _, r1 = env.run(r, "--limit", "1")
    _, r2 = env.run(r, "--limit", "1")                                          # 첫 첨부는 건너뛰고 둘째를 처리해야 한다
    done = [bool(pages(env, a)) for a in ids]
    assert done == [True, True, False]
    assert r2["result"]["outcomes"] == {"skipped": 1, "extracted": 1}


def test_unsupported_stored_type_is_withheld_with_failure_record_not_doc_failure(env):
    doc, ids = seed(env.m, "700", atts=[(1, U % "x", "full", None, None, "표.xls"), (2, U % "y", "full", None, None, "본문.pdf")])
    blob, spec = simple_pdf("P", 2)
    ole = b"\xd0\xcf\x11\xe0 xls bytes"
    place_raw(env.m, env.corpus, ids[1], blob)
    place_raw(env.m, env.corpus, ids[0], ole, kind="xls")
    r = FakeRunner({blob: spec})
    _, res = env.run(r)
    assert res["result"]["outcomes"] == {"extracted": 1, "withheld_unsupported": 1}
    assert env.m.select("SELECT status FROM attachments WHERE att_id=?", (ids[0],))[0]["status"] == "withheld"
    assert [f["error_class"] for f in env.m.failures() if f["target"] == ids[0]] == ["unsupported_type"]
    assert env.m.get_document(doc)["status"] == "extracted"                     # 보류된 첨부는 문서 상태를 막지 않는다


def test_att_filter_extracts_only_the_named_attachment_even_outside_a_known_tier(env):
    ids = []
    docs = {}
    for k in ("A", "B"):
        doc, (a,) = seed(env.m, "2" + k * 2, source="made_up_source", atts=[(1, U % k, "full", None, "application/pdf", "PDF")])
        blob, spec = simple_pdf(k, 2)
        place_raw(env.m, env.corpus, a, blob)
        docs[blob] = spec
        ids.append(a)
    r = FakeRunner(docs)
    _, r0 = env.run(r)                                                         # 필터 없음: 매핑 없는 소스는 건드리지 않는다
    assert r0["result"]["attempted"] == 0 and not pages(env, ids[0])
    _, r1 = env.run(r, "--att", ids[1])
    assert r1["result"]["attempted"] == 1 and not pages(env, ids[0]) and len(pages(env, ids[1])) == 2


def test_disk_guard_error_in_page_loop_pauses_without_failure_record(env):
    """페이지 루프의 check_time 에서 DiskGuardError 가 나면 첨부 실패로 삼키지 않고 위로 전파해 paused_disk_* 로 정지한다."""
    from press_review import diskguard
    from press_review.ingestion.context import RunContext
    doc, a1, blob, spec = one_pdf(env, n=3)
    n = {"c": 0}
    real = RunContext.check_time

    def check_time(self):
        n["c"] += 1
        if n["c"] >= 3:                       # 1: 문서 루프, 2: 첨부 루프, 3~: 쪽 루프(try 안)
            raise diskguard.DiskGuardError("low", 1, 2, "부족")
        real(self)
    env.mp.setattr(RunContext, "check_time", check_time)
    code, res = env.run(FakeRunner({blob: spec}))
    assert code == diskguard.EXIT_LOW and res["status"] == "paused_disk_low"
    assert env.m.select("SELECT * FROM attachments WHERE att_id=?", (a1,))[0]["status"] != "failed"
    assert env.m.select("SELECT * FROM failures WHERE error_class='DiskGuardError'") == []


def test_extract_start_checks_candidate_count(env):
    """후보 첨부 수 x 항목당 예상량이 여유를 넘으면 첫 첨부를 건드리기 전에 low 로 정지한다."""
    from press_review import diskguard
    env.mp.setenv("PRESS_DISK_STRUCT_MB", "1024")
    env.mp.setattr(diskguard, "_default_avail", lambda p: 20 * 1024 ** 3)
    ids = []
    for i in range(30):
        doc, (a,) = seed(env.m, doc_key=str(200 + i), atts=[(1, U % f"G{i}", "full", None, "application/pdf", "PDF")])
        blob, spec = simple_pdf(f"G{i}", 1)
        place_raw(env.m, env.corpus, a, blob)
        ids.append(a)
    code, res = env.run(FakeRunner({}))
    assert code == diskguard.EXIT_LOW and res["status"] == "paused_disk_low"
    assert all(r["status"] not in ("extracted", "failed") for r in env.m.select("SELECT status FROM attachments"))
    code2, res2 = env.run(FakeRunner({}), "--limit", "5")                    # limit 적용 후 5개 = 5GB 이므로 시작은 통과
    assert res2["status"] != "paused_disk_low"

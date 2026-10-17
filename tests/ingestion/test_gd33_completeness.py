"""G-D33 완결성 검사: 본문 사건번호 수 대비 장부 사건 수(합성 장부·본문, 네트워크 없음)."""
import io
import json

import pdr_ing_paths  # noqa: F401
from press_review.ingestion import admission
from press_review.ingestion.__main__ import main
from press_review.ingestion.manifest import Manifest

PAC = "pac_board_correction_status"
IEN = "iendc_review_results"


def body(nums):
    return "# 시정권고 현황\n\n| 순번 | 안건번호 |\n" + "".join(f"| {i} | 2016안건<br>{n} | 매체 |\n" for i, n in enumerate(nums, 1))


def build(corpus, specs):
    """specs: [(소스, 이름, 본문 번호 목록, 장부 번호 목록)]. 모든 사건은 인용 일치 1/1, 격리 0."""
    corpus.mkdir(parents=True, exist_ok=True)
    m = Manifest(corpus / "manifest.sqlite")
    for src in (PAC, IEN):
        m.ensure_source(src, "pac_cases", "합성")
    for src, name, body_nums, ledger_nums in specs:
        doc, att = f"{src}:{name}", f"{src}:{name}#1"
        m.upsert_document(doc, src, title=name)
        m.upsert_attachment(att, doc, f"https://example.invalid/{name}.pdf", kind="full")
        (corpus / f"{name}.txt").write_text(body(body_nums), encoding="utf-8")
        m.upsert_page(f"{att}:p1", doc, att_id=att, method="pdf_text", text_path=f"{name}.txt")
        for i, n in enumerate(ledger_nums):
            m.upsert_case(f"{doc}:{i}", doc, {"case_number": f"2016안건{n}" if n is not None else None}, att_id=att,
                          quote_total=1, quote_ok=1, verification_status="unverified")
    m.close()
    return corpus


def run(corpus, source):
    out = io.StringIO()
    assert main(["admission", "--source", source, "--dry-run"], corpus=corpus, stdout=out) == 0
    rep = json.loads(out.getvalue())
    return rep, {a["att_id"].split(":")[1].split("#")[0]: a for a in rep["attachments"]}


def test_completeness_cases(tmp_path):
    full = list(range(1, 21))
    build(tmp_path / "c", [
        (PAC, "full", full, full),                          # (a) 완결
        (PAC, "half", full, full[:10]),                     # (b) 절반 누락: 인용·격리는 통과하지만 제외돼야 한다
        (PAC, "dup", full, full + full[:3]),                # (e) 같은 번호 중복 행은 한 행으로 센다
        (IEN, "undeclared", full, full),                    # (d) 정규식 미선언 소스
    ])
    rep, by = run(tmp_path / "c", PAC)
    assert by["full"]["status"] == "admitted" and (by["full"]["body_rows"], by["full"]["ledger_rows"]) == (20, 20)
    assert by["half"]["status"] == "excluded" and by["half"]["reasons"] == ["completeness 10/20 < 0.95"]
    assert by["dup"]["status"] == "admitted" and by["dup"]["cases"] == 23 and by["dup"]["ledger_rows"] == 20
    assert rep["summary"]["completeness_failed"] == 1 and rep["criteria"]["min_completeness"] == admission.MIN_COMPLETENESS
    rep, by = run(tmp_path / "c", IEN)
    assert by["undeclared"]["status"] == "admitted" and by["undeclared"]["notes"] == ["완결성 미검사(사건번호 정규식 미선언)"]
    assert rep["summary"]["completeness_unchecked"] == 1


def test_no_case_numbers_in_body_is_unchecked_not_failed(tmp_path):        # (c) 본문 사건번호 0
    build(tmp_path / "c", [(PAC, "zero", [], [None, None])])
    _, by = run(tmp_path / "c", PAC)
    assert by["zero"]["status"] == "admitted" and by["zero"]["notes"] == ["완결성 미검사(본문에서 사건번호 0건)"]
    assert admission.judge(4, 4, 0, 0, numbered_cases=0, body_rows=0) == ("not_applicable", ["no_case_rows"])


def test_apply_records_completeness_failure_distinguishable(tmp_path):
    build(tmp_path / "c", [(PAC, "half", list(range(1, 11)), [1, 2, 3, 4, 5])])
    out = io.StringIO()
    main(["admission", "--source", PAC, "--apply"], corpus=tmp_path / "c", stdout=out)
    m = Manifest(tmp_path / "c" / "manifest.sqlite", in_memory_copy=True)
    fails = [f for f in m.failures() if f["stage"] == "admission"]
    assert len(fails) == 1 and fails[0]["error_class"] == "attachment_excluded" and "completeness 5/10" in fails[0]["message"]
    m.close()

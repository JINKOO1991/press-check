"""G-D33(docs/P2_DESIGN.md): 시정권고 현황·IENDC 첨부 단위 인입 기준(인용 85% 이상, 격리 20% 이하). 합성 장부, 네트워크 없음."""
import io
import json

import pytest

import pdr_ing_paths  # noqa: F401
from press_review import corpus_sqlite
from press_review.ingestion import admission
from press_review.ingestion.__main__ import main
from press_review.ingestion.manifest import Manifest

SRC = "pac_board_correction_status"
ELECTION = "pac_election_whitepapers"


def build(corpus):
    corpus.mkdir(parents=True, exist_ok=True)
    m = Manifest(corpus / "manifest.sqlite")
    m.ensure_source(SRC, "pac_cases", "합성 현황")
    m.ensure_source(ELECTION, "pac_other", "합성 선거백서")
    # (문서, 사건 수, 격리 수, 사건당 인용 총계, 사건당 일치) -> 첨부 #1 경계(85%/20%) 통과, #2 인용 미달, #3 격리 초과
    specs = {SRC: [("s1", 10, 2, 10, [8] * 5 + [9] * 5),     # 일치 85/100, 격리 2/10: 경계값 통과
                   ("s2", 10, 0, 10, [8] * 10),               # 80/100: 인용 미달
                   ("s3", 10, 3, 10, [10] * 10)],             # 격리 3/10: 초과
             ELECTION: [("e1", 10, 5, 10, [5] * 10)]}
    for src, items in specs.items():
        for name, n, q, tot, oks in items:
            doc = f"{src}:{name}"
            m.upsert_document(doc, src, title=name)
            att = f"{doc}#1"
            m.upsert_attachment(att, doc, f"https://example.invalid/{name}.pdf", kind="full")
            for i in range(n):
                m.upsert_case(f"{doc}:{i}", doc, {"case_id": f"{doc}:{i}"}, att_id=att, quote_total=tot, quote_ok=min(oks[i], tot),
                              verification_status="quarantined" if i < q else "unverified")
                m.upsert_chunk(f"{doc}:{i}#c1", doc, f"합성 사건 {name} {i}의 본문 문장이다.", "pdf_page", "1", case_id=f"{doc}:{i}",
                               authority_type="pac_decision")
    doc = f"{SRC}:s4"      # 구조화되지 않은 첨부(사건 0건)
    m.upsert_document(doc, SRC, title="s4")
    m.upsert_attachment(doc + "#1", doc, "https://example.invalid/s4.pdf", kind="full")
    m.close()
    return corpus / "manifest.sqlite"


def run(corpus, *args):
    out = io.StringIO()
    code = main(["admission", *args], corpus=corpus, stdout=out)
    return code, json.loads(out.getvalue())


def test_judge_boundaries():
    assert admission.judge(10, 2, 100, 85)[0] == "admitted"          # 정확히 85%, 20% 는 통과
    assert admission.judge(10, 2, 100, 84)[0] == "excluded"
    assert admission.judge(10, 3, 100, 100)[0] == "excluded"
    assert admission.judge(0, 0, 0, 0)[0] == "unevaluated"


def test_dry_run_leaves_ledger_unchanged_and_reports(tmp_path):
    path = build(tmp_path / "c")
    before = path.read_bytes()
    code, rep = run(tmp_path / "c", "--source", SRC, "--dry-run")
    assert code == 0 and path.read_bytes() == before
    assert rep["summary"] == {"admitted": 1, "excluded": 2, "unevaluated": 1, "completeness_failed": 0, "completeness_unchecked": 3, "attachments": 4}
    by = {a["att_id"].split(":")[1]: a for a in rep["attachments"]}
    assert by["s1#1"]["status"] == "admitted" and by["s2#1"]["status"] == "excluded" and by["s3#1"]["status"] == "excluded"
    run(tmp_path / "c", "--source", SRC)                               # --apply 없이도 쓰지 않는다
    assert path.read_bytes() == before


def test_election_whitepapers_not_covered(tmp_path):
    build(tmp_path / "c")
    with pytest.raises(SystemExit) as e:
        run(tmp_path / "c", "--source", ELECTION)
    assert e.value.code == 2
    with pytest.raises(ValueError):
        admission.evaluate(Manifest(tmp_path / "c" / "manifest.sqlite", in_memory_copy=True), ELECTION)


def test_apply_records_failures_and_index_skips_cases(tmp_path):
    path = build(tmp_path / "c")
    code, rep = run(tmp_path / "c", "--source", SRC, "--apply")
    assert rep["written"] == {"failures_recorded": 2, "failures_resolved": 0}
    m = Manifest(path, in_memory_copy=True)
    fails = [f for f in m.failures() if f["stage"] == "admission"]
    assert sorted(f["target"] for f in fails) == [f"{SRC}:s2#1", f"{SRC}:s3#1"]
    assert all(f["error_class"] == "attachment_excluded" and "G-D33" in f["message"] for f in fails)
    m.close()
    res = corpus_sqlite.build_index(path, tmp_path / "idx", dry_run=True)
    assert res["excluded"]["case_att_excluded"] == 17          # s2 10건 + s3 비격리 7건(격리 3건은 먼저 case_quarantined)
    assert res["passages"] == 8 + 5                            # s1 비격리 8 + 선거백서 5(적용 대상 아님)

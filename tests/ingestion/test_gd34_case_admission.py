"""G-D34: 사건 단위 인입 평가(admission --source lawgo_prec_supreme|lawgo_detc), 색인 빌더 제외, 수동 격리 이행. 합성 장부, 실제 코퍼스 접근 없음."""
import io
import json

import pytest

import pdr_ing_paths  # noqa: F401
from press_review import corpus_sqlite
from press_review.ingestion import admission
from press_review.ingestion.__main__ import main
from press_review.ingestion.manifest import Manifest

PREC, DETC, OTHER = "lawgo_prec_supreme", "lawgo_detc", "pac_board_correction_status"


def add_case(m, src, cid, qt, qok, status="unverified", number=None, rec_status="unverified"):
    doc = f"{src}:{cid}"
    m.upsert_document(doc, src, title=cid)
    m.upsert_case(cid, doc, {"case_id": cid, "case_number": number, "verification_status": rec_status}, quote_total=qt, quote_ok=qok,
                  verification_status=status)
    m.upsert_chunk(f"{cid}#c1", doc, f"합성 사건 {cid}의 본문 문장이다.", "pdf_page", "1", case_id=cid, authority_type="pac_decision")


def build(corpus):
    corpus.mkdir(parents=True, exist_ok=True)
    m = Manifest(corpus / "manifest.sqlite")
    for s in (PREC, DETC, OTHER):
        m.ensure_source(s, "pac_cases", s)
    add_case(m, PREC, "lawgo_prec:1:ok", 10, 9, number="A1")
    add_case(m, PREC, "lawgo_prec:2:low", 10, 8, number="B1")          # 0.80 제외
    add_case(m, PREC, "lawgo_prec:3:low", 10, 8, number="B1")          # 같은 사건번호 B1: 둘 다 제외
    add_case(m, PREC, "lawgo_prec:4:edge", 100, 85, number="C1")        # 정확히 0.85 통과
    add_case(m, PREC, "lawgo_prec:5:mix1", 10, 9, number="D1")
    add_case(m, PREC, "lawgo_prec:6:mix2", 10, 1, number="D1")          # D1 은 일부만 제외
    add_case(m, PREC, "lawgo_prec:7:noq", 0, 0, number="E1")
    add_case(m, PREC, "lawgo_prec:8:quar", 10, 1, status="quarantined", number="F1")
    add_case(m, DETC, "lawgo_detc:1:low", 10, 2, number="H1")
    add_case(m, OTHER, "pac:1:low", 10, 2)
    m.close()
    return corpus / "manifest.sqlite"


def run(corpus, *args, expect=0):
    out = io.StringIO()
    code = main(["admission", *args], corpus=corpus, stdout=out)
    assert code == expect
    return json.loads(out.getvalue())


def open_rows(path, cls="case_excluded"):
    m = Manifest(path, in_memory_copy=True)
    r = sorted(f["target"] for f in m.failures() if f["stage"] == "admission" and f["error_class"] == cls)
    m.close()
    return r


def test_case_dry_run_counts_and_no_write(tmp_path):
    path = build(tmp_path / "c")
    before = path.read_bytes()
    rep = run(tmp_path / "c", "--source", PREC)
    assert path.read_bytes() == before and rep["written"] is None and rep["applied"] is False
    s = rep["summary"]
    assert (s["evaluated"], s["admitted"], s["excluded"], s["no_quotes"], s["skipped_quarantined"]) == (8, 3, 3, 1, 1)
    assert s["pending_changes"] == 3 and s["orphans_resolved"] == 0 and s["case_number_partial"] == 1
    assert rep["excluded_cases"] == ["lawgo_prec:2:low", "lawgo_prec:3:low", "lawgo_prec:6:mix2"]
    run(tmp_path / "c", "--source", PREC, "--apply", "--dry-run")
    assert path.read_bytes() == before


def test_apply_records_resolves_and_is_idempotent(tmp_path):
    path = build(tmp_path / "c")
    rep = run(tmp_path / "c", "--source", PREC, "--apply")
    assert rep["written"] == {"failures_recorded": 3, "failures_resolved": 0}
    m = Manifest(path, in_memory_copy=True)
    f = [x for x in m.failures() if x["error_class"] == "case_excluded"]
    assert all(x["message"].startswith("G-D34 case admission: quote_rate ") and x["message"].endswith("< 0.85") for x in f)
    m.close()
    assert run(tmp_path / "c", "--source", PREC)["summary"]["pending_changes"] == 0
    # 인용이 고쳐져 통과하면 이전 행을 해결한다
    m = Manifest(path)
    m.upsert_case("lawgo_prec:2:low", f"{PREC}:lawgo_prec:2:low", {"case_id": "x"}, quote_total=10, quote_ok=10)
    m.close()
    rep = run(tmp_path / "c", "--source", PREC, "--apply")
    assert rep["summary"]["pending_changes"] == 1 and rep["written"] == {"failures_recorded": 0, "failures_resolved": 1}
    assert open_rows(path) == ["lawgo_prec:3:low", "lawgo_prec:6:mix2"]


def test_orphan_resolved_only_within_source_prefix_and_not_with_case(tmp_path):
    path = build(tmp_path / "c")
    m = Manifest(path)
    for t in ("lawgo_prec:99:gone", "lawgo_detc:99:gone", "lawgo_prec:146:migrated"):
        m.record_failure(t, "admission", "case_excluded", "x", retry_count=0)
    m.close()
    rep = run(tmp_path / "c", "--source", PREC, "--case", "lawgo_prec:2:low", "--apply")
    assert rep["summary"]["orphans_resolved"] == 0 and rep["summary"]["evaluated"] == 1       # --case: orphan 해제 끔
    assert "lawgo_prec:99:gone" in open_rows(path)
    rep = run(tmp_path / "c", "--source", DETC, "--apply")                                   # 다른 소스 apply 는 판례 행을 건드리지 않는다
    assert rep["summary"]["orphans_resolved"] == 1
    rows = open_rows(path)
    assert "lawgo_detc:99:gone" not in rows and "lawgo_prec:99:gone" in rows and "lawgo_prec:146:migrated" in rows
    rep = run(tmp_path / "c", "--source", PREC, "--apply")                                   # 판례 apply 는 판례 접두 orphan 만
    assert rep["summary"]["orphans_resolved"] == 2
    assert "lawgo_detc:1:low" in open_rows(path) and "lawgo_prec:99:gone" not in open_rows(path)


def test_attachment_cli_unchanged_for_non_case_source(tmp_path):
    build(tmp_path / "c")
    rep = run(tmp_path / "c", "--source", OTHER)
    assert "attachments" in rep and "excluded_cases" not in rep


def test_builder_excludes_with_own_stat_and_same_snapshot(tmp_path):
    path = build(tmp_path / "c")
    m = Manifest(path)
    m.set_case_verification("lawgo_prec:2:low", "quarantined")
    m.close()
    a = corpus_sqlite.build_index(path, tmp_path / "i1", dry_run=True)
    m = Manifest(path)
    m.set_case_verification("lawgo_prec:2:low", "unverified")
    m.record_failure("lawgo_prec:2:low", "admission", "case_excluded", "x", retry_count=0)
    m.close()
    b = corpus_sqlite.build_index(path, tmp_path / "i2", dry_run=True)
    assert a["excluded"]["case_quarantined"] - b["excluded"].get("case_quarantined", 0) >= 1      # 격리 -> 사건 제외로 이름이 바뀐다
    assert b["excluded"]["case_admission_excluded"] == 1 and b["excluded"]["case_quarantined"] == a["excluded"]["case_quarantined"] - 1
    assert a["passages"] == b["passages"] and a["snapshot_id"] == b["snapshot_id"]


# ---- 수동 이행 ----
MANUAL = ["lawgo_prec:2:low", "lawgo_prec:3:low"]


def build_manual(corpus, ok_case=False):
    path = build(corpus)
    m = Manifest(path)
    for t in MANUAL:
        m.set_case_verification(t, "quarantined")
        m.record_failure(t, "manual_exclusion", "judgment_quote_mismatch", "참고 기준 제외", retry_count=0)
    if ok_case:      # 사전조건 위반: 인용 일치 통과 사건을 수동 목록에 넣는다
        m.set_case_verification("lawgo_prec:1:ok", "quarantined")
        m.record_failure("lawgo_prec:1:ok", "manual_exclusion", "judgment_quote_mismatch", "x", retry_count=0)
    m.close()
    return path


def state(path):
    m = Manifest(path, in_memory_copy=True)
    cs = {r["case_id"]: r["verification_status"] for r in m.select("SELECT case_id, verification_status FROM cases")}
    fl = [(f["target"], f["stage"], f["error_class"], f["resolved_at"] is None) for f in m.failures(open_only=False)]
    m.close()
    return cs, sorted(fl)


def test_migrate_dry_run_then_apply_idempotent(tmp_path):
    path = build_manual(tmp_path / "c")
    before = path.read_bytes()
    rep = run(tmp_path / "c", "--source", PREC, "--migrate-manual")
    assert path.read_bytes() == before and rep["migrated"] == 2 and rep["applied"] is False
    run(tmp_path / "c", "--source", PREC, "--migrate-manual", "--apply", "--dry-run", "--undo-out", str(tmp_path / "u0.sql"))
    assert path.read_bytes() == before and not (tmp_path / "u0.sql").exists()
    undo = tmp_path / "u.sql"
    rep = run(tmp_path / "c", "--source", PREC, "--migrate-manual", "--apply", "--undo-out", str(undo))
    assert rep["migrated"] == 2 and undo.exists()
    cs, fl = state(path)
    assert all(cs[t] == "unverified" for t in MANUAL)
    for t in MANUAL:
        assert (t, "manual_exclusion", "judgment_quote_mismatch", False) in fl            # 행은 보존, resolved_at 만 기록
        assert (t, "admission", "case_excluded", True) in fl
    m = Manifest(path, in_memory_copy=True)
    assert all("migrated from manual_exclusion 2026-10-17" in f["message"] for f in m.failures() if f["error_class"] == "case_excluded")
    m.close()
    snap = path.read_bytes()
    assert run(tmp_path / "c", "--source", PREC, "--migrate-manual", "--apply")["migrated"] == 0       # 멱등
    assert state(path) == (cs, fl)
    del snap


def test_migrate_precondition_violation_rolls_back_all(tmp_path):
    path = build_manual(tmp_path / "c", ok_case=True)
    s0 = state(path)
    rep = run(tmp_path / "c", "--source", PREC, "--migrate-manual", "--apply", expect=1)
    assert rep["error"] == "MigrationError" and state(path) == s0


def test_migrate_undo_sql_restores(tmp_path):
    import sqlite3
    path = build_manual(tmp_path / "c")
    s0 = state(path)
    undo = tmp_path / "u.sql"
    run(tmp_path / "c", "--source", PREC, "--migrate-manual", "--apply", "--undo-out", str(undo))
    assert state(path) != s0
    con = sqlite3.connect(str(path), isolation_level=None)
    con.executescript(undo.read_text(encoding="utf-8"))
    con.close()
    assert state(path) == s0
    with pytest.raises(FileExistsError):
        admission.migrate_manual(Manifest(path), PREC, undo_out=undo)       # 기존 파일 덮어쓰기 거부

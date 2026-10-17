import json

import pytest

from press_review import contracts
from press_review.ingestion import coverage, rights as R
from press_review.ingestion.__main__ import main


def seed(m, clock):
    m.ensure_source("pac_casebooks", "pac_cases", "사례집", "https://www.pac.or.kr/kor/pages/?p=207")
    m.ensure_source("iendc_x", "iendc", "IENDC", "https://www.iendc.go.kr/board.do?menuPos=45")
    m.record_robots("www.pac.or.kr", "2026-10-08T00:00:00+00:00", 404, None, "no_rules")
    m.record_robots("www.iendc.go.kr", "2026-10-08T00:00:00+00:00", 200, "a" * 64, "allowed")
    m.upsert_list_page("pac_casebooks", 1, "https://www.pac.or.kr/kor/pages/?p=207&page=1", http_status=200, item_count=2,
                       body_sha256="b" * 64, ok=True)
    m.upsert_document("pac_cases:1", "pac_casebooks", canonical_url="https://www.pac.or.kr/kor/pages/?p=207&bn=1",
                      title="가", year=2020, doc_type="casebook")
    m.upsert_document("pac_cases:2", "pac_casebooks", title="나", year=None, detail_url="https://www.pac.or.kr/d/2")
    m.upsert_document("iendc:9", "iendc_x", title="다", year=2021, publication_date="2021-01-02")
    m.upsert_attachment("pac_cases:1#1", "pac_cases:1", "https://www.pac.or.kr/f/1.pdf", content_length=100, content_type="application/pdf",
                        head_checked_at="2026-10-08T00:00:00+00:00")
    m.record_failure("iendc:9", "attachment", "post_only_non_pdf", "x")


def test_documents_inherit_source_rights(manifest, clock):
    seed(manifest, clock)
    e = R.effective_rights(manifest, "pac_cases:1")
    assert e["inherited"] and e["rights"]["redistribute_raw"] == "permission_required" and e["rights"]["external_api"] == "allowed"
    assert e["review_status"] == "pending" and e["rights_basis"]


def test_document_override_wins_and_is_guarded(manifest, clock):
    seed(manifest, clock)
    strict = {k: "permission_required" for k in R.RIGHT_KEYS}
    R.set_document_override(manifest, "pac_cases:2", strict, [])
    e = R.effective_rights(manifest, "pac_cases:2")
    assert not e["inherited"] and e["rights"]["read"] == "permission_required"
    assert R.effective_rights(manifest, "pac_cases:1")["inherited"]
    with pytest.raises(ValueError):  # allowed without basis
        R.set_document_override(manifest, "pac_cases:1", {**strict, "read": "allowed"}, [])
    with pytest.raises(KeyError):
        R.set_document_override(manifest, "pac_cases:404", strict, [])
    assert R.effective_rights(manifest, "pac_cases:1")["inherited"]


def test_override_cannot_loosen_forbidden():
    src = {k: "forbidden" for k in R.RIGHT_KEYS}
    with pytest.raises(ValueError):
        R.validate_override(src, {**src, "read": "unknown"}, [])


def test_source_records_validate_against_contract(manifest, clock):
    seed(manifest, clock)
    for d in ("pac_cases:1", "pac_cases:2", "iendc:9"):
        rec = R.build_source_record(manifest, d)
        assert contracts.validate_source_record(rec) == []
    r = R.build_source_record(manifest, "pac_cases:1")
    assert r["robots"]["purpose_status"] == "allowed" and "no_rules" in r["robots"]["notes"] and r["collection_enabled"] is True
    assert R.build_source_record(manifest, "pac_cases:2")["canonical_url"].endswith("/d/2")  # canonical 없으면 detail_url


def test_unknown_robots_never_enables_collection(manifest, clock):
    manifest.ensure_source("s", "pac_cases", "t", "https://www.pac.or.kr/")
    manifest.upsert_document("pac_cases:7", "s", title="x")
    rec = R.build_source_record(manifest, "pac_cases:7")  # robots 행 없음
    assert rec["robots"]["purpose_status"] == "unknown" and rec["collection_enabled"] is False
    assert contracts.validate_source_record(rec) == []


def test_ledger_export_counts_and_not_collected(manifest, clock):
    seed(manifest, clock)
    R.set_document_override(manifest, "pac_cases:2", {k: "permission_required" for k in R.RIGHT_KEYS}, [])
    led = R.export_ledger(manifest, "2026-10-09T00:00:00+00:00", validate=contracts.validate_source_record)
    by = {s["source_id"]: s for s in led["sources"]}
    assert by["pac_casebooks"]["documents"] == 2 and by["pac_casebooks"]["documents_inheriting"] == 1
    assert by["pac_casebooks"]["document_overrides"] == ["pac_cases:2"]
    assert by["pac_casebooks"]["record_validation"]["invalid"] == 0
    nc = {r["source_family"]: r for r in led["not_collected"]}
    assert set(nc) == {"naver", "daum", "research"}
    assert all(v == "forbidden" for v in nc["naver"]["rights"].values())
    assert all(v == "unknown" for v in nc["daum"]["rights"].values())


def test_coverage_numbers_come_from_the_manifest(manifest, clock):
    seed(manifest, clock)
    cov = coverage.build_coverage(manifest, "2026-10-09T00:00:00+00:00")
    s = {x["source_id"]: x for x in cov["sources"]}["pac_casebooks"]
    assert s["documents"]["count"] == 2 and s["list_pages"]["count"] == 1 and s["list_pages"]["item_count_sum"] == 2
    assert s["attachments"]["count"] == 1 and s["attachments"]["total_bytes"] == 100
    assert s["years"] == {"2020": 1, "미상": 1} and s["screen_total_check"]["item_sum_equals_documents"] is True
    i = {x["source_id"]: x for x in cov["sources"]}["iendc_x"]
    assert i["failures_open"] == {"post_only_non_pdf": 1}
    md = coverage.render_markdown(cov)
    assert "수집 안 함" in md and "naver" not in md.lower().split("## 수집하지 않음")[0]


def test_export_coverage_cli_writes_files_and_leaves_manifest_unchanged(corpus, manifest, clock, tmp_path):
    seed(manifest, clock)
    before = manifest.counts()
    out = tmp_path / "cov"
    import io
    buf = io.StringIO()
    assert main(["export-coverage", "--out-dir", str(out)], stdout=buf, corpus=corpus) == 0
    j = json.loads(buf.getvalue())
    assert j["records_checked"] == 3 and j["records_invalid"] == 0
    assert (out / "P1_inventory.md").exists() and json.loads((out / "rights_ledger.json").read_text(encoding="utf-8"))["sources"]
    assert manifest.counts() == before
    with pytest.raises(SystemExit):
        main(["export-coverage"], stdout=io.StringIO(), corpus=corpus)

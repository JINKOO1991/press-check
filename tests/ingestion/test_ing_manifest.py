import json
import sqlite3

import pytest

from press_review.ingestion.manifest import DOC_STATUSES, Manifest


def mk_src(m, sid="pac_casebooks", fam="pac_cases"):
    m.ensure_source(sid, fam, "사례집", "https://www.pac.or.kr/kor/pages/?p=207")


def test_rediscovery_changes_only_last_seen_at(manifest, clock):
    mk_src(manifest)
    assert manifest.upsert_document("pac_cases:1212", "pac_casebooks", title="제목", year=2020, doc_type="casebook",
                                    extra={"k": 1}) == "new"
    before = manifest.get_document("pac_cases:1212")
    clock.advance(3600)
    assert manifest.upsert_document("pac_cases:1212", "pac_casebooks", title="다른 제목", year=1999) == "seen"
    after = manifest.get_document("pac_cases:1212")
    assert after["last_seen_at"] != before["last_seen_at"] and after["discovered_at"] == before["discovered_at"]
    for k in before:
        if k != "last_seen_at":
            assert after[k] == before[k], k
    assert manifest.counts()["documents"] == 1


def test_documents_missing_from_later_listing_are_preserved(manifest, clock):
    mk_src(manifest)
    for i in (1, 2, 3):
        manifest.upsert_document(f"pac_cases:{i}", "pac_casebooks", title=f"t{i}")
    clock.advance(86400)
    cutoff = clock.now().isoformat(timespec="seconds")
    for i in (1, 2):                                                     # 3은 두 번째 열거에서 사라짐
        manifest.upsert_document(f"pac_cases:{i}", "pac_casebooks")
    assert manifest.counts()["documents"] == 3
    assert manifest.documents_not_seen_since("pac_casebooks", cutoff.replace("+00:00", "+00:00")) == ["pac_cases:3"]
    assert not hasattr(manifest, "delete_document")


def test_transaction_rollback_leaves_no_partial_rows(manifest):
    mk_src(manifest)
    with pytest.raises(RuntimeError):
        with manifest.transaction():
            manifest.upsert_document("pac_cases:9", "pac_casebooks")
            manifest.upsert_attachment("pac_cases:9#1", "pac_cases:9", "https://www.pac.or.kr/f")
            manifest.record_failure("t", "s", "e", "m")
            raise RuntimeError("boom")
    assert manifest.get_document("pac_cases:9") is None
    assert manifest.counts()["attachments"] == 0 and manifest.failures() == []


def test_nested_transaction_inner_rollback_keeps_outer(manifest):
    mk_src(manifest)
    with manifest.transaction():
        manifest.upsert_document("pac_cases:1", "pac_casebooks")
        with pytest.raises(ValueError):
            with manifest.transaction():
                manifest.upsert_document("pac_cases:2", "pac_casebooks")
                raise ValueError
        manifest.upsert_document("pac_cases:3", "pac_casebooks")
    assert {d for d in ("pac_cases:1", "pac_cases:2", "pac_cases:3") if manifest.get_document(d)} == {"pac_cases:1", "pac_cases:3"}


def test_committed_data_survives_reopen(corpus, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    mk_src(m)
    m.upsert_document("pac_cases:1", "pac_casebooks", title="a")
    m.close()
    m2 = Manifest(corpus / "manifest.sqlite", now=clock.now)
    assert m2.get_document("pac_cases:1")["title"] == "a"
    mk_src(m2)                                                           # 재등록은 멱등
    assert m2.counts()["sources"] == 1
    m2.close()


def test_failures_one_row_per_target_stage_and_retry_count_increments(manifest, clock):
    manifest.record_failure("https://x/1", "http", "retries_exhausted", "boom")
    clock.advance(10)
    manifest.record_failure("https://x/1", "http", "retries_exhausted", "boom again")
    manifest.record_failure("https://x/1", "parse", "no_marker", "m")
    f = manifest.failures()
    assert len(f) == 2
    http = next(x for x in f if x["stage"] == "http")
    assert http["retry_count"] == 1 and http["message"] == "boom again"
    assert manifest.resolve_failures("https://x/1", "http") == 1
    assert len(manifest.failures()) == 1 and len(manifest.failures(open_only=False)) == 2
    manifest.record_failure("https://x/1", "http", "again", "m")        # 재발하면 다시 열린다
    assert len(manifest.failures()) == 2


def test_list_page_upsert_is_idempotent_and_keyed_by_url(manifest):
    mk_src(manifest)
    for _ in range(3):
        manifest.upsert_list_page("pac_casebooks", 1, "https://www.pac.or.kr/l?p=1", http_status=200, item_count=10,
                                  body_sha256="ab" * 32, ok=True)
    manifest.upsert_list_page("pac_casebooks", 1, "https://www.pac.or.kr/l?p=1&q=2", http_status=200, item_count=3,
                              body_sha256=None, ok=False)
    pages = manifest.list_pages("pac_casebooks")
    assert len(pages) == 2 and pages[0]["ok"] == 1


def test_attachment_idempotent_and_head_update(manifest):
    mk_src(manifest)
    manifest.upsert_document("pac_cases:1", "pac_casebooks")
    assert manifest.upsert_attachment("pac_cases:1#1", "pac_cases:1", "https://www.pac.or.kr/f1", label="전체", kind="full") == "new"
    assert manifest.upsert_attachment("pac_cases:1#1", "pac_cases:1", "https://www.pac.or.kr/f1", label=None, kind="full") == "seen"
    manifest.update_attachment_head("pac_cases:1#1", content_length=2136847, content_type="application/octet-stream")
    a = manifest.attachments_for("pac_cases:1")
    assert len(a) == 1 and a[0]["content_length"] == 2136847 and a[0]["label"] == "전체" and a[0]["head_checked_at"]
    with pytest.raises(ValueError):
        manifest.upsert_attachment("bad", "pac_cases:1", "u")
    with pytest.raises(ValueError):
        manifest.upsert_attachment("pac_cases:1#2", "pac_cases:1", "u", kind="weird")


def test_ids_and_status_validation(manifest):
    mk_src(manifest)
    with pytest.raises(ValueError):
        manifest.upsert_document("no-family-prefix", "pac_casebooks")
    manifest.upsert_document("iendc:3104", "pac_casebooks")
    for s in DOC_STATUSES:
        manifest.set_document_status("iendc:3104", s)
    with pytest.raises(ValueError):
        manifest.set_document_status("iendc:3104", "done")
    with pytest.raises(sqlite3.IntegrityError):
        manifest._con.execute("UPDATE documents SET status='x'")


def test_update_document_fields_enrich_and_extra_merge(manifest):
    mk_src(manifest)
    manifest.upsert_document("law_go_kr:194652", "pac_casebooks", extra={"relevance": "candidate", "terms": ["명예훼손"]})
    assert manifest.update_document_fields("law_go_kr:194652", title="사건명", decision_date="2020-01-02", year=2020,
                                           extra_merge={"terms": ["정정보도"], "n": 1}) is True
    d = manifest.get_document("law_go_kr:194652")
    assert d["title"] == "사건명" and json.loads(d["extra_json"]) == {"relevance": "candidate", "terms": ["명예훼손", "정정보도"], "n": 1}
    assert manifest.update_document_fields("law_go_kr:194652", title="사건명") is False
    with pytest.raises(ValueError):
        manifest.update_document_fields("law_go_kr:194652", last_seen_at="x")


@pytest.mark.parametrize("fam", ["pac_cases", "pac_election", "pac_other", "iendc", "law_go_kr"])
def test_new_install_default_rights_are_all_unknown(manifest, fam, real_default_rights):
    manifest.ensure_source(f"s_{fam}", fam, "t", "https://www.pac.or.kr/")
    r = manifest.get_rights("source", f"s_{fam}")
    assert all(r[k] == "unknown" for k in ("read", "store_raw", "create_derivatives", "external_api",
                                           "redistribute_raw", "redistribute_derivatives", "fine_tune"))
    assert r["review_status"] == "pending" and r["rights_basis"] == []


def test_naver_forbidden_and_daum_unknown_never_allowed(manifest):
    manifest.ensure_source("naver_c", "naver", "n", "https://news.naver.com/", enabled=False)
    manifest.ensure_source("daum_c", "daum", "d", "https://news.daum.net/correct")
    manifest.ensure_source("kci_x", "research", "k", "https://www.kci.go.kr/")
    keys = ("read", "store_raw", "create_derivatives", "external_api", "redistribute_raw", "redistribute_derivatives", "fine_tune")
    assert all(manifest.get_rights("source", "naver_c")[k] == "forbidden" for k in keys)
    for s in ("daum_c", "kci_x"):
        assert all(manifest.get_rights("source", s)[k] == "unknown" for k in keys)
    assert manifest.get_source("naver_c")["enabled"] == 0


def test_ensure_source_does_not_overwrite_a_reviewed_rights_row(manifest):
    mk_src(manifest)
    r = manifest.get_rights("source", "pac_casebooks")
    new = {k: r[k] for k in ("read", "store_raw", "create_derivatives", "external_api", "redistribute_raw",
                             "redistribute_derivatives", "fine_tune")}
    new["redistribute_raw"] = "allowed"
    manifest.set_rights("source", "pac_casebooks", new, [], "verified")
    mk_src(manifest)
    assert manifest.get_rights("source", "pac_casebooks")["redistribute_raw"] == "allowed"
    with pytest.raises(ValueError):
        manifest.set_rights("source", "pac_casebooks", {**new, "read": "maybe"}, [])


def test_robots_and_api_usage_tables(manifest):
    manifest.record_robots("www.pac.or.kr", "2026-10-08T00:00:00+00:00", 404, None, "no_rules")
    manifest.record_robots("www.pac.or.kr", "2026-10-09T00:00:00+00:00", 200, "ab" * 32, "allowed")
    assert manifest.latest_robots("www.pac.or.kr")["fetched_at"].startswith("2026-10-09")
    manifest.record_api_usage("deepseek", "deepseek-v4-flash", price_snapshot={"in": 1}, prompt_tokens=5, cost_usd=0.001, job_id="j")
    assert manifest._q("SELECT COUNT(*) FROM api_usage")[0][0] == 1


def test_dry_run_copy_never_writes_the_file(corpus, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    mk_src(m)
    m.upsert_document("pac_cases:1", "pac_casebooks")
    m.close()
    before = (corpus / "manifest.sqlite").read_bytes()
    d = Manifest(corpus / "manifest.sqlite", now=clock.now, in_memory_copy=True)
    assert d.get_document("pac_cases:1") is not None
    d.upsert_document("pac_cases:2", "pac_casebooks")
    d.close()
    assert (corpus / "manifest.sqlite").read_bytes() == before
    m2 = Manifest(corpus / "manifest.sqlite", now=clock.now)
    assert m2.get_document("pac_cases:2") is None
    m2.close()


def test_dry_run_with_no_existing_file_creates_nothing(corpus):
    d = Manifest(corpus / "manifest.sqlite", in_memory_copy=True)
    d.ensure_source("s", "pac_cases", "t", "u")
    d.close()
    assert not (corpus / "manifest.sqlite").exists()


def test_inventory_counts_come_from_the_ledger(manifest):
    mk_src(manifest)
    manifest.upsert_list_page("pac_casebooks", 1, "https://www.pac.or.kr/l?p=1", http_status=200, item_count=2, body_sha256="a", ok=True)
    manifest.upsert_document("pac_cases:1", "pac_casebooks", year=2020, doc_type="casebook")
    manifest.upsert_document("pac_cases:2", "pac_casebooks", year=2021, doc_type="casebook")
    manifest.set_document_status("pac_cases:2", "withheld")
    manifest.upsert_attachment("pac_cases:1#1", "pac_cases:1", "https://www.pac.or.kr/f", kind="full")
    manifest.update_attachment_head("pac_cases:1#1", content_length=1000, content_type="application/pdf")
    manifest.record_failure("pac_cases:2", "head", "http", "x")
    inv = manifest.inventory("pac_cases")
    assert inv["list_pages"] == {"count": 1, "ok": 1, "item_count_sum": 2}
    assert inv["documents"]["count"] == 2 and inv["documents"]["by_year"] == {"2020": 1, "2021": 1}
    assert inv["attachments"] == {"count": 1, "total_bytes_known": 1000, "head_checked": 1}
    assert inv["failures_open"] == 1 and inv["withheld"] == 1
    assert manifest.inventory("iendc")["documents"]["count"] == 0

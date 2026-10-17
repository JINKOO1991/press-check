"""P2 장부: 마이그레이션(기존 행 보존), 공개 질의 헬퍼, P2 테이블, extra_merge, 데이터 경로."""
import json
import sqlite3

import pytest

import pdr_ing_paths  # noqa: F401
from press_review import config
from press_review.ingestion import rights as rights_mod
from press_review.ingestion.manifest import SCHEMA_VERSION, Manifest

LEGACY_DDL = """
CREATE TABLE IF NOT EXISTS sources(
  source_id TEXT PRIMARY KEY, source_family TEXT NOT NULL, title TEXT, entry_url TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, checked_at TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS list_pages(
  source_id TEXT NOT NULL, page_no INTEGER NOT NULL, url TEXT NOT NULL, fetched_at TEXT, http_status INTEGER,
  item_count INTEGER, body_sha256 TEXT, ok INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(source_id, url));
CREATE TABLE IF NOT EXISTS documents(
  doc_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, canonical_url TEXT, title TEXT, publication_date TEXT,
  decision_date TEXT, year INTEGER, doc_type TEXT, detail_url TEXT, discovered_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'discovered', extra_json TEXT,
  CHECK(status IN ('discovered','fetched','extracted','structured','verified','indexed','failed','withheld')));
CREATE INDEX IF NOT EXISTS documents_source ON documents(source_id);
CREATE TABLE IF NOT EXISTS attachments(
  att_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, url TEXT NOT NULL, label TEXT,
  kind TEXT NOT NULL DEFAULT 'other' CHECK(kind IN ('full','chapter','table','other')),
  content_length INTEGER, content_type TEXT, head_checked_at TEXT, raw_sha256 TEXT, raw_path TEXT,
  status TEXT NOT NULL DEFAULT 'discovered');
CREATE INDEX IF NOT EXISTS attachments_doc ON attachments(doc_id);
CREATE TABLE IF NOT EXISTS rights(
  scope_kind TEXT NOT NULL CHECK(scope_kind IN ('source','document')), scope_id TEXT NOT NULL,
  read TEXT NOT NULL, store_raw TEXT NOT NULL, create_derivatives TEXT NOT NULL, external_api TEXT NOT NULL,
  redistribute_raw TEXT NOT NULL, redistribute_derivatives TEXT NOT NULL, fine_tune TEXT NOT NULL,
  rights_basis_json TEXT NOT NULL, review_status TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(scope_kind, scope_id));
CREATE TABLE IF NOT EXISTS failures(
  id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, stage TEXT NOT NULL, error_class TEXT NOT NULL,
  message TEXT, at TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0, resolved_at TEXT,
  UNIQUE(target, stage));
CREATE TABLE IF NOT EXISTS robots(
  host TEXT NOT NULL, fetched_at TEXT NOT NULL, http_status INTEGER, body_sha256 TEXT, purpose_status TEXT NOT NULL,
  PRIMARY KEY(host, fetched_at));
CREATE TABLE IF NOT EXISTS api_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, model TEXT NOT NULL, price_snapshot_json TEXT,
  prompt_tokens INTEGER, cache_hit_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL, at TEXT NOT NULL, job_id TEXT);
"""


def make_legacy(path, n_docs=50):
    con = sqlite3.connect(path)
    con.executescript(LEGACY_DDL)
    con.execute("INSERT INTO sources VALUES('pac_adj','pac_cases','사례집','https://www.pac.or.kr/x',1,NULL,NULL)")
    con.execute("INSERT INTO sources VALUES('lawgo','law_go_kr','판례','https://www.law.go.kr/x',1,NULL,NULL)")
    for i in range(n_docs):
        extra = json.dumps({"list_page": 1 + i % 2, "att_files": {"a": [1]}}) if i < 10 else None
        con.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"pac_cases:{i}", "pac_adj", f"https://www.pac.or.kr/{i}", f"제목 {i}", None, None, 2000 + i % 20, "report",
                     None, "2026-10-08T00:00:00+00:00", "2026-10-08T00:00:00+00:00", "discovered", extra))
    con.execute("INSERT INTO documents VALUES('law:1','lawgo',NULL,'판례',NULL,'2020-01-01',2020,NULL,NULL,'t','t','failed',NULL)")
    for i in range(5):
        con.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (f"pac_cases:{i}#1", f"pac_cases:{i}", f"https://www.pac.or.kr/f{i}.pdf", "전체", "full", 100 + i,
                     "application/pdf", None, None, None, "discovered"))
    for p in (1, 2):
        con.execute("INSERT INTO list_pages VALUES('pac_adj',?,?,?,200,5,'h',1)", (p, f"https://www.pac.or.kr/list?p={p}", "t"))
    con.execute("INSERT INTO failures(target,stage,error_class,message,at) VALUES('pac_cases:3','head','x','m','t')")
    con.execute("INSERT INTO api_usage(provider,model,at) VALUES('deepseek','deepseek-v4-flash','t')")
    con.commit()
    con.close()


def counts_of(path):
    con = sqlite3.connect(path)
    out = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
           ("sources", "list_pages", "documents", "attachments", "rights", "failures", "robots", "api_usage")}
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()
    return out, ver


def test_migration_preserves_rows_and_is_idempotent(tmp_path):
    db = tmp_path / "m.sqlite"
    make_legacy(db)
    before, ver0 = counts_of(db)
    assert ver0 == 0
    con = sqlite3.connect(db)
    old_docs = con.execute("SELECT * FROM documents ORDER BY doc_id").fetchall()
    old_atts = con.execute("SELECT * FROM attachments ORDER BY att_id").fetchall()
    con.close()
    m = Manifest(db)
    assert m.migration["migrated"] and m.migration["to"] == SCHEMA_VERSION
    assert m.migration["before"] == m.migration["after"]
    m.close()
    after, ver1 = counts_of(db)
    assert after == before and ver1 == SCHEMA_VERSION
    con = sqlite3.connect(db)
    ncol = len(old_docs[0])
    assert [r[:ncol] for r in con.execute("SELECT * FROM documents ORDER BY doc_id")] == old_docs      # 원래 열 값 그대로
    assert [r[:len(old_atts[0])] for r in con.execute("SELECT * FROM attachments ORDER BY att_id")] == old_atts
    con.close()
    m = Manifest(db)                                                       # 두 번째 열기는 아무것도 하지 않는다
    assert not m.migration["migrated"]
    m.set_document_status("pac_cases:0", "quarantined")                   # CHECK 에 quarantined 가 들어갔다
    with pytest.raises(sqlite3.IntegrityError):
        m._con.execute("UPDATE documents SET status='x'")
    m.close()
    assert counts_of(db)[0] == before


def test_migration_backfills_list_page_links_only_where_unique(tmp_path):
    db = tmp_path / "m.sqlite"
    make_legacy(db)
    m = Manifest(db)
    rows = m.select("SELECT doc_id, list_url FROM document_list_pages ORDER BY doc_id")
    assert len(rows) == 10 and rows[0] == {"doc_id": "pac_cases:0", "list_url": "https://www.pac.or.kr/list?p=1"}
    assert rows[1]["list_url"].endswith("p=2")
    m.close()


def test_migration_rolls_back_on_failure(tmp_path, monkeypatch):
    from press_review.ingestion import manifest as mod
    db = tmp_path / "m.sqlite"
    make_legacy(db)
    monkeypatch.setattr(mod, "_backfill_document_list_pages", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        Manifest(db)
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 51
    assert "extractor_version" not in [r[1] for r in con.execute("PRAGMA table_info(documents)")]
    con.close()


def test_dry_run_copy_migrates_in_memory_only(tmp_path):
    db = tmp_path / "m.sqlite"
    make_legacy(db)
    m = Manifest(db, in_memory_copy=True)
    assert m.counts()["documents"] == 51 and m.migration["migrated"]
    m.close()
    assert counts_of(db)[1] == 0


def test_fresh_manifest_has_p2_tables(manifest):
    names = {r["name"] for r in manifest.select("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"pages", "cases", "chunks", "structure_jobs", "dead_letter", "document_list_pages", "api_usage"} <= names
    assert "cache_miss_tokens" in manifest.table_columns("api_usage")
    assert manifest.p2_counts()["pages"] == 0


def test_extra_merge_keeps_nested_keys_and_unions_lists(manifest):
    manifest.ensure_source("s", "pac_cases")
    manifest.upsert_document("pac_cases:1", "s", extra={"att_files": {"a": [1], "b": [2]}, "tags": ["x"], "n": 1})
    manifest.update_document_fields("pac_cases:1", extra_merge={"att_files": {"c": [3], "a": [1, 9]}, "tags": ["y"], "n": 2})
    ex = json.loads(manifest.get_document("pac_cases:1")["extra_json"])
    assert ex == {"att_files": {"a": [1, 9], "b": [2], "c": [3]}, "tags": ["x", "y"], "n": 2}
    manifest.update_document_fields("pac_cases:1", extra_merge={"items": [{"k": 1}]})
    manifest.update_document_fields("pac_cases:1", extra_merge={"items": [{"k": 1}, {"k": 2}]})
    assert json.loads(manifest.get_document("pac_cases:1")["extra_json"])["items"] == [{"k": 1}, {"k": 2}]


def seed(m):
    m.ensure_source("s_cases", "pac_cases", "사례집")
    m.ensure_source("s_res", "pac_other", "연구")
    m.upsert_document("pac_cases:1", "s_cases", year=2001, extra={"case_no": "2001다1", "meta": {"k": "v"}})
    m.upsert_document("pac_cases:2", "s_cases", year=2002, extra={"case_no": "2002다2"})
    m.upsert_document("pac_other:3", "s_res", year=2003)
    m.upsert_attachment("pac_cases:1#1", "pac_cases:1", "https://www.pac.or.kr/a1.pdf", kind="full")
    m.upsert_attachment("pac_cases:1#2", "pac_cases:1", "https://www.pac.or.kr/a2.pdf", kind="chapter")
    m.upsert_attachment("pac_cases:2#1", "pac_cases:2", "https://www.pac.or.kr/b.pdf", kind="full")
    m.upsert_attachment("pac_other:3#1", "pac_other:3", "https://www.pac.or.kr/c.pdf", kind="full")


def test_query_helpers(manifest):
    seed(manifest)
    assert [d["doc_id"] for d in manifest.list_documents(family="pac_cases")] == ["pac_cases:1", "pac_cases:2"]
    assert [d["doc_id"] for d in manifest.list_documents(source_id="s_res")] == ["pac_other:3"]
    assert len(manifest.list_documents(year_min=2002, limit=1)) == 1
    assert {a["att_id"] for a in manifest.list_attachments(kind="chapter")} == {"pac_cases:1#2"}
    atts = manifest.list_attachments(status="discovered", right=("store_raw", "allowed"))
    assert len(atts) == 4 and atts[0]["source_id"] == "s_cases" and atts[0]["doc_status"] == "discovered"
    d = rights_mod.default_rights("pac_other")
    deny = dict(d["rights"], store_raw="forbidden")
    manifest.set_rights("document", "pac_other:3", deny, d["rights_basis"])           # 문서 행이 소스 행을 대체한다
    assert {a["doc_id"] for a in manifest.list_attachments(right=("store_raw", "allowed"))} == {"pac_cases:1", "pac_cases:2"}
    assert [a["att_id"] for a in manifest.list_attachments(right=("store_raw", "forbidden"))] == ["pac_other:3#1"]
    with pytest.raises(ValueError):
        manifest.list_attachments(right=("nope", "allowed"))
    with pytest.raises(ValueError):
        manifest.list_documents(order_by="doc_id; DROP TABLE documents")
    assert manifest.count_by("documents", "status") == [{"status": "discovered", "n": 3}]
    assert manifest.count_by("attachments", "kind", where={"status": "discovered"}) == [{"kind": "chapter", "n": 1}, {"kind": "full", "n": 3}]
    assert manifest.count_by("documents") == [{"n": 3}]
    with pytest.raises(ValueError):
        manifest.count_by("documents; x", "status")
    with pytest.raises(ValueError):
        manifest.count_by("documents", "nope")


def test_find_by_json_validates_field_and_binds_values(manifest):
    seed(manifest)
    assert [r["doc_id"] for r in manifest.find_by_json("documents", "extra_json", "case_no", "2001다1")] == ["pac_cases:1"]
    assert [r["doc_id"] for r in manifest.find_by_json("documents", "extra_json", "meta.k", "v")] == ["pac_cases:1"]
    assert len(manifest.find_by_json("documents", "extra_json", "case_no", op="exists", where={"source_id": "s_cases"})) == 2
    assert manifest.find_by_json("documents", "extra_json", "case_no", "x') OR 1=1 --") == []
    for bad in ("case_no') OR 1=1 --", "a b", "", "a..b", "$.x"):
        with pytest.raises(ValueError):
            manifest.find_by_json("documents", "extra_json", bad, "v")
    with pytest.raises(ValueError):
        manifest.find_by_json("documents", "title", "x", "v")
    with pytest.raises(ValueError):
        manifest.find_by_json("documents", "extra_json", "case_no", "v", op="; DROP")


@pytest.mark.parametrize("sql", [
    "DELETE FROM documents", "INSERT INTO sources(source_id, source_family) VALUES('x','y')", "PRAGMA user_version=9",
    "WITH t AS (SELECT 1) DELETE FROM documents", "ATTACH DATABASE ':memory:' AS x", "DROP TABLE documents",
])
def test_select_is_read_only(manifest, sql):
    seed(manifest)
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        manifest.select(sql)
    assert manifest.counts()["documents"] == 3
    assert manifest.select("SELECT COUNT(*) AS n FROM documents") == [{"n": 3}]          # 권한 콜백이 풀려 있다
    manifest.upsert_document("pac_cases:9", "s_cases")                                    # 쓰기 경로는 영향 없음


def test_pages_cases_chunks_and_lineage(manifest):
    seed(manifest)
    manifest.upsert_list_page("s_cases", 1, "https://www.pac.or.kr/list?p=1", http_status=200, item_count=2, body_sha256="h", ok=True)
    assert manifest.link_document_list_page("pac_cases:1", "s_cases", "https://www.pac.or.kr/list?p=1")
    assert not manifest.link_document_list_page("pac_cases:1", "s_cases", "https://www.pac.or.kr/list?p=1")
    manifest.upsert_page("pac_cases:1#1@p3", "pac_cases:1", att_id="pac_cases:1#1", pdf_page_no=3, printed_page_no="5",
                         method="pdftotext", char_count=100, quality=0.9, quality_json={"hangul": 0.8}, extractor_version="e1")
    manifest.upsert_page("pac_cases:2#1@p1", "pac_cases:2", att_id="pac_cases:2#1", pdf_page_no=1, method="ocr",
                         needs_review=True, ocr_confidence=0.7)
    assert [p["pdf_page_no"] for p in manifest.pages_of(att_id="pac_cases:1#1")] == [3]
    manifest.upsert_case("c1", "pac_cases:1", {"institution": "PAC", "case_number": "2001-1", "authority_type": "pac_settlement",
                                               "outcome_type": "settlement"}, att_id="pac_cases:1#1",
                         first_page_id="pac_cases:1#1@p3", page_first=3, page_last=4, quote_total=4, quote_ok=3)
    assert manifest.get_case("c1")["record"]["institution"] == "PAC" and manifest.get_case("c1")["authority_type"] == "pac_settlement"
    manifest.upsert_chunk("k1", "pac_cases:1", "본문", "pdf_page", "3", case_id="c1", authority_type="pac_settlement")
    manifest.upsert_chunk("k2", "pac_cases:2", "연구", "pdf_page", "1", page_id="pac_cases:2#1@p1", authority_type="research")
    lin = manifest.lineage_of_chunk("k1")
    assert (lin["case_id"], lin["page_id"], lin["att_id"], lin["doc_id"], lin["list_url"], lin["list_page_no"]) == \
        ("c1", "pac_cases:1#1@p3", "pac_cases:1#1", "pac_cases:1", "https://www.pac.or.kr/list?p=1", 1)
    lin2 = manifest.lineage_of_chunk("k2")                                  # case 없는 chunk, 목록 연결 없음
    assert lin2["case_id"] is None and lin2["doc_id"] == "pac_cases:2" and lin2["list_url"] is None
    assert manifest.lineage_of_chunk("nope") is None
    with pytest.raises(ValueError):
        manifest.upsert_chunk("k3", "pac_cases:1", "x", "pdf_page", "1")
    with pytest.raises(ValueError):
        manifest.upsert_chunk("k3", "pac_cases:1", "x", "weird", "1", case_id="c1")
    with pytest.raises(ValueError):
        manifest.upsert_case("c2", "pac_cases:1", {}, verification_status="maybe")
    assert manifest.delete_derived(att_id="pac_cases:1#1") == {"chunks": 1, "cases": 1, "pages": 1}
    assert manifest.counts()["documents"] == 3 and manifest.p2_counts()["chunks"] == 1      # 장부 행은 보존, 다른 첨부는 그대로


def test_jobs_dead_letter_and_usage(manifest):
    j = manifest.upsert_job("j1", "pac_cases:1#1", "h" * 8, "s1", unit="win:0", model="deepseek-v4-flash", est_cost_usd=0.01)
    assert j["status"] == "queued"
    again = manifest.upsert_job("j2", "pac_cases:1#1", "h" * 8, "s1", unit="win:0")          # 같은 작업 키: 기존 행
    assert again["job_id"] == "j1"
    manifest.update_job("j1", status="running", attempts=1)
    manifest.update_job("j1", status="done", cost_usd=0.002, error_message="OC=abc leaked")
    assert manifest.jobs(status="done")[0]["finished_at"] and "abc" not in manifest.jobs()[0]["error_message"]
    manifest.upsert_job("j3", "pac_cases:1#1", "h" * 8, "s1", unit="win:1")
    manifest.record_dead_letter("j3", "pac_cases:1#1", "truncated json", attempts=3, response_sha256="x")
    assert manifest.jobs(status="dead")[0]["job_id"] == "j3" and len(manifest.dead_letters()) == 1
    manifest.resolve_dead_letter(manifest.dead_letters()[0]["id"])
    assert manifest.dead_letters() == [] and len(manifest.dead_letters(open_only=False)) == 1
    with pytest.raises(ValueError):
        manifest.update_job("j1", status="weird")
    manifest.record_api_usage("deepseek", "deepseek-v4-flash", prompt_tokens=1000, cache_hit_tokens=400, completion_tokens=50,
                              cost_usd=0.001, job_id="j1", target_id="pac_cases:1#1", attempt=1, status="ok", latency_ms=900)
    manifest.record_api_usage("deepseek", "deepseek-v4-flash", prompt_tokens=10, completion_tokens=1, cost_usd=0.0001, status="error",
                              error_class="http_429")
    s = manifest.api_usage_summary()
    assert s["by_model"]["deepseek-v4-flash"] == {"calls": 2, "prompt_tokens": 1010, "cache_hit_tokens": 400, "cache_miss_tokens": 610,
                                                  "completion_tokens": 51, "cost_usd": pytest.approx(0.0011)}
    assert s["total"]["calls"] == 2


def test_attachment_and_document_stage_fields(manifest):
    seed(manifest)
    manifest.update_attachment_fields("pac_cases:1#1", raw_sha256="a" * 64, raw_path="raw/x.pdf", orig_filename="원본.pdf", page_count=14)
    a = manifest.attachments_for("pac_cases:1")[0]
    assert a["raw_sha256"] == "a" * 64 and a["orig_filename"] == "원본.pdf" and a["page_count"] == 14
    manifest.update_document_stage("pac_cases:1", extractor_version="e1", extract_input_sha256="b" * 64)
    assert manifest.get_document("pac_cases:1")["extractor_version"] == "e1"
    with pytest.raises(ValueError):
        manifest.update_attachment_fields("pac_cases:1#1", url="x")
    with pytest.raises(ValueError):
        manifest.update_document_stage("pac_cases:1", title="x")
    with pytest.raises(KeyError):
        manifest.update_attachment_fields("pac_cases:9#9", raw_path="x")
    manifest.set_attachment_status("pac_cases:1#1", "quarantined")
    with pytest.raises(ValueError):
        manifest.set_attachment_status("pac_cases:1#1", "done")


# ---- 데이터 경로(P2 §8) ----
def test_data_dir_is_one_function_and_checks_plugin_name(monkeypatch, tmp_path):
    from press_review.ingestion import context
    monkeypatch.delenv("PRESS_DISPUTE_DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    default = tmp_path / ".local/share/press-dispute-review"
    for foreign in (".claude/plugins/data/codex-openai-codex", "data/press-dispute"):
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / foreign))
        assert config.data_dir() == default == context.data_root(), foreign
    own = tmp_path / ".claude/plugins/data/press-dispute-review-market"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(own))
    assert config.data_dir() == own == context.data_root() and context.corpus_dir() == own / "corpus"
    monkeypatch.setenv("PRESS_DISPUTE_DATA_DIR", str(tmp_path / "x"))
    assert config.data_dir() == tmp_path / "x" == context.data_root()


def test_upsert_document_links_list_page_idempotently(manifest):
    manifest.ensure_source("s", "pac_cases")
    assert manifest.upsert_document("pac_cases:1", "s", list_url="https://www.pac.or.kr/l?p=1") == "new"
    assert manifest.upsert_document("pac_cases:1", "s", list_url="https://www.pac.or.kr/l?p=1") == "seen"
    assert manifest.upsert_document("pac_cases:1", "s", list_url="https://www.pac.or.kr/l?p=2") == "seen"
    assert manifest.p2_counts()["document_list_pages"] == 2
    assert manifest.upsert_document("pac_cases:2", "s") == "new" and manifest.p2_counts()["document_list_pages"] == 2


def test_upsert_job_never_returns_a_job_for_a_different_input(manifest):
    manifest.upsert_job("j1", "t", "a" * 8, "s1")
    manifest.update_job("j1", status="done")
    with pytest.raises(ValueError):                                         # 같은 job_id, 다른 입력 해시
        manifest.upsert_job("j1", "t", "b" * 8, "s1")
    fresh = manifest.upsert_job("j9", "t", "b" * 8, "s1")                  # 입력이 바뀌면 새 작업
    assert fresh["job_id"] == "j9" and fresh["status"] == "queued"
    assert manifest.upsert_job("jX", "t", "a" * 8, "s1")["status"] == "done"   # 같은 키: 기존(완료) 행
    assert manifest.upsert_job("j10", "t", "a" * 8, "s2")["status"] == "queued"  # 단계 버전이 바뀌면 새 작업


def test_lineage_prefers_a_resolvable_list_page_over_a_dangling_link(manifest):
    seed(manifest)
    manifest.upsert_list_page("s_cases", 1, "https://www.pac.or.kr/list?p=9", http_status=200, item_count=1, body_sha256="h", ok=True)
    manifest.link_document_list_page("pac_cases:1", "s_cases", "https://www.pac.or.kr/AAA-dangling")
    manifest.link_document_list_page("pac_cases:1", "s_cases", "https://www.pac.or.kr/list?p=9")
    manifest.upsert_page("pg", "pac_cases:1", att_id="pac_cases:1#1", pdf_page_no=1, method="pdftotext")
    manifest.upsert_chunk("k", "pac_cases:1", "t", "pdf_page", "1", page_id="pg")
    assert manifest.lineage_of_chunk("k")["list_url"] == "https://www.pac.or.kr/list?p=9"

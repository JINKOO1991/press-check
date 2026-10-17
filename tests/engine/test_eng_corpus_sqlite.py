"""P3 검색 색인: 빌드 재현성·제외 규칙·검색 순서·권리별 표시 상한·증거 검증·합성 코퍼스 폴백."""
import copy
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: E402

from press_review import config, corpus as corpus_mod, corpus_sqlite as cs, review  # noqa: E402
from press_review.ingestion.__main__ import main as ingest_main  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


@pytest.fixture()
def data(tmp_path):
    d = tmp_path / "data"
    sl.build_ledger(d / "corpus")
    return d


@pytest.fixture()
def built(data):
    res = cs.build_index(data / "corpus" / "manifest.sqlite", data / "index", now=NOW)
    assert res["status"] == "built"
    return data, res


@pytest.fixture()
def idx(built):
    data, _ = built
    c = cs.open_current(data / "index")
    yield c
    c.close()


def ids(hits):
    return [h["passage_id"] for h in hits]


# ------------------------------------------------------------------ 빌드

def test_build_counts_and_exclusions(built):
    _, res = built
    assert res["passages"] == 7
    assert res["excluded"] == {"case_quarantined": 1, "case_rejected": 1, "doc_quarantined": 1, "no_valid_url": 1}
    assert res["by_authority_type"] == {"iendc_review": 1, "judgment": 4, "pac_settlement": 1, "research": 1}
    assert res["by_verification_status"] == {"not_applicable": 1, "unverified": 5, "verified": 1}
    assert res["latest_decision_date"] == "2024-11-07"
    assert res["snapshot_id"].startswith("idx-") and len(res["snapshot_id"]) == 4 + 16


def test_no_ledger_write_and_no_stored_url_with_oc(built):
    data, _ = built
    c = sqlite3.connect(data / "index" / (Path(built[1]["path"]).name))
    urls = [r[0] for r in c.execute("SELECT url FROM passages")]
    assert urls and not any("oc=" in u.lower() for u in urls)
    assert "https://www.pac.or.kr/_common/new_download_file.asp?sub_idx=5001" in urls       # pdf_page 는 첨부 URL
    assert "https://www.law.go.kr/LSW/precInfoP.do?precSeq=900001" in urls                  # text_range 는 문서 정본 URL
    c.close()


def test_snapshot_is_reproducible_and_changes_with_content(tmp_path, data):
    a = cs.build_index(data / "corpus" / "manifest.sqlite", tmp_path / "ia", now=NOW)
    b = cs.build_index(data / "corpus" / "manifest.sqlite", tmp_path / "ib", now=NOW + dt.timedelta(days=3))
    assert a["snapshot_id"] == b["snapshot_id"]                                   # 구축 시각은 id 에 들어가지 않는다
    again = cs.build_index(data / "corpus" / "manifest.sqlite", tmp_path / "ia", now=NOW)
    assert again["status"] == "unchanged" and again["snapshot_id"] == a["snapshot_id"]
    qa, qb = cs.open_current(tmp_path / "ia"), cs.open_current(tmp_path / "ib")
    for q in ("정정보도", "2021다20003", "기자", "명예훼손 상당한 이유"):
        assert qa.search(q, limit=10) == qb.search(q, limit=10)                  # 파일 바이트가 아니라 결과가 같다
    # 텍스트가 바뀌면 id 가 바뀐다
    from press_review.ingestion.manifest import Manifest
    m = Manifest(data / "corpus" / "manifest.sqlite")
    m.upsert_chunk(sl.J3, "lawgo_prec:900003", "형법 제309조 출판물에 의한 명예훼손에서 비방할 목적을 따로 본다. 추가 문장.", "text_range",
                   "lawgo_prec:900003#s006:0-60", case_id="lawgo_prec:900003:2023도30003", authority_type="judgment", decision_date="2024-11-07")
    m.close()
    c = cs.build_index(data / "corpus" / "manifest.sqlite", tmp_path / "ic", now=NOW)
    assert c["snapshot_id"] != a["snapshot_id"]
    # 권리가 바뀌어도 id 가 바뀐다(텍스트만 해시하면 놓친다)
    m = Manifest(data / "corpus" / "manifest.sqlite")
    m.set_rights("document", "lawgo_prec:900003", sl._rights(redistribute_raw="allowed", redistribute_derivatives="allowed"),
                 [{"scope": "시험", "review_status": "verified", "url": "https://www.law.go.kr/"}], "verified")
    m.close()
    d = cs.build_index(data / "corpus" / "manifest.sqlite", tmp_path / "id", now=NOW)
    assert d["snapshot_id"] not in (a["snapshot_id"], c["snapshot_id"])
    for x in (qa, qb):
        x.close()


def test_time_budget_abort_publishes_nothing(data, tmp_path):
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("budget")

    out = tmp_path / "idx_abort"
    with pytest.raises(RuntimeError):
        cs.build_index(data / "corpus" / "manifest.sqlite", out, check=check)
    assert not out.exists() or list(out.iterdir()) == []


def test_dry_run_and_empty_ledger(data, tmp_path):
    r = cs.build_index(data / "corpus" / "manifest.sqlite", tmp_path / "dry", dry_run=True)
    assert r["status"] == "dry_run" and r["passages"] == 7 and not (tmp_path / "dry").exists()
    from press_review.ingestion.manifest import Manifest
    (tmp_path / "empty").mkdir()
    Manifest(tmp_path / "empty" / "manifest.sqlite").close()              # 스키마만 있고 chunk 가 없는 장부
    e = cs.build_index(tmp_path / "empty" / "manifest.sqlite", tmp_path / "eidx")
    assert e["status"] == "no_passages" and not (tmp_path / "eidx").exists()
    with pytest.raises(corpus_mod.CorpusError):
        cs.build_index(tmp_path / "missing.sqlite", tmp_path / "x")


def test_ledger_is_opened_read_only(data, tmp_path):
    p = data / "corpus" / "manifest.sqlite"
    before = p.read_bytes()
    cs.build_index(p, tmp_path / "ro", now=NOW)
    assert p.read_bytes() == before                                        # 장부 본 파일은 한 바이트도 바뀌지 않는다
    c = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
    assert c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 11
    c.close()


def test_ingestion_cli_build_index(data, capsys):
    import io
    out = io.StringIO()
    rc = ingest_main(["build_index"], corpus=data / "corpus", stdout=out)
    res = json.loads(out.getvalue())
    assert rc == 0 and res["status"] == "completed" and res["result"]["status"] == "built" and res["result"]["passages"] == 7
    assert (data / "index" / "CURRENT").is_file()
    out2 = io.StringIO()
    assert ingest_main(["build_index"], corpus=data / "corpus", stdout=out2) == 0
    assert json.loads(out2.getvalue())["result"]["status"] == "unchanged"
    out3 = io.StringIO()
    assert ingest_main(["build_index", "--dry-run"], corpus=data / "corpus", stdout=out3) == 0
    assert json.loads(out3.getvalue())["result"]["status"] == "dry_run"


# ------------------------------------------------------------------ 보조 함수

def test_case_number_parsing():
    assert cs.case_numbers("2023다240763, 240770") == ["2023다240763", "2023다240770"]
    assert cs.case_numbers("2008다27301,27318") == ["2008다27301", "2008다27318"]
    assert cs.case_numbers("2025서울조정432·433/434·435") == ["2025서울조정432", "2025서울조정433", "2025서울조정434", "2025서울조정435"]
    assert cs.case_numbers("2025서울조정1362‧1363‧1364") == ["2025서울조정1362", "2025서울조정1363", "2025서울조정1364"]
    assert cs.case_numbers("99다6203") == ["99다6203"]
    assert cs.case_numbers("2019다 12345") == ["2019다12345"]
    assert cs.case_numbers("2019다12345ㆍ2020다 6789") == ["2019다12345", "2020다6789"]
    assert cs.case_numbers(None) == [] and cs.case_numbers("사건번호 없음") == []
    assert [c for c, _ in cs.query_case_numbers("2022년12월 보도와 2022다251650 판결")] == ["2022다251650"]


def test_josa_strip_and_cites():
    assert cs.strip_josa("정정보도를") == "정정보도" and cs.strip_josa("기자가") == "기자" and cs.strip_josa("언론사에서는") == "언론사"
    assert cs.strip_josa("기자") == "기자" and cs.strip_josa("abc") == "abc"
    assert cs.cites_in("언론중재법 제14조와 민법 제750조, 제17조의2") == ["언론중재법 제14조", "제14조", "민법 제750조", "제750조", "제17조의2"]
    assert [c for c, _ in cs.query_cites("언론중재법 제14조 정정")] == ["언론중재법 제14조"]


# ------------------------------------------------------------------ 검색

def test_case_number_exact_search_comes_first(idx):
    hits = idx.search("명예훼손 정정보도 2021다20003")
    assert hits[0]["passage_id"] == sl.J2 and hits[0]["match"] == "case_number"          # 병합 사건번호의 두 번째 번호로도 찾는다
    assert idx.search("2018다10001")[0]["passage_id"] == sl.J1
    assert idx.search("2099다99999") == []                                                # 없는 번호는 0건


def test_citation_exact_search(idx):
    hits = idx.search("언론중재법 제14조")
    assert hits and hits[0]["passage_id"] == sl.J1 and hits[0]["match"] == "citation"


def test_citation_from_legal_provisions_field_is_indexed(idx):
    hits = idx.search("형법 제310조")                 # J3 본문에는 없고 사건 구조화의 legal_provisions 에만 있다
    assert hits and hits[0]["passage_id"] == sl.J3 and hits[0]["match"] == "citation"


def test_trigram_and_two_char_queries(idx):
    assert sl.P1 in ids(idx.search("정정보도"))
    assert idx.search("정정보도")[0]["match"] == "text"
    two = idx.search("초상")                                   # 2글자: trigram 은 아무것도 못 찾으므로 bigram 이 보완
    assert ids(two)[0] == sl.P1 and two[0]["match"] == "bigram"
    assert sl.R1 in ids(idx.search("추후"))
    assert idx.search("정정보도를 청구한다")[0]["passage_id"] in (sl.J1, sl.P1)             # 조사 변화에도 찾는다


def test_hostile_query_does_not_raise(idx):
    for q in ('- : ( * AND OR NOT "', '"""', "NEAR(a b)", "정정 -보도 (x) *", "a", "한", "\x00"):
        assert isinstance(idx.search(q), list)
    with pytest.raises(ValueError):
        idx.search("   ")


def test_filters(idx):
    q = "명예훼손"
    assert {h["authority_type"] for h in idx.search(q, authority_types=["judgment"], limit=10)} == {"judgment"}
    assert idx.search(q, authority_types=[]) == []
    assert ids(idx.search("명예훼손", institution="supreme_court", limit=10)) and \
        all(h["institution"] == "supreme_court" for h in idx.search("명예훼손", institution="supreme_court", limit=10))
    assert sl.P1 in ids(idx.search("정정보도", risk_channel="correction", limit=10))
    assert sl.J3 in ids(idx.search("명예훼손", risk_channel="criminal", limit=10))
    assert sl.J3 not in ids(idx.search("명예훼손", risk_channel="civil", limit=10))
    assert idx.search("정정보도", risk_channel="portrait", limit=10)[0]["passage_id"] == sl.P1
    with pytest.raises(ValueError):
        idx.search("a b", institution="nowhere")
    with pytest.raises(ValueError):
        idx.search("a b", stance="maybe")


def test_as_of_filters_dated_and_keeps_undated(idx):
    q = "명예훼손 정정보도 상당한 이유 초상"
    got = ids(idx.search(q, as_of="2019-12-31", limit=10))
    assert sl.J1 in got and sl.P1 in got                                       # 날짜 없는 조정 사례는 남는다
    assert sl.J2 not in got and sl.J3 not in got                               # 이후 결정은 제외
    assert sl.J2 in ids(idx.search(q, as_of="2022-05-26", limit=10))           # 경계일 포함


def test_counter_stance_reorders_but_requires_relevance(idx):
    q = "명예훼손 상당한 이유 공적 인물"
    counter = idx.search(q, stance="counter", limit=10)
    assert counter[0]["passage_id"] == sl.J2
    assert "outcome_type:dismissal_merits" in counter[0]["counter_signals"]
    both = idx.search(q, stance="both", limit=10)
    assert set(ids(both)) == set(ids(counter))                                  # 같은 후보를 재정렬할 뿐이다
    assert idx.search("바나나 축구장", stance="counter") == []                     # 질의와 무관한 기각 사건을 끌어오지 않는다
    # 결과가 청구 배척 표지가 없는 쪽으로 밀리는 것도 확인: 정정보도 질의에서 J2(표지 있음)가 J1보다 앞선다
    c2 = ids(idx.search("명예훼손 정정보도", stance="counter", limit=10))
    assert c2.index(sl.J2) < c2.index(sl.J1)


def test_display_cap_by_rights(idx):
    j1 = idx.get_passage("lawgo_prec:900001", sl.J1)
    assert len(j1["text"]) > cs.DISPLAY_QUOTE_MAX_CP == 300
    assert j1["redistributable"] is False and j1["display_truncated"] is True and len(j1["display_text"]) == 300
    assert j1["display_text"] == j1["text"][:300] and j1["display_limit_chars"] == 300
    j4 = idx.get_passage("lawgo_prec:900004", sl.J4)                           # 문서 단위 allowed 가 소스 단위 기본값을 덮는다
    assert j4["redistributable"] is True and j4["display_truncated"] is False and j4["display_text"] == j4["text"] and len(j4["text"]) > 300
    short = idx.get_passage("lawgo_prec:900002", sl.J2)
    assert short["display_truncated"] is False and short["display_text"] == short["text"]    # 권리 제한이어도 짧으면 자르지 않는다
    r = review.search_cases(idx, query="정정보도 요건", authority_types=["judgment"])
    assert r["ok"] and "display_text" in r["citation_rule"]


def test_opinion_kinds_exposed(idx):
    j2 = idx.get_passage("lawgo_prec:900002", sl.J2)
    assert j2["opinion_kinds"] == ["dissenting", "majority"] and j2["opinion_kind"] == "mixed" and j2["includes_dissent"] is True
    j1 = idx.get_passage("lawgo_prec:900001", sl.J1)
    assert j1["opinion_kinds"] == ["majority"] and j1["opinion_kind"] == "majority" and j1["includes_dissent"] is False
    assert idx.get_passage("pac_cases:5001", sl.P1)["opinion_kind"] is None


def test_get_passage_needs_matching_source_id(idx):
    assert idx.get_passage("lawgo_prec:900001", sl.J1) is not None
    assert idx.get_passage("lawgo_prec:900002", sl.J1) is None
    assert idx.get_passage("x", "y") is None and idx.get_passage(1, 2) is None
    p = idx.get_passage("lawgo_prec:900001", sl.J1)
    assert set(corpus_mod.PUBLIC_KEYS) <= set(p) and p["is_fixture"] is False


def test_status(idx, built):
    st = idx.status()
    assert st["is_fixture"] is False and st["snapshot_id"] == built[1]["snapshot_id"] and st["passage_count"] == 7
    assert st["as_of"] == "2026-10-09" and st["index_built_at"].startswith("2026-10-09") and st["latest_decision_date"] == "2024-11-07"
    assert st["search_mode"] == "trigram+bigram" and st["display_limit_chars"] == 300
    assert any("permission_required" in h for h in st["rights_hold"]) and any("색인에서 제외" in m for m in st["missing"])
    assert st["by_authority_type"]["judgment"] == 4
    assert any("미검수" in w for w in st["warnings"])


def test_bigram_only_fallback_without_fts(built, monkeypatch):
    data, _ = built
    monkeypatch.setattr(cs, "fts_trigram_available", lambda: False)
    c = cs.open_current(data / "index")
    assert c.status()["search_mode"] == "bigram_only" and any("bigram" in w for w in c.status()["warnings"])
    assert sl.P1 in ids(c.search("정정보도", limit=10))
    assert c.search("2021다20003")[0]["passage_id"] == sl.J2                    # 정확 검색은 FTS 와 무관
    c.close()


# ------------------------------------------------------------------ load_default · 보고서 연결

def test_load_default_falls_back_to_fixture_with_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("PRESS_DISPUTE_DATA_DIR", str(tmp_path / "nothing"))
    c = corpus_mod.load_default()
    assert c.is_fixture
    st = review.corpus_status(c)
    assert st["ok"] and any("실자료 미적재" in w for w in st["warnings"])


def test_fallback_warning_names_searched_paths(tmp_path, monkeypatch):
    data, home = tmp_path / "plugin-data", tmp_path / "home-default"
    monkeypatch.setenv("PRESS_DISPUTE_DATA_DIR", str(data))
    monkeypatch.setattr(config, "default_data_dir", lambda: home, raising=False)
    c = corpus_mod.load_default()
    w = " ".join(review.corpus_status(c)["warnings"])
    assert c.is_fixture and w.startswith("실자료 미적재: 검색 색인이 없어 합성 코퍼스를 씁니다")
    assert str(data) in w and str(data / "index") in w and "PRESS_DISPUTE_DATA_DIR=" not in w


def test_fallback_warning_points_to_default_location_index(tmp_path, monkeypatch):
    data, home = tmp_path / "plugin-data", tmp_path / "home-default"
    (home / "index").mkdir(parents=True)
    name = "corpus-idx-" + "0" * 16 + ".sqlite"
    (home / "index" / name).write_bytes(b"")          # 열어 보지 않으므로 존재만 필요
    (home / "index" / "CURRENT").write_text(name, encoding="utf-8")
    monkeypatch.setenv("PRESS_DISPUTE_DATA_DIR", str(data))
    monkeypatch.setattr(config, "default_data_dir", lambda: home, raising=False)
    c = corpus_mod.load_default()
    w = " ".join(review.corpus_status(c)["warnings"])
    assert c.is_fixture                                 # 동작은 그대로(다른 폴더를 채택하지 않는다)
    assert str(data / "index") in w and str(home) in w and f"PRESS_DISPUTE_DATA_DIR={home}" in w


def test_no_fallback_warning_with_valid_index(built, monkeypatch):
    data, _ = built
    monkeypatch.setenv("PRESS_DISPUTE_DATA_DIR", str(data))
    c = corpus_mod.load_default()
    assert not c.is_fixture and not any("실자료 미적재" in w for w in c.status()["warnings"])
    c.close()


def test_load_default_uses_index_and_survives_a_broken_pointer(built, monkeypatch):
    data, _ = built
    monkeypatch.setenv("PRESS_DISPUTE_DATA_DIR", str(data))
    c = corpus_mod.load_default()
    assert not c.is_fixture and isinstance(c, cs.SqliteCorpus)
    c.close()
    (data / "index" / "CURRENT").write_text("corpus-idx-0000000000000000.sqlite\n", encoding="utf-8")
    fx = corpus_mod.load_default()
    assert fx.is_fixture and any("열 수 없어" in w for w in fx.status()["warnings"])
    (data / "index" / "CURRENT").write_text("../../etc/passwd\n", encoding="utf-8")        # 포인터는 색인 파일명만 허용
    assert corpus_mod.load_default().is_fixture


def test_pointer_missing_uses_newest_index_file(built, monkeypatch):
    data, _ = built
    (data / "index" / "CURRENT").unlink()
    monkeypatch.setenv("PRESS_DISPUTE_DATA_DIR", str(data))
    c = corpus_mod.load_default()
    assert isinstance(c, cs.SqliteCorpus)
    c.close()


def make_article(pub="2026-10-05"):
    return {
        "schema_version": "0.1", "article_id": "t-live", "publication_date": pub,
        "blocks": [{"block_id": "title", "section": "title", "text": "공적 인물 비판 기사"},
                   {"block_id": "p1", "section": "body", "text": "기자는 공적 인물의 행적을 비판하는 기사를 썼다."}],
        "context": {"genre": "news", "election_context": None, "verification_notes": [], "sensitive_material": False},
    }


def finding(pid, sid, **kw):
    f = {"finding_id": "finding-001", "span": {"block_id": "title", "start": 0, "end": 9, "quote": "공적 인물 비판 기사"[:9]},
         "priority_stars": 2, "risk_channels": ["civil"], "issue_category": "unsupported_assertion", "review_confidence": "medium",
         "reason_short": "표현의 확정성을 확인하세요.", "suggested_edit": {"mode": "verify_first", "replacement": None, "related_edits": [],
                                                                  "required_verification": ["근거 확인"]},
         "evidence": [{"source_id": sid, "passage_id": pid}], "counter_considerations": ["공익 보도"]}
    f.update(kw)
    return f


def test_evidence_is_verified_against_the_index(idx):
    art = make_article()
    res = review.build_report_ex(art, [finding(sl.J2, "lawgo_prec:900002")], {}, idx, now=NOW)
    assert res.errors == [] and res.report["execution_mode"] == "live"
    r = res.report
    ev = r["findings"][0]["evidence"][0]
    assert ev["verified"] is True and ev["url"] == "https://www.law.go.kr/LSW/precInfoP.do?precSeq=900002" and ev["authority_type"] == "judgment"
    assert r["corpus_snapshot_id"] == idx.snapshot_id
    assert r["legal_as_of"] == "2026-10-05"                                    # 기사 발행(예정)일. 색인 구축일(2026-10-09)이 아니다
    notes = "\n".join(r["coverage_notes"])
    assert idx.snapshot_id in notes and "구축 2026-10-09" in notes and "최신 결정일 2024-11-07" in notes and "2026-10-05" in notes
    assert "합성" not in notes


def test_evidence_errors_on_index(idx):
    art = make_article()
    bad_sid = review.build_report_ex(art, [finding(sl.J2, "lawgo_prec:900001")], {}, idx, now=NOW)
    assert bad_sid.report is None and any("not in the corpus" in e for e in bad_sid.errors)
    invented = review.build_report_ex(art, [finding("lawgo_prec:1:2000다1#c1", "lawgo_prec:1")], {}, idx, now=NOW)
    assert invented.report is None
    wrong_url = finding(sl.J2, "lawgo_prec:900002")
    wrong_url["evidence"][0]["url"] = "https://www.law.go.kr/made-up"
    assert review.build_report_ex(art, [wrong_url], {}, idx, now=NOW).report is None
    # 기사 발행일 이후의 결정은 근거로 쓸 수 없다
    late = review.build_report_ex(make_article("2024-01-01"), [finding(sl.J3, "lawgo_prec:900003")], {}, idx, now=NOW)
    assert late.report is None and any("after legal_as_of" in e for e in late.errors)
    ok = review.build_report_ex(make_article("2024-12-01"), [finding(sl.J3, "lawgo_prec:900003")], {}, idx, now=NOW)
    assert ok.errors == []


def test_fixture_fallback_note_in_report(tmp_path, monkeypatch):
    monkeypatch.setenv("PRESS_DISPUTE_DATA_DIR", str(tmp_path / "none"))
    c = corpus_mod.load_default()
    res = review.build_report_ex(make_article(), [], {}, c, now=NOW)
    assert res.report["execution_mode"] == "synthetic_example" and res.report["overall_status"] == "partial"
    assert any("실자료 미적재" in n for n in res.report["coverage_notes"])


def test_search_cases_tool_passes_new_arguments(idx):
    r = review.search_cases(idx, query="명예훼손 상당한 이유", stance="counter", institution="supreme_court", limit=3)
    assert r["ok"] and r["stance"] == "counter" and r["passages"][0]["passage_id"] == sl.J2
    bad = review.search_cases(idx, query="x y", stance="zzz")
    assert bad["ok"] is False
    zero = review.search_cases(idx, query="바나나")
    assert zero["ok"] and zero["count"] == 0 and "만들지" in zero["message"]


def test_fixture_corpus_accepts_new_arguments():
    fx = corpus_mod.FixtureCorpus()
    assert fx.search("합성 요약", stance="counter", limit=3)
    assert fx.search("합성 요약", institution="pac") == []
    with pytest.raises(ValueError):
        fx.search("합성", stance="nope")

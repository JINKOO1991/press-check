"""색인 재빌드(P3): 같은 본문 passage 중복 제거·별칭 표·chunk_kind 열·3번 형식 색인 읽기.

중복 제거는 본문과 사건 키·검색·공개 메타데이터(사건번호·권위·기준일·결과·태그·권리·검증 등)가 모두 같은 chunk 만 별칭으로 접는다(Codex 1회차 F1·F2).
본문만 같고 메타데이터가 다른 chunk 는 각자 passage 로 남아 정확 검색·필터에 걸리고, 검색 결과 단계에서 같은 본문을 하나로 접는다."""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: F401,E402

from press_review import corpus_sqlite as cs  # noqa: E402
from press_review.ingestion.manifest import Manifest  # noqa: E402

NOW = dt.datetime(2026, 10, 16, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
FAMILY = "pac_adjustment_casebooks"
SAME = "신청인은 반론보도를 청구하였고 위원회는 조정을 권고하였다."


def build(tmp_path):
    """같은 본문 chunk 3개와 다른 본문 사건 1개, 목록 장 chunk 1개. a 는 사건 c 의 두 번째 chunk 라 사건 키·메타데이터가 c 와 같고
    (c 가 먼저 들어옴, chunk_id 순서는 a 가 앞), b 는 다른 사건이며 사건번호·결정일·권위 유형도 다르다."""
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    m = Manifest(corpus / "manifest.sqlite")
    ids = {}
    try:
        m.ensure_source(FAMILY, "pac", "합성 사례집")
        for name, auth, text, cn, date in (("b", "pac_settlement", SAME, "2012서울조정101", "2012-04-01"),
                                           ("c", "pac_decision", SAME, None, "2012-05-01"), ("d", "pac_decision", "다른 사건 본문이다.", None, "2012-05-01")):
            doc = f"pac_adj:{name}"
            m.upsert_document(doc, FAMILY, canonical_url=f"https://www.pac.or.kr/synthetic/{name}", title="합성 사례집 2012", year=2012)
            m.set_document_status(doc, "structured")
            cid = f"{doc}:case"
            m.upsert_case(cid, doc, {"case_id": cid, "doc_id": doc, "issue_tags": [], "legal_reasoning": [], "institution": "pac", "case_number": cn,
                                     "decision_date": date, "authority_type": auth, "outcome_type": "decision"}, verification_status="unverified")
            m.upsert_chunk(f"{cid}#c1", doc, text, "pdf_page", f"p{name}", case_id=cid, authority_type=auth, decision_date=date)
            ids[name] = f"{cid}#c1"
        m.upsert_chunk("pac_adj:c:case#a0", "pac_adj:c", SAME, "pdf_page", "pa", case_id="pac_adj:c:case", authority_type="pac_decision", decision_date="2012-05-01")
        ids["a"] = "pac_adj:c:case#a0"
        doc = "pac_adj:a"
        m.upsert_document(doc, FAMILY, canonical_url="https://www.pac.or.kr/synthetic/a", title="합성 사례집 2012", year=2012)
        m.set_document_status(doc, "structured")
        m.upsert_attachment(f"{doc}#1", doc, "https://www.pac.or.kr/synthetic/a.pdf", label="전체 목록")
        m.upsert_page(f"{doc}#1:p1", doc, att_id=f"{doc}#1", pdf_page_no=1, method="pdftotext")
        m.upsert_chunk(f"{doc}#1:p1#pc", doc, "2012서울조정100 반론보도 취하 ", "pdf_page", "1", page_id=f"{doc}#1:p1", authority_type="pac_decision")
        ids["list"] = f"{doc}#1:p1#pc"
    finally:
        m.close()
    db = sqlite3.connect(corpus / "manifest.sqlite")
    with db:
        db.execute("UPDATE chunks SET created_at='2026-10-01T00:00:00+00:00' WHERE chunk_id=?", (ids["c"],))
        db.execute("UPDATE chunks SET created_at='2026-10-02T00:00:00+00:00' WHERE chunk_id IN (?,?)", (ids["a"], ids["b"]))
    db.close()
    res = cs.build_index(corpus / "manifest.sqlite", tmp_path / "index", now=NOW)
    assert res["status"] == "built"
    return res, cs.open_current(tmp_path / "index"), ids


def test_same_text_and_metadata_keeps_earliest_chunk_and_alias_resolves_to_its_own_locator(tmp_path):
    res, idx, ids = build(tmp_path)
    try:
        assert res["passages"] == 4 and res["aliases"] == 1 and res["excluded"]["duplicate_text"] == 1     # a 만 c 로 접힌다. b 는 메타데이터가 달라 남는다
        assert res["by_chunk_kind"] == {"case": 3, "list": 1}
        assert idx.get_passage("pac_adj:c", ids["c"])["text"] == SAME                    # created_at 이 가장 이른 c 가 남는다(chunk_id 순서는 a)
        p = idx.get_passage("pac_adj:c", ids["a"])                                       # 옛 id: 정본 본문 + 옛 passage 자신의 출처·url·위치
        assert p["text"] == SAME and p["alias_of"] == ids["c"] and p["passage_id"] == ids["a"]
        assert (p["url"], p["locator"], p["authority_type"]) == ("https://www.pac.or.kr/synthetic/c", {"kind": "pdf_page", "value": "pa"}, "pac_decision")
        assert idx.get_passage("pac_adj:b", ids["a"]) is None                             # 출처가 다르면 해석하지 않는다
        got = [h["passage_id"] for h in idx.search("반론보도", limit=10)]
        assert len(got) == 2 and got[0] in (ids["b"], ids["c"]) and got[1] == ids["list"]     # 같은 본문 b·c 는 결과 단계에서 하나로 접힌다, 별칭 a 는 나오지 않는다
    finally:
        idx.close()


def test_same_text_with_other_metadata_stays_searchable_by_its_own_case_number_authority_and_date(tmp_path):
    """F1: 본문이 같아도 b 의 사건번호·권위 유형·기준일로 찾을 수 있어야 한다(P3_DESIGN §1 정확 검색·필터 계약)."""
    res, idx, ids = build(tmp_path)
    try:
        assert [h["passage_id"] for h in idx.search("2012서울조정101", limit=5)] == [ids["b"]]
        assert [h["passage_id"] for h in idx.search("반론보도", authority_types=["pac_settlement"], limit=5)] == [ids["b"]]
        assert [h["passage_id"] for h in idx.search("반론보도", as_of="2012-04-15", limit=5) if h["text"] == SAME] == [ids["b"]]
        assert idx.get_passage("pac_adj:b", ids["b"])["authority_type"] == "pac_settlement" and "alias_of" not in idx.get_passage("pac_adj:b", ids["b"])
        got = [h["passage_id"] for h in idx.search("조정", limit=2)]                     # 2글자 질의: 같은 본문을 접어 모자란 자리는 남은 후보로 채운다
        assert len(got) == 2 and got[0] in (ids["b"], ids["c"]) and got[1] == ids["list"]
    finally:
        idx.close()


def test_alias_lookup_keeps_its_own_rights_display_cap_and_rights_change_moves_snapshot(tmp_path):
    """F2: 같은 720자 본문에서 정본은 재배포 허용, 다른 문서는 권리 미허락이면 그 문서의 조회는 300자 상한을 지킨다. 그 문서의 권리만 바꿔도 snapshot 이 바뀐다."""
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    text = "가" * 720
    m = Manifest(corpus / "manifest.sqlite")
    try:
        m.ensure_source(FAMILY, "pac", "합성 사례집")
        for name in ("x", "y"):
            doc = f"pac_adj:{name}"
            m.upsert_document(doc, FAMILY, canonical_url=f"https://www.pac.or.kr/synthetic/{name}", title="합성", year=2012)
            m.set_document_status(doc, "structured")
            m.upsert_attachment(f"{doc}#1", doc, f"https://www.pac.or.kr/synthetic/{name}.pdf", label="본문")
            m.upsert_page(f"{doc}#1:p1", doc, att_id=f"{doc}#1", pdf_page_no=1, method="pdftotext")
            m.upsert_chunk(f"{doc}#p1", doc, text, "pdf_page", "1", page_id=f"{doc}#1:p1", authority_type="pac_decision", decision_date="2012-05-01")
        m.set_rights("document", "pac_adj:x", sl._rights(redistribute_raw="allowed", redistribute_derivatives="allowed"),
                     [{"scope": "시험", "review_status": "verified", "url": "https://www.pac.or.kr/"}], "verified")
    finally:
        m.close()
    a = cs.build_index(corpus / "manifest.sqlite", tmp_path / "ia", now=NOW)
    idx = cs.open_current(tmp_path / "ia")
    try:
        y = idx.get_passage("pac_adj:y", "pac_adj:y#p1")
        assert y["redistributable"] is False and len(y["display_text"]) == cs.DISPLAY_QUOTE_MAX_CP and y["display_truncated"] is True
        assert idx.get_passage("pac_adj:x", "pac_adj:x#p1")["redistributable"] is True
    finally:
        idx.close()
    m = Manifest(corpus / "manifest.sqlite")
    m.set_rights("document", "pac_adj:y", sl._rights(redistribute_raw="allowed", redistribute_derivatives="allowed"),
                 [{"scope": "시험", "review_status": "verified", "url": "https://www.pac.or.kr/"}], "verified")
    m.close()
    b = cs.build_index(corpus / "manifest.sqlite", tmp_path / "ib", now=NOW, dry_run=True)
    assert b["snapshot_id"] != a["snapshot_id"]


def test_page_chunk_without_case_stays_indexed_even_if_its_attachment_is_admission_excluded(tmp_path):
    """G-D33(P2_DESIGN): 첨부 단위 제외는 그 첨부의 사건에만 적용한다. 사건 없는 쪽 chunk(case_id 없음)는 이 제외의 대상이 아니다."""
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    m = Manifest(corpus / "manifest.sqlite")
    try:
        m.ensure_source(FAMILY, "pac", "합성 사례집")
        doc = "pac_adj:z"
        m.upsert_document(doc, FAMILY, canonical_url="https://www.pac.or.kr/synthetic/z", title="합성", year=2012)
        m.set_document_status(doc, "structured")
        for k in ("1", "2"):
            m.upsert_attachment(f"{doc}#{k}", doc, f"https://www.pac.or.kr/synthetic/z{k}.pdf", label="본문")
            m.upsert_page(f"{doc}#{k}:p1", doc, att_id=f"{doc}#{k}", pdf_page_no=1, method="pdftotext")
            m.upsert_chunk(f"{doc}#{k}:p1#pc", doc, f"첨부 {k} 쪽 본문", "pdf_page", "1", page_id=f"{doc}#{k}:p1", authority_type="pac_decision")
        m.record_failure(f"{doc}#2", "admission", "attachment_excluded", "G-D33 합성")
    finally:
        m.close()
    res = cs.build_index(corpus / "manifest.sqlite", tmp_path / "idx", now=NOW, dry_run=True)
    assert res["passages"] == 2 and "case_att_excluded" not in res["excluded"]


def test_case_counts_equal_eligible_case_keys_when_cases_share_text(tmp_path):
    """G-D22: 사건 수 통계(cases_by_authority_type, core_coverage 게이트 입력)는 본문이 같은 다른 사건을 접어도 줄지 않는다."""
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    m = Manifest(corpus / "manifest.sqlite")
    try:
        m.ensure_source(FAMILY, "pac", "합성 사례집")
        for name in ("x", "y"):                      # 사건만 다르고 본문·제목·권위·결정일·결과가 같다
            doc = f"pac_adj:{name}"
            m.upsert_document(doc, FAMILY, canonical_url=f"https://www.pac.or.kr/synthetic/{name}", title="합성", year=2012)
            m.set_document_status(doc, "structured")
            cid = f"{doc}:case"
            m.upsert_case(cid, doc, {"case_id": cid, "doc_id": doc, "issue_tags": [], "legal_reasoning": [], "institution": "pac",
                                     "decision_date": "2012-05-01", "authority_type": "pac_decision", "outcome_type": "decision"}, verification_status="unverified")
            m.upsert_chunk(f"{cid}#c1", doc, SAME, "pdf_page", "p1", case_id=cid, authority_type="pac_decision", decision_date="2012-05-01")
    finally:
        m.close()
    db = sqlite3.connect(corpus / "manifest.sqlite")
    eligible = db.execute("SELECT count(DISTINCT case_id) FROM chunks WHERE case_id IS NOT NULL").fetchone()[0]
    db.close()
    res = cs.build_index(corpus / "manifest.sqlite", tmp_path / "idx", now=NOW, dry_run=True)
    assert eligible == 2 and res["cases_by_authority_type"] == {"pac_decision": eligible}


def test_chunk_kind_drives_demotion_and_index_format_3_still_opens(tmp_path):
    res, idx, ids = build(tmp_path)
    kinds = {h["passage_id"]: h["chunk_kind"] for h in idx.search("반론보도", limit=10)}
    path = Path(idx._path)
    idx.close()
    assert sorted(kinds.values()) == ["case", "list"] and kinds[ids["list"]] == "list"
    db = sqlite3.connect(path)                      # 3번 형식 색인 모양으로 되돌린다: chunk_kind 열·별칭 표 없음
    with db:
        db.execute("DROP TABLE passage_alias")
        db.execute("ALTER TABLE passages DROP COLUMN chunk_kind")
        db.execute("UPDATE meta SET value='pdr-index-3' WHERE key='format'")
    db.close()
    old = cs.SqliteCorpus(path)
    try:
        got = [h["passage_id"] for h in old.search("반론보도", limit=10)]
        assert set(got[:2]) == {ids["b"], ids["c"]} and got[2] == ids["list"] and old.get_passage("pac_adj:c", ids["a"]) is None   # 3번 형식은 결과 접기 없음, 목록 장은 이전 술어로 강등, 별칭 없음
    finally:
        old.close()

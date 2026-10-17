"""검색 순위: 사례집 목록·색인 장 chunk(G-D32) 강등과 2글자 질의어 점수(IDF·빈도 포화, 후보 전체에 반영)."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: F401,E402

from press_review import corpus_sqlite as cs  # noqa: E402
from press_review.ingestion.manifest import Manifest  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
FAMILY = "pac_adjustment_casebooks"


def build(tmp_path, cases, lists=()):
    """cases: (name, text[, authority_type]) 구조화 사건 passage(기본 pac_decision). lists: (name, text) 목록 장 쪽 chunk(case 없음, G-D32)."""
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    m = Manifest(corpus / "manifest.sqlite")
    ids = {}
    try:
        m.ensure_source(FAMILY, "pac", "합성 사례집")
        doc = "pac_adj:9001"
        m.upsert_document(doc, FAMILY, canonical_url="https://www.pac.or.kr/synthetic/9001", title="합성 사례집", year=2012)
        m.set_document_status(doc, "structured")
        for i, (name, text, *auth) in enumerate(cases):
            auth = auth[0] if auth else "pac_decision"
            cid = f"{doc}:case{i:03d}"
            rec = {"case_id": cid, "doc_id": doc, "issue_tags": [], "legal_reasoning": [], "institution": "pac",
                   "decision_date": "2012-05-01", "authority_type": auth, "outcome_type": "decision"}
            m.upsert_case(cid, doc, rec, verification_status="unverified")
            m.upsert_chunk(f"{cid}#c1", doc, text, "pdf_page", f"p{10 + i}", case_id=cid, authority_type=auth, decision_date="2012-05-01")
            ids[name] = f"{cid}#c1"
        if lists:
            m.upsert_attachment(f"{doc}#1", doc, "https://www.pac.or.kr/synthetic/9001.pdf", label="제5부 언론조정·중재사건 전체 목록")
        for i, (name, text) in enumerate(lists):
            page = f"{doc}#1:p{500 + i:04d}"
            m.upsert_page(page, doc, att_id=f"{doc}#1", pdf_page_no=500 + i, method="pdftotext")
            pid = f"{page}#pc"
            m.upsert_chunk(pid, doc, text, "pdf_page", str(500 + i), page_id=page, authority_type="pac_decision")
            ids[name] = pid
    finally:
        m.close()
    res = cs.build_index(corpus / "manifest.sqlite", tmp_path / "index", now=NOW)
    assert res["status"] == "built"
    return cs.open_current(tmp_path / "index"), ids


def pids(hits):
    return [h["passage_id"] for h in hits]


def test_list_chunk_ranks_below_case_passages_on_general_queries(tmp_path):
    row = "2012서울조정{} 반론보도 기사삭제 취하 "
    lst = "".join(row.format(100 + k) for k in range(20))
    idx, ids = build(tmp_path, [("case", "신청인은 반론보도를 청구하였고 기사삭제도 요구하였다. 위원회는 조정을 권고하였다.")], [("list", lst)])
    try:
        for q in ("반론보도", "삭제"):                      # trigram BM25 경로와 2글자(bigram) 경로
            got = pids(idx.search(q, limit=5))
            assert got == [ids["case"], ids["list"]], (q, got)       # 강등이지 제외가 아니다
    finally:
        idx.close()


def test_list_chunk_still_first_for_exact_phrase_and_when_alone(tmp_path):
    lst = "2012서울조정100 반론보도 취하 2012서울조정101 정정보도 기각 "
    idx, ids = build(tmp_path, [("case", "신청인은 반론보도를 청구하였다.")], [("list", lst)])
    try:
        assert pids(idx.search("2012서울조정101 반론보도", limit=5))[0] == ids["list"]     # 본문 속 사건번호 구문 일치(tier 1)는 강등하지 않는다
        assert pids(idx.search("정정보도", limit=5)) == [ids["list"]]                        # 유일한 일치면 그대로 나온다
    finally:
        idx.close()


def test_two_char_query_weights_rare_terms_over_repeated_common_terms(tmp_path):
    cases = [("common", "공개 " * 30 + "자료"), ("both", "피해자의 실명을 공개한 보도")]
    cases += [(f"f{i}", f"공개 회의 {i}회차 기록") for i in range(8)]                   # '공개'는 흔한 어휘, '실명'은 드문 어휘
    idx, ids = build(tmp_path, cases)
    try:
        assert pids(idx.search("실명 공개", limit=3))[0] == ids["both"]
    finally:
        idx.close()


def test_two_char_words_in_mixed_query_score_every_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CANDIDATE_CAP", 2)          # 2글자 점수의 전역 후보 상한 밖에 있는 후보도 점수를 받아야 한다
    cases = [("z", "반론보도 반론보도 사건이다"), ("y", "반론보도 청구를 기각한 사건이다")]
    cases += [(f"w{i}", "기각 " * 20 + f"사건 {i}") for i in range(2)]
    idx, ids = build(tmp_path, cases)
    try:
        assert pids(idx.search("반론보도 기각", limit=2))[0] == ids["y"]
    finally:
        idx.close()


def test_authority_weight_puts_judgment_over_slightly_stronger_research_without_dropping_it(tmp_path):
    cases = [("research", "정정보도 청구 사건에서 정정보도의 요건을 정리한 연구 정정보도", "research"),
             ("judgment", "정정보도 청구 사건에서 정정보도의 요건을 판단한 판결", "judgment")]
    cases += [(f"f{i}", f"무관한 기록 {i}") for i in range(6)]
    idx, ids = build(tmp_path, cases)
    try:
        got = pids(idx.search("정정보도 요건", limit=5))
        assert got[:2] == [ids["judgment"], ids["research"]], got     # 판례가 앞서되 연구자료도 남는다
    finally:
        idx.close()


def test_noun_ending_in_josa_letter_is_not_stripped_when_the_index_shows_it_takes_particles(tmp_path):
    cases = [("n1", "전문가의 의견을 들었다"), ("n2", "전문가는 신중했다"), ("n3", "전문가를 불렀다"),
             ("other", "전문 분야의 기록이다"), ("josa", "기자는 현장을 취재했다")]
    idx, ids = build(tmp_path, cases)
    try:
        assert ids["other"] not in pids(idx.search("전문가", limit=5))           # '전문'으로 잘라 2글자 잡음으로 넓히지 않는다
        assert ids["josa"] in pids(idx.search("기자가", limit=5))                # '기자가를' 꼴은 없으므로 조사로 보고 뗀다
    finally:
        idx.close()

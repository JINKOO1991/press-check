"""U3c: vote_opinion 구간(법정의견 표기 없는 위헌·합헌 등 의견)의 court_majority 항목을 의견 입장과 최종 주문의 편으로 다시 매긴다. 합성 fixture.
한계: 다중 주문(예: 위헌+기각+각하)은 sides(D)가 여러 편이라 입장의 편이 그중 하나면 concurring 이다."""
import json

import pytest

import pdr_ing_paths  # noqa: F401
from press_review import corpus_sqlite as cs
from test_detc_structure import DIS, LEDGER, QUOTE, TXT, base_case, full_text, pid_of, reply, run_structurer, seed_detc
from test_detc_structure import assemble, draft

ORDER_UNCONST = "1. A법 제1조는 헌법에 위반된다.\n2. 나머지 청구를 각하한다."


def vrow(vote, opinion="vote_opinion", needs_review=0):
    return {"quality_json": json.dumps({"section_kind": "opinion", "opinion_kind": opinion, "vote": vote}), "needs_review": needs_review, "pdf_page_no": None}


def run(profile, vote, dispositions, opinion="vote_opinion", needs_review=0):
    rows = {"p": vrow(vote, opinion, needs_review), "p_dis": vrow(None, "dissent")}
    d = draft("p", "court_majority", institution="constitutional_court" if profile != "court_judgment" else "supreme_court")
    return assemble(profile, rows, TXT, d, dict(LEDGER, dispositions=dispositions) if profile != "court_judgment" else {})


@pytest.mark.parametrize("vote,dispositions,speaker", [
    ("위헌", ["위헌"], "court_concurring"),
    ("합헌", ["위헌"], "court_dissenting"),
    ("각하", ["위헌", "각하"], "court_concurring"),            # 다중 주문 한계: 각하 편이 sides(D) 안에 있으므로 concurring
    ("헌법불합치", ["위헌"], "court_concurring"),              # 같은 무효 편
    ("기각", ["위헌"], "court_dissenting"),
    ("단순위헌", ["위헌"], "court_concurring"),
])
def test_vote_opinion_relabel_by_side_of_order(vote, dispositions, speaker):
    co = run("constitutional_decision", vote, dispositions)
    got = [x["speaker"] for x in co.record["legal_reasoning"]]
    assert got == [speaker, "court_dissenting"] and co.record["verification_status"] == "unverified"
    n_c, n_d = (1, 0) if speaker == "court_concurring" else (0, 1)
    assert f"vote_relabel:{n_c}_concurring/{n_d}_dissenting" in co.record["quote_check"]["dropped_fields"]
    assert co.record["quote_check"]["total"] == co.record["quote_check"]["matched"]            # 인용 일치율 불변
    assert not any("opinion_guard" in f for f in co.record["quote_check"]["dropped_fields"])


@pytest.mark.parametrize("vote,dispositions", [(None, ["위헌"]), ("알수없음", ["위헌"]), ("위헌", []), ("위헌", ["unknown", "unknown"])])
def test_vote_opinion_unknown_stance_or_no_classified_order_still_removes(vote, dispositions):
    co = run("constitutional_decision", vote, dispositions)
    assert [x["speaker"] for x in co.record["legal_reasoning"]] == ["court_dissenting"]
    assert any("opinion_guard:opinion_kind_vote_opinion" in f for f in co.record["quote_check"]["dropped_fields"])
    assert not any(f.startswith("vote_relabel") for f in co.record["quote_check"]["dropped_fields"])


@pytest.mark.parametrize("opinion", ["dissent", "concurrence", "majority_supplement", "dissent_supplement"])
def test_non_vote_opinion_sections_behave_as_before(opinion):
    co = run("constitutional_decision", "위헌", ["위헌"], opinion=opinion)
    assert [x["speaker"] for x in co.record["legal_reasoning"]] == ["court_dissenting"]            # 제거 유지
    assert not any(f.startswith("vote_relabel") for f in co.record["quote_check"]["dropped_fields"])


def test_vote_opinion_in_needs_review_page_is_still_removed():
    co = run("constitutional_decision", "위헌", ["위헌"], needs_review=1)
    assert [x["speaker"] for x in co.record["legal_reasoning"]] == ["court_dissenting"]


def test_court_judgment_profile_drops_vote_page_quote_without_relabel():
    co = run("court_judgment", "위헌", ["위헌"])
    assert [x["speaker"] for x in co.record["legal_reasoning"]] == ["court_dissenting"]
    fields = co.record["quote_check"]["dropped_fields"]
    assert any("opinion_guard" in f for f in fields) and not any(f.startswith("vote_relabel") for f in fields)


@pytest.mark.parametrize("order,speaker,kind", [("1. A법 제1조는 헌법에 위반된다.\n2. 나머지 청구를 각하한다.", "court_concurring", "concurring"),
                                                 (None, "court_dissenting", "dissenting")])
def test_relabelled_item_reaches_chunk_and_opinion_kinds(manifest, corpus, order, speaker, kind):
    tail = "6. 재판관 가나다의 위헌의견\n합성 위헌 이유이다.\n"
    full = full_text(tail=tail) if order is None else full_text(order=order, tail=tail)
    doc_id = seed_detc(manifest, corpus, full=full)
    vp = pid_of(manifest, doc_id, "opinion", "vote_opinion")
    c = base_case(manifest, doc_id)
    if order is not None:               # 주문 인용·처분 표기를 새 주문에 맞춘다(처분 가드 통과)
        c["decision_type"] = "위헌, 각하"
        c["outcome"]["quote"]["quote"] = "1. A법 제1조는 헌법에 위반된다."
    c["legal_reasoning"].append({"text": "위헌의견", "speaker": "court_majority", "quote": {"page_id": vp, "quote": "합성 위헌 이유이다."}})
    run_structurer(manifest, corpus, reply(c))
    rec = manifest.get_case("lawgo_detc:1:2020헌마1")["record"]
    assert [x["speaker"] for x in rec["legal_reasoning"]] == ["court_majority", speaker]
    ch = manifest.select("SELECT * FROM chunks WHERE case_id=?", (rec["case_id"],))[0]
    assert "합성 위헌 이유이다." in ch["text"]
    assert kind in cs.opinion_kinds_of(rec) and "majority" in cs.opinion_kinds_of(rec)

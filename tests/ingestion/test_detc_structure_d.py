"""U3d: 처분 가드의 '거친 표기'(위헌 vs 한정위헌·헌법불합치, 합헌 vs 한정합헌) 허용과 decision_type 구체화. 합성 fixture."""
import pytest

import pdr_ing_paths  # noqa: F401
from test_detc_structure import LEDGER, TXT, assemble, draft, row
from test_detc_structure_c import vrow

ROWS = {"p": row("reasons", "majority"), "p_dis": row("opinion", "dissent"), "p_ord": row("order")}


def run(decision_type, dispositions, rows=ROWS, **over):
    return assemble("constitutional_decision", rows, dict(TXT, p_ord="주문 문장이다.\n"), draft("p", "court_majority", decision_type=decision_type, **over),
                    dict(LEDGER, dispositions=dispositions))


def notes(co):
    return [f for f in co.record["quote_check"]["dropped_fields"] if f.startswith("decision_type:decision_type_refined")]


@pytest.mark.parametrize("model,ledger,want,note", [
    ("헌법에 위반된다", ["한정위헌"], "한정위헌", "위헌->한정위헌"),
    ("위헌", ["헌법불합치"], "헌법불합치", "위헌->헌법불합치"),
    ("합헌", ["한정합헌"], "한정합헌", "합헌->한정합헌"),
    ("위헌, 각하", ["한정위헌", "각하"], "한정위헌, 각하", "위헌->한정위헌"),
])
def test_coarse_printed_disposition_is_refined_not_quarantined(model, ledger, want, note):
    co = run(model, ledger)
    assert co.record["verification_status"] == "unverified" and co.record["decision_type"] == want
    assert notes(co) == [f"decision_type:decision_type_refined:{note}"]


@pytest.mark.parametrize("model,ledger", [("합헌", ["위헌"]), ("기각", ["한정위헌"]), ("합헌", ["한정위헌"]), ("위헌", ["한정합헌"]), ("위헌", ["기각"])])
def test_real_contradictions_still_quarantine(model, ledger):
    co = run(model, ledger)
    assert co.record["verification_status"] == "quarantined" and any(e.startswith("label_guard: decision_type") for e in co.errors)
    assert not notes(co)


def test_exact_equivalent_and_unclassified_cases_are_untouched():
    for model, ledger in (("위헌", ["위헌"]), ("위헌", ["인용"]), ("인용", ["위헌"]), ("헌법불합치", ["헌법불합치"])):
        co = run(model, ledger)
        assert co.record["verification_status"] == "unverified" and co.record["decision_type"] == model and not notes(co)
    co = run("위헌", ["unknown"])                                  # 전부 unknown 이면 가드·구체화 생략
    assert co.record["decision_type"] == "위헌" and not notes(co)
    co = run("위헌, 기타", ["한정위헌"])                              # 대응 못 한 조각이 있으면 격리하지 않되 문자열은 바꾸지 않는다
    assert co.record["verification_status"] == "unverified" and co.record["decision_type"] == "위헌, 기타"


def test_refinement_does_not_change_vote_opinion_relabel_which_uses_ledger_sides():
    rows = dict(ROWS, p_vote=vrow("위헌"))
    txt = dict(TXT, p_ord="주문 문장이다.\n", p_vote=TXT["p"])
    d = draft("p_vote", "court_majority", decision_type="위헌")
    co = assemble("constitutional_decision", rows, txt, d, dict(LEDGER, dispositions=["한정위헌"]))
    assert [x["speaker"] for x in co.record["legal_reasoning"]][0] == "court_concurring"        # 장부 편(INV) 기준: 불변
    assert co.record["decision_type"] == "한정위헌"

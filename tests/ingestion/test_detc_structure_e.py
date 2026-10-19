"""U3e: 각하 결정의 outcome_scope 'procedural' 정규화(계약: 법원 판결에는 procedural 불가)와 인쇄 표기('위헌임을 확인', '…처분을 취소') 처분 매핑. 합성 fixture."""
import pytest

import pdr_ing_paths  # noqa: F401
from press_review.ingestion import structure_records as sr
from test_detc_structure import LEDGER, QUOTE, TXT, assemble, draft, full_text, pid_of, q, reply, row, run_structurer, seed_detc

DISMISS = "이 사건 심판청구를 각하한다."


@pytest.mark.parametrize("order,decision_type,quote", [
    (DISMISS, "각하", DISMISS),
    ("1. 청구를 기각한다.\n2. 나머지 청구를 각하한다.", "기각, 각하", "2. 나머지 청구를 각하한다."),
])
def test_dismissal_decision_with_procedural_scope_is_not_quarantined_and_gets_chunk(manifest, corpus, order, decision_type, quote):
    doc_id = seed_detc(manifest, corpus, full=full_text(order=order, tail=""))
    c = {"case_number": "2020헌마1", "institution": "constitutional_court", "authority_type": "judgment", "decision_type": decision_type,
         "outcome_type": "judgment", "outcome_scope": "procedural", "settlement_or_adjudication": "adjudication",
         "first_page_id": pid_of(manifest, doc_id, "case_header"),
         "outcome": {"text": decision_type, "speaker": "court_majority", "quote": q(pid_of(manifest, doc_id, "order"), quote)}}
    res, _ = run_structurer(manifest, corpus, reply(c))
    case = manifest.get_case("lawgo_detc:1:2020헌마1")
    rec = case["record"]
    assert res["cases_quarantined"] == 0 and rec["verification_status"] == "unverified" and rec["outcome_type"] == "judgment"
    assert rec["outcome_scope"] == "unclear" and any("outcome_scope:procedural_not_allowed_for_judgment" in f for f in rec["quote_check"]["dropped_fields"])
    assert len(manifest.select("SELECT * FROM chunks WHERE case_id=?", (case["case_id"],))) == 1


def test_merits_scope_unchanged_and_court_judgment_profile_still_rejects_procedural():
    rows = {"p": row("order"), "p_dis": row("opinion", "dissent")}
    out = {"text": "결론", "speaker": "court_majority", "quote": q("p", QUOTE)}
    for scope in ("whole", "partial"):
        co = assemble("constitutional_decision", rows, TXT, draft("p", "court_majority", outcome=out, outcome_type="judgment", outcome_scope=scope), LEDGER)
        assert co.record["outcome_scope"] == scope and co.record["verification_status"] == "unverified"
    cj = assemble("court_judgment", rows, TXT, draft("p", "court_majority", outcome=out, outcome_type="judgment", outcome_scope="procedural",
                                                       institution="supreme_court"), {})
    assert cj.record["verification_status"] == "quarantined" and cj.record["outcome_scope"] == "procedural"       # 판례: 계약 그대로


@pytest.mark.parametrize("text,want", [
    ("위헌임을 확인한다", {"인용"}), ("위헌임을 확인", {"인용"}), ("기소유예처분을 취소한다", {"인용"}), ("기소유예처분을 취소", {"인용"}),
    ("기소유예처분을 취소, 각하", {"인용", "각하"}), ("위헌, 각하", {"위헌", "각하"}), ("각 헌법에 위반된다", {"위헌"}),
])
def test_map_decision_type_confirm_and_revoke_phrases(text, want):
    assert sr.map_decision_type(text) == (want, [])


def test_non_disposition_fragments_are_ignored_but_unknown_words_still_noted():
    assert sr.map_decision_type("확인") == (set(), []) and sr.map_decision_type("취소 각하") == ({"각하"}, [])
    assert sr.map_decision_type("기타 결정") == (set(), ["기타", "결정"])
    assert sr.map_decision_type("각하 확인") == ({"각하"}, [])

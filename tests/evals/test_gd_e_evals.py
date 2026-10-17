"""G-D21·G-D23이 평가 규칙에 반영됐는지: 선거 근거에 guideline 허용, 기준일은 reference_date."""
import evalkit


class _Corpus:
    def __init__(self, passage):
        self._p = passage

    def get_passage(self, source_id, passage_id):
        return self._p


def _report(auth):
    return {"legal_as_of": "2026-10-05", "findings": [{
        "finding_id": "finding-001", "risk_channels": ["pac_election"], "suggested_edit": {"mode": "verify_first"},
        "evidence": [{"source_id": "s", "passage_id": "p", "authority_type": auth}]}]}


def _run(auth, passage):
    out = evalkit.rule_outdated_election({}, _report(auth), {}, {"corpus": _Corpus(passage)})
    assert len(out) == 1
    return out[0]


def test_guideline_is_allowed_election_evidence():
    assert "guideline" in evalkit._ELECTION_AUTH["pac_election"] and "guideline" in evalkit._ELECTION_AUTH["iendc_election"]
    assert _run("guideline", {"decision_date": None, "reference_date": "2026-01-01"})["status"] == evalkit.PASS


def test_election_rule_uses_reference_date_when_decision_date_is_missing():
    late = _run("pac_election_review", {"decision_date": None, "reference_date": "2026-12-01"})
    assert late["status"] == evalkit.FAIL and "after legal_as_of" in late["detail"]
    assert _run("pac_election_review", {"decision_date": None, "reference_date": None, "date_unknown": True})["status"] == evalkit.PASS

"""P4 V3: 스키마 v2 하위 호환, 다중 매칭 recall, 불필요 수정률, 사실 추가 어휘 탐지, 자기 사건 차단, 호스트 기록."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))
import evalkit  # noqa: E402
import p3_kit  # noqa: E402
import p4_metrics as pm  # noqa: E402
import run_p3_evals  # noqa: E402

EX = evalkit.HERE / "p4_scenarios_v2_examples.json"


def sp(bid, s, e):
    return {"block_id": bid, "start": s, "end": e, "quote": "x"}


def fnd(fid, mode, spans, channels=("civil",), related=()):
    return {"finding_id": fid, "span": spans[0], "risk_channels": list(channels),
            "suggested_edit": {"mode": mode, "replacement": "r" if mode == "replace" else None,
                               "related_edits": [{"span": s, "replacement": "r"} for s in [*spans[1:], *related]]}}


def gold(iid, spans, sev="must_fix", ch=("civil",)):
    return {"issue_id": iid, "spans": spans, "channels": list(ch), "action": "replace", "severity": sev, "label_status": "draft"}


def test_v1_files_validate_unchanged_and_v2_examples_validate():
    assert p3_kit.validate_scenarios(p3_kit.load_scenarios()) == []
    assert p3_kit.validate_scenarios(p3_kit.load_scenarios(EX)) == []
    bad = copy.deepcopy(p3_kit.load_scenarios(EX))
    bad["cases"][0]["gold_issues"][0]["severity"] = "huge"
    bad["cases"][0]["split"] = "val"
    assert len(p3_kit.validate_scenarios(bad)) >= 2


def test_one_finding_fills_several_gold_issues_but_counts_once():
    f = fnd("f1", "replace", [sp("title", 0, 5), sp("p1", 10, 20)])           # 제목 구간 + 본문 related_edits 구간
    m = pm.match_gold([f, fnd("f2", "verify_first", [sp("p3", 0, 3)])],
                      [gold("a", [sp("title", 3, 8)]), gold("b", [sp("p1", 15, 18)], "should_check"), gold("c", [sp("p2", 0, 9)], "uncertain"),
                       gold("d", [sp("title", 5, 9)])])                      # 반개구간: [0,5) 와 [5,9) 는 안 겹친다
    assert m["recall_must_or_should"] == {"matched": 2, "total": 3}          # uncertain 은 분모에서 뺀다
    assert m["findings"] == {"total": 2, "matched": 1}


def test_overedit_rates_article_and_span():
    assert pm.article_overedit([{"expected_edit": "none", "has_replace": True}, {"expected_edit": "none", "has_replace": False},
                                {"expected_edit": "required", "has_replace": True}]) == {"overedited": 1, "total": 2}
    touched = pm.preserve_touched([fnd("f", "replace", [sp("p1", 0, 5)]), fnd("g", "verify_first", [sp("p2", 0, 5)])],
                                  [sp("p1", 3, 9), sp("p2", 0, 9), sp("p3", 0, 9)])
    assert touched == [0]                                                      # verify_first 는 수정이 아니다


def test_added_fact_lexicon_respects_reply_state_and_existing_text():
    art = {"blocks": [{"block_id": "p1", "text": "측이 해명을 요청받았다고 했다"}]}
    assert pm.added_fact_scan(art, {"reply_request": "unknown"}, ["연락이 닿지 않았다"]) != []
    assert pm.added_fact_scan(art, {"reply_request": "requested_no_answer"}, ["연락이 닿지 않았다"]) == []
    assert pm.added_fact_scan(art, {"reply_request": "unknown"}, ["해명을 요청"]) == []        # 기사에 이미 있는 문구
    assert pm.added_fact_scan(art, {"reply_request": "unknown"}, ["문서를 확보했다"]) != []


def test_self_case_group_is_removed_from_search_and_evidence():
    class C:
        def search(self, q, **kw):
            return [{"passage_id": "p1", "source_id": "s1", "case_number": "2020다1"}, {"passage_id": "p2", "source_id": "s2", "case_number": None}]
        def get_passage(self, s, p):
            return {"passage_id": p, "source_id": s, "case_number": "2020다1" if s == "s1" else None}
    b = pm.BlockedCorpus(C(), {"case_numbers": ["2020다1"]})
    assert [r["passage_id"] for r in b.search("x")] == ["p2"] and b.removed == 1
    ev = [{"finding_id": "f", "evidence": [{"source_id": "s1", "passage_id": "p1"}, {"source_id": "s2", "passage_id": "p2"}]}]
    assert pm.self_case_evidence(ev, C(), {"case_numbers": ["2020다1"]}) == ["f:p1"]


def test_runner_records_host_and_scores_v2_examples(tmp_path):
    args = ["--scenarios", str(EX), "--outputs-dir", str(evalkit.HERE / "p4_golden"), "--out-dir", str(tmp_path), "--out-name", "o.json"]
    assert run_p3_evals.main(args + ["--require-host"]) == 2                    # 호스트 미기록은 거부
    assert run_p3_evals.main(args + ["--host-model", "m-x", "--host-effort", "high", "--strict"]) == 0
    out = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))
    assert out["meta"]["host"] == {"model": "m-x", "effort": "high", "skill_version": None, "pinned": True}
    s = out["p4_summary"]
    assert s["by_derivation"] == {"card_from_case": 0, "minimal_pair_of": 1, "fully_invented": 2, "v1(미분류)": 0}
    assert s["scenarios"] == 3 and s["recall_must_fix"] == {"matched": 2, "total": 2} and s["overedit_article"] == {"overedited": 0, "total": 1}


def test_future_window_filter_drops_undated_and_year_upper_bound_and_pins_as_of():
    rows = [{"passage_id": "a", "reference_date": "2015-01-01", "reference_date_basis": "decision_date", "date_unknown": False},
            {"passage_id": "b", "reference_date": None, "reference_date_basis": None, "date_unknown": True},
            {"passage_id": "c", "reference_date": "2012-12-31", "reference_date_basis": "year_upper_bound", "date_unknown": False}]
    kept, n = pm.future_window_filter(rows)
    assert [r["passage_id"] for r in kept] == ["a"] and n == {"date_unknown": 1, "year_upper_bound": 1}

    class C:
        def search(self, q, **kw):
            self.kw = kw
            return rows
    c = C()
    w = pm.FutureWindowCorpus(c)
    assert [r["passage_id"] for r in w.search("x", as_of="2030-01-01")] == ["a"] and c.kw["as_of"] == "2018-09-30" == pm.FUTURE_AS_OF
    assert pm.FUTURE_T == "2018-10-01" and w.removed == {"date_unknown": 1, "year_upper_bound": 1}

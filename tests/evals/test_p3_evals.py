"""P3 평가 확장: 시나리오 파일 구조, 색인 연결, pending_corpus, 자동 판정 항목과 오류 주입."""
import copy
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: E402

import evalkit  # noqa: E402
import p3_kit  # noqa: E402
import run_p3_evals  # noqa: E402
from press_review import contracts, corpus as corpus_mod, corpus_sqlite as cs  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
RESERVED = ("report", "findings", "summary", "analysis")


# ------------------------------------------------------------------ 배포 파일 구조

def test_shipped_scenarios_are_well_formed():
    data = p3_kit.load_scenarios()
    assert p3_kit.validate_scenarios(data) == []
    cases = data["cases"]
    assert len(cases) >= 8
    for cat in p3_kit.CATEGORIES:
        assert sum(c["category"] == cat for c in cases) >= 2, cat
    for c in cases:
        art = p3_kit.load_article(c)
        assert contracts.validate_article(art) == [], c["id"]
        assert (evalkit.HERE / "p3_golden" / f"{c['id']}.json").exists()
        assert not any(w in n for n in (c["id"], c["article"]) for w in RESERVED)
        assert c["synthetic"] is True and c["not_a_legal_gold_label"] is True


def test_no_new_eval_file_name_uses_reserved_words():
    for p in list((evalkit.HERE / "p3_articles").iterdir()) + list((evalkit.HERE / "p3_golden").iterdir()) + [evalkit.HERE / "p3_kit.py", evalkit.HERE / "p3_scenarios.json", evalkit.HERE / "run_p3_evals.py"]:
        assert not any(w in p.name for w in RESERVED), p.name


def test_golden_files_hold_no_passage_text():
    """기준 출력은 자리표시만 갖고 실제 판결·결정의 문장을 옮기지 않는다."""
    for p in (evalkit.HERE / "p3_golden").iterdir():
        g = json.loads(p.read_text(encoding="utf-8"))
        assert "{{quote|" in json.dumps(g, ensure_ascii=False) or not g["findings"]


def test_all_pending_on_fixture_corpus():
    data = p3_kit.load_scenarios()
    outs = {c["id"]: json.loads((evalkit.HERE / "p3_golden" / f"{c['id']}.json").read_text(encoding="utf-8")) for c in data["cases"]}
    res = p3_kit.run_p3(corpus_mod.FixtureCorpus(), data, outputs=outs)
    assert res["counts"] == {"pass": 0, "fail": 0, "pending_corpus": len(data["cases"])}
    assert res["meta"]["corpus_is_fixture"] is True


# ------------------------------------------------------------------ 합성 색인 위에서 판정

@pytest.fixture()
def idx(tmp_path):
    data = tmp_path / "data"
    sl.build_ledger(data / "corpus")
    cs.build_index(data / "corpus" / "manifest.sqlite", data / "index", now=NOW)
    c = cs.open_current(data / "index")
    yield c, data
    c.close()


def article(pid_date="2026-10-05", genre="news", ec=None):
    return {"schema_version": "0.1", "article_id": "p3-t", "publication_date": pid_date,
            "blocks": [{"block_id": "title", "section": "title", "text": "시 예산 집행 비판"},
                       {"block_id": "p1", "section": "body", "text": "시는 올해 3억 원을 집행했다. 관계자는 “절차를 지켰다”고 말했다."}],
            "context": {"genre": genre, "election_context": ec, "verification_notes": [], "sensitive_material": False}}


def scenario(cat, passage, source, **expected):
    exp = {"edit": "none", "findings": "none_ok", "max_stars": 2}
    exp.update(expected)
    return {"id": f"p3_t_{cat}", "category": cat, "article": "unused.json", "expected": exp, "synthetic": True,
            "evidence_links": [{"passage_id": passage, "source_id": source, "role": "support"}]}


def finding(pid, sid, *, channels=("civil",), stars=1, mode="verify_first", span_text="시 예산 집행 비판", replacement=None, support=None, differences="선거 시기와 기준이 다르다"):
    f = {"finding_id": "finding-001", "span": {"block_id": "title", "start": 0, "end": len(span_text), "quote": span_text},
         "priority_stars": stars, "risk_channels": list(channels), "issue_category": "unsupported_assertion", "review_confidence": "medium",
         "reason_short": "표현의 확정성을 확인하세요.",
         "suggested_edit": {"mode": mode, "replacement": replacement, "related_edits": [], "required_verification": ["근거 확인"]},
         "evidence": [{"source_id": sid, "passage_id": pid}], "counter_considerations": ["공익 보도"]}
    out = {"findings": [f], "explanations": {"finding-001": {"support": support or "사례 passage", "differences": differences}}}
    return out


def run_one(corpus, sc, out, art=None):
    return p3_kit.run_p3(corpus, {"purpose": "t", "cases": [sc]}, outputs={sc["id"]: out}, articles={sc["id"]: art or article()})["cases"][0]


def names(row, status="fail"):
    return {c["name"] for c in row["checks"] if c["status"] == status}


def test_pass_path_and_materialized_quote(idx):
    c, _ = idx
    sc = scenario("opinion_commentary", sl.J2, "lawgo_prec:900002")
    out = finding(sl.J2, "lawgo_prec:900002", support="판결 일부: “{{quote|%s|0|20}}”" % sl.J2)
    row = run_one(c, sc, out)
    assert row["status"] == "pass", row["checks"]
    assert {"P3_links_in_index", "P3_cited_evidence_in_index", "P3_quotes_match_source", "P3_no_overedit", "P3_no_added_facts", "G2_direct_quotes_preserved"} <= {x["name"] for x in row["checks"]}


def test_missing_link_is_pending_corpus_not_fail(idx):
    c, _ = idx
    sc = scenario("opinion_commentary", "lawgo_prec:999:2099다1#c1", "lawgo_prec:999")
    row = run_one(c, sc, {"findings": [], "explanations": {}})
    assert row["status"] == "pending_corpus" and row["missing_passages"] == ["lawgo_prec:999:2099다1#c1"] and row["checks"] == []


def test_quote_not_in_source_fails(idx):
    c, _ = idx
    sc = scenario("opinion_commentary", sl.J2, "lawgo_prec:900002")
    row = run_one(c, sc, finding(sl.J2, "lawgo_prec:900002", support="판결 일부: “법원은 모든 의견 표명을 면책한다고 판시했다”"))
    assert row["status"] == "fail" and "P3_quotes_match_source" in names(row)


def test_quote_beyond_display_limit_is_not_accepted(idx):
    """재배포 제한 소스에서는 300자 안쪽만 인용 근거로 인정한다(J1 의 뒤쪽 문장은 전문에는 있으나 화면 인용 범위 밖)."""
    c, _ = idx
    p = c.get_passage("lawgo_prec:900001", sl.J1)
    tail = sl.TAIL_J1
    assert tail and tail in p["text"] and tail not in p["display_text"]
    sc = scenario("opinion_commentary", sl.J1, "lawgo_prec:900001")
    row = run_one(c, sc, finding(sl.J1, "lawgo_prec:900001", support=f"판결 일부: “{tail}”"))
    assert "P3_quotes_match_source" in names(row)


def test_overedit_on_no_edit_scenario_fails(idx):
    c, _ = idx
    sc = scenario("public_interest_no_edit", sl.J2, "lawgo_prec:900002")
    row = run_one(c, sc, finding(sl.J2, "lawgo_prec:900002", mode="replace", replacement="시 예산 집행 논란"))
    assert row["status"] == "fail" and "P3_no_overedit" in names(row)


def test_star_cap(idx):
    c, _ = idx
    sc = scenario("public_interest_no_edit", sl.J2, "lawgo_prec:900002", max_stars=1)
    row = run_one(c, sc, finding(sl.J2, "lawgo_prec:900002", stars=3))
    assert "P3_max_stars" in names(row)


def test_added_number_in_replacement_is_rejected_by_engine(idx):
    c, _ = idx
    sc = scenario("opinion_commentary", sl.J2, "lawgo_prec:900002", edit="allowed")
    row = run_one(c, sc, finding(sl.J2, "lawgo_prec:900002", mode="replace", replacement="시 예산 5억 원 집행 비판"))
    assert row["status"] == "fail" and "G1_engine_validator_accepts" in names(row)


def test_reply_channel_must_stay_separate(idx):
    c, _ = idx
    sc = scenario("reply_vs_truth", sl.J4, "lawgo_prec:900004", findings="required", channels_required=["reply"], channels_forbidden=["correction"])
    ok = run_one(c, sc, finding(sl.J4, "lawgo_prec:900004", channels=("reply",)))
    assert ok["status"] == "pass", ok["checks"]
    bad = run_one(c, sc, finding(sl.J4, "lawgo_prec:900004", channels=("reply", "correction")))
    assert {"P3_channels", "P3_reply_separated_from_truth_channels"} <= names(bad)
    falsity = finding(sl.J4, "lawgo_prec:900004", channels=("reply",))
    falsity["findings"][0]["reason_short"] = "보도는 허위입니다."
    assert "P3_no_falsity_or_illegality_assertion" in names(run_one(c, sc, falsity))
    none = run_one(c, sc, {"findings": [], "explanations": {}})
    assert "P3_has_finding" in names(none)


def test_opinion_blanket_immunity_is_caught(idx):
    c, _ = idx
    sc = scenario("opinion_commentary", sl.J2, "lawgo_prec:900002")
    out = finding(sl.J2, "lawgo_prec:900002")
    out["explanations"]["finding-001"]["support"] = "의견이므로 항상 면책된다."
    assert "P3_no_blanket_immunity_claim" in names(run_one(c, sc, out))


def test_election_scope_and_date(idx):
    c, _ = idx
    sc = scenario("election_standard", sl.I1, "iendc:7001", findings="required", channels_required=["iendc_election"])
    ok = run_one(c, sc, finding(sl.I1, "iendc:7001", channels=("iendc_election",)), art=article("2026-05-20", ec="지방선거 기간"))
    assert ok["status"] == "pass", ok["checks"]
    wrong_channel = run_one(c, sc, finding(sl.I1, "iendc:7001", channels=("iendc_election", "pac_election")), art=article("2026-05-20"))
    assert "P3_election_scope_and_date_consistent_or_abstain" in names(wrong_channel)
    no_scope = run_one(c, sc, finding(sl.I1, "iendc:7001", channels=("iendc_election",), differences="사실관계가 다르다"), art=article("2026-05-20"))
    assert "P3_election_difference_stated" in names(no_scope)
    # 결정일(2022-05-31)보다 이른 기사에는 그 결정을 쓸 수 없다: 엔진이 거부한다
    early = run_one(c, sc, finding(sl.I1, "iendc:7001", channels=("iendc_election",)), art=article("2022-01-01"))
    assert early["status"] == "fail" and "G1_engine_validator_accepts" in names(early)


def test_runner_main_writes_pending_for_shipped_scenarios_on_synthetic_index(idx, tmp_path):
    _, data = idx
    out = tmp_path / "out"
    rc = run_p3_evals.main(["--data-dir", str(data), "--out-dir", str(out), "--strict"])
    assert rc == 0
    res = json.loads((out / "eval_p3.json").read_text(encoding="utf-8"))
    assert res["meta"]["corpus_is_fixture"] is False and res["counts"]["pending_corpus"] == 8 and res["counts"]["fail"] == 0


def test_runner_rejects_missing_output_file(tmp_path):
    assert run_p3_evals.main(["--data-dir", str(tmp_path / "none"), "--outputs-dir", str(tmp_path / "empty"), "--out-dir", str(tmp_path / "o")]) == 2

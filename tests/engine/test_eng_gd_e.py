"""과제 E: G-D16 라벨 짝 규칙 정본, G-D21 guideline, G-D22 색인 규모 기준, G-D23 날짜 없는 근거, G-D24 counter 정밀도."""
import copy
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: F401,E402
import pdr_engine_paths as paths  # noqa: E402
from case_record_fixture import PAGES, base_record, mutated  # noqa: E402

from press_review import contracts, corpus as corpus_mod, corpus_sqlite as cs, mcp_server, review  # noqa: E402
from press_review.ingestion.manifest import Manifest  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
PAC_PROCEDURES = ("pac_settlement", "pac_decision", "pac_election_review", "iendc_review")


def errs(rec):
    return contracts.validate_case_record(rec, PAGES)


# ======================================================================= G-D16

def _label(authority, outcome_type, scope, sa=None):
    def fn(o):
        o["authority_type"], o["outcome_type"], o["outcome_scope"] = authority, outcome_type, scope
        o["settlement_or_adjudication"] = sa or ("withdrawal" if outcome_type == "withdrawal" else "adjudication")
    return mutated(fn)


@pytest.mark.parametrize("authority", PAC_PROCEDURES)
@pytest.mark.parametrize("outcome_type, scope", [("dismissal_procedural", "procedural"), ("dismissal_merits", "whole"),
                                                 ("dismissal_merits", "partial"), ("withdrawal", "unclear")])
def test_gd16_dismissal_and_withdrawal_are_allowed_in_every_pac_iendc_procedure(authority, outcome_type, scope):
    assert errs(_label(authority, outcome_type, scope)) == []


def test_gd16_court_judgment_cannot_carry_dismissal_or_procedural_scope():
    for t, sc in (("dismissal_merits", "whole"), ("dismissal_procedural", "procedural"), ("withdrawal", "whole")):
        assert any("cannot carry outcome_type" in e for e in errs(_label("judgment", t, sc))), (t, sc)
    out = errs(_label("judgment", "judgment", "procedural"))
    assert any("outcome_scope 'procedural'" in e for e in out), out
    out = errs(_label("judgment", "unknown", "procedural"))
    assert any("outcome_scope 'procedural'" in e for e in out), out
    assert errs(_label("judgment", "judgment", "whole")) == []
    assert errs(_label("judgment", "judgment", "partial")) == []


def test_gd16_scope_type_pair_rule_is_one_table_in_contracts():
    assert contracts.PROCEDURAL_SCOPE_OUTCOMES == {"dismissal_procedural", "withdrawal", "unknown"}
    assert contracts.AUTHORITY_OUTCOMES["judgment"] == {"judgment", "unknown"}
    for a in PAC_PROCEDURES:
        assert {"dismissal_procedural", "dismissal_merits", "withdrawal"} <= contracts.AUTHORITY_OUTCOMES[a]
    # 짝 규칙: 절차 각하는 procedural 범위, procedural 범위는 절차 각하·취하·unknown 만
    assert any("requires outcome_scope 'procedural'" in e for e in errs(_label("pac_decision", "dismissal_procedural", "whole")))
    assert any("outcome_scope 'procedural' cannot" in e for e in errs(_label("pac_decision", "decision", "procedural")))
    assert any("outcome_scope 'procedural' cannot" in e for e in errs(_label("pac_election_review", "correction", "procedural")))


# ======================================================================= G-D21

def _enum(schema, *path):
    node = schema
    for p in path:
        node = node[p]
    return node["enum"]


def test_gd21_guideline_in_both_schemas_and_every_copy():
    rev = contracts.load_schema("review")
    case = contracts.load_schema("case-record")
    ev = _enum(rev, "properties", "findings", "items", "properties", "evidence", "items", "properties", "authority_type")
    assert "guideline" in ev and "guideline" in _enum(case, "properties", "authority_type")
    for f in ("review", "case-record"):
        a = (paths.CONTRACTS / f"{f}.schema.json").read_bytes()
        b = (paths.ENGINE / "press_review" / "schemas" / f"{f}.schema.json").read_bytes()
        assert a == b, f
    assert "guideline" in corpus_mod.AUTHORITY_TYPES
    assert set(contracts.AUTHORITY_OUTCOMES) == set(_enum(case, "properties", "authority_type"))
    enum_in_mcp = [t for t in mcp_server._tool_defs() if t["name"] == "search_cases"][0]["inputSchema"]["properties"]["authority_types"]["items"]["enum"]
    assert "guideline" in enum_in_mcp


def test_gd21_guideline_case_record_validates_and_rejects_court_labels():
    ok = _label("guideline", "guideline", "whole", sa="adjudication")
    assert errs(ok) == []
    assert errs(_label("guideline", "unknown", "unclear", sa="adjudication")) == []
    assert any("cannot carry outcome_type" in e for e in errs(_label("guideline", "judgment", "whole")))
    assert any("cannot carry outcome_type" in e for e in errs(_label("guideline", "dismissal_merits", "whole")))


def test_gd21_guideline_evidence_passes_report_validation(tmp_path):
    fx = json.loads((paths.ENGINE / "press_review" / "fixtures" / "corpus.json").read_text(encoding="utf-8"))
    fx["passages"].append({**copy.deepcopy(fx["passages"][0]), "source_id": "fixture-guideline-001", "passage_id": "g1", "authority_type": "guideline",
                           "url": "https://example.invalid/guideline"})
    p = tmp_path / "corpus.json"
    p.write_text(json.dumps(fx, ensure_ascii=False), encoding="utf-8")
    c = corpus_mod.FixtureCorpus(p)
    got = c.search("합성 요약", authority_types=["guideline"], limit=3)
    assert got and all(h["authority_type"] == "guideline" for h in got)
    art = {"schema_version": "0.1", "article_id": "t-g", "publication_date": "2026-10-05",
           "blocks": [{"block_id": "title", "section": "title", "text": "선거 기사 제목입니다"},
                      {"block_id": "p1", "section": "body", "text": "선거 기사 본문입니다."}],
           "context": {"genre": "news", "election_context": None, "verification_notes": [], "sensitive_material": False}}
    f = {"finding_id": "finding-001", "span": {"block_id": "title", "start": 0, "end": 5, "quote": "선거 기사"}, "priority_stars": 1,
         "risk_channels": ["pac_election"], "issue_category": "election_fairness", "review_confidence": "medium", "reason_short": "기준을 확인하세요.",
         "suggested_edit": {"mode": "verify_first", "replacement": None, "related_edits": [], "required_verification": ["기준 확인"]},
         "evidence": [{"source_id": "fixture-guideline-001", "passage_id": "g1"}], "counter_considerations": []}
    res = review.build_report_ex(art, [f], {}, c, now=NOW)
    assert res.errors == [], res.errors
    assert res.report["findings"][0]["evidence"][0]["authority_type"] == "guideline"


def _guideline_index(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    m = Manifest(corpus / "manifest.sqlite")
    try:
        m.ensure_source("pac_standards_election_page", "pac_other", "합성 심의기준")
        doc = "pac_other:static16"
        m.upsert_document(doc, "pac_standards_election_page", canonical_url="https://www.pac.or.kr/kor/pages/?p=1&idx=16&m=view", title="합성 가이드북")
        m.set_document_status(doc, "structured")
        m.upsert_attachment(f"{doc}#1", doc, "https://www.pac.or.kr/_common/new_download_file.asp?sub_idx=16", kind="full")
        m.upsert_page(f"{doc}#1:p0003", doc, att_id=f"{doc}#1", pdf_page_no=3, method="pdftotext")
        m.upsert_chunk(f"{doc}#1:p0003#pc", doc, "선거기사 심의기준은 후보자 동등 취급을 요구한다. 합성 문장.", "pdf_page", "3", page_id=f"{doc}#1:p0003",
                       authority_type="guideline", decision_date=None)
    finally:
        m.close()
    res = cs.build_index(corpus / "manifest.sqlite", tmp_path / "index", now=NOW)
    assert res["status"] == "built", res
    return cs.open_current(tmp_path / "index")


def test_gd21_sqlite_index_keeps_and_filters_guideline(tmp_path):
    idx = _guideline_index(tmp_path)
    try:
        got = idx.search("심의기준 후보자", authority_types=["guideline"])
        assert got and got[0]["authority_type"] == "guideline"
        assert idx.search("심의기준 후보자", authority_types=["statute"]) == []
        assert idx.status()["by_authority_type"] == {"guideline": 1}
    finally:
        idx.close()


# ======================================================================= G-D22

def _big_ledger(tmp_path, n_judgment, n_decision, n_settlement=0, chunks_per_judgment=1):
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    m = Manifest(corpus / "manifest.sqlite")
    try:
        m.ensure_source("lawgo_prec_supreme", "law_go_kr", "합성")
        m.ensure_source("pac_adjustment_casebooks", "pac_cases", "합성")

        def add(kind, i, auth, fam, otype, k=1):
            doc = f"{kind}:{i:05d}"
            m.upsert_document(doc, fam, canonical_url=f"https://www.law.go.kr/LSW/precInfoP.do?precSeq={i}" if kind == "lawgo_prec"
                              else f"https://www.pac.or.kr/kor/pages/?p=207&idx={i}&m=view", title=f"합성 {i}")
            m.set_document_status(doc, "structured")
            cid = f"{doc}:c"
            m.upsert_case(cid, doc, {"case_id": cid, "doc_id": doc, "issue_tags": ["정정보도"], "outcome": None, "legal_reasoning": [],
                                     "institution": "supreme_court" if kind == "lawgo_prec" else "pac", "case_number": f"2020다{i}",
                                     "decision_date": "2021-01-01", "authority_type": auth, "outcome_type": otype}, verification_status="unverified")
            for j in range(k):
                m.upsert_chunk(f"{cid}#c{j}", doc, f"정정보도 청구 합성 사건 {i} 부분 {j}.", "text_range", f"{doc}#s006:{j}-{j + 9}", case_id=cid,
                               authority_type=auth, decision_date="2021-01-01")
        for i in range(n_judgment):
            add("lawgo_prec", i, "judgment", "lawgo_prec_supreme", "judgment", chunks_per_judgment)
        for i in range(n_decision):
            add("pac_cases", 10000 + i, "pac_decision", "pac_adjustment_casebooks", "decision")
        for i in range(n_settlement):
            add("pac_cases", 20000 + i, "pac_settlement", "pac_adjustment_casebooks", "settlement")
    finally:
        m.close()
    res = cs.build_index(corpus / "manifest.sqlite", tmp_path / "index", now=NOW)
    assert res["status"] == "built", res
    return cs.open_current(tmp_path / "index")


def _article(pub="2026-10-05"):
    return {"schema_version": "0.1", "article_id": "t-live", "publication_date": pub,
            "blocks": [{"block_id": "title", "section": "title", "text": "공적 인물 비판 기사"},
                       {"block_id": "p1", "section": "body", "text": "기자는 공적 인물의 행적을 비판하는 기사를 썼다."}],
            "context": {"genre": "news", "election_context": None, "verification_notes": [], "sensitive_material": False}}


def test_gd22_constants_and_pure_gate():
    assert contracts.CORE_MIN_CASES == 100
    assert contracts.CORE_GROUPS == (("judgment",), ("pac_settlement", "pac_decision"))
    f = contracts.core_coverage
    ok = f({"cases_by_authority_type": {"judgment": 100, "pac_decision": 100}})
    assert ok["no_actionable_findings_allowed"] is True and all(g["met"] for g in ok["groups"])
    ok2 = f({"cases_by_authority_type": {"judgment": 100, "pac_settlement": 100}})
    assert ok2["no_actionable_findings_allowed"] is True                    # pac_settlement 또는 pac_decision 중 하나면 된다
    short = f({"cases_by_authority_type": {"judgment": 99, "pac_decision": 500}})
    assert short["no_actionable_findings_allowed"] is False and short["groups"][0]["met"] is False and "judgment" in short["reason"]
    assert f({"cases_by_authority_type": {"judgment": 500, "pac_settlement": 60, "pac_decision": 60}})["no_actionable_findings_allowed"] is False   # 합산하지 않는다
    assert f({})["no_actionable_findings_allowed"] is False                  # 키 없는 옛 색인은 기준 미달
    assert f({"cases_by_authority_type": {"judgment": 100, "pac_decision": 100}, "is_fixture": True})["no_actionable_findings_allowed"] is False


def test_gd22_small_index_cannot_say_no_actionable(tmp_path):
    idx = _big_ledger(tmp_path, 3, 3)
    try:
        st = idx.status()
        cc = st["core_coverage"]
        assert cc["min_cases"] == 100 and cc["no_actionable_findings_allowed"] is False
        assert st["cases_by_authority_type"] == {"judgment": 3, "pac_decision": 3}
        assert any("100" in w and "no_actionable_findings" in w for w in st["warnings"])
        # 자동 판정: finding 이 없어도 partial
        res = review.build_report_ex(_article(), [], {}, idx, now=NOW)
        assert res.errors == [] and res.report["overall_status"] == "partial"
        assert any("no_actionable_findings" in n and "100" in n for n in res.report["coverage_notes"])
        # 호출자가 직접 지정해도 거부
        res = review.build_report_ex(_article(), [], {}, idx, now=NOW, overall_status="no_actionable_findings", coverage_notes=["범위 안내"])
        assert res.report is None and any("minimum" in e or "100" in e for e in res.errors), res.errors
        # corpus_status 도구 응답에도 실린다
        assert review.corpus_status(idx)["core_coverage"]["no_actionable_findings_allowed"] is False
    finally:
        idx.close()


def test_gd22_threshold_met_allows_no_actionable_and_counts_cases_not_chunks(tmp_path):
    idx = _big_ledger(tmp_path, 100, 100, chunks_per_judgment=2)
    try:
        st = idx.status()
        assert st["by_authority_type"]["judgment"] == 200 and st["cases_by_authority_type"]["judgment"] == 100
        assert st["core_coverage"]["no_actionable_findings_allowed"] is True
        res = review.build_report_ex(_article(), [], {}, idx, now=NOW)
        assert res.errors == [] and res.report["overall_status"] == "no_actionable_findings"
    finally:
        idx.close()
    # 판결 사건 99건(청크는 198개)은 청크 수로 100을 넘겨도 미달이다
    sub = tmp_path / "b"
    idx = _big_ledger(sub, 99, 100, chunks_per_judgment=2)
    try:
        assert idx.status()["by_authority_type"]["judgment"] == 198
        assert idx.status()["core_coverage"]["no_actionable_findings_allowed"] is False
        assert review.build_report_ex(_article(), [], {}, idx, now=NOW).report["overall_status"] == "partial"
    finally:
        idx.close()


def test_gd22_missing_group_blocks_even_when_other_is_large(tmp_path):
    idx = _big_ledger(tmp_path, 100, 0, n_settlement=0)
    try:
        cc = idx.status()["core_coverage"]
        assert cc["no_actionable_findings_allowed"] is False
        assert [g["met"] for g in cc["groups"]] == [True, False]
    finally:
        idx.close()


# ======================================================================= G-D23

def _date_ledger(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    m = Manifest(corpus / "manifest.sqlite")
    ids = {}
    try:
        m.ensure_source("pac_adjustment_casebooks", "pac_cases", "합성")

        def add(name, *, decision, pub, case_decision=None, authority="pac_settlement"):
            doc = f"pac_cases:7{len(ids):03d}"
            m.upsert_document(doc, "pac_adjustment_casebooks", canonical_url=f"https://www.pac.or.kr/kor/pages/?p=207&idx=7{len(ids):03d}&m=view",
                              title=name, publication_date=pub)
            m.set_document_status(doc, "structured")
            cid = f"{doc}:c"
            m.upsert_case(cid, doc, {"case_id": cid, "doc_id": doc, "issue_tags": ["정정보도"], "outcome": None, "legal_reasoning": [],
                                     "institution": "pac", "case_number": None, "decision_date": case_decision, "authority_type": authority,
                                     "outcome_type": "settlement"}, verification_status="unverified")
            pid = f"{cid}#c1"
            m.upsert_chunk(pid, doc, f"정정보도 조정 합성 사례 {name}.", "text_range", f"{doc}#s006:0-20", case_id=cid, authority_type=authority,
                           decision_date=decision)
            ids[name] = (doc, pid)
        add("dated", decision="2018-04-01", pub="2019-01-01")
        add("pubonly", decision=None, pub="2015-06-01")
        add("nodate", decision=None, pub=None)
        add("garbage", decision=None, pub="not-a-date")
    finally:
        m.close()
    res = cs.build_index(corpus / "manifest.sqlite", tmp_path / "index", now=NOW)
    assert res["status"] == "built", res
    return cs.open_current(tmp_path / "index"), ids


def test_gd23_reference_date_falls_back_to_publication_date_then_date_unknown(tmp_path):
    idx, ids = _date_ledger(tmp_path)
    try:
        def get(name):
            doc, pid = ids[name]
            return idx.get_passage(doc, pid)
        d = get("dated")
        assert (d["reference_date"], d["reference_date_basis"], d["date_unknown"]) == ("2018-04-01", "decision_date", False)
        assert d["decision_date"] == "2018-04-01"
        p = get("pubonly")
        assert p["decision_date"] is None
        assert (p["reference_date"], p["reference_date_basis"], p["date_unknown"]) == ("2015-06-01", "publication_date", False)
        for name in ("nodate", "garbage"):
            n = get(name)
            assert (n["reference_date"], n["reference_date_basis"], n["date_unknown"]) == (None, None, True), name
    finally:
        idx.close()


def test_gd23_as_of_uses_reference_date_and_exempts_date_unknown(tmp_path):
    idx, ids = _date_ledger(tmp_path)
    try:
        def pids(**kw):
            return {h["passage_id"] for h in idx.search("정정보도 조정", limit=10, **kw)}
        before_pub = pids(as_of="2015-05-31")
        assert ids["pubonly"][1] not in before_pub and ids["dated"][1] not in before_pub          # 발행일이 기준일 이후면 제외
        assert ids["nodate"][1] in before_pub and ids["garbage"][1] in before_pub                  # 날짜 없음은 필터에서 빠지고 date_unknown 으로 표시
        assert ids["pubonly"][1] in pids(as_of="2015-06-01")                                       # 경계일 포함
        assert ids["dated"][1] not in pids(as_of="2018-03-31") and ids["dated"][1] in pids(as_of="2018-04-01")
        flagged = {h["passage_id"]: h["date_unknown"] for h in idx.search("정정보도 조정", limit=10)}
        assert flagged == {ids["dated"][1]: False, ids["pubonly"][1]: False, ids["nodate"][1]: True, ids["garbage"][1]: True}
    finally:
        idx.close()


def _finding(pid, sid):
    return {"finding_id": "finding-001", "span": {"block_id": "title", "start": 0, "end": 9, "quote": "공적 인물 비판 기사"[:9]},
            "priority_stars": 2, "risk_channels": ["correction"], "issue_category": "unsupported_assertion", "review_confidence": "medium",
            "reason_short": "표현의 확정성을 확인하세요.", "suggested_edit": {"mode": "verify_first", "replacement": None, "related_edits": [],
                                                                     "required_verification": ["근거 확인"]},
            "evidence": [{"source_id": sid, "passage_id": pid}], "counter_considerations": ["공익 보도"]}


def test_gd23_report_may_cite_date_unknown_but_must_say_so(tmp_path):
    idx, ids = _date_ledger(tmp_path)
    try:
        doc, pid = ids["nodate"]
        res = review.build_report_ex(_article(), [_finding(pid, doc)], {}, idx, now=NOW)
        assert res.errors == [], res.errors
        notes = "\n".join(res.report["coverage_notes"])
        assert "date_unknown" in notes and pid in notes
        # 호출자가 만든 보고서를 그대로 검증할 때도 note 가 없으면 거부한다
        rep = copy.deepcopy(res.report)
        rep["coverage_notes"] = [n for n in rep["coverage_notes"] if "date_unknown" not in n]
        assert any("date_unknown" in e for e in contracts.validate_report(rep, _article(), idx))
        # 날짜가 있는 근거에는 note 가 붙지 않는다
        doc2, pid2 = ids["dated"]
        ok = review.build_report_ex(_article(), [_finding(pid2, doc2)], {}, idx, now=NOW)
        assert ok.errors == [] and "date_unknown" not in "\n".join(ok.report["coverage_notes"])
    finally:
        idx.close()


def test_gd23_evidence_dated_by_publication_after_legal_as_of_is_rejected(tmp_path):
    idx, ids = _date_ledger(tmp_path)
    try:
        doc, pid = ids["pubonly"]
        late = review.build_report_ex(_article("2015-01-01"), [_finding(pid, doc)], {}, idx, now=NOW)
        assert late.report is None and any("after legal_as_of" in e for e in late.errors), late.errors
        assert review.build_report_ex(_article("2015-06-01"), [_finding(pid, doc)], {}, idx, now=NOW).errors == []
    finally:
        idx.close()


def test_gd23_as_of_description_mentions_date_unknown():
    d = [t for t in mcp_server._tool_defs() if t["name"] == "search_cases"][0]["inputSchema"]["properties"]["as_of"]["description"]
    assert "date_unknown" in d and "발행일" in d


# ======================================================================= G-D24 (현행 승인, 특성 시험)

def test_gd24_counter_signal_needs_the_losing_party_in_the_sentence():
    f = cs.counter_signals
    assert f("judgment", [], "상고를 기각한다") == []                              # 처분 문구만 있는 판결: 신호 없음
    assert f("judgment", [], "상고를 모두 기각한다") == []
    assert f("judgment", [], "피고의 상고를 기각한다") == []                        # 패소 쪽이 언론사
    assert f("judgment", [], "원고의 나머지 청구를 기각한다") == []                  # 일부 승소
    assert f("judgment", [], "원고의 청구를 기각한다") == ["outcome_text:원고의청구를기각"]
    assert f("judgment", [], "피고의 상고를 기각하고 원고의 청구를 기각한다")            # 한 문장에 둘이면 청구 쪽 패소가 잡힌다
    assert f("judgment", [], "상고기각", ) == []


def test_gd24_disposition_only_judgment_is_counter_only_via_immunity_tag():
    plain = cs.counter_signals("judgment", ["명예훼손"], "상고를 기각한다")
    tagged = cs.counter_signals("judgment", ["상당한 이유"], "상고를 기각한다")
    assert plain == [] and tagged == ["tag:상당한 이유"]
    assert cs.counter_signals("judgment", ["공익성"], None) == ["tag:공익성"]
    assert cs.counter_signals("judgment", [], "상고를 기각한다") == []


def test_gd24_counter_ranking_puts_tagged_disposition_only_judgment_first(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    m = Manifest(corpus / "manifest.sqlite")
    ids = {}
    try:
        m.ensure_source("lawgo_prec_supreme", "law_go_kr", "합성")
        for i, (name, tags) in enumerate([("plain", ["정정보도"]), ("tagged", ["정정보도", "상당한 이유"])]):
            doc = f"lawgo_prec:81{i:04d}"
            m.upsert_document(doc, "lawgo_prec_supreme", canonical_url=f"https://www.law.go.kr/LSW/precInfoP.do?precSeq=81{i:04d}", title=name)
            m.set_document_status(doc, "structured")
            cid = f"{doc}:2020다{i + 1:05d}"
            m.upsert_case(cid, doc, {"case_id": cid, "doc_id": doc, "issue_tags": tags, "legal_reasoning": [], "institution": "supreme_court",
                                     "case_number": f"2020다{i + 1:05d}", "decision_date": "2021-01-01", "authority_type": "judgment",
                                     "outcome_type": "judgment", "outcome": {"text": "상고를 기각한다", "speaker": "court_majority"}},
                          verification_status="unverified")
            pid = f"{cid}#c1"
            m.upsert_chunk(pid, doc, "정정보도 청구 사건이다. " + ("정정보도 " * 3 if name == "plain" else ""), "text_range", f"{doc}#s006:0-40",
                           case_id=cid, authority_type="judgment", decision_date="2021-01-01")
            ids[name] = pid
    finally:
        m.close()
    cs.build_index(corpus / "manifest.sqlite", tmp_path / "index", now=NOW)
    idx = cs.open_current(tmp_path / "index")
    try:
        got = idx.search("정정보도 청구", stance="counter", limit=5)
        assert [h["passage_id"] for h in got][0] == ids["tagged"]
        assert got[0]["counter_signals"] == ["tag:상당한 이유"]
        plain = [h for h in got if h["passage_id"] == ids["plain"]][0]
        assert plain["counter_signals"] == []
    finally:
        idx.close()

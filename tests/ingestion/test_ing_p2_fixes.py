"""P2 게이트 결함 수정 회귀 시험(V-resume-G3, V-cost-nousage, V-locator-1, V-meta-*, V-label-2). 가짜 transport, 네트워크 없음."""
import json

import pytest

import pdr_ing_paths  # noqa: F401
from ds_fakes import FRI_PEAK, TEST_KEY, DSFake, chat_payload, good_case
from press_review.ingestion import structure as S
from press_review.providers import deepseek as ds
from test_ing_structure import go, handler_for, make_run


# ------------------------------------------------------------------ V-resume-G3
def test_window_cache_is_scoped_to_the_job_not_just_the_input(manifest, corpus):
    """flash -> pro -> flash 순서로 같은 대상을 돌려도 각 모델의 응답으로만 조립된다(다른 모델의 응답 파일을 재사용하지 않는다)."""
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None)
    fake.handler = handler_for(att, issue_tags=["플래시태그"])
    go(st, manifest)
    fake.handler = handler_for(att, issue_tags=["프로태그"])
    pro = S.Structurer(manifest, prov, corpus, model="deepseek-v4-pro")
    go(pro, manifest)
    cid = f"{att}:2001-조정-1"
    assert manifest.get_case(cid)["record"]["issue_tags"] == ["프로태그"] and fake.n_chat == 2
    flash = S.Structurer(manifest, prov, corpus, model="deepseek-v4-flash")
    res = go(flash, manifest)
    rec = manifest.get_case(cid)["record"]
    assert rec["structurer"]["model"] == "deepseek-flash" and rec["issue_tags"] == ["플래시태그"]       # G-D11: 구 이름은 정식 이름으로 기록된다
    assert res["windows_cached"] == 1 and fake.n_chat == 2         # flash 응답은 자기 작업 파일에 남아 있어 API 재호출 0건


def test_legacy_unit_file_is_accepted_only_for_its_own_job(manifest, corpus):
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None)
    fake.handler = handler_for(att, issue_tags=["플래시태그"])
    go(st, manifest)
    job = manifest.jobs()[0]
    import pathlib
    p = pathlib.Path(job["output_path"])
    legacy = p.with_name("w0000.json")
    p.replace(legacy)                                              # 예전 버전이 쓰던 위치와 이름
    res = go(S.Structurer(manifest, prov, corpus, model="deepseek-v4-flash", force=True), manifest)
    assert res["windows_cached"] == 1 and fake.n_chat == 1
    d = json.loads(legacy.read_text(encoding="utf-8"))
    d["job_id"] = "sj-someoneelse"                                 # 다른 작업의 파일이면 쓰지 않는다
    legacy.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    res2 = go(S.Structurer(manifest, prov, corpus, model="deepseek-v4-flash", force=True), manifest)
    assert res2["windows_cached"] == 0 and fake.n_chat == 2


# ------------------------------------------------------------------ V-cost-nousage
def test_missing_usage_cost_includes_prompt_upper_bound(manifest, corpus):
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None)
    d = good_case([f"{att}@p1", f"{att}@p2"])
    payload = chat_payload({"cases": [d]})
    del payload["usage"]
    fake.handler = lambda b, n: (200, payload, {})
    go(st, manifest)
    row = manifest.select("SELECT cost_usd FROM api_usage")[0]
    msgs = fake.bodies[0]["messages"]
    expected = prov.estimate_cost("deepseek-v4-flash", msgs, S.MAX_TOKENS)
    assert expected > prov.prices.upper_bound("deepseek-v4-flash", 0, S.MAX_TOKENS)
    assert row["cost_usd"] == pytest.approx(expected)


# ------------------------------------------------------------------ V-locator-1
def _rec_with_quotes(quotes):
    items = [{"text": "t", "speaker": "court_majority", "quote": q} for q in quotes]
    return {"outcome": None, "legal_provisions": [], "law_versions": [], "appeal_or_later_history": [],
            "available_original_article": {"status": "absent", "quote": None}, "available_corrected_article": {"status": "absent", "quote": None},
            "factual_background": [], "article_claims": [], "disputed_spans": [], "claims_or_requested_remedy": [], "legal_reasoning": items}


def test_text_range_locator_names_every_quoted_span():
    from press_review.ingestion import structure_records as sr
    pages = {"d#s001": "가" * 50 + "판결요지 문장이다." + "나" * 50, "d#s006": "다" * 30 + "상고를 모두 기각한다." + "라" * 30}
    def quote(pid, t):
        s = pages[pid].index(t)
        return {"quote": t, "page_id": pid, "start": s, "end": s + len(t)}
    rec = _rec_with_quotes([quote("d#s001", "판결요지 문장이다."), quote("d#s006", "상고를 모두 기각한다.")])
    ch = sr.chunk_for_case(rec, pages, {})
    assert ch["locator_kind"] == "text_range" and "기각" in ch["text"]
    spans = ch["locator_value"].split(";")
    assert spans == ["d#s001:50-60", "d#s006:30-42"]
    for sp in spans:                                   # 위치마다 원문에서 그대로 읽힌다
        pid, rng = sp.rsplit(":", 1)
        a, b = map(int, rng.split("-"))
        assert pages[pid][a:b] in ch["text"]


# ------------------------------------------------------------------ 판례(court_judgment) 시드
COURT_HEAD = "사건번호: 2022다284711, 284728\n선고일자: 2026. 2. 12.\n법원명: 대법원"
COURT_HOLD = "명예훼손에 의한 불법행위가 성립하려면 피해자가 특정되어야 한다."
COURT_BODY = ("【원심판결】 광주고법 2022. 9. 14. 선고 2018나24881, 24898 판결\n"
              "상고이유를 판단한다. 원심의 판단은 정당하다.\n")
COURT_ORDER = "상고를 모두 기각한다."
COURT_REFS = "[1] 대법원 2002. 12. 24. 선고 2000다14613 판결(공2003상, 425)"


def seed_court(manifest, corpus, *, case_no="2022다284711, 284728", decision_date="2026-02-12", with_body_case_no=True):
    from ds_fakes import seed
    pages = {"h": COURT_HEAD, "s1": COURT_HOLD, "s4": COURT_BODY, "s6": COURT_ORDER, "s3": COURT_REFS}
    doc, att, texts = seed(manifest, corpus, pages, source_id="lawgo_prec_supreme", family="law_go_kr", title="손해배상(기)")
    with manifest.transaction() as c:
        c.execute("UPDATE pages SET quality_json=? WHERE page_id=?", (json.dumps({"section_kind": "ref_cases"}), f"{att}@p5"))
    body = {"case_nos": ["2022다284711", "2022다284728"], **({"body_case_no": case_no} if with_body_case_no else {})}
    with manifest.transaction() as c:
        c.execute("UPDATE documents SET decision_date=?, extra_json=? WHERE doc_id=?",
                  (decision_date, json.dumps({"case_no": "2022다284711", "body": body}, ensure_ascii=False), doc))
    return doc, att, texts


def court_draft(att, **over):
    h, s1, s4, s6 = (f"{att}@p{i}" for i in (1, 2, 3, 4))
    d = {"case_number": "2018나24881, 24898", "decision_date": "2022-09-14", "publication_date": None, "institution": "supreme_court",
         "institution_name": "대법원", "authority_type": "judgment", "decision_type": "판결", "parties": [], "issue_tags": ["명예훼손"],
         "factual_background": [], "article_claims": [], "disputed_spans": [], "claims_or_requested_remedy": [],
         "outcome_type": "judgment", "outcome_scope": "whole",
         "outcome": {"text": "상고를 모두 기각했다.", "speaker": "court_majority", "quote": {"page_id": s6, "quote": COURT_ORDER}},
         "legal_reasoning": [{"text": "피해자가 특정되어야 한다.", "speaker": "court_majority", "quote": {"page_id": s1, "quote": COURT_HOLD}}],
         "legal_provisions": [], "law_versions": [], "settlement_or_adjudication": "adjudication",
         "original_article": {"status": "absent", "quote": None}, "corrected_article": {"status": "absent", "quote": None},
         "correction_pair_confidence": "not_applicable",
         "later_history": [{"relation": "appeal", "case_number": "2018나24881, 24898", "note": "원심은 광주고법 판결이다.",
                            "quote": {"page_id": s4, "quote": "광주고법 2022. 9. 14. 선고 2018나24881, 24898 판결"}}],
         "first_page_id": h, "starts_before_window": False, "continues_after_window": False}
    d.update(over)
    return d


# ------------------------------------------------------------------ V-meta-2 / V-meta-3
def test_court_case_number_and_date_come_from_the_ledger_not_the_cited_lower_court(manifest, corpus):
    doc, att, texts = seed_court(manifest, corpus)
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [court_draft(att)]}), {}))
    prov = ds.DeepSeekProvider(api_key=TEST_KEY, transport=fake, sleep=lambda s: None, now=lambda: FRI_PEAK, budget=ds.BudgetGuard(None))
    res = go(S.Structurer(manifest, prov, corpus), manifest)
    assert res["cases"] == 1 and res["cases_quarantined"] == 0, res
    case = manifest.select("SELECT * FROM cases")[0]
    rec = case["record_json"] and json.loads(case["record_json"])
    assert rec["case_number"] == "2022다284711, 284728" and rec["decision_date"] == "2026-02-12"        # 원심 번호·날짜가 아니다
    assert case["case_id"] == f"{att}:2022다284711,284728" and case["case_number"] == "2022다284711, 284728"
    assert "case_number:not_in_ledger" in rec["quote_check"]["dropped_fields"] and "decision_date:not_in_ledger" in rec["quote_check"]["dropped_fields"]
    ch = manifest.select("SELECT * FROM chunks")[0]
    assert ch["decision_date"] == "2026-02-12"                                                           # V-meta-3
    assert rec["appeal_or_later_history"][0]["case_number"] == "2018나24881, 24898"                      # 원심 번호는 이력에 남는다


def test_court_ledger_values_agree_with_model_values_without_notes_and_missing_ledger_keeps_model_values(manifest, corpus):
    doc, att, texts = seed_court(manifest, corpus)
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [court_draft(att, case_number="2022다284711", decision_date="2026-02-12")]}), {}))
    prov = ds.DeepSeekProvider(api_key=TEST_KEY, transport=fake, sleep=lambda s: None, now=lambda: FRI_PEAK, budget=ds.BudgetGuard(None))
    go(S.Structurer(manifest, prov, corpus), manifest)
    rec = json.loads(manifest.select("SELECT record_json FROM cases")[0]["record_json"])
    assert rec["case_number"] == "2022다284711, 284728"                       # 번호 일부만 적어도 장부 표기로 통일된다(case_id 안정)
    assert not [d for d in rec["quote_check"]["dropped_fields"] if d.startswith(("case_number", "decision_date"))]
    # 장부에 값이 없으면(예: 선고일 불명) 모델 값은 원문 대조를 거친 것만 남는다
    with manifest.transaction() as c:
        c.execute("UPDATE documents SET decision_date=NULL WHERE doc_id=?", (doc,))
    fake.handler = lambda b, n: (200, chat_payload({"cases": [court_draft(att, case_number="2022다284711", decision_date="2026-02-12")]}), {})
    go(S.Structurer(manifest, prov, corpus, force=True), manifest)
    rec = json.loads(manifest.select("SELECT record_json FROM cases")[0]["record_json"])
    assert rec["decision_date"] == "2026-02-12"                                # 머리 쪽 원문에 같은 날짜가 있다


def test_non_court_profile_ignores_document_level_dates(manifest, corpus):
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None)
    with manifest.transaction() as c:
        c.execute("UPDATE documents SET decision_date='2020-01-01' WHERE doc_id=?", (doc,))
    fake.handler = handler_for(att)
    go(st, manifest)
    assert manifest.select("SELECT decision_date FROM chunks")[0]["decision_date"] is None


# ------------------------------------------------------------------ V-label-2
# test_history_item_quoted_from_cited_precedents_is_a_label_violation 은 G-D3 로 바뀌었다: 사건 격리 대신 그 항목만 지운다(test_gd.py::test_gd3_*).


def test_lower_court_history_item_is_not_blocked(manifest, corpus):
    doc, att, texts = seed_court(manifest, corpus)
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [court_draft(att)]}), {}))
    prov = ds.DeepSeekProvider(api_key=TEST_KEY, transport=fake, sleep=lambda s: None, now=lambda: FRI_PEAK, budget=ds.BudgetGuard(None))
    res = go(S.Structurer(manifest, prov, corpus), manifest)
    assert res["cases_quarantined"] == 0 and res["chunks"] == 1


def test_court_chunk_decision_date_is_filled_when_the_model_returns_none(manifest, corpus):
    """V-meta-3: 모델이 선고일을 비워도 판례 chunk 의 decision_date 는 장부의 선고일이다(P3 검색 필터)."""
    doc, att, texts = seed_court(manifest, corpus)
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [court_draft(att, decision_date=None, case_number=None)]}), {}))
    prov = ds.DeepSeekProvider(api_key=TEST_KEY, transport=fake, sleep=lambda s: None, now=lambda: FRI_PEAK, budget=ds.BudgetGuard(None))
    go(S.Structurer(manifest, prov, corpus), manifest)
    ch = manifest.select("SELECT case_id, decision_date FROM chunks")[0]
    assert ch["decision_date"] == "2026-02-12" and ch["case_id"] == f"{att}:2022다284711,284728"


# ------------------------------------------------------------------ V-GD2-label / V-GD3-history (P3 수정)
def _run_court(manifest, corpus, draft_fn):
    doc, att, texts = seed_court(manifest, corpus)
    d = draft_fn(att)
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [d]}), {}))
    prov = ds.DeepSeekProvider(api_key=TEST_KEY, transport=fake, sleep=lambda s: None, now=lambda: FRI_PEAK, budget=ds.BudgetGuard(None))
    go(S.Structurer(manifest, prov, corpus), manifest)
    c = manifest.select("SELECT * FROM cases")[0]
    return c, json.loads(c["record_json"])


@pytest.mark.parametrize("bad", ["상고기각", "파기환송", "dismissal_merits", "decision"])
def test_court_outcome_type_outside_judgment_quarantines_instead_of_becoming_unknown(manifest, corpus, bad):
    c, rec = _run_court(manifest, corpus, lambda att: court_draft(att, outcome_type=bad))
    assert c["verification_status"] == "quarantined", (bad, rec["outcome_type"])


def test_court_outcome_type_unknown_with_grounded_outcome_or_judgment_stays_unverified(manifest, corpus):
    c, rec = _run_court(manifest, corpus, lambda att: court_draft(att))
    assert c["verification_status"] == "unverified" and rec["outcome_type"] == "judgment"


def test_cited_precedent_without_quote_is_removed_from_history_when_only_on_ref_page(manifest, corpus):
    def mk(att):
        d = court_draft(att)
        d["later_history"].append({"relation": "related_case", "case_number": "2000다14613", "note": "참조판례이다.", "quote": None})
        return d
    c, rec = _run_court(manifest, corpus, mk)
    assert c["verification_status"] == "unverified"
    assert [h["case_number"] for h in rec["appeal_or_later_history"]] == ["2018나24881, 24898"]
    assert any(x.startswith("appeal_or_later_history[") and x.endswith("cited_precedent_removed") for x in rec["quote_check"]["dropped_fields"])

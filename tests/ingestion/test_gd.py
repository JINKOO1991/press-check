"""P2 게이트 후 결정 G-D1~G-D11(docs/P2_DESIGN.md §9) 시험. 가짜 transport·가짜 실행기, 네트워크 없음. 합성 텍스트이며 실제 사건이 아니다.

테스트 이름은 ``test_gd<번호>_...`` 이고 번호 뒤에 밑줄이 붙는다(``-k "gd1_"`` 이 gd10·gd11 을 끌어오지 않게).
"""
import io
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

import pdr_ing_paths  # noqa: F401
from conftest import FakeClock, FakeTransport
from ds_fakes import FRI_PEAK, TEST_KEY, DSFake, chat_payload, good_case, q, seed
from ext_helpers import FakeRunner, make_pdf, page_text, place_raw
from ext_helpers import seed as ext_seed
from press_review.ingestion import __main__ as cli
from press_review.ingestion import extract as ext
from press_review.ingestion import structure as S
from press_review.ingestion import structure_records as sr
from press_review.ingestion.extract.tools import ToolResult
from press_review.ingestion.manifest import Manifest
from press_review.ingestion.sources import iendc
from press_review.ingestion.sources import lawapi as L
from press_review.providers import deepseek as ds
from test_ing_extract_run import env  # noqa: F401  (fixture)
from test_ing_p2_fixes import court_draft, seed_court
from test_ing_structure import go, handler_for, make_run, pids_of
from test_src_iendc import detail_html, routes as iendc_routes, run as iendc_run
from test_src_lawapi_body import Server, doc as law_doc, run_cli as law_run, seed as law_seed, prec_json


def prov_for(fake, **kw):
    return ds.DeepSeekProvider(api_key=TEST_KEY, transport=fake, sleep=lambda s: None, now=lambda: FRI_PEAK, budget=ds.BudgetGuard(None), **kw)


def record_of(manifest, case_id):
    return manifest.get_case(case_id)["record"]


# =====================================================================================================================
# G-D1  P-T1 정의: 언론어에 출판
# =====================================================================================================================
def test_gd1_publishing_is_a_media_term_for_the_p_t1_rule():
    assert "출판" in L.MEDIA_TERMS
    assert L.compute_tier(["출판", "명예훼손"]) == "P-T1" and L.compute_tier(["출판", "초상권"]) == "P-T1"
    assert L.compute_tier(["출판"]) == "P-T2"                       # 인격권어 없이 언론어만이면 T2 그대로


def test_gd1_prefilter_media_pattern_and_filter_version_bump():
    r = L.relevance_prefilter("출판물에 의한 명예훼손이 문제 되었다.")
    assert r["passed"] and r["media_terms"] == ["출판"] and r["version"] == L.FILTER_VERSION == "prefilter-3"


def test_gd1_stale_prefilter_is_recomputed_from_stored_raw_without_network(corpus):
    body = "【주    문】<br/>상고를 기각한다.<br/>【이    유】출판물에 의한 명예훼손이 문제 되었다."
    srv = Server({"7": prec_json(7, content=body, case_no="2020다1")})
    tr = srv.transport()
    m = Manifest(corpus / "manifest.sqlite")
    law_seed(m, 7, "2020다1", "20210722", ["의견"])                     # 적중 검색어는 일반어뿐: 순위 T3, 본문 사전 필터로만 갈린다
    m.close()
    code, _o = law_run(["--tier", "T3", "--no-expand"], corpus, tr)
    assert code == 0
    _d, extra, _p = law_doc(corpus, "lawgo_prec:7")
    assert extra["body"]["prefilter"]["passed"] is True and extra["body"]["prefilter"]["version"] == "prefilter-3"
    m = Manifest(corpus / "manifest.sqlite")                         # 옛 필터 결과를 흉내 낸다
    L._set_extra(m, "lawgo_prec:7", "body.prefilter", {"passed": False, "media_terms": [], "issue_terms": ["명예훼손"], "version": "prefilter-1"})
    m.close()
    n = len(srv.service_calls)
    code, _o = law_run(["--tier", "T3", "--no-expand"], corpus, tr)
    _d, extra, _p = law_doc(corpus, "lawgo_prec:7")
    assert code == 0 and len(srv.service_calls) == n                  # 저장된 raw 로 다시 계산: 네트워크 0건
    pf = extra["body"]["prefilter"]
    assert pf["version"] == L.FILTER_VERSION and pf["passed"] is True and pf["media_terms"] == ["출판"]
    assert L.is_structure_candidate(extra)


# =====================================================================================================================
# G-D2  법원 판결의 결과 라벨은 judgment
# =====================================================================================================================
def _court_run(manifest, corpus, **over):
    doc, att, texts = seed_court(manifest, corpus)
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [court_draft(att, **over)]}), {}))
    res = go(S.Structurer(manifest, prov_for(fake), corpus), manifest)
    return doc, att, texts, fake, res


def test_gd2_court_judgment_with_dismissal_label_is_quarantined_not_repaired(manifest, corpus):
    doc, att, texts, fake, res = _court_run(manifest, corpus, outcome_type="dismissal_merits")
    case = manifest.select("SELECT * FROM cases")[0]
    rec = json.loads(case["record_json"])
    assert case["verification_status"] == "quarantined" and res["cases_quarantined"] == 1 and res["chunks"] == 0
    assert rec["outcome_type"] == "dismissal_merits"                  # judgment 로 바꿔 넣지 않는다
    detail = json.load(open(next((corpus / "structured").rglob("assembled.json")), encoding="utf-8"))
    assert any(e.startswith("label_guard") and "court" in e and "judgment" in e for e in detail["cases_detail"][0]["errors"])


def test_gd2_court_judgment_keeps_the_printed_disposition_in_decision_type(manifest, corpus):
    doc, att, texts, fake, res = _court_run(manifest, corpus, decision_type="상고기각")
    rec = json.loads(manifest.select("SELECT record_json FROM cases")[0]["record_json"])
    assert res["cases_quarantined"] == 0 and rec["outcome_type"] == "judgment" and rec["decision_type"] == "상고기각"


def test_gd2_label_guard_unit_only_constrains_court_authority():
    out = {"text": "상고 각하", "speaker": "court_majority", "quote": {"quote": "상고를 각하한다."}}
    court = {"authority_type": "judgment", "outcome_type": "dismissal_procedural", "outcome": out}
    assert any("judgment" in e for e in sr.label_guard_errors(court))
    assert not [e for e in sr.label_guard_errors(dict(court, outcome_type="judgment")) if "court" in e]
    assert not [e for e in sr.label_guard_errors(dict(court, outcome_type="unknown", outcome=None))]
    pac = {"authority_type": "pac_decision", "outcome_type": "dismissal_procedural", "outcome": dict(out, speaker="institution")}
    assert not [e for e in sr.label_guard_errors(pac) if "court" in e]         # PAC·IENDC 절차의 각하는 dismissal_* 그대로


def test_gd2_prompts_state_the_court_exception_and_versions_moved():
    court = (S.PROMPTS_DIR / "court_judgment.md").read_text(encoding="utf-8")
    common = (S.PROMPTS_DIR / "common.md").read_text(encoding="utf-8")
    system = S.load_prompt(sr.PROFILES["court_judgment"]).system
    assert "outcome_type is always `judgment`" in system and "Never use `dismissal_" in system
    assert "decision_type" in court and "상고기각" in court and "never `procedural`" in court
    assert "except for court judgments" in common
    assert court.startswith("version: 2026-10-09.3") and common.startswith("version: 2026-10-09.3")  # G-D16: 공통 프롬프트 개정


# =====================================================================================================================
# G-D3  참조판례가 이력에 들어가면 그 항목만 지운다
# =====================================================================================================================
def test_gd3_cited_precedent_item_is_removed_and_the_case_survives(manifest, corpus):
    doc, att, texts = seed_court(manifest, corpus)
    refs_page = f"{att}@p5"
    bad = court_draft(att)
    bad["later_history"].append({"relation": "related_case", "case_number": "2000다14613", "note": "참조판례이다.",
                                 "quote": {"page_id": refs_page, "quote": "대법원 2002. 12. 24. 선고 2000다14613 판결"}})
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": [bad]}), {}))
    res = go(S.Structurer(manifest, prov_for(fake), corpus), manifest)
    assert res["cases_quarantined"] == 0 and res["chunks"] == 1, res
    rec = json.loads(manifest.select("SELECT record_json FROM cases")[0]["record_json"])
    assert [h["case_number"] for h in rec["appeal_or_later_history"]] == ["2018나24881, 24898"]      # 원심 항목만 남는다
    assert "appeal_or_later_history[1]:cited_precedent_removed" in rec["quote_check"]["dropped_fields"]
    assert refs_page not in rec["supporting_passage_ids"]
    assert rec["quote_check"]["matched"] == rec["quote_check"]["total"]                               # 실패율 계산에는 들어가지 않는다
    detail = json.load(open(next((corpus / "structured").rglob("assembled.json")), encoding="utf-8"))
    assert {"field": "appeal_or_later_history[1]", "reason": "cited_precedent_removed"} in detail["cases_detail"][0]["dropped"]
    assert not [e for e in detail["cases_detail"][0]["errors"] if e.startswith("label_guard")]


# =====================================================================================================================
# G-D4  IENDC 표 PDF 는 kordoc 쪽별 텍스트가 기본, pdftotext 는 폴백
# =====================================================================================================================
IENDC_SRC, IENDC_FAM = "iendc_review_results", "iendc"
KD_TEXT = ["인터넷선거보도심의위원회 조치내역 첫 번째 쪽의 표 본문입니다 가나다라마바사",
           "인터넷선거보도심의위원회 조치내역 두 번째 쪽의 표 본문입니다 가나다라마바사",
           "인터넷선거보도심의위원회 조치내역 세 번째 쪽의 표 본문입니다 가나다라마바사"]


class KdRunner(FakeRunner):
    """kordoc 을 ``-p N`` 쪽별로 부른다. spec: kd_pages(쪽별 markdown), kd_fail({쪽}), kd_wrong_page({쪽: 잘못된 pageNumber})."""

    def run(self, argv, *, timeout):
        if self.kordoc and argv[0] == self.kordoc:
            self.calls.append(list(argv))
            spec = self._spec(argv[1])
            if "-p" not in argv:
                n = None
            else:
                n = int(argv[argv.index("-p") + 1])
            if n in spec.get("kd_fail", ()):
                return ToolResult(1, b"", b"boom")
            if n is None:       # -p 없이 부르면 kordoc 은 PDF 를 한 쪽으로 합쳐 낸다(실측): 이 시험은 그것을 쓰면 실패하게 한다
                data = {"success": True, "metadata": {"pageMode": "layout"}, "pages": [{"pageNumber": 1, "markdown": "\n".join(spec["kd_pages"])}]}
            else:
                no = spec.get("kd_wrong_page", {}).get(n, n)
                data = {"success": True, "metadata": {"pageMode": "layout"}, "pages": [{"pageNumber": no, "markdown": spec["kd_pages"][n - 1]}]}
            return ToolResult(0, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        return super().run(argv, timeout=timeout)


def _iendc_pdf(env, n=3, **spec_over):
    doc, (a1,) = ext_seed(env.m, "200", source=IENDC_SRC, family=IENDC_FAM, atts=[(1, "https://www.iendc.go.kr/images/pdf/x/loader.do", "full", None, "application/pdf", "PDF")])
    blob = make_pdf([f"tag page {i + 1}" for i in range(n)])
    spec = {"pages": [page_text(i + 1, i + 1, "T") for i in range(n)], "kd_pages": KD_TEXT[:n]}
    spec.update(spec_over)
    place_raw(env.m, env.corpus, a1, blob, family=IENDC_FAM)
    return doc, a1, blob, spec


def test_gd4_iendc_pdf_pages_come_from_kordoc_one_call_per_page(env):
    doc, a1, blob, spec = _iendc_pdf(env)
    r = KdRunner({blob: spec})
    code, res = env.run(r)
    assert code == 0 and res["result"]["outcomes"] == {"extracted": 1}
    rows = env.m.pages_of(att_id=a1)
    assert [p["method"] for p in rows] == ["kordoc"] * 3 and [p["pdf_page_no"] for p in rows] == [1, 2, 3]
    assert [(env.corpus / p["text_path"]).read_text(encoding="utf-8") for p in rows] == KD_TEXT
    ps = [a[a.index("-p") + 1] for a in r.calls if a[0] == "/fake/kordoc"]
    assert ps == ["1", "2", "3"] and r.count("pdftotext") == 0
    assert env.m.select("SELECT extractor_version FROM attachments WHERE att_id=?", (a1,))[0]["extractor_version"] == ext.EXTRACTOR_VERSION


def test_gd4_page_where_kordoc_fails_or_misnumbers_falls_back_to_pdftotext_with_a_reason(env):
    doc, a1, blob, spec = _iendc_pdf(env, kd_fail={2}, kd_wrong_page={3: 1})
    r = KdRunner({blob: spec})
    code, res = env.run(r)
    rows = env.m.pages_of(att_id=a1)
    assert code == 0 and [p["method"] for p in rows] == ["kordoc", "pdftotext", "pdftotext"]
    qj = [json.loads(p["quality_json"]) for p in rows]
    assert "extractor_fallback" not in qj[0] and qj[1]["extractor_fallback"].startswith("kordoc_") and qj[2]["extractor_fallback"] == "kordoc_page_mismatch"
    assert (env.corpus / rows[1]["text_path"]).read_text(encoding="utf-8") == spec["pages"][1].strip("\n")


def test_gd4_without_kordoc_everything_falls_back_and_a_later_run_with_kordoc_redoes_it(env):
    doc, a1, blob, spec = _iendc_pdf(env)
    r0 = KdRunner({blob: spec}, kordoc=None)
    code, res = env.run(r0)
    rows = env.m.pages_of(att_id=a1)
    assert res["result"]["outcomes"] == {"extracted": 1} and [p["method"] for p in rows] == ["pdftotext"] * 3
    assert all(json.loads(p["quality_json"])["extractor_fallback"] == "kordoc_unavailable" for p in rows)
    note = json.loads(env.m.get_document(doc)["extra_json"])["p2_extract"]
    assert note["pdf_extractor"] == "pdftotext_fallback:kordoc_unavailable"                       # 숨기지 않는다
    r1 = KdRunner({blob: spec})
    _, res1 = env.run(r1)
    assert res1["result"]["outcomes"] == {"extracted": 1}                                           # 폴백 결과는 완료로 굳지 않는다
    assert [p["method"] for p in env.m.pages_of(att_id=a1)] == ["kordoc"] * 3
    _, res2 = env.run(r1)
    assert res2["result"]["outcomes"] == {"skipped": 1}


def test_gd4_extractor_choice_is_a_per_source_constant_and_pac_stays_on_pdftotext(env):
    assert ext.PDF_EXTRACTOR_BY_SOURCE[IENDC_SRC] == "kordoc" and ext.pdf_extractor_for({"source_id": "pac_board_committee_rules"}) == "pdftotext_layout"
    doc, (a1,) = ext_seed(env.m, "300", atts=[(1, "https://www.pac.or.kr/files/Z.pdf", "full", None, "application/pdf", "PDF")])
    blob = make_pdf(["pac page 1", "pac page 2"])
    spec = {"pages": [page_text(1, 1, "P"), page_text(2, 2, "P")], "kd_pages": KD_TEXT[:2]}
    place_raw(env.m, env.corpus, a1, blob)
    r = KdRunner({blob: spec})
    env.run(r)
    assert [p["method"] for p in env.m.pages_of(att_id=a1)] == ["pdftotext", "pdftotext"] and not [a for a in r.calls if a[0] == "/fake/kordoc"]


# =====================================================================================================================
# G-D5  IENDC 병합 칸 인용
# =====================================================================================================================
IENDC_PAGE = ("번호 선거명 언론사명 보도제목 위반내용 조치\n"
              "1 2026 제9회 지선 열린시민뉴스 시민단체 예비후보 비리 의혹 고발 보도 위반내용 후보자 비방 조치 주의\n"
              "2 가상일보 예비후보 재산 의혹 단독 보도 위반내용 사실 확인 부족 조치 경고\n")        # 병합 칸(선거명)은 첫 행에만 인쇄된다. 행마다 되풀이되면 인용은 각 행의 칸에 닿는다(V-FID-01)
MERGED = "2026 제9회 지선"


def _row_case(page, row_no, title, violation, measure, *, with_shared=True, extra_facts=()):
    fb = []
    if with_shared:
        fb.append({"text": "선거명", "speaker": "record", "quote": q(page, MERGED)})
    fb.append({"text": "보도제목", "speaker": "record", "quote": q(page, title)})
    fb += [{"text": f"사실 {i}", "speaker": "record", "quote": q(page, t)} for i, t in enumerate(extra_facts)]
    return {"case_number": None, "institution": "iendc", "authority_type": "iendc_review", "outcome_type": "decision", "outcome_scope": "whole",
            "settlement_or_adjudication": "adjudication", "first_page_id": page, "factual_background": fb,
            "disputed_spans": [{"text": "위반내용", "speaker": "article", "quote": q(page, violation)}],
            "outcome": {"text": measure, "speaker": "institution", "quote": q(page, MERGED)},
            "legal_reasoning": [{"text": "심의", "speaker": "institution", "quote": q(page, "번호 선거명 언론사명 보도제목 위반내용 조치")}]}


def _iendc_run(manifest, corpus, cases_fn):
    doc, att, texts = seed(manifest, corpus, {"p1": IENDC_PAGE}, source_id=IENDC_SRC, family=IENDC_FAM, title="심의결과")
    page = f"{att}@p1"
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": cases_fn(page)}), {}))
    res = go(S.Structurer(manifest, prov_for(fake), corpus), manifest)
    return doc, att, page, fake, res


def test_gd5_merged_cell_quote_shared_by_two_cases_is_dropped_from_fact_fields(manifest, corpus):
    def cases(page):
        return [_row_case(page, 1, "시민단체 예비후보 비리 의혹 고발 보도", "후보자 비방", "주의",
                          extra_facts=("열린시민뉴스 시민단체", "예비후보 비리 의혹")),
                _row_case(page, 2, "예비후보 재산 의혹 단독 보도", "사실 확인 부족", "경고", extra_facts=("가상일보 예비후보", "재산 의혹 단독"))]
    doc, att, page, fake, res = _iendc_run(manifest, corpus, cases)
    recs = [json.loads(r["record_json"]) for r in manifest.select("SELECT record_json FROM cases ORDER BY case_id")]
    assert len(recs) == 2 and res["cases_quarantined"] == 0, res
    for rec in recs:
        assert all(it["quote"]["quote"] != MERGED for it in rec["factual_background"])           # 두 사건이 같은 구간을 사실 인용으로 썼다
        assert "factual_background[0]:shared_cell_quote" in rec["quote_check"]["dropped_fields"]
        assert rec["outcome"] is not None and rec["outcome"]["quote"]["quote"] == MERGED          # 사실 인용이 아닌 결론 필드는 건드리지 않는다
        assert len(rec["factual_background"]) >= 3


def test_gd5_quote_used_by_only_one_case_is_kept(manifest, corpus):
    def cases(page):
        return [_row_case(page, 1, "시민단체 예비후보 비리 의혹 고발 보도", "후보자 비방", "주의", extra_facts=("열린시민뉴스 시민단체",)),
                _row_case(page, 2, "예비후보 재산 의혹 단독 보도", "사실 확인 부족", "경고", with_shared=False, extra_facts=("가상일보 예비후보",))]
    doc, att, page, fake, res = _iendc_run(manifest, corpus, cases)
    recs = {r["case_id"]: json.loads(r["record_json"]) for r in manifest.select("SELECT case_id, record_json FROM cases")}
    assert any(it["quote"]["quote"] == MERGED for rec in recs.values() for it in rec["factual_background"])      # 한 사건만 쓴 칸은 남는다


def test_gd5_guard_is_scoped_to_the_iendc_profile(manifest, corpus):
    doc, att, texts = seed(manifest, corpus, {"p1": PAGE1_SHARED})
    p1 = f"{att}@p1"
    a, b = (good_case([p1, p1], case_number=n, first_page_id=p1) for n in ("2001-조정-1", "2001-조정-2"))
    for c in (a, b):
        c["outcome"] = None
        c["outcome_type"], c["outcome_scope"] = "unknown", "unclear"
        c["legal_reasoning"] = []
        c["original_article"] = {"status": "absent", "quote": None}
        c["factual_background"] = [{"text": "공통", "speaker": "claimant", "quote": q(p1, "신청인 A는 가상일보의 기사가 사실과 다르다며 정정보도를 청구하였다.")}]
    fake = DSFake(lambda bb, n: (200, chat_payload({"cases": [a, b]}), {}))
    go(S.Structurer(manifest, prov_for(fake), corpus), manifest)
    recs = [json.loads(r["record_json"]) for r in manifest.select("SELECT record_json FROM cases")]
    assert len(recs) == 2 and all(len(r["factual_background"]) == 1 for r in recs)               # PAC 프로필은 같은 구간 인용을 그대로 둔다


PAGE1_SHARED = ("사건 2001-조정-1, 2001-조정-2\n"
                "신청인 A는 가상일보의 기사가 사실과 다르다며 정정보도를 청구하였다.\n"
                "기사 구절: 가상시 시장이 공사비를 빼돌렸다는 의혹이 제기됐다.\n")


def test_gd5_prompt_tells_the_model_to_quote_row_specific_cells():
    p = S.load_prompt(sr.PROFILES["iendc_table"]).system
    assert "row-specific cell" in p and "merged" in p
    assert (S.PROMPTS_DIR / "iendc_table.md").read_text(encoding="utf-8").startswith("version: 2026-10-09.3")


# =====================================================================================================================
# G-D6  프롬프트 없는 순위 A 소스
# =====================================================================================================================
RESEARCH_PAGE = ("판결 소개\n대법원 2011다12345 판결(2011. 5. 4. 선고)은 언론사의 정정보도 의무를 다루었다.\n"
                 "법원은 보도 내용이 진실에 부합하지 않는다고 보았다.\n"
                 "아래 판결 2012다999 판결(2012. 1. 5. 선고)은 사생활 침해를 다루었다.\n")


def _research_cases(page):
    def one(no, date, intro, held):
        return {"case_number": no, "decision_date": date, "institution": "pac", "authority_type": "research", "decision_type": "판결",
                "issue_tags": ["정정보도"], "outcome_type": "research", "outcome_scope": "whole", "settlement_or_adjudication": "none",
                "first_page_id": page,
                "factual_background": [{"text": "소개된 판결", "speaker": "record", "quote": q(page, intro)}],
                "outcome": {"text": "법원이 판단한 내용으로 소개됐다.", "speaker": "institution", "quote": q(page, held)}}
    return [one("2011다12345", "2011-05-04", "대법원 2011다12345 판결(2011. 5. 4. 선고)은 언론사의 정정보도 의무를 다루었다.", "법원은 보도 내용이 진실에 부합하지 않는다고 보았다."),
            one("2012다999", "2012-01-05", "아래 판결 2012다999 판결(2012. 1. 5. 선고)은 사생활 침해를 다루었다.", "사생활 침해를 다루었다")]


def _law_ledger(manifest):
    manifest.ensure_source("lawgo_prec_supreme", "law_go_kr", "판례", "https://www.law.go.kr/x")
    manifest.upsert_document("lawgo_prec:555", "lawgo_prec_supreme", title="손해배상", decision_date="2011-05-04", extra={"case_no": "2011다12345"})


def test_gd6_judgment_analysis_source_gets_a_research_profile_and_ledger_links(manifest, corpus):
    assert sr.SOURCE_PROFILE["pac_research_judgment_analysis"] == "pac_judgment_cases"
    prof = sr.PROFILES["pac_judgment_cases"]
    assert prof.authority_types == ("research",) and "judgment_cases" in S.load_prompt(prof).version
    _law_ledger(manifest)
    doc, att, texts = seed(manifest, corpus, {"p1": RESEARCH_PAGE}, source_id="pac_research_judgment_analysis", family="pac_other", title="판결분석")
    page = f"{att}@p1"
    fake = DSFake(lambda b, n: (200, chat_payload({"cases": _research_cases(page)}), {}))
    res = go(S.Structurer(manifest, prov_for(fake), corpus), manifest)
    assert res["cases"] == 2 and res["cases_quarantined"] == 0, res
    r1 = record_of(manifest, f"{att}:2011다12345")
    assert r1["authority_type"] == "research" and r1["outcome_type"] == "research" and r1["case_number"] == "2011다12345"
    links = manifest.case_links_of(f"{att}:2011다12345")
    assert [(x["rel"], x["target_doc_id"], x["status"]) for x in links] == [("introduces_judgment", "lawgo_prec:555", "linked")]
    miss = manifest.case_links_of(f"{att}:2012다999")
    assert [(x["target_doc_id"], x["status"]) for x in miss] == [("", "not_in_ledger")]            # 장부에 없으면 없다고 적는다(추측 연결 없음)
    ch = manifest.select("SELECT authority_type FROM chunks")
    assert {c["authority_type"] for c in ch} == {"research"}


def test_gd6_link_requires_the_decision_date_to_agree_when_both_are_known(manifest):
    from press_review.ingestion import links
    _law_ledger(manifest)
    assert links.find_ledger_docs(manifest, "2011다12345", "2011-05-04") == (["lawgo_prec:555"], "linked")
    assert links.find_ledger_docs(manifest, "2011다12345", None) == (["lawgo_prec:555"], "linked")
    assert links.find_ledger_docs(manifest, "2011다12345", "2012-01-01") == ([], "date_mismatch")
    assert links.find_ledger_docs(manifest, "2011다99999", None) == ([], "not_in_ledger")


@pytest.mark.parametrize("source,authority", [("pac_standards_correction_page", "guideline"), ("pac_standards_election_page", "guideline"),
                                              ("pac_board_committee_rules", "statute")])
def test_gd6_standards_and_rules_become_chunks_only_and_never_reach_the_provider(manifest, corpus, source, authority):
    pages = {"p1": "시정권고 기준 제1조 언론은 사실을 정확하게 보도하여야 한다.\n", "p2": "제2조 언론은 개인의 사생활을 존중하여야 한다.\n"}
    doc, att, texts = seed(manifest, corpus, pages, source_id=source, family="pac_other", title="기준")
    fake = DSFake(lambda b, n: pytest.fail("provider must not be called"))
    st = S.Structurer(manifest, prov_for(fake), corpus)
    res = go(st, manifest)
    assert res["chunk_only"] == 1 and res["cases"] == 0 and res["chunks"] == 2 and res["skipped_no_profile"] == 0
    assert fake.calls == [] and manifest.select("SELECT * FROM cases") == []                          # /models 조회도 없다
    ch = manifest.select("SELECT * FROM chunks ORDER BY chunk_id")
    assert [c["authority_type"] for c in ch] == [authority] * 2 and ch[0]["text"] == pages["p1"] and ch[0]["case_id"] is None
    assert ch[0]["locator_kind"] == "pdf_page" and ch[0]["page_id"] == f"{att}@p1"
    assert manifest.select("SELECT status FROM attachments")[0]["status"] == "structured"
    res2 = go(S.Structurer(manifest, prov_for(fake), corpus), manifest)
    assert res2["skipped_unchanged"] == 1 and res2["chunks"] == 0 and len(manifest.select("SELECT * FROM chunks")) == 2


def test_gd6_chunk_only_respects_the_derivative_right(manifest, corpus):
    from press_review.ingestion import rights as R
    doc, att, texts = seed(manifest, corpus, {"p1": "제1조 기준이다.\n"}, source_id="pac_board_committee_rules", family="pac_other", title="규정")
    row = manifest.get_rights("source", "pac_board_committee_rules")
    rights = {k: row[k] for k in R.RIGHT_KEYS}
    rights["create_derivatives"] = "permission_required"
    manifest.set_rights("source", "pac_board_committee_rules", rights, row["rights_basis"], "pending")
    res = go(S.Structurer(manifest, prov_for(DSFake(lambda b, n: None)), corpus), manifest)
    assert res["chunk_only"] == 0 and res["refused"] == 1 and manifest.select("SELECT * FROM chunks") == []


# =====================================================================================================================
# G-D7  검증 완료 대상의 재구조화는 --force 가 있어야 한다
# =====================================================================================================================
def _changed_prompt(tmp_path, monkeypatch):
    copy = tmp_path / "prompts_gd7"
    shutil.copytree(S.PROMPTS_DIR, copy)
    p = copy / "pac_adjustment.md"
    p.write_text(p.read_text(encoding="utf-8") + "\n- extra rule\n", encoding="utf-8")
    monkeypatch.setattr(S, "PROMPTS_DIR", copy)


def _structured_and_verified(manifest, corpus):
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None)
    fake.handler = handler_for(att)
    go(st, manifest)
    cid = f"{att}:2001-조정-1"
    S.mark_verified(manifest, cid, by="독립 검수자", at="2026-10-09T00:00:00+00:00")
    manifest.set_attachment_status(att, "verified")
    manifest.set_document_status(doc, "verified")
    return doc, att, texts, fake, prov, cid


def test_gd7_verified_target_is_found_but_refused_without_force(manifest, corpus, tmp_path, monkeypatch):
    doc, att, texts, fake, prov, cid = _structured_and_verified(manifest, corpus)
    assert [t.key for t in S.find_targets(manifest)] == [att]                                          # 기본 선택에 verified 도 들어간다
    n = fake.n_chat
    _changed_prompt(tmp_path, monkeypatch)
    res = go(S.Structurer(manifest, prov, corpus), manifest)
    assert res["refused_verified"] == 1 and res["processed"] == 0 and fake.n_chat == n
    assert manifest.select("SELECT status FROM attachments")[0]["status"] == "verified"
    case = manifest.get_case(cid)
    assert case["verification_status"] == "verified" and case["record"]["verified_by"]               # 검수 기록이 그대로다
    assert any(f["target"] == att and f["error_class"] == "verified_needs_force" for f in manifest.failures())


def test_gd7_force_restructures_and_returns_the_status_to_structured(manifest, corpus, tmp_path, monkeypatch):
    doc, att, texts, fake, prov, cid = _structured_and_verified(manifest, corpus)
    n = fake.n_chat
    _changed_prompt(tmp_path, monkeypatch)
    res = go(S.Structurer(manifest, prov, corpus, force=True), manifest)
    assert res["processed"] == 1 and fake.n_chat == n + 1 and res["refused_verified"] == 0
    assert manifest.select("SELECT status FROM attachments")[0]["status"] == "structured"
    assert manifest.get_document(doc)["status"] == "structured"
    assert manifest.get_case(cid)["verification_status"] == "unverified"
    assert not [f for f in manifest.failures() if f["error_class"] == "verified_needs_force"]


def test_gd7_force_on_unchanged_verified_target_also_resets_the_status(manifest, corpus):
    doc, att, texts, fake, prov, cid = _structured_and_verified(manifest, corpus)
    n = fake.n_chat
    assert go(S.Structurer(manifest, prov, corpus), manifest)["skipped_unchanged"] == 1                # 입력·버전이 같으면 건드리지 않는다
    assert manifest.select("SELECT status FROM attachments")[0]["status"] == "verified"
    go(S.Structurer(manifest, prov, corpus, force=True), manifest)
    assert fake.n_chat == n and manifest.select("SELECT status FROM attachments")[0]["status"] == "structured"


def test_gd7_a_verified_case_alone_is_enough_to_require_force(manifest, corpus, tmp_path, monkeypatch):
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None)
    fake.handler = handler_for(att)
    go(st, manifest)
    cid = f"{att}:2001-조정-1"
    S.mark_verified(manifest, cid, by="독립 검수자", at="2026-10-09T00:00:00+00:00")                  # mark_verified 는 첨부 상태를 바꾸지 않는다
    assert manifest.select("SELECT status FROM attachments")[0]["status"] == "structured"
    _changed_prompt(tmp_path, monkeypatch)
    res = go(S.Structurer(manifest, prov, corpus), manifest)
    assert res["refused_verified"] == 1 and manifest.get_case(cid)["verification_status"] == "verified"


def test_gd7_chunk_only_target_that_is_indexed_is_not_rebuilt_without_force(manifest, corpus):
    pages = {"p1": "제1조 기준이다.\n"}
    doc, att, texts = seed(manifest, corpus, pages, source_id="pac_board_committee_rules", family="pac_other", title="규정")
    fake = DSFake(lambda b, n: None)
    assert go(S.Structurer(manifest, prov_for(fake), corpus), manifest)["chunk_only"] == 1
    manifest.set_attachment_status(att, "indexed")                                                     # P3 색인이 이 chunk 를 읽었다
    (corpus / "text" / "pac_board_committee_rules" / "1_1.txt").write_text("제1조 기준이 바뀌었다.\n", encoding="utf-8")
    manifest.upsert_page(f"{att}@p1", doc, att_id=att, pdf_page_no=1, method="pdftotext", text_path="text/pac_board_committee_rules/1_1.txt",
                         text_sha256=__import__("hashlib").sha256("제1조 기준이 바뀌었다.\n".encode()).hexdigest(), char_count=12, quality=1.0)
    res = go(S.Structurer(manifest, prov_for(fake), corpus), manifest)
    assert res["refused_verified"] == 1 and res["chunk_only"] == 0 and manifest.select("SELECT text FROM chunks")[0]["text"] == "제1조 기준이다.\n"
    res2 = go(S.Structurer(manifest, prov_for(fake), corpus, force=True), manifest)
    assert res2["chunk_only"] == 1 and manifest.select("SELECT text FROM chunks")[0]["text"] == "제1조 기준이 바뀌었다.\n"
    assert manifest.select("SELECT status FROM attachments")[0]["status"] == "structured"


# =====================================================================================================================
# G-D8  PAC e-book 뷰어 껍데기는 받지 않고 withheld(viewer_shell)
# =====================================================================================================================
EB = "https://www.pac.or.kr/kor/ebook/periodical/mz_per_2/2003_fall/index.html"


def _shell_ledger(m):
    m.ensure_source("pac_research_quarterly", "pac_other", "계간", "https://www.pac.or.kr/x")
    m.upsert_document("pac_other:1", "pac_research_quarterly", title="a")
    m.upsert_attachment("pac_other:1#1", "pac_other:1", "https://www.pac.or.kr/kor/pages/?p=200&sub_idx=1&m=read", kind="full", label="PDF")
    m.upsert_attachment("pac_other:1#2", "pac_other:1", EB, kind="other", label="e-book")
    m.upsert_document("pac_other:2", "pac_research_quarterly", title="b")                      # PDF 짝이 없는 문서
    m.upsert_attachment("pac_other:2#1", "pac_other:2", "https://www.pac.or.kr/kor/ebook/periodical/mz_per_2/2007_winter.htm", kind="other", label="e-book")
    m.upsert_attachment("pac_other:2#2", "pac_other:2", "https://www.pac.or.kr/kor/pages/?p=200&idx=2&m=read", kind="other", label="상세")   # ebook 이 아닌 other
    m.upsert_document("iendc:9", "pac_research_quarterly", title="c")
    m.upsert_attachment("iendc:9#1", "iendc:9", "https://www.iendc.go.kr/fileDownload.do?filename=a.hwp&downname=b", kind="other", label="a.hwp")


def _withhold(corpus, *argv):
    out = io.StringIO()
    code = cli.main(["withhold", *argv], stdout=out, corpus=corpus, log_stream=io.StringIO(), clock=FakeClock(), sleep=lambda s: None)
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def test_gd8_viewer_shell_rule_is_the_ebook_path_on_pac_not_every_other_kind():
    from press_review.ingestion import withhold as W
    assert W.viewer_shell_reason({"kind": "other", "url": EB}) == "viewer_shell"
    assert W.viewer_shell_reason({"kind": "other", "url": "http://www.pac.or.kr/kor/ebook/etc/etc_mz_per_1/2012.htm"}) == "viewer_shell"
    assert W.viewer_shell_reason({"kind": "other", "url": "https://www.iendc.go.kr/fileDownload.do?filename=a.hwp"}) is None
    assert W.viewer_shell_reason({"kind": "other", "url": "https://www.pac.or.kr/kor/pages/?p=200&m=read"}) is None
    assert W.viewer_shell_reason({"kind": "full", "url": EB}) is None
    assert W.viewer_shell_reason({"kind": "other", "url": "https://evil.example/kor/ebook/x.htm"}) is None


def test_gd8_command_withholds_shells_records_the_reason_and_is_idempotent(corpus):
    m = Manifest(corpus / "manifest.sqlite")
    _shell_ledger(m)
    m.close()
    code, o = _withhold(corpus, "--dry-run")
    assert code == 0 and o["result"]["matched"] == 2 and o["result"]["withheld"] == 0 and o["result"]["dry_run"] is True
    m = Manifest(corpus / "manifest.sqlite")
    assert {a["status"] for a in m.attachments_for("pac_other:1")} == {"discovered"}                      # dry-run 은 장부를 바꾸지 않는다
    m.close()
    code, o = _withhold(corpus)
    r = o["result"]
    assert code == 0 and (r["matched"], r["withheld"], r["already_withheld"]) == (2, 2, 0) and r["without_pdf_sibling"] == 1
    assert r["by_source"] == {"pac_research_quarterly": 2}
    m = Manifest(corpus / "manifest.sqlite")
    st = {a["att_id"]: a["status"] for d in ("pac_other:1", "pac_other:2", "iendc:9") for a in m.attachments_for(d)}
    assert st == {"pac_other:1#1": "discovered", "pac_other:1#2": "withheld", "pac_other:2#1": "withheld", "pac_other:2#2": "discovered",
                  "iendc:9#1": "discovered"}
    f = [x for x in m.failures() if x["error_class"] == "viewer_shell"]
    assert sorted(x["target"] for x in f) == ["pac_other:1#2", "pac_other:2#1"] and all(x["stage"] == "fetch" for x in f)
    m.upsert_attachment("pac_other:1#2", "pac_other:1", EB, kind="other", label="e-book")                  # 어댑터 재실행이 껍데기를 되살리지 않는다
    assert m.select("SELECT status FROM attachments WHERE att_id='pac_other:1#2'")[0]["status"] == "withheld"
    m.close()
    code, o2 = _withhold(corpus)
    assert (o2["result"]["withheld"], o2["result"]["already_withheld"]) == (0, 2)


def test_gd8_fetch_never_selects_a_viewer_shell_even_if_it_is_still_discovered(corpus):
    from press_review.ingestion import fetch as F
    m = Manifest(corpus / "manifest.sqlite")
    _shell_ledger(m)
    cands, info = F.select_candidates(m, corpus)
    m.close()
    ids = {a["att_id"] for a in cands}
    assert "pac_other:1#2" not in ids and "pac_other:2#1" not in ids and info["viewer_shells_skipped"] == 2


def test_gd8_pac_adapter_withholds_a_newly_found_ebook_shell_and_a_rerun_keeps_it(corpus, monkeypatch):
    import test_src_pac as P
    monkeypatch.setattr(P.pac, "PAGE_SIZE", 2)
    shell = "/kor/ebook/periodical/mz_per_2/2003_fall/index.html"
    detail = P.DETAIL.replace('<li><a href="" target="_blank">링크 없는 항목</a></li>', f'<li><a href="{shell}">e-book</a></li>')
    routes = P._routes()
    for i in (1212, 1169, 793, 700):
        routes[P.D.format(i=i)] = (200, detail, {})
    clock = FakeClock()
    t = FakeTransport(routes, clock=clock)
    code, o = P._run(corpus, t, clock)
    assert code == 0
    m = Manifest(corpus / "manifest.sqlite")
    shells = [a for d in ("pac_cases:1212", "pac_cases:1169") for a in m.attachments_for(d) if a["url"].endswith(shell)]
    assert len(shells) == 2 and {a["status"] for a in shells} == {"withheld"} and {a["kind"] for a in shells} == {"other"}
    assert not any(shell in u for u in t.urls())                                           # 뷰어 주소는 요청하지 않는다
    m.close()
    code, o2 = P._run(corpus, t, clock)
    m = Manifest(corpus / "manifest.sqlite")
    assert {a["status"] for d in ("pac_cases:1212",) for a in m.attachments_for(d) if a["url"].endswith(shell)} == {"withheld"}
    m.close()


# =====================================================================================================================
# G-D9  IENDC POST 전용 HWP 의 첨부 행
# =====================================================================================================================
def test_gd9_hwp_only_idx_get_post_attachment_rows_and_rerun_adds_none(corpus):
    r = iendc_routes()
    hwp = "<a onClick=\"cmmn_file_down('조치내역%d.hwp','unfair_%d+x.hwp')\">조치내역.hwp</a>"
    for no in (5, 6, 7):
        r[f"{iendc.BASE}/home/kor/board.do?menuPos=45&act=detail&idx={100 + no}"] = (200, detail_html(no).split("<script>PDFObject")[0] + hwp % (no, no), {})
    tr = FakeTransport(r)
    clock = FakeClock()
    code, o = iendc_run([], corpus, tr, clock)
    assert code == 0 and o["status"] == "completed"
    m = Manifest(corpus / "manifest.sqlite")
    rows = [(d, m.attachments_for(d)) for d in ("iendc:105", "iendc:106", "iendc:107")]
    n_att = m.counts()["attachments"]
    m.close()
    assert all(len(a) == 1 and a[0]["kind"] == "other" and a[0]["url"].startswith(iendc.BASE + "/fileDownload.do?filename=") for _d, a in rows)
    assert not any("fileDownload" in u for u in tr.urls())                                              # 행만 만들고 내려받지는 않는다
    code, o2 = iendc_run([], corpus, tr, clock)
    m = Manifest(corpus / "manifest.sqlite")
    assert m.counts()["attachments"] == n_att
    m.close()


# =====================================================================================================================
# G-D10  병합 판결: 문서 1건 = 판결 1건
# =====================================================================================================================
def test_gd10_merged_judgment_document_stays_one_case(manifest, corpus):
    doc, att, texts = seed_court(manifest, corpus)                       # 사건번호 '2022다284711, 284728' 병합 판결 1건
    a = court_draft(att)
    b = court_draft(att, case_number="2022다284728")
    fake = DSFake(lambda bb, n: (200, chat_payload({"cases": [a, b]}), {}))
    res = go(S.Structurer(manifest, prov_for(fake), corpus), manifest)
    rows = manifest.select("SELECT case_id, case_number FROM cases")
    assert res["cases"] == 1 and len(rows) == 1 and rows[0]["case_number"] == "2022다284711, 284728"
    assert rows[0]["case_id"] == f"{att}:2022다284711,284728"


# =====================================================================================================================
# G-D11  모델 이름
# =====================================================================================================================
def test_gd11_allowlist_uses_the_canonical_names_and_the_legacy_name_is_an_alias():
    assert ds.ALLOWED_MODELS == ("deepseek-flash", "deepseek-v4-pro")
    assert ds.canonical_model("deepseek-v4-flash") == "deepseek-flash" and ds.canonical_model("deepseek-flash") == "deepseek-flash"
    assert ds.canonical_model("deepseek-v4-pro") == "deepseek-v4-pro"
    for bad in ("deepseek-chat", "gpt-5", "deepseek-v4"):
        with pytest.raises(ds.ModelNotAllowed):
            ds.canonical_model(bad)
    assert S.DEFAULT_MODEL == "deepseek-flash"
    pb = ds.PriceBook.load()
    assert pb.canonical_id("deepseek-flash") == "deepseek-flash" and pb.entry("deepseek-flash") == pb.entry("deepseek-v4-flash")


def test_gd11_verify_model_is_judged_by_the_models_listing(manifest):
    fake = DSFake(lambda b, n: None, models=("deepseek-flash", "deepseek-v4-pro"))
    assert prov_for(fake).verify_model("deepseek-flash") == "deepseek-flash"
    assert prov_for(DSFake(lambda b, n: None, models=("deepseek-flash", "deepseek-v4-pro"))).verify_model("deepseek-v4-flash") == "deepseek-flash"
    with pytest.raises(ds.ModelUnavailable):                                                     # 목록에 정식 이름이 없으면 구 이름만 있어도 쓰지 않는다
        prov_for(DSFake(lambda b, n: None, models=("deepseek-v4-flash", "deepseek-v4-pro"))).verify_model("deepseek-flash")


def test_gd11_ledger_keeps_the_requested_name_and_the_returned_name(manifest, corpus):
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None, model="deepseek-v4-flash")      # 구 이름으로 요청해도
    d = good_case(pids_of(att))
    fake.handler = lambda b, n: (200, chat_payload({"cases": [d]}, model="deepseek-v4.1-flash"), {})
    go(st, manifest)
    assert st.model == "deepseek-flash" and fake.bodies[0]["model"] == "deepseek-flash"               # 공급자 현행 이름으로 보낸다
    row = manifest.select("SELECT * FROM api_usage")[0]
    assert row["requested_model"] == "deepseek-flash" and row["model"] == "deepseek-v4.1-flash"
    snap = json.loads(row["price_snapshot_json"])
    assert snap["requested_model"] == "deepseek-flash" and snap["returned_model"] == "deepseek-v4.1-flash" and snap["canonical_id"] == "deepseek-flash"
    assert row["cost_usd"] > 0
    assert record_of(manifest, f"{att}:2001-조정-1")["structurer"]["model"] == "deepseek-flash"


def test_gd11_requested_model_column_is_added_to_an_existing_v2_ledger(corpus):
    p = corpus / "manifest.sqlite"
    m = Manifest(p)
    m.close()
    con = sqlite3.connect(p)
    con.execute("ALTER TABLE api_usage DROP COLUMN requested_model")
    con.commit()
    assert "requested_model" not in [r[1] for r in con.execute("PRAGMA table_info(api_usage)")]
    con.close()
    m = Manifest(p)                                                                                 # 이미 user_version 2 인 장부를 다시 연다
    m.record_api_usage("deepseek", "deepseek-v4.1-flash", requested_model="deepseek-flash", prompt_tokens=5, cost_usd=0.001, job_id="j")
    row = m.select("SELECT requested_model, model FROM api_usage")[0]
    m.close()
    assert (row["requested_model"], row["model"]) == ("deepseek-flash", "deepseek-v4.1-flash")


def test_gd11_cli_accepts_both_names_and_reports_the_canonical_one(manifest, corpus):
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None)
    for name in ("deepseek-v4-flash", "deepseek-flash"):
        buf = io.StringIO()
        code = S.main(["--corpus", str(corpus), "--model", name, "--plan-only"], provider=prov, out=buf)
        assert code == 0 and json.loads(buf.getvalue())["model"] == "deepseek-flash"
    with pytest.raises(SystemExit):
        S.main(["--corpus", str(corpus), "--model", "deepseek-chat", "--plan-only"], provider=prov, out=io.StringIO())

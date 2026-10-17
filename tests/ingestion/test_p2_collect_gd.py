"""P2 미니 게이트 후 결정 G-D13~G-D20(docs/P2_DESIGN.md §10) 중 수집 쪽 시험. 가짜 transport·가짜 실행기, 네트워크 없음. 합성 텍스트이며 실제 사건이 아니다.

테스트 이름은 ``test_gd<번호>_...`` 이고 번호 뒤에 밑줄이 붙는다(``-k "gd1_"`` 이 gd13 을 끌어오지 않게).
G-D16 시험은 §10 문장(허용 표)에 대조하며 contracts.py 의 현재 내용에는 기대지 않는다(짝 규칙 정본은 엔진 쪽 contracts.validate_case_record).
"""
import io
import json
import re

import pytest

import pdr_ing_paths  # noqa: F401
from conftest import FakeClock, FakeTransport, ROBOTS_OK
from ds_fakes import DSFake, chat_payload, good_case, q, seed
from ext_helpers import FakeRunner, make_pdf, page_text, place_raw
from ext_helpers import seed as ext_seed
from press_review import contracts
from press_review.ingestion import __main__ as cli
from press_review.ingestion import coverage as cov_mod
from press_review.ingestion import extract as ext
from press_review.ingestion import fetch as fetch_mod
from press_review.ingestion import structure as S
from press_review.ingestion import structure_records as sr
from press_review.ingestion.manifest import Manifest
from press_review.ingestion.sources import lawapi as L
from test_gd import IENDC_FAM, IENDC_SRC, KD_TEXT, KdRunner, _iendc_pdf, prov_for
from test_ing_extract_run import env  # noqa: F401  (fixture)
from test_ing_structure import go, make_run, pids_of

SPEC = {"version": "v", "model": "m", "input_sha256": "0" * 64}


def ctx_for(page_texts, profile="pac_adjustment", source_id="", **kw):
    pages = {pid: sr.PageText(t) for pid, t in page_texts.items()}
    return sr.WindowCtx(profile=sr.PROFILES[profile], doc_id="d:1", att_id=None, target_key="k", unit="w0000", pages=pages, page_meta={},
                        structurer=SPEC, **({"source_id": source_id} if source_id else {}), **kw)


# =====================================================================================================================
# G-D13  인용 대조는 공백을 모두 지운 문자열로, offset 은 원문 기준
# =====================================================================================================================
def test_gd13_quote_found_when_only_whitespace_differs_inside_a_word():
    page = "판결집 조판에서 줄바꿈이 단어 중\n간에 들어간다. 다음 문장이다."
    ctx = ctx_for({"pg": page})
    loc, why = sr.locate_quote({"page_id": "pg", "quote": "단어 중간에 들어간다."}, ctx)
    assert why == "ok" and loc is not None
    assert page[loc["start"]:loc["end"]] == loc["quote"] == "단어 중\n간에 들어간다."
    assert contracts._collapse(page[loc["start"]:loc["end"]]) == contracts._collapse(loc["quote"])


def test_gd13_span_starts_at_first_and_ends_at_last_non_space_character():
    page = "가나다   라마바사  \n\n  아자차카"
    ctx = ctx_for({"pg": page})
    loc, why = sr.locate_quote({"page_id": "pg", "quote": "  라마 바사 아자  "}, ctx)
    assert why == "ok" and page[loc["start"]:loc["end"]] == "라마바사  \n\n  아자"
    assert not page[loc["start"]].isspace() and not page[loc["end"] - 1].isspace()


def test_gd13_nothing_but_whitespace_is_normalised():
    ctx = ctx_for({"pg": "제3조 「정정보도」 청구권은 2년이다. 한자 名譽毀損 포함"})
    assert sr.locate_quote({"page_id": "pg", "quote": "제３조 「정정보도」 청구권은"}, ctx)[1] == "quote_not_found"      # 전각 숫자
    assert sr.locate_quote({"page_id": "pg", "quote": "정정보도 청구권은 2년이다"}, ctx)[1] == "quote_not_found"          # 기호(「」)
    assert sr.locate_quote({"page_id": "pg", "quote": "한자 명예훼손 포함"}, ctx)[1] == "quote_not_found"                 # 한자 -> 한글 아님
    assert sr.locate_quote({"page_id": "pg", "quote": "정정보도」 청구권은 2년이다"}, ctx)[1] == "ok"


def test_gd13_first_occurrence_wins_and_other_pages_are_still_searched():
    page = "반복 문장이다 끝\n반복 문장이다 끝"
    ctx = ctx_for({"a": page, "b": "다른 쪽의 고유한 문장이다 정말"})
    loc, _ = sr.locate_quote({"page_id": "a", "quote": "반복문장이다끝"}, ctx)
    assert loc["start"] == 0
    loc2, why2 = sr.locate_quote({"page_id": "a", "quote": "다른 쪽의 고유한문장이다 정말"}, ctx)
    assert why2 == "ok_relocated" and loc2["page_id"] == "b"


def test_gd13_end_to_end_a_quote_with_different_line_breaks_is_kept_and_contract_valid(manifest, corpus):
    pages = {"p1": "사건 2001-조정-1\n신청인 A는 가상일보의 기사가 사실과 다르다며 정정\n보도를 청구하였다.\n", "p2": "위원회는 양 당사자가 조정합의에\n이르렀음을 확인한다.\n"}
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None, pages=pages)
    p1, p2 = pids_of(att)
    c = good_case([p1, p2], disputed_spans=[], claims_or_requested_remedy=[], legal_reasoning=[], original_article={"status": "absent", "quote": None},
                  correction_pair_confidence="not_applicable",
                  factual_background=[{"text": "청구", "speaker": "claimant", "quote": q(p1, "신청인 A는 가상일보의 기사가 사실과 다르다며 정정보도를 청구하였다.")}],
                  outcome={"text": "조정합의", "speaker": "institution", "quote": q(p2, "위원회는 양 당사자가 조정합의에 이르렀음을 확인한다.")})
    fake.handler = lambda b, n: (200, chat_payload({"cases": [c]}), {})
    res = go(st, manifest)
    assert res["cases"] == 1 and res["cases_quarantined"] == 0
    rec = manifest.get_case(f"{att}:2001-조정-1")["record"]
    assert rec["quote_check"]["matched"] == rec["quote_check"]["total"] == 2
    assert contracts.validate_case_record(rec, texts) == []
    fb = rec["factual_background"][0]["quote"]
    assert texts[fb["page_id"]][fb["start"]:fb["end"]] == fb["quote"] == "신청인 A는 가상일보의 기사가 사실과 다르다며 정정\n보도를 청구하였다."


# =====================================================================================================================
# G-D14  추출기 선택 인프라(소스별 상수 + --extractor, 재추출 시 이후 단계 무효화)
# =====================================================================================================================
def _pac_pdf(env, n=2, source=None, family=None, doc_key="500"):
    kw = {"source": source, "family": family} if source else {}
    doc, (a1,) = ext_seed(env.m, doc_key, atts=[(1, f"https://www.pac.or.kr/files/{doc_key}.pdf", "full", None, "application/pdf", "PDF")], **kw)
    blob = make_pdf([f"pac {doc_key} page {i + 1}" for i in range(n)])
    spec = {"pages": [page_text(i + 1, i + 1, doc_key) for i in range(n)], "kd_pages": KD_TEXT[:n]}
    place_raw(env.m, env.corpus, a1, blob, **({"family": family} if family else {}))
    return doc, a1, blob, spec


def _att(env, a):
    return env.m.select("SELECT * FROM attachments WHERE att_id=?", (a,))[0]


def _pdftotext_args(r):
    return [a for a in r.calls if a[0].endswith("pdftotext")]


def test_gd14_three_extractor_names_and_legacy_defaults_are_unchanged(env):
    assert tuple(ext.PDF_EXTRACTORS)[:2] == ("pdftotext", "pdftotext_layout") and "kordoc" in ext.PDF_EXTRACTORS      # G-D28 이 pdftotext_raw·column_crop 을 더했다(tests/ingestion/test_gd28.py)
    assert ext.pdf_extractor_for({"source_id": "pac_board_committee_rules"}) == "pdftotext_layout"          # 목록에 없는 소스는 기존 동작(-layout)이 기본값
    assert ext.pdf_extractor_for({"source_id": IENDC_SRC}) == "kordoc"
    doc, a1, blob, spec = _pac_pdf(env)
    r = FakeRunner({blob: spec})
    env.run(r)
    assert all("-layout" in a for a in _pdftotext_args(r))
    row = _att(env, a1)
    assert row["extract_input_sha256"] == row["raw_sha256"] and row["extractor_version"] == ext.EXTRACTOR_VERSION           # 기본 추출 결과는 해시·버전이 그대로
    _, res = env.run(r)
    assert res["result"]["outcomes"] == {"skipped": 1}


def test_gd14_extractor_option_changes_reading_mode_and_rewrites_pages_and_downstream(env):
    doc, a1, blob, spec = _pac_pdf(env)
    r = FakeRunner({blob: spec})
    env.run(r)
    old = env.m.pages_of(att_id=a1)
    assert [p["method"] for p in old] == ["pdftotext", "pdftotext"]
    env.m.upsert_case("c1", doc, {}, att_id=a1)                                # 이후 단계 산출물을 흉내 낸다
    env.m.upsert_chunk("ch1", doc, "t", "pdf_page", "1", case_id="c1")
    env.m.set_attachment_status(a1, "structured")
    n0 = len(_pdftotext_args(r))
    code, res = env.run(r, "--extractor", "pdftotext")
    assert code == 0 and res["result"]["outcomes"] == {"extracted": 1}
    new_calls = _pdftotext_args(r)[n0:]
    assert new_calls and all("-layout" not in a for a in new_calls)             # 읽기 순서 모드
    rows = env.m.pages_of(att_id=a1)
    assert [p["method"] for p in rows] == ["pdftotext_reading"] * 2 and json.loads(rows[0]["quality_json"])["extractor"] == "pdftotext"
    assert env.m.select("SELECT COUNT(*) n FROM cases")[0]["n"] == 0 and env.m.select("SELECT COUNT(*) n FROM chunks")[0]["n"] == 0
    row = _att(env, a1)
    assert row["status"] == "extracted" and row["extract_input_sha256"] != row["raw_sha256"]
    assert json.loads(env.m.get_document(doc)["extra_json"])["p2_extract"]["pdf_extractor"] == "pdftotext"
    _, res2 = env.run(r, "--extractor", "pdftotext")
    assert res2["result"]["outcomes"] == {"skipped": 1}                          # 같은 선택이면 다시 하지 않는다
    _, res3 = env.run(r, "--extractor", "pdftotext_layout")                      # 기본으로 돌아가면 다시 -layout 으로, 해시는 원래 값
    assert res3["result"]["outcomes"] == {"extracted": 1} and _att(env, a1)["extract_input_sha256"] == _att(env, a1)["raw_sha256"]
    assert [p["method"] for p in env.m.pages_of(att_id=a1)] == ["pdftotext"] * 2


def test_gd14_per_source_constant_change_reextracts_even_structured_attachments(env, monkeypatch):
    doc, a1, blob, spec = _pac_pdf(env)
    r = KdRunner({blob: spec})
    env.run(r)
    env.m.set_attachment_status(a1, "structured")
    monkeypatch.setitem(ext.PDF_EXTRACTOR_BY_SOURCE, "pac_board_committee_rules", "kordoc")
    code, res = env.run(r)
    assert code == 0 and res["result"]["outcomes"] == {"extracted": 1}
    assert [p["method"] for p in env.m.pages_of(att_id=a1)] == ["kordoc", "kordoc"]
    _, res2 = env.run(r)
    assert res2["result"]["outcomes"] == {"skipped": 1}


def test_gd14_plain_run_leaves_structured_attachments_alone_even_with_an_old_extractor_version(env):
    doc, a1, blob, spec = _pac_pdf(env)
    r = FakeRunner({blob: spec})
    env.run(r)
    env.m.set_attachment_status(a1, "structured")
    env.m.update_attachment_fields(a1, extractor_version="p2x-1")
    n = len(r.calls)
    _, res = env.run(r)
    assert res["result"]["outcomes"] == {"skipped": 1} and len(r.calls) == n and _att(env, a1)["status"] == "structured"


def test_gd14_verified_attachment_is_refused_without_force(env):
    doc, a1, blob, spec = _pac_pdf(env)
    r = FakeRunner({blob: spec})
    env.run(r)
    env.m.upsert_case("c1", doc, {}, att_id=a1, verification_status="verified")
    env.m.set_attachment_status(a1, "verified")
    n = len(r.calls)
    _, res = env.run(r, "--extractor", "pdftotext")
    assert res["result"]["outcomes"] == {"refused_verified": 1} and len(r.calls) == n
    assert env.m.select("SELECT COUNT(*) n FROM cases")[0]["n"] == 1 and _att(env, a1)["status"] == "verified"
    assert [f["error_class"] for f in env.m.failures(open_only=True) if f["target"] == a1] == ["verified_needs_force"]
    _, res2 = env.run(r, "--extractor", "pdftotext", "--force")
    assert res2["result"]["outcomes"] == {"extracted": 1} and env.m.select("SELECT COUNT(*) n FROM cases")[0]["n"] == 0
    assert _att(env, a1)["status"] == "extracted" and not [f for f in env.m.failures(open_only=True) if f["target"] == a1]


def test_gd14_kordoc_option_overrides_a_pdftotext_source_and_unknown_names_are_rejected(env, capsys):
    doc, a1, blob, spec = _pac_pdf(env)
    r = KdRunner({blob: spec})
    _, res = env.run(r, "--extractor", "kordoc")
    assert res["result"]["outcomes"] == {"extracted": 1} and [p["method"] for p in env.m.pages_of(att_id=a1)] == ["kordoc", "kordoc"]
    assert json.loads(env.m.get_document(doc)["extra_json"])["p2_extract"]["pdf_extractor"] == "kordoc"
    with pytest.raises(SystemExit):
        env.run(r, "--extractor", "tesseract")


def test_gd14_extractor_option_does_not_touch_non_pdf_kinds_hash(env):
    doc, (a1,) = ext_seed(env.m, "700", source=IENDC_SRC, family=IENDC_FAM, atts=[(1, "https://www.iendc.go.kr/x.hwp", "other", None, None, "x.hwp")])
    blob = b"\xd0\xcf\x11\xe0 fake hwp bytes"
    place_raw(env.m, env.corpus, a1, blob, kind="hwp", family=IENDC_FAM)
    r = FakeRunner({blob: {"kordoc": {"success": True, "metadata": {"pageMode": "layout"}, "pages": [{"pageNumber": 1, "markdown": "본문 텍스트가 충분히 길게 들어 있는 한 쪽입니다"}]}}})
    env.run(r, "--extractor", "pdftotext")
    row = _att(env, a1)
    assert row["status"] == "extracted" and row["extract_input_sha256"] == row["raw_sha256"]


# =====================================================================================================================
# G-D15  표 칸 하나와 정확히 일치하는 짧은 인용
# =====================================================================================================================
HTML_TABLE = ("<table>\n<tr><td>가상일보</td><td>「A시장 공사비 의혹」</td><td>주 의</td></tr>\n"
              "<tr><td>다른일보</td><td>「B군수 인사 의혹」</td><td>주 의</td></tr>\n</table>")
MD_TABLE = "| 언론사 | 보도제목 | 조 치 |\n| --- | --- | --- |\n| 가상일보 | 「A시장 공사비 의혹」 | 경 고 |\n"


@pytest.mark.parametrize("source", ["iendc_review_results", "pac_board_correction_status"])
def test_gd15_whole_cell_quote_skips_the_minimum_length_in_table_sources(source):
    assert source in sr.TABLE_CELL_SOURCES
    ctx = ctx_for({"pg": HTML_TABLE}, profile="iendc_table", source_id=source)
    loc, why = sr.locate_quote({"page_id": "pg", "quote": "주의"}, ctx)
    assert why == "ok_cell" and HTML_TABLE[loc["start"]:loc["end"]] == "주 의"
    ctx_md = ctx_for({"pg": MD_TABLE}, profile="iendc_table", source_id=source)
    loc2, why2 = sr.locate_quote({"page_id": "pg", "quote": "경고"}, ctx_md)
    assert why2 == "ok_cell" and MD_TABLE[loc2["start"]:loc2["end"]] == "경 고"


def test_gd15_short_quote_that_is_not_exactly_one_cell_is_still_too_short():
    page = "<table><tr><td>경고 및 주의 조치</td><td>해당없음</td></tr></table>"
    ctx = ctx_for({"pg": page}, profile="iendc_table", source_id="iendc_review_results")
    assert sr.locate_quote({"page_id": "pg", "quote": "주의"}, ctx)[1] == "quote_too_short"           # 칸의 일부
    assert sr.locate_quote({"page_id": "pg", "quote": "해당"}, ctx)[1] == "quote_too_short"
    assert sr.locate_quote({"page_id": "pg", "quote": "없음"}, ctx)[1] == "quote_too_short"
    assert sr.locate_quote({"page_id": "pg", "quote": "해당없음"}, ctx)[1] == "ok"                      # 4자 이상은 원래 규칙으로 통과


def test_gd15_non_table_sources_and_pages_without_table_markup_get_no_exemption():
    ctx = ctx_for({"pg": HTML_TABLE}, profile="pac_adjustment", source_id="pac_adjustment_casebooks")
    assert sr.locate_quote({"page_id": "pg", "quote": "주의"}, ctx)[1] == "quote_too_short"
    flat = "가상일보      「A시장 공사비 의혹」      주 의\n다른일보      「B군수 인사 의혹」      주 의\n"
    ctx2 = ctx_for({"pg": flat}, profile="iendc_table", source_id="iendc_review_results")           # pdftotext 식 평문: 칸 경계를 모른다
    assert sr.locate_quote({"page_id": "pg", "quote": "주의"}, ctx2)[1] == "quote_too_short"


def test_gd15_short_cell_quote_lands_on_the_row_of_the_case_not_the_first_row():
    ctx = ctx_for({"pg": HTML_TABLE}, profile="iendc_table", source_id="iendc_review_results")

    def draft(title):
        return {"case_number": None, "institution": "iendc", "authority_type": "iendc_review", "first_page_id": "pg",
                "outcome_type": "decision", "outcome_scope": "whole",
                "disputed_spans": [{"text": "보도제목", "speaker": "article", "quote": {"page_id": "pg", "quote": title}}],
                "outcome": {"text": "주의 조치", "speaker": "institution", "quote": {"page_id": "pg", "quote": "주의"}}}
    a = sr.assemble_case(draft("「A시장 공사비 의혹」"), ctx, 0)
    b = sr.assemble_case(draft("「B군수 인사 의혹」"), ctx, 1)
    row_b = HTML_TABLE.index("다른일보")
    oa, ob = a.record["outcome"]["quote"], b.record["outcome"]["quote"]
    assert oa["start"] < row_b <= ob["start"]
    assert HTML_TABLE[oa["start"]:oa["end"]] == HTML_TABLE[ob["start"]:ob["end"]] == "주 의"
    assert a.record["verification_status"] == "unverified" == b.record["verification_status"]
    assert a.errors == [] and b.errors == []


def test_gd15_prompts_tell_the_model_about_short_cell_quotes():
    for name in ("iendc_table", "pac_correction"):
        text = S.load_prompt(sr.PROFILES[name]).system
        assert re.search(r"exactly one (whole )?(table )?cell", text) and "shorter than 4" in text


def test_gd15_end_to_end_source_id_reaches_the_window_context(manifest, corpus):
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None, pages={"p1": HTML_TABLE, "p2": "<table><tr><td>끝</td></tr></table>"},
                                               seed_kw={"source_id": "iendc_review_results", "family": "iendc"})
    p1 = pids_of(att)[0]
    d = {"case_number": None, "institution": "iendc", "authority_type": "iendc_review", "first_page_id": p1, "outcome_type": "decision", "outcome_scope": "whole",
         "disputed_spans": [{"text": "보도제목", "speaker": "article", "quote": q(p1, "「B군수 인사 의혹」")}],
         "outcome": {"text": "주의", "speaker": "institution", "quote": q(p1, "주의")}}
    fake.handler = lambda b, n: (200, chat_payload({"cases": [d]}), {})
    res = go(st, manifest)
    assert res["cases"] == 1 and res["cases_quarantined"] == 0
    rec = json.loads(manifest.select("SELECT record_json FROM cases")[0]["record_json"])
    assert rec["outcome"]["quote"]["start"] > HTML_TABLE.index("다른일보")


# =====================================================================================================================
# G-D16  프롬프트 문구를 §10 허용 표에 맞춘다(짝 규칙 정본은 contracts.validate_case_record)
# =====================================================================================================================
PROC = {"dismissal_procedural", "dismissal_merits", "withdrawal"}
G_D16_ALLOWED = {
    "judgment": {"judgment", "unknown"},
    "pac_settlement": {"settlement", "unknown"} | PROC,
    "pac_decision": {"decision", "correction", "unknown"} | PROC,
    "pac_election_review": {"decision", "correction", "guideline", "unknown"} | PROC,
    "iendc_review": {"decision", "correction", "guideline", "unknown"} | PROC,
    "research": {"research", "unknown"},
}
OUTCOME_VALUES = {"judgment", "decision", "settlement", "withdrawal", "dismissal_procedural", "dismissal_merits", "correction", "guideline", "research", "unknown"}


def _outcome_values(text):
    """프로필 프롬프트에서 'outcome_type' 이 들어간 줄들의 백틱 값."""
    vals = set()
    for line in text.splitlines():
        if "outcome_type" in line:
            vals |= set(re.findall(r"`([a-z_]+)`", line)) & OUTCOME_VALUES
    return vals


@pytest.mark.parametrize("profile,authorities", [("pac_election", ["pac_election_review"]), ("iendc_table", ["iendc_review"]),
                                                  ("pac_correction", ["pac_decision"])])
def test_gd16_pac_and_iendc_profiles_list_dismissal_and_withdrawal(profile, authorities):
    body = S.load_prompt(sr.PROFILES[profile]).system.split("## Source profile", 1)[1]
    vals = _outcome_values(body)
    assert {"dismissal_procedural", "dismissal_merits", "withdrawal"} <= vals, vals
    allowed = set().union(*(G_D16_ALLOWED[a] for a in authorities))
    assert vals <= allowed, vals - allowed


def test_gd16_adjustment_profile_never_offers_values_outside_the_table():
    body = S.load_prompt(sr.PROFILES["pac_adjustment"]).system.split("## Source profile", 1)[1]
    lines = [l for l in body.splitlines() if "outcome_type" in l]
    assert any("pac_settlement" in l for l in lines) and any("pac_decision" in l for l in lines)
    for l in lines:
        vals = set(re.findall(r"`([a-z_]+)`", l)) & OUTCOME_VALUES
        if "pac_settlement" in l:
            assert vals <= G_D16_ALLOWED["pac_settlement"]
        if "pac_decision" in l:
            assert vals <= G_D16_ALLOWED["pac_decision"] and {"withdrawal", "dismissal_procedural", "dismissal_merits"} <= vals


@pytest.mark.parametrize("profile,authority", [("pac_election", "pac_election_review"), ("iendc_table", "iendc_review"), ("pac_correction", "pac_decision")])
def test_gd16_prompt_values_are_accepted_by_the_current_contract_table(profile, authority):
    table = getattr(contracts, "AUTHORITY_OUTCOMES", None)
    if table is None:
        pytest.skip("contracts has no AUTHORITY_OUTCOMES")
    vals = _outcome_values(S.load_prompt(sr.PROFILES[profile]).system.split("## Source profile", 1)[1])
    assert vals <= table[authority], vals - table[authority]
    assert PROC <= table[authority]                                # 계약도 §10 대로 dismissal_*·withdrawal 을 허용한다


def test_gd16_court_profile_still_forbids_dismissal_labels():
    court = S.load_prompt(sr.PROFILES["court_judgment"]).system
    assert "Never use `dismissal_procedural` or `dismissal_merits` for a court" in court


def test_gd16_common_prompt_states_the_pairings_in_one_place():
    common = S.load_prompt(sr.PROFILES["pac_election"]).system.split("## Source profile", 1)[0]
    assert re.search(r"outcome_scope `procedural`[^.]*only[^.]*`dismissal_procedural`, `withdrawal` or `unknown`", common)       # contracts.PROCEDURAL_SCOPE_OUTCOMES 와 같은 집합
    assert re.search(r"`dismissal_procedural` always takes outcome_scope `procedural`", common)
    assert re.search(r"`decision`, `correction` and `guideline`[^.]*(never|not)[^.]*`procedural`", common)
    assert re.search(r"`withdrawal`[^.]*settlement_or_adjudication `withdrawal`", common)
    assert re.search(r"`settlement`[^.]*settlement_or_adjudication `settlement`", common)
    assert "PAC and IENDC" in common
    for name in ("pac_election", "iendc_table", "pac_correction", "pac_adjustment"):            # 짝 규칙은 common 한 곳에만 둔다
        prof = S.load_prompt(sr.PROFILES[name]).system.split("## Source profile", 1)[1]
        assert "outcome_scope `procedural`" not in prof


def test_gd16_pairing_logic_is_not_duplicated_in_ingestion_code():
    import inspect
    src = inspect.getsource(sr)
    assert "requires outcome_scope" not in src and "cannot be recorded with outcome_type" not in src       # 정본은 contracts.validate_case_record


def test_gd16_changed_prompt_files_carry_new_version_lines():
    for name, floor in (("common.md", "2026-10-09.3"), ("pac_election.md", "2026-10-09.2"), ("iendc_table.md", "2026-10-09.3"),
                        ("pac_correction.md", "2026-10-09.2"), ("pac_adjustment.md", "2026-10-09.2")):
        first = (S.PROMPTS_DIR / name).read_text(encoding="utf-8").splitlines()[0]
        assert first >= f"version: {floor}", (name, first)


# =====================================================================================================================
# G-D17  correction_pair_confidence 는 원보도 인용 건수로 결정적으로 정한다
# =====================================================================================================================
def _art(status, quoted=True):
    return {"status": status, "quote": {"quote": "x", "page_id": "p", "start": 0, "end": 1} if status == "quoted_in_source" else None}


@pytest.mark.parametrize("model,orig,corr,want", [
    ("before_missing", "quoted_in_source", "described_only", "partial_quote"),      # 원보도 인용이 있으면 before_missing 일 수 없다
    ("before_missing", "quoted_in_source", "quoted_in_source", "partial_quote"),
    ("partial_quote", "described_only", "quoted_in_source", "before_missing"),      # 원보도 인용 0건
    ("full_pair_verified", "absent", "unknown", "before_missing"),
    ("unverified_pair", "described_only", "described_only", "before_missing"),
    ("unverified_pair", "quoted_in_source", "unknown", "partial_quote"),
    ("full_pair_verified", "quoted_in_source", "quoted_in_source", "partial_quote"),     # CX1-6: 전문 확인 정보가 없으면 모델 값도 partial_quote
    ("unverified_pair", "quoted_in_source", "quoted_in_source", "unverified_pair"),
    ("not_applicable", "unknown", "unknown", "not_applicable"),                      # 정정 사건이 아니라는 표시이고 어느 기사도 인용·서술되지 않았다
    ("not_applicable", "quoted_in_source", "unknown", "partial_quote"),
])
def test_gd17_pair_value_is_derived_from_the_quoted_original(model, orig, corr, want):
    assert sr.derive_pair_confidence(model, _art(orig), _art(corr)) == want


def test_gd17_a_contradictory_model_value_does_not_quarantine_the_case():
    page = "정정 전 기사: 가상시 시장이 공사비를 빼돌렸다고 보도했다.\n정정 후: 사실과 다르다."
    ctx = ctx_for({"pg": page}, profile="pac_correction", source_id="pac_correction_casebooks")
    d = {"case_number": None, "institution": "pac", "authority_type": "pac_decision", "first_page_id": "pg",
         "outcome_type": "correction", "outcome_scope": "whole", "correction_pair_confidence": "before_missing",
         "original_article": {"status": "quoted_in_source", "quote": {"page_id": "pg", "quote": "가상시 시장이 공사비를 빼돌렸다고 보도했다."}},
         "corrected_article": {"status": "described_only", "quote": None},
         "outcome": {"text": "정정", "speaker": "institution", "quote": {"page_id": "pg", "quote": "정정 후: 사실과 다르다."}}}
    co = sr.assemble_case(d, ctx, 0)
    assert co.record["correction_pair_confidence"] == "partial_quote" and co.record["verification_status"] == "unverified"
    assert not [e for e in co.errors if "before_missing" in e]


def test_gd17_no_original_quote_means_before_missing_even_if_model_claims_partial():
    page = "정정 후: 사실과 다르다. 정정 전 기사의 원문은 싣지 않았다."
    ctx = ctx_for({"pg": page}, profile="pac_correction", source_id="pac_correction_casebooks")
    d = {"case_number": None, "institution": "pac", "authority_type": "pac_decision", "first_page_id": "pg",
         "outcome_type": "correction", "outcome_scope": "whole", "correction_pair_confidence": "partial_quote",
         "corrected_article": {"status": "quoted_in_source", "quote": {"page_id": "pg", "quote": "정정 후: 사실과 다르다."}},
         "outcome": {"text": "정정", "speaker": "institution", "quote": {"page_id": "pg", "quote": "정정 전 기사의 원문은 싣지 않았다."}}}
    co = sr.assemble_case(d, ctx, 0)
    assert co.record["correction_pair_confidence"] == "before_missing" and co.errors == []


def test_gd17_merging_windows_never_leaves_before_missing_next_to_a_quoted_original():
    pa, pb = "사건 가-1 정정 후: 사실과 다르다. 정정 전 기사는 싣지 않았다.", "사건 가-1 계속. 정정 전 기사: 가상시 시장이 공사비를 빼돌렸다고 보도했다."
    ctx_a, ctx_b = ctx_for({"a": pa}, profile="pac_correction"), ctx_for({"b": pb}, profile="pac_correction")
    base = {"case_number": None, "institution": "pac", "authority_type": "pac_decision", "outcome_type": "correction", "outcome_scope": "whole",
            "correction_pair_confidence": "before_missing"}
    da = {**base, "first_page_id": "a", "corrected_article": {"status": "quoted_in_source", "quote": {"page_id": "a", "quote": "정정 후: 사실과 다르다."}},
          "outcome": {"text": "정정", "speaker": "institution", "quote": {"page_id": "a", "quote": "정정 전 기사는 싣지 않았다."}}}
    db = {**base, "first_page_id": "b", "original_article": {"status": "quoted_in_source", "quote": {"page_id": "b", "quote": "가상시 시장이 공사비를 빼돌렸다고 보도했다."}},
          "corrected_article": {"status": "described_only", "quote": None}, "starts_before_window": True}
    ca, cb = sr.assemble_case(da, ctx_a, 0), sr.assemble_case(db, ctx_b, 0)
    assert ca.record["correction_pair_confidence"] == "before_missing" and cb.record["correction_pair_confidence"] == "partial_quote"
    merged = sr.merge_cases(ca, cb, {"a": pa, "b": pb}, 0.25)
    rec = merged.record
    assert rec["available_original_article"]["status"] == "quoted_in_source"
    assert rec["correction_pair_confidence"] == "partial_quote"
    assert not [e for e in merged.errors if "before_missing" in e]
    ca2 = sr.assemble_case(da, ctx_a, 0)                                       # 반대 순서: 모델 값이 어디서 왔든 인용 건수가 정한다
    cb2 = sr.assemble_case({**db, "correction_pair_confidence": "full_pair_verified"}, ctx_b, 0)
    assert sr.merge_cases(cb2, ca2, {"a": pa, "b": pb}, 0.25).record["correction_pair_confidence"] == "partial_quote"       # CX1-6: 전문 확인 정보 없이 full 로 올리지 않는다


# =====================================================================================================================
# G-D18  잘린 JSON·invalid_json 은 같은 창을 반으로 나눠 다시 요청(최대 2단계), 그래도 실패하면 dead-letter
# =====================================================================================================================
BIG = {"p1": "첫째 쪽의 핵심 근거 문장이다. " + "반복되는 설명 문장입니다. " * 360, "p2": "둘째 쪽의 핵심 근거 문장이다. " + "반복되는 설명 문장입니다. " * 360}


def test_gd18_invalid_json_window_is_split_and_the_halves_succeed(manifest, corpus):
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None, pages=BIG, concurrency=1)
    p1, p2 = pids_of(att)

    def handler(b, n):
        user = b["messages"][1]["content"]
        if f"<<PAGE {p1}>>" in user and f"<<PAGE {p2}>>" in user:
            return 200, chat_payload("정상적인 JSON 이 아닌 응답입니다 {cases"), {}
        pid, sent = (p1, "첫째 쪽의 핵심 근거 문장이다.") if "첫째 쪽의 핵심" in user else (p2, "둘째 쪽의 핵심 근거 문장이다.")
        c = {"case_number": None, "institution": "pac", "authority_type": "pac_decision", "first_page_id": pid,
             "factual_background": [{"text": "요지", "speaker": "institution", "quote": q(pid, sent)}]}
        return 200, chat_payload({"cases": [c]}), {}
    fake.handler = handler
    res = go(st, manifest)
    assert res["windows_split"] == 1 and res["windows_dead"] == 0 and res["cases"] == 2 and res["coverage"] == 1.0
    assert fake.n_chat == 3 + 2
    assert sorted(j["unit"] for j in manifest.jobs()) == ["w0000", "w0000.0", "w0000.1"]
    sidecar = json.load(open(next((corpus / "structured").rglob("w0000@*.json")), encoding="utf-8"))
    assert sidecar["reason"] in ("invalid_json", "truncated_output") and len(sidecar["split"]) == 2


@pytest.mark.parametrize("bad", ["truncated", "invalid"])
def test_gd18_split_stops_after_two_levels_and_the_rest_is_dead_lettered(manifest, corpus, bad):
    h = (lambda b, n: (200, chat_payload('{"cases": [{"case_num', finish="length"), {})) if bad == "truncated" else \
        (lambda b, n: (200, chat_payload("이것은 JSON 이 아니다"), {}))
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, h, pages=BIG, concurrency=1)
    res = go(st, manifest)
    units = sorted(j["unit"] for j in manifest.jobs())
    assert units == sorted(["w0000", "w0000.0", "w0000.1", "w0000.0.0", "w0000.0.1", "w0000.1.0", "w0000.1.1"]), units
    assert max(u.count(".") for u in units) == 2
    assert res["windows_split"] == 3 and res["windows_dead"] == 4 and res["cases"] == 0
    assert len(manifest.dead_letters()) == 4 and all(dl["attempts"] == 3 for dl in manifest.dead_letters())
    assert fake.n_chat == 7 * 3
    assert manifest.select("SELECT status FROM attachments")[0]["status"] == "failed"
    assert manifest.failures()[0]["error_class"] == "dead_letter"


def test_gd18_split_children_are_cached_on_rerun_without_calls(manifest, corpus):
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None, pages=BIG, concurrency=1)
    p1, p2 = pids_of(att)

    def handler(b, n):
        user = b["messages"][1]["content"]
        if f"<<PAGE {p1}>>" in user and f"<<PAGE {p2}>>" in user:
            return 200, chat_payload("not json"), {}
        return 200, chat_payload({"cases": []}), {}
    fake.handler = handler
    go(st, manifest)
    n = fake.n_chat
    res2 = go(S.Structurer(manifest, prov, corpus, force=True), manifest)
    assert fake.n_chat == n and res2["windows_cached"] == 2


# =====================================================================================================================
# G-D19  오래된 HWP: 매직 바이트 기록, HWP 3.x 면 kordoc 1회 시험
# =====================================================================================================================
ROB = {"https://www.pac.or.kr/robots.txt": ROBOTS_OK, "https://www.iendc.go.kr/robots.txt": ROBOTS_OK}
U_OLD = "https://www.pac.or.kr/files/old.hwp"
HWP3 = b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + b"\x00" * 40 + "옛 한글 본문".encode("cp949")
KD_OK = {"kordoc": {"success": True, "metadata": {"pageMode": "layout"}, "pages": [{"pageNumber": 1, "markdown": "옛 한글 문서의 본문이 충분히 길게 들어 있는 쪽입니다"}]}}


@pytest.fixture()
def fetch_env(corpus, clock, monkeypatch):
    class E:
        pass
    e = E()
    e.corpus, e.clock, e.mp = corpus, clock, monkeypatch
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    e.doc, e.ids = ext_seed(m, "900", atts=[(1, U_OLD, "other", None, None, "old.hwp")])
    m.close()

    def run(body, runner, *argv, headers=None):
        monkeypatch.setattr(fetch_mod, "ToolRunner", lambda: runner)
        out = io.StringIO()
        t = FakeTransport({**ROB, U_OLD: (200, body, headers or {})})
        code = cli.main(["fetch", *argv], transport=t, clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus, log_stream=io.StringIO())
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    e.run = run
    return e


def _row(corpus, att_id):
    m = Manifest(corpus / "manifest.sqlite")
    try:
        return m.select("SELECT * FROM attachments WHERE att_id=?", (att_id,))[0], [dict(f) for f in m.failures() if f["target"] == att_id]
    finally:
        m.close()


def test_gd19_unknown_type_failure_records_the_magic_bytes(fetch_env):
    code, res = fetch_env.run(b"\x00\x01\x02\x03abcdefgh" + b"z" * 40, FakeRunner({}))
    assert res["result"]["errors"] == {"unknown_type": 1}
    row, fails = _row(fetch_env.corpus, fetch_env.ids[0])
    assert row["status"] == "failed" and fails[0]["error_class"] == "unknown_type"
    assert "magic=0001020361626364" in fails[0]["message"] and "00010203" in fails[0]["message"]


@pytest.mark.parametrize("blob,label", [(b"{\\rtf1\\ansi hello", "rtf"), (b"\x1f\x8b\x08\x00 gzip", "gzip"), (b"HWP Document File V3.00 \x1a\x01", "hwp3"),
                                        (b"\xef\xbb\xbfplain text file", "text"), (b"\xff\xd8\xff\xe0 jpeg", "jpeg")])
def test_gd19_magic_labels(blob, label):
    sig = fetch_mod.sniff_magic(blob)
    assert sig["label"] == label and sig["hex"] == blob[:16].hex()


def test_gd19_hwp3_supported_by_kordoc_is_stored_as_its_own_kind_and_extracts(fetch_env, monkeypatch):
    r = FakeRunner({HWP3[:30]: KD_OK})
    code, res = fetch_env.run(HWP3, r)
    assert code == 0 and res["result"]["fetched"] == 1 and res["result"]["errors"] == {}
    row, fails = _row(fetch_env.corpus, fetch_env.ids[0])
    assert row["status"] == "fetched" and row["detected_type"] == "hwp3" and row["raw_path"].endswith(".hwp3") and not fails
    assert (fetch_env.corpus / row["raw_path"]).read_bytes() == HWP3
    assert sum(1 for a in r.calls if a[0] == r.kordoc) == 1                                  # 시험은 1회
    assert not list((fetch_env.corpus / "tmp").glob("*")) if (fetch_env.corpus / "tmp").exists() else True      # 시험용 임시 파일은 남지 않는다
    # 추출 단계가 같은 형식을 kordoc 으로 읽는다
    monkeypatch.setattr(ext, "ToolRunner", lambda: r)
    out = io.StringIO()
    cli.main(["extract"], clock=fetch_env.clock, sleep=fetch_env.clock.sleep, stdout=out, corpus=fetch_env.corpus, log_stream=io.StringIO())
    assert json.loads(out.getvalue().strip().splitlines()[-1])["result"]["outcomes"] == {"extracted": 1}
    m = Manifest(fetch_env.corpus / "manifest.sqlite")
    try:
        assert [p["method"] for p in m.pages_of(att_id=fetch_env.ids[0])] == ["kordoc"]
    finally:
        m.close()


def test_gd19_kordoc_output_without_pages_keeps_table_blocks_via_the_markdown_body():
    """HWP 3.x 의 kordoc 출력(실측): pages·pageNumber 가 없고 표는 ``text`` 없는 table 블록이다. blocks 만 쓰면 표가 통째로 빠진다."""
    from press_review.ingestion.extract import hwp as hwp_mod
    md = "2005년도 2차 시정권고현황\n\n<table>\n<tr><td>구분</td><td>심의</td></tr>\n<tr><td>1차</td><td>29</td></tr>\n</table>\n\n라. 매체별 현황"
    data = {"success": True, "markdown": md, "metadata": {"version": "3.0"},
            "blocks": [{"type": "paragraph", "text": "2005년도 2차 시정권고현황"}, {"type": "table", "table": {"rows": 2, "cols": 2, "cells": []}},
                       {"type": "paragraph", "text": "라. 매체별 현황"}]}
    pages, mode = hwp_mod.kordoc_pages(data)
    assert len(pages) == 1 and "<td>29</td>" in pages[0][1] and "라. 매체별 현황" in pages[0][1] and mode == "unknown"
    plain = {"success": True, "markdown": "x", "blocks": [{"type": "paragraph", "text": "문단 하나", "pageNumber": 2}, {"type": "paragraph", "text": "문단 둘", "pageNumber": 3}]}
    assert hwp_mod.kordoc_pages(plain)[0] == [(2, "문단 하나"), (3, "문단 둘")]            # 표 블록이 없으면 쪽 번호별 묶음은 그대로


def test_gd19_hwp3_not_supported_is_failed_and_not_retried_endlessly(fetch_env):
    r = FakeRunner({HWP3[:30]: {"kordoc_rc": 1}})
    code, res = fetch_env.run(HWP3, r)
    assert res["result"]["errors"] == {"hwp3_unsupported": 1}
    row, fails = _row(fetch_env.corpus, fetch_env.ids[0])
    assert row["status"] == "failed" and row["raw_path"] is None and fails[0]["error_class"] == "hwp3_unsupported"
    assert "magic=" in fails[0]["message"] and "hwp3" in fails[0]["message"]
    assert fails[0]["retry_count"] > fetch_mod.MAX_RETRIES_BEFORE_SKIP                    # 같은 kordoc 으로는 다시 시험하지 않는다
    n = sum(1 for a in r.calls if a[0] == r.kordoc)
    _, res2 = fetch_env.run(HWP3, r)
    assert res2["result"]["candidates"] == 0 and sum(1 for a in r.calls if a[0] == r.kordoc) == n
    _, res3 = fetch_env.run(HWP3, r, "--retry-exhausted")                                  # kordoc 을 올린 뒤에는 명시적으로 다시 시험할 수 있다
    assert res3["result"]["candidates"] == 1 and sum(1 for a in r.calls if a[0] == r.kordoc) == n + 1


def test_gd19_without_kordoc_the_failure_is_retryable_and_not_labelled_unsupported(fetch_env):
    code, res = fetch_env.run(HWP3, FakeRunner({}, kordoc=None))
    row, fails = _row(fetch_env.corpus, fetch_env.ids[0])
    assert res["result"]["errors"] == {"no_hwp_extractor": 1} and fails[0]["retry_count"] <= fetch_mod.MAX_RETRIES_BEFORE_SKIP and row["status"] == "failed"


def test_gd19_dry_run_never_runs_the_trial(fetch_env, corpus):
    r = FakeRunner({HWP3[:30]: KD_OK})
    code, res = fetch_env.run(HWP3, r, "--dry-run")
    assert not [a for a in r.calls if a[0] == r.kordoc]


# =====================================================================================================================
# G-D20  P-T1 건수는 장부 질의로 보고한다(고정 숫자를 적지 않는다)
# =====================================================================================================================
def _seed_law(manifest, specs):
    manifest.ensure_source(L.SOURCE_ID, L.FAMILY, "법제처 판례", "https://www.law.go.kr/")
    for n, extra in specs:
        manifest.upsert_document(f"lawgo_prec:{n}", L.SOURCE_ID, title=f"판례{n}", extra=extra)
    L.assign_tiers(manifest)


def test_gd20_coverage_reports_p_t1_from_ledger_query_including_expanded(manifest):
    _seed_law(manifest, [
        (1, {"matched_terms": ["정정보도"], "source_scope": "supreme"}),                                    # 핵심어
        (2, {"matched_terms": ["기사", "명예훼손"], "source_scope": "supreme"}),                             # 언론어+인격권어
        (3, {"matched_terms": ["명예훼손"], "source_scope": "supreme"}),                                     # P-T2
        (4, {"matched_terms": ["의견"], "source_scope": "non_court_source"}),                              # P-T3
        (5, {"matched_terms": [], "source_scope": "supreme", "expanded_from": ["lawgo_prec:1"]}),          # 확장분: P-T1 로 처리
    ])
    cov = cov_mod.build_coverage(manifest, "2026-10-09T00:00:00")
    law = next(s for s in cov["sources"] if s["source_id"] == L.SOURCE_ID)
    pt = law["processing_tiers"]
    assert pt["counts"] == {"P-T1": 3, "P-T2": 1, "P-T3": 1, "none": 0} or pt["counts"] == {"P-T1": 3, "P-T2": 1, "P-T3": 1}
    assert pt["p_t1_total"] == 3 and pt["p_t1_by_rule"] == 2 and pt["p_t1_expanded_only"] == 1
    assert pt["by_scope"]["non_court_source"] == {"P-T3": 1}
    md = cov_mod.render_markdown(cov)
    assert "판례 처리 순위" in md and "P-T1" in md and "장부 질의" in md


def test_gd20_the_reported_number_follows_the_ledger_not_a_constant(manifest):
    _seed_law(manifest, [(1, {"matched_terms": ["정정보도"], "source_scope": "supreme"})])
    pt1 = cov_mod.build_coverage(manifest, "t")["sources"][0]["processing_tiers"]
    assert pt1["p_t1_total"] == 1
    manifest.upsert_document("lawgo_prec:2", L.SOURCE_ID, title="x", extra={"matched_terms": ["반론보도"], "source_scope": "supreme"})
    L.assign_tiers(manifest)
    pt2 = cov_mod.build_coverage(manifest, "t")["sources"][0]["processing_tiers"]
    assert pt2["p_t1_total"] == 2
    assert "P-T1" in pt2["definition"] and not re.search(r"\b4\d\d\b", pt2["definition"])             # 정의 문구에 고정 건수를 쓰지 않는다


def test_gd20_non_law_sources_have_no_processing_tiers_entry(manifest):
    manifest.ensure_source("pac_adjustment_casebooks", "pac_cases", "t", "https://www.pac.or.kr/")
    cov = cov_mod.build_coverage(manifest, "t")
    assert "processing_tiers" not in cov["sources"][0]

"""재게이트 뒤 검수에서 확정된 결함(V-FID-01~07, V-consistency-01·04·05·06) 수정 시험. 합성 텍스트이며 실제 사건이 아니다.

테스트 이름은 ``test_vfidNN_`` / ``test_vconNN_`` 으로 시작한다(``-k vfid01`` 로 결함 하나만 고른다).
"""
import re
from pathlib import Path

import pytest

import pdr_ing_paths  # noqa: F401
from ds_fakes import DSFake
from press_review import contracts
from press_review.ingestion import structure as S
from press_review.ingestion import structure_records as sr
from test_gd import prov_for
from test_ing_structure import go, handler_for, make_run
from test_p2_collect_gd import ctx_for

ROOT = Path(__file__).resolve().parents[2]


def dropped(co):
    return co.record["quote_check"]["dropped_fields"]


def draft(authority, institution, first, **kw):
    d = {"case_number": None, "institution": institution, "authority_type": authority, "first_page_id": first,
         "outcome_type": "unknown", "outcome_scope": "unclear"}
    d.update(kw)
    return d


def Q(pid, text):
    return {"page_id": pid, "quote": text}


def item(text, speaker, pid, quote):
    return {"text": text, "speaker": speaker, "quote": Q(pid, quote)}


# =====================================================================================================================
# V-FID-01  같은 결과 문구가 표의 여러 행에 반복될 때 인용은 그 사건의 행에 닿아야 한다
# =====================================================================================================================
ROWS = "".join(f"| 2024-0{i} | {n}일보 보도 기사 | 공정보도<br>협조요청 |\n" for i, n in enumerate("ABC", 1))
ROW_C = ROWS.index("C일보")


def _iendc_ctx():
    return ctx_for({"pg": ROWS}, profile="iendc_table", source_id="iendc_review_results")


def test_vfid01_repeated_result_text_lands_in_the_cases_own_row():
    d = draft("iendc_review", "iendc", "pg", outcome_type="decision", outcome_scope="whole",
              factual_background=[item("C 기사", "record", "pg", "C일보 보도 기사")],
              outcome=item("협조요청", "institution", "pg", "공정보도<br>협조요청"))
    co = sr.assemble_case(d, _iendc_ctx(), 0)
    assert co.record["outcome"]["quote"]["start"] > ROW_C, "the outcome quote must be the cell of row C, not the first row"
    assert co.record["outcome"]["quote"]["quote"] == "공정보도<br>협조요청"


def test_vfid01_an_ambiguous_quote_found_before_the_unique_one_still_goes_to_the_unique_ones_row():
    d = draft("iendc_review", "iendc", "pg", outcome_type="decision", outcome_scope="whole",
              factual_background=[item("결과 문구", "record", "pg", "공정보도<br>협조요청")],
              outcome=item("C 기사", "institution", "pg", "C일보 보도 기사"))
    co = sr.assemble_case(d, _iendc_ctx(), 0)
    assert co.record["factual_background"][0]["quote"]["start"] > ROW_C


def test_vfid01_without_any_anchor_the_first_match_is_used_but_reported():
    d = draft("iendc_review", "iendc", "pg", outcome_type="decision", outcome_scope="whole",
              outcome=item("협조요청", "institution", "pg", "공정보도<br>협조요청"))
    co = sr.assemble_case(d, _iendc_ctx(), 0)
    assert co.record["outcome"]["quote"]["start"] == ROWS.index("공정보도")
    assert "outcome:ambiguous_quote_first_match" in dropped(co)


def test_vfid01_a_unique_match_is_not_reported_as_ambiguous():
    d = draft("iendc_review", "iendc", "pg", outcome_type="decision", outcome_scope="whole",
              outcome=item("C 기사", "institution", "pg", "C일보 보도 기사"))
    co = sr.assemble_case(d, _iendc_ctx(), 0)
    assert not [x for x in dropped(co) if "ambiguous" in x]


def test_vfid01_pagetext_find_all_lists_every_occurrence():
    pt = sr.PageText("가나다 라마 가나다\n라마 가나다")
    assert [s for s, _e in pt.find_all("가나다")] == [0, 7, 14]


# =====================================================================================================================
# V-FID-02  여러 사건이 같은 구간을 결론으로 인용하면 그 구간은 사건의 유일한 근거가 될 수 없다
# =====================================================================================================================
BOARD = ("시정권고 현황(제8차)\n\n■ 시정권고 결정 67건\n\n 1   제2099   가상일간지   시정권고\n      -901호                결정\n"
         " 2   제2099   예시신문    시정권고\n      -902호                결정\n 3   제2099   가상연예뉴스   시정권고\n      -903호                결정\n")


def _board_batch():
    ctx = ctx_for({"pg": BOARD}, profile="pac_correction", source_id="pac_board_correction_status")
    out = []
    for k, extra in enumerate((None, None, "가상연예뉴스")):
        kw = {"outcome_type": "correction", "outcome_scope": "whole", "outcome": item("시정권고 결정", "institution", "pg", "시정권고 결정")}
        if extra:
            kw["factual_background"] = [item("매체명", "record", "pg", extra)]
        out.append(sr.assemble_case(draft("pac_decision", "pac", "pg", **kw), ctx, k))
    return ctx, out


def test_vfid02_a_header_span_shared_as_outcome_cannot_be_the_only_support():
    ctx, batch = _board_batch()
    assert all(co.record["outcome"]["quote"]["start"] == BOARD.index("시정권고 결정") for co in batch)       # 수정 전 전제: 셋 다 머리글을 인용한다
    n = sr.drop_shared_outcome_quotes(batch, {"pg": BOARD}, ctx.quarantine_rate, {})
    assert n == 2
    for co in batch[:2]:
        assert co.record["outcome"] is None and co.record["outcome_type"] == "unknown" and co.record["verification_status"] == "quarantined"
        assert "outcome:shared_outcome_quote" in dropped(co)
        assert any("no grounded content" in e for e in co.errors)
        assert co.record["quote_check"]["matched"] == 0


def test_vfid02_a_case_with_its_own_fact_quote_keeps_the_shared_outcome():
    ctx, batch = _board_batch()
    sr.drop_shared_outcome_quotes(batch, {"pg": BOARD}, ctx.quarantine_rate, {})
    own = batch[2]
    assert own.record["outcome"] is not None and own.record["verification_status"] == "unverified" and own.errors == []


def test_vfid02_distinct_outcome_spans_are_not_touched():
    page = "사건 하나는 조정이 성립하였다.\n사건 둘은 조정이 성립하지 않았다.\n"
    ctx = ctx_for({"pg": page}, profile="pac_adjustment")
    batch = [sr.assemble_case(draft("pac_settlement", "pac", "pg", outcome_type="settlement", outcome_scope="whole",
                                    outcome=item("o", "institution", "pg", q)), ctx, k)
             for k, q in enumerate(("조정이 성립하였다.", "조정이 성립하지 않았다."))]
    assert sr.drop_shared_outcome_quotes(batch, {"pg": page}, ctx.quarantine_rate, {}) == 0
    assert all(co.record["outcome"] is not None for co in batch)


def test_vfid02_structure_run_applies_the_guard(manifest, corpus):
    """창 하나에서 두 사건이 같은 결론 구간만 인용하면 둘 다 격리된다(구조화 실행 경로)."""
    pages = {"p1": "시정권고 현황\n\n■ 시정권고 결정 2건\n\n1 제2099-901호 가상일간지\n2 제2099-902호 예시신문\n", "p2": "끝.\n"}
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None, pages=pages)
    p1 = f"{att}@p1"
    cases = [draft("pac_settlement", "pac", p1, outcome_type="settlement", outcome_scope="whole", settlement_or_adjudication="settlement",
                   outcome=item("결정", "institution", p1, "시정권고 결정 2건")) for _ in range(2)]
    cases[0]["case_number"], cases[1]["case_number"] = None, None
    from ds_fakes import chat_payload
    fake.handler = lambda b, n: (200, chat_payload({"cases": cases}, hit=0, miss=1000, completion=400), {})
    res = go(st, manifest)
    assert res["cases"] == 2 and res["cases_quarantined"] == 2


# =====================================================================================================================
# V-FID-03  -layout 표에서 줄로 갈라진 사건번호는 원문에 있는 것으로 인정한다
# =====================================================================================================================
LAYOUT_NO = ("순번   의결번호        매체명\n"
             "     제2099      가상일간지       시정권고\n 1                  07월 05일\n     -901호    (Gasang Daily)    결정\n"
             "     제2099     예시신문       시정권고\n 2                  07월 06일\n     -902호    (Yesi Times)  결정\n"
             "\n\n\n\n\n     -999호   먼 곳의 꼬리\n")


def _cn_case(number):
    ctx = ctx_for({"pg": LAYOUT_NO}, profile="pac_correction", source_id="pac_board_correction_status")
    d = draft("pac_decision", "pac", "pg", case_number=number, outcome_type="correction", outcome_scope="whole",
              factual_background=[item("매체", "record", "pg", "가상일간지")])
    return sr.assemble_case(d, ctx, 0)


def test_vfid03_case_number_split_over_lines_is_kept():
    co = _cn_case("제2099-901호")
    assert co.record["case_number"] == "제2099-901호" and co.record["case_id"].endswith(":제2099-901호")
    assert not [x for x in dropped(co) if x.startswith("case_number")]


def test_vfid03_the_tail_must_follow_the_head_closely_and_in_order():
    assert _cn_case("제2099-999호").record["case_number"] is None           # 꼬리 '-999호' 는 있으나 머리에서 멀다
    assert _cn_case("제2099-555호").record["case_number"] is None           # 꼬리가 아예 없다(지어낸 번호)
    assert "case_number:not_in_source" in dropped(_cn_case("제2099-555호"))


def test_vfid03_dash_at_the_end_of_the_head_is_also_a_valid_split():
    page = "     제2018-      인터넷\n  59       내외일보\n     1265호     뉴스\n"
    ctx = ctx_for({"pg": page}, profile="pac_correction", source_id="pac_board_correction_status")
    d = draft("pac_decision", "pac", "pg", case_number="제2018-1265호", factual_background=[item("매체", "record", "pg", "내외일보")])
    assert sr.assemble_case(d, ctx, 0).record["case_number"] == "제2018-1265호"


def test_vfid03_later_history_case_numbers_use_the_same_rule():
    page = "     제2099      본건\n     -901호    후속\n"
    ctx = ctx_for({"pg": page}, profile="pac_correction", source_id="pac_board_correction_status")
    d = draft("pac_decision", "pac", "pg", factual_background=[item("본건", "record", "pg", "본건")],
              later_history=[{"relation": "related_case", "case_number": "제2099-901호", "note": "관련", "quote": None}])
    assert sr.assemble_case(d, ctx, 0).record["appeal_or_later_history"][0]["case_number"] == "제2099-901호"


# =====================================================================================================================
# V-FID-04  조문 번호가 인용 구간과 같은 쪽 원문에 없으면 그 조문 항목을 버린다
# =====================================================================================================================
GARBLED = "【가상선거보도법 저 18조(가상 보도의무)， 가상기준 저 11 조(공정성) 각 위반】\n주문과 같이 결정한다.\n"
BENIGN = "가상심의기준\n제1조(가상 보호 조항)\n② 언론은 가상의 자료나 서신 내용 등을 당사자의 동의 없이 공표하여서는 아니 된다.\n"


def _prov(page, citation, quote, profile="pac_election", authority="pac_election_review", inst="pac"):
    ctx = ctx_for({"pg": page}, profile=profile, source_id="")
    d = draft(authority, inst, "pg", factual_background=[item("본문", "record", "pg", page.splitlines()[-1] if page.splitlines()[-1] else page.splitlines()[0])],
              legal_provisions=[{"citation": citation, "quote": Q("pg", quote)}])
    return sr.assemble_case(d, ctx, 0)


def test_vfid04_article_number_misread_from_a_garbled_text_layer_is_dropped():
    co = _prov(GARBLED, "가상선거보도법 제18조(가상 보도의무)", "가상선거보도법 저 18조(가상 보도의무)")
    assert co.record["legal_provisions"] == []
    assert "legal_provisions[0]:citation_not_in_source" in dropped(co)
    assert co.record["quote_check"]["total"] == co.record["quote_check"]["matched"] == 1        # 인용은 일치했으므로 실패율에 넣지 않는다(남은 근거는 본문 1건)


def test_vfid04_a_citation_whose_article_is_printed_on_the_page_is_kept_even_if_the_quote_is_only_the_clause():
    co = _prov(BENIGN, "가상심의기준 제1조 (가상 보호 조항) 제2항", "② 언론은 가상의 자료나 서신 내용 등을 당사자의 동의 없이 공표하여서는 아니 된다.",
               profile="pac_correction", authority="pac_decision")
    assert len(co.record["legal_provisions"]) == 1


def test_vfid04_article_with_a_sub_number_must_match_as_a_whole():
    page = "가 법 제18조의 3 제3항에 따른다. 다른 곳에 제18조가 있다.\n"
    ok = _prov(page, "가 법 제18조의3 제3항", "가 법 제18조의 3 제3항에 따른다.")
    assert len(ok.record["legal_provisions"]) == 1
    bad = _prov(page, "가 법 제18조의4 제3항", "가 법 제18조의 3 제3항에 따른다.")
    assert bad.record["legal_provisions"] == []


# =====================================================================================================================
# V-FID-05  2단 조판의 좌우 단을 줄마다 이어 붙인 인용은 통과시키지 않는다
# =====================================================================================================================
def _two_col(left, right, width=42, gap=7):
    return "\n".join(l.ljust(width) + " " * gap + r for l, r in zip(left, right)) + "\n"


LEFT = ["고향은 어머니의 따뜻한 품입니다. 그리고 보듬", "어 주는 안식처입니다. 차가운 눈일지라도 고향이라", "는 이름 앞에서는 차라리 포근해집니다",
        "새로운 일꾼이 필요한 때입니다. 지역의 발전을", "함께 이끌 사람을 뽑는 일은 우리 모두의", "일입니다. 그래서 이번 선거가 중요합니다.",
        "믿음직한 후보를 선택해 주십시오. 여러분의", "한 표가 고향의 내일을 바꿉니다."]
RIGHT = ["우리 전라북도민과 전주시민에게는 그 어느 선거보", "다 이번 총선이 매우 중요합니다.", "우리 고장을 분열과 배신의 땅으로 만들어 버린",
         "낡은 정치를 이제는 끝내야 한다는 목소리가", "곳곳에서 커지고 있습니다. 변화를 바라는", "시민들의 뜻을 모아 새 길을 열겠습니다.",
         "깨끗하고 유능한 일꾼이 되겠다고 약속드립", "니다. 많은 성원을 부탁드립니다."]
COLS = _two_col(LEFT, RIGHT)


def _col_case(page, quote, source_id=""):
    ctx = ctx_for({"pg": page}, profile="pac_election", source_id=source_id)
    d = draft("pac_election_review", "pac", "pg", original_article={"status": "quoted_in_source", "quote": Q("pg", quote)},
              outcome_type="decision", outcome_scope="whole", outcome=item("o", "institution", "pg", "우리 고장을 분열과 배신의 땅으로 만들어 버린"))
    return sr.assemble_case(d, ctx, 0)


def test_vfid05_quote_that_joins_left_and_right_columns_is_rejected():
    joined = COLS.splitlines()[0] + "\n" + COLS.splitlines()[1]
    co = _col_case(COLS, joined)
    assert co.record["available_original_article"]["status"] == "described_only"
    assert any(x.endswith(":column_join") for x in dropped(co))
    assert co.record["correction_pair_confidence"] != "partial_quote"


def test_vfid05_a_quote_inside_one_column_line_passes():
    co = _col_case(COLS, "고향은 어머니의 따뜻한 품입니다.")
    assert co.record["available_original_article"]["status"] == "quoted_in_source"


def test_vfid05_two_aligned_redaction_blanks_on_an_otherwise_gapless_page_are_not_columns():
    a, b = "원고 소유의 이 사건 건물과 소외 신", "이라는 위 신"
    page = a + " " * 6 + "소유토지를 배경으로 한 영상 및 사람이\n" + b.ljust(len(a)) + " " * 6 + "의 인터뷰 내용을 방송함으로써\n그 밖의 서술은 평범한 한 줄이다.\n"
    ctx = ctx_for({"pg": page}, profile="pac_judgment_cases", source_id="")
    d = draft("research", "pac", "pg", original_article={"status": "quoted_in_source", "quote": Q("pg", page.strip())})
    assert sr.assemble_case(d, ctx, 0).record["available_original_article"]["status"] == "quoted_in_source"


def test_vfid05_redaction_blanks_that_do_not_line_up_are_not_columns():
    page = "원고 조      가 실제로 세계 최초로 제작하였고\n위 광고에는 이         가 소개되어 있는데도 피고들이\n그 밖의 일부 보도기사 중 위 원고       가 라고 일부 소개되어 있음을\n"
    ctx = ctx_for({"pg": page}, profile="pac_judgment_cases", source_id="")
    d = draft("research", "pac", "pg", original_article={"status": "quoted_in_source", "quote": Q("pg", page.strip())})
    assert sr.assemble_case(d, ctx, 0).record["available_original_article"]["status"] == "quoted_in_source"


def test_vfid05_table_sources_are_exempt():
    page = _two_col(["제2099-901호 가상일간지", "제2099-902호 예시신문"], ["시정권고 결정", "시정권고 결정"])
    ctx = ctx_for({"pg": page}, profile="pac_correction", source_id="pac_board_correction_status")
    d = draft("pac_decision", "pac", "pg", factual_background=[item("행", "record", "pg", page.strip())])
    assert len(sr.assemble_case(d, ctx, 0).record["factual_background"]) == 1


# =====================================================================================================================
# V-FID-06  원보도를 서술만 하고 인용하지 않았으면 모델이 not_applicable 이라 해도 before_missing
# =====================================================================================================================
def _art(status):
    return {"status": status, "quote": {"quote": "x", "page_id": "p", "start": 0, "end": 1} if status == "quoted_in_source" else None}


@pytest.mark.parametrize("model,orig,corr,want", [
    ("not_applicable", "described_only", "absent", "before_missing"),
    ("not_applicable", "described_only", "described_only", "before_missing"),
    ("not_applicable", "absent", "described_only", "before_missing"),
    ("not_applicable", "described_only", "quoted_in_source", "before_missing"),
    ("not_applicable", "absent", "unknown", "not_applicable"),
    ("not_applicable", "unknown", "unknown", "not_applicable"),
    ("not_applicable", "quoted_in_source", "described_only", "partial_quote"),
    ("unverified_pair", "described_only", "absent", "before_missing"),
])
def test_vfid06_derive_pair_confidence_exception_needs_neither_article_quoted_nor_described(model, orig, corr, want):
    assert sr.derive_pair_confidence(model, _art(orig), _art(corr)) == want


# =====================================================================================================================
# V-FID-07  speaker article 항목은 기사 자체의 문구만 담는다
# =====================================================================================================================
DESC = "위 기사는 가상의 사건과 관련해 가상인물 갑이 소유한 건물의 사진을 동의 없이 보도하였다.\n신청인은 기사가 사실과 다르다고 주장한다.\n"


def test_vfid07_commission_description_of_the_article_is_not_speaker_article():
    ctx = ctx_for({"pg": DESC}, profile="pac_correction", source_id="pac_correction_casebooks")
    d = draft("pac_decision", "pac", "pg", disputed_spans=[item("기사 요지", "article", "pg", "위 기사는 가상의 사건과 관련해 가상인물 갑이 소유한 건물의 사진을 동의 없이 보도하였다.")],
              claims_or_requested_remedy=[item("주장", "claimant", "pg", "신청인은 기사가 사실과 다르다고 주장한다.")])
    co = sr.assemble_case(d, ctx, 0)
    assert co.record["disputed_spans"] == []
    moved = co.record["factual_background"]
    assert len(moved) == 1 and moved[0]["speaker"] == "institution" and moved[0]["quote"]["quote"].startswith("위 기사는")
    assert "disputed_spans[0]:rerouted_institution_description" in dropped(co)
    assert co.record["quote_check"]["total"] == co.record["quote_check"]["matched"] == 2      # 버린 것이 아니므로 실패율에 넣지 않는다
    assert contracts.validate_case_record(co.record, {"pg": DESC}) == []


def test_vfid07_court_judgment_description_is_attributed_to_the_court():
    ctx = ctx_for({"pg": DESC}, profile="court_judgment", source_id="")
    d = draft("judgment", "district_court", "pg", article_claims=[item("기사 요지", "article", "pg", "위 기사는 가상의 사건과 관련해 가상인물 갑이 소유한 건물의 사진을 동의 없이 보도하였다.")])
    co = sr.assemble_case(d, ctx, 0)
    assert co.record["article_claims"] == [] and co.record["factual_background"][0]["speaker"] == "court_majority"


def test_vfid07_article_item_with_the_same_span_as_a_party_claim_is_dropped_from_the_article_field():
    page = "원고는 이 사건 기사가 원고의 명예를 훼손하였다고 주장한다.\n"
    ctx = ctx_for({"pg": page}, profile="court_judgment", source_id="")
    same = "이 사건 기사가 원고의 명예를 훼손하였다고 주장한다."
    d = draft("judgment", "district_court", "pg", article_claims=[item("기사", "article", "pg", same)],
              claims_or_requested_remedy=[item("주장", "claimant", "pg", same)])
    co = sr.assemble_case(d, ctx, 0)
    assert co.record["article_claims"] == [] and len(co.record["claims_or_requested_remedy"]) == 1
    assert "article_claims[0]:span_shared_with_party_claim" in dropped(co)


# =====================================================================================================================
# 조립 규칙 버전: 올리면 캐시된 응답을 API 호출 없이 다시 조립한다
# =====================================================================================================================
def test_assembly_version_bump_reassembles_without_calling_the_provider(manifest, corpus, monkeypatch):
    doc, att, texts, fake, prov, st = make_run(manifest, corpus, lambda b, n: None)
    fake.handler = handler_for(att)
    go(st, manifest)
    old = manifest.get_case(f"{att}:2001-조정-1")["record"]["structurer"]["version"]
    assert fake.n_chat == 1 and f"|asm={S.ASSEMBLY_VERSION}" in old
    n_jobs = len(manifest.jobs())
    monkeypatch.setattr(S, "ASSEMBLY_VERSION", "a-test")
    res = go(S.Structurer(manifest, prov, corpus), manifest)
    assert fake.n_chat == 1 and res["skipped_unchanged"] == 0 and res["processed"] == 1 and res["windows_called"] == 0
    new = manifest.get_case(f"{att}:2001-조정-1")["record"]["structurer"]["version"]
    assert new.endswith("|asm=a-test") and new != old and len(manifest.jobs()) == n_jobs
    assert manifest.select("SELECT structurer_version FROM attachments")[0]["structurer_version"] == new


# =====================================================================================================================
# V-consistency-01  pac_standards_* 는 guideline (G-D21), 위원회 규칙은 statute
# =====================================================================================================================
def test_vcon01_chunk_only_authority_follows_gd21():
    assert sr.CHUNK_ONLY_SOURCES["pac_standards_correction_page"] == "guideline"
    assert sr.CHUNK_ONLY_SOURCES["pac_standards_election_page"] == "guideline"
    assert sr.CHUNK_ONLY_SOURCES["pac_board_committee_rules"] == "statute"
    assert "guideline is not in the schema" not in Path(sr.__file__).read_text(encoding="utf-8")


def test_vcon01_index_accepts_guideline_chunks():
    from press_review import corpus
    assert "guideline" in corpus.AUTHORITY_TYPES


# =====================================================================================================================
# V-consistency-04  pac_adjustment 프롬프트의 pac_settlement 값 목록 == 계약 표 (G-D16)
# =====================================================================================================================
def test_vcon04_adjustment_prompt_lists_exactly_the_contract_values_for_pac_settlement():
    body = S.load_prompt(sr.PROFILES["pac_adjustment"]).system.split("## Source profile", 1)[1]
    lines = [l for l in body.splitlines() if "outcome_type for `pac_settlement`" in l]
    assert len(lines) == 1
    vals = set(re.findall(r"`([a-z_]+)`", lines[0].split(":", 1)[1].split(". ")[0]))
    assert vals == contracts.AUTHORITY_OUTCOMES["pac_settlement"]
    assert "is never a settlement" not in body


# =====================================================================================================================
# V-consistency-05·06  문서가 코드·계약과 같은 말을 한다
# =====================================================================================================================
def _doc(name):
    return (ROOT / "docs" / name).read_text(encoding="utf-8")


def test_vcon05_gd2_names_decision_type_not_outcome_scope_for_court_dispositions():
    row = next(l for l in _doc("P2_DESIGN.md").splitlines() if l.startswith("| G-D2 "))
    assert "decision_type" in row and "outcome_scope에 원문 그대로" not in row


def test_vcon06_p3_and_p0_use_the_reference_date_and_p2_states_the_rerun_policy():
    p3, p0, p2 = _doc("P3_DESIGN.md"), _doc("P0_DESIGN.md"), _doc("P2_DESIGN.md")
    assert "decision_date ≤ as_of" not in p3 and "reference_date" in p3
    assert "G-D23" in p0
    assert "ASSEMBLY_VERSION" in p2 and "CODE_VERSION" in p2

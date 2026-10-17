"""G-D26(docs/P2_DESIGN.md §11): 표 PDF 는 첨부 단위로 추출기를 고른다. 결정적 표 감지(같은 칸 구분 패턴의 반복 행)가 되면 kordoc, 아니면 소스 기본값.
표 행 사건의 인용은 행 고유 칸을 최소 1개 포함해야 하고, 머리줄만 인용한 사건은 격리한다. 합성 텍스트, 가짜 실행기, 네트워크 없음.
"""
import json

import pytest

import pdr_ing_paths  # noqa: F401
from ext_helpers import make_pdf, place_raw
from ext_helpers import seed as ext_seed
from press_review.ingestion import extract as ext
from press_review.ingestion import structure_records as sr
from press_review.ingestion.extract import layout
from test_gd import KdRunner
from test_ing_extract_run import env  # noqa: F401  (fixture)
from test_p2_collect_gd import ctx_for

STATUS_SRC, STATUS_FAM = "pac_board_correction_status", "pac_cases"


def table_text(rows=8, tag=""):
    """-layout 텍스트처럼 칸이 3칸 이상 공백으로 갈라지고 열 위치가 행마다 같은 합성 표."""
    cols = (6, 14, 18, 14, 10, 28, 14, 8)
    head = ["순번", "의결번호", "매체명", "보도일", "지면", "기사제목", "침해유형", "결과"]
    out = ["", "".join(h.ljust(w) for h, w in zip(head, cols)).rstrip()]
    for i in range(1, rows + 1):
        cells = [str(i), f"제2021-{800 + i}호", f"매체{i}{tag}", f"07월 {i:02d}일", "연예면", f"합성 기사 제목 {i}번", "사생활 보호 등", "시정권고"]
        out.append("".join(c.ljust(w) for c, w in zip(cells, cols)).rstrip())
    return "\n".join(out)


def prose_text(lines=20):
    return "\n".join(f"이것은 합성 본문 {i}번째 줄이며 공백은 한 칸씩만 있고 칸 구분은 없다 그래서 표가 아니다" for i in range(lines))


def two_col_text(lines=20):
    return "\n".join(f"{'왼쪽 단의 합성 문장 ' + str(i):<40}     {'오른쪽 단의 합성 문장 ' + str(i)}" for i in range(lines))


# ------------------------------------------------------------------ 순수 감지
def test_gd26_repeated_rows_with_the_same_cell_gaps_are_a_table_page():
    assert layout.table_page(table_text(8)) is True


@pytest.mark.parametrize("text", [prose_text(), two_col_text(), table_text(3), "", "한 줄"])
def test_gd26_prose_two_columns_and_short_tables_are_not_table_pages(text):
    assert layout.table_page(text) is False                  # 2단 쪽은 줄마다 간격이 하나뿐이다


def test_gd26_a_page_with_a_few_loosely_spaced_lines_is_not_a_table():
    text = prose_text(12) + "\n" + "\n".join(f"{'가나다':<10}      {'라마바':<10}      {'사아자'}   {i}" for i in range(2))
    assert layout.table_page(text) is False


def test_gd26_pdf_is_a_table_when_at_least_half_of_the_text_pages_are_table_pages():
    pages = [table_text(8), table_text(9), prose_text(), "", "   "]
    v = layout.table_pdf_verdict(pages)
    assert v["pages"] == 3 and v["table_pages"] == 2 and v["is_table"] is True
    assert layout.table_pdf_verdict([table_text(8), prose_text(), prose_text()])["is_table"] is False
    assert layout.table_pdf_verdict([])["is_table"] is False and layout.table_pdf_verdict(["", " "])["is_table"] is False


# ------------------------------------------------------------------ 선택·해시
def test_gd26_only_the_correction_status_source_selects_per_attachment():
    assert ext.TABLE_AUTO_SOURCES == frozenset({STATUS_SRC})


def test_gd26_input_hash_tag_names_the_policy_not_the_detected_result():
    sig = lambda s, **kw: ext.extractor_signature({"source_id": s, "detected_type": "pdf"}, **kw)  # noqa: E731
    assert sig(STATUS_SRC) == "x:auto-table-v1"
    assert sig(STATUS_SRC, extractor="kordoc") == "x:kordoc"                      # 명시 선택은 그 선택의 꼬리표
    assert sig("pac_adjustment_casebooks") == "x:pdftotext" and sig("pac_board_committee_rules") == ""
    assert ext.extractor_signature({"source_id": STATUS_SRC, "detected_type": "hwp"}) == ""


def _status_pdf(env, pages, *, key="500", n=None):
    doc, (a1,) = ext_seed(env.m, key, source=STATUS_SRC, family=STATUS_FAM, atts=[(1, "https://www.pac.or.kr/files/S.pdf", "full", None, "application/pdf", "PDF")])
    blob = make_pdf([f"status table {key} page {i + 1}" for i in range(len(pages))])
    place_raw(env.m, env.corpus, a1, blob, family=STATUS_FAM)
    kd = [f"| 순번 | 의결번호 |\n| --- | --- |\n| {i + 1} | 제2021-{800 + i}호 |" for i in range(len(pages))]
    return doc, a1, blob, {"pages": pages, "kd_pages": kd}


def test_gd26_table_attachment_is_read_with_kordoc_and_the_choice_is_recorded(env):
    doc, a1, blob, spec = _status_pdf(env, [table_text(8), table_text(9)])
    r = KdRunner({blob: spec})
    code, res = env.run(r)
    assert code == 0 and res["result"]["outcomes"] == {"extracted": 1}
    rows = env.m.pages_of(att_id=a1)
    assert [p["method"] for p in rows] == ["kordoc", "kordoc"]
    note = json.loads(env.m.get_document(doc)["extra_json"])["p2_extract"]
    assert note["pdf_extractor"] == "kordoc" and note["pdf_extractor_selection"] == "table_detected"
    assert json.loads(env.m.select("SELECT extra_json FROM documents WHERE doc_id=?", (doc,))[0]["extra_json"])["p2_extract"]["table_pages"] == "2/2"
    _, res2 = env.run(r)
    assert res2["result"]["outcomes"] == {"skipped": 1}                       # 같은 입력이면 다시 하지 않는다(꼬리표는 정책 이름)


def test_gd26_non_table_attachment_keeps_the_source_default_and_never_calls_kordoc(env):
    doc, a1, blob, spec = _status_pdf(env, [prose_text(), two_col_text()], key="501")
    r = KdRunner({blob: spec})
    code, res = env.run(r)
    assert code == 0
    assert [p["method"] for p in env.m.pages_of(att_id=a1)] == ["pdftotext", "pdftotext"]
    assert not [a for a in r.calls if a[0] == "/fake/kordoc"]
    note = json.loads(env.m.get_document(doc)["extra_json"])["p2_extract"]
    assert note["pdf_extractor"] == "pdftotext_layout" and note["pdf_extractor_selection"] == "table_not_detected"


def test_gd26_explicit_extractor_option_wins_and_skips_detection(env):
    doc, a1, blob, spec = _status_pdf(env, [table_text(8)], key="502")
    r = KdRunner({blob: spec})
    code, res = env.run(r, "--extractor", "pdftotext_layout")
    assert code == 0 and [p["method"] for p in env.m.pages_of(att_id=a1)] == ["pdftotext"]
    assert r.count("pdftotext") == 1 + 0 and not [a for a in r.calls if a[0] == "/fake/kordoc"]      # 감지용 추가 호출이 없다(쪽 추출 1회뿐)
    note = json.loads(env.m.get_document(doc)["extra_json"])["p2_extract"]
    assert "pdf_extractor_selection" not in note


def test_gd26_other_sources_are_not_inspected(env):
    doc, (a1,) = ext_seed(env.m, "503", source="pac_adjustment_casebooks", family=STATUS_FAM, atts=[(1, "https://www.pac.or.kr/files/C.pdf", "full", None, "application/pdf", "PDF")])
    blob = make_pdf(["casebook page 1"])
    place_raw(env.m, env.corpus, a1, blob, family=STATUS_FAM)
    r = KdRunner({blob: {"pages": [table_text(8)], "kd_pages": ["x"]}})
    env.run(r)
    assert r.count("pdftotext") == 1 and not [a for a in r.calls if a[0] == "/fake/kordoc"]
    note = json.loads(env.m.get_document(doc)["extra_json"])["p2_extract"]
    assert "pdf_extractor_selection" not in note


# ------------------------------------------------------------------ 행 고유 칸 인용
STATUS_PAGE = ("| 순번 | 의결번호 | 기사제목 | 결과 |\n| --- | --- | --- | --- |\n"
               "| 1 | 제2021-813호 | 합성 기사 제목 첫째 | 시정권고 |\n"
               "| 2 | 제2021-814호 | 합성 기사 제목 둘째 | 시정권고 |\n"
               "| 3 | 제2021-815호 | 합성 기사 제목 셋째 | 시정권고 |\n")
HEADER_PAGE_TAIL = "■ 시정권고 결정 3건\n" + STATUS_PAGE


def _case(ctx, k, *, case_number=None, quotes=(), outcome=None):
    d = {"case_number": case_number, "institution": "pac", "authority_type": "pac_decision", "first_page_id": "pg",
         "article_claims": [{"text": "기사", "speaker": "article", "quote": {"page_id": "pg", "quote": q}} for q in quotes]}
    if outcome:
        d["outcome"] = {"text": "결론", "speaker": "institution", "quote": {"page_id": "pg", "quote": outcome}}
        d["outcome_type"] = "correction"
        d["outcome_scope"] = "whole"
    return sr.assemble_case(d, ctx, k)


def _check(quote_sets, page=HEADER_PAGE_TAIL, source_id=STATUS_SRC):
    ctx = ctx_for({"pg": page}, profile="pac_correction", source_id=source_id)
    cases = [_case(ctx, k, **kw) for k, kw in enumerate(quote_sets)]
    pages = {pid: pt for pid, pt in ctx.pages.items()}
    n = sr.require_row_unique_quote(cases, pages, source_id)
    return cases, n


def test_gd26_case_citing_only_the_table_header_is_quarantined_with_a_named_reason():
    cases, n = _check([{"case_number": "제2021-813호", "quotes": ["■ 시정권고 결정 3건"]}])
    assert n == 1 and cases[0].record["verification_status"] == "quarantined"
    assert any(e.startswith("row_unique_quote") for e in cases[0].errors)


def test_gd26_citing_the_own_case_number_cell_or_a_page_unique_title_cell_passes():
    cases, n = _check([{"case_number": "제2021-813호", "quotes": ["제2021-813호"]},
                       {"case_number": None, "quotes": ["합성 기사 제목 둘째", "시정권고"]},
                       {"case_number": "제2021-815호", "quotes": ["시정권고"], "outcome": "합성 기사 제목 셋째"}])
    assert n == 0 and [c.record["verification_status"] for c in cases] == ["unverified"] * 3


def test_gd26_repeated_result_cell_alone_is_not_row_unique():
    cases, n = _check([{"case_number": None, "quotes": ["시정권고"]}])
    assert n == 1 and cases[0].record["verification_status"] == "quarantined"


def test_gd26_a_title_cell_cited_by_two_cases_belongs_to_neither_row():
    cases, n = _check([{"case_number": None, "quotes": ["합성 기사 제목 둘째"]}, {"case_number": None, "quotes": ["합성 기사 제목 둘째"]}])
    assert n == 2 and all(c.record["verification_status"] == "quarantined" for c in cases)


def test_gd26_a_case_with_one_row_unique_quote_among_shared_ones_passes():
    cases, n = _check([{"case_number": None, "quotes": ["■ 시정권고 결정 3건", "합성 기사 제목 첫째"]}])
    assert n == 0 and cases[0].record["verification_status"] == "unverified"


def test_gd26_rule_is_limited_to_table_sources_and_leaves_other_quarantine_reasons_alone():
    cases, n = _check([{"case_number": None, "quotes": ["■ 시정권고 결정 3건"]}], source_id="pac_correction_casebooks")
    assert n == 0 and cases[0].record["verification_status"] == "unverified"
    ctx = ctx_for({"pg": HEADER_PAGE_TAIL}, profile="pac_correction", source_id=STATUS_SRC)
    already = _case(ctx, 0, case_number=None, quotes=["제목이 없는 문장입니다"])            # 근거 없음으로 이미 격리
    assert already.record["verification_status"] == "quarantined"
    before = list(already.errors)
    assert sr.require_row_unique_quote([already], ctx.pages, STATUS_SRC) == 0 and already.errors == before


def test_gd26_br_split_case_number_cell_counts_as_the_case_number():
    page = "| 순번 | 의결번호 |\n| --- | --- |\n| 1 | 제2021<br>-813호 |\n| 2 | 제2021<br>-814호 |\n"
    cases, n = _check([{"case_number": "제2021-813호", "quotes": ["제2021-813호"]}], page=page)
    assert n == 0 and cases[0].record["verification_status"] == "unverified"

"""G-D14 소스별 PDF 추출기 선택 상수 시험(P2_DESIGN §10, docs/coverage/P2_extractor_compare.md '## G-D14 오프라인 비교').

상수는 오프라인 대조율 비교(evals/extractor_compare.py)의 결과이고, 문서의 선택 표와 코드 상수가 어긋나지 않게 묶는다.
합성 입력만 쓰며 장부·네트워크를 읽지 않는다.
"""
import re
import sys
from pathlib import Path

import pdr_ing_paths  # noqa: F401
from press_review.ingestion import extract as ext
from press_review.ingestion.sources import pac as pac_src

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "coverage" / "P2_extractor_compare.md"
SECTION = "## G-D14 오프라인 비교"
TABLE_HEAD = "### 소스별 기본 추출기 선택"

# 2026-10-09 오프라인 비교 결과(모델이 본 텍스트 편향이 있는 대조율, 문서 표 참조). 이 표를 바꾸면 문서의 선택 표도 함께 바꾼다.
EXPECTED = {
    "iendc_review_results": "kordoc",
    "pac_correction_casebooks": "pdftotext",
    "pac_adjustment_casebooks": "pdftotext",
    "pac_election_whitepapers": "column_crop",           # G-D28 비교가 G-D14 의 pdftotext_layout 을 대체했다(docs/coverage/P2_extractor_compare.md '## G-D28 비교')
    "pac_board_correction_status": "pdftotext_layout",
    "pac_research_judgment_analysis": "pdftotext_layout",
}


def test_gd14x_per_source_extractor_constant_is_the_offline_comparison_result():
    assert dict(ext.PDF_EXTRACTOR_BY_SOURCE) == EXPECTED
    for src, name in EXPECTED.items():
        assert ext.pdf_extractor_for({"source_id": src}) == name


def test_gd14x_constant_keys_are_real_sources_and_values_are_known_extractors():
    from press_review.ingestion.sources import iendc as iendc_src
    known = {iendc_src.SOURCE_ID} | {m["source_id"] for m in pac_src.MENUS}
    for src, name in ext.PDF_EXTRACTOR_BY_SOURCE.items():
        assert name in ext.PDF_EXTRACTORS
        assert src in known, src
    # 정하지 않은 소스는 기본(-layout)으로 남는다.
    assert ext.pdf_extractor_for({"source_id": "pac_board_committee_rules"}) == ext.DEFAULT_PDF_EXTRACTOR == "pdftotext_layout"


def test_gd14x_input_hash_tag_marks_only_choices_that_differ_from_the_legacy_behaviour():
    sig = lambda s: ext.extractor_signature({"source_id": s, "detected_type": "pdf"})  # noqa: E731
    assert sig("pac_correction_casebooks") == "x:pdftotext" and sig("pac_adjustment_casebooks") == "x:pdftotext"
    assert sig("pac_election_whitepapers") == "x:column_crop" and sig("pac_research_judgment_analysis") == ""      # 선거백서는 G-D28 에서 바뀌었다
    assert sig("pac_board_correction_status") == "x:auto-table-v1"                                              # G-D26: 정책 이름
    assert sig("iendc_review_results") == ""
    assert ext.extractor_signature({"source_id": "pac_correction_casebooks", "detected_type": "hwp"}) == ""


def test_gd14x_doc_selection_table_matches_the_code_constant():
    text = DOC.read_text(encoding="utf-8")
    assert SECTION in text
    body = text.split(SECTION, 1)[1]
    assert TABLE_HEAD in body
    rows = dict(re.findall(r"^\|\s*`([a-z_]+)`\s*\|\s*`(pdftotext_layout|pdftotext_raw|pdftotext|column_crop|kordoc)`\s*\|", body.split(TABLE_HEAD, 1)[1].split("\n###", 1)[0], re.M))
    assert rows == dict(ext.PDF_EXTRACTOR_BY_SOURCE)


def test_gd14x_quote_harvest_walks_nested_response_objects():
    sys.path.insert(0, str(REPO / "evals"))
    import extractor_compare as ec
    resp = {"cases": [{"first_page_id": "a:p0001", "outcome": {"quote": {"page_id": "a:p0002", "quote": "결론 문구"}, "text": "x"},
                       "original_article": {"quote": None, "status": "absent"},
                       "article_claims": [{"quote": {"quote": "주장 문구"}, "text": "y"}]}]}
    got = sorted(ec.harvest(resp))
    assert got == [("결론 문구", "a:p0002", "a:p0001"), ("주장 문구", None, "a:p0001")]

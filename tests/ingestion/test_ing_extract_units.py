"""추출 단위 시험: 품질 지표, 인쇄 쪽 번호 감지, TSV 파서, kind/HTML 정규화."""
import pytest

from ext_helpers import tsv
from press_review.ingestion.extract import html as html_mod
from press_review.ingestion.extract import pageno, quality
from press_review.ingestion.extract.pdf import clean_text, norm_signature, parse_tsv
from press_review.ingestion.fetch import check_content, detect_kind, kind_of
from press_review.ingestion.stage import ranges_str, resolve


def test_quality_metrics_reasons():
    q, m, r = quality.assess("한글로 된 충분히 긴 문장입니다 이것은 시험용 페이지 본문이다")
    assert r == [] and m["hangul_ratio"] > 0.7 and q > 0.9
    assert quality.assess("")[2] == ["too_short"]
    assert quality.assess("abc")[2] == ["too_short"]
    _, m, r = quality.assess("���" + "정상 텍스트가 이어집니다 " * 3)
    assert "garbled" in r and m["garble_ratio"] > 0.03
    assert "low_text_ratio" in quality.assess("1 2 3 4 5 6 7 8 9 0 " * 3)[2]
    assert quality.assess("The committee considered the request " * 2)[2] == []       # 영문 페이지는 OCR 대상이 아니다


def test_line_flags_numbers_case_numbers_negations():
    assert quality.line_flags("대법원 2019다12345 판결") == ["case_number", "number"]
    assert quality.line_flags("원고의 청구를 기각한다") == ["negation"]
    assert quality.line_flags("피고는 책임이 없다 3000원") == ["number", "negation"]
    assert quality.line_flags("This is not a drill") == ["negation"]
    assert quality.line_flags("평범한 설명 문장") == []


@pytest.mark.parametrize("line,expect", [("12", ("arabic", 12)), ("- 12 -", ("arabic", 12)), ("3 / 14", ("arabic", 3)),
                                         ("Page 4", ("arabic", 4)), ("제 5 쪽", ("arabic", 5)), ("iv", ("roman", 4)),
                                         ("iiii", None), ("2019년 12월", None), ("제목 12", None), ("5 / 3", None), ("", None)])
def test_line_candidates(line, expect):
    assert pageno.line_candidate(line) == expect


def cand(v, kind="arabic"):
    return [{"kind": kind, "value": v, "where": "tail"}]


def test_printed_numbers_need_a_consistent_run_and_never_interpolate():
    cands = {1: [], 2: cand(1), 3: cand(2), 4: cand(3), 5: [], 6: cand(5), 7: cand(99)}
    out = pageno.resolve(cands, 7)
    assert out == {2: "1", 3: "2", 4: "3", 6: "5"}                  # 5쪽(후보 없음)과 7쪽(어긋난 값)은 비워 둔다
    assert pageno.resolve({1: cand(1), 2: cand(7)}, 2) == {}         # 일관된 연속이 없으면 채우지 않는다
    assert pageno.resolve({1: cand(1), 2: cand(2), 3: cand(3)}, 3) == {1: "1", 2: "2", 3: "3"}


def test_roman_front_matter_and_arabic_body():
    cands = {1: cand(1, "roman"), 2: cand(2, "roman"), 3: cand(3, "roman"), 4: cand(1), 5: cand(2), 6: cand(3)}
    assert pageno.resolve(cands, 6) == {1: "i", 2: "ii", 3: "iii", 4: "1", 5: "2", 6: "3"}


def test_page_candidates_look_only_at_edges():
    text = "\n".join(["제목 줄", "본문 하나", "본문 둘", "본문 셋", "42", "본문 다섯", "본문 여섯", "본문 일곱", "- 8 -"])
    assert pageno.page_candidates(text) == [{"kind": "arabic", "value": 8, "where": "tail"}]   # 가운데의 42 는 무시


def test_parse_tsv_lines_paragraphs_confidence():
    o = parse_tsv(tsv([[("2019다1 선고", 90), ("기각 한다", 80)], [("둘째 문단", 100)]]))
    assert o.text == "2019다1 선고\n기각 한다\n\n둘째 문단"
    assert o.confidence == pytest.approx(0.9, abs=0.01)
    assert [l["line"] for l in o.lines] == [0, 1] and o.lines[1]["start"] == len("2019다1 선고\n")
    assert parse_tsv(tsv([])).text == "" and parse_tsv(tsv([])).confidence is None


def test_signature_and_clean_text():
    assert norm_signature("짧음") is None
    a = norm_signature("가나다라 " * 20)
    assert a and a == norm_signature("가나다라" * 20) and a != norm_signature("가나다마 " * 20)
    assert clean_text(b"\xef\xbb\xbf\n\nline1  \r\nline2\x00\n\x0c") == "line1\nline2"


def test_kind_of_ported_and_detect_kind_ignores_extension():
    assert kind_of(b"%PDF-1.7", "") == "pdf" and kind_of(b"\xd0\xcf\x11\xe0xx", ".hwp") == "hwp"
    assert kind_of(b"PK\x03\x04", "") == "hwpx" and kind_of(b"PK\x03\x04", ".docx") == "docx" and kind_of(b"zzz", "") == "unknown"
    assert detect_kind(b"\xef\xbb\xbf <!DOCTYPE html><html>") == "html" and detect_kind(b"plain text") == "unknown"
    att = {"url": "https://www.pac.or.kr/a", "label": "PDF", "content_type": "application/pdf"}
    assert check_content(att, b"%PDF-1.4 x", {}) is None
    assert check_content(att, b"%PDF-1.4 x", {"content-type": "text/html"}).startswith("magic_mismatch")
    assert check_content({"url": "https://x/a.hwp", "label": None}, b"PK\x03\x04..", {}) is None      # hwp 선언 + hwpx(zip) 본문 허용
    assert check_content({"url": "https://x/a", "label": None}, b"MZ\x90", {}).startswith("unknown_type")


def test_html_cells_headings_and_stuck_warning():
    secs, warns = html_mod.html_sections("<body><h1>제목</h1><table><tr><th>가</th><td>나</td></tr><tr><td>다</td><td></td></tr></table>"
                                          "<footer>끝</footer></body>".encode())
    text = "\n".join(t for _, t in secs)
    assert "가\t나" in text and "끝" in text and warns == []
    _, warns = html_mod.html_sections(b"<body><p>a</p><iframe><p>swallowed tail</p>")
    assert warns == ["unclosed_skip_tag:1"]
    secs, _ = html_mod.html_sections(b"<head><title>x</title><body><p>head never closed</p>")
    assert secs and "head never closed" in secs[0][1]
    assert html_mod.decode_html("한글".encode("cp949")) == "한글"                     # utf-8 실패 -> cp949


def test_stage_helpers():
    assert ranges_str([1, 2, 3, 7, 9, 10]) == "1-3,7,9-10" and ranges_str([]) == ""
    with pytest.raises(ValueError):
        resolve(__import__("pathlib").Path("/x"), "../etc/passwd")

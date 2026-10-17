"""G-D28(docs/P2_DESIGN.md §11): 선거백서 2단 조판·깨진 텍스트층. 2단 감지 -> 단별 crop 추출기, ``pdftotext -raw`` 추출기 옵션,
깨진 텍스트층 감지와 OCR 강제, 오프라인 비교로 정한 소스 기본값. 합성 텍스트, 가짜 실행기, 네트워크 없음.
"""
import json
import re
from pathlib import Path

import pytest

import pdr_ing_paths  # noqa: F401
from ext_helpers import FakeRunner, make_pdf, place_raw, tsv
from ext_helpers import seed as ext_seed
from press_review.ingestion import extract as ext
from press_review.ingestion.extract import layout, pdf as pdf_mod, quality
from press_review.ingestion.extract.tools import ToolError, ToolResult
from test_gd import KdRunner
from test_ing_extract_run import env  # noqa: F401  (fixture)

REPO = Path(__file__).resolve().parents[2]
ELECTION = "pac_election_whitepapers"
FAMILY = "pac_election"


def two_col_page(lines=14, gap=6):
    return "\n".join(f"{'왼쪽 단 합성 문장 ' + str(i) + ' 입니다 가나다':<34}{' ' * gap}{'오른쪽 단 합성 문장 ' + str(i) + ' 입니다 라마바'}" for i in range(lines))


def one_col_page(lines=14):
    return "\n".join(f"한 단짜리 합성 본문 {i}번째 줄입니다 이 줄은 쪽 전체 너비를 쓰고 가운데에 빈 띠가 없다 가나다라마바사아자차카타파하" for i in range(lines))


# ------------------------------------------------------------------ 2단 감지(텍스트)
def test_gd28_vertical_blank_band_across_most_lines_is_two_column():
    band = layout.two_column_band(two_col_page())
    assert band is not None and band[1] - band[0] + 1 >= 2
    assert layout.two_column_page(two_col_page()) is True


@pytest.mark.parametrize("text", [one_col_page(), two_col_page(lines=5), "", "한 줄만 있는 쪽"])
def test_gd28_single_column_and_tiny_pages_are_not_two_column(text):
    assert layout.two_column_page(text) is False


def test_gd28_mixed_page_with_a_two_column_top_and_a_one_column_body_is_not_cropped():
    mixed = two_col_page(lines=10) + "\n" + one_col_page(lines=12)           # 778 p60 처럼 위는 2단, 아래 결정문은 1단
    assert layout.two_column_page(mixed) is False


def test_gd28_a_few_full_width_lines_do_not_hide_a_two_column_body():
    assert layout.two_column_page(one_col_page(lines=2) + "\n" + two_col_page(lines=30)) is True


# ------------------------------------------------------------------ 단 사이 간격(단어 상자)
def boxes_for(left_end=240.0, right_start=270.0, rows=12, extra=()):
    out = []
    for r in range(rows):
        y = 50.0 + 14 * r
        out += [(40.0, y, left_end, y + 10), (right_start, y, 520.0, y + 10)]
    return out + list(extra)


def test_gd28_gutter_is_the_widest_strip_no_word_box_crosses():
    cut, gap = pdf_mod.find_gutter(boxes_for(), 560.0)
    assert 240.0 < cut < 270.0 and gap >= 25.0


@pytest.mark.parametrize("boxes", [
    boxes_for(extra=[(60.0, 400.0, 500.0, 410.0)]),                  # 전폭 제목 줄 하나가 간격을 가로지른다
    boxes_for(left_end=263.0, right_start=267.0),                    # 6pt 미만
    [(40.0, 50.0 + 14 * r, 240.0, 60.0 + 14 * r) for r in range(12)],      # 한쪽이 비었다
    [],
])
def test_gd28_no_gutter_when_a_box_crosses_it_or_it_is_too_narrow_or_one_sided(boxes):
    assert pdf_mod.find_gutter(boxes, 560.0) is None


# ------------------------------------------------------------------ 가짜 실행기: crop·raw·bbox
class GeoRunner(FakeRunner):
    """pdfinfo -f/-l, pdftotext -bbox/-x/-raw 를 쪽별 명세로 흉내 낸다.
    spec: pages(쪽별 -layout 텍스트), geom {'size': (w, h), 'rot': 0}, boxes({쪽: 상자 목록}), crops({쪽: (왼쪽 텍스트, 오른쪽 텍스트)}), whole({쪽: 읽기 순서 텍스트}), raw({쪽: -raw 텍스트})."""

    def run(self, argv, *, timeout):
        tool = argv[0].rsplit("/", 1)[-1]
        if tool == "pdfinfo" and "-f" in argv:
            self.calls.append(list(argv))
            spec = self._spec(argv[-1])
            n = int(argv[argv.index("-f") + 1])
            w, h = spec.get("geom", {}).get("size", (595.0, 842.0))
            rot = spec.get("geom", {}).get("rot", 0)
            return ToolResult(0, f"Page {n:>4} size:      {w} x {h} pts (A4)\nPage {n:>4} rot:       {rot}\n".encode())
        if tool == "pdftotext" and ("-bbox" in argv or "-x" in argv or "-raw" in argv or "-layout" not in argv):
            self.calls.append(list(argv))
            spec = self._spec(argv[-2])
            n = int(argv[argv.index("-f") + 1])
            if "-bbox" in argv:
                words = "".join(f'<word xMin="{b[0]}" yMin="{b[1]}" xMax="{b[2]}" yMax="{b[3]}">w</word>\n' for b in spec.get("boxes", {}).get(n, []))
                return ToolResult(0, f"<doc><page>{words}</page></doc>".encode())
            if "-x" in argv:
                left, right = spec["crops"][n]
                return ToolResult(0, (left if argv[argv.index("-x") + 1] == "0" else right).encode("utf-8"))
            if "-raw" in argv:
                return ToolResult(0, spec["raw"][n].encode("utf-8"))
            return ToolResult(0, spec.get("whole", {}).get(n, spec["pages"][n - 1]).encode("utf-8"))
        return super().run(argv, timeout=timeout)


LEFT = "왼쪽 단의 첫 문장입니다 가나다라\n왼쪽 단의 둘째 문장입니다 마바사아"
RIGHT = "오른쪽 단의 첫 문장입니다 자차카타\n오른쪽 단의 둘째 문장입니다 파하거너"


def geo_spec(**over):
    spec = {"pages": [two_col_page()], "geom": {"size": (595.0, 842.0), "rot": 0}, "boxes": {1: boxes_for()},
            "crops": {1: (LEFT, RIGHT)}, "whole": {1: LEFT + "\n" + RIGHT}, "raw": {1: "원시 순서 합성 문장입니다"}}
    spec.update(over)
    return spec


def test_gd28_two_column_page_is_cut_at_the_gutter_and_read_left_then_right(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(make_pdf(["a"]))
    r = GeoRunner({pdf.read_bytes(): geo_spec()})
    text, info = pdf_mod.column_crop_text(r, pdf, 1, two_col_page())
    assert info["reason"] == "cropped" and text == LEFT + "\n\n" + RIGHT
    crops = [a for a in r.calls if "-x" in a]
    assert len(crops) == 2 and all(a[a.index("-y") + 1] == "0" and "-W" in a and "-H" in a for a in crops)
    assert crops[0][crops[0].index("-x") + 1] == "0" and 240 < int(crops[1][crops[1].index("-x") + 1]) < 270
    assert int(crops[0][crops[0].index("-W") + 1]) + int(crops[1][crops[1].index("-W") + 1]) == 595       # 쪽 너비는 pdfinfo 에서
    assert int(crops[0][crops[0].index("-H") + 1]) == 842


def test_gd28_a_cut_that_splits_a_word_is_discarded(tmp_path):
    # 왼쪽+오른쪽 crop 의 단어 목록이 자르지 않은 쪽의 단어 목록과 다르다
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(make_pdf(["b"]))
    r = GeoRunner({pdf.read_bytes(): geo_spec(whole={1: LEFT + "\n" + RIGHT + " 잘린단어"})})
    text, info = pdf_mod.column_crop_text(r, pdf, 1, two_col_page())
    assert text is None and info["reason"] == "words_split_by_cut"


@pytest.mark.parametrize("name,text,spec,reason", [
    ("one_col", one_col_page(), {}, "not_two_column"),
    ("rotated", two_col_page(), {"geom": {"size": (595.0, 842.0), "rot": 90}}, "page_rotated"),
    ("no_gutter", two_col_page(), {"boxes": {1: boxes_for(extra=[(60.0, 400.0, 500.0, 410.0)])}}, "no_gutter_in_word_boxes"),
])
def test_gd28_pages_that_cannot_be_cut_report_why_and_fall_back(tmp_path, name, text, spec, reason):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(make_pdf([name]))
    r = GeoRunner({pdf.read_bytes(): geo_spec(**spec)})
    out, info = pdf_mod.column_crop_text(r, pdf, 1, text)
    assert out is None and info["reason"] == reason
    if reason == "not_two_column":
        assert not r.calls                                               # 2단이 아니면 도구를 부르지 않는다


# ------------------------------------------------------------------ 추출기 이름과 쪽 추출
def test_gd28_extractor_names_and_cli_choices_include_raw_and_column_crop():
    assert ext.PDF_EXTRACTORS == ("pdftotext", "pdftotext_layout", "pdftotext_raw", "column_crop", "kordoc")
    from press_review.ingestion.sources import extract as cli_extract
    src = Path(cli_extract.__file__).read_text(encoding="utf-8")
    assert "pdftotext_raw" in src and "column_crop" in src


def test_gd28_raw_mode_calls_pdftotext_raw_and_labels_the_page(tmp_path):
    blob = make_pdf(["r"])
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(blob)
    r = GeoRunner({blob: geo_spec(raw={1: "원시 순서로 읽은 합성 문장입니다 가나다라마바사아자차카타"})})
    out = pdf_mod.extract_page(r, pdf, 1, ocr=False, mode="pdftotext_raw")
    assert out.method == "pdftotext_raw" and out.text.startswith("원시 순서로") and out.quality_json["extractor"] == "pdftotext_raw"
    assert any("-raw" in a for a in r.calls) and not any("-layout" in a for a in r.calls)


def test_gd28_column_crop_mode_labels_cropped_pages_and_keeps_layout_for_the_rest(tmp_path):
    blob = make_pdf(["c1", "c2"])
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(blob)
    spec = geo_spec(pages=[two_col_page(), one_col_page()], boxes={1: boxes_for()}, crops={1: (LEFT, RIGHT)}, whole={1: LEFT + "\n" + RIGHT})
    r = GeoRunner({blob: spec})
    a = pdf_mod.extract_page(r, pdf, 1, ocr=False, mode="column_crop")
    b = pdf_mod.extract_page(r, pdf, 2, ocr=False, mode="column_crop")
    assert (a.method, a.quality_json["extractor"], a.quality_json["columns"]["reason"]) == ("pdftotext_columns", "column_crop", "cropped")
    assert a.text == LEFT + "\n\n" + RIGHT
    assert (b.method, b.quality_json["extractor"], b.quality_json["columns"]["reason"]) == ("pdftotext", "column_crop", "not_two_column")
    assert b.text == one_col_page()


# ------------------------------------------------------------------ 깨진 텍스트층
BROKEN = ("저 18조 위반 결정， 심의대상기사를 통해 ↑월 ↑↑일자 가상인물의 칼럼을 게재하였다． 가상기준 저 l ↑조(공정성)， 각 위반 "
          "또다시 위 심의대상끼사에 대하여 저 12 항에 따라 주문과 같이 결정한다．") * 3
CLEAN = ("공직선거법 제18조 위반으로 결정한다. 심의대상 기사를 통해 특정 후보예정자의 칼럼을 게재하였다. 선거기사심의기준 제11조(공정성)에 따라 "
         "주문과 같이 결정한다. 언론사 대표이사는 위원회의 통보에 따라 해당 기사를 정정하여야 한다. 아래는 그 이유이다.") * 3


def test_gd28_broken_text_layer_is_flagged_and_clean_text_is_not():
    q, m, reasons = quality.assess(BROKEN)
    assert "broken_layer" in reasons and m["hangul_chars"] >= quality.BROKEN_MIN_HANGUL and m["noise_per_100"] >= quality.BROKEN_NOISE_MAX
    assert "broken_layer" not in quality.assess(CLEAN)[2]


def test_gd28_noise_rule_ignores_legit_fullwidth_colon_markdown_pipes_and_short_pages():
    labels = "\n".join(f"정당：무소속 나이：{40 + i} 직업：농업 학력：대명중 졸업 합성 후보 {i}" for i in range(12))
    table = "\n".join(f"| {i} | 제2021-{800 + i}호 | 매체명 | 합성 기사 제목입니다 | 시정권고 |" for i in range(12))
    assert "broken_layer" not in quality.assess(labels)[2] and "broken_layer" not in quality.assess(table)[2]
    assert "broken_layer" not in quality.assess("저 18조 ， ↑ 짧은 쪽")[2]


def test_gd28_broken_page_gets_ocr_and_the_ocr_text_replaces_the_layer(tmp_path):
    blob = make_pdf(["broken"])
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(blob)
    ocr_text = [[("공직선거법 제18조 위반으로 결정한다 심의대상 기사를 통해 특정 후보예정자의 칼럼을 게재하였다", 90)] * 3]
    r = GeoRunner({blob: {"pages": [BROKEN], "ocr": {1: tsv(ocr_text)}}})
    out = pdf_mod.extract_page(r, pdf, 1, ocr=True, workdir_root=tmp_path / "w", check_broken=True)
    assert out.method == "ocr" and "제18조" in out.text
    assert "broken_layer" in out.quality_json["text_layer"]["reasons"] and out.quality_json["ocr"]["adopted"] is True


def test_gd28_broken_page_without_ocr_tools_or_with_ocr_disabled_is_marked_for_review(tmp_path):
    blob = make_pdf(["broken2"])
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(blob)
    r = GeoRunner({blob: {"pages": [BROKEN]}}, available=("pdfinfo", "pdftotext"))
    out = pdf_mod.extract_page(r, pdf, 1, ocr=True, check_broken=True)
    assert out.method == "pdftotext" and out.needs_review and out.quality_json["ocr"] == {"attempted": False, "reason": "tool_unavailable"}
    out2 = pdf_mod.extract_page(GeoRunner({blob: {"pages": [BROKEN]}}), pdf, 1, ocr=False, check_broken=True)
    assert out2.needs_review and out2.quality_json["ocr"]["reason"] == "disabled" and "broken_layer" in out2.quality_json["reasons"]


def test_gd28_kordoc_page_with_a_broken_text_falls_back_to_pdftotext_and_ocr_for_the_election_source(env):
    doc, (a1,) = ext_seed(env.m, "700", source=ELECTION, family=FAMILY, atts=[(1, "https://www.pac.or.kr/files/K.pdf", "chapter", None, "application/pdf", "PDF")])
    blob = make_pdf(["election kordoc broken"])
    place_raw(env.m, env.corpus, a1, blob, family=FAMILY)
    r = KdRunner({blob: {"pages": [CLEAN], "kd_pages": [BROKEN]}})
    code, res = env.run(r, "--extractor", "kordoc")
    rows = env.m.pages_of(att_id=a1)
    assert code == 0 and rows[0]["method"] == "pdftotext" and json.loads(rows[0]["quality_json"])["extractor_fallback"] == "kordoc_low_quality"


@pytest.mark.parametrize("source,family", [("pac_adjustment_casebooks", "pac_cases"), ("pac_research_judgment_analysis", "pac_other"), ("pac_board_correction_status", "pac_cases"),
                                           ("iendc_review_results", "iendc")])
def test_gd28_broken_layer_ocr_is_limited_to_the_election_source(env, source, family):
    assert ext.BROKEN_LAYER_SOURCES == frozenset({ELECTION})
    doc, (a1,) = ext_seed(env.m, "710", source=source, family=family, atts=[(1, "https://example.test/x.pdf", "full", None, "application/pdf", "PDF")])
    blob = make_pdf(["scoped " + source])
    place_raw(env.m, env.corpus, a1, blob, family=family)
    ocr_text = [[("공직선거법 제18조 위반으로 결정한다 심의대상 기사를 통해 특정 후보예정자의 칼럼을 게재하였다", 90)] * 3]
    r = GeoRunner({blob: {"pages": [BROKEN], "ocr": {1: tsv(ocr_text)}, "kd_pages": [BROKEN]}}, kordoc=None)
    code, res = env.run(r, "--extractor", "pdftotext_layout")
    row = env.m.pages_of(att_id=a1)[0]
    qj = json.loads(row["quality_json"])
    assert code == 0 and row["method"] == "pdftotext" and not row["needs_review"] and "broken_layer" not in qj["reasons"] and r.count("tesseract") == 0
    assert qj["noise_per_100"] >= quality.BROKEN_NOISE_MAX                  # 지표는 남는다(판정만 하지 않는다)


def test_gd28_extraction_records_which_pages_had_a_broken_text_layer(env):
    doc, (a1,) = ext_seed(env.m, "701", source=ELECTION, family=FAMILY, atts=[(1, "https://www.pac.or.kr/files/E.pdf", "chapter", None, "application/pdf", "PDF")])
    blob = make_pdf(["e1", "e2", "e3"])
    place_raw(env.m, env.corpus, a1, blob, family=FAMILY)
    ocr_text = [[("공직선거법 제18조 위반으로 결정한다 심의대상 기사를 통해 특정 후보예정자의 칼럼을 게재하였다", 90)] * 3]
    r = GeoRunner({blob: {"pages": [CLEAN, BROKEN, CLEAN], "ocr": {2: tsv(ocr_text)}}})
    code, res = env.run(r, "--extractor", "pdftotext_layout")
    assert code == 0
    note = json.loads(env.m.get_document(doc)["extra_json"])["p2_extract"]
    assert note["broken_layer_pages"] == "2"
    assert [p["method"] for p in env.m.pages_of(att_id=a1)] == ["pdftotext", "ocr", "pdftotext"]


# ------------------------------------------------------------------ 추출 단계 연결과 소스 기본값
@pytest.mark.parametrize("name,method", [("pdftotext_raw", "pdftotext_raw"), ("column_crop", "pdftotext_columns")])
def test_gd28_extract_run_accepts_the_new_extractors_and_the_choice_enters_the_input_hash(env, name, method):
    doc, (a1,) = ext_seed(env.m, "702" + name[-1], source=ELECTION, family=FAMILY, atts=[(1, "https://www.pac.or.kr/files/E2.pdf", "chapter", None, "application/pdf", "PDF")])
    blob = make_pdf([f"e {name}"])
    place_raw(env.m, env.corpus, a1, blob, family=FAMILY)
    r = GeoRunner({blob: geo_spec(raw={1: "원시 순서 합성 문장입니다 가나다라마바사아자차카타파하 " * 2})})
    code, res = env.run(r, "--extractor", name)
    assert code == 0 and res["result"]["outcomes"] == {"extracted": 1}
    assert [p["method"] for p in env.m.pages_of(att_id=a1)] == [method]
    h1 = env.m.select("SELECT extract_input_sha256 FROM attachments WHERE att_id=?", (a1,))[0]["extract_input_sha256"]
    assert h1 == ext.stage.sha256_bytes(f"{env.m.select('SELECT raw_sha256 FROM attachments WHERE att_id=?', (a1,))[0]['raw_sha256']}|x:{name}".encode("utf-8"))


def test_gd28_source_default_for_election_whitepapers_matches_the_offline_comparison_section():
    doc = (REPO / "docs" / "coverage" / "P2_extractor_compare.md").read_text(encoding="utf-8")
    assert "## G-D28 비교" in doc
    body = doc.split("## G-D28 비교", 1)[1]
    m = re.search(r"^\|\s*`pac_election_whitepapers`\s*\|\s*`(pdftotext_layout|pdftotext|pdftotext_raw|column_crop|kordoc)`\s*\|", body, re.M)
    assert m, "G-D28 section must carry the source default row"
    assert ext.PDF_EXTRACTOR_BY_SOURCE["pac_election_whitepapers"] == m.group(1)
    assert ext.pdf_extractor_for({"source_id": "pac_election_whitepapers"}) == m.group(1)

"""법제처 판례 본문(lawapi 본문 절): 네트워크 없는 가짜 transport.

EN_BANC 는 2026-10-09 실응답(공개 판례, 대법원 2020다248124)의 구조와 표지 문구를 줄인 것이고 변호사 이름은 지웠다. OLD_STYLE·UNSPLIT·PLAIN 은
표지 형식을 검사하려고 만든 합성 텍스트다(의견 내용도 합성이며 실제 대법관 의견이 아니다)."""
import io
import json
import urllib.parse as up

import pytest

from conftest import FAKE_OC, FakeClock, FakeTransport
from press_review.ingestion import __main__ as cli
from press_review.ingestion.http import IngestionError
from press_review.ingestion.manifest import Manifest
from press_review.ingestion.sources import lawapi as L

ROBOTS = {"https://www.law.go.kr/robots.txt": (200, "User-agent: *\nAllow: /\n", {})}

EN_BANC = "<br/>".join([
    "【원고, 피상고인】                원고 (소송대리인 법무법인 한마루 담당변호사 ○○○ 외 4인)",
    "【피고, 상고인】                피고 (소송대리인 법무법인(유한) 민 담당변호사 ○○○ 외 3인)",
    "【원심판결】             대전지법 2020. 6. 30. 선고 2019나102169 판결         ",
    "【주    문】",
    "상고를 기각한다.  상고비용은 피고가 부담한다.",
    "",
    "【이    유】1. 사건 개요",
    "  상고이유를 판단한다. 언론중재법상 정정보도 청구와 관련한 입법이 이루어졌다.",
    "  이 판결에는 대법관 이기택, 대법관 안철상, 대법관 이흥구의 반대의견이 있는 외에는 관여 법관의 의견이 일치하였으며, 다수의견에 대한 대법관 김재형의 보충의견이 있다.",
    "5. 판결절차에서 간접강제를 명할 수 있는지 여부에 관한 대법관 이기택, 대법관 안철상, 대법관 이흥구의 반대의견",
    "가. 다수의견은 판결절차에서 간접강제를 명할 수 있다고 한다.",
    "이상과 같은 이유로 다수의견에 찬성할 수 없음을 밝힌다.",
    "6. 다수의견에 대한 대법관 김재형의 보충의견",
    "다수의견과 반대의견의 차이는 본안판결에서 간접강제를 명할 수 있는 법적 근거의 존부에 있다.",
    "이상과 같이 다수의견의 논거를 보충하는 의견을 개진한다.",
    "7. 다수의견에 대한 대법관 김선수의 보충의견",
    "반대의견이 제시하는 몇 가지 논점들에 대하여 필요한 범위에서 검토한다.",
    "",
    "대법원장 김명수(재판장) 이기택(주심) 김재형 조재연",
])
OLD_STYLE = "<br/>".join([
    "【주    문】", "상고를 기각한다.", "【이    유】상고이유를 판단한다.",
    "대법관 ○○○의 별개의견은 다음과 같다.", "나는 결론에 찬성하나 이유가 다르다.",
    "대법관 ○○○의 반대의견은 다음과 같다.", "나는 다수의견에 찬성할 수 없다.",
    "대법관 송진훈(재판장) 변재승 윤재식(주심) 이규홍",
])
UNSPLIT = "<br/>".join(["【주    문】", "상고를 기각한다.", "【이    유】상고이유를 판단한다.",
                        "이 판결에는 대법관 홍길동의 반대의견이 있다.", "대법관 송진훈(재판장) 변재승 윤재식(주심) 이규홍"])
PLAIN = "<br/>".join(["【원고, 상고인】 원고", "【주    문】", "상고를 모두 기각한다.", "【이    유】기록에 비추어 보면 이유 없다.",
                      "그러므로 상고를 기각하기로 하여 관여 법관의 일치된 의견으로 주문과 같이 판결한다.", "",
                      "대법관 송진훈(재판장) 변재승 윤재식(주심) 이규홍 "])
REFS = ("[1][2][3] 대법원 2002. 1. 22. 선고 2000다37524, 37531 판결(공2002상, 522) / [1] 대법원 1988. 6. 14. 선고 87다카1450 판결(공1988, 1020), "
        "대법원 1991. 5. 14.자 91마1 결정 / [5] 헌법재판소 2006. 6. 29. 선고 2005헌마165, 314, 555, 807, 2006헌가3 전원재판부 결정(헌공117, 950), "
        "서울고등법원 2009. 6. 17. 선고 2008나80595 판결<br/>")


def prec_json(serial, content=PLAIN, case_no="2020다1", date="20210722", court="대법원", kind="판결", issue="", holding="", ref_st="", ref_cases=""):
    return json.dumps({"PrecService": {
        "판례정보일련번호": str(serial), "사건번호": case_no, "선고일자": date, "법원명": court, "판결유형": kind, "사건명": "손해배상(기)",
        "판시사항": issue, "판결요지": holding, "참조조문": ref_st, "참조판례": ref_cases, "판례내용": content}}, ensure_ascii=False)


def list_json(items, total=None):
    return json.dumps({"PrecSearch": {"target": "prec", "totalCnt": str(len(items) if total is None else total), "page": "1",
                                      "section": "bdyText", **({"prec": items} if items else {})}}, ensure_ascii=False)


def list_item(serial, case_no, date, court="대법원"):
    return {"id": "1", "사건번호": case_no, "데이터출처명": "대법원", "사건종류코드": "400101", "사건종류명": "민사", "선고": "선고",
            "선고일자": date, "판례일련번호": str(serial), "판결유형": "판결", "법원종류코드": "", "법원명": court,
            "판례상세링크": f"/DRF/lawService.do?OC={FAKE_OC}&target=prec&ID={serial}&type=HTML&mobileYn=", "사건명": "손해배상(기)"}


class Server:
    """lawService·lawSearch(nb) 가짜 서버. bodies: {serial: str | (status, str)}, nb: {사건번호: [list_item...]}."""

    def __init__(self, bodies, nb=None):
        self.bodies, self.nb = dict(bodies), dict(nb or {})
        self.service_calls, self.nb_calls = [], []

    def __call__(self, url, n):
        q = up.parse_qs(up.urlsplit(url).query)
        if "ID" in q:
            self.service_calls.append(q["ID"][0])
            b = self.bodies.get(q["ID"][0])
            if b is None:
                return 200, json.dumps({"Law": "일치하는 판례가 없습니다. 판례명을 확인하여 주세요."}, ensure_ascii=False), {}
            return b if isinstance(b, tuple) else (200, b, {"Content-Type": "application/json;charset=UTF-8"})
        if "nb" in q:
            self.nb_calls.append((q["nb"][0], q["org"][0]))
            items = [i for i in self.nb.get(q["nb"][0], [])]
            return 200, list_json(items), {"Content-Type": "application/json;charset=UTF-8"}
        return 404, b"", {}

    def transport(self):
        return FakeTransport(ROBOTS, default=self)


def seed(m, serial, case_no, date, terms, *, scope="supreme", court="대법원", extra=None):
    iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    m.upsert_document(f"lawgo_prec:{serial}", L.SOURCE_ID, canonical_url=L.PUBLIC_VIEW + str(serial), title="손해배상(기)", decision_date=iso,
                      year=int(date[:4]), doc_type="판결", extra={"relevance": "candidate", "matched_terms": terms, "serial": str(serial),
                                                                 "case_no": case_no, "court": court, "source_scope": scope, "raw_date": iso, **(extra or {})})


def run_cli(argv, corpus, transport, **kw):
    clock = kw.pop("clock", None) or FakeClock()
    out = io.StringIO()
    code = cli.main(["lawapi_body", *argv], transport=transport, clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus,
                    log_stream=io.StringIO(), oc_provider=lambda: FAKE_OC, resolver=lambda h: ["93.184.216.34"], **kw)
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


@pytest.fixture()
def seeded(corpus):
    m = Manifest(corpus / "manifest.sqlite")
    yield m
    m.close()


def doc(corpus, doc_id):
    m = Manifest(corpus / "manifest.sqlite")
    try:
        d = m.get_document(doc_id)
        return d, (json.loads(d["extra_json"]) if d and d["extra_json"] else {}), m.pages_of(doc_id=doc_id)
    finally:
        m.close()


# ------------------------------------------------------------------------------------------------ 순수 함수
def test_parse_body_json_and_xml_and_error_bodies():
    p = L.parse_body(prec_json(7, issue="판시<br/>사항").encode())
    assert p["format"] == "json" and p["fields"]["판시사항"] == "판시<br/>사항" and p["fields"]["판례정보일련번호"] == "7"
    xml = "<PrecService><판례정보일련번호>7</판례정보일련번호><판시사항>A</판시사항><판례내용>B&amp;C</판례내용></PrecService>".encode()
    x = L.parse_body(xml)
    assert x["format"] == "xml" and x["fields"]["판례내용"] == "B&C" and L.accept_body(xml, {}, "7") is None
    assert L.accept_body(prec_json(8).encode(), {}, "7").startswith("serial_mismatch")
    for bad, kind in ((b'{"Law":"\xec\x9d\xbc\xec\xb9\x98\xed\x95\x98\xeb\x8a\x94 \xed\x8c\x90\xeb\xa1\x80\xea\xb0\x80 \xec\x97\x86\xec\x8a\xb5\xeb\x8b\x88\xeb\x8b\xa4."}', "no_match"),
                      (b'{"result":"fail"}', "denied"), (b"<html>login</html>", "denied"), (b"not json", "malformed"),
                      (b'<!DOCTYPE x [<!ENTITY a "b">]><PrecService/>', "malformed")):
        with pytest.raises(L.BodyParseError) as e:
            L.parse_body(bad)
        assert e.value.kind == kind
        assert L.accept_body(bad, {}, "7") is not None


def test_html_to_text_only_br_and_entities():
    assert L.html_to_text("a<br/>b<br />c&lt;d&gt;&amp;") == "a\nb\nc<d>&"


def test_tier_follows_section_8_definition():
    t = L.compute_tier
    assert t(["정정보도"]) == "P-T1" and t(["기사삭제", "의견"]) == "P-T1" and t(["선거보도"]) == "P-T1"
    assert t(["기사", "명예훼손"]) == "P-T1" and t(["언론", "초상권"]) == "P-T1"
    assert t(["기사", "보도"]) == "P-T2" and t(["명예훼손"]) == "P-T2" and t(["실명", "의견"]) == "P-T2"
    assert t(["의견"]) == "P-T3" and t(["의견", "상당한 이유"]) == "P-T3"
    assert t([]) is None and t(None) is None
    assert [L.normalize_tier(x) for x in ("T1", "t2", "P-T3", "p_t1")] == ["P-T1", "P-T2", "P-T3", "P-T1"]
    with pytest.raises(ValueError):
        L.normalize_tier("T9")


def test_prefilter_requires_media_and_issue_terms_and_skips_other_senses():
    assert L.relevance_prefilter("피고 신문사가 기사를 게재하여 명예훼손이 문제 되었다.")["passed"]
    r = L.relevance_prefilter("증인신문과 피고인신문을 하였고 기자재를 납품하였다. 택시기사가 운전하였다. 명예훼손도 다툰다.")
    assert not r["passed"] and r["media_terms"] == [] and r["issue_terms"] == ["명예훼손"]
    assert not L.relevance_prefilter("방송 보도가 있었다.")["passed"]            # 쟁점어 없음
    assert L.relevance_prefilter("정정보도 청구는 부적법하다")["passed"]            # 정정보도 자체가 언론어(보도)와 쟁점어를 겸한다


def test_split_sections_en_banc_with_dissent_supplements_and_signature():
    text = L.html_to_text(EN_BANC)
    secs, info = L.split_sections(text)
    assert "".join(s["text"] for s in secs) == text                       # 구간은 겹침·빈틈 없이 원문을 덮는다
    assert [(s["kind"], s["opinion_kind"]) for s in secs] == [
        ("caption", None), ("order", None), ("reasons", "majority"), ("opinion", "dissent"),
        ("opinion", "majority_supplement"), ("opinion", "majority_supplement"), ("signature", None)]
    assert info["status"] == "split" and info["marker_count"] == 3
    assert all(text[s["char_start"]:s["char_end"]] == s["text"] for s in secs)
    assert "관여 법관의 의견이 일치하였으며" in secs[2]["text"]                   # '반대의견이 있다' 서술문은 표지가 아니다
    assert secs[3]["text"].startswith("5. 판결절차에서") and secs[-1]["text"].startswith("대법원장 김명수(재판장)")


def test_split_sections_old_style_markers_plain_and_unsplit():
    secs, info = L.split_sections(L.html_to_text(OLD_STYLE))
    assert [s["opinion_kind"] for s in secs if s["kind"] == "opinion"] == ["concurrence", "dissent"] and info["status"] == "split"
    secs, info = L.split_sections(L.html_to_text(PLAIN))
    assert info["status"] == "none" and [(s["kind"], s["opinion_kind"]) for s in secs] == [
        ("caption", None), ("order", None), ("reasons", "main"), ("signature", None)]
    secs, info = L.split_sections(L.html_to_text(UNSPLIT))
    assert info["status"] == "unsplit" and info["marker_count"] == 0                # 언급은 있으나 표지를 못 찾음 → 미분리 표시
    assert [s["kind"] for s in secs] == ["order", "reasons", "signature"]
    assert L.split_sections("한 줄짜리 본문")[1]["status"] == "none"


def test_split_case_numbers_and_parse_refs():
    assert L.split_case_numbers("2000다37524, 37531") == ["2000다37524", "2000다37531"]
    assert L.split_case_numbers("2005헌마165, 314, 555, 807, 2006헌가3") == ["2005헌마165", "2005헌마314", "2005헌마555", "2005헌마807", "2006헌가3"]
    assert L.split_case_numbers("85다카1973") == ["85다카1973"] and L.split_case_numbers("2022다284711, 284728") == ["2022다284711", "2022다284728"]
    refs, gap = L.parse_refs(L.html_to_text(REFS))
    assert gap == 0 and len(refs) == 5
    by = {(r["court"], r["date"]): r for r in refs}
    r1 = by[("대법원", "2002-01-22")]
    assert r1["numbers"] == ["2000다37524", "2000다37531"] and r1["court_class"] == "supreme" and r1["form"] == "판결"
    assert by[("대법원", "1991-05-14")]["kind"] == "자" and by[("대법원", "1991-05-14")]["form"] == "결정"
    h = by[("헌법재판소", "2006-06-29")]
    assert h["court_class"] == "constitutional" and h["numbers"][-1] == "2006헌가3" and h["form"] == "전원재판부결정"
    assert by[("서울고등법원", "2009-06-17")]["court_class"] == "lower"
    assert L.ref_doc_id(r1) == "lawgo_ref:대법원-2002-01-22-2000다37524"
    assert L.parse_refs("대법원 2002. 1. 22. 선고 (번호 누락) 판결")[1] == 1        # 해석하지 못한 날짜 표현은 gap 으로 드러난다


# ------------------------------------------------------------------------------------------------ 실행(CLI 끝까지)
def test_body_cli_end_to_end_resume_and_idempotent(corpus, seeded):
    seed(seeded, 1, "2020다1", "20210722", ["정정보도"])                       # P-T1
    seed(seeded, 2, "2020다2", "20210723", ["기사"])                             # P-T2 → 이번 실행 대상 아님
    seed(seeded, 3, "2020다3", "20210724", ["정정보도"], scope="non_court_source")  # 판례가 아님 → 대상 아님
    seed(seeded, 4, "2020다4", "20210725", ["명예훼손", "기사"])                 # P-T1
    leak = f"https://x/lawService.do?OC={FAKE_OC}&ID=1"
    srv = Server({"1": prec_json(1, EN_BANC, case_no="2020다1", kind="전원합의체 판결", issue="[1] 정정보도 " + leak,
                                 holding="요지", ref_cases=""), "4": prec_json(4, PLAIN, case_no="2020다4", date="20210725")})
    code, out = run_cli(["--tier", "T1", "--limit", "5", "--no-expand"], corpus, srv.transport())
    assert code == 0 and out["status"] == "completed", out
    r = out["result"]
    assert r["fetched"] == 2 and r["extracted"] == 2 and r["failed"] == 0 and srv.service_calls == ["1", "4"]
    assert r["processing_tier_counts"] == {"P-T1": 3, "P-T2": 1}
    assert r["opinion_split"] == {"none": 1, "split": 1, "unsplit": 0}
    d, extra, pages = doc(corpus, "lawgo_prec:1")
    assert d["status"] == "extracted" and d["extractor_version"] == L.EXTRACTOR_VERSION and len(d["extract_input_sha256"]) == 64
    assert extra["tier"] == "P-T1" and extra["processing_tier"] == "P-T1" and extra["body"]["raw_sha256"] == d["extract_input_sha256"]
    raw = (corpus / extra["body"]["raw_path"]).read_bytes()
    assert extra["body"]["raw_path"] == "raw/law_go_kr/prec-1.json" and json.loads(raw)["PrecService"]["판례정보일련번호"] == "1"
    ids = [p["page_id"] for p in pages]
    assert ids == sorted(ids) and ids[0] == "lawgo_prec:1#h000" and ids[1] == "lawgo_prec:1#s000" and ids[2] == "lawgo_prec:1#s001"      # 머리 쪽, 판시사항, 판결요지 → s004 부터 판례내용
    kinds = {json.loads(p["quality_json"])["locator"]: json.loads(p["quality_json"])["section_kind"] for p in pages}
    assert kinds["s000"] == "headnote_issue" and kinds["s004"] == "caption" and kinds["s006"] == "reasons" and kinds["s010"] == "signature"
    for p in pages:
        assert p["method"] == "html_section" and p["pdf_page_no"] is None and p["printed_page_no"] is None and p["att_id"] is None
        t = (corpus / p["text_path"]).read_text(encoding="utf-8")
        assert len(t) == p["char_count"] and FAKE_OC not in t               # OC 되울림은 http 계층이 지운다
    op = [json.loads(p["quality_json"]) for p in pages if json.loads(p["quality_json"])["section_kind"] == "opinion"]
    assert [o["opinion_kind"] for o in op] == ["dissent", "majority_supplement", "majority_supplement"]
    assert FAKE_OC not in raw.decode() and extra["body"]["opinion_split"]["status"] == "split"
    assert extra["body"]["refs_count"] == 0 and extra["body"]["verify_mismatches"] == []
    assert doc(corpus, "lawgo_prec:2")[0]["status"] == "discovered" and doc(corpus, "lawgo_prec:3")[0]["status"] == "discovered"
    # 재실행: 완료 문서는 다시 받지 않는다
    srv2 = Server({})
    code, out = run_cli(["--tier", "T1", "--no-expand"], corpus, srv2.transport())
    assert code == 0 and out["result"]["fetched"] == 0 and out["result"]["candidates_seen"] == 0 and srv2.service_calls == []
    # --tier T2 는 T2 만
    srv3 = Server({"2": prec_json(2, PLAIN, case_no="2020다2", date="20210723")})
    code, out = run_cli(["--tier", "T2", "--no-expand"], corpus, srv3.transport())
    assert srv3.service_calls == ["2"] and out["result"]["fetched"] == 1


def test_limit_counts_new_fetches_and_ignores_list_page_limit(corpus, seeded):
    for i in range(1, 5):
        seed(seeded, i, f"2020다{i}", f"2021072{i}", ["정정보도"])
    srv = Server({str(i): prec_json(i, PLAIN, case_no=f"2020다{i}", date=f"2021072{i}") for i in range(1, 5)})
    code, out = run_cli(["--tier", "T1", "--limit", "3", "--no-expand"], corpus, srv.transport())
    assert code == 0 and out["result"]["fetched"] == 3 and out["result"]["limit_hit"] and srv.service_calls == ["1", "2", "3"]
    code, out = run_cli(["--tier", "T1", "--max-docs", "3", "--no-expand"], corpus, srv.transport())
    assert out["result"]["fetched"] == 1 and srv.service_calls == ["1", "2", "3", "4"]      # 남은 1건만 이어 받는다


def test_time_pause_then_resume(corpus, seeded):
    for i in range(1, 4):
        seed(seeded, i, f"2020다{i}", f"2021072{i}", ["정정보도"])
    srv = Server({str(i): prec_json(i, PLAIN, case_no=f"2020다{i}", date=f"2021072{i}") for i in range(1, 4)})
    clock = FakeClock()
    tr = FakeTransport(ROBOTS, default=srv, clock=clock, latency=3.0)
    code, out = run_cli(["--tier", "T1", "--max-seconds", "7", "--no-expand"], corpus, tr, clock=clock)
    assert code == 0 and out["status"] == "paused_time_limit"
    done = len(srv.service_calls)
    assert 0 < done < 3
    code, out = run_cli(["--tier", "T1", "--no-expand"], corpus, srv.transport())
    assert out["status"] == "completed" and sorted(set(srv.service_calls)) == ["1", "2", "3"] and len(srv.service_calls) == 3


def test_failures_no_match_is_recorded_and_denied_stops_after_three(corpus, seeded):
    seed(seeded, 1, "2020다1", "20210721", ["정정보도"])
    seed(seeded, 2, "2020다2", "20210722", ["정정보도"])
    srv = Server({"2": prec_json(2, PLAIN, case_no="2020다2", date="20210722")})              # 1번은 '일치하는 판례가 없습니다'
    code, out = run_cli(["--tier", "T1", "--no-expand"], corpus, srv.transport())
    assert code == 0 and out["result"]["no_match"] == 1 and out["result"]["extracted"] == 1      # 한 건의 부재가 나머지를 막지 않는다
    d, _, _ = doc(corpus, "lawgo_prec:1")
    assert d["status"] == "failed"
    m = Manifest(corpus / "manifest.sqlite")
    try:
        f = [x for x in m.failures() if x["target"] == "lawgo_prec:1"]
        assert f and f[0]["stage"] == "body" and f[0]["error_class"] == "no_match"
    finally:
        m.close()
    for i in range(10, 15):                                                                  # 인증 거부 응답이 연속 3번이면 멈춘다
        seed(seeded, i, f"2020다{i}", "20210730", ["정정보도"])
    denied = (200, json.dumps({"result": "사용자 정보 검증에 실패하였습니다."}, ensure_ascii=False), {})
    srv = Server({str(i): denied for i in range(10, 15)})
    code, out = run_cli(["--tier", "T1", "--no-expand"], corpus, srv.transport())
    assert code == 1 and out["status"] == "failed" and "consecutive" in out["error"]
    assert len([s for s in srv.service_calls if int(s) >= 10]) == 3


def test_retry_exhaustion_skips_a_missing_serial(corpus, seeded):
    seed(seeded, 1, "2020다1", "20210721", ["정정보도"])
    for _ in range(L.MAX_BODY_RETRIES):
        srv = Server({})
        run_cli(["--tier", "T1", "--no-expand"], corpus, srv.transport())
        assert srv.service_calls == ["1"]
    srv = Server({})
    code, out = run_cli(["--tier", "T1", "--no-expand"], corpus, srv.transport())
    assert srv.service_calls == [] and out["result"]["skipped_exhausted"] == 1


def test_body_header_mismatch_is_recorded_not_hidden(corpus, seeded):
    seed(seeded, 1, "2020다1", "20210721", ["정정보도"])
    srv = Server({"1": prec_json(1, PLAIN, case_no="2020다9", date="20210722", court="서울고등법원")})
    code, out = run_cli(["--tier", "T1", "--no-expand"], corpus, srv.transport())
    assert out["result"]["mismatch_docs"] == 1
    _, extra, _ = doc(corpus, "lawgo_prec:1")
    assert {m["field"] for m in extra["body"]["verify_mismatches"]} == {"case_no", "decision_date", "court"}


def test_unsplit_opinions_flag_pages_for_review(corpus, seeded):
    seed(seeded, 1, "2020다1", "20210721", ["정정보도"])
    srv = Server({"1": prec_json(1, UNSPLIT, case_no="2020다1", date="20210721", kind="전원합의체 판결")})
    run_cli(["--tier", "T1", "--no-expand"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "lawgo_prec:1")
    assert extra["body"]["opinion_split"]["status"] == "unsplit" and "opinion_markers_not_found" in extra["body"]["warnings"]
    assert any(p["needs_review"] for p in pages) and not any(p["needs_review"] for p in pages if json.loads(p["quality_json"])["section_kind"] == "order")


# ------------------------------------------------------------------------------------------------ 참조판례 확장
def test_reference_expansion_local_api_referenced_only_and_idempotent(corpus, seeded):
    seed(seeded, 100, "2019다100", "20200101", ["정정보도"])
    seed(seeded, 200, "2000다37524", "20020122", ["의견"])                      # P-T3, 장부에 이미 있다 → 네트워크 없이 연결, 확장 표시
    seed(seeded, 201, "87다카1450", "19880614", ["기사"])                         # P-T2, 사건번호 토큰에 속함
    content = "<br/>".join(["【주    문】", "기각", "【이    유】정정보도 사건이다.", "대법관 송진훈(재판장) 변재승 윤재식(주심) 이규홍"])
    refs = ("대법원 2002. 1. 22. 선고 2000다37524, 37531 판결, 대법원 1988. 6. 14. 선고 87다카1450 판결, 대법원 1991. 5. 14.자 91마1 결정, "
            "대법원 2010. 3. 3. 선고 2009다3 판결, 헌법재판소 2006. 6. 29. 선고 2005헌마165 전원재판부 결정, 서울고등법원 2009. 6. 17. 선고 2008나80595 판결")
    srv = Server({
        "100": prec_json(100, content, case_no="2019다100", date="20200101", ref_cases=refs),
        "200": prec_json(200, PLAIN, case_no="2000다37524", date="20020122"),
        "201": prec_json(201, PLAIN, case_no="87다카1450", date="19880614"),
        "300": prec_json(300, PLAIN, case_no="91마1", date="19910514"),
        "400": prec_json(400, PLAIN, case_no="2009다3", date="20100303")},
        nb={"91마1": [list_item(300, "91마1", "1991.05.14")],
            "2009다3": [list_item(999, "2009다33", "2010.03.03"), list_item(400, "2009다3", "2010.03.03"), list_item(998, "2009다3", "2011.03.03")]})
    code, out = run_cli(["--tier", "T1"], corpus, srv.transport())
    assert code == 0 and out["status"] == "completed", out
    r = out["result"]
    assert r["refs_enqueued"] == 6 and r["refs_resolved_local"] == 2 and r["refs_resolved_api"] == 2 and r["refs_referenced_only"] == 2
    assert r["refs_new_docs"] == 2 and r["ref_queries"] == 3                    # 91마1, 2009다3, 2008나80595(하급심)
    assert sorted(srv.nb_calls) == [("2008나80595", "400202"), ("2009다3", "400201"), ("91마1", "400201")]
    assert sorted(srv.service_calls) == ["100", "200", "201", "300", "400"]      # 확장분(P-T3·P-T2 였던 것 포함)도 같은 T1 실행에서 본문을 받는다
    for s in ("200", "201", "300", "400"):
        _, extra, pages = doc(corpus, f"lawgo_prec:{s}")
        assert extra["processing_tier"] == "P-T1" and extra["expanded_from"] == ["lawgo_prec:100"] and extra["ref_depth"] == 1 and pages
    assert doc(corpus, "lawgo_prec:200")[1]["tier"] == "P-T3"                  # §8 tier 는 적중 검색어 기준 그대로
    assert doc(corpus, "lawgo_prec:300")[1]["tier"] is None and doc(corpus, "lawgo_prec:300")[1]["relevance"] == "referenced"
    m = Manifest(corpus / "manifest.sqlite")
    try:
        by = {x["doc_id"]: json.loads(x["extra_json"]) for x in m.documents_of(L.SOURCE_REFS)}
        st = {k: v["ref_state"] for k, v in by.items()}
        assert st["lawgo_ref:헌법재판소-2006-06-29-2005헌마165"] == "referenced_only"
        assert by["lawgo_ref:헌법재판소-2006-06-29-2005헌마165"]["reason"] == "constitutional_court_decision_is_not_a_prec_target"
        assert by["lawgo_ref:서울고등법원-2009-06-17-2008나80595"]["reason"] == "not_found_by_nb_search"
        assert by["lawgo_ref:대법원-2010-03-03-2009다3"]["resolved_doc_ids"] == ["lawgo_prec:400"]    # 접두 일치(2009다33)와 날짜 다른 행은 버린다
        assert by["lawgo_ref:대법원-2002-01-22-2000다37524"]["resolved_via"] == "local"
        assert {x["status"] for x in m.documents_of(L.SOURCE_REFS) if json.loads(x["extra_json"])["ref_state"] == "referenced_only"} == {"withheld"}
        assert m.get_document("lawgo_prec:999") is None and m.get_document("lawgo_prec:998") is None
        n_docs = m.counts()["documents"]
    finally:
        m.close()
    srv2 = Server({}, nb={})
    code, out = run_cli(["--tier", "T1"], corpus, srv2.transport())
    assert srv2.service_calls == [] and srv2.nb_calls == [] and out["result"]["refs_enqueued"] == 0
    m = Manifest(corpus / "manifest.sqlite")
    try:
        assert m.counts()["documents"] == n_docs
    finally:
        m.close()


def test_ref_depth_limit_and_no_expand_do_not_search(corpus, seeded):
    seed(seeded, 100, "2019다100", "20200101", ["정정보도"])
    content = "<br/>".join(["【주    문】", "기각", "【이    유】정정보도 사건이다."])
    srv = Server({"100": prec_json(100, content, case_no="2019다100", date="20200101", ref_cases="대법원 2010. 3. 3. 선고 2009다3 판결")})
    code, out = run_cli(["--tier", "T1", "--ref-depth", "0"], corpus, srv.transport())
    assert out["result"]["refs_depth_limited"] == 1 and out["result"]["refs_enqueued"] == 0 and srv.nb_calls == []
    seeded.update_document_fields("lawgo_prec:100", extra_merge={"expand_state": "pending"})
    code, out = run_cli(["--tier", "T1", "--no-expand"], corpus, srv.transport())
    assert out["result"]["refs_enqueued"] == 1 and out["result"]["ref_queries"] == 0 and srv.nb_calls == []     # 큐에는 넣되 검색은 안 한다


# ------------------------------------------------------------------------------------------------ 단계 버전·입력 해시
def test_version_bump_reextracts_from_saved_raw_without_network(corpus, seeded, monkeypatch):
    seed(seeded, 1, "2020다1", "20210721", ["정정보도"])
    srv = Server({"1": prec_json(1, EN_BANC, case_no="2020다1", date="20210721", issue="판시")})
    run_cli(["--tier", "T1", "--no-expand"], corpus, srv.transport())
    m = Manifest(corpus / "manifest.sqlite")
    m.upsert_chunk("c1", "lawgo_prec:1", "x", "html_section", "s006", page_id="lawgo_prec:1#s006")
    m.close()
    monkeypatch.setattr(L, "EXTRACTOR_VERSION", "lawgo-prec-html-test-bump")
    srv2 = Server({})
    code, out = run_cli(["--tier", "T1", "--no-expand"], corpus, srv2.transport())
    assert srv2.service_calls == [] and out["result"]["reextracted"] == 1
    d, extra, pages = doc(corpus, "lawgo_prec:1")
    assert d["extractor_version"] == "lawgo-prec-html-test-bump" and all(p["extractor_version"] == "lawgo-prec-html-test-bump" for p in pages)
    m = Manifest(corpus / "manifest.sqlite")
    try:
        assert m.p2_counts()["chunks"] == 0                                    # 입력이 바뀐 단계의 파생 chunk 는 지워진다
    finally:
        m.close()


def test_crash_between_fetch_and_extract_resumes_from_raw(corpus, seeded, monkeypatch):
    seed(seeded, 1, "2020다1", "20210721", ["정정보도"])
    srv = Server({"1": prec_json(1, PLAIN, case_no="2020다1", date="20210721")})
    real = L._BodyRun._extract
    calls = {"n": 0}

    def boom(self, *a, **k):
        calls["n"] += 1
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(L._BodyRun, "_extract", boom)
    code, out = run_cli(["--tier", "T1", "--no-expand"], corpus, srv.transport())
    assert code == 1 and doc(corpus, "lawgo_prec:1")[0]["status"] == "fetched"
    monkeypatch.setattr(L._BodyRun, "_extract", real)
    srv2 = Server({})
    code, out = run_cli(["--tier", "T1", "--no-expand"], corpus, srv2.transport())
    assert code == 0 and srv2.service_calls == [] and out["result"]["resumed_from_raw"] == 1
    assert doc(corpus, "lawgo_prec:1")[0]["status"] == "extracted"


def test_is_structure_candidate_uses_tier_or_prefilter():
    body = lambda passed: {"text_available": True, "prefilter": {"passed": passed}}
    assert L.is_structure_candidate({"processing_tier": "P-T1", "body": body(False)})
    assert L.is_structure_candidate({"processing_tier": "P-T2", "body": body(True)})
    assert not L.is_structure_candidate({"processing_tier": "P-T3", "body": body(False)})
    assert not L.is_structure_candidate({"processing_tier": "P-T1"})


def test_joined_case_numbers_are_not_a_mismatch_and_are_indexed(corpus, seeded):
    seed(seeded, 1, "2020다1", "20210721", ["정정보도"])
    seed(seeded, 2, "2019다2", "20200101", ["정정보도"])
    srv = Server({"1": prec_json(1, PLAIN, case_no="2020다1, 2", date="20210721"),
                  "2": prec_json(2, PLAIN, case_no="2019다2", date="20200101", ref_cases="대법원 2021. 7. 21. 선고 2020다2 판결")})
    code, out = run_cli(["--serial", "1", "--no-expand"], corpus, srv.transport())
    _, extra, _ = doc(corpus, "lawgo_prec:1")
    assert out["result"]["mismatch_docs"] == 0 and extra["body"]["case_nos"] == ["2020다1", "2020다2"]
    code, out = run_cli(["--serial", "2", "--no-expand"], corpus, srv.transport())
    assert out["result"]["refs_resolved_local"] == 1 and out["result"]["refs_enqueued"] == 1 and srv.nb_calls == []     # 병합 번호로 로컬 해결


def test_refresh_refetch_unchanged_keeps_derived_changed_input_deletes_them(corpus, seeded):
    seed(seeded, 1, "2020다1", "20210721", ["정정보도"])
    srv = Server({"1": prec_json(1, PLAIN, case_no="2020다1", date="20210721")})
    run_cli(["--tier", "T1", "--no-expand"], corpus, srv.transport())
    seeded.upsert_chunk("c1", "lawgo_prec:1", "x", "html_section", "s006", page_id="lawgo_prec:1#s006")
    seeded.set_document_status("lawgo_prec:1", "structured")
    code, out = run_cli(["--tier", "T1", "--no-expand", "--refresh"], corpus, srv.transport())
    assert out["result"]["refetch_unchanged"] == 1 and out["result"]["extracted"] == 0 and srv.service_calls == ["1", "1"]
    assert seeded.p2_counts()["chunks"] == 1 and doc(corpus, "lawgo_prec:1")[0]["status"] == "structured"
    srv.bodies["1"] = prec_json(1, PLAIN.replace("기각하기로", "기각하기로 하고"), case_no="2020다1", date="20210721")
    code, out = run_cli(["--tier", "T1", "--no-expand", "--refresh"], corpus, srv.transport())
    assert out["result"]["extracted"] == 1 and seeded.p2_counts()["chunks"] == 0           # 입력 해시가 바뀐 단계부터 다시, 파생 chunk 삭제
    d, extra, _ = doc(corpus, "lawgo_prec:1")
    assert d["status"] == "extracted" and extra["body"]["raw_sha256"] == d["extract_input_sha256"]


def test_dry_run_writes_no_files_and_missing_serials_are_reported(corpus, seeded):
    seed(seeded, 1, "2020다1", "20210721", ["정정보도"])
    srv = Server({"1": prec_json(1, PLAIN, case_no="2020다1", date="20210721")})
    code, out = run_cli(["--tier", "T1", "--dry-run"], corpus, srv.transport())
    assert out["status"] == "dry_run_stopped" and srv.service_calls == [] and not (corpus / "raw").exists() and not (corpus / "text").exists()
    code, out = run_cli(["--serial", "1", "--serial", "999", "--no-expand"], corpus, srv.transport())
    assert out["result"]["serials_missing"] == ["999"] and out["result"]["extracted"] == 1


def test_export_commands_survive_reference_pointer_rows(corpus, seeded, tmp_path):
    """참조 포인터 행(lawgo_prec_refs)이 있어도 공용 내보내기(export-inventory·export-coverage)가 깨지지 않는다."""
    seed(seeded, 100, "2019다100", "20200101", ["정정보도"])
    content = "<br/>".join(["【주    문】", "기각", "【이    유】정정보도 사건이다."])
    srv = Server({"100": prec_json(100, content, case_no="2019다100", date="20200101",
                                   ref_cases="대법원 2010. 3. 3. 선고 2009다3 판결, 헌법재판소 2006. 6. 29. 선고 2005헌마165 결정")})
    run_cli(["--tier", "T1"], corpus, srv.transport())
    out = io.StringIO()
    assert cli.main(["export-inventory", "--out-dir", str(tmp_path / "inv")], corpus=corpus, stdout=out, log_stream=io.StringIO()) == 0
    out = io.StringIO()
    code = cli.main(["export-coverage", "--out-dir", str(tmp_path / "cov")], corpus=corpus, stdout=out, log_stream=io.StringIO())
    assert code == 0, out.getvalue()


def test_header_page_carries_the_decision_own_case_number_date_and_court(corpus, seeded):
    """V-meta-1: 본문 필드에 없는 사건번호·선고일자·법원명을 머리 쪽(#h000)으로 저장한다. 판시사항 등 번호(s000~)는 그대로다."""
    seed(seeded, 1, "2020다1", "20210722", ["정정보도"])
    srv = Server({"1": prec_json(1, EN_BANC, case_no="2022다284711, 284728", date="20260212", court="대법원", issue="[1] 정정보도", holding="요지")})
    code, out = run_cli(["--tier", "T1", "--limit", "5", "--no-expand"], corpus, srv.transport())
    assert code == 0, out
    d, extra, pages = doc(corpus, "lawgo_prec:1")
    ids = [p["page_id"] for p in pages]
    assert ids[0] == "lawgo_prec:1#h000" and "lawgo_prec:1#s000" in ids and "lawgo_prec:1#s004" in ids
    head = next(p for p in pages if p["page_id"].endswith("#h000"))
    assert json.loads(head["quality_json"])["section_kind"] == "case_header"
    t = (corpus / head["text_path"]).read_text(encoding="utf-8")
    assert "2022다284711, 284728" in t and "2026. 2. 12." in t and "대법원" in t
    assert extra["body"]["body_case_no"] == "2022다284711, 284728" and extra["body"]["text_available"] is True

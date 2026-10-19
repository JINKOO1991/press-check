"""헌재결정례 어댑터(lawapi_detc 번호 해석 · lawapi_detc_body 본문): 네트워크 없는 가짜 transport.

모든 fixture 는 합성이다(가짜 번호·'갑 재판관'·'A법 제1조'). 실제 결정문의 문장·당사자 성명은 넣지 않는다(공개 저장소, 재배포는 permission_required).
목록 응답 형식(DetcSearch/Detc)은 실응답 구조를 따르고, 본문은 G0 실측대로 XML(DetcService)이다."""
import io
import json
import urllib.parse as up
from xml.sax.saxutils import escape

import pytest

from conftest import FAKE_OC, FakeClock, FakeTransport
from press_review.ingestion import __main__ as cli, rights as R
from press_review.ingestion.admission import CASE_ID_PREFIX
from press_review.ingestion.manifest import Manifest
from press_review.ingestion.structure_records import compact
from press_review.ingestion.sources import lawapi as L, lawapi_detc as D, lawapi_detc_body as B

ROBOTS = {"https://www.law.go.kr/robots.txt": (200, "User-agent: *\nAllow: /\n", {})}
JSON_H = {"Content-Type": "application/json;charset=UTF-8"}
XML_H = {"Content-Type": "application/xml;charset=UTF-8"}
ORDER = "1. A법 제1조는 헌법에 위반되지 아니한다.\n2. 나머지 청구를 각하한다."


def case_text(party="2020헌마1", tail="", order=ORDER):
    return f"【당 사 자】\n청구인 갑 대리인 변호사 을\n{party}\n【주　　문】\n{order}\n【이  유】\n1. 사건\n다수 판단이다.\n{tail}"


def detc_xml(serial, case_no="2020헌마1", full=None, date="20200102", **over):
    f = {"헌재결정례일련번호": str(serial), "종국일자": date, "사건번호": case_no, "사건명": "A법 제1조 위헌확인", "사건종류코드": "430201",
         "사건종류명": "헌마", "재판부구분코드": "430301", "판시사항": "가 쟁점", "결정요지": "나 요지", "참조조문": "A법 제1조", "참조판례": "",
         "심판대상조문": "", "전문": case_text() if full is None else full}
    f.update(over)
    return "<DetcService>" + "".join(f"<{k}>{escape(v)}</{k}>" for k, v in f.items()) + "</DetcService>"


def list_item(serial, case_no, date="2020.01.02"):
    return {"id": "1", "사건번호": case_no, "종국일자": date, "헌재결정례일련번호": str(serial), "사건명": "A법 제1조 위헌확인",
            "헌재결정례상세링크": f"/DRF/lawService.do?OC={FAKE_OC}&target=detc&ID={serial}&type=HTML&mobileYn="}


def list_json(items, total=None):
    return json.dumps({"DetcSearch": {"resultMsg": "success", "target": "detc", "totalCnt": str(len(items) if total is None else total),
                                      "page": "1", "section": "evtNm", **({"Detc": items} if items else {})}}, ensure_ascii=False)


class Server:
    """lawService(ID)·lawSearch(nb · search=2) 가짜 서버. bodies: {serial: xml | (status, body, headers)}, lists: {번호: [item…] | 오류 본문 문자열}, search: {번호: [item…]}."""

    def __init__(self, bodies=None, lists=None, search=None):
        self.bodies, self.lists, self.search = dict(bodies or {}), dict(lists or {}), dict(search or {})
        self.service_calls, self.nb_calls, self.search_calls = [], [], []

    def __call__(self, url, n):
        q = up.parse_qs(up.urlsplit(url).query)
        if "ID" in q:
            self.service_calls.append(q["ID"][0])
            b = self.bodies.get(q["ID"][0])
            if b is None:
                return 200, "<Law>일치하는 헌재결정례가 없습니다. 헌재결정례명을 확인하여 주세요.</Law>", XML_H
            return b if isinstance(b, tuple) else (200, b, XML_H)
        if q.get("search") == ["2"]:
            self.search_calls.append(q["query"][0])
            return 200, list_json(self.search.get(q["query"][0], [])), JSON_H
        if "nb" in q:
            nb, page = q["nb"][0], int(q["page"][0])
            self.nb_calls.append((nb, page))
            items = self.lists.get(nb, [])
            if isinstance(items, str):
                return 200, items, JSON_H
            return 200, list_json(items[(page - 1) * 100:page * 100], total=len(items)), JSON_H
        return 404, b"", {}

    def transport(self):
        return FakeTransport(ROBOTS, default=self)


def run_cli(adapter, argv, corpus, transport):
    clock = FakeClock()
    out = io.StringIO()
    code = cli.main([adapter, *argv], transport=transport, clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus,
                    log_stream=io.StringIO(), oc_provider=lambda: FAKE_OC, resolver=lambda h: ["93.184.216.34"])
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


@pytest.fixture()
def seeded(corpus):
    m = Manifest(corpus / "manifest.sqlite")
    m.ensure_source(D.SOURCE_ID, D.FAMILY, "헌재결정례", "https://www.law.go.kr/")
    yield m
    m.close()


def seed(m, serial, case_no="2020헌마1", iso="2020-01-02"):
    m.upsert_document(f"lawgo_detc:{serial}", D.SOURCE_ID, canonical_url=D.PUBLIC_VIEW + str(serial), title="합성", decision_date=iso,
                      year=int(iso[:4]), extra={"serial": str(serial), "case_no": case_no, "court": "헌법재판소"})


def seed_ref(m, number, iso):
    m.ensure_source(L.SOURCE_REFS, L.FAMILY, "refs", "https://www.law.go.kr/")
    m.upsert_document(f"lawgo_ref:헌법재판소-{iso}-{number}", L.SOURCE_REFS, title="r", decision_date=iso, year=int(iso[:4]), doc_type="reference",
                      extra={"court_class": "constitutional", "numbers": [number], "date": iso})


def doc(corpus, doc_id):
    m = Manifest(corpus / "manifest.sqlite")
    try:
        d = m.get_document(doc_id)
        return d, (json.loads(d["extra_json"]) if d and d["extra_json"] else {}), m.pages_of(doc_id=doc_id)
    finally:
        m.close()


def failures(corpus, target=None):
    m = Manifest(corpus / "manifest.sqlite")
    try:
        return {f["target"]: f for f in m.failures() if f["stage"] == "resolve" and (target is None or f["target"] == target)}
    finally:
        m.close()


# ------------------------------------------------------------------------------------------------ 번호 해석
@pytest.mark.parametrize("raw,ok", [("2005헌마165", "2005헌마165"), (" 2005 헌마 165 등", "2005헌마165"), ("２００５헌마１６５", "2005헌마165"), ("99헌마480등", "99헌마480"),
                                    ("2005마165", None), ("헌마165", None), ("2005헌마", None), ("2005헌타165", None), ("", None)])
def test_normalize_number(raw, ok):
    assert D.normalize_number(raw) == ok


def test_parse_detc_list_shapes():
    assert D.parse_detc_list(list_json([], 0).encode()) == (0, [])
    one = json.dumps({"DetcSearch": {"totalCnt": "1", "Detc": list_item(5, "2020헌마1")}}).encode()
    assert D.parse_detc_list(one)[1][0]["헌재결정례일련번호"] == "5"
    for bad in (b"not json", b'{"result":"fail"}', b'{"DetcSearch":{"totalCnt":"x"}}', b'{"DetcSearch":{"totalCnt":"1","Detc":"s"}}'):
        with pytest.raises(D.ListParseError):
            D.parse_detc_list(bad)
        assert D.accept_detc_list(bad, {}) is not None


def test_exact_tokens_only_partial_matches_ignored_and_no_detail_link_stored(corpus):
    items = [list_item(1, "88헌마22"), list_item(2, "2022헌마688"), list_item(3, "188헌마22"), list_item(4, "88헌마220")]
    srv = Server(lists={"88헌마22": items})
    code, out = run_cli("lawapi_detc", ["--number", "88헌마22"], corpus, srv.transport())
    assert code == 0 and out["status"] == "completed", out
    assert out["result"]["resolved_exact"] == 1 and out["result"]["docs_new"] == 1 and out["result"]["multi_serial_numbers"] == 0
    d, extra, _ = doc(corpus, "lawgo_detc:1")
    assert d["status"] == "discovered" and d["canonical_url"] == "https://www.law.go.kr/LSW/detcInfoP.do?detcSeq=1"
    assert d["decision_date"] == "2020-01-02" and extra["resolved_numbers"] == ["88헌마22"] and extra["resolved_via"] == {"88헌마22": "exact"}
    assert d["detail_url"] is None and "multi_serial" not in extra
    assert all(doc(corpus, f"lawgo_detc:{s}")[0] is None for s in (2, 3, 4))
    summary = json.loads((corpus / "inventory" / D.RESOLVE_FILE).read_text(encoding="utf-8"))
    assert summary["counts"]["resolved_exact"] == 1 and "청구인" not in json.dumps(summary, ensure_ascii=False)


def test_multi_page_list_and_trailing_deung(corpus):
    filler = [list_item(1000 + i, f"2019헌마{i + 1}") for i in range(130)]
    srv = Server(lists={"2005헌마165": filler + [list_item(7, "2005헌마165, 314등")]})
    code, out = run_cli("lawapi_detc", ["--number", "2005헌마165등", "--number", "2005 헌마165"], corpus, srv.transport())
    assert code == 0 and out["result"]["duplicates"] == 1 and srv.nb_calls == [("2005헌마165", 1), ("2005헌마165", 2)]
    assert doc(corpus, "lawgo_detc:7")[1]["resolved_numbers"] == ["2005헌마165"]


def test_page_overflow_registers_nothing_and_retries_exhaust(corpus):
    srv = Server(lists={"2005헌마165": [list_item(1000 + i, "2019헌마1") for i in range(301)] + [list_item(9, "2005헌마165")]})
    for i in range(3):
        code, out = run_cli("lawapi_detc", ["--number", "2005헌마165"], corpus, srv.transport())
        assert code == 0 and out["result"]["page_overflow"] == 1 and out["result"]["docs_new"] == 0
    f = failures(corpus, "lawgo_detc_no:2005헌마165")["lawgo_detc_no:2005헌마165"]
    assert f["error_class"] == "page_overflow" and f["retry_count"] == 2
    assert {c[1] for c in srv.nb_calls} == {1}                           # 1쪽에서 totalCnt 로 초과를 알고 2쪽 이후는 요청하지 않는다(반복 실행은 HTTP 캐시 적중)
    code, out = run_cli("lawapi_detc", ["--number", "2005헌마165"], corpus, srv.transport())
    assert out["result"]["skipped_exhausted"] == 1 and out["result"]["requests"] == 0
    assert doc(corpus, "lawgo_detc:9")[0] is None


def test_malformed_number_is_recorded_without_request_or_correction(corpus):
    srv = Server()
    code, out = run_cli("lawapi_detc", ["--number", "2005마165", "--number", "헌마165"], corpus, srv.transport())
    assert code == 0 and out["result"]["malformed_number"] == 2 and srv.nb_calls == []
    fs = failures(corpus)
    assert fs["lawgo_detc_no:2005마165"]["error_class"] == "malformed_number" and fs["lawgo_detc_no:2005마165"]["retry_count"] == 0


def test_multi_serial_marks_every_matching_document(corpus):
    srv = Server(lists={"93헌가13": [list_item(11, "93헌가13", "1993.02.23"), list_item(12, "93헌가13, 14", "1993.03.01")]})
    code, out = run_cli("lawapi_detc", ["--number", "93헌가13"], corpus, srv.transport())
    assert out["result"]["multi_serial_numbers"] == 1 and out["result"]["docs_new"] == 2
    assert all(doc(corpus, f"lawgo_detc:{s}")[1]["multi_serial"] is True for s in (11, 12))


def test_numbers_file_and_resume_skips_resolved(corpus, tmp_path):
    f = tmp_path / "nums.txt"
    f.write_text("# 주석\n2020헌마1\n\n2020헌마2\n", encoding="utf-8")
    srv = Server(lists={"2020헌마1": [list_item(1, "2020헌마1")], "2020헌마2": [list_item(2, "2020헌마2")]})
    code, out = run_cli("lawapi_detc", ["--numbers-file", str(f)], corpus, srv.transport())
    assert out["result"]["resolved_exact"] == 2
    srv2 = Server()
    code, out = run_cli("lawapi_detc", ["--numbers-file", str(f)], corpus, srv2.transport())
    assert out["result"]["skipped_resolved"] == 2 and srv2.nb_calls == []


def test_missing_input_is_a_usage_error_after_gate(corpus):
    code, out = run_cli("lawapi_detc", [], corpus, Server().transport())
    assert code == 1 and out["status"] == "failed" and "no case numbers" in out["error"]


def test_consecutive_list_failures_stop_after_three(corpus):
    bad = "<html>login</html>"
    srv = Server(lists={n: bad for n in ("2020헌마1", "2020헌마2", "2020헌마3", "2020헌마4")})
    code, out = run_cli("lawapi_detc", [a for n in range(1, 5) for a in ("--number", f"2020헌마{n}")], corpus, srv.transport())
    assert code == 1 and out["status"] == "failed" and "consecutive" in out["error"]
    assert [c[0] for c in srv.nb_calls] == ["2020헌마1", "2020헌마2", "2020헌마3"]
    assert {f["error_class"] for f in failures(corpus).values()} == {"http_content_rejected"} and len(failures(corpus)) == 2     # 3번째는 중단 예외가 먼저 났다


# ------------------------------------------------------------------------------------------------ 로컬 해석·병합 검증·날짜 대조
def test_deferred_local_never_exhausts_then_resolves_from_fetched_body(corpus, seeded):
    seed(seeded, 1, "2020헌마1")
    srv = Server(lists={"2020헌마2": []}, bodies={"1": detc_xml(1, "2020헌마1", full=case_text("2020헌마1\n2020헌마2 (병합)"))})
    for _ in range(4):
        code, out = run_cli("lawapi_detc", ["--number", "2020헌마2"], corpus, srv.transport())
        assert code == 0 and out["result"]["deferred_local"] == 1 and out["result"]["skipped_exhausted"] == 0
    f = failures(corpus)["lawgo_detc_no:2020헌마2"]
    assert f["error_class"] == "deferred_local" and f["retry_count"] == 0
    code, out = run_cli("lawapi_detc_body", ["--serial", "1"], corpus, srv.transport())
    assert code == 0 and out["result"]["extracted"] == 1
    code, out = run_cli("lawapi_detc", ["--number", "2020헌마2"], corpus, srv.transport())
    assert out["result"]["resolved_local"] == 1 and failures(corpus) == {}
    _, extra, _ = doc(corpus, "lawgo_detc:1")
    assert extra["resolved_numbers"] == ["2020헌마2"] and extra["resolved_via"]["2020헌마2"] == "local" and extra["body"]["case_nos"] == ["2020헌마1", "2020헌마2"]


def test_merged_search_verifies_party_section_only(corpus):
    cand = list_item(21, "2020헌마1")
    srv = Server(search={"2020헌마2": [cand], "2020헌마9": [cand], "2020헌마8": [cand]},
                 bodies={"21": detc_xml(21, "2020헌마1", full=case_text("2020헌마1\n2020헌마2 (병합)"), 판시사항="이 사건은 2020헌마9 와 같다")})
    code, out = run_cli("lawapi_detc", ["--number", "2020헌마2", "--number", "2020헌마9", "--number", "2020헌마8", "--merged-search"], corpus, srv.transport())
    assert code == 0, out
    r = out["result"]
    assert r["resolved_merged"] == 1 and r["merged_unverified"] == 1 and r["not_found"] == 1 and r["deferred_local"] == 0
    assert doc(corpus, "lawgo_detc:21")[1]["resolved_numbers"] == ["2020헌마2"]      # 판시사항 머리의 2020헌마9 는 근거가 아니다
    fs = failures(corpus)
    assert fs["lawgo_detc_no:2020헌마9"]["error_class"] == "merged_unverified" and fs["lawgo_detc_no:2020헌마8"]["error_class"] == "not_found"
    # not_found 는 --retry-not-found 가 있어야 다시 시도한다
    code, out = run_cli("lawapi_detc", ["--number", "2020헌마8", "--merged-search"], corpus, srv.transport())
    assert out["result"]["skipped_not_found"] == 1 and out["result"]["requests"] == 0
    code, out = run_cli("lawapi_detc", ["--number", "2020헌마8", "--merged-search", "--retry-not-found"], corpus, srv.transport())
    assert out["result"]["not_found"] == 1 and out["result"]["requests"] == 3      # nb 목록 + 본문검색 목록 + 후보 본문(HTTP 캐시 적중이라 서버 호출 수와 다르다)


def test_date_mismatch_blocks_link_exact_and_merged(corpus, seeded):
    seed_ref(seeded, "2020헌마2", "2019-05-05")
    seed_ref(seeded, "2020헌마3", "2020-01-02")
    cand = list_item(31, "2020헌마1")
    srv = Server(lists={"2020헌마2": [list_item(30, "2020헌마2")], "2020헌마3": [list_item(32, "2020헌마3")]}, search={"2020헌마5": [cand]},
                 bodies={"31": detc_xml(31, "2020헌마1", full=case_text("2020헌마1\n2020헌마5 (병합)"))})
    seed_ref(seeded, "2020헌마5", "2018-01-01")
    code, out = run_cli("lawapi_detc", ["--number", "2020헌마2", "--number", "2020헌마3", "--number", "2020헌마5", "--merged-search"], corpus, srv.transport())
    r = out["result"]
    assert r["date_mismatch"] == 2 and r["resolved_exact"] == 1 and r["resolved_merged"] == 0
    assert doc(corpus, "lawgo_detc:30")[0] is None and doc(corpus, "lawgo_detc:31")[0] is None and doc(corpus, "lawgo_detc:32")[0] is not None
    assert failures(corpus)["lawgo_detc_no:2020헌마2"]["error_class"] == "date_mismatch"


# ------------------------------------------------------------------------------------------------ 본문
def test_body_end_to_end_pages_case_nos_dispositions_and_resume(corpus, seeded, monkeypatch):
    seed(seeded, 1)
    seed(seeded, 2, "2020헌마5")
    tail = "6. 재판관 가나다의 반대의견\n합성 반대 이유이다.\n"
    leak = f"https://x/lawService.do?OC={FAKE_OC}&ID=1"
    srv = Server(bodies={"1": detc_xml(1, full=case_text("2020헌마1\n2020헌마2, 2020헌마3 (병합)", tail), 참조판례=leak, 심판대상조문="A법 제1조"),
                         "2": detc_xml(2, "2020헌마5", full="【주 문】\n1. 청구를 기각한다.\n【이 유】\n다수 판단이다.")})
    code, out = run_cli("lawapi_detc_body", ["--serial", "1", "--serial", "2"], corpus, srv.transport())
    assert code == 0 and out["status"] == "completed", out
    assert out["result"]["fetched"] == 2 and out["result"]["extracted"] == 2 and out["result"]["failed"] == 0 and srv.service_calls == ["1", "2"]
    d, extra, pages = doc(corpus, "lawgo_detc:1")
    b = extra["body"]
    assert d["status"] == "extracted" and d["extractor_version"] == B.EXTRACTOR_VERSION and b["raw_path"] == "raw/law_go_kr/detc-1.xml"
    assert b["body_case_no"] == "2020헌마1" and b["case_nos"] == ["2020헌마1", "2020헌마2", "2020헌마3"] and b["merged_numbers"] == b["case_nos"]
    assert b["dispositions"] == ["합헌", "각하"] and b["opinion_status"] == "split" and b["text_available"] is True
    assert extra["codes"] == {"case_kind_name": "헌마", "case_kind_code": "430201", "bench_code": "430301"}
    ids = [p["page_id"] for p in pages]
    assert ids == sorted(ids) and ids[0] == "lawgo_detc:1#h000" and "lawgo_detc:1#s000" in ids and "lawgo_detc:1#s004" in ids
    raw = (corpus / b["raw_path"]).read_bytes()
    assert "청구인 갑" in raw.decode()                                            # 당사자 구간은 raw 에는 남고
    for p in pages:
        t = (corpus / p["text_path"]).read_text(encoding="utf-8")
        assert "청구인 갑" not in t and "2020헌마2, 2020헌마3" not in t and len(t) == p["char_count"]      # 쪽(chunk 입력)으로는 만들지 않는다
    q = {json.loads(p["quality_json"])["locator"]: json.loads(p["quality_json"]) for p in pages}
    assert q["h000"]["section_kind"] == "case_header" and "사건번호: 2020헌마1" in (corpus / pages[0]["text_path"]).read_text(encoding="utf-8")
    assert [x["opinion_kind"] for x in q.values() if x["section_kind"] == "opinion"] == ["dissent"]
    assert all(p["needs_review"] == 0 for p in pages)
    assert doc(corpus, "lawgo_detc:2")[1]["body"]["opinion_status"] == "none"
    # 재실행: 완료 문서는 다시 받지 않는다
    srv2 = Server()
    code, out = run_cli("lawapi_detc_body", ["--serial", "1"], corpus, srv2.transport())
    assert out["result"]["candidates_seen"] == 0 and srv2.service_calls == []


def test_crash_between_fetch_and_extract_resumes_from_raw(corpus, seeded, monkeypatch):
    seed(seeded, 1)
    srv = Server(bodies={"1": detc_xml(1)})
    real = B._BodyRun._extract
    monkeypatch.setattr(B._BodyRun, "_extract", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("simulated crash")))
    code, out = run_cli("lawapi_detc_body", ["--serial", "1"], corpus, srv.transport())
    assert code == 1 and doc(corpus, "lawgo_detc:1")[0]["status"] == "fetched"
    monkeypatch.setattr(B._BodyRun, "_extract", real)
    srv2 = Server()
    code, out = run_cli("lawapi_detc_body", ["--serial", "1"], corpus, srv2.transport())
    assert code == 0 and srv2.service_calls == [] and out["result"]["resumed_from_raw"] == 1 and out["result"]["fetched"] == 0
    assert doc(corpus, "lawgo_detc:1")[0]["status"] == "extracted"


def test_unsplit_and_headnote_opinions_flag_pages(corpus, seeded):
    seed(seeded, 1)
    seed(seeded, 2, "2020헌마2")
    srv = Server(bodies={
        "1": detc_xml(1, full=case_text(tail="이 결정에는 재판관 갑의 반대의견이 있다.\n"), 결정요지="다수 요지이다.\n(1) 재판관 갑의 반대의견\n반대 요지이다."),
        "2": detc_xml(2, "2020헌마2", 결정요지="요지이다. 재판관 갑의 반대의견이 있다.")})
    code, out = run_cli("lawapi_detc_body", ["--serial", "1", "--serial", "2"], corpus, srv.transport())
    assert code == 0 and out["result"]["opinion_status"]["unsplit"] == 1
    _, extra, pages = doc(corpus, "lawgo_detc:1")
    q = {json.loads(p["quality_json"])["locator"]: (json.loads(p["quality_json"]), p["needs_review"]) for p in pages}
    assert extra["body"]["opinion_status"] == "unsplit" and "opinion_markers_not_found" in extra["body"]["warnings"]
    assert q["s007"][0]["section_kind"] == "reasons" and q["s007"][1] == 1 and q["s006"][0]["section_kind"] == "order" and q["s006"][1] == 0     # s005 는 당사자(쪽 없음)
    assert q["s500"][0]["section_kind"] == "headnote_opinion" and q["s500"][0]["opinion_kind"] == "dissent" and q["s500"][1] == 0
    assert q["s001"][0]["opinion_kind"] == "majority"
    _, extra2, pages2 = doc(corpus, "lawgo_detc:2")
    hold = [json.loads(p["quality_json"]) | {"nr": p["needs_review"]} for p in pages2 if json.loads(p["quality_json"])["field"] == "결정요지"]
    assert extra2["body"]["headnote_split"]["headnote_split"] == "unsplit" and hold[0]["opinion_kind"] is None and hold[0]["nr"] == 1


def test_serial_mismatch_fails_that_doc_only(corpus, seeded):
    seed(seeded, 1)
    seed(seeded, 2, "2020헌마2")
    srv = Server(bodies={"1": detc_xml(99), "2": detc_xml(2, "2020헌마2")})
    code, out = run_cli("lawapi_detc_body", ["--serial", "1", "--serial", "2"], corpus, srv.transport())
    assert code == 0 and out["result"]["serial_mismatch"] == 1 and out["result"]["extracted"] == 1
    m = Manifest(corpus / "manifest.sqlite")
    try:
        f = [x for x in m.failures() if x["target"] == "lawgo_detc:1"][0]
    finally:
        m.close()
    assert f["stage"] == "body" and f["error_class"] == "serial_mismatch" and doc(corpus, "lawgo_detc:1")[0]["status"] == "failed"
    assert doc(corpus, "lawgo_detc:1")[1].get("body") is None


def test_three_consecutive_body_failures_stop(corpus, seeded):
    for i in range(1, 5):
        seed(seeded, i, f"2020헌마{i}", f"2020-01-0{i}")
    srv = Server(bodies={str(i): (200, "<html>login</html>", XML_H) for i in range(1, 5)})
    code, out = run_cli("lawapi_detc_body", [], corpus, srv.transport())
    assert code == 1 and "consecutive" in out["error"] and srv.service_calls == ["1", "2", "3"]
    assert [doc(corpus, f"lawgo_detc:{i}")[0]["status"] for i in range(1, 5)] == ["failed", "failed", "failed", "discovered"]


# ------------------------------------------------------------------------------------------------ 권리 게이트·등록·OC
@pytest.mark.parametrize("adapter,argv", [("lawapi_detc", ["--number", "2020헌마1"]), ("lawapi_detc_body", ["--serial", "1"])])
def test_rights_gate_stops_before_any_request_and_scoped_record_opens_it(corpus, real_default_rights, adapter, argv):
    m = Manifest(corpus / "manifest.sqlite")
    m.ensure_source(L.SOURCE_ID, L.FAMILY, "판례", "https://www.law.go.kr/")
    srv = Server(lists={"2020헌마1": [list_item(1, "2020헌마1")]}, bodies={"1": detc_xml(1)})
    tr = srv.transport()
    code, out = run_cli(adapter, argv, corpus, tr)
    assert code == 1 and out["status"] == "failed" and out["error_class"] == "rights_not_recorded"
    assert tr.calls == [] and m.get_source(D.SOURCE_ID) is not None                    # 등록은 됐고 요청은 한 건도 없다(robots 포함)
    R.record_installer_decision(m, "law_go_kr", {"read": "allowed", "store_raw": "allowed"}, "시험", "t", source_ids=[D.SOURCE_ID])
    assert m.get_rights("source", L.SOURCE_ID)["read"] == "unknown"                     # 판례 행은 그대로
    seed(m, 1)
    m.close()
    code, out = run_cli(adapter, argv, corpus, srv.transport())
    assert code == 0 and out["status"] == "completed", out


def test_case_id_matches_admission_prefix_and_registration_constants():
    assert CASE_ID_PREFIX[D.SOURCE_ID] == "lawgo_detc:" and D.FAMILY == "law_go_kr" == L.FAMILY
    cid = f"lawgo_detc:123:{compact('2005 헌마165')}"            # structure_records: f"{target_key(=doc_id)}:{compact(case_number)}"
    assert cid == "lawgo_detc:123:2005헌마165" and cid.startswith(CASE_ID_PREFIX[D.SOURCE_ID])


def test_disk_guard_registration_and_adapter_discovery():
    assert cli.DISK_GUARDED["lawapi_detc"][0] == "case_json" and cli.DISK_GUARDED["lawapi_detc_body"][0] == "case_json"
    assert {"lawapi_detc", "lawapi_detc_body"} <= set(cli.available_sources())


def test_oc_value_is_absent_from_every_stored_artifact(corpus):
    leak = f"https://x/lawService.do?OC={FAKE_OC}&ID=1"
    srv = Server(lists={"2020헌마1": [list_item(1, "2020헌마1")]}, search={"2020헌마2": [list_item(1, "2020헌마1")]},
                 bodies={"1": detc_xml(1, 판시사항="가 " + leak, 참조판례=leak)})
    assert run_cli("lawapi_detc", ["--number", "2020헌마1", "--number", "2020헌마2", "--merged-search"], corpus, srv.transport())[0] == 0
    assert run_cli("lawapi_detc_body", ["--serial", "1"], corpus, srv.transport())[0] == 0
    files = [p for p in corpus.rglob("*") if p.is_file()]
    assert any(p.name == "manifest.sqlite" for p in files) and any("http_cache" in str(p) for p in files) and any(p.suffix == ".xml" for p in files)
    hits = [str(p.relative_to(corpus)) for p in files if FAKE_OC.encode() in p.read_bytes()]
    assert hits == []

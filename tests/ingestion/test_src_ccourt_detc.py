"""헌법재판소 판례정보 API 어댑터(ccourt_detc, 공공데이터포털): 네트워크 없는 가짜 transport·가짜 키.

모든 fixture 는 합성이다(가짜 번호·'갑'·'A법 제1조'). 실제 결정문의 문장·성명은 넣지 않는다. 키는 URL 인코딩된 Encoding 키 형태의 가짜 값이다."""
import hashlib
import io
import json
import sqlite3
import types
import urllib.parse as up

import pytest

from conftest import FakeClock, FakeTransport
from press_review import corpus_sqlite as CS
from press_review.ingestion import http as H
from press_review.ingestion import __main__ as cli, rights as R, structure as S, structure_records as SR
from press_review.ingestion.admission import CASE_ADMISSION_SOURCES, CASE_ID_PREFIX
from press_review.ingestion.http import ALLOWED_HOSTS, BlockedURL, HttpClient
from press_review.ingestion.manifest import Manifest
from press_review.ingestion.sources import ccourt_detc as C
from press_review.ingestion.sources.detc_parse import classify_orders

KEY_ENC = "FAKEKEY%2Bab%3D%3Dxyz9f3a"            # 저장된 Encoding 키 형태(이미 인코딩됨)
KEY_RAW = "FAKEKEY+ab==xyz9f3a"
ROBOTS = {"https://apis.data.go.kr/robots.txt": (200, "User-agent: *\nAllow: /\n", {})}
JSON_H = {"Content-Type": "application/json;charset=UTF-8"}
ORDER = "1. A법 제1조는 헌법에 위반되지 아니한다.\n2. 나머지 청구를 각하한다."
AUTH_ERR = json.dumps({"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "returnReasonCode": "30"}}})


def envelope(code="0", msg="NORMAL SERVICE", body=None):
    return json.dumps({"header": {"resultCode": code, "resultMsg": msg}, **({"body": body} if body is not None else {})}, ensure_ascii=False)


def list_json(items, total=None):
    n = len(items) if total is None else total
    return envelope(body={"items": ({"item": items} if items else None), "numOfRows": 50, "pageNo": 1, "totalCount": n, "type": "json"})


def list_item(num, no, label="판례집", date="20200102"):
    return {"eventNum": str(num), "eventNo": no, "eventNm": "A법 제1조 위헌확인", "panreType": label, "eventType": "헌마",
            "jgdmtCort": "전원재판부", "rstaRsta": "각하", "rstaDate": date}


def detail_item(num, no="2020헌마1", **over):
    it = {"eventNum": str(num), "eventNo": no, "eventNm": "A법 제1조 위헌확인", "panreType": "결정문", "panreTitle": "", "panreInfo": None,
          "textOfDecision": ORDER, "eventSummary": "사건개요 합성 문장이다.", "abjObt": "[심판대상조항]\nA법 제1조", "relatedProvision": None,
          "caseRelatedOpinion": "청구인 갑은 주장한다.", "judgement": "다수 판단 합성 문장이다.", "conclusion": "결론 합성 문장이다.",
          "otherOpinions": "", "enclsr": None, "pansiMatt": "", "decisionGst": "", "adjobtTxt": "", "refrnPrvsn": "", "refrnPrcdnt": "",
          "event": f"사 건 {no} A법 제1조 위헌확인\n청구인 갑\n대리인 변호사 을", "reason": "합성 이유 전체", "rstaDate": "20200102", "jgdmtCort": "전원재판부",
          "rstaRsta": "각하", "eventType": "헌마"}
    it.update(over)
    return it


def detail_json(num, **over):
    return envelope(body={"items": {"item": detail_item(num, **over)}, "totalCount": 1})


EMPTY_DETAIL = envelope(body={"items": None, "totalCount": 0})


class Server:
    """getKorPrcdntList·getKorPrcdntDetail 가짜 서버. lists: {번호: [item…] | (status, body)}, details: {(eventNum, 코드): dict(over) | (status, body)}."""

    def __init__(self, lists=None, details=None):
        self.lists, self.details = dict(lists or {}), dict(details or {})
        self.list_calls, self.detail_calls, self.keys, self.urls = [], [], [], []

    def __call__(self, url, n):
        self.urls.append(url)
        parts = up.urlsplit(url)
        q = up.parse_qs(parts.query)
        self.keys.append(q.get("serviceKey", [None])[0])
        if parts.path.endswith("/getKorPrcdntList"):
            no = q["eventNo"][0]
            self.list_calls.append(no)
            v = self.lists.get(no, [])
            if isinstance(v, tuple):
                return v[0], v[1], JSON_H
            return 200, list_json(v), JSON_H
        if parts.path.endswith("/getKorPrcdntDetail"):
            key = (q["eventNum"][0], q["panreType"][0])
            self.detail_calls.append(key)
            v = self.details.get(key)
            if v is None:
                return 200, EMPTY_DETAIL, JSON_H
            if isinstance(v, tuple):
                return v[0], v[1], JSON_H
            return 200, detail_json(key[0], **v), JSON_H
        return 404, b"", {}

    def transport(self):
        return FakeTransport(ROBOTS, default=self)


def run_cli(argv, corpus, transport, clock=None, log_stream=None):
    clock = clock or FakeClock()
    out = io.StringIO()
    code = cli.main(["ccourt_detc", *argv], transport=transport, clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus,
                    log_stream=log_stream or io.StringIO(), oc_provider=lambda: "UNUSED_OC", key_provider=lambda: KEY_ENC,
                    resolver=lambda h: ["93.184.216.34"])
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def doc(corpus, doc_id):
    m = Manifest(corpus / "manifest.sqlite")
    try:
        d = m.get_document(doc_id)
        return d, (json.loads(d["extra_json"]) if d and d["extra_json"] else {}), m.pages_of(doc_id=doc_id)
    finally:
        m.close()


def qual(p):
    q = p["quality_json"]
    return json.loads(q) if isinstance(q, str) else q


def failures(corpus, stage=None):
    m = Manifest(corpus / "manifest.sqlite")
    try:
        return {f["target"]: f for f in m.failures() if stage is None or f["stage"] == stage}
    finally:
        m.close()


def ok_server(num=11, no="2020헌마1", code="01", **over):
    return Server(lists={no: [list_item(num, no)]}, details={(str(num), code): over})


# ------------------------------------------------------------------------------------------------ 목록·상세 호출
def test_list_exact_match_only_and_zero_hits(corpus):
    srv = Server(lists={"2020헌마1": [list_item(12, "2020헌마10"), list_item(13, "2020헌마11")], "2020헌마2": []})
    code, out = run_cli(["--number", "2020헌마1", "--number", "2020헌마2"], corpus, srv.transport())
    assert code == 0 and out["status"] == "completed", out
    assert srv.detail_calls == [] and out["result"]["not_found"] == 2 and out["result"]["docs_new"] == 0
    assert {f["error_class"] for f in failures(corpus).values()} == {"not_found"}
    assert all("eventNo=2020%ED%97%8C%EB%A7%881" in u or "eventNo=2020%ED%97%8C%EB%A7%882" in u for u in srv.urls if "getKorPrcdntList" in u)


def test_detail_uses_code_not_label_and_prefers_01(corpus):
    srv = ok_server(11, "2020헌마1", "01")
    code, out = run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    assert out["result"]["docs_new"] == 1 and srv.detail_calls == [("11", "01")]
    assert not any("panreType=%ED" in u for u in srv.urls)          # 목록의 라벨('판례집')을 상세 호출에 넘기지 않는다
    assert doc(corpus, "ccourt_detc:11")[1]["body"]["panre_type"] == "01" and C.panre_code("판례집") == "03"


def test_falls_back_to_03_when_01_has_no_item(corpus):
    srv = ok_server(11, "2020헌마1", "03", pansiMatt="가 쟁점", decisionGst="나 요지", refrnPrcdnt="다 참조", refrnPrvsn="A법 제1조")
    code, out = run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    assert srv.detail_calls == [("11", "01"), ("11", "03")] and out["result"]["docs_new"] == 1
    d, extra, pages = doc(corpus, "ccourt_detc:11")
    assert extra["body"]["panre_type"] == "03" and {qual(p)["section_kind"] for p in pages} >= {"headnote_issue", "headnote_holding", "ref_statutes", "ref_cases"}


def test_both_codes_empty_records_no_body_without_document(corpus):
    srv = Server(lists={"2020헌마1": [list_item(11, "2020헌마1")]})
    code, out = run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    assert srv.detail_calls == [("11", "01"), ("11", "03")] and doc(corpus, "ccourt_detc:11")[0] is None
    assert [f["error_class"] for f in failures(corpus).values()] == ["no_body"]


# ------------------------------------------------------------------------------------------------ 필드 → 쪽
def test_fields_map_to_pages_and_opinion_kinds(corpus):
    op = "재판관 갑의 반대의견\n반대 이유 합성이다.\n재판관 을의 보충의견\n보충 이유 합성이다."
    srv = ok_server(11, "2020헌마1", "01", otherOpinions=op)
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "ccourt_detc:11")
    by = {qual(p)["label"]: qual(p) for p in pages}
    assert d["status"] == "extracted" and by["사건표지"]["locator"] == "h000"
    assert by["주문"]["section_kind"] == "order" and by["주문"]["opinion_kind"] is None
    for lab in ("사건개요", "심판대상", "판단", "결론"):
        assert by[lab]["section_kind"] == "reasons" and by[lab]["opinion_kind"] == "majority"           # 기타의견이 있으면 법정의견 구간
    assert by["사건관계인의 의견"]["opinion_kind"] == "party_submission"
    ops = [qual(p) for p in pages if qual(p)["section_kind"] == "opinion"]
    assert len(ops) == 2 and all(o["opinion_kind"] not in ("majority", "main") for o in ops)
    assert not any(qual(p)["opinion_kind"] in ("majority", "main") and qual(p)["field"] == "기타의견" for p in pages)
    assert SR.detc_majority_barred({"quality_json": json.dumps(by["사건관계인의 의견"])}, {}) == "opinion_kind_party_submission"
    assert extra["body"]["opinion_status"] == "split"


def test_no_other_opinions_means_main_and_unsplit_other_opinions_are_flagged(corpus):
    srv = ok_server(11, "2020헌마1", "01")
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "ccourt_detc:11")
    assert {qual(p)["opinion_kind"] for p in pages if qual(p)["label"] == "판단"} == {"main"} and extra["body"]["opinion_status"] == "none"
    srv2 = ok_server(21, "2020헌마2", "01", otherOpinions="표제 없는 기타의견 합성 문장이다.")
    run_cli(["--number", "2020헌마2"], corpus, srv2.transport())
    d, extra, pages = doc(corpus, "ccourt_detc:21")
    ops = [p for p in pages if qual(p)["section_kind"] == "opinion"]
    assert len(ops) == 1 and qual(ops[0])["opinion_kind"] is None and ops[0]["needs_review"]
    assert extra["body"]["opinion_status"] == "unsplit"
    assert SR.detc_majority_barred(ops[0], {}) == "needs_review"


def test_order_classified_and_party_info_never_becomes_a_page(corpus):
    ev = "사 건 2020헌마1 A법 제1조 위헌확인\n2020헌마7 병합\n청구인 갑\n대리인 변호사 을"
    srv = ok_server(11, "2020헌마1", "01", event=ev)
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "ccourt_detc:11")
    body = extra["body"]
    assert body["dispositions"] == classify_orders(ORDER) and "각하" in body["dispositions"]
    assert body["case_nos"][0] == "2020헌마1" and "2020헌마7" in body["case_nos"] and body["body_case_no"] == "2020헌마1"
    texts = "".join((p["text_path"] and (corpus / p["text_path"]).read_text(encoding="utf-8")) for p in pages)
    assert "대리인" not in texts and "병합" not in texts and "변호사" not in texts and "A법 제1조 위헌확인" not in texts
    assert S.ledger_values(Manifest(corpus / "manifest.sqlite"), "ccourt_detc:11", detc=True)["case_id_number"] == "2020헌마1"


def test_reason_field_is_not_a_page_when_split_fields_exist(corpus):
    srv = ok_server(11, "2020헌마1", "01", reason="전체 이유 합성 문장 RSN")
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "ccourt_detc:11")
    assert not any("RSN" in (corpus / p["text_path"]).read_text(encoding="utf-8") for p in pages)


def test_unseparated_reason_is_kept_only_as_flagged_unsplit_page(corpus):
    srv = ok_server(11, "2020헌마1", "01", judgement="", conclusion="", eventSummary="", abjObt="", caseRelatedOpinion="", reason="이유 전체 합성 문장 RSN")
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "ccourt_detc:11")
    rs = [p for p in pages if "RSN" in (corpus / p["text_path"]).read_text(encoding="utf-8")]
    assert len(rs) == 1 and rs[0]["needs_review"] and qual(rs[0])["opinion_kind"] is None


# ------------------------------------------------------------------------------------------------ 중복 색인 방지
def seed_lawgo(corpus, number, *, case_row=False, text=True, serial="900"):
    m = Manifest(corpus / "manifest.sqlite")
    m.ensure_source("lawgo_detc", "law_go_kr", "헌재결정례", "https://www.law.go.kr/")
    m.upsert_document(f"lawgo_detc:{serial}", "lawgo_detc", title="합성", decision_date="2020-01-02",
                      extra={"serial": serial, "case_no": number, "body": {"case_nos": [number], "text_available": text}})
    if case_row:
        cid = f"lawgo_detc:{serial}:{number}"
        m.upsert_case(cid, f"lawgo_detc:{serial}", {"case_id": cid, "doc_id": f"lawgo_detc:{serial}", "institution": "constitutional_court",
                                                   "case_number": number, "authority_type": "judgment", "outcome_type": "judgment",
                                                   "issue_tags": [], "legal_reasoning": []}, quote_total=1, quote_ok=1)
    m.close()


@pytest.mark.parametrize("kw", [{"case_row": True, "text": False}, {"case_row": False, "text": True}])
def test_number_already_held_by_lawgo_detc_is_skipped_before_any_request(corpus, kw):
    seed_lawgo(corpus, "2020헌마1", **kw)
    srv = ok_server(11, "2020헌마1", "01")
    tr = srv.transport()
    code, out = run_cli(["--number", "2020헌마1"], corpus, tr)
    assert out["result"]["skipped_covered"] == 1 and tr.calls == [] and doc(corpus, "ccourt_detc:11")[0] is None


def test_no_body_lawgo_document_does_not_block_enrichment(corpus):
    seed_lawgo(corpus, "2020헌마1", case_row=False, text=False)
    code, out = run_cli(["--number", "2020헌마1"], corpus, ok_server(11, "2020헌마1", "01").transport())
    assert out["result"]["docs_new"] == 1


def test_merged_number_held_by_lawgo_detc_blocks_registration_after_detail(corpus):
    seed_lawgo(corpus, "2020헌마7", case_row=True)
    ev = "사 건 2020헌마1 A법 제1조 위헌확인\n2020헌마7 병합\n청구인 갑"
    srv = ok_server(11, "2020헌마1", "01", event=ev)
    code, out = run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    assert out["result"]["already_covered"] == 1 and doc(corpus, "ccourt_detc:11")[0] is None
    assert [f["error_class"] for f in failures(corpus).values()] == ["already_covered"]


# ------------------------------------------------------------------------------------------------ 이어받기
def test_resume_skips_done_numbers_and_rebuilds_from_raw_without_detail_request(corpus):
    srv = ok_server(11, "2020헌마1", "01")
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    srv2 = Server()
    tr2 = srv2.transport()
    code, out = run_cli(["--number", "2020헌마1"], corpus, tr2)
    assert out["result"]["skipped_done"] == 1 and tr2.calls == []
    m = Manifest(corpus / "manifest.sqlite")                                      # 추출이 중간에 끊긴 상태를 만든다
    m.delete_derived(doc_id="ccourt_detc:11")
    m.set_document_status("ccourt_detc:11", "fetched")
    m.update_document_fields("ccourt_detc:11", extra_merge={"resolved_numbers": []})
    m.close()
    srv3 = Server(lists={"2020헌마1": [list_item(11, "2020헌마1")]})
    code, out = run_cli(["--number", "2020헌마1"], corpus, srv3.transport())
    assert srv3.detail_calls == [] and out["result"]["resumed_from_raw"] == 1
    d, extra, pages = doc(corpus, "ccourt_detc:11")
    assert d["status"] == "extracted" and len(pages) >= 6 and "2020헌마1" in extra["resolved_numbers"]


# ------------------------------------------------------------------------------------------------ 실패 처리
def test_consecutive_failures_stop_after_three(corpus):
    bad = (200, "<html>login</html>")
    srv = Server(lists={f"2020헌마{n}": bad for n in range(1, 6)})
    code, out = run_cli([a for n in range(1, 6) for a in ("--number", f"2020헌마{n}")], corpus, srv.transport())
    assert code == 1 and out["status"] == "failed" and "consecutive" in out["error"]
    assert srv.list_calls == ["2020헌마1", "2020헌마2", "2020헌마3"]


def test_nonzero_result_code_is_a_failure_and_not_cached(corpus):
    srv = Server(lists={"2020헌마1": (200, envelope("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"))})
    code, out = run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    assert code == 0 and [f["error_class"] for f in failures(corpus).values()] == ["api_22"]
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    assert srv.list_calls == ["2020헌마1", "2020헌마1"]                           # 오류 응답은 캐시되지 않아 다시 요청한다


@pytest.mark.parametrize("status", [403, 200])
def test_auth_error_stops_everything_at_once(corpus, status):
    srv = Server(lists={f"2020헌마{n}": (status, AUTH_ERR) for n in range(1, 4)})
    code, out = run_cli([a for n in range(1, 4) for a in ("--number", f"2020헌마{n}")], corpus, srv.transport())
    assert code == 1 and out["error_class"] == "auth_failed" and srv.list_calls == ["2020헌마1"]
    assert "serviceKey" not in json.dumps(out) and "http" not in out["error"]


# ------------------------------------------------------------------------------------------------ 키·권리·간격
def corpus_bytes(corpus):
    return b"\n".join(p.read_bytes() for p in corpus.rglob("*") if p.is_file())


def test_key_is_sent_once_encoded_and_never_stored_or_logged(corpus):
    log = io.StringIO()
    srv = Server(lists={"2020헌마1": [list_item(11, "2020헌마1")], "2020헌마2": (403, AUTH_ERR)}, details={("11", "01"): {}})
    tr = srv.transport()
    code, out = run_cli(["--number", "2020헌마1", "--number", "2020헌마2"], corpus, tr, log_stream=log)
    data_urls = [u for u in tr.urls("apis.data.go.kr") if "robots.txt" not in u]
    assert data_urls and all(u.endswith("&serviceKey=" + KEY_ENC) and "%25" not in u for u in data_urls)       # 다시 인코딩하지 않는다
    assert set(srv.keys) == {KEY_RAW}
    blob = corpus_bytes(corpus) + log.getvalue().encode() + json.dumps(out).encode()
    for k in (KEY_ENC, KEY_RAW, "xyz9f3a"):
        assert k.encode() not in blob
    m = Manifest(corpus / "manifest.sqlite")
    assert all("xyz9f3a" not in json.dumps(dict(r)) for r in m._q("SELECT * FROM failures"))
    m.close()
    assert b"serviceKey=[MASKED]" in corpus_bytes(corpus)                              # 캐시 키·메타에는 마스킹된 표기만


def test_response_echoing_the_key_is_dropped_not_cached(corpus):
    echo = envelope(body={"items": None, "totalCount": 0, "echo": KEY_RAW})
    srv = Server(lists={"2020헌마1": (200, echo)})
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    assert b"xyz9f3a" not in corpus_bytes(corpus) and [f["error_class"] for f in failures(corpus, "resolve").values()] == ["http_content_rejected"]


def test_key_only_goes_to_the_data_go_kr_path_and_must_not_be_written_in_urls(corpus, make_client):
    c = make_client(FakeTransport({}), key_provider=lambda: KEY_ENC, robots=None, resolver=lambda h: ["93.184.216.34"])
    with pytest.raises(BlockedURL):
        c.get("https://apis.data.go.kr/9750000/x?serviceKey=abc")
    with pytest.raises(BlockedURL):
        c.get("https://www.law.go.kr/DRF/lawSearch.do?x=1", with_key=True)
    with pytest.raises(BlockedURL):
        c.get("https://apis.data.go.kr/other/x", with_key=True)
    assert "apis.data.go.kr" in ALLOWED_HOSTS


def test_rights_gate_stops_before_any_request_and_scoped_record_opens_it(corpus, real_default_rights):
    srv = ok_server(11, "2020헌마1", "01")
    tr = srv.transport()
    code, out = run_cli(["--number", "2020헌마1"], corpus, tr)
    assert code == 1 and out["status"] == "failed" and out["error_class"] == "rights_not_recorded" and tr.calls == []
    m = Manifest(corpus / "manifest.sqlite")
    assert m.get_source(C.SOURCE_ID)["source_family"] == "data_go_kr"
    R.record_installer_decision(m, "data_go_kr", {"read": "allowed", "store_raw": "allowed", "create_derivatives": "allowed"}, "시험", "t", source_ids=[C.SOURCE_ID])
    m.close()
    code, out = run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    assert code == 0 and out["status"] == "completed", out


def test_requests_are_spaced_at_least_three_seconds(corpus):
    clock = FakeClock()
    srv = Server(lists={f"2020헌마{n}": [list_item(n, f"2020헌마{n}")] for n in (1, 2)}, details={(str(n), "01"): {} for n in (1, 2)})
    tr = srv.transport()
    run_cli([a for n in (1, 2) for a in ("--number", f"2020헌마{n}")], corpus, tr, clock=clock)
    assert len(tr.urls("apis.data.go.kr")) >= 5 and clock.sleeps and min(clock.sleeps) >= 3.0


def test_dry_run_makes_no_request_and_reports_the_miss(corpus):
    srv = ok_server(11, "2020헌마1", "01")
    tr = srv.transport()
    code, out = run_cli(["--number", "2020헌마1", "--dry-run"], corpus, tr)
    assert tr.calls == [] and "serviceKey=[MASKED]" in json.dumps(out) and KEY_ENC not in json.dumps(out)


# ------------------------------------------------------------------------------------------------ 연결 상수
def test_registration_constants_and_attribution():
    assert C.SOURCE_ID == "ccourt_detc" and C.FAMILY == "data_go_kr" and C.DOC_PREFIX == "ccourt_detc"
    assert C.FAMILY in R.COLLECTED_FAMILIES and R.FAMILY_HOST[C.FAMILY] == "apis.data.go.kr" and C.FAMILY in S.EXTERNAL_API_FAMILIES
    assert SR.SOURCE_PROFILE[C.SOURCE_ID] == "constitutional_decision" and C.SOURCE_ID in CASE_ADMISSION_SOURCES
    assert CASE_ID_PREFIX[C.SOURCE_ID] == "ccourt_detc:"
    assert CS.SOURCE_ATTRIBUTION["ccourt_detc"] == "헌법재판소 결정, 헌법재판소 제공"
    assert CS.attribution_for("ccourt_detc:52393") == "헌법재판소 결정, 헌법재판소 제공"
    assert CS.attribution_for("lawgo_detc:1") == "헌법재판소 결정, 법제처 국가법령정보센터 제공"


def test_structure_candidate_requires_body_text():
    mk = lambda t: {"source_id": "ccourt_detc", "extra_json": json.dumps({"body": {"text_available": t}})}
    assert S._structure_candidate_doc(mk(True)) and not S._structure_candidate_doc(mk(False))


def test_disk_guard_registration_and_adapter_discovery():
    assert cli.DISK_GUARDED["ccourt_detc"][0] == "case_json" and "ccourt_detc" in cli.available_sources()


def test_public_url_is_the_law_go_kr_permalink_not_the_keyed_api():
    url = C.public_url("2017헌바456")
    assert url.isascii() and "apis.data.go.kr" not in url and "serviceKey" not in url
    assert up.unquote(url) == "https://www.law.go.kr/헌재결정례/(2017헌바456)"


def test_html_entities_are_decoded_but_a_bare_ampersand_is_kept():
    assert C._field({"a": "명예&#183;신용 R&D &amp; A&B"}, "a") == "명예·신용 R&D & A&B"


JUDGEMENT_WITH_OPINIONS = ("공통 판단 합성 문장이다.\n(가) 재판관 갑, 재판관 을의 기각의견\n기각 이유 합성이다.\n"
                           "(나) 재판관 병, 재판관 정의 반대의견\n반대 이유 합성이다.")


def test_opinions_inside_judgement_are_split_and_not_left_as_court_reasons(corpus):
    srv = ok_server(11, "2020헌마1", "01", judgement=JUDGEMENT_WITH_OPINIONS)
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    d, extra, pages = doc(corpus, "ccourt_detc:11")
    q = [qual(p) for p in pages]
    main = [x for x in q if x["label"] == "판단"]
    ops = [x for x in q if x["section_kind"] == "opinion"]
    assert len(main) == 1 and main[0]["opinion_kind"] == "majority" and main[0]["char_end"] < len(JUDGEMENT_WITH_OPINIONS)
    assert [(o["field"], o["opinion_kind"], o["vote"]) for o in ops] == [("판단", "vote_opinion", "기각"), ("판단", "dissent", None)]
    assert extra["body"]["opinion_status"] == "split"
    assert SR.detc_majority_barred(next(p for p in pages if qual(p)["opinion_kind"] == "dissent"), {})


def test_opinions_in_judgement_and_other_opinions_get_distinct_pages(corpus):
    srv = ok_server(11, "2020헌마1", "01", judgement=JUDGEMENT_WITH_OPINIONS, otherOpinions="재판관 무의 보충의견\n보충 이유 합성이다.")
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    _, _, pages = doc(corpus, "ccourt_detc:11")
    locs = [qual(p)["locator"] for p in pages]
    assert len(locs) == len(set(locs)) and len([p for p in pages if qual(p)["section_kind"] == "opinion"]) == 3


def test_unsplit_opinion_mention_in_judgement_is_not_court_reasoning(corpus):
    srv = ok_server(11, "2020헌마1", "01", judgement="재판관 갑의 반대의견이 있다. 표제 없이 이어지는 합성 문장이다.")
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    _, extra, pages = doc(corpus, "ccourt_detc:11")
    p = next(x for x in pages if qual(x)["label"] == "판단")
    assert qual(p)["opinion_kind"] is None and p["needs_review"] and "judgement_opinions_unsplit" in extra["body"]["warnings"]


def test_reparse_rebuilds_pages_and_link_from_raw_without_requests(corpus):
    srv = ok_server(11, "2020헌마1", "01", judgement=JUDGEMENT_WITH_OPINIONS)
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    d, extra, before = doc(corpus, "ccourt_detc:11")
    assert d["canonical_url"] == C.public_url("2020헌마1")
    m = Manifest(corpus / "manifest.sqlite")
    m.update_document_fields("ccourt_detc:11", canonical_url="https://apis.data.go.kr/9750000/old")
    m.close()
    empty = Server()
    code, res = run_cli(["--reparse"], corpus, empty.transport())
    d2, _, after = doc(corpus, "ccourt_detc:11")
    assert code == 0 and empty.urls == [] and d2["canonical_url"] == C.public_url("2020헌마1")
    assert d2["status"] == "extracted" and sorted(p["page_id"] for p in after) == sorted(p["page_id"] for p in before)
    (corpus / extra["body"]["raw_path"]).write_bytes(b"tampered")
    code, _ = run_cli(["--reparse"], corpus, Server().transport())
    assert code == 0 and doc(corpus, "ccourt_detc:11")[0]["status"] == "extracted"


def test_key_echo_written_as_a_json_escape_is_caught(corpus):
    body = list_json([list_item(11, "2020헌마1")]).replace("A법 제1조 위헌확인", KEY_RAW.replace("+", "\\u002b"))
    srv = Server(lists={"2020헌마1": (200, body)})
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    assert doc(corpus, "ccourt_detc:11")[0] is None
    assert not [p for p in corpus.rglob("*") if p.is_file() and b"u002bab" in p.read_bytes()]


class HeaderEchoServer(Server):
    def __call__(self, url, n):
        status, body, headers = super().__call__(url, n)
        return status, body, {**headers, "ETag": KEY_ENC}


def test_key_echo_in_a_response_header_is_not_stored(corpus):
    srv = HeaderEchoServer(lists={"2020헌마1": [list_item(11, "2020헌마1")]}, details={("11", "01"): {}})
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    assert doc(corpus, "ccourt_detc:11")[0]["status"] == "extracted"
    assert not [p for p in corpus.rglob("*") if p.is_file() and KEY_ENC.encode() in p.read_bytes()]


def test_reparse_keeps_the_decision_chosen_at_fetch_time(corpus):
    both = envelope(body={"items": {"item": [detail_item(99, no="2020헌마9"), detail_item(11, no="2020헌마1")]}, "totalCount": 2})
    srv = Server(lists={"2020헌마1": [list_item(11, "2020헌마1")]}, details={("11", "01"): (200, both)})
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    run_cli(["--reparse"], corpus, Server().transport())
    _, extra, _ = doc(corpus, "ccourt_detc:11")
    assert extra["body"]["body_case_no"] == "2020헌마1"


def test_reparse_skips_a_raw_without_a_unique_matching_item(corpus):
    srv = ok_server(11, "2020헌마1", "01")
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    d, extra, _ = doc(corpus, "ccourt_detc:11")
    raw = detail_json(99, no="2020헌마9").encode("utf-8")
    (corpus / extra["body"]["raw_path"]).write_bytes(raw)
    m = Manifest(corpus / "manifest.sqlite")
    m.update_document_fields("ccourt_detc:11", extra_merge={"body": {**extra["body"], "raw_sha256": hashlib.sha256(raw).hexdigest()}})
    m.close()
    run_cli(["--reparse"], corpus, Server().transport())
    assert doc(corpus, "ccourt_detc:11")[1]["body"]["body_case_no"] == "2020헌마1"


def test_partly_failed_number_is_retried_until_every_decision_is_in(corpus):
    items = [list_item(11, "2020헌마1"), list_item(12, "2020헌마1")]
    run_cli(["--number", "2020헌마1"], corpus, Server(lists={"2020헌마1": items}, details={("11", "01"): {}, ("12", "01"): (500, "일시 오류")}).transport())
    assert doc(corpus, "ccourt_detc:11")[0] is not None and doc(corpus, "ccourt_detc:12")[0] is None
    run_cli(["--number", "2020헌마1"], corpus, Server(lists={"2020헌마1": items}, details={("11", "01"): {}, ("12", "01"): {}}).transport())
    assert doc(corpus, "ccourt_detc:12")[0]["status"] == "extracted" and not failures(corpus, "resolve")


def test_multi_decision_number_left_unfinished_by_an_interruption_is_not_treated_as_done(corpus):
    items = [list_item(11, "2020헌마1"), list_item(12, "2020헌마1")]
    srv = Server(lists={"2020헌마1": items}, details={("11", "01"): {}, ("12", "01"): {}})
    real = C._Run.event
    calls = []

    def stop_second(self, number, it, multi):
        calls.append(it["eventNum"])
        if len(calls) == 2:
            raise SystemExit(9)
        return real(self, number, it, multi)

    C._Run.event = stop_second
    try:
        with pytest.raises(SystemExit):
            run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    finally:
        C._Run.event = real
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    assert doc(corpus, "ccourt_detc:12")[0]["status"] == "extracted"


def test_refresh_reprocesses_a_number_that_is_already_done(corpus):
    srv = ok_server(11, "2020헌마1", "01")
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    before = len(srv.detail_calls)
    run_cli(["--number", "2020헌마1", "--refresh"], corpus, srv.transport())
    assert len(srv.detail_calls) > before


def _passage(family, pid, number, created="2026-01-01"):
    return {"source_family": family, "source_id": f"{family}:{pid}", "passage_id": pid, "case_number": number, "_created": created}


def _indexed(monkeypatch, rows, key=lambda p: p["passage_id"]):
    monkeypatch.setattr(CS, "_iter_eligible", lambda *a, **k: iter([dict(r) for r in rows]))
    monkeypatch.setattr(CS, "_dedup_key", key)
    excluded = {}
    return [p["passage_id"] for p in CS._iter_passages(None, {}, {}, excluded, None)], excluded


def test_ccourt_passage_is_dropped_when_lawgo_really_indexes_the_same_number(monkeypatch):
    rows = [_passage("ccourt_detc", "c1", "2003헌가6"), _passage("ccourt_detc", "c2", "2003헌가7"), _passage("lawgo_detc", "l1", "2003헌가5, 2003헌가6")]
    out, excluded = _indexed(monkeypatch, rows)
    assert sorted(out) == ["c2", "l1"] and excluded == {"case_superseded_by_lawgo": 1}


def test_ccourt_passage_stays_when_the_lawgo_case_did_not_pass_eligibility(monkeypatch):
    out, excluded = _indexed(monkeypatch, [_passage("ccourt_detc", "c1", "2003헌가6")])      # 격리·보류된 법제처 사건은 적격 흐름에 없다
    assert out == ["c1"] and excluded == {}


def test_superseded_ccourt_passage_cannot_win_the_dedup_over_lawgo(monkeypatch):
    rows = [_passage("ccourt_detc", "c1", "2003헌가6", created="2020-01-01"), _passage("lawgo_detc", "l1", "2003헌가6", created="2026-01-01")]
    out, excluded = _indexed(monkeypatch, rows, key=lambda p: "same")
    assert out == ["l1"] and excluded == {"case_superseded_by_lawgo": 1}


def _page(kind, section):
    return {"quality_json": json.dumps({"opinion_kind": kind, "section_kind": section})}


def test_opinion_section_without_a_kind_cannot_carry_court_majority():
    assert SR.detc_majority_barred(_page(None, "opinion"), {}) == "opinion_kind_unclassified"
    assert SR.detc_majority_barred(_page(None, "headnote_opinion"), {}) == "opinion_kind_unclassified"
    assert SR.detc_majority_barred(_page("majority", "reasons"), {}) is None
    assert SR.detc_majority_barred(_page(None, "order"), {}) is None


@pytest.mark.parametrize("kind, section, kept", [("dissent", "opinion", 0), ("party_submission", "party_opinion", 0), (None, "opinion", 0), ("majority", "reasons", 1)])
def test_rerouted_article_quote_passes_the_opinion_guard(kind, section, kept):
    ctx = types.SimpleNamespace(profile=types.SimpleNamespace(detc_guards=True), ledger={}, page_meta={"p1": _page(kind, section)})
    it = {"text": "서술", "quote": {"page_id": "p1", "start": 0, "end": 8, "quote": "위 기사는 합성 문장이다"}}
    items = {"claims_or_requested_remedy": [], "factual_background": [], "article_claims": [it], "disputed_spans": []}
    SR._fix_article_speaker_items(items, SR._Stats(), "judgment", ctx)
    assert len(items["factual_background"]) == kept and items["article_claims"] == []


KEY_LOWER_HEX = "FAKEKEY%2bab%3d%3dxyz9f3a"


def test_key_echo_with_lowercase_percent_hex_is_caught_in_the_body(corpus):
    body = list_json([list_item(11, "2020헌마1")]).replace("A법 제1조 위헌확인", KEY_LOWER_HEX)
    run_cli(["--number", "2020헌마1"], corpus, Server(lists={"2020헌마1": (200, body)}).transport())
    assert doc(corpus, "ccourt_detc:11")[0] is None
    assert not [p for p in corpus.rglob("*") if p.is_file() and KEY_LOWER_HEX.encode() in p.read_bytes()]


class LowerHexEtagServer(Server):
    def __call__(self, url, n):
        status, body, headers = super().__call__(url, n)
        return status, body, {**headers, "ETag": KEY_LOWER_HEX}


def test_key_echo_with_lowercase_percent_hex_is_not_stored_from_a_header(corpus):
    srv = LowerHexEtagServer(lists={"2020헌마1": [list_item(11, "2020헌마1")]}, details={("11", "01"): {}})
    run_cli(["--number", "2020헌마1"], corpus, srv.transport())
    assert doc(corpus, "ccourt_detc:11")[0]["status"] == "extracted"
    assert not [p for p in corpus.rglob("*") if p.is_file() and KEY_LOWER_HEX.encode() in p.read_bytes()]


@pytest.mark.parametrize("path, ok", [
    ("/9750000/PrecedentInfomationService/getKorPrcdntList", True),
    ("/9750000/PrecedentInfomationService/getKorPrcdntDetail", True),
    ("/9750000/other", False),
    ("/9750000/../other", False),
    ("/9750000/PrecedentInfomationService/../../other", False),
    ("/9750000/PrecedentInfomationService/%2e%2e/%2e%2e/other", False),
    ("/9750000/PrecedentInfomationService/%252e%252e/other", False),
    ("/9750000/PrecedentInfomationService/./x", False),
    ("/9750000/PrecedentInfomationService/..\\..\\other", False),
])
def test_service_key_path_rejects_dot_segments_and_encoded_forms(path, ok):
    assert H._key_path_allowed(path) is ok


class DotRedirectServer(Server):
    def __call__(self, url, n):
        if "getKorPrcdntList" in url:
            self.urls.append(url)
            return 302, "", {"Location": "https://apis.data.go.kr/9750000/../other-service"}
        return super().__call__(url, n)


def test_redirect_through_dot_segments_never_carries_the_key(corpus):
    srv = DotRedirectServer(lists={"2020헌마1": [list_item(11, "2020헌마1")]})
    tr = srv.transport()
    code, out = run_cli(["--number", "2020헌마1"], corpus, tr)
    assert code != 0 or out["status"] != "completed" or out["result"]["failed"] >= 1
    assert not [u for u in srv.urls if "other-service" in u]


def test_rights_gate_refuses_when_derivatives_are_not_allowed(corpus, real_default_rights):
    run_cli(["--number", "2020헌마1"], corpus, ok_server(11, "2020헌마1", "01").transport())       # 소스 등록
    m = Manifest(corpus / "manifest.sqlite")
    R.record_installer_decision(m, "data_go_kr", {"read": "allowed", "store_raw": "allowed"}, "시험", "t", source_ids=[C.SOURCE_ID])
    m.close()
    tr = ok_server(11, "2020헌마1", "01").transport()
    code, out = run_cli(["--number", "2020헌마1"], corpus, tr)
    assert code == 1 and out["error_class"] == "rights_not_recorded" and tr.calls == [] and doc(corpus, "ccourt_detc:11")[0] is None


@pytest.mark.parametrize("state, case_row", [("quarantined", True), ("withheld", True), ("withheld", False), ("admission_excluded", True)])
def test_lawgo_material_that_cannot_be_indexed_does_not_block_enrichment(corpus, state, case_row):
    seed_lawgo(corpus, "2020헌마1", case_row=case_row)
    if state == "admission_excluded":
        m = Manifest(corpus / "manifest.sqlite")
        m.record_failure("lawgo_detc:900:2020헌마1", "admission", "case_excluded", "시험")
        m.close()
    else:
        conn = sqlite3.connect(corpus / "manifest.sqlite")
        conn.execute("UPDATE cases SET verification_status='quarantined'" if state == "quarantined" else "UPDATE documents SET status='withheld' WHERE source_id='lawgo_detc'")
        conn.commit()
        conn.close()
    _, out = run_cli(["--number", "2020헌마1"], corpus, ok_server(11, "2020헌마1", "01").transport())
    assert out["result"]["docs_new"] == 1

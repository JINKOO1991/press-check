"""법제처 판례 목록 어댑터(lawapi): 네트워크 없는 가짜 transport. 픽스처는 2026-10-08 실응답 형태에서 줄인 것."""
import json
import logging
import types
import urllib.parse as up

import pytest

from conftest import FAKE_OC, ROBOTS_OK, FakeTransport
from press_review.ingestion.context import Budget, RunContext
from press_review.ingestion.http import IngestionError
from press_review.ingestion.sources import lawapi

ROBOTS = {"https://www.law.go.kr/robots.txt": ROBOTS_OK}


def item(serial, case_no, date, name="신문보도정정보도명령", kind="판결"):
    return {"id": "1", "사건번호": case_no, "데이터출처명": "대법원", "사건종류코드": "400101", "사건종류명": "민사", "선고": "선고",
            "선고일자": date, "판례일련번호": str(serial), "판결유형": kind, "법원종류코드": "", "법원명": "대법원",
            "판례상세링크": f"/DRF/lawService.do?OC={FAKE_OC}&target=prec&ID={serial}&type=HTML&mobileYn=", "사건명": name}


def env(items, total=None, term_items=None):
    return json.dumps({"PrecSearch": {"키워드": "x", "page": "1", "target": "prec", "totalCnt": str(len(items) if total is None else total),
                                      "section": "bdyText", **({"prec": items} if items else {})}}, ensure_ascii=False)


DB = {"정정보도": [item(1, "85다카1973", "1986.01.28"), item(2, "80도306", "1980.05.20"), item(3, "80도306", "1980.05.20", kind="전원합의체 판결")],
      "기사": [item(3, "80도306", "1980.05.20"), item(4, "99다1", "0001.01.01"), item(5, "2000다2", "2026.12.11")]}


def handler(db=None):
    db = db or DB

    def h(url, n):
        q = up.parse_qs(up.urlsplit(url).query)
        if "query" not in q:
            return 200, env([], total=9), {}
        rows = db.get(q["query"][0], [])
        d, p = int(q["display"][0]), int(q["page"][0])
        return 200, env(rows[(p - 1) * d:p * d], total=len(rows)), {"Content-Type": "application/json;charset=UTF-8"}
    return h


@pytest.fixture()
def make_ctx(corpus, clock, manifest, make_client):
    def _make(transport, limit=None, **kw):
        client = make_client(transport)
        return RunContext(http=client, manifest=manifest, budget=Budget(kw.pop("max_seconds", None), clock), logger=logging.getLogger("t"),
                          corpus_dir=corpus, now=clock.now, limit_pages=limit)
    return _make


def args(**kw):
    return types.SimpleNamespace(**{"terms": None, "display": 2, "seeds_file": None, "no_probe": True, **kw})


def prefix_transport(h):
    return FakeTransport(ROBOTS, default=h)


def test_parse_variants_and_error_bodies():
    assert lawapi.parse_list(env([item(1, "a", "2000.01.01")]).encode())[0] == 1
    single = json.dumps({"PrecSearch": {"totalCnt": "1", "prec": item(1, "a", "2000.01.01")}}).encode()
    assert len(lawapi.parse_list(single)[1]) == 1                       # 1건이면 dict
    assert lawapi.parse_list(env([]).encode()) == (0, [], "bdyText")      # 0건이면 prec 없음
    for bad in (b"<html>error</html>", b'{"result":"fail"}', b'{"PrecSearch":{"totalCnt":"x"}}'):
        assert lawapi.accept_list(bad, {}) is not None


def test_date_classification():
    from datetime import date
    t = date(2026, 10, 8)
    assert lawapi.classify_date("1986.01.28", t) == ("1986-01-28", "ok")
    assert lawapi.classify_date("0001.01.01", t) == (None, "sentinel")
    assert lawapi.classify_date("2026.12.11", t) == ("2026-12-11", "future")
    assert lawapi.classify_date("", t) == (None, "missing")


def test_enumerate_two_terms_merge_matched_terms_and_idempotent(make_ctx, manifest):
    t = prefix_transport(handler())
    ctx = make_ctx(t)
    res = lawapi.run(ctx, args())
    assert res["documents"] == 5 and res["new_docs"] == 5 and res["terms_total"] == 27
    ctx = make_ctx(t)
    r1 = lawapi.run(ctx, args(terms=["정정보도", "기사"]))
    assert r1["terms_complete"] == 2 and r1["new_docs"] == 0 and r1["documents"] == 5
    d3 = manifest.get_document("lawgo_prec:3")
    assert json.loads(d3["extra_json"])["matched_terms"] == ["기사", "정정보도"]
    assert d3["detail_url"] == "https://www.law.go.kr/DRF/lawService.do?target=prec&ID=3&type=HTML&mobileYn="
    d4, d5 = (json.loads(manifest.get_document(f"lawgo_prec:{i}")["extra_json"]) for i in (4, 5))
    assert d4["date_status"] == "sentinel" and manifest.get_document("lawgo_prec:4")["year"] is None and d4["raw_date"] == "0001.01.01"
    assert d5["date_status"] == "future" and r1["max_valid_date"] == "1986-01-28"
    assert r1["case_no_date_groups_with_multiple_serials"] == 1          # 80도306 이 일련번호 둘(합치지 않음)


def test_slices_split_and_sum_check(monkeypatch, make_ctx):
    monkeypatch.setattr(lawapi, "SPLIT_THRESHOLD", 2)
    rows = {"정정보도": DB["정정보도"]}

    def h(url, n):
        q = up.parse_qs(up.urlsplit(url).query)
        rng = q.get("prncYd", [None])[0]
        sel = [r for r in rows["정정보도"] if rng is None or rng.split("~")[0][:4] == r["선고일자"][:4] or (rng.startswith("0001") and r["선고일자"] < "1950")]
        sel = [r for r in sel if rng is None or r["선고일자"][:4] == rng[:4]]
        d, p = int(q["display"][0]), int(q["page"][0])
        return 200, env(sel[(p - 1) * d:p * d], total=len(sel)), {}
    ctx = make_ctx(prefix_transport(h))
    res = lawapi.run(ctx, args(terms=["정정보도"]))
    st = json.loads((ctx.inventory_dir / lawapi.TERMS_FILE).read_text())["terms"]["정정보도"]
    assert st["split"] and st["sum_segment_totals"] == 3 and st["unique_serials"] == 3 and st["status"] == "complete"
    assert res["documents"] == 3 and len(st["segments"]) == len(lawapi.year_slices(2026))


def test_limit_pages_then_resume_without_duplicates(make_ctx):
    t = prefix_transport(handler())
    r1 = lawapi.run(make_ctx(t, limit=1), args(terms=["정정보도"]))
    assert r1["limited"] and r1["pages_handled"] == 1 and r1["terms_complete"] == 0
    r2 = lawapi.run(make_ctx(t), args(terms=["정정보도"]))
    assert r2["terms_complete"] == 1 and r2["documents"] == 3 and r2["new_docs"] == 1   # 1쪽(2건)은 앞 실행에서 들어갔다


def test_error_html_not_cached_and_three_failures_abort(make_ctx, manifest):
    t = prefix_transport(lambda url, n: (200, "<html>오류</html>", {}))
    ctx = make_ctx(t)
    with pytest.raises(IngestionError):
        lawapi.run(ctx, args(terms=["정정보도", "기사", "언론"]))
    assert manifest.counts()["documents"] == 0 and manifest.counts()["failures_open"] >= 1
    assert not [p for p in (ctx.corpus_dir / "http_cache" / "www.law.go.kr").glob("*.body") if "오류".encode() in p.read_bytes()]


def test_seed_check_by_serial_and_case_number(make_ctx, tmp_path):
    seeds = tmp_path / "s.json"
    seeds.write_text(json.dumps({"judgments": [
        {"id": "a", "url": "https://www.law.go.kr/LSW/precInfoP.do?precSeq=1", "date": "1986-01-28", "case_numbers": ["85다카1973"]},
        {"id": "b", "url": "https://www.law.go.kr/precInfoP.do?evtNo=x", "date": "1980-05-20", "case_numbers": ["80도306"]},
        {"id": "c", "url": "https://www.law.go.kr/precInfoP.do?evtNo=y", "date": "2000-07-28", "case_numbers": ["99다6203"]}]}))
    ctx = make_ctx(prefix_transport(handler()))
    res = lawapi.run(ctx, args(seeds_file=str(seeds)))
    assert [s["found"] for s in res["seeds"]] == [True, True, False]


def test_time_pause_keeps_partial_state_and_resumes(make_ctx, clock):
    from press_review.ingestion.context import TimeBudgetExceeded
    t = FakeTransport(ROBOTS, default=handler(), clock=clock, latency=3.0)
    ctx = make_ctx(t, max_seconds=7)
    with pytest.raises(TimeBudgetExceeded):
        lawapi.run(ctx, args(terms=["정정보도"], display=1))
    st = json.loads((ctx.inventory_dir / lawapi.TERMS_FILE).read_text())["terms"]["정정보도"]
    assert st["status"] == "partial" and st["time_paused"] and st["segments"][0]["pages_done"] >= 1
    r = lawapi.run(make_ctx(t), args(terms=["정정보도"], display=1))
    assert r["terms_complete"] == 1 and r["documents"] == 3


def test_source_scope_flags_non_court_sources():
    assert lawapi.source_scope("대법원", "대법원") == "supreme"
    assert lawapi.source_scope("", "국세법령정보시스템") == "non_court_source"
    assert lawapi.source_scope("대법원", "지방세법령정보시스템") == "supreme_via_other_source"
    assert lawapi.source_scope("서울고등법원", "대법원") == "lower_court_in_supreme_source"


def test_future_probe_starts_the_day_after_today_not_next_year(make_ctx):
    """V-NR-05: classify_date 가 'future' 로 보는 선고일(> 오늘)을 탐침이 같은 경계로 세야 한다(올해 안의 미래 날짜 포함)."""
    seen = []

    def h(url, n):
        q = up.parse_qs(up.urlsplit(url).query)
        rng = q.get("prncYd", [None])[0]
        seen.append(rng)
        return 200, env([], total={None: 75951, "00010101~18991231": 3426}.get(rng, 1)), {}
    ctx = make_ctx(prefix_transport(h))
    got = lawapi._probe_all(lawapi._Run(ctx, args()))
    today = ctx.now().date()                                   # 2026-10-08
    assert seen[2] == "20261009~99991231"
    assert got["after_today"] == 1 and got["after_today_from"] == "2026-10-09" and got["all"] == 75951
    assert lawapi.classify_date("2026.10.09", today)[1] == "future" and lawapi.classify_date("2026.10.08", today)[1] == "ok"


def test_saved_probe_in_old_format_is_probed_again(make_ctx):
    ctx = make_ctx(prefix_transport(handler()))
    ctx.inventory_dir.mkdir(parents=True, exist_ok=True)
    (ctx.inventory_dir / lawapi.TERMS_FILE).write_text(json.dumps({"terms": {}, "probe_all": {"all": 5, "before_1900": 1, "after_this_year": 0}}))
    res = lawapi.run(ctx, args(no_probe=False, terms=None, limit_pages=None))
    assert res["probe_all"]["after_today"] is not None and res["probe_all"]["after_today_from"] == "2026-10-09"

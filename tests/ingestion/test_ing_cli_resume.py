import io
import json
import sys
import types

import pytest

from conftest import FakeClock, FakeTransport
from press_review.ingestion import __main__ as cli
from press_review.ingestion.context import TimeBudgetExceeded
from press_review.ingestion.manifest import Manifest

PAGES = 6
ITEMS = 3
BASE = "https://www.pac.or.kr/kor/pages/?p=207&page="


def page_url(n):
    return f"{BASE}{n}"


def page_html(n):
    return "".join(f'<a href="/d/{n}-{i}">item</a>' for i in range(ITEMS))


def install_fake_source(monkeypatch, name="fakepac"):
    mod = types.ModuleType(f"press_review.ingestion.sources.{name}")

    def run(ctx, args):
        ctx.manifest.ensure_source("fakepac_list", "pac_cases", "fake", BASE + "1")
        pages = PAGES if ctx.limit_pages is None else min(PAGES, ctx.limit_pages)
        for n in range(1, pages + 1):
            ctx.check_time()
            url = page_url(n)
            if ctx.manifest.get_list_page("fakepac_list", url) and ctx.manifest.get_list_page("fakepac_list", url)["ok"]:
                continue                                                    # 장부로 이미 끝난 쪽은 건너뜀
            r = ctx.http.get(url, accept=lambda b, h: None if b"item" in b else "no marker")
            with ctx.manifest.transaction():
                if not r.ok:
                    ctx.manifest.record_failure(url, "list", "bad_page", r.content_error or r.status)
                    ctx.manifest.upsert_list_page("fakepac_list", n, url, http_status=r.status, item_count=0, body_sha256=None, ok=False)
                    continue
                for i in range(ITEMS):
                    ctx.manifest.upsert_document(f"pac_cases:{n}-{i}", "fakepac_list", title=f"t{n}-{i}")
                ctx.manifest.upsert_list_page("fakepac_list", n, url, http_status=200, item_count=ITEMS, body_sha256=r.sha256, ok=True)
        return {"pages": pages}

    mod.run = run
    mod.add_arguments = lambda p: p.add_argument("--flavor", default="plain")
    monkeypatch.setitem(sys.modules, f"press_review.ingestion.sources.{name}", mod)
    return mod


def make_transport():
    routes = {"https://www.pac.or.kr/robots.txt": (404, b"", {})}
    for n in range(1, PAGES + 1):
        routes[page_url(n)] = (200, page_html(n), {})
    return FakeTransport(routes)


def run_cli(argv, corpus, transport, clock, **kw):
    out = io.StringIO()
    code = cli.main(argv, transport=transport, clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus,
                    log_stream=io.StringIO(), **kw)
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def test_max_seconds_stops_then_resume_has_no_duplicates_or_refetches(monkeypatch, corpus):
    install_fake_source(monkeypatch)
    clock = FakeClock()
    t = make_transport()
    seen_status = []
    for _ in range(10):
        code, res = run_cli(["fakepac", "--max-seconds", "5"], corpus, t, clock)
        assert code == 0
        seen_status.append(res["status"])
        if res["status"] == "completed":
            break
    assert seen_status[0] == "paused_time_limit" and seen_status[-1] == "completed" and len(seen_status) >= 3
    m = Manifest(corpus / "manifest.sqlite")
    try:
        assert m.counts()["documents"] == PAGES * ITEMS                       # 중복 0, 누락 0
        assert m.counts()["list_pages"] == PAGES
        assert m._q("SELECT COUNT(DISTINCT doc_id) FROM documents")[0][0] == PAGES * ITEMS
        assert m.failures() == [] and m.counts()["failures_open"] == 0
        assert m._q("SELECT COUNT(*) FROM robots")[0][0] == 1                  # 실행이 여러 번이어도 robots는 하루 1회
    finally:
        m.close()
    for n in range(1, PAGES + 1):
        assert t.counts[page_url(n)] == 1, n                                   # 어느 쪽도 두 번 받지 않는다
    assert t.counts["https://www.pac.or.kr/robots.txt"] == 1


def test_resume_after_interruption_inside_a_page_is_idempotent(monkeypatch, corpus):
    """문서 일부만 쓴 뒤 중단돼도(트랜잭션 롤백) 재실행 결과가 같다."""
    mod = install_fake_source(monkeypatch)
    real_run = mod.run
    clock = FakeClock()
    t = make_transport()
    boom = {"on": True}

    def flaky(ctx, args):
        orig = ctx.manifest.upsert_list_page

        def up(*a, **k):
            if boom["on"] and a[1] == 2:
                boom["on"] = False
                raise RuntimeError("simulated crash")
            return orig(*a, **k)
        ctx.manifest.upsert_list_page = up
        return real_run(ctx, args)

    mod.run = flaky
    code, res = run_cli(["fakepac"], corpus, t, clock)
    assert code == 1 and res["status"] == "failed"
    m = Manifest(corpus / "manifest.sqlite")
    assert m.counts()["documents"] == ITEMS and m.counts()["list_pages"] == 1   # 2쪽 문서는 롤백됨
    m.close()
    code, res = run_cli(["fakepac"], corpus, t, clock)
    assert code == 0 and res["status"] == "completed"
    m = Manifest(corpus / "manifest.sqlite")
    assert m.counts()["documents"] == PAGES * ITEMS and m.counts()["list_pages"] == PAGES
    m.close()


def test_limit_pages_and_status_and_export_inventory(monkeypatch, corpus, tmp_path):
    install_fake_source(monkeypatch)
    clock = FakeClock()
    t = make_transport()
    code, res = run_cli(["fakepac", "--limit-pages", "2"], corpus, t, clock)
    assert res["status"] == "completed" and res["result"] == {"pages": 2} and res["manifest_counts"]["documents"] == 2 * ITEMS
    code, st = run_cli(["status"], corpus, t, clock)
    assert st["sources"]["fakepac_list"]["documents"] == 2 * ITEMS and st["robots"]["www.pac.or.kr"]["purpose_status"] == "no_rules"
    out_dir = tmp_path / "inv"
    code, ex = run_cli(["export-inventory", "--out-dir", str(out_dir)], corpus, t, clock)
    inv = json.loads((out_dir / "pac_cases.json").read_text(encoding="utf-8"))
    assert inv["documents"]["count"] == 2 * ITEMS and ex["written"]["pac_cases"]["documents"] == 2 * ITEMS
    assert inv["rights"]["fakepac_list"]["redistribute_raw"] == "permission_required"


def test_dry_run_reports_first_uncached_url_and_writes_nothing(monkeypatch, corpus):
    install_fake_source(monkeypatch)
    clock = FakeClock()
    t = make_transport()
    code, res = run_cli(["fakepac", "--dry-run"], corpus, t, clock)
    assert code == 0 and res["status"] == "dry_run_stopped" and res["would_fetch"] == "GET " + page_url(1)
    assert t.calls == [] and not (corpus / "manifest.sqlite").exists()


def test_adapter_flags_via_add_arguments_and_unknown_source(monkeypatch, corpus):
    mod = install_fake_source(monkeypatch)
    seen = {}
    mod.run = lambda ctx, args: seen.update(flavor=args.flavor, max=args.max_seconds, refresh=args.refresh) or {}
    clock = FakeClock()
    run_cli(["fakepac", "--flavor", "spicy", "--refresh"], corpus, make_transport(), clock)
    assert seen == {"flavor": "spicy", "max": None, "refresh": True}
    for bad in (["nope"], ["../etc"], ["Fake"], ["fakepac.x"]):
        with pytest.raises(SystemExit) as e:
            run_cli(bad, corpus, make_transport(), clock)
        assert e.value.code == 2


def test_adapter_crash_reports_failed_without_traceback_leak(monkeypatch, corpus):
    mod = install_fake_source(monkeypatch)

    def run(ctx, args):
        raise KeyError("https://www.law.go.kr/DRF/x?OC=SHOULDNOTLEAK")
    mod.run = run
    code, res = run_cli(["fakepac"], corpus, make_transport(), FakeClock())
    assert code == 1 and res["status"] == "failed" and "SHOULDNOTLEAK" not in json.dumps(res)


def test_time_budget_exception_is_normal_stop(monkeypatch, corpus):
    mod = install_fake_source(monkeypatch)

    def run(ctx, args):
        raise TimeBudgetExceeded("x")
    mod.run = run
    code, res = run_cli(["fakepac"], corpus, make_transport(), FakeClock())
    assert code == 0 and res["status"] == "paused_time_limit"


def test_bad_marker_page_is_a_failure_row_not_a_success(monkeypatch, corpus):
    """HTTP 200 오류 페이지(IENDC bare 주소 사례)는 성공으로 세지 않고 캐시하지 않는다."""
    install_fake_source(monkeypatch)
    clock = FakeClock()
    t = make_transport()
    t.routes[page_url(2)] = [(200, "비정상적 접근입니다", {}), (200, page_html(2), {})]
    code, res = run_cli(["fakepac"], corpus, t, clock)
    m = Manifest(corpus / "manifest.sqlite")
    assert m.counts()["list_pages"] == PAGES and m._q("SELECT COUNT(*) FROM list_pages WHERE ok=0")[0][0] == 1
    assert len(m.failures()) == 1
    m.close()
    # 다음 실행: 오류 쪽만 다시 받는다(캐시에 오류 페이지가 없다)
    code, res = run_cli(["fakepac"], corpus, t, clock)
    m = Manifest(corpus / "manifest.sqlite")
    assert m.counts()["documents"] == PAGES * ITEMS and t.counts[page_url(2)] == 2
    m.close()


def test_robots_check_adapter_records_only_requested_hosts(corpus):
    clock = FakeClock()
    t = FakeTransport({"https://www.pac.or.kr/robots.txt": (404, b"", {}),
                       "https://www.iendc.go.kr/robots.txt": (200, "User-agent: *\nAllow: /\n", {}),
                       "https://www.law.go.kr/robots.txt": (200, "User-agent: *\nAllow: /\n", {})})
    code, res = run_cli(["robots_check"], corpus, t, clock)
    r = res["result"]["robots"]
    assert set(r) == {"www.pac.or.kr", "www.iendc.go.kr", "www.law.go.kr"}
    assert r["www.pac.or.kr"]["http_status"] == 404 and r["www.iendc.go.kr"]["sha12"]
    assert sorted(u for _, u in t.calls) == sorted(["https://www.pac.or.kr/robots.txt", "https://www.iendc.go.kr/robots.txt",
                                                     "https://www.law.go.kr/robots.txt"])
    code, res = run_cli(["robots_check", "--host", "news.naver.com"], corpus, t, clock)
    assert res["result"]["robots"]["news.naver.com"] == {"error": "host not allowed"} and len(t.calls) == 3


def test_corpus_dir_ignores_foreign_plugin_data_env(monkeypatch, tmp_path):
    from press_review.ingestion.context import corpus_dir
    monkeypatch.delenv("PRESS_DISPUTE_DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / ".claude/plugins/data/codex-openai-codex"))
    assert corpus_dir() == tmp_path / ".local/share/press-dispute-review/corpus"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data/press-dispute-review-market"))
    assert corpus_dir() == tmp_path / "data/press-dispute-review-market/corpus"
    monkeypatch.setenv("PRESS_DISPUTE_DATA_DIR", str(tmp_path / "x"))
    assert corpus_dir() == tmp_path / "x/corpus"

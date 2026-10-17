"""검증에서 확인된 결함의 회귀 테스트(V-CC01/02/05/08/09, V-SEC-02/03/06, V-LOAD-1)."""
import os
import re
import subprocess
import sys
import textwrap

import pytest

import refschema
from press_review import article_io, contracts, corpus as corpus_mod, edit_checks as ec, logsafe, render, review
from press_review.store import EphemeralStore

import pdr_engine_paths as paths


def codes(issues, level=None):
    return [i.code for i in issues if level is None or i.level == level]


# ---- V-CC01: 부정어 활용형 -----------------------------------------------------------------

@pytest.mark.parametrize("s", ["아닌", "아님", "아냐", "아닙니다", "아니다", "피의자가 아닌 것으로"])
def test_cc01_a_ni_forms_count_as_negation(s):
    assert ec.count_negations(s) == 1


def test_cc01_removing_anin_raises_negation_changed(mk_article):
    art = mk_article([("title", "title", "제목"), ("p1", "body", "경찰은 피의자가 아닌 것으로 판단했다.")])
    issues = ec.check_replacement("피의자가 아닌 것으로", "피의자인 것으로", art, block_id="p1", start=4)
    assert "NEGATION_CHANGED" in codes(issues, "warning")


def test_cc01_no_overcount_on_plain_words():
    assert ec.count_negations("아내와 아이가 아침에 나갔다") == 0


# ---- V-CC02: 직접인용 안쪽 위치 인식 ---------------------------------------------------------

BODY = "그는 “돈을 받았다”고 말했다."


def _art(mk_article):
    return mk_article([("title", "title", "제목"), ("p1", "body", BODY)])


def _check(mk_article, quote, rep):
    start = BODY.index(quote)
    return ec.check_replacement(quote, rep, _art(mk_article), block_id="p1", start=start)


def test_cc02_insertion_inside_quote_from_span_start_inside_is_error(mk_article):
    issues = _check(mk_article, "받았다”고 말했다", "뇌물을 받았다”고 말했다")
    assert "DIRECT_QUOTE_CHANGED" in codes(issues, "error")


def test_cc02_edit_after_quote_close_is_allowed(mk_article):
    assert codes(_check(mk_article, "받았다”고 말했다", "받았다”고 밝혔다"), "error") == []


def test_cc02_span_end_inside_quote(mk_article):
    assert "DIRECT_QUOTE_CHANGED" in codes(_check(mk_article, "그는 “돈을", "그는 “뇌물 돈을"), "error")
    assert codes(_check(mk_article, "그는 “돈을", "그가 “돈을"), "error") == []


def test_cc02_span_wholly_inside_quote_is_error_even_if_replacement_contains_it(mk_article):
    assert "DIRECT_QUOTE_CHANGED" in codes(_check(mk_article, "받았", "받았고 받았"), "error")


# ---- V-CC05: partial / insufficient_information 은 호출자의 coverage_notes 가 필요 -----------------

class _Live(corpus_mod.FixtureCorpus):
    is_fixture = False

    @property
    def snapshot_id(self):
        return "live-test-1"


NOW = review._dt.datetime(2026, 10, 8, 12, 0, tzinfo=review._dt.timezone(review._dt.timedelta(hours=9)))


@pytest.mark.parametrize("status", ["partial", "insufficient_information"])
def test_cc05_status_without_caller_notes_is_rejected(article, status):
    res = review.build_report_ex(article, [], None, _Live(), now=NOW, overall_status=status, coverage_notes=None)
    assert res.report is None and any("coverage_notes" in e for e in res.errors)
    blank = review.build_report_ex(article, [], None, _Live(), now=NOW, overall_status=status, coverage_notes=["  "])
    assert blank.report is None
    ok = review.build_report_ex(article, [], None, _Live(), now=NOW, overall_status=status, coverage_notes=["입력 범위가 제목뿐이다."])
    assert ok.report is not None and ok.errors == []


def test_cc05_auto_status_still_works(article):
    res = review.build_report_ex(article, [], None, corpus_mod.FixtureCorpus(), now=NOW)
    assert res.report is not None and res.report["overall_status"] == "partial"


# ---- V-CC08: pattern 의 $ 는 입력 끝에서만 -------------------------------------------------------

def test_cc08_dollar_does_not_match_before_trailing_newline():
    schema = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    assert contracts.schema_errors("a" * 64, schema) == []
    assert contracts.schema_errors("a" * 64 + "\n", schema) != []


def test_cc08_dollar_inside_class_or_escaped_is_literal():
    assert contracts.schema_errors("a$", {"pattern": r"^a[$]$"}) == []
    assert contracts.schema_errors("a$", {"pattern": r"^a\$$"}) == []
    assert contracts.schema_errors("a\n", {"pattern": r"^a[$]$"}) != []


def test_cc08_source_record_raw_sha256_with_newline_rejected():
    from test_eng_schema_parity import source_cases
    import copy
    rec = copy.deepcopy(source_cases()[0]["base"])
    rec["raw_sha256"] = "a" * 64 + "\n"
    assert any("pattern" in e for e in contracts.validate_source_record(rec))


# ---- V-CC09: 비교 기준 검사기 자체의 정확성 --------------------------------------------------------

def test_cc09_reference_datetime_accepts_lowercase_t_and_z():
    assert refschema.ref_datetime("2026-10-05t12:00:00z") is True
    assert refschema.ref_datetime("2026-10-05T12:00:00z") is True
    assert refschema.ref_datetime("2026-10-05t12:00:00+09:00") is True
    assert refschema.ref_datetime("2026-10-05 12:00:00Z") is False
    assert refschema.ref_datetime("2026-10-05T12:00:60Z") is True  # 윤초


def test_cc09_reference_uri_rejects_bad_percent_encoding():
    assert refschema.ref_uri("https://e.invalid/%zz") is False
    assert refschema.ref_uri("https://e.invalid/%") is False
    assert refschema.ref_uri("https://e.invalid/%2F%7e") is True


@pytest.mark.parametrize("s", ["2026-10-05t12:00:00z", "2026-10-05T12:00:00Z", "2026-10-05t12:00:00+09:00", "2026-10-05 12:00:00Z", "2026-10-05T12:00:60Z"])
def test_cc09_contracts_datetime_agrees_with_reference(s):
    assert contracts.is_datetime(s) == refschema.ref_datetime(s)


@pytest.mark.parametrize("s", ["https://e.invalid/%zz", "https://e.invalid/%2F", "https://e.invalid/%", "https://e.invalid/a b"])
def test_cc09_contracts_uri_agrees_with_reference(s):
    assert contracts.is_uri(s) == refschema.ref_uri(s)


# ---- V-SEC-02 / V-SEC-03: TTL 만료 시 원고와 색인 삭제 -------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_sec02_on_expire_hook_fires_for_expiry_delete_clear():
    seen = []
    c = Clock()
    s = EphemeralStore(ttl_seconds=60, clock=c, on_expire=seen.append)
    s.put("a", 1)
    s.put("b", 2)
    s.put("c", 3, ttl_seconds=600)
    c.t += 61
    assert s.purge_expired() == 2
    assert sorted(seen) == ["a", "b"]
    s.put("d", 4)
    s.delete("d")
    s.clear()
    assert sorted(seen) == ["a", "b", "c", "d"]


def test_sec02_hook_failure_does_not_break_store():
    def bad(_k):
        raise RuntimeError("x")

    c = Clock()
    s = EphemeralStore(ttl_seconds=1, clock=c, on_expire=bad)
    s.put("a", 1)
    c.t += 2
    assert s.get("a") is None and len(s) == 0


def test_sec03_review_store_forgets_logsafe_index_after_expiry(article):
    c = Clock()
    store = review.new_store(ttl_seconds=1800, clock=c)
    prep = review.prepare_article(store, article=article)
    assert prep["ok"] and logsafe._articles
    c.t += 1801
    assert store.purge_expired() == 1
    assert not logsafe._articles and not store._items


def test_sec03_tool_call_style_cleanup_via_len(article):
    c = Clock()
    store = review.new_store(ttl_seconds=1800, clock=c)
    review.prepare_article(store, article=article)
    c.t += 1801
    assert len(store) == 0 and not logsafe._articles


# ---- V-SEC-06: 일반 파일이 아닌 입력과 st_size 0 ----------------------------------------------------

def _run_load(path, mem_limit=400_000_000, timeout=15):
    code = textwrap.dedent(f"""
        import resource, sys
        resource.setrlimit(resource.RLIMIT_AS, ({mem_limit}, {mem_limit}))
        from press_review import article_io
        try:
            article_io.load_article({str(path)!r})
        except article_io.ArticleLoadError:
            print("ArticleLoadError")
    """)
    env = {**os.environ, "PYTHONPATH": str(paths.ENGINE)}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=timeout, env=env)


@pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="needs /dev/zero")
def test_sec06_char_device_is_refused_not_read():
    r = _run_load("/dev/zero")
    assert r.returncode == 0 and r.stdout.strip() == "ArticleLoadError", r.stderr[-300:]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_sec06_fifo_is_refused_without_blocking(tmp_path):
    p = tmp_path / "pipe.txt"
    os.mkfifo(p)
    r = _run_load(p, timeout=10)
    assert r.returncode == 0 and r.stdout.strip() == "ArticleLoadError", r.stderr[-300:]


def test_sec06_read_is_bounded_even_if_stat_size_lies(tmp_path, monkeypatch):
    import pathlib
    p = tmp_path / "a.txt"
    p.write_bytes(b"x" * 500)
    monkeypatch.setattr(article_io, "MAX_FILE_BYTES", 100)
    real = os.stat(p)
    fake = os.stat_result(tuple(0 if i == 6 else v for i, v in enumerate(tuple(real)[:10])))
    monkeypatch.setattr(pathlib.Path, "stat", lambda self, **kw: fake)
    with pytest.raises(article_io.ArticleLoadError):
        article_io.load_article(p)


def test_sec06_cli_exits_2_on_device(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(paths.ENGINE), "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "press_review", "review", "--input", "/dev/zero"],
                       capture_output=True, text=True, timeout=15, env=env, cwd=str(tmp_path))
    assert r.returncode == 2 and "Traceback" not in r.stderr


# ---- V-LOAD-1: 스킬이 안내하는 설명 키와 서버 키의 일치 --------------------------------------------------

def _skill_keys():
    text = (paths.REPO / "plugins" / "press-check" / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8")
    sect = text.split("## 10.", 1)[1].split("## 11.", 1)[0]
    return re.findall(r"^\| `([a-z_]+)` \|", sect, flags=re.M)


def test_load1_review_skill_explanation_keys_are_server_keys():
    keys = _skill_keys()
    assert len(keys) == 7
    server = [k for k, _ in render._DETAIL_LABELS]
    assert keys == server


def test_load1_all_seven_cells_get_korean_titles_in_order():
    rep = {
        "findings": [{
            "finding_id": "finding-001", "issue_category": "x", "priority_stars": 2, "review_confidence": "low",
            "validation_status": "needs_information", "risk_channels": [], "reason_short": "r",
            "span": {"block_id": "p1", "start": 0, "end": 1, "quote": "q"},
            "suggested_edit": {"mode": "no_edit", "required_verification": []},
            "evidence": [], "counter_considerations": [],
        }],
    }
    expl = {k: f"v-{k}" for k in _skill_keys()}
    md = render.render_detail(rep, "finding-001", expl)
    for k in expl:
        assert f"**{k}**" not in md
    titles = ["적용 쟁점", "기사 맥락", "지지 근거", "사례와의 차이", "표현의 자유·공익 보도 측 근거", "필요한 취재 확인", "불확실성"]
    pos = [md.index(f"**{t}**") for t in titles]
    assert pos == sorted(pos)

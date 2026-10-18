"""CLI 연결 시험: 가드 중단 시 종료 코드 3, 확인 불가 시 4, 읽기 전용 서브커맨드는 가드를 부르지 않는다."""
import io
import json
import sys
import types

import pytest

from press_review import diskguard as dg
from press_review.ingestion import __main__ as cli

GB = 1024 ** 3


@pytest.fixture
def guard_env(monkeypatch):
    monkeypatch.setenv("PRESS_DISK_MIN_FREE_GB", "10")
    monkeypatch.setenv("PRESS_DISK_MARGIN_GB", "5")
    calls = []

    def fake(free):
        def fn(path):
            calls.append(path)
            if isinstance(free, Exception):
                raise free
            return free
        monkeypatch.setattr(dg, "_default_avail", fn)
    return fake, calls


def install_fake(monkeypatch, name, ran):
    mod = types.ModuleType(f"press_review.ingestion.sources.{name}")

    def run(ctx, args):
        ran.append(name)
        return {"ok": True}
    mod.run = run
    mod.add_arguments = lambda p: p.add_argument("--limit", type=int, default=None)
    monkeypatch.setitem(sys.modules, f"press_review.ingestion.sources.{name}", mod)


def run_cli(argv, tmp_path):
    out = io.StringIO()
    code = cli.main(argv, stdout=out, corpus=tmp_path / "corpus", log_stream=io.StringIO())
    return code, out.getvalue()


@pytest.mark.parametrize("target", ["fetch", "extract", "build_index", "lawapi", "pac", "iendc", "lawapi_body"])
def test_writing_subcommands_stop_with_3(target, guard_env, monkeypatch, tmp_path):
    fake, calls = guard_env
    fake(1 * GB)
    ran = []
    install_fake(monkeypatch, target, ran)
    code, out = run_cli([target], tmp_path)
    assert code == dg.EXIT_LOW == 3 and ran == []
    j = json.loads(out.strip().splitlines()[-1])
    assert j["status"] == "disk_guard_stopped" and j["kind"] == "low" and "여유" in j["message_ko"]


def test_unknown_gives_4(guard_env, monkeypatch, tmp_path):
    fake, _ = guard_env
    fake(OSError("gone"))
    ran = []
    install_fake(monkeypatch, "fetch", ran)
    code, out = run_cli(["fetch"], tmp_path)
    assert code == 4 and ran == []
    assert json.loads(out.strip().splitlines()[-1])["kind"] == "unknown"


def test_passes_when_enough(guard_env, monkeypatch, tmp_path):
    fake, calls = guard_env
    fake(100 * GB)
    ran = []
    install_fake(monkeypatch, "fetch", ran)
    code, _ = run_cli(["fetch"], tmp_path)
    assert code == 0 and ran == ["fetch"] and calls


def test_force_disk_runs_and_records(guard_env, monkeypatch, tmp_path, capsys):
    fake, _ = guard_env
    fake(1 * GB)
    ran = []
    install_fake(monkeypatch, "fetch", ran)
    code, _ = run_cli(["fetch", "--force-disk"], tmp_path)
    assert code == 0 and ran == ["fetch"]
    assert "--force-disk" in capsys.readouterr().err


def test_dry_run_not_guarded(guard_env, monkeypatch, tmp_path):
    fake, calls = guard_env
    fake(1 * GB)
    ran = []
    install_fake(monkeypatch, "fetch", ran)
    code, _ = run_cli(["fetch", "--dry-run"], tmp_path)
    assert code == 0 and ran == ["fetch"] and calls == []


def test_readonly_subcommands_not_guarded(guard_env, tmp_path):
    fake, calls = guard_env
    fake(1 * GB)
    code, _ = run_cli(["status"], tmp_path)
    assert code == 0 and calls == []
    code, _ = run_cli(["rights", "show"], tmp_path)
    assert code == 0 and calls == []
    code, _ = run_cli(["export-coverage", "--out-dir", str(tmp_path / "cov")], tmp_path)
    assert code == 0 and calls == []


def test_admission_apply_guarded_but_report_is_not(guard_env, tmp_path):
    fake, calls = guard_env
    fake(1 * GB)
    code, _ = run_cli(["admission", "--source", "pac_board_correction_status", "--apply"], tmp_path)
    assert code == 3 and calls
    calls.clear()
    code, _ = run_cli(["admission", "--source", "pac_board_correction_status"], tmp_path)
    assert code == 0 and calls == []
    code, _ = run_cli(["admission", "--source", "pac_board_correction_status", "--apply", "--dry-run"], tmp_path)
    assert code == 0 and calls == []


def test_admission_apply_force_is_recorded(guard_env, tmp_path, capsys):
    fake, _ = guard_env
    fake(1 * GB)
    code, out = run_cli(["admission", "--source", "pac_board_correction_status", "--apply", "--force-disk"], tmp_path)
    assert code == 0
    j = json.loads(out.strip().splitlines()[-1])
    assert j["disk_guard_forced"] and "admission" in j["disk_guard_forced"][0]


def test_mid_run_guard_error_gives_3(guard_env, monkeypatch, tmp_path):
    fake, _ = guard_env
    fake(100 * GB)
    mod = types.ModuleType("press_review.ingestion.sources.fetch")

    def run(ctx, args):
        raise dg.DiskGuardError("low", 1, 2, "중간 부족")
    mod.run = run
    mod.add_arguments = lambda p: p.add_argument("--limit", type=int, default=None)
    monkeypatch.setitem(sys.modules, "press_review.ingestion.sources.fetch", mod)
    code, out = run_cli(["fetch"], tmp_path)
    assert code == 3 and json.loads(out.strip().splitlines()[-1])["status"] == "paused_disk_low"


def test_ctx_check_time_ticks_guard(tmp_path):
    from press_review.ingestion.context import Budget, RunContext
    ticks = []
    ctx = RunContext(http=None, manifest=None, budget=Budget(None), logger=None, corpus_dir=tmp_path)
    ctx.extras["disk_guard"] = types.SimpleNamespace(tick=lambda: ticks.append(1))
    ctx.check_time(); ctx.check_time()
    assert len(ticks) == 2


def test_structure_cli_guard(guard_env, tmp_path):
    from press_review.ingestion import structure
    fake, calls = guard_env
    fake(1 * GB)
    out = io.StringIO()
    code = structure.main(["--corpus", str(tmp_path / "c"), "--plan-only"], out=out)
    assert code == 0 and calls == []                                        # plan-only 는 읽기 전용
    out = io.StringIO()
    code = structure.main(["--corpus", str(tmp_path / "c")], out=out, provider=object())
    assert code == 3 and json.loads(out.getvalue())["status"] == "disk_guard_stopped"
    fake(OSError("x"))
    code = structure.main(["--corpus", str(tmp_path / "c")], out=io.StringIO(), provider=object())
    assert code == 4


def test_build_index_guard_before_tmp(guard_env, tmp_path):
    from press_review import corpus_sqlite as cs
    from press_review.ingestion.manifest import Manifest
    fake, calls = guard_env
    fake(1 * GB)
    # 장부에 passage 가 없으면 no_passages 로 끝나 가드에 닿지 않는다 -> 가드 지점은 임시 파일 직전이므로
    # 가드 함수를 직접 호출하는 헬퍼를 시험한다.
    with pytest.raises(dg.DiskGuardError):
        cs._guard_index_build(tmp_path / "idx", force=False)
    assert not (tmp_path / "idx").exists()


def test_structurer_pauses_on_midrun_guard(tmp_path):
    from press_review.ingestion import structure
    from press_review.ingestion.manifest import Manifest
    n = {"c": 0}

    class G:
        def start(self, n_items):
            pass

        def advance(self, n=1):
            pass

        def tick(self):
            n["c"] += 1
            if n["c"] == 2:
                raise dg.DiskGuardError("low", 1, 2, "부족")
    prov = types.SimpleNamespace(budget=types.SimpleNamespace(remaining=lambda: None))
    m = Manifest(tmp_path / "m.sqlite")
    try:
        st = structure.Structurer(m, prov, tmp_path, disk_guard=G())
        st._target = lambda t: None
        res = st.run([object(), object(), object()])
    finally:
        m.close()
    assert res["status"] == "paused_disk_low" and res["targets"] == 2 and st.disk_error.kind == "low"


def test_dispatcher_start_check_ignores_limit(guard_env, monkeypatch, tmp_path):
    fake, calls = guard_env
    fake(20 * GB)                                                            # --limit 10000 x 2MB 로 추정하면 막히지만, 시작 검사는 예상량 0
    ran = []
    install_fake(monkeypatch, "fetch", ran)
    code, _ = run_cli(["fetch", "--limit", "10000"], tmp_path)
    assert code == 0 and ran == ["fetch"]


def _fetch_ctx(tmp_path, guard):
    from press_review.ingestion import fetch as f
    ctx = types.SimpleNamespace(manifest=object(), corpus_dir=tmp_path, dry_run=True, extras={"disk_guard": guard},
                                check_time=lambda: None, logger=types.SimpleNamespace(info=lambda *a, **k: None))
    return f, ctx


def test_run_fetch_checks_actual_candidates(monkeypatch, tmp_path):
    from press_review.ingestion import stage
    f, ctx = _fetch_ctx(tmp_path, None)
    ctx.dry_run = False
    att = {"att_id": "a#1", "source_id": "s", "doc_id": "d"}
    monkeypatch.setattr(f, "select_candidates", lambda *a, **k: ([att], {}))
    monkeypatch.setattr(f, "fetch_one", lambda c, a, fam: {"outcome": "fetched"})
    monkeypatch.setattr(stage, "family_of", lambda *a, **k: "fam")
    ctx.extras["disk_guard"] = dg.DiskGuard("PDF 수신", dg.PDF_BYTES, avail_fn=lambda p: 20 * GB)
    res = f.run_fetch(ctx, limit=10000)                                      # 후보 1개: 통과
    assert res["fetched"] == 1 and ctx.extras["disk_guard"].done == 1        # 완료 지점에서 advance
    monkeypatch.setattr(f, "select_candidates", lambda *a, **k: ([att] * 20000, {}))
    ctx.extras["disk_guard"] = dg.DiskGuard("PDF 수신", dg.PDF_BYTES, avail_fn=lambda p: 20 * GB)
    with pytest.raises(dg.DiskGuardError):                                   # 후보 2만 개: 예상량 초과
        f.run_fetch(ctx)


def test_structurer_start_checks_target_count(tmp_path):
    from press_review.ingestion import structure
    from press_review.ingestion.manifest import Manifest
    prov = types.SimpleNamespace(budget=types.SimpleNamespace(remaining=lambda: None))
    m = Manifest(tmp_path / "m.sqlite")
    try:
        g = dg.DiskGuard("구조화", 1 * GB, avail_fn=lambda p: 20 * GB)
        st = structure.Structurer(m, prov, tmp_path, disk_guard=g)
        done = []
        st._target = lambda t: done.append(1)
        res = st.run([object() for _ in range(30)])                          # 30GB 예상 > 여유 20GB
        assert res["status"] == "paused_disk_low" and done == []
        g2 = dg.DiskGuard("구조화", 1 * GB, avail_fn=lambda p: 20 * GB)
        st2 = structure.Structurer(m, prov, tmp_path, disk_guard=g2)
        st2._target = lambda t: done.append(1)
        res2 = st2.run([object() for _ in range(3)])
        assert res2["status"] == "completed" and len(done) == 3 and g2.done == 3
    finally:
        m.close()

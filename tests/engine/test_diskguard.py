"""디스크 가드: 실제 디스크를 채우지 않고 avail_fn 주입으로 시험한다."""
import pytest

from press_review import diskguard as dg

GB = 1024 ** 3
MB = 1024 ** 2


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # tests/conftest.py 가 0 으로 두는 값을 단위 시험에서는 기본값 상태로 되돌린다
    for k in ("PRESS_DISK_MIN_FREE_GB", "PRESS_DISK_MARGIN_GB", "PRESS_DISK_PATH", "PRESS_DISK_PDF_MB",
              "PRESS_DISK_CASE_JSON_MB", "PRESS_DISK_STRUCT_MB", "PRESS_DISK_INDEX_FACTOR"):
        monkeypatch.delenv(k, raising=False)


def avail(n):
    return lambda path: n


def test_enough_free_passes():
    dg.require_free(1 * GB, "t", avail_fn=avail(30 * GB))


def test_below_min_free_stops():
    with pytest.raises(dg.DiskGuardError) as ei:
        dg.require_free(0, "t", avail_fn=avail(9 * GB))
    assert ei.value.kind == "low" and ei.value.free_bytes == 9 * GB


def test_estimate_plus_margin_stops():
    # 여유 20GB >= 최소 10GB 지만 예상 16GB + 마진 5GB = 21GB 가 필요하다
    with pytest.raises(dg.DiskGuardError) as ei:
        dg.require_free(16 * GB, "t", avail_fn=avail(20 * GB))
    assert ei.value.kind == "low" and ei.value.needed_bytes == 21 * GB


def test_boundaries_exact_pass_one_byte_less_fails():
    dg.require_free(0, "t", avail_fn=avail(10 * GB))                      # 정확히 최소 여유: 통과
    with pytest.raises(dg.DiskGuardError):
        dg.require_free(0, "t", avail_fn=avail(10 * GB - 1))
    dg.require_free(5 * GB, "t", avail_fn=avail(10 * GB))                # 예상+마진 = 정확히 10GB: 통과
    with pytest.raises(dg.DiskGuardError):
        dg.require_free(5 * GB + 1, "t", avail_fn=avail(10 * GB))


def test_statvfs_failure_is_unknown(monkeypatch):
    def boom(path):
        raise OSError("mount gone")
    with pytest.raises(dg.DiskGuardError) as ei:
        dg.require_free(1, "t", avail_fn=boom)
    assert ei.value.kind == "unknown" and ei.value.free_bytes is None
    monkeypatch.setattr(dg.os, "statvfs", lambda p: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(dg.DiskGuardError) as ei2:
        dg.require_free(1, "t", path="/nonexistent-mount")
    assert ei2.value.kind == "unknown"


def test_env_thresholds_and_path(monkeypatch):
    seen = []

    def fn(path):
        seen.append(str(path))
        return 8 * GB
    monkeypatch.setenv("PRESS_DISK_PATH", "/some/where")
    monkeypatch.setenv("PRESS_DISK_MIN_FREE_GB", "7")
    monkeypatch.setenv("PRESS_DISK_MARGIN_GB", "1")
    dg.require_free(6 * GB, "t", avail_fn=fn)                                # 8 >= max(7, 6+1)
    assert seen == ["/some/where"]
    with pytest.raises(dg.DiskGuardError):
        dg.require_free(7 * GB + 1, "t", avail_fn=fn)                        # 예상+마진 = 8GB+1 > 여유 8GB
    dg.require_free(7 * GB, "t", avail_fn=fn)                                # 정확히 8GB: 통과
    monkeypatch.setenv("PRESS_DISK_MIN_FREE_GB", "9")
    with pytest.raises(dg.DiskGuardError):
        dg.require_free(0, "t", avail_fn=fn)
    # 인자가 환경변수보다 우선
    dg.require_free(0, "t", avail_fn=fn, min_free_gb=1, margin_gb=0, path="/arg/path")
    assert seen[-1] == "/arg/path"


def test_default_path_prefers_mnt_c_then_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(dg, "WSL_C_PATH", str(tmp_path))                     # 존재하는 /mnt/c 역할
    assert str(dg.resolve_path()) == str(tmp_path)
    monkeypatch.setattr(dg, "WSL_C_PATH", str(tmp_path / "absent"))
    fb = tmp_path / "data"                                                   # 비 WSL: 없는 하위는 존재하는 부모로 대체
    assert str(dg.resolve_path(fallback=fb, is_wsl=False)) == str(tmp_path)


def test_wsl_without_mnt_c_is_unknown_not_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(dg, "WSL_C_PATH", str(tmp_path / "absent"))
    with pytest.raises(dg.DiskGuardError) as ei:
        dg.resolve_path(fallback=tmp_path, is_wsl=True)
    assert ei.value.kind == "unknown"
    monkeypatch.setattr(dg, "_is_wsl", lambda: True)
    with pytest.raises(dg.DiskGuardError) as ei2:                            # require_free 도 다른 파일시스템으로 대체하지 않는다
        dg.require_free(1, "t", avail_fn=avail(100 * GB), fallback_path=tmp_path)
    assert ei2.value.kind == "unknown" and "PRESS_DISK_PATH" in ei2.value.message_ko
    monkeypatch.setenv("PRESS_DISK_PATH", str(tmp_path))                     # 경로를 명시하면 통과
    dg.require_free(1, "t", avail_fn=avail(100 * GB), fallback_path=tmp_path)
    monkeypatch.delenv("PRESS_DISK_PATH")
    monkeypatch.setattr(dg, "_is_wsl", lambda: False)                        # 비 WSL 은 fallback 사용
    dg.require_free(1, "t", avail_fn=avail(100 * GB), fallback_path=tmp_path)


def test_is_wsl_detection(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert dg._is_wsl() is True


def test_message_is_korean_with_numbers():
    with pytest.raises(dg.DiskGuardError) as ei:
        dg.require_free(2 * GB, "PDF 수신", avail_fn=avail(3 * GB))
    m = ei.value.message_ko
    assert "PDF 수신" in m and "3.00GB" in m and "7.00GB" in m and "10" in m and "vhdx" in m
    assert str(ei.value) == m
    with pytest.raises(dg.DiskGuardError) as ei2:
        dg.require_free(1, "x", avail_fn=lambda p: (_ for _ in ()).throw(OSError("n")))
    assert "확인" in ei2.value.message_ko


def test_exit_code_constants():
    assert dg.EXIT_LOW == 3 and dg.EXIT_UNKNOWN == 4
    assert dg.exit_code(dg.DiskGuardError("low", 1, 2, "m")) == 3
    assert dg.exit_code(dg.DiskGuardError("unknown", None, 2, "m")) == 4


def test_force_warns_and_calls_back(capsys):
    got = []
    dg.require_free(1 * GB, "t", avail_fn=avail(1 * GB), force=True, on_force=got.append)
    err = capsys.readouterr().err
    assert "디스크" in err and "--force-disk" in err and len(err.strip().splitlines()) == 1
    assert len(got) == 1 and "t" in got[0]
    dg.require_free(1 * GB, "t", avail_fn=avail(1 * GB), force=True)          # 콜백 없이도 동작
    dg.require_free(1 * GB, "t", avail_fn=avail(100 * GB), force=True)        # 부족하지 않으면 경고도 없다
    assert capsys.readouterr().err.count("\n") == 1


def test_force_overrides_unknown_too(capsys):
    dg.require_free(1, "t", avail_fn=lambda p: (_ for _ in ()).throw(OSError("n")), force=True)
    assert "디스크" in capsys.readouterr().err


def test_diskguard_tick_checks_every_n():
    calls = []
    free = {"v": 100 * GB}

    def fn(path):
        calls.append(1)
        return free["v"]
    g = dg.DiskGuard("수신", per_item_bytes=1 * GB, check_every=3, avail_fn=fn)
    g.start(10)                                                              # 예상 10GB + 마진 5 <= 100
    assert len(calls) == 1
    g.tick(); g.tick()
    assert len(calls) == 1
    free["v"] = 9 * GB                                                      # 최소 10GB 미만으로 급감
    with pytest.raises(dg.DiskGuardError) as ei:
        g.tick()
    assert ei.value.kind == "low" and len(calls) == 2


def test_diskguard_start_refuses_and_remaining_estimate():
    g = dg.DiskGuard("수신", per_item_bytes=1 * GB, check_every=2, avail_fn=avail(20 * GB))
    with pytest.raises(dg.DiskGuardError):
        g.start(16)                                                          # 16+5 > 20
    g2 = dg.DiskGuard("수신", per_item_bytes=1 * GB, check_every=2, avail_fn=avail(20 * GB))
    g2.start(10)
    g2.advance(3)
    g2.tick(); g2.tick()                                                     # 남은 7개 -> 12 <= 20
    g2.advance(6)
    g2.tick(); g2.tick()                                                     # 남은 1개


def test_tick_does_not_consume_remaining():
    """check_time 은 페이지마다 불리므로 tick 횟수가 완료 항목 수가 아니다: advance 없이는 remaining 이 줄지 않는다."""
    free = {"v": 200 * GB}
    g = dg.DiskGuard("x", per_item_bytes=1 * GB, check_every=25, avail_fn=lambda p: free["v"])
    g.start(100)                                                             # 100GB + 5 <= 200GB
    free["v"] = 20 * GB                                                      # 이후 여유가 줄었다
    with pytest.raises(dg.DiskGuardError):
        for _ in range(1000):
            g.tick()                                                         # tick 만 1000번: remaining 은 여전히 100
    assert g.done == 0
    g.advance(95)                                                            # 남은 5개 -> 5GB + 5 <= 20GB
    for _ in range(50):
        g.tick()


def test_diskguard_tick_before_start_uses_check_every_window():
    g = dg.DiskGuard("x", per_item_bytes=1 * MB, check_every=2, avail_fn=avail(100 * GB))
    g.tick(); g.tick()


def test_diskguard_force_continues_with_callback(capsys):
    got = []
    g = dg.DiskGuard("x", per_item_bytes=1 * MB, check_every=1, avail_fn=avail(1 * GB), force=True, on_force=got.append)
    g.start(1); g.tick(); g.tick()
    assert len(got) >= 1


def test_estimate_constants_and_env(monkeypatch):
    assert dg.PDF_BYTES == 2 * MB and dg.CASE_JSON_BYTES == int(0.3 * MB) and dg.STRUCT_BYTES == int(0.1 * MB)
    assert dg.estimate("pdf", 10) == 10 * dg.PDF_BYTES
    assert dg.estimate("case_json", 10) == 10 * dg.CASE_JSON_BYTES
    assert dg.estimate("struct", 10) == 10 * dg.STRUCT_BYTES
    monkeypatch.setenv("PRESS_DISK_PDF_MB", "4")
    assert dg.estimate("pdf", 1) == 4 * MB
    with pytest.raises(KeyError):
        dg.estimate("nope", 1)


def test_index_and_backup_estimates(tmp_path, monkeypatch):
    idx = tmp_path / "index"
    idx.mkdir()
    assert dg.index_build_estimate(idx) == 0                                  # 이전 snapshot 없음
    (idx / "corpus-a.sqlite").write_bytes(b"x" * 1000)
    (idx / "CURRENT").write_text("corpus-a.sqlite\n")
    assert dg.index_build_estimate(idx) == int(1000 * 1.15)
    monkeypatch.setenv("PRESS_DISK_INDEX_FACTOR", "2")
    assert dg.index_build_estimate(idx) == 2000
    f = tmp_path / "f.db"
    f.write_bytes(b"y" * 123)
    assert dg.backup_estimate(f) == 123

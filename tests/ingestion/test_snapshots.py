import datetime as dt
import json
import sqlite3

import pytest

from press_review import diskguard as dg
from press_review import snapshots as sn

T1 = dt.datetime(2026, 10, 18, 9, 5)
T2 = dt.datetime(2026, 10, 18, 9, 6)


def mk_db(path, rows=3):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (a INTEGER)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    c.commit()
    c.close()
    return path


def test_backup_sqlite_ok_and_manifest(tmp_path):
    src = mk_db(tmp_path / "manifest.sqlite")
    dest = tmp_path / "bk"
    p = sn.make_backup(src, "pre-migration", dest, now=T1)
    assert p.name == "20261018-0905_pre-migration_manifest.sqlite" and p.exists()
    assert not list(dest.glob("*.tmp"))
    lines = (dest / "MANIFEST.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["name"] == p.name and rec["purpose"] == "pre-migration" and rec["source_path"] == str(src)
    assert rec["bytes"] == p.stat().st_size and rec["sqlite"]["integrity_check"] == "ok" and rec["sqlite"]["tables"] == {"t": 3}
    assert rec["created_at"]


def test_backup_plain_file_sha256(tmp_path):
    src = tmp_path / "a.json"
    src.write_text("hello")
    p = sn.make_backup(src, "x", tmp_path / "bk", now=T1)
    rec = json.loads((tmp_path / "bk" / "MANIFEST.jsonl").read_text())
    assert rec["source_sha256"] and "sqlite" not in rec and p.read_text() == "hello"


def test_verify_failure_removes_copy_keeps_previous(tmp_path):
    good = mk_db(tmp_path / "good.sqlite")
    dest = tmp_path / "bk"
    prev = sn.make_backup(good, "keep", dest, now=T1)
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
    with pytest.raises(sn.BackupError):
        sn.make_backup(bad, "bad", dest, now=T2)
    assert prev.exists()
    assert sorted(x.name for x in dest.iterdir()) == sorted([prev.name, "MANIFEST.jsonl"])
    assert len((dest / "MANIFEST.jsonl").read_text().strip().splitlines()) == 1


def test_guard_stop_creates_no_copy(tmp_path, monkeypatch):
    src = mk_db(tmp_path / "m.sqlite")
    dest = tmp_path / "bk"
    monkeypatch.setenv("PRESS_DISK_MIN_FREE_GB", "10")
    monkeypatch.setattr(dg, "_default_avail", lambda path: 1)
    with pytest.raises(dg.DiskGuardError):
        sn.make_backup(src, "x", dest, now=T1)
    assert not dest.exists() or not any(dest.iterdir())
    sn.make_backup(src, "x", dest, now=T1, guard=False)                      # guard=False 는 건너뛴다
    assert any(dest.glob("*_m.sqlite"))


def test_existing_destination_not_overwritten(tmp_path):
    src = mk_db(tmp_path / "m.sqlite")
    sn.make_backup(src, "x", tmp_path / "bk", now=T1)
    with pytest.raises(FileExistsError):
        sn.make_backup(src, "x", tmp_path / "bk", now=T1)


def test_list_and_prune_requires_confirm(tmp_path):
    src = mk_db(tmp_path / "m.sqlite")
    dest = tmp_path / "bk"
    a = sn.make_backup(src, "a", dest, now=T1)
    b = sn.make_backup(src, "b", dest, now=T2)
    assert [r["name"] for r in sn.list_backups(dest)] == [a.name, b.name]
    res = sn.prune(dest, [a.name], confirm=False)
    assert res["deleted"] == [] and res["targets"] == [a.name] and a.exists()
    res = sn.prune(dest, [a.name], confirm=True)
    assert res["deleted"] == [a.name] and not a.exists() and b.exists()
    assert [r["name"] for r in sn.list_backups(dest)] == [b.name]


def test_prune_rejects_unknown_and_path_names(tmp_path):
    src = mk_db(tmp_path / "m.sqlite")
    dest = tmp_path / "bk"
    a = sn.make_backup(src, "a", dest, now=T1)
    victim = tmp_path / "victim.txt"
    victim.write_text("x")
    for bad in ("../victim.txt", "MANIFEST.jsonl", "nope.sqlite"):
        with pytest.raises(ValueError):
            sn.prune(dest, [bad], confirm=True)
    assert victim.exists() and a.exists() and (dest / "MANIFEST.jsonl").exists()
    with pytest.raises(ValueError):
        sn.prune(dest, [], confirm=True)                                      # 이름 목록이 비면 거부(자동 삭제 없음)


def test_wal_committed_rows_are_in_backup(tmp_path):
    """WAL 에만 있는 커밋 데이터가 사본에 있어야 한다(원본 연결을 닫지 않은 상태)."""
    src = tmp_path / "wal.sqlite"
    c = sqlite3.connect(src)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE t (a INTEGER)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(3)])
    c.commit()
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(3, 8)])
    c.commit()
    assert (tmp_path / "wal.sqlite-wal").stat().st_size > 0
    try:
        p = sn.make_backup(src, "wal", tmp_path / "bk", now=T1)
    finally:
        c.close()
    rec = json.loads((tmp_path / "bk" / "MANIFEST.jsonl").read_text())
    assert rec["sqlite"]["tables"] == {"t": 8} and rec["sqlite"]["integrity_check"] == "ok"
    r = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    assert r.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 8
    r.close()


def test_backup_count_mismatch_is_rejected(tmp_path, monkeypatch):
    src = mk_db(tmp_path / "m.sqlite")
    real = sn._sqlite_summary

    def lying(path):
        out = real(path)
        out["tables"] = {k: v + 1 for k, v in out["tables"].items()}
        return out
    monkeypatch.setattr(sn, "_sqlite_summary", lying)
    with pytest.raises(sn.BackupError):
        sn.make_backup(src, "x", tmp_path / "bk", now=T1)
    assert not [p for p in (tmp_path / "bk").iterdir() if p.name != "MANIFEST.jsonl"]


def test_concurrent_loser_does_not_delete_winner_copy(tmp_path, monkeypatch):
    """A 가 복사 중일 때 같은 이름으로 B 가 들어와도, A 가 실패하며 B 의 사본을 지우지 않는다."""
    a_src = tmp_path / "a" / "m.sqlite"
    b_src = tmp_path / "b" / "m.sqlite"
    a_src.parent.mkdir(); b_src.parent.mkdir()
    mk_db(a_src); mk_db(b_src, rows=7)
    dest = tmp_path / "bk"
    res = {}
    real = sn._copy_sqlite

    def hook(src, tmp):
        if src == a_src:
            try:
                res["b"] = sn.make_backup(b_src, "x", dest, now=T1)
            except FileExistsError:
                res["b"] = None
            raise sn.BackupError("A fails after copy")
        return real(src, tmp)
    monkeypatch.setattr(sn, "_copy_sqlite", hook)
    with pytest.raises(sn.BackupError):
        sn.make_backup(a_src, "x", dest, now=T1)
    if res["b"] is not None:
        assert res["b"].exists()                                              # B 가 성공했다면 그 사본은 남아야 한다
    assert not list(dest.glob("*.tmp")) and not list(dest.glob("*.tmp-*"))
    assert [r["name"] for r in sn.list_backups(dest)] == ([res["b"].name] if res["b"] else [])


def test_second_call_same_name_does_not_clobber_first(tmp_path):
    a = mk_db(tmp_path / "a" / "m.sqlite") if (tmp_path / "a").mkdir() is None else None
    first = sn.make_backup(a, "x", tmp_path / "bk", now=T1)
    before = first.read_bytes()
    with pytest.raises(FileExistsError):
        sn.make_backup(a, "x", tmp_path / "bk", now=T1)
    assert first.read_bytes() == before


def _only_manifest(dest):
    return not [p for p in dest.iterdir() if p.name != "MANIFEST.jsonl"]


def test_failure_during_copy_leaves_no_final(tmp_path, monkeypatch):
    src = mk_db(tmp_path / "m.sqlite")
    dest = tmp_path / "bk"
    seen = {}

    def boom(s, tmp):
        seen["final_exists"] = any(p.suffix == ".sqlite" for p in dest.iterdir())      # 복사 중에는 빈 최종 파일이 없다
        raise OSError("disk full")
    monkeypatch.setattr(sn, "_copy_sqlite", boom)
    with pytest.raises(OSError):
        sn.make_backup(src, "x", dest, now=T1)
    assert seen["final_exists"] is False and _only_manifest(dest)


def test_manifest_write_failure_removes_final(tmp_path, monkeypatch):
    src = mk_db(tmp_path / "m.sqlite")
    dest = tmp_path / "bk"
    monkeypatch.setattr(sn, "_append_manifest", lambda d, rec: (_ for _ in ()).throw(OSError("no space")))
    with pytest.raises(OSError):
        sn.make_backup(src, "x", dest, now=T1)
    assert _only_manifest(dest)


def test_existing_final_not_overwritten_and_no_tmp_left(tmp_path):
    src = mk_db(tmp_path / "m.sqlite")
    dest = tmp_path / "bk"
    dest.mkdir()
    existing = dest / "20261018-0905_x_m.sqlite"
    existing.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        sn.make_backup(src, "x", dest, now=T1)
    assert existing.read_bytes() == b"precious" and _only_manifest_except(dest, existing)


def _only_manifest_except(dest, keep):
    return not [p for p in dest.iterdir() if p.name not in ("MANIFEST.jsonl", keep.name)]

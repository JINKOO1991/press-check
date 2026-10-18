"""사본(백업) 보존 정책: 검증된 사본을 만들고 장부(MANIFEST.jsonl)에 남긴다. 이 모듈은 사본을 스스로 지우지 않는다.

``make_backup`` 은 디스크 가드(예상 = 원본 크기) -> 임시 이름(.tmp)으로 복사 -> rename -> 무결성 검증 -> MANIFEST.jsonl 한 줄 기록 순이다.
검증 실패 시 이번 사본만 지우고 예외를 던진다. 이전 사본은 건드리지 않는다. 같은 이름이 있으면 덮어쓰지 않고 거부한다.

보존 정책(keep): 되돌릴 수 있는 지점을 1개 이상 유지하는 것을 권고한다. 작업 전 사본은 작업 결과를 확인한 뒤에 정리하고, 정리는 사람이
``prune(dest_dir, names, confirm=True)`` 로 이름을 명시해서만 한다(자동 삭제 없음). 디스크가 빠듯한 상황에서도 마지막 정상 사본은 남긴다.

SQLite 파일은 파일 복사가 아니라 ``sqlite3.Connection.backup()``(원본은 ``mode=ro`` 연결, 읽기 트랜잭션 안)으로 일관된 사본을 만든다. 원본 연결이 열려
있어 커밋 데이터가 WAL 에만 있어도 사본에 들어간다. 검증은 사본의 integrity_check 와 표별 행 수가 원본 읽기 시점과 같은지 확인한다.
발행: 호출별 고유 임시 파일에서 복사·검증을 끝낸 뒤 os.link 로 최종 이름에 원자적으로 발행한다(이미 있으면 FileExistsError, 빈 최종 파일 없음). MANIFEST 기록이 실패하면 자기가 발행한 사본을 지운다.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from . import diskguard

MANIFEST_NAME = "MANIFEST.jsonl"
_SQLITE_MAGIC = b"SQLite format 3\x00"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class BackupError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def _sqlite_summary(path: Path) -> dict:
    """사본을 읽기 전용·immutable 로 열어 integrity_check 와 표별 행 수를 돌려준다. 실패하면 BackupError."""
    try:
        c = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            res = c.execute("PRAGMA integrity_check").fetchall()
            if res != [("ok",)]:
                raise BackupError("integrity_check failed: " + "; ".join(str(r[0]) for r in res[:3]))
            counts = _row_counts(c)
        finally:
            c.close()
    except sqlite3.Error as e:
        raise BackupError(f"sqlite verification failed: {type(e).__name__}") from None
    return {"integrity_check": "ok", "tables": counts}


def _row_counts(c: sqlite3.Connection) -> dict:
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: c.execute('SELECT COUNT(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0] for t in tables}


def _copy_sqlite(src: Path, tmp: Path) -> dict:
    """원본을 읽기 전용으로 열어 읽기 트랜잭션 안에서 행 수를 세고 Connection.backup() 으로 tmp 에 복사한다. 원본 시점의 행 수를 돌려준다."""
    try:
        sc = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
        try:
            sc.execute("BEGIN")
            counts = _row_counts(sc)
            dc = sqlite3.connect(tmp)
            try:
                sc.backup(dc)
            finally:
                dc.close()
        finally:
            sc.close()
    except sqlite3.Error as e:
        raise BackupError(f"sqlite backup failed: {type(e).__name__}") from None
    return counts


def make_backup(src, purpose: str, dest_dir, *, guard: bool = True, now: Optional[_dt.datetime] = None,
                force_disk: bool = False, on_force=None) -> Path:
    s, d = Path(src), Path(dest_dir)
    if not s.is_file():
        raise FileNotFoundError(f"backup source not found: {s}")
    if guard:
        diskguard.require_free(diskguard.backup_estimate(s), f"백업 {s.name}", force=force_disk, on_force=on_force,
                               fallback_path=d)
    now = now or _dt.datetime.now()
    name = f"{now.strftime('%Y%m%d-%H%M')}_{_SAFE.sub('-', purpose).strip('-') or 'backup'}_{s.name}"
    d.mkdir(parents=True, exist_ok=True)
    final = d / name
    tmp = d / f"{name}.{os.getpid()}-{os.urandom(4).hex()}.tmp"
    published = False
    try:
        rec = {"name": name, "purpose": purpose, "created_at": now.isoformat(timespec="seconds"), "source_path": str(s)}
        if _is_sqlite(s):
            src_counts = _copy_sqlite(s, tmp)
            summary = _sqlite_summary(tmp)
            if summary["tables"] != src_counts:
                raise BackupError("row counts differ between source and copy")
            rec["sqlite"] = summary
            rec["source_sha256"] = _sha256(tmp)         # 사본 해시
        else:
            shutil.copy2(s, tmp)
            src_sha, dst_sha = _sha256(s), _sha256(tmp)
            if src_sha != dst_sha:
                raise BackupError("sha256 mismatch between source and copy")
            rec["source_sha256"] = src_sha
        rec["bytes"] = tmp.stat().st_size
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, final)                          # 검증을 마친 완성본만 원자적으로 발행한다. 이미 있으면 덮어쓰지 않는다
        except FileExistsError:
            raise FileExistsError(f"backup already exists (not overwritten): {final}") from None
        published = True
        _append_manifest(d, rec)
    except BaseException:
        if published:
            final.unlink(missing_ok=True)                # 자기가 발행한 사본만 지운다(미등록 사본 금지)
        raise
    finally:
        tmp.unlink(missing_ok=True)
    return final


def _append_manifest(d: Path, rec: dict) -> None:
    with open(d / MANIFEST_NAME, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _read_manifest(d: Path) -> list:
    p = d / MANIFEST_NAME
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def list_backups(dest_dir) -> list:
    """MANIFEST 에 기록됐고 파일이 남아 있는 사본 목록(오래된 순)."""
    d = Path(dest_dir)
    pruned = {r["name"] for r in _read_manifest(d) if r.get("event") == "pruned"}
    return [r for r in _read_manifest(d) if "event" not in r and r["name"] not in pruned and (d / r["name"]).is_file()]


def prune(dest_dir, names, *, confirm: bool, echo=None) -> dict:
    """이름을 명시한 사본만, confirm=True 일 때만 지운다. confirm 이 아니면 대상 목록만 돌려준다. 자동 삭제는 없다."""
    d = Path(dest_dir)
    names = list(names or [])
    if not names:
        raise ValueError("prune requires explicit backup names")
    known = {r["name"] for r in list_backups(d)}
    for n in names:
        if n not in known or Path(n).name != n:
            raise ValueError(f"not a recorded backup in this folder: {n}")
    stream = echo if echo is not None else sys.stderr
    print(("삭제 대상: " if confirm else "삭제 대상(확인 전, 삭제하지 않음): ") + ", ".join(names), file=stream)
    deleted = []
    if confirm:
        with open(d / MANIFEST_NAME, "a", encoding="utf-8") as f:
            for n in names:
                (d / n).unlink(missing_ok=True)
                deleted.append(n)
                f.write(json.dumps({"event": "pruned", "name": n, "at": _dt.datetime.now().isoformat(timespec="seconds")},
                                   ensure_ascii=False, sort_keys=True) + "\n")
    return {"targets": names, "deleted": deleted, "confirmed": bool(confirm)}

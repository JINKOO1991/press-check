"""번들 색인 설치(설치자용 최소 명령): ``python -m press_review install-bundle [--sha256 HEX]``.

릴리스 위치(``config.BUNDLE_RELEASE_BASE_URL``)의 ``bundle-info.json`` 을 읽고 색인 파일을 내려받아 sha256·크기·형식·색인 구조(필수 표·열, quick_check, 조회 1회)를 검증한 뒤에만
``<data>/index/`` 에 놓고 ``CURRENT`` 를 바꾼다. 내려받기 실패·해시 불일치·형식 오류에서는 임시 파일만 지우고 기존 색인과 ``CURRENT`` 를 건드리지 않는다.

- https 만 쓴다(리다이렉트도 https 만). 요청에는 원고·사용자 정보가 없다(고정 URL 두 개: 판본 정보 JSON, 색인 파일).
- ``--sha256`` 은 릴리스 공지에 적힌 값을 사용자가 따로 넣는 검증이다. 넣지 않으면 판본 정보 JSON 의 해시로 전송 손상만 걸러낸다(같은 서버가 준 값이므로 변조는 못 막는다).
- 오래된 색인은 지우지 않는다(``_prune`` 을 부르지 않는다).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import BinaryIO, Callable

from . import config, corpus_sqlite
from .corpus import CorpusError

MAX_INFO_BYTES = 1 << 20
MAX_BUNDLE_BYTES = 4 << 30
TIMEOUT_SECONDS = 60
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class BundleError(Exception):
    """설치 중단 사유. 메시지는 사용자에게 그대로 보여 줄 수 있고 URL 외의 비밀을 담지 않는다."""


def placeholder_url(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host == "invalid" or host.endswith(".invalid")


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.URLError("https 가 아닌 리다이렉트를 거부했다")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_https(url: str) -> BinaryIO:
    opener = urllib.request.build_opener(_HttpsOnlyRedirect)
    return opener.open(urllib.request.Request(url, headers={"User-Agent": "press-check-bundle-install"}), timeout=TIMEOUT_SECONDS)


def _read_limited(stream: BinaryIO, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise BundleError("판본 정보가 너무 크다")
    return data


def _parse_info(raw: bytes) -> dict:
    try:
        info = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BundleError("판본 정보(bundle-info.json)를 읽을 수 없다") from None
    if not isinstance(info, dict):
        raise BundleError("판본 정보 형식이 잘못됐다")
    name, sha, size = info.get("file"), info.get("sha256"), info.get("size_bytes")
    if not isinstance(name, str) or not corpus_sqlite._INDEX_RE.match(name):
        raise BundleError("판본 정보의 파일 이름이 색인 이름 규칙(corpus-idx-<16자리 16진수>.sqlite)에 맞지 않는다")
    if not isinstance(sha, str) or not _HEX64.match(sha):
        raise BundleError("판본 정보의 sha256 이 잘못됐다")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BUNDLE_BYTES:
        raise BundleError("판본 정보의 크기가 잘못됐다")
    if info.get("index_format") not in corpus_sqlite.READABLE_FORMATS:
        raise BundleError("이 플러그인이 열 수 없는 색인 형식이다(플러그인을 갱신하세요)")
    return info


def _required_columns(fmt: str) -> dict[str, set[str]]:
    """corpus_sqlite 의 색인 스키마(_INDEX_SCHEMA)에서 읽은 표별 필수 열. 형식별로 SqliteCorpus 가 열지 않는 열·표는 뺀다(3번: chunk_kind·passage_alias, 3·4번: ne)."""
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(corpus_sqlite._INDEX_SCHEMA)
        need = {t: {r[1] for r in mem.execute(f"PRAGMA table_info({t})")}
                for (t,) in mem.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        mem.close()
    if fmt == "pdr-index-3":
        need.pop("passage_alias", None)
        need["passages"].discard("chunk_kind")
    if fmt != corpus_sqlite.INDEX_FORMAT:
        need["bigram_terms"].discard("ne")
    return need


def _verify_index(path: Path) -> None:
    """전환 전 검증: 메타 키, 필수 표·열, PRAGMA quick_check, 검색이 쓰는 조회 1회(passages 건수, fts 색인이면 passages_fts 질의). 실패는 BundleError."""
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        raise BundleError("내려받은 색인을 열 수 없다. 설치하지 않았다.") from None
    try:
        try:
            meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
            for k in ("format", "snapshot_id", "fts_trigram"):
                if not isinstance(meta.get(k), str) or not meta[k]:
                    raise BundleError(f"색인 메타에 {k} 가 없다. 설치하지 않았다.")
            if meta["fts_trigram"] not in ("0", "1"):
                raise BundleError("색인 메타의 fts_trigram 값이 잘못됐다. 설치하지 않았다.")
            for table, cols in _required_columns(meta["format"]).items():
                have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                if not have:
                    raise BundleError(f"색인에 {table} 표가 없다. 설치하지 않았다.")
                if cols - have:
                    raise BundleError(f"색인의 {table} 표에 열이 없다({', '.join(sorted(cols - have))}). 설치하지 않았다.")
            qc = db.execute("PRAGMA quick_check").fetchall()
            if [r[0] for r in qc] != ["ok"]:
                raise BundleError("색인 파일의 무결성 검사(quick_check)에 실패했다. 설치하지 않았다.")
            if db.execute("SELECT COUNT(*) FROM passages").fetchone()[0] <= 0:
                raise BundleError("색인에 passage 가 없다. 설치하지 않았다.")
            if meta["fts_trigram"] == "1":
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='passages_fts'").fetchone():
                    raise BundleError("색인에 passages_fts 표가 없다. 설치하지 않았다.")
                if corpus_sqlite.fts_trigram_available():
                    db.execute("SELECT rowid FROM passages_fts WHERE passages_fts MATCH ? LIMIT 1", (corpus_sqlite._fts_phrase("abc"),)).fetchall()
        except sqlite3.Error as e:
            raise BundleError(f"내려받은 색인이 검색 조회를 통과하지 못했다({type(e).__name__}). 설치하지 않았다.") from None
    finally:
        db.close()


def install_bundle(*, data_dir: Path | None = None, base_url: str | None = None, expected_sha256: str | None = None,
                   fetch: Callable[[str], BinaryIO] | None = None) -> dict:
    """번들을 내려받아 검증하고 CURRENT 로 지정한다. 돌려주는 dict 는 건수·경로·해시뿐이다. 실패는 BundleError."""
    base = base_url or config.BUNDLE_RELEASE_BASE_URL
    if placeholder_url(base):
        raise BundleError("번들 릴리스 위치가 아직 정해지지 않았다(config.BUNDLE_RELEASE_BASE_URL 이 자리표시자). 내려받지 않았고 기존 색인은 그대로다.")
    if urllib.parse.urlsplit(base).scheme != "https":
        raise BundleError("번들 릴리스 위치는 https 여야 한다")
    if expected_sha256 is not None and not _HEX64.match(expected_sha256.lower()):
        raise BundleError("--sha256 은 64자리 16진수여야 한다")
    base = base if base.endswith("/") else base + "/"
    fetch = fetch or _open_https
    try:
        with fetch(base + config.BUNDLE_INFO_NAME) as s:
            info = _parse_info(_read_limited(s, MAX_INFO_BYTES))
    except (urllib.error.URLError, OSError) as e:
        raise BundleError(f"판본 정보를 내려받지 못했다({type(e).__name__}). 기존 색인은 그대로다.") from None
    if expected_sha256 is not None and expected_sha256.lower() != info["sha256"]:
        raise BundleError("공지한 sha256 과 판본 정보의 sha256 이 다르다. 설치하지 않았다.")

    od = (Path(data_dir) / corpus_sqlite.INDEX_DIR_NAME) if data_dir is not None else corpus_sqlite.index_dir()
    config.private_mkdir(od)
    if shutil.disk_usage(od).free < info["size_bytes"] + (64 << 20):
        raise BundleError("저장 공간이 부족하다. 기존 색인은 그대로다.")
    tmp = od / f".bundle-download-{os.getpid()}.tmp"
    try:
        h, n = hashlib.sha256(), 0
        try:
            with fetch(base + info["file"]) as s, open(tmp, "wb") as out:
                for chunk in iter(lambda: s.read(1 << 20), b""):
                    n += len(chunk)
                    if n > info["size_bytes"]:
                        raise BundleError("내려받은 크기가 판본 정보보다 크다. 설치하지 않았다.")
                    h.update(chunk)
                    out.write(chunk)
        except (urllib.error.URLError, OSError) as e:
            raise BundleError(f"색인을 내려받지 못했다({type(e).__name__}). 기존 색인은 그대로다.") from None
        if n != info["size_bytes"] or h.hexdigest() != info["sha256"]:
            raise BundleError("내려받은 파일의 크기 또는 sha256 이 판본 정보와 다르다. 설치하지 않았고 기존 색인은 그대로다.")
        try:
            c = corpus_sqlite.SqliteCorpus(tmp)
        except CorpusError as e:
            raise BundleError(f"내려받은 색인을 열 수 없다({e}). 설치하지 않았다.") from None
        try:
            snap = c.snapshot_id
        finally:
            c.close()
        if info["file"] != f"corpus-{snap}.sqlite":
            raise BundleError("색인 안의 snapshot 과 파일 이름이 다르다. 설치하지 않았다.")
        _verify_index(tmp)
        os.chmod(tmp, 0o600)
        target = od / info["file"]
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    corpus_sqlite._write_pointer(od, target.name)
    return {"status": "installed", "snapshot_id": snap, "path": str(target), "sha256": info["sha256"], "size_bytes": n,
            "passage_count": info.get("passage_count")}

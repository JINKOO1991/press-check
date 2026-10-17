"""수집 장부(SQLite, 표준 라이브러리 sqlite3). 스키마는 docs/P1_DESIGN.md '수집 장부' 표를 따른다.

계약:
- 모든 쓰기는 트랜잭션 단위다(``transaction()`` 은 중첩 시 SAVEPOINT).
- 같은 doc_id 재발견(``upsert_document``)은 ``last_seen_at`` 만 갱신한다(멱등). 지우는 API는 없다: 목록에서 사라진 문서도 남는다.
- ``failures`` 는 (target, stage) 당 1행이고 재발생 시 retry_count가 늘어난다. 해결되면 ``resolved_at`` 을 채운다(행은 보존).

P2 확장(스키마 버전 2, ``PRAGMA user_version``): documents.status 에 quarantined 추가(테이블 재작성), 단계별 입력 해시·단계 버전 열,
pages / cases / chunks / structure_jobs / dead_letter / document_list_pages 테이블, api_usage 열 보강. 열기 전 장부는 열 때
한 트랜잭션으로 마이그레이션하며 기존 행은 보존한다(전후 테이블 내용 해시를 비교해 다르면 롤백). 계약: docs/P2_DESIGN.md §2·§4·§5.
공개 질의 헬퍼(``select``, ``list_documents``, ``list_attachments``, ``count_by``, ``find_by_json``, ``lineage_of_chunk``)가 있으므로
어댑터·단계 모듈은 ``_q`` 를 직접 부르지 않는다.

P1_DESIGN 표와 다른 점(워커 보고 대상):
- ``list_pages`` 는 (source_id, url) 이 유일 키다(질의×기간처럼 page_no가 겹치는 목록을 한 source_id에 둘 수 있게).
- ``documents.extra_json``(어댑터 메모: 적중 질의어, relevance 등), ``failures.resolved_at``, ``rights`` 의 (scope_kind, scope_id) 키 추가.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from ..config import private_mkdir
from .. import logsafe
from . import rights as rights_mod
from .context import iso, utcnow

DOC_STATUSES = ("discovered", "fetched", "extracted", "structured", "verified", "indexed", "failed", "withheld", "quarantined")
ATT_STATUSES = DOC_STATUSES
SCHEMA_VERSION = 2
JOB_STATUSES = ("queued", "running", "done", "failed", "dead")
VERIFICATION_STATUSES = ("unverified", "verified", "quarantined", "rejected")
LOCATOR_KINDS = ("pdf_page", "html_section", "text_range")
_JSON_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
ATT_KINDS = ("full", "chapter", "table", "other")
_DOC_ID_RE = re.compile(r"^[a-z][a-z0-9_]*:\S+$")
_ATT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*:\S+#\d+$")
_DOC_FIELDS = ("canonical_url", "title", "publication_date", "decision_date", "year", "doc_type", "detail_url")
_OC_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])(OC=)[^&\s\"'<>\\]*")
MAX_MESSAGE = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  source_id TEXT PRIMARY KEY, source_family TEXT NOT NULL, title TEXT, entry_url TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, checked_at TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS list_pages(
  source_id TEXT NOT NULL, page_no INTEGER NOT NULL, url TEXT NOT NULL, fetched_at TEXT, http_status INTEGER,
  item_count INTEGER, body_sha256 TEXT, ok INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(source_id, url));
CREATE TABLE IF NOT EXISTS documents(
  doc_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, canonical_url TEXT, title TEXT, publication_date TEXT,
  decision_date TEXT, year INTEGER, doc_type TEXT, detail_url TEXT, discovered_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'discovered', extra_json TEXT,
  CHECK(status IN ('discovered','fetched','extracted','structured','verified','indexed','failed','withheld')));
CREATE INDEX IF NOT EXISTS documents_source ON documents(source_id);
CREATE TABLE IF NOT EXISTS attachments(
  att_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, url TEXT NOT NULL, label TEXT,
  kind TEXT NOT NULL DEFAULT 'other' CHECK(kind IN ('full','chapter','table','other')),
  content_length INTEGER, content_type TEXT, head_checked_at TEXT, raw_sha256 TEXT, raw_path TEXT,
  status TEXT NOT NULL DEFAULT 'discovered');
CREATE INDEX IF NOT EXISTS attachments_doc ON attachments(doc_id);
CREATE TABLE IF NOT EXISTS rights(
  scope_kind TEXT NOT NULL CHECK(scope_kind IN ('source','document')), scope_id TEXT NOT NULL,
  read TEXT NOT NULL, store_raw TEXT NOT NULL, create_derivatives TEXT NOT NULL, external_api TEXT NOT NULL,
  redistribute_raw TEXT NOT NULL, redistribute_derivatives TEXT NOT NULL, fine_tune TEXT NOT NULL,
  rights_basis_json TEXT NOT NULL, review_status TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(scope_kind, scope_id));
CREATE TABLE IF NOT EXISTS failures(
  id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, stage TEXT NOT NULL, error_class TEXT NOT NULL,
  message TEXT, at TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0, resolved_at TEXT,
  UNIQUE(target, stage));
CREATE TABLE IF NOT EXISTS robots(
  host TEXT NOT NULL, fetched_at TEXT NOT NULL, http_status INTEGER, body_sha256 TEXT, purpose_status TEXT NOT NULL,
  PRIMARY KEY(host, fetched_at));
CREATE TABLE IF NOT EXISTS api_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, model TEXT NOT NULL, price_snapshot_json TEXT,
  prompt_tokens INTEGER, cache_hit_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL, at TEXT NOT NULL, job_id TEXT);
"""


# 스키마 버전 2에서 새로 생기는 테이블(IF NOT EXISTS 라 열 때마다 실행해도 안전).
P2_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_list_pages(
  doc_id TEXT NOT NULL, source_id TEXT NOT NULL, list_url TEXT NOT NULL, linked_at TEXT NOT NULL,
  PRIMARY KEY(doc_id, list_url));
CREATE INDEX IF NOT EXISTS document_list_pages_list ON document_list_pages(source_id, list_url);
CREATE TABLE IF NOT EXISTS pages(
  page_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, att_id TEXT, pdf_page_no INTEGER, printed_page_no TEXT,
  method TEXT NOT NULL, text_path TEXT, text_sha256 TEXT, char_count INTEGER NOT NULL DEFAULT 0, quality REAL,
  quality_json TEXT, ocr_confidence REAL, needs_review INTEGER NOT NULL DEFAULT 0, extractor_version TEXT,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pages_att ON pages(att_id, pdf_page_no);
CREATE INDEX IF NOT EXISTS pages_doc ON pages(doc_id);
CREATE TABLE IF NOT EXISTS cases(
  case_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, att_id TEXT, first_page_id TEXT, page_first INTEGER, page_last INTEGER,
  page_ids_json TEXT, institution TEXT, case_number TEXT, decision_date TEXT, authority_type TEXT, outcome_type TEXT,
  record_json TEXT NOT NULL, verification_status TEXT NOT NULL DEFAULT 'unverified'
    CHECK(verification_status IN ('unverified','verified','quarantined','rejected')),
  quote_total INTEGER, quote_ok INTEGER, structurer_version TEXT, input_sha256 TEXT, model TEXT, job_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cases_doc ON cases(doc_id);
CREATE INDEX IF NOT EXISTS cases_att ON cases(att_id);
CREATE TABLE IF NOT EXISTS chunks(
  chunk_id TEXT PRIMARY KEY, case_id TEXT, page_id TEXT, doc_id TEXT NOT NULL, text TEXT NOT NULL,
  locator_kind TEXT NOT NULL CHECK(locator_kind IN ('pdf_page','html_section','text_range')), locator_value TEXT NOT NULL,
  authority_type TEXT, decision_date TEXT, text_sha256 TEXT, created_at TEXT NOT NULL,
  CHECK(case_id IS NOT NULL OR page_id IS NOT NULL));
CREATE INDEX IF NOT EXISTS chunks_case ON chunks(case_id);
CREATE INDEX IF NOT EXISTS chunks_page ON chunks(page_id);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE TABLE IF NOT EXISTS structure_jobs(
  job_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, unit TEXT NOT NULL DEFAULT '', input_sha256 TEXT NOT NULL,
  structurer_version TEXT NOT NULL, model TEXT, status TEXT NOT NULL DEFAULT 'queued'
    CHECK(status IN ('queued','running','done','failed','dead')),
  attempts INTEGER NOT NULL DEFAULT 0, est_cost_usd REAL, cost_usd REAL, error_class TEXT, error_message TEXT,
  output_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
  UNIQUE(target_id, unit, input_sha256, structurer_version));
CREATE INDEX IF NOT EXISTS structure_jobs_status ON structure_jobs(status);
CREATE TABLE IF NOT EXISTS dead_letter(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, target_id TEXT NOT NULL, reason TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, response_sha256 TEXT, response_path TEXT, at TEXT NOT NULL, resolved_at TEXT);
CREATE INDEX IF NOT EXISTS dead_letter_job ON dead_letter(job_id);
CREATE TABLE IF NOT EXISTS case_links(
  case_id TEXT NOT NULL, rel TEXT NOT NULL, case_number TEXT NOT NULL, target_doc_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN ('linked','not_in_ledger','date_mismatch')), linked_at TEXT NOT NULL,
  PRIMARY KEY(case_id, rel, target_doc_id));
CREATE INDEX IF NOT EXISTS case_links_target ON case_links(target_doc_id);
"""

_DOCUMENTS_V2 = """
CREATE TABLE documents_v2(
  doc_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, canonical_url TEXT, title TEXT, publication_date TEXT,
  decision_date TEXT, year INTEGER, doc_type TEXT, detail_url TEXT, discovered_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'discovered', extra_json TEXT,
  extractor_version TEXT, extract_input_sha256 TEXT, structurer_version TEXT, structure_input_sha256 TEXT,
  CHECK(status IN ('discovered','fetched','extracted','structured','verified','indexed','failed','withheld','quarantined')))
"""
_V1_COLUMNS = {
    "sources": ("source_id", "source_family", "title", "entry_url", "enabled", "checked_at", "notes"),
    "list_pages": ("source_id", "page_no", "url", "fetched_at", "http_status", "item_count", "body_sha256", "ok"),
    "documents": ("doc_id", "source_id", "canonical_url", "title", "publication_date", "decision_date", "year", "doc_type",
                  "detail_url", "discovered_at", "last_seen_at", "status", "extra_json"),
    "attachments": ("att_id", "doc_id", "url", "label", "kind", "content_length", "content_type", "head_checked_at",
                    "raw_sha256", "raw_path", "status"),
    "rights": ("scope_kind", "scope_id", "read", "store_raw", "create_derivatives", "external_api", "redistribute_raw",
               "redistribute_derivatives", "fine_tune", "rights_basis_json", "review_status", "updated_at"),
    "failures": ("id", "target", "stage", "error_class", "message", "at", "retry_count", "resolved_at"),
    "robots": ("host", "fetched_at", "http_status", "body_sha256", "purpose_status"),
    "api_usage": ("id", "provider", "model", "price_snapshot_json", "prompt_tokens", "cache_hit_tokens",
                  "completion_tokens", "cost_usd", "at", "job_id"),
}
_ADD_COLUMNS = {
    "attachments": (("orig_filename", "TEXT"), ("dedup_of", "TEXT"), ("page_count", "INTEGER"), ("fetched_at", "TEXT"),
                    ("detected_type", "TEXT"), ("extractor_version", "TEXT"), ("extract_input_sha256", "TEXT"),
                    ("structurer_version", "TEXT"), ("structure_input_sha256", "TEXT")),
    "api_usage": (("cache_miss_tokens", "INTEGER"), ("target_id", "TEXT"), ("attempt", "INTEGER"), ("status", "TEXT"),
                  ("error_class", "TEXT"), ("latency_ms", "INTEGER"), ("finish_reason", "TEXT"),
                  ("requested_model", "TEXT")),            # G-D11: 요청한 모델 이름(model 열은 응답이 돌려준 이름)
}


def _table_hash(c: sqlite3.Connection, table: str, cols: tuple, order: str) -> tuple[int, str]:
    """(행 수, 내용 해시). 마이그레이션 전후 원래 열의 내용이 같은지 비교하는 데 쓴다."""
    h = hashlib.sha256()
    n = 0
    for row in c.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}"):
        h.update(repr(tuple(row)).encode("utf-8"))
        n += 1
    return n, h.hexdigest()


def _columns(c: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def _ensure_added_columns(con: sqlite3.Connection) -> None:
    for table, adds in _ADD_COLUMNS.items():
        have = set(_columns(con, table))
        for name, decl in adds:
            if name not in have:
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():       # 다른 프로세스가 같은 순간에 먼저 추가했다(확인과 추가 사이의 경합): 목표 상태이므로 통과
                        raise


def migrate(con: sqlite3.Connection) -> dict:
    """장부를 SCHEMA_VERSION 2로 올린다(멱등). 한 트랜잭션이며 원래 열의 내용 해시가 달라지면 롤백하고 예외를 올린다.

    반환: {'from': 이전 버전, 'to': 현재 버전, 'migrated': bool, 'before': {table: [행수, 해시]}, 'after': ...}.
    """
    con.executescript(SCHEMA)
    con.executescript(P2_SCHEMA)
    _ensure_added_columns(con)                        # 이미 버전 2 인 장부에도 뒤에 늘린 열이 들어가게 한다(멱등)
    if con.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return {"from": SCHEMA_VERSION, "to": SCHEMA_VERSION, "migrated": False}
    con.execute("BEGIN IMMEDIATE")                               # 동시에 연 다른 프로세스가 먼저 끝냈는지는 잠금을 잡은 뒤 다시 본다
    try:
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        if ver >= SCHEMA_VERSION:
            con.execute("ROLLBACK")
            return {"from": ver, "to": ver, "migrated": False}
        order = {"sources": "source_id", "list_pages": "source_id, url", "documents": "doc_id", "attachments": "att_id",
                 "rights": "scope_kind, scope_id", "failures": "id", "robots": "host, fetched_at", "api_usage": "id"}
        before = {t: _table_hash(con, t, cols, order[t]) for t, cols in _V1_COLUMNS.items()}
        # documents: CHECK 에 quarantined 를 넣으려면 재작성이 필요하다(SQLite 는 CHECK 변경을 지원하지 않는다).
        con.execute("DROP TABLE IF EXISTS documents_v2")
        con.execute(_DOCUMENTS_V2)
        cols = ", ".join(_V1_COLUMNS["documents"])
        con.execute(f"INSERT INTO documents_v2({cols}) SELECT {cols} FROM documents ORDER BY doc_id")
        con.execute("DROP TABLE documents")
        con.execute("ALTER TABLE documents_v2 RENAME TO documents")
        con.execute("CREATE INDEX IF NOT EXISTS documents_source ON documents(source_id)")
        con.execute("CREATE INDEX IF NOT EXISTS documents_status ON documents(status)")
        for table, adds in _ADD_COLUMNS.items():
            have = set(_columns(con, table))
            for name, decl in adds:
                if name not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        con.execute("CREATE INDEX IF NOT EXISTS attachments_status ON attachments(status)")
        _backfill_document_list_pages(con)
        after = {t: _table_hash(con, t, cols, order[t]) for t, cols in _V1_COLUMNS.items()}
        if before != after:
            bad = sorted(t for t in before if before[t] != after[t])
            raise RuntimeError(f"manifest migration changed existing rows in {bad}; rolled back")
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return {"from": ver, "to": SCHEMA_VERSION, "migrated": True,
            "before": {t: list(v) for t, v in before.items()}, "after": {t: list(v) for t, v in after.items()}}


def _backfill_document_list_pages(con: sqlite3.Connection) -> int:
    """documents.extra_json.list_page(PAC·IENDC 어댑터가 기록) -> list_pages 행이 (source_id, page_no)로 하나일 때만 연결한다.

    법제처 판례 문서는 질의×기간 목록에서 나와 목록 쪽 번호가 문서에 남지 않았으므로 연결하지 않는다(NULL이 아니라 행 없음).
    """
    n = 0
    now = iso(utcnow())
    cur = con.execute("SELECT doc_id, source_id, json_extract(extra_json,'$.list_page') FROM documents "
                      "WHERE json_extract(extra_json,'$.list_page') IS NOT NULL")
    for doc_id, source_id, page_no in cur.fetchall():
        urls = [r[0] for r in con.execute("SELECT url FROM list_pages WHERE source_id=? AND page_no=?", (source_id, page_no))]
        if len(urls) == 1:
            n += con.execute("INSERT OR IGNORE INTO document_list_pages(doc_id, source_id, list_url, linked_at) VALUES(?,?,?,?)",
                             (doc_id, source_id, urls[0], now)).rowcount
    return n


def _merge_json(cur: Any, new: Any) -> Any:
    """extra_merge 규칙: dict 는 키별 재귀 병합(없는 키는 보존), list 는 합집합, 그 밖은 새 값이 덮어쓴다."""
    if isinstance(cur, dict) and isinstance(new, dict):
        out = dict(cur)
        for k, v in new.items():
            out[k] = _merge_json(cur[k], v) if k in cur else v
        return out
    if isinstance(cur, list) and isinstance(new, list):
        try:
            return sorted(set(cur) | set(new), key=str)
        except TypeError:                                  # dict 등 해시 불가 원소: 순서 유지 중복 제거
            seen, out = set(), []
            for item in [*cur, *new]:
                k = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if k not in seen:
                    seen.add(k)
                    out.append(item)
            return out
    return new


def sanitize_message(msg: Any) -> str:
    """장부·로그용 메시지: OC 파라미터 값 제거(문맥 기준), 비밀·원고 조각이 있으면 전체 치환, 길이 제한.

    평범한 단어인 OC 값이 메시지의 다른 부분(경로·법령 문구)에 있어도 메시지를 지우지 않는다.
    """
    s = logsafe.mask_param_secrets(_OC_RE.sub(r"\1[MASKED]", str(msg)))
    if logsafe.contains_sensitive(s):
        return logsafe.REDACTED
    return s[:MAX_MESSAGE]


class Manifest:
    def __init__(self, path: Path | str, *, now: Callable[[], datetime] = utcnow, timeout: float = 30.0,
                 in_memory_copy: bool = False):
        """in_memory_copy=True(--dry-run): 기존 파일을 메모리로 복사해 쓴다. 쓰기는 사라지고 파일은 건드리지 않는다."""
        self.path = Path(path)
        self._now = now
        self._depth = 0
        if in_memory_copy:
            self._con = sqlite3.connect(":memory:", isolation_level=None)
            if self.path.exists():
                src = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=timeout)
                try:
                    src.backup(self._con)
                finally:
                    src.close()
        else:
            private_mkdir(self.path.parent)
            self._con = sqlite3.connect(str(self.path), timeout=timeout, isolation_level=None)
            self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        self._con.row_factory = sqlite3.Row
        self.migration = migrate(self._con)

    # ---- 트랜잭션 ----
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._depth == 0:
            self._con.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self._con
            except BaseException:
                self._depth = 0
                self._con.execute("ROLLBACK")
                raise
            self._depth = 0
            self._con.execute("COMMIT")
        else:
            name = f"sp{self._depth}"
            self._con.execute(f"SAVEPOINT {name}")
            self._depth += 1
            try:
                yield self._con
            except BaseException:
                self._depth -= 1
                self._con.execute(f"ROLLBACK TO {name}")
                self._con.execute(f"RELEASE {name}")
                raise
            self._depth -= 1
            self._con.execute(f"RELEASE {name}")

    def close(self) -> None:
        self._con.close()

    def _ts(self) -> str:
        return iso(self._now())

    def _q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self._con.execute(sql, params))

    # ---- sources / rights ----
    def ensure_source(self, source_id: str, source_family: str, title: str = "", entry_url: str = "",
                      enabled: bool = True, notes: Optional[str] = None) -> None:
        """등록 또는 갱신(멱등). source 권리 행이 없으면 family 기본값(unknown, naver 는 forbidden)을 기록한다. 기존 행은 건드리지 않는다."""
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO sources(source_id, source_family, title, entry_url, enabled, notes) "
                      "VALUES(?,?,?,?,?,?)", (source_id, source_family, title, entry_url, int(enabled), notes))
            c.execute("UPDATE sources SET source_family=?, title=?, entry_url=?, enabled=?, notes=COALESCE(?, notes) "
                      "WHERE source_id=?", (source_family, title, entry_url, int(enabled), notes, source_id))
            if not self._q("SELECT 1 FROM rights WHERE scope_kind='source' AND scope_id=?", (source_id,)):
                d = rights_mod.default_rights(source_family)
                self._set_rights(c, "source", source_id, d["rights"], d["rights_basis"], d["review_status"])

    def touch_source_checked(self, source_id: str) -> None:
        with self.transaction() as c:
            c.execute("UPDATE sources SET checked_at=? WHERE source_id=?", (self._ts(), source_id))

    def get_source(self, source_id: str) -> Optional[dict]:
        r = self._q("SELECT * FROM sources WHERE source_id=?", (source_id,))
        return dict(r[0]) if r else None

    def set_rights(self, scope_kind: str, scope_id: str, rights: dict, rights_basis: list,
                   review_status: str = "pending") -> None:
        rights_mod.validate_rights(rights)
        with self.transaction() as c:
            self._set_rights(c, scope_kind, scope_id, rights, rights_basis, review_status)

    def _set_rights(self, c: sqlite3.Connection, kind: str, sid: str, rights: dict, basis: list, review: str) -> None:
        ts = self._ts()
        basis = [dict(b, checked_at=b.get("checked_at", ts)) for b in basis]
        rights_mod.validate_rights(rights)
        c.execute(
            "INSERT OR REPLACE INTO rights(scope_kind, scope_id, read, store_raw, create_derivatives, external_api, "
            "redistribute_raw, redistribute_derivatives, fine_tune, rights_basis_json, review_status, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, sid, *[rights[k] for k in rights_mod.RIGHT_KEYS],
             json.dumps(basis, ensure_ascii=False, sort_keys=True), review, ts))

    def get_rights(self, scope_kind: str, scope_id: str) -> Optional[dict]:
        r = self._q("SELECT * FROM rights WHERE scope_kind=? AND scope_id=?", (scope_kind, scope_id))
        if not r:
            return None
        d = dict(r[0])
        d["rights_basis"] = json.loads(d.pop("rights_basis_json"))
        return d

    # ---- list_pages ----
    def upsert_list_page(self, source_id: str, page_no: int, url: str, *, http_status: Optional[int],
                         item_count: Optional[int], body_sha256: Optional[str], ok: bool) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO list_pages(source_id, page_no, url, fetched_at, http_status, item_count, body_sha256, ok) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id, url) DO UPDATE SET page_no=excluded.page_no, "
                "fetched_at=excluded.fetched_at, http_status=excluded.http_status, item_count=excluded.item_count, "
                "body_sha256=excluded.body_sha256, ok=excluded.ok",
                (source_id, page_no, url, self._ts(), http_status, item_count, body_sha256, int(ok)))

    def get_list_page(self, source_id: str, url: str) -> Optional[dict]:
        r = self._q("SELECT * FROM list_pages WHERE source_id=? AND url=?", (source_id, url))
        return dict(r[0]) if r else None

    def list_pages(self, source_id: str) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM list_pages WHERE source_id=? ORDER BY page_no, url", (source_id,))]

    # ---- documents ----
    def upsert_document(self, doc_id: str, source_id: str, *, canonical_url: Optional[str] = None,
                        title: Optional[str] = None, publication_date: Optional[str] = None,
                        decision_date: Optional[str] = None, year: Optional[int] = None,
                        doc_type: Optional[str] = None, detail_url: Optional[str] = None,
                        extra: Optional[dict] = None, list_url: Optional[str] = None) -> str:
        """신규면 'new', 재발견이면 last_seen_at만 갱신하고 'seen'. 재발견 때 다른 열은 바꾸지 않는다.

        list_url 을 주면 문서가 나온 목록 쪽을 document_list_pages 에 연결한다(계보: P2_DESIGN §5, 멱등)."""
        if not _DOC_ID_RE.match(doc_id):
            raise ValueError("doc_id must be '<family>:<key>'")
        ts = self._ts()
        with self.transaction() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO documents(doc_id, source_id, canonical_url, title, publication_date, decision_date, "
                "year, doc_type, detail_url, discovered_at, last_seen_at, status, extra_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'discovered', ?)",
                (doc_id, source_id, canonical_url, title, publication_date, decision_date, year, doc_type, detail_url,
                 ts, ts, json.dumps(extra, ensure_ascii=False, sort_keys=True) if extra else None))
            res = "new" if cur.rowcount == 1 else "seen"
            if res == "seen":
                c.execute("UPDATE documents SET last_seen_at=? WHERE doc_id=?", (ts, doc_id))
            if list_url:
                c.execute("INSERT OR IGNORE INTO document_list_pages(doc_id, source_id, list_url, linked_at) VALUES(?,?,?,?)",
                          (doc_id, source_id, list_url, ts))
            return res

    def update_document_fields(self, doc_id: str, *, extra_merge: Optional[dict] = None, **fields: Any) -> bool:
        """어댑터의 명시적 보강(상세 페이지에서 얻은 값 등). last_seen_at은 건드리지 않는다. extra_merge는 키별 재귀 병합(dict 는 하위 키 보존, 리스트는 합집합)."""
        bad = [k for k in fields if k not in _DOC_FIELDS]
        if bad:
            raise ValueError(f"unknown document fields: {bad}")
        with self.transaction() as c:
            row = self._q("SELECT * FROM documents WHERE doc_id=?", (doc_id,))
            if not row:
                raise KeyError(doc_id)
            sets, vals = [], []
            for k, v in fields.items():
                sets.append(f"{k}=?")
                vals.append(v)
            if extra_merge:
                cur = json.loads(row[0]["extra_json"]) if row[0]["extra_json"] else {}
                cur = _merge_json(cur, extra_merge)
                sets.append("extra_json=?")
                vals.append(json.dumps(cur, ensure_ascii=False, sort_keys=True))
            if not sets:
                return False
            before = tuple(row[0][k] for k in row[0].keys())
            c.execute(f"UPDATE documents SET {', '.join(sets)} WHERE doc_id=?", (*vals, doc_id))
            after = self._q("SELECT * FROM documents WHERE doc_id=?", (doc_id,))[0]
            return before != tuple(after[k] for k in after.keys())

    def set_document_status(self, doc_id: str, status: str) -> None:
        if status not in DOC_STATUSES:
            raise ValueError(f"invalid status '{status}'")
        with self.transaction() as c:
            if c.execute("UPDATE documents SET status=? WHERE doc_id=?", (status, doc_id)).rowcount != 1:
                raise KeyError(doc_id)

    def get_document(self, doc_id: str) -> Optional[dict]:
        r = self._q("SELECT * FROM documents WHERE doc_id=?", (doc_id,))
        return dict(r[0]) if r else None

    def documents_not_seen_since(self, source_id: str, since_iso: str) -> list[str]:
        """last_seen_at이 since_iso보다 오래된 doc_id(목록에서 사라졌을 수 있는 문서). 삭제하지 않는다."""
        return [r["doc_id"] for r in self._q(
            "SELECT doc_id FROM documents WHERE source_id=? AND last_seen_at<? ORDER BY doc_id", (source_id, since_iso))]

    # ---- attachments ----
    def upsert_attachment(self, att_id: str, doc_id: str, url: str, *, label: Optional[str] = None, kind: str = "other",
                          content_length: Optional[int] = None, content_type: Optional[str] = None,
                          head_checked_at: Optional[str] = None) -> str:
        if not _ATT_ID_RE.match(att_id) or not att_id.startswith(doc_id + "#"):
            raise ValueError("att_id must be '<doc_id>#<n>'")
        if kind not in ATT_KINDS:
            raise ValueError(f"invalid kind '{kind}'")
        with self.transaction() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO attachments(att_id, doc_id, url, label, kind, content_length, content_type, "
                "head_checked_at, status) VALUES(?,?,?,?,?,?,?,?, 'discovered')",
                (att_id, doc_id, url, label, kind, content_length, content_type, head_checked_at))
            if cur.rowcount == 1:
                return "new"
            c.execute("UPDATE attachments SET url=?, label=COALESCE(?, label), kind=?, "
                      "content_length=COALESCE(?, content_length), content_type=COALESCE(?, content_type), "
                      "head_checked_at=COALESCE(?, head_checked_at) WHERE att_id=?",
                      (url, label, kind, content_length, content_type, head_checked_at, att_id))
            return "seen"

    def update_attachment_head(self, att_id: str, *, content_length: Optional[int], content_type: Optional[str]) -> None:
        with self.transaction() as c:
            if c.execute("UPDATE attachments SET content_length=?, content_type=?, head_checked_at=? WHERE att_id=?",
                         (content_length, content_type, self._ts(), att_id)).rowcount != 1:
                raise KeyError(att_id)

    def set_attachment_status(self, att_id: str, status: str) -> None:
        if status not in ATT_STATUSES:
            raise ValueError(f"invalid status '{status}'")
        with self.transaction() as c:
            if c.execute("UPDATE attachments SET status=? WHERE att_id=?", (status, att_id)).rowcount != 1:
                raise KeyError(att_id)

    def attachments_for(self, doc_id: str) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM attachments WHERE doc_id=? ORDER BY att_id", (doc_id,))]

    # ---- failures ----
    def record_failure(self, target: str, stage: str, error_class: str, message: Any = "",
                       retry_count: Optional[int] = None) -> None:
        """(target, stage) 당 1행. 다시 실패하면 at·메시지를 갱신하고 retry_count를 올린다(미지정 시 +1). 메시지는 비밀값을 제거한다."""
        msg = sanitize_message(message)
        target = sanitize_message(target) if _OC_RE.search(str(target)) else str(target)
        ts = self._ts()
        with self.transaction() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO failures(target, stage, error_class, message, at, retry_count) VALUES(?,?,?,?,?,?)",
                (target, stage, error_class, msg, ts, retry_count or 0))
            if cur.rowcount == 0:
                c.execute("UPDATE failures SET error_class=?, message=?, at=?, resolved_at=NULL, "
                          "retry_count=COALESCE(?, retry_count+1) WHERE target=? AND stage=?",
                          (error_class, msg, ts, retry_count, target, stage))

    def resolve_failures(self, target: str, stage: Optional[str] = None) -> int:
        with self.transaction() as c:
            if stage is None:
                cur = c.execute("UPDATE failures SET resolved_at=? WHERE target=? AND resolved_at IS NULL",
                                (self._ts(), target))
            else:
                cur = c.execute("UPDATE failures SET resolved_at=? WHERE target=? AND stage=? AND resolved_at IS NULL",
                                (self._ts(), target, stage))
            return cur.rowcount

    def failures(self, *, open_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM failures" + (" WHERE resolved_at IS NULL" if open_only else "") + " ORDER BY id"
        return [dict(r) for r in self._q(sql)]

    # ---- robots / api_usage ----
    def record_robots(self, host: str, fetched_at: str, http_status: Optional[int], body_sha256: Optional[str],
                      purpose_status: str) -> None:
        with self.transaction() as c:
            c.execute("INSERT OR REPLACE INTO robots(host, fetched_at, http_status, body_sha256, purpose_status) "
                      "VALUES(?,?,?,?,?)", (host, fetched_at, http_status, body_sha256, purpose_status))

    def latest_robots(self, host: str) -> Optional[dict]:
        r = self._q("SELECT * FROM robots WHERE host=? ORDER BY fetched_at DESC LIMIT 1", (host,))
        return dict(r[0]) if r else None

    def robots_rows(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM robots ORDER BY host, fetched_at")]

    def record_api_usage(self, provider: str, model: str, *, price_snapshot: Optional[dict] = None,
                         prompt_tokens: int = 0, cache_hit_tokens: int = 0, completion_tokens: int = 0,
                         cost_usd: float = 0.0, job_id: Optional[str] = None, cache_miss_tokens: Optional[int] = None,
                         target_id: Optional[str] = None, attempt: Optional[int] = None, status: Optional[str] = None,
                         error_class: Optional[str] = None, latency_ms: Optional[int] = None,
                         finish_reason: Optional[str] = None, requested_model: Optional[str] = None) -> None:
        """prompt_tokens = 입력 토큰 전체, cache_hit_tokens = 그중 캐시 적중분, cache_miss_tokens = 미적중분(None 이면 전체 - 적중)."""
        if cache_miss_tokens is None:
            cache_miss_tokens = max(0, prompt_tokens - cache_hit_tokens)
        with self.transaction() as c:
            c.execute("INSERT INTO api_usage(provider, model, price_snapshot_json, prompt_tokens, cache_hit_tokens, "
                      "completion_tokens, cost_usd, at, job_id, cache_miss_tokens, target_id, attempt, status, "
                      "error_class, latency_ms, finish_reason, requested_model) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (provider, model, json.dumps(price_snapshot, ensure_ascii=False) if price_snapshot else None,
                       prompt_tokens, cache_hit_tokens, completion_tokens, cost_usd, self._ts(), job_id,
                       cache_miss_tokens, target_id, attempt, status,
                       sanitize_message(error_class) if error_class else None, latency_ms, finish_reason, requested_model))

    def api_usage_summary(self, *, provider: Optional[str] = None) -> dict:
        """모델별 호출 수·토큰·비용 합계와 전체 합계."""
        where, params = ("WHERE provider=?", (provider,)) if provider else ("", ())
        rows = self._q("SELECT model, COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(cache_hit_tokens),0), "
                       "COALESCE(SUM(cache_miss_tokens),0), COALESCE(SUM(completion_tokens),0), COALESCE(SUM(cost_usd),0) "
                       f"FROM api_usage {where} GROUP BY model ORDER BY model", params)
        by_model = {r[0]: {"calls": r[1], "prompt_tokens": r[2], "cache_hit_tokens": r[3], "cache_miss_tokens": r[4],
                           "completion_tokens": r[5], "cost_usd": r[6]} for r in rows}
        total = {k: sum(v[k] for v in by_model.values()) for k in
                 ("calls", "prompt_tokens", "cache_hit_tokens", "cache_miss_tokens", "completion_tokens", "cost_usd")}
        return {"by_model": by_model, "total": total}

    # ---- 집계 ----
    def counts(self) -> dict:
        def n(sql: str) -> int:
            return self._q(sql)[0][0]
        return {
            "sources": n("SELECT COUNT(*) FROM sources"),
            "list_pages": n("SELECT COUNT(*) FROM list_pages"),
            "documents": n("SELECT COUNT(*) FROM documents"),
            "attachments": n("SELECT COUNT(*) FROM attachments"),
            "failures_open": n("SELECT COUNT(*) FROM failures WHERE resolved_at IS NULL"),
            "robots_rows": n("SELECT COUNT(*) FROM robots"),
        }

    def status_summary(self) -> dict:
        out: dict[str, Any] = {"counts": self.counts(), "sources": {}, "robots": {}}
        for s in self._q("SELECT * FROM sources ORDER BY source_id"):
            sid = s["source_id"]
            lp = self._q("SELECT COUNT(*), COALESCE(SUM(ok),0), COALESCE(SUM(item_count),0) FROM list_pages "
                         "WHERE source_id=?", (sid,))[0]
            by_status = {r[0]: r[1] for r in self._q(
                "SELECT status, COUNT(*) FROM documents WHERE source_id=? GROUP BY status", (sid,))}
            att = self._q("SELECT COUNT(*) FROM attachments WHERE doc_id IN (SELECT doc_id FROM documents WHERE source_id=?)",
                          (sid,))[0][0]
            out["sources"][sid] = {
                "family": s["source_family"], "enabled": bool(s["enabled"]), "checked_at": s["checked_at"],
                "list_pages": lp[0], "list_pages_ok": lp[1], "list_item_sum": lp[2],
                "documents": sum(by_status.values()), "documents_by_status": by_status, "attachments": att,
            }
        for host in {r["host"] for r in self._q("SELECT host FROM robots")}:
            r = self.latest_robots(host)
            out["robots"][host] = {k: r[k] for k in ("fetched_at", "http_status", "body_sha256", "purpose_status")}
        return out

    def inventory(self, source_family: str) -> dict:
        """사람이 볼 인벤토리 한 family 분(corpus/inventory/<family>.json). 수치는 전부 장부 질의 결과다."""
        srcs = [dict(r) for r in self._q("SELECT * FROM sources WHERE source_family=? ORDER BY source_id", (source_family,))]
        ids = [s["source_id"] for s in srcs]
        ph = ",".join("?" * len(ids)) or "''"
        def rows(sql: str) -> list[sqlite3.Row]:
            return self._q(sql.replace("{ph}", ph), tuple(ids) * sql.count("{ph}"))
        lp = rows("SELECT COUNT(*), COALESCE(SUM(ok),0), COALESCE(SUM(item_count),0) FROM list_pages "
                  "WHERE source_id IN ({ph})")[0]
        att = rows("SELECT COUNT(*), COALESCE(SUM(content_length),0), "
                   "COALESCE(SUM(CASE WHEN head_checked_at IS NOT NULL THEN 1 ELSE 0 END),0) FROM attachments "
                   "WHERE doc_id IN (SELECT doc_id FROM documents WHERE source_id IN ({ph}))")[0]
        fail = rows("SELECT COUNT(*) FROM failures WHERE resolved_at IS NULL AND "
                    "(target IN (SELECT doc_id FROM documents WHERE source_id IN ({ph})) OR target IN ({ph}))")[0][0]
        return {
            "source_family": source_family,
            "generated_at": self._ts(),
            "sources": [{k: s[k] for k in ("source_id", "title", "entry_url", "enabled", "checked_at")} for s in srcs],
            "list_pages": {"count": lp[0], "ok": lp[1], "item_count_sum": lp[2]},
            "documents": {
                "count": rows("SELECT COUNT(*) FROM documents WHERE source_id IN ({ph})")[0][0],
                "by_status": {r[0]: r[1] for r in rows(
                    "SELECT status, COUNT(*) FROM documents WHERE source_id IN ({ph}) GROUP BY status")},
                "by_doc_type": {str(r[0]): r[1] for r in rows(
                    "SELECT doc_type, COUNT(*) FROM documents WHERE source_id IN ({ph}) GROUP BY doc_type")},
                "by_year": {str(r[0]): r[1] for r in rows(
                    "SELECT year, COUNT(*) FROM documents WHERE source_id IN ({ph}) GROUP BY year ORDER BY year")},
            },
            "attachments": {"count": att[0], "total_bytes_known": att[1], "head_checked": att[2]},
            "failures_open": fail,
            "withheld": rows("SELECT COUNT(*) FROM documents WHERE source_id IN ({ph}) AND status='withheld'")[0][0],
            "rights": {s["source_id"]: self.get_rights("source", s["source_id"]) for s in srcs},
        }

    # ---- 권리 장부·커버리지 질의(읽기 전용) ----
    def sources(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM sources ORDER BY source_id")]

    def documents_of(self, source_id: str) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM documents WHERE source_id=? ORDER BY doc_id", (source_id,))]

    def document_rights_scope_ids(self) -> list[str]:
        """문서 단위 권리 행(상속 재정의)이 있는 doc_id."""
        return [r[0] for r in self._q("SELECT scope_id FROM rights WHERE scope_kind='document' ORDER BY scope_id")]

    def coverage_rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """커버리지 집계용 SELECT 전용 질의. SELECT 이외는 거부한다."""
        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("coverage_rows accepts SELECT only")
        return self._q(sql, params)

    def families(self) -> list[str]:
        return [r[0] for r in self._q("SELECT DISTINCT source_family FROM sources ORDER BY 1")]

    # ================================================================== P2 공개 질의 헬퍼(읽기 전용)
    def select(self, sql: str, params: tuple = ()) -> list[dict]:
        """SELECT(또는 WITH ... SELECT) 전용 질의. 쓰기·PRAGMA·ATTACH 는 SQLite 권한 콜백으로 막는다. 행은 dict 로 돌려준다."""
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

        def _auth(action: int, *_a: Any) -> int:
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        self._con.set_authorizer(_auth)
        try:
            try:
                return [dict(r) for r in self._con.execute(sql, params)]
            except sqlite3.DatabaseError as e:
                if "not authorized" in str(e):
                    raise ValueError("select() accepts read-only SELECT statements only") from None
                raise
        finally:
            self._con.set_authorizer(None)

    def table_columns(self, table: str) -> list[str]:
        return _columns(self._con, self._table(table))

    def _table(self, table: str) -> str:
        names = {r[0] for r in self._q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if table not in names:
            raise ValueError(f"unknown table '{table}'")
        return table

    def _col(self, table: str, col: str) -> str:
        if col not in _columns(self._con, self._table(table)):
            raise ValueError(f"unknown column '{col}' in {table}")
        return col

    def list_documents(self, *, source_id: Optional[str] = None, family: Optional[str] = None,
                       status: Optional[str] = None, statuses: Optional[tuple] = None, year_min: Optional[int] = None,
                       year_max: Optional[int] = None, order_by: str = "doc_id", limit: Optional[int] = None,
                       offset: int = 0) -> list[dict]:
        """문서 목록. order_by 는 documents 열 이름만 받는다."""
        where, params = ["1=1"], []
        if source_id:
            where.append("d.source_id=?"); params.append(source_id)
        if family:
            where.append("d.source_id IN (SELECT source_id FROM sources WHERE source_family=?)"); params.append(family)
        if status:
            where.append("d.status=?"); params.append(status)
        if statuses:
            where.append(f"d.status IN ({','.join('?' * len(statuses))})"); params.extend(statuses)
        if year_min is not None:
            where.append("d.year>=?"); params.append(year_min)
        if year_max is not None:
            where.append("d.year<=?"); params.append(year_max)
        sql = f"SELECT d.* FROM documents d WHERE {' AND '.join(where)} ORDER BY d.{self._col('documents', order_by)}"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"; params.extend((int(limit), int(offset)))
        return self.select(sql, tuple(params))

    def list_attachments(self, *, source_id: Optional[str] = None, family: Optional[str] = None,
                         status: Optional[str] = None, doc_status: Optional[str] = None, kind: Optional[str] = None,
                         doc_id: Optional[str] = None, right: Optional[tuple] = None, order_by: str = "att_id",
                         limit: Optional[int] = None, offset: int = 0) -> list[dict]:
        """첨부 목록(문서 열 source_id·doc_status 포함). right=("store_raw","allowed") 는 유효 권리(문서 행이 있으면 그것, 없으면 소스 행)로 거른다."""
        where, params = ["1=1"], []
        if source_id:
            where.append("d.source_id=?"); params.append(source_id)
        if family:
            where.append("d.source_id IN (SELECT source_id FROM sources WHERE source_family=?)"); params.append(family)
        if status:
            where.append("a.status=?"); params.append(status)
        if doc_status:
            where.append("d.status=?"); params.append(doc_status)
        if kind:
            where.append("a.kind=?"); params.append(kind)
        if doc_id:
            where.append("a.doc_id=?"); params.append(doc_id)
        if right:
            from .rights import RIGHT_KEYS
            key, val = right
            if key not in RIGHT_KEYS:
                raise ValueError(f"unknown right '{key}'")
            where.append(f"COALESCE(rd.{key}, rs.{key})=?"); params.append(val)
        sql = ("SELECT a.*, d.source_id AS source_id, d.status AS doc_status FROM attachments a JOIN documents d ON d.doc_id=a.doc_id "
               "LEFT JOIN rights rd ON rd.scope_kind='document' AND rd.scope_id=d.doc_id "
               "LEFT JOIN rights rs ON rs.scope_kind='source' AND rs.scope_id=d.source_id "
               f"WHERE {' AND '.join(where)} ORDER BY a.{self._col('attachments', order_by)}")
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"; params.extend((int(limit), int(offset)))
        return self.select(sql, tuple(params))

    def count_by(self, table: str, *group_cols: str, where: Optional[dict] = None) -> list[dict]:
        """GROUP BY 집계. 표와 열 이름은 장부 스키마에 있는 것만 받고, where 는 열=값 일치만 지원한다. 행: {열..., 'n': 건수}."""
        t = self._table(table)
        cols = [self._col(t, c) for c in group_cols]
        w, params = [], []
        for k, v in (where or {}).items():
            if v is None:
                w.append(f"{self._col(t, k)} IS NULL")
            else:
                w.append(f"{self._col(t, k)}=?"); params.append(v)
        sel = ", ".join(cols + ["COUNT(*) AS n"])
        sql = f"SELECT {sel} FROM {t}" + (f" WHERE {' AND '.join(w)}" if w else "") + \
              (f" GROUP BY {', '.join(cols)} ORDER BY {', '.join(cols)}" if cols else "")
        return self.select(sql, tuple(params))

    _JSON_COLUMNS = {("documents", "extra_json"), ("cases", "record_json")}

    def find_by_json(self, table: str, json_column: str, field: str, value: Any = None, *, op: str = "=",
                     where: Optional[dict] = None, limit: Optional[int] = None) -> list[dict]:
        """JSON 열의 필드 질의. field 는 점 표기 경로(``a.b``)이며 식별자 규칙을 지킨 것만 받는다(SQL에 값으로만 들어간다).

        op: '=', '!=', '<', '<=', '>', '>=', 'exists'(필드가 null 이 아님). where: 같은 표의 열=값 일치.
        """
        if (table, json_column) not in self._JSON_COLUMNS:
            raise ValueError(f"{table}.{json_column} is not a queryable json column")
        if not _JSON_FIELD_RE.match(field):
            raise ValueError("invalid json field path")
        if op not in ("=", "!=", "<", "<=", ">", ">=", "exists"):
            raise ValueError(f"unsupported op '{op}'")
        expr = f"json_extract({json_column}, ?)"
        params: list = [f"$.{field}"]
        w = []
        if op == "exists":
            w.append(f"{expr} IS NOT NULL")
        else:
            w.append(f"{expr} {op} ?"); params.append(value)
        for k, v in (where or {}).items():
            w.append(f"{self._col(table, k)}=?"); params.append(v)
        sql = f"SELECT * FROM {table} WHERE {' AND '.join(w)} ORDER BY 1"
        if limit is not None:
            sql += " LIMIT ?"; params.append(int(limit))
        return self.select(sql, tuple(params))

    def lineage_of_chunk(self, chunk_id: str) -> Optional[dict]:
        """chunk -> case -> page -> attachment -> document -> list_page 를 SQL 한 번으로 거슬러 올라간다(P2_DESIGN §5).

        case 없는 chunk(연구 자료)는 case_id 가 None. 목록 쪽 연결이 없으면 list_url 이 None. 문서가 여러 목록에 나오면 첫 행.
        """
        rows = self.select(
            "SELECT ch.chunk_id, ch.case_id, ca.first_page_id AS case_first_page_id, pg.page_id, pg.pdf_page_no, pg.att_id, "
            "att.url AS att_url, att.kind AS att_kind, d.doc_id, d.title AS doc_title, d.source_id, "
            "lp.url AS list_url, lp.page_no AS list_page_no "
            "FROM chunks ch LEFT JOIN cases ca ON ca.case_id=ch.case_id "
            "LEFT JOIN pages pg ON pg.page_id=COALESCE(ch.page_id, ca.first_page_id) "
            "LEFT JOIN attachments att ON att.att_id=pg.att_id "
            "LEFT JOIN documents d ON d.doc_id=COALESCE(att.doc_id, pg.doc_id, ch.doc_id) "
            "LEFT JOIN document_list_pages dl ON dl.doc_id=d.doc_id "
            "LEFT JOIN list_pages lp ON lp.source_id=dl.source_id AND lp.url=dl.list_url "
            "WHERE ch.chunk_id=? ORDER BY lp.url IS NULL, lp.url LIMIT 1", (chunk_id,))
        return rows[0] if rows else None

    def p2_counts(self) -> dict:
        def n(sql: str) -> int:
            return self._q(sql)[0][0]
        return {"pages": n("SELECT COUNT(*) FROM pages"), "cases": n("SELECT COUNT(*) FROM cases"),
                "chunks": n("SELECT COUNT(*) FROM chunks"), "structure_jobs": n("SELECT COUNT(*) FROM structure_jobs"),
                "dead_letter_open": n("SELECT COUNT(*) FROM dead_letter WHERE resolved_at IS NULL"),
                "api_usage": n("SELECT COUNT(*) FROM api_usage"), "document_list_pages": n("SELECT COUNT(*) FROM document_list_pages")}

    # ================================================================== P2 쓰기 헬퍼
    _ATT_FIELDS = ("raw_sha256", "raw_path", "orig_filename", "dedup_of", "page_count", "fetched_at", "detected_type",
                   "content_length", "content_type", "extractor_version", "extract_input_sha256", "structurer_version",
                   "structure_input_sha256")
    _DOC_STAGE_FIELDS = ("extractor_version", "extract_input_sha256", "structurer_version", "structure_input_sha256")

    def link_document_list_page(self, doc_id: str, source_id: str, list_url: str) -> bool:
        """문서가 나온 목록 쪽을 연결한다(멱등). 새 연결이면 True."""
        with self.transaction() as c:
            return c.execute("INSERT OR IGNORE INTO document_list_pages(doc_id, source_id, list_url, linked_at) VALUES(?,?,?,?)",
                             (doc_id, source_id, list_url, self._ts())).rowcount == 1

    def update_attachment_fields(self, att_id: str, **fields: Any) -> None:
        bad = [k for k in fields if k not in self._ATT_FIELDS]
        if bad or not fields:
            raise ValueError(f"unknown or empty attachment fields: {bad}")
        with self.transaction() as c:
            sets = ", ".join(f"{k}=?" for k in fields)
            if c.execute(f"UPDATE attachments SET {sets} WHERE att_id=?", (*fields.values(), att_id)).rowcount != 1:
                raise KeyError(att_id)

    def update_document_stage(self, doc_id: str, **fields: Any) -> None:
        bad = [k for k in fields if k not in self._DOC_STAGE_FIELDS]
        if bad or not fields:
            raise ValueError(f"unknown or empty document stage fields: {bad}")
        with self.transaction() as c:
            sets = ", ".join(f"{k}=?" for k in fields)
            if c.execute(f"UPDATE documents SET {sets} WHERE doc_id=?", (*fields.values(), doc_id)).rowcount != 1:
                raise KeyError(doc_id)

    def upsert_page(self, page_id: str, doc_id: str, *, att_id: Optional[str] = None, pdf_page_no: Optional[int] = None,
                    printed_page_no: Optional[str] = None, method: str, text_path: Optional[str] = None,
                    text_sha256: Optional[str] = None, char_count: int = 0, quality: Optional[float] = None,
                    quality_json: Optional[dict] = None, ocr_confidence: Optional[float] = None, needs_review: bool = False,
                    extractor_version: Optional[str] = None) -> None:
        """같은 page_id 는 덮어쓴다(입력 해시가 바뀌어 다시 추출할 때). 파생 case·chunk 는 호출자가 정리한다."""
        if not page_id or not method:
            raise ValueError("page_id and method are required")
        with self.transaction() as c:
            c.execute(
                "INSERT OR REPLACE INTO pages(page_id, doc_id, att_id, pdf_page_no, printed_page_no, method, text_path, "
                "text_sha256, char_count, quality, quality_json, ocr_confidence, needs_review, extractor_version, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (page_id, doc_id, att_id, pdf_page_no, printed_page_no, method, text_path, text_sha256, char_count, quality,
                 json.dumps(quality_json, ensure_ascii=False, sort_keys=True) if quality_json else None, ocr_confidence,
                 int(needs_review), extractor_version, self._ts()))

    def get_page(self, page_id: str) -> Optional[dict]:
        r = self._q("SELECT * FROM pages WHERE page_id=?", (page_id,))
        return dict(r[0]) if r else None

    def pages_of(self, *, att_id: Optional[str] = None, doc_id: Optional[str] = None) -> list[dict]:
        if (att_id is None) == (doc_id is None):
            raise ValueError("give exactly one of att_id, doc_id")
        col, val = ("att_id", att_id) if att_id else ("doc_id", doc_id)
        return [dict(r) for r in self._q(f"SELECT * FROM pages WHERE {col}=? ORDER BY COALESCE(pdf_page_no, 0), page_id", (val,))]

    def upsert_case(self, case_id: str, doc_id: str, record: dict, *, att_id: Optional[str] = None,
                    first_page_id: Optional[str] = None, page_first: Optional[int] = None, page_last: Optional[int] = None,
                    page_ids: Optional[list] = None, verification_status: str = "unverified",
                    quote_total: Optional[int] = None, quote_ok: Optional[int] = None, structurer_version: Optional[str] = None,
                    input_sha256: Optional[str] = None, model: Optional[str] = None, job_id: Optional[str] = None) -> None:
        if verification_status not in VERIFICATION_STATUSES:
            raise ValueError(f"invalid verification_status '{verification_status}'")
        ts = self._ts()
        with self.transaction() as c:
            old = c.execute("SELECT created_at FROM cases WHERE case_id=?", (case_id,)).fetchone()
            c.execute(
                "INSERT OR REPLACE INTO cases(case_id, doc_id, att_id, first_page_id, page_first, page_last, page_ids_json, "
                "institution, case_number, decision_date, authority_type, outcome_type, record_json, verification_status, "
                "quote_total, quote_ok, structurer_version, input_sha256, model, job_id, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, doc_id, att_id, first_page_id, page_first, page_last,
                 json.dumps(page_ids, ensure_ascii=False) if page_ids is not None else None,
                 record.get("institution"), record.get("case_number"), record.get("decision_date"),
                 record.get("authority_type"), record.get("outcome_type"),
                 json.dumps(record, ensure_ascii=False, sort_keys=True), verification_status, quote_total, quote_ok,
                 structurer_version, input_sha256, model, job_id, old[0] if old else ts, ts))

    def replace_case_links(self, case_id: str, rows: list) -> None:
        """사건의 연결 행을 통째로 바꾼다. rows: [{rel, case_number, target_doc_id('' 가능), status}]. 연결은 결정적 규칙의 결과이며 모델 출력이 아니다."""
        ts = self._ts()
        with self.transaction() as c:
            c.execute("DELETE FROM case_links WHERE case_id=?", (case_id,))
            for r in rows:
                c.execute("INSERT OR REPLACE INTO case_links(case_id, rel, case_number, target_doc_id, status, linked_at) VALUES(?,?,?,?,?,?)",
                          (case_id, r["rel"], r["case_number"], r.get("target_doc_id") or "", r["status"], ts))

    def case_links_of(self, case_id: str) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM case_links WHERE case_id=? ORDER BY rel, target_doc_id", (case_id,))]

    def get_case(self, case_id: str) -> Optional[dict]:
        r = self._q("SELECT * FROM cases WHERE case_id=?", (case_id,))
        if not r:
            return None
        d = dict(r[0])
        d["record"] = json.loads(d.pop("record_json"))
        return d

    def set_case_verification(self, case_id: str, status: str) -> None:
        if status not in VERIFICATION_STATUSES:
            raise ValueError(f"invalid verification_status '{status}'")
        with self.transaction() as c:
            if c.execute("UPDATE cases SET verification_status=?, updated_at=? WHERE case_id=?",
                         (status, self._ts(), case_id)).rowcount != 1:
                raise KeyError(case_id)

    def upsert_chunk(self, chunk_id: str, doc_id: str, text: str, locator_kind: str, locator_value: str, *,
                     case_id: Optional[str] = None, page_id: Optional[str] = None, authority_type: Optional[str] = None,
                     decision_date: Optional[str] = None) -> None:
        if locator_kind not in LOCATOR_KINDS:
            raise ValueError(f"invalid locator_kind '{locator_kind}'")
        if case_id is None and page_id is None:
            raise ValueError("chunk needs case_id or page_id")
        with self.transaction() as c:
            c.execute(
                "INSERT OR REPLACE INTO chunks(chunk_id, case_id, page_id, doc_id, text, locator_kind, locator_value, "
                "authority_type, decision_date, text_sha256, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (chunk_id, case_id, page_id, doc_id, text, locator_kind, locator_value, authority_type, decision_date,
                 hashlib.sha256(text.encode("utf-8")).hexdigest(), self._ts()))

    def delete_derived(self, *, att_id: Optional[str] = None, doc_id: Optional[str] = None) -> dict:
        """입력 해시가 바뀌어 단계를 다시 할 때 그 첨부·문서의 파생 행(chunks, cases, pages)을 지운다. 문서·첨부·장부 행은 지우지 않는다."""
        if (att_id is None) == (doc_id is None):
            raise ValueError("give exactly one of att_id, doc_id")
        col, val = ("att_id", att_id) if att_id else ("doc_id", doc_id)
        with self.transaction() as c:
            if att_id:
                n_ch = c.execute("DELETE FROM chunks WHERE case_id IN (SELECT case_id FROM cases WHERE att_id=?) "
                                 "OR page_id IN (SELECT page_id FROM pages WHERE att_id=?)", (val, val)).rowcount
            else:
                n_ch = c.execute("DELETE FROM chunks WHERE doc_id=?", (val,)).rowcount
            c.execute(f"DELETE FROM case_links WHERE case_id IN (SELECT case_id FROM cases WHERE {col}=?)", (val,))
            n_ca = c.execute(f"DELETE FROM cases WHERE {col}=?", (val,)).rowcount
            n_pg = c.execute(f"DELETE FROM pages WHERE {col}=?", (val,)).rowcount
        return {"chunks": n_ch, "cases": n_ca, "pages": n_pg}

    # ---- structure_jobs / dead_letter ----
    def upsert_job(self, job_id: str, target_id: str, input_sha256: str, structurer_version: str, *, unit: str = "",
                   model: Optional[str] = None, est_cost_usd: Optional[float] = None) -> dict:
        """작업 등록(멱등). 같은 (target, unit, 입력 해시, 단계 버전)이 이미 있으면 그 행을 그대로 돌려준다(done 이면 호출자가 건너뛴다)."""
        ts = self._ts()
        key = (target_id, unit, input_sha256, structurer_version)
        with self.transaction() as c:
            r = c.execute("SELECT * FROM structure_jobs WHERE target_id=? AND unit=? AND input_sha256=? AND structurer_version=?",
                          key).fetchone()
            if r is not None:
                return dict(r)
            if c.execute("SELECT 1 FROM structure_jobs WHERE job_id=?", (job_id,)).fetchone():
                raise ValueError(f"job_id '{job_id}' already belongs to a different (target, unit, input, version)")
            c.execute("INSERT INTO structure_jobs(job_id, target_id, unit, input_sha256, structurer_version, model, "
                      "est_cost_usd, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (job_id, *key, model, est_cost_usd, ts, ts))
            return dict(c.execute("SELECT * FROM structure_jobs WHERE job_id=?", (job_id,)).fetchone())

    def update_job(self, job_id: str, *, status: Optional[str] = None, attempts: Optional[int] = None,
                   cost_usd: Optional[float] = None, error_class: Optional[str] = None, error_message: Any = None,
                   output_path: Optional[str] = None, model: Optional[str] = None) -> None:
        if status is not None and status not in JOB_STATUSES:
            raise ValueError(f"invalid job status '{status}'")
        sets, vals = ["updated_at=?"], [self._ts()]
        for col, v in (("status", status), ("attempts", attempts), ("cost_usd", cost_usd), ("error_class", error_class),
                       ("output_path", output_path), ("model", model)):
            if v is not None:
                sets.append(f"{col}=?"); vals.append(v)
        if error_message is not None:
            sets.append("error_message=?"); vals.append(sanitize_message(error_message))
        if status in ("done", "dead"):
            sets.append("finished_at=?"); vals.append(self._ts())
        with self.transaction() as c:
            if c.execute(f"UPDATE structure_jobs SET {', '.join(sets)} WHERE job_id=?", (*vals, job_id)).rowcount != 1:
                raise KeyError(job_id)

    def jobs(self, *, status: Optional[str] = None) -> list[dict]:
        if status:
            return [dict(r) for r in self._q("SELECT * FROM structure_jobs WHERE status=? ORDER BY job_id", (status,))]
        return [dict(r) for r in self._q("SELECT * FROM structure_jobs ORDER BY job_id")]

    def record_dead_letter(self, job_id: str, target_id: str, reason: Any, *, attempts: int = 0,
                           response_sha256: Optional[str] = None, response_path: Optional[str] = None) -> None:
        """재시도를 다 쓴 작업을 격리한다(job 상태도 dead 로 바꾼다). 응답 원문은 장부에 넣지 않고 해시·경로만 둔다."""
        with self.transaction() as c:
            c.execute("INSERT INTO dead_letter(job_id, target_id, reason, attempts, response_sha256, response_path, at) "
                      "VALUES(?,?,?,?,?,?,?)", (job_id, target_id, sanitize_message(reason), attempts, response_sha256,
                                                response_path, self._ts()))
            c.execute("UPDATE structure_jobs SET status='dead', attempts=?, updated_at=?, finished_at=? WHERE job_id=?",
                      (attempts, self._ts(), self._ts(), job_id))

    def dead_letters(self, *, open_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM dead_letter" + (" WHERE resolved_at IS NULL" if open_only else "") + " ORDER BY id"
        return [dict(r) for r in self._q(sql)]

    def resolve_dead_letter(self, dead_id: int) -> None:
        with self.transaction() as c:
            c.execute("UPDATE dead_letter SET resolved_at=? WHERE id=? AND resolved_at IS NULL", (self._ts(), dead_id))

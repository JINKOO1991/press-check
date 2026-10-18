"""raw 외부 보관 표시(attachments.raw_archived_to/raw_archived_at): 표시가 있으면 파일이 없어도 재수집·상태 강등을 하지 않는다.

실제 Manifest 클래스로 임시 corpus 에 장부를 만든다(mock 장부 없음). 네트워크 없음.
"""
import hashlib
import io
import json
import sqlite3

import pytest

from conftest import FakeClock
from ext_helpers import FAMILY, FakeRunner, SOURCE, make_pdf, page_text, place_raw, seed
from press_review.ingestion import __main__ as cli
from press_review.ingestion import extract as ext
from press_review.ingestion import fetch as fetch_mod
from press_review.ingestion.extract.tools import ToolResult
from press_review.ingestion.manifest import Manifest

U = "https://www.pac.or.kr/files/%s.pdf"
MARK = "ARCHIVE_ROOT\\pac_cases\\x.pdf"


@pytest.fixture()
def env(corpus, clock, monkeypatch):
    class Env:
        pass

    e = Env()
    e.corpus, e.clock, e.mp = corpus, clock, monkeypatch
    e.m = Manifest(corpus / "manifest.sqlite", now=clock.now)

    def run(runner, *argv):
        e.m.close()
        monkeypatch.setattr(ext, "ToolRunner", lambda: runner)
        out = io.StringIO()
        code = cli.main(["extract", *argv], clock=clock, sleep=clock.sleep, stdout=out, corpus=corpus, log_stream=io.StringIO())
        e.m = Manifest(corpus / "manifest.sqlite", now=clock.now)
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    e.run = run
    yield e
    e.m.close()


def row(m, aid):
    return m.select("SELECT * FROM attachments WHERE att_id=?", (aid,))[0]


def mark(m, aid, value=MARK):
    m.update_attachment_fields(aid, raw_archived_to=value, raw_archived_at="2026-10-18T00:00:00+00:00")


def drop_file(env, aid):
    (env.corpus / row(env.m, aid)["raw_path"]).unlink()


def extracted_pdf(env, tag="A", n=3):
    doc, (a1,) = seed(env.m, atts=[(1, U % tag, "full", None, "application/pdf", "PDF")])
    blob = make_pdf([f"{tag} page {i + 1}" for i in range(n)])
    spec = {"pages": [page_text(i + 1, i + 1, tag) for i in range(n)]}
    place_raw(env.m, env.corpus, a1, blob)
    r = FakeRunner({blob: spec})
    _, res = env.run(r)
    assert res["result"]["outcomes"] == {"extracted": 1}
    return doc, a1, r


# ------------------------------------------------------------------ 작업 2: 새 열이 실제 코드 경로에 닿는가
def test_columns_added_to_old_schema_are_idempotent_and_keep_rows(corpus, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    doc, (a1,) = seed(m, atts=[(1, U % "A", "full", None, "application/pdf", "PDF")])
    place_raw(m, corpus, a1, make_pdf(["a"]))
    m.close()
    con = sqlite3.connect(corpus / "manifest.sqlite")
    con.execute("ALTER TABLE attachments DROP COLUMN raw_archived_to")       # 구 스키마 흉내: 열 없음
    con.execute("ALTER TABLE attachments DROP COLUMN raw_archived_at")
    con.commit()
    before = con.execute("SELECT att_id, status, raw_path, raw_sha256 FROM attachments ORDER BY att_id").fetchall()
    cols = {r[1] for r in con.execute("PRAGMA table_info(attachments)")}
    assert "raw_archived_to" not in cols
    con.close()
    for _ in range(2):                                                         # 두 번 열어도 오류 없음
        m = Manifest(corpus / "manifest.sqlite", now=clock.now)
        r = m.list_attachments()[0]
        assert isinstance(r, dict) and "raw_archived_to" in r and r["raw_archived_to"] is None   # 작업 2 a·b
        assert [(x["att_id"], x["status"], x["raw_path"], x["raw_sha256"]) for x in m.list_attachments()] == before
        assert isinstance(m.attachments_for(doc)[0], dict) and "raw_archived_at" in m.attachments_for(doc)[0]
        m.close()


def test_dry_run_copy_of_old_schema_migrates_in_memory_only(corpus, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    seed(m, atts=[(1, U % "A", "full", None, "application/pdf", "PDF")])
    m.close()
    con = sqlite3.connect(corpus / "manifest.sqlite")
    con.execute("ALTER TABLE attachments DROP COLUMN raw_archived_to")
    con.execute("ALTER TABLE attachments DROP COLUMN raw_archived_at")
    con.commit(); con.close()
    h = hashlib.sha256((corpus / "manifest.sqlite").read_bytes()).hexdigest()
    mm = Manifest(corpus / "manifest.sqlite", in_memory_copy=True)
    assert "raw_archived_to" in mm.list_attachments()[0]
    mm.close()
    assert hashlib.sha256((corpus / "manifest.sqlite").read_bytes()).hexdigest() == h      # 파일은 그대로


def test_update_attachment_fields_accepts_the_new_columns(corpus, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    doc, (a1,) = seed(m, atts=[(1, U % "A", "full", None, None, "PDF")])
    mark(m, a1)
    assert row(m, a1)["raw_archived_to"] == MARK and row(m, a1)["raw_archived_at"].startswith("2026-10-18")
    m.update_attachment_fields(a1, raw_archived_to=None, raw_archived_at=None)
    assert row(m, a1)["raw_archived_to"] is None
    m.close()


# ------------------------------------------------------------------ A2~A4: fetch 후보 선택
def select(m, corpus):
    return fetch_mod.select_candidates(m, corpus, source=SOURCE)


def one(m, tag="A", status="fetched"):
    doc, (a1,) = seed(m, atts=[(1, U % tag, "full", None, "application/pdf", "PDF")])
    blob = make_pdf([f"{tag} text"])
    rel = place_raw(m, m_corpus(m), a1, blob)
    m.set_attachment_status(a1, status)
    return a1, rel


def m_corpus(m):
    return m.path.parent


def test_a2_fetched_missing_file_archived_is_not_refetched(corpus, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    a1, rel = one(m)
    (corpus / rel).unlink()
    cands, info = select(m, corpus)
    assert [c["att_id"] for c in cands] == [a1] and info["skipped_archived"] == 0      # 표시 없음: 기존대로 다시 받는다(회귀 없음)
    mark(m, a1)
    cands, info = select(m, corpus)
    assert cands == [] and info["skipped_archived"] == 1
    m.close()


@pytest.mark.parametrize("status", ["failed", "fetched", "extracted", "structured"])
def test_a3_a4_marked_and_file_present_behaves_like_unmarked(corpus, clock, status):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    a1, rel = one(m, status=status)
    base = [c["att_id"] for c in select(m, corpus)[0]]
    mark(m, a1)
    assert [c["att_id"] for c in select(m, corpus)[0]] == base                  # 파일이 있으면 표시와 무관(복원된 경우)
    m.close()


def test_a3_failed_missing_file_archived_is_skipped(corpus, clock):
    m = Manifest(corpus / "manifest.sqlite", now=clock.now)
    a1, rel = one(m, status="failed")
    (corpus / rel).unlink()
    assert [c["att_id"] for c in select(m, corpus)[0]] == [a1]                  # 표시 없음: 재시도 대상
    mark(m, a1)
    cands, info = select(m, corpus)
    assert cands == [] and info["skipped_archived"] == 1
    m.close()


# ------------------------------------------------------------------ A5·A6·A7: extract 강등 경로
def test_a5_extracted_archived_version_bump_keeps_status_without_raw_missing(env):
    doc, a1, r = extracted_pdf(env)
    drop_file(env, a1)
    mark(env.m, a1)
    env.mp.setattr(ext, "EXTRACTOR_VERSION", ext.EXTRACTOR_VERSION + "-next")
    _, res = env.run(r)
    assert res["result"]["outcomes"] == {"skipped_archived": 1}
    assert row(env.m, a1)["status"] == "extracted"
    assert env.m.failures(open_only=True) == []                                # raw_missing 이 기록되지 않았다
    assert len(env.m.pages_of(att_id=a1)) == 3                                  # 파생 행도 그대로


def test_a5_control_unmarked_version_bump_demotes_to_discovered(env):
    doc, a1, r = extracted_pdf(env)
    drop_file(env, a1)
    env.mp.setattr(ext, "EXTRACTOR_VERSION", ext.EXTRACTOR_VERSION + "-next")
    _, res = env.run(r)
    assert res["result"]["errors"] == {"raw_missing": 1}
    assert row(env.m, a1)["status"] == "discovered"                             # 위험이 실제로 있었다는 대조군


def test_a5_structured_archived_extractor_change_keeps_status(env):
    doc, a1, r = extracted_pdf(env)
    env.m.set_attachment_status(a1, "structured")
    drop_file(env, a1)
    mark(env.m, a1)
    _, res = env.run(r, "--extractor", "pdftotext_raw")                         # 입력 서명이 바뀌어 같은 입력 건너뛰기를 타지 않는다
    assert res["result"]["outcomes"] == {"skipped_archived": 1}
    assert row(env.m, a1)["status"] == "structured" and env.m.failures(open_only=True) == []


def test_a5_control_structured_unmarked_extractor_change_demotes(env):
    doc, a1, r = extracted_pdf(env)
    env.m.set_attachment_status(a1, "structured")
    drop_file(env, a1)
    _, res = env.run(r, "--extractor", "pdftotext_raw")
    assert res["result"]["errors"] == {"raw_missing": 1} and row(env.m, a1)["status"] == "discovered"


def test_a6_verified_force_archived_is_not_demoted(env):
    doc, a1, r = extracted_pdf(env)
    env.m.set_attachment_status(a1, "verified")
    drop_file(env, a1)
    mark(env.m, a1)
    _, res = env.run(r, "--force", "--extractor", "pdftotext_raw")
    assert res["result"]["outcomes"] == {"skipped_archived": 1}
    assert row(env.m, a1)["status"] == "verified" and env.m.failures(open_only=True) == []


def test_a6_control_verified_force_unmarked_is_demoted(env):
    doc, a1, r = extracted_pdf(env)
    env.m.set_attachment_status(a1, "verified")
    drop_file(env, a1)
    _, res = env.run(r, "--force", "--extractor", "pdftotext_raw")
    assert row(env.m, a1)["status"] == "discovered"                             # 검수 완료 첨부도 강등된다는 것을 확인


def test_a7_limit_does_not_count_skipped_archived(env):
    doc, a1, r = extracted_pdf(env, "A")
    doc2, (a2,) = seed(env.m, "200", atts=[(1, U % "B", "full", None, "application/pdf", "PDF")])
    blob = make_pdf(["B page 1"])
    place_raw(env.m, env.corpus, a2, blob)
    r2 = FakeRunner({blob: {"pages": [page_text(1, 1, "B")]}, **r.docs})
    env.run(r2)                                                                 # a2 도 추출
    for aid in (a1, a2):
        drop_file(env, aid)
        mark(env.m, aid)
    env.mp.setattr(ext, "EXTRACTOR_VERSION", ext.EXTRACTOR_VERSION + "-next")
    _, res = env.run(r2, "--limit", "1")
    assert res["result"]["outcomes"] == {"skipped_archived": 2} and res["result"]["attempted"] == 0   # --limit 1 인데 둘 다 훑는다


def test_restored_file_with_mark_still_extracts_normally(env):
    doc, a1, r = extracted_pdf(env)
    mark(env.m, a1)                                                             # 파일이 있다: 가드에 걸리지 않는다
    env.mp.setattr(ext, "EXTRACTOR_VERSION", ext.EXTRACTOR_VERSION + "-next")
    _, res = env.run(r)
    assert res["result"]["outcomes"] == {"extracted": 1}


# ------------------------------------------------------------------ A8: 전체 PDF + 장별 PDF(보충 모드)
def chapter_doc(env):
    doc, ids = seed(env.m, atts=[(0, U % "full", "full", None, "application/pdf", "PDF"),
                                 (10, U % "c0", "chapter", None, "application/pdf", "PDF"),
                                 (11, U % "c1", "chapter", None, "application/pdf", "PDF")])
    full_texts = [page_text(i, i, "FULL") for i in range(1, 7)]
    full = make_pdf([f"full {i}" for i in range(1, 7)])
    docs = {full: {"pages": full_texts}}
    place_raw(env.m, env.corpus, ids[0], full)
    for k, (a, b) in enumerate(((2, 3), (5, 6))):
        blob = make_pdf([f"chapter {k} page {i}" for i in range(a, b + 1)])
        docs[blob] = {"pages": full_texts[a - 1:b]}
        place_raw(env.m, env.corpus, ids[1 + k], blob)
    return doc, ids, docs


class MissingAwareRunner(FakeRunner):
    """실제 pdfinfo/pdftotext 처럼 없는 파일을 열면 비정상 종료로 돌려준다."""

    def run(self, argv, *, timeout):
        import os
        if any(a.endswith(".pdf") and not os.path.exists(a) for a in argv[1:]):
            self.calls.append(list(argv))
            return ToolResult(1, b"", b"I/O Error: Couldn't open file")
        return super().run(argv, timeout=timeout)


def test_a8_archived_chapter_pdf_does_not_break_or_wipe_full_pdf_rows(env):
    doc, ids, docs = chapter_doc(env)
    full, c1, c2 = ids
    r = MissingAwareRunner(docs)
    _, res = env.run(r)
    assert res["result"]["outcomes"] == {"extracted": 3}
    full_pages = [p["pdf_page_no"] for p in env.m.pages_of(att_id=full)]
    assert full_pages == [1, 4]
    drop_file(env, c1)
    mark(env.m, c1)
    _, res = env.run(r, "--extractor", "pdftotext_raw")                         # 입력 서명이 바뀌어 전체 PDF 를 다시 추출하려 한다
    out = res["result"]["outcomes"]
    assert out.get("failed") is None and out.get("skipped_archived", 0) >= 1, out
    assert row(env.m, full)["status"] == "extracted"
    assert [p["pdf_page_no"] for p in env.m.pages_of(att_id=full)] == full_pages    # 파생 행이 지워지지 않았다
    assert env.m.failures(open_only=True) == []


def test_a8_without_the_guard_the_full_pdf_rows_are_wiped(env):
    """가드(ext._local_file 판정)를 끄면 실제로 어떻게 되는지: 재현으로 위험을 확인한다."""
    doc, ids, docs = chapter_doc(env)
    full, c1, c2 = ids
    r = MissingAwareRunner(docs)
    env.run(r)
    drop_file(env, c1)
    mark(env.m, c1)
    env.mp.setattr(ext, "_local_file", lambda ctx, att: True)                   # 가드 무력화
    _, res = env.run(r, "--extractor", "pdftotext_raw")
    assert res["result"]["errors"] == {"pdfinfo_failed": 1}
    assert row(env.m, full)["status"] == "failed"                               # 전체 PDF 가 failed 로 바뀌고
    assert env.m.pages_of(att_id=full) == []                                    # 파생 쪽 행이 이미 지워졌다(재현된 위험)

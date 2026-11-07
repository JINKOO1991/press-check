"""번들: 만들기(devtools/engine/press_review/bundle_build.py)와 설치(plugins/press-check/engine/press_review/bundle_install.py). 합성 색인만 쓴다."""
import datetime as dt
import hashlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: E402

from press_review import bundle_build, bundle_install, config, corpus_sqlite as cs  # noqa: E402
from press_review.ingestion import admission  # noqa: E402
from press_review.ingestion.manifest import Manifest  # noqa: E402

NOW = dt.datetime(2026, 11, 7, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
ALLOWED = {"read": "allowed", "store_raw": "allowed", "create_derivatives": "allowed", "external_api": "allowed",
           "redistribute_raw": "allowed", "redistribute_derivatives": "allowed", "fine_tune": "permission_required"}
BASIS = [{"scope": "합성 시험: 소스 단위 재배포 허락", "review_status": "verified", "url": "https://www.law.go.kr/"}]
EXCLUDED_DOC = "lawgo_prec:900002"


@pytest.fixture()
def active(tmp_path):
    """판례 소스 전체를 재배포 허락으로 만든 합성 장부의 활성 색인(CURRENT 포함)."""
    data = tmp_path / "data"
    ledger = sl.build_ledger(data / "corpus")
    m = Manifest(ledger)
    m.set_rights("source", "lawgo_prec_supreme", ALLOWED, BASIS, "verified")
    m.close()
    built = cs.build_index(ledger, data / "index", now=NOW, pending_fn=admission.case_admission_pending)
    return data / "index", built


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _rows(path: Path, sql: str):
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_build_index_without_pending_fn_leaves_pending_unmeasured(tmp_path):
    ledger = sl.build_ledger(tmp_path / "c")
    res = cs.build_index(ledger, tmp_path / "idx", now=NOW)
    assert res["case_admission_pending"] is None and "warnings" not in res
    st = cs.open_current(tmp_path / "idx").status()
    assert "case_admission_pending" not in st


def test_bundle_keeps_only_redistributable_and_drops_excluded_docs(active, tmp_path):
    index_dir, built = active
    cur_before, snap_file = (index_dir / "CURRENT").read_bytes(), Path(built["path"])
    src_hash = _sha(snap_file)
    out = tmp_path / "bundle"
    res = bundle_build.build_bundle(index_dir, out, exclude_docs=[EXCLUDED_DOC, "lawgo_prec:none"], now=NOW)
    # 입력과 CURRENT 는 그대로
    assert (index_dir / "CURRENT").read_bytes() == cur_before and _sha(snap_file) == src_hash
    assert sorted(p.name for p in out.iterdir()) == sorted([res["file"], res["file"] + ".sha256", "bundle-info.json"])
    b = out / res["file"]
    assert res["sha256"] == _sha(b) and (out / (res["file"] + ".sha256")).read_text().split()[0] == res["sha256"]
    assert json.loads((out / "bundle-info.json").read_text())["sha256"] == res["sha256"]
    ids = {r[0] for r in _rows(b, "SELECT source_id FROM passages")}
    assert ids == {"lawgo_prec:900001", "lawgo_prec:900003", "lawgo_prec:900004"}          # 재배포 불가(위원회 계열)와 제외 문서가 없다
    assert all(r[0] == 1 for r in _rows(b, "SELECT redistributable FROM passages"))
    assert res["excluded_documents_listed"] == 2 and res["excluded_documents_matched"] == 1 and res["unmatched_exclusions"] == ["lawgo_prec:none"]
    assert res["dropped_passages_of_excluded_documents"] == 1 and res["passage_count"] == 3
    # 보조 표와 FTS 에 지운 rid 가 남지 않는다
    for t in ("passage_risk", "case_numbers", "cites", "bigram_terms"):
        assert _rows(b, f"SELECT COUNT(*) FROM {t} WHERE rid NOT IN (SELECT rid FROM passages)")[0][0] == 0
    assert _rows(b, "SELECT COUNT(*) FROM passages_fts")[0][0] == 3
    # 제외한 본문이 파일 어디에도 남지 않는다(빈 페이지·FTS 조각 포함)
    assert "2021다20002".encode() not in b.read_bytes() and EXCLUDED_DOC.encode() not in b.read_bytes()
    # 설치자가 여는 색인으로 동작하고 통계는 담은 행으로 센다
    c = cs.SqliteCorpus(b)
    st = c.status()
    assert st["passage_count"] == 3 and st["snapshot_id"] == res["snapshot_id"] and st["is_fixture"] is False
    assert st["by_source_family"] == {"lawgo_prec_supreme": 3} and st["sources"]["lawgo_prec_supreme"]["redistribute_raw"] == "allowed"
    assert st["cases_by_authority_type"] == {"judgment": 3}
    hits = c.search("정정보도 요건")
    assert hits and all(p["redistributable"] and not p["display_truncated"] for p in hits)        # 번들 passage 는 화면 인용 상한이 없다
    c.close()


def test_bundle_is_deterministic_and_aliases_follow_kept_passages(active, tmp_path):
    index_dir, _ = active
    a = bundle_build.build_bundle(index_dir, tmp_path / "a", now=NOW)
    b = bundle_build.build_bundle(index_dir, tmp_path / "b", exclude_docs=[], now=NOW)
    assert a["snapshot_id"] == b["snapshot_id"] and a["sha256"] == b["sha256"]
    c = bundle_build.build_bundle(index_dir, tmp_path / "c", exclude_docs=[EXCLUDED_DOC], now=NOW)
    assert c["snapshot_id"] != a["snapshot_id"]
    assert _rows(tmp_path / "c" / c["file"], "SELECT COUNT(*) FROM passage_alias WHERE passage_id NOT IN (SELECT passage_id FROM passages)")[0][0] == 0


def test_bundle_build_refuses_index_folder_and_empty_result(active, tmp_path):
    index_dir, _ = active
    with pytest.raises(bundle_build.BundleBuildError):
        bundle_build.build_bundle(index_dir, index_dir, now=NOW)                         # 운영 색인 폴더에는 쓰지 않는다
    all_docs = [f"lawgo_prec:90000{i}" for i in range(1, 5)]
    with pytest.raises(bundle_build.BundleBuildError):
        bundle_build.build_bundle(index_dir, tmp_path / "none", exclude_docs=all_docs, now=NOW)
    assert not list((tmp_path / "none").glob("*.tmp")) and not list((tmp_path / "none").glob("corpus-*"))


def test_read_exclusions_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "ex.txt"
    f.write_text("# 실명 검수 결과\n\nlawgo_prec:1\n  lawgo_prec:2  \nlawgo_prec:1\n", encoding="utf-8")
    assert bundle_build.read_exclusions(f) == ["lawgo_prec:1", "lawgo_prec:2"]
    assert bundle_build.read_exclusions(None) == []


# ---------------------------------------------------------------- 설치

BASE = "https://example.test/press-check-bundle/v1/"


@pytest.fixture()
def release(active, tmp_path):
    index_dir, _ = active
    out = tmp_path / "release"
    bundle_build.build_bundle(index_dir, out, now=NOW)
    files = {BASE + p.name: p.read_bytes() for p in out.iterdir()}
    return files


def _fetcher(files, calls=None):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        if url not in files:
            import urllib.error
            raise urllib.error.URLError("not found")
        return io.BytesIO(files[url])
    return fetch


def test_install_places_verified_bundle_and_sets_current(release, tmp_path):
    data = tmp_path / "user"
    info = json.loads(release[BASE + "bundle-info.json"])
    res = bundle_install.install_bundle(data_dir=data, base_url=BASE, expected_sha256=info["sha256"].upper(), fetch=_fetcher(release))
    od = data / "index"
    assert (od / "CURRENT").read_text().strip() == info["file"] and res["sha256"] == info["sha256"]
    c = cs.open_current(od)
    assert c.snapshot_id == info["snapshot_id"] and c.status()["passage_count"] == 4
    c.close()
    assert not [p.name for p in od.iterdir() if p.name.endswith(".tmp")]


def test_install_failures_leave_existing_index_untouched(release, active, tmp_path):
    index_dir, built = active                                  # 기존 색인(CURRENT 있음)을 사용자 데이터 폴더로 취급한다
    cur, target = (index_dir / "CURRENT").read_bytes(), Path(built["path"])
    snap_before = sorted(p.name for p in index_dir.iterdir())
    data = index_dir.parent
    info_url, file_url = BASE + "bundle-info.json", BASE + json.loads(release[BASE + "bundle-info.json"])["file"]

    def run(files, **kw):
        with pytest.raises(bundle_install.BundleError) as ei:
            bundle_install.install_bundle(data_dir=data, base_url=BASE, fetch=_fetcher(files), **kw)
        assert (index_dir / "CURRENT").read_bytes() == cur and sorted(p.name for p in index_dir.iterdir()) == snap_before
        return str(ei.value)

    corrupt = dict(release)
    corrupt[file_url] = bytearray(release[file_url])
    corrupt[file_url][len(corrupt[file_url]) // 2] ^= 0xFF
    corrupt[file_url] = bytes(corrupt[file_url])
    assert "sha256" in run(corrupt)                                                      # 해시 불일치
    assert "내려받지" in run({info_url: release[info_url]})                              # 색인 파일 다운로드 실패
    assert "내려받지" in run({})                                                          # 판본 정보 다운로드 실패
    assert "공지한 sha256" in run(release, expected_sha256="0" * 64)                      # 공지 해시와 다름
    bad = dict(release)
    bad[info_url] = json.dumps({**json.loads(release[info_url]), "file": "../evil.sqlite"}).encode()
    assert "이름 규칙" in run(bad)                                                        # 경로 이탈 이름
    short = dict(release)
    short[file_url] = release[file_url][:-10]
    assert "크기" in run(short)
    notindex = dict(release)
    junk = b"not a sqlite file" * 10
    notindex[file_url] = junk
    notindex[info_url] = json.dumps({**json.loads(release[info_url]), "sha256": hashlib.sha256(junk).hexdigest(), "size_bytes": len(junk)}).encode()
    assert "열 수 없다" in run(notindex)                                                  # 해시는 맞지만 색인이 아님
    assert target.exists()


def test_install_refuses_placeholder_and_non_https(tmp_path):
    assert bundle_install.placeholder_url(config.BUNDLE_RELEASE_BASE_URL)                 # 지금은 자리표시자
    calls: list[str] = []
    with pytest.raises(bundle_install.BundleError, match="정해지지 않았다"):
        bundle_install.install_bundle(data_dir=tmp_path / "d", fetch=_fetcher({}, calls))
    with pytest.raises(bundle_install.BundleError, match="https"):
        bundle_install.install_bundle(data_dir=tmp_path / "d", base_url="http://example.test/x/", fetch=_fetcher({}, calls))
    assert calls == [] and not (tmp_path / "d").exists()                                  # 내려받기 시도도, 폴더 생성도 없다


def test_cli_install_bundle_reports_placeholder(tmp_path, monkeypatch, capsys):
    from press_review import cli
    monkeypatch.setenv("PRESS_DISPUTE_DATA_DIR", str(tmp_path / "d"))
    assert cli.main(["install-bundle"]) == 1
    assert "정해지지 않았다" in capsys.readouterr().err


def test_install_refuses_index_without_required_tables_and_keeps_current(release, active, tmp_path):
    index_dir, _ = active
    cur, before = (index_dir / "CURRENT").read_bytes(), sorted(p.name for p in index_dir.iterdir())
    info = json.loads(release[BASE + "bundle-info.json"])
    hollow = tmp_path / "hollow.sqlite"                                  # meta 만 있고 해시·크기·형식·snapshot 은 맞는 파일
    c = sqlite3.connect(hollow)
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.executemany("INSERT INTO meta VALUES(?,?)", [("format", cs.INDEX_FORMAT), ("snapshot_id", info["snapshot_id"]), ("fts_trigram", "0")])
    c.commit()
    c.close()
    raw = hollow.read_bytes()
    files = {BASE + "bundle-info.json": json.dumps({**info, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}).encode(), BASE + info["file"]: raw}
    with pytest.raises(bundle_install.BundleError, match="passages"):
        bundle_install.install_bundle(data_dir=index_dir.parent, base_url=BASE, fetch=_fetcher(files))
    assert (index_dir / "CURRENT").read_bytes() == cur and sorted(p.name for p in index_dir.iterdir()) == before

"""UDEC-36: 공개 장부의 PAC 소스는 external_api 가 permission_required 이고, 그 값으로는 구조화 반출 게이트가 DeepSeek 호출 전에 거부한다."""
import json
from pathlib import Path

import pytest

import pdr_ing_paths  # noqa: F401
from press_review.ingestion import rights as R
from press_review.ingestion import structure as S

LEDGER = Path(__file__).resolve().parents[2] / "docs" / "coverage" / "rights_ledger.json"
# 회신(2026-11-11)이 제외한 심의기준 고정 페이지 2개는 이 결정의 대상이 아니다.
EXEMPT = {"pac_standards_correction_page", "pac_standards_election_page"}


def _pac_rows():
    rows = json.loads(LEDGER.read_text(encoding="utf-8"))["sources"]
    return [r for r in rows if r["source_family"].startswith("pac_") and r["source_id"] not in EXEMPT]


def test_ledger_has_sixteen_pac_targets():
    assert len(_pac_rows()) == 16


@pytest.mark.parametrize("row", _pac_rows(), ids=lambda r: r["source_id"])
def test_pac_sources_are_refused_before_any_send(manifest, row):
    sid = row["source_id"]
    manifest.ensure_source(sid, row["source_family"], row["title"], row["entry_url"])
    manifest.upsert_document(f"{sid}:1", sid, title=row["title"])
    manifest.set_rights("source", sid, {k: row["rights"][k] for k in R.RIGHT_KEYS}, row["rights_basis"], row["review_status"] or "pending")
    assert S.egress_reason(manifest, f"{sid}:1") == "external_api_permission_required"
    assert row["rights"]["external_api"] == "permission_required"

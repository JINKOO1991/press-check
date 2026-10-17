"""색인이 없을 때: search_cases·submit_report 는 합성 근거 대신 오류, ALLOW_SYNTHETIC=1 이면 합성 코퍼스, corpus_status 는 항상 동작."""
import json

from conftest import McpClient


def _run(tmp_path, allow_synthetic, calls):
    c = McpClient(tmp_path, allow_synthetic=allow_synthetic)
    try:
        c.initialize()
        return [c.call(n, a) for n, a in calls]
    finally:
        c.close()


def test_no_index_refuses_synthetic_evidence(tmp_path):
    search, submit, status = _run(tmp_path, False, [
        ("search_cases", {"query": "정정보도"}),
        ("submit_report", {"article_handle": "art-x", "findings": []}),
        ("corpus_status", {}),
    ])
    for r in (search, submit):
        assert r["isError"] is True
        text = " ".join(r["structuredContent"]["errors"])
        assert str(tmp_path / "data") in text and "PRESS_DISPUTE_DATA_DIR" in text and "PRESS_REVIEW_ALLOW_SYNTHETIC=1" in text
    assert status["isError"] is False
    assert json.loads(status["content"][0]["text"])["is_fixture"] is True


def test_allow_synthetic_restores_fixture_search(tmp_path):
    (search,) = _run(tmp_path, True, [("search_cases", {"query": "기사 전체 인상 제목 본문 단정 혐의 수사", "limit": 3})])
    assert search["isError"] is False
    assert search["structuredContent"]["is_fixture"] is True

"""평가 하네스 배선 점검: 규칙이 기준 출력을 받아들이고, 시나리오·기사·규칙이 어긋나지 않는지."""
import json
import subprocess
import sys

import evalkit


def test_scenarios_match_contract_and_rules_cover_all(articles):
    contract = json.loads((evalkit.REPO / "docs/handoff/contracts/evaluation-scenarios.json").read_bytes())
    mine = json.loads((evalkit.HERE / "scenarios.json").read_bytes())
    assert contract == mine
    ids = [c["id"] for c in mine["cases"]]
    assert len(ids) == 12
    assert set(ids) == set(evalkit.CASES) == set(articles)
    for i in ids:
        assert (evalkit.HERE / "golden" / f"{i}.json").exists()


def test_all_articles_are_valid_contracts(articles):
    from press_review import contracts
    for cid, a in articles.items():
        assert contracts.validate_article(a) == [], cid


def test_golden_passes_every_machine_rule():
    r = evalkit.run_all("golden")
    assert r["counts"] == {"pass": 11, "fail": 0, "manual": 1}
    assert [c["case_id"] for c in r["cases"] if c["status"] == "manual"] == ["pure_opinion"]


def test_mock_counts_are_stable_and_gaps_are_honest():
    r = evalkit.run_all("mock")
    assert r["counts"] == {"pass": 6, "fail": 5, "manual": 1}
    failed = {c["case_id"] for c in r["cases"] if c["status"] == "fail"}
    # mock 에는 제목 단정 규칙뿐이라 적극 판단이 필요한 사례는 finding 이 없어 실패한다.
    assert failed == {"insult_not_fact", "reply_truth", "unknown_response", "anonymous_identifiable", "followup_outcome"}
    for c in r["cases"]:
        if c["case_id"] in failed:
            assert [x["name"] for x in c["checks"] if x["status"] == "fail"][0].startswith("has_")


def test_run_evals_cli_writes_json(tmp_path):
    p = subprocess.run([sys.executable, str(evalkit.HERE / "run_evals.py"), "--out-dir", str(tmp_path)],
                       capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    data = json.loads((tmp_path / "eval_mock.json").read_text(encoding="utf-8"))
    assert data["counts"] == {"pass": 6, "fail": 5, "manual": 1}
    assert "법률 정확도" in data["meta"]["note"]
    assert "pass=6 fail=5 manual=1" in p.stdout


def test_sandbox_blocks_network_and_restores_env():
    import os
    import socket
    before = os.environ.get("HOME")
    with evalkit.sandbox() as st:
        try:
            socket.create_connection(("example.invalid", 80))
        except OSError:
            pass
        assert os.environ["DEEPSEEK_API_KEY"] == "fake-deepseek-0000"
    assert st.network_attempts == 1
    assert os.environ.get("HOME") == before

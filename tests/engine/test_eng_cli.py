import json
import os
import subprocess
import sys

import refschema
from press_review import contracts

import pdr_engine_paths as paths

ARTICLE = str(paths.ARTICLE_FIXTURE)
NOW = "2026-10-08T12:00:00+09:00"


def cli(*args, cwd, home=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(paths.ENGINE)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if home:
        env["HOME"] = str(home)
    for k in ("DEEPSEEK_API_KEY", "LAW_OPEN_DATA_OC", "PRESS_DISPUTE_ENABLE_PERCENTAGE"):
        env.pop(k, None)
    return subprocess.run([sys.executable, "-m", "press_review", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8")


def test_review_prints_table(tmp_path):
    p = cli("review", "--input", ARTICLE, "--provider", "mock", "--now", NOW, cwd=tmp_path, home=tmp_path)
    assert p.returncode == 0, p.stderr
    lines = p.stdout.splitlines()
    assert lines[0] == "별(★)은 실제 분쟁 발생 확률이 아니라 검토 우선순위입니다."
    assert "| 문제 요소 | 검토 필요도 | 사유 | 추천 수정안 |" in lines
    assert "★★★" in p.stdout and "회사 돈 빼돌린 혐의…경찰, 대표 수사" in p.stdout
    assert "file_bytes" in p.stdout
    assert list(tmp_path.iterdir()) == []                                  # 디스크 무기록


def test_review_json_is_valid_for_both_validators(tmp_path):
    p = cli("review", "--input", ARTICLE, "--format", "json", "--now", NOW, cwd=tmp_path, home=tmp_path)
    assert p.returncode == 0, p.stderr
    rep = json.loads(p.stdout)
    schema = contracts.load_schema("review")
    assert contracts.schema_errors(rep, schema) == [] and list(refschema.ref_validator(schema).iter_errors(rep)) == []
    assert rep["article_sha256"].startswith("ac38fd55")
    rp = tmp_path / "out.json"
    rp.write_text(p.stdout, encoding="utf-8")
    v = cli("validate", "--article", ARTICLE, "--report", str(rp), cwd=tmp_path, home=tmp_path)
    assert v.returncode == 0 and v.stdout.strip() == "OK", v.stderr


def test_validate_catches_tampered_report(tmp_path):
    p = cli("review", "--input", ARTICLE, "--format", "json", "--now", NOW, cwd=tmp_path, home=tmp_path)
    rep = json.loads(p.stdout)
    rep["findings"][0]["span"]["quote"] = "변조된 인용"
    rp = tmp_path / "t.json"
    rp.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    v = cli("validate", "--article", ARTICLE, "--report", str(rp), cwd=tmp_path, home=tmp_path)
    assert v.returncode == 1 and "quote does not equal" in v.stderr


def test_review_plaintext_file(tmp_path):
    f = tmp_path / "기사.txt"
    f.write_text("회사 돈 빼돌린 대표, 경찰 수사\n\n경찰, 횡령 혐의로 사실관계 확인\n\n경찰은 횡령 혐의를 수사하고 있다고 밝혔다.\n", encoding="utf-8")
    p = cli("review", "--input", str(f), "--now", NOW, cwd=tmp_path, home=tmp_path)
    assert p.returncode == 0, p.stderr
    assert "★★★" in p.stdout and "warning:" in p.stderr


def test_bad_input_exit_codes(tmp_path):
    assert cli("review", "--input", str(tmp_path / "nope.json"), cwd=tmp_path, home=tmp_path).returncode == 2
    bad = tmp_path / "b.json"
    bad.write_text("{}", encoding="utf-8")
    p = cli("review", "--input", str(bad), cwd=tmp_path, home=tmp_path)
    assert p.returncode == 2 and "schema" in p.stderr
    assert cli("review", "--input", ARTICLE, "--now", "yesterday", cwd=tmp_path, home=tmp_path).returncode == 2
    assert cli("review", "--input", ARTICLE, "--provider", "deepseek", cwd=tmp_path, home=tmp_path).returncode == 2   # argparse 거부


def test_corpus_status_command(tmp_path):
    p = cli("corpus-status", cwd=tmp_path, home=tmp_path)
    st = json.loads(p.stdout)
    assert p.returncode == 0 and st["is_fixture"] is True and st["snapshot_id"] == "fixture-synthetic-0.1"


def test_secrets_never_printed_even_if_set(tmp_path):
    env_home = tmp_path / "home"
    key = env_home / ".config" / "deepseek" / "api_key"
    key.parent.mkdir(parents=True)
    key.write_text("fake-secret-value-0001\n")
    os.chmod(key, 0o600)
    p = cli("review", "--input", ARTICLE, "--now", NOW, cwd=tmp_path, home=env_home)
    assert "fake-secret-value-0001" not in p.stdout + p.stderr

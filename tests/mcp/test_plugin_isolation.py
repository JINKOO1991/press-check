"""플러그인 폴더(마켓플레이스 설치 대상)에는 수집 어댑터·DeepSeek 코드가 없고, 그것만 복사한 환경에서 MCP 서버가 뜬다(UDEC-25)."""
import ast
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synth_ledger as sl  # noqa: E402

from press_review import corpus_sqlite  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "press-check"
DEV_ENGINE = REPO / "devtools" / "engine"
NOW = dt.datetime(2026, 11, 7, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def _py_files(root: Path):
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_plugin_folder_has_no_ingestion_or_deepseek_files():
    bad = [str(p.relative_to(PLUGIN)) for p in PLUGIN.rglob("*") if "__pycache__" not in p.parts
           and (p.name == "ingestion" or "deepseek" in p.name.lower() or p.name in ("prompts", "prices"))]
    assert bad == []


def test_plugin_python_does_not_import_ingestion_or_deepseek():
    offenders = []
    for p in _py_files(PLUGIN):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                names = [base] + [f"{base}.{a.name}".strip(".") for a in node.names]
            for n in names:
                parts = n.split(".")
                if "ingestion" in parts or "deepseek" in parts:
                    offenders.append(f"{p.relative_to(PLUGIN)}:{node.lineno} {n}")
    assert offenders == []


def test_developer_tree_has_the_moved_code_and_no_package_init_collision():
    root = DEV_ENGINE / "press_review"
    assert (root / "ingestion" / "__main__.py").is_file() and (root / "providers" / "deepseek.py").is_file()
    assert (root / "prompts" / "common.md").is_file() and (root / "prices" / "deepseek_prices.json").is_file()
    assert not (root / "__init__.py").exists() and not (root / "providers" / "__init__.py").exists()   # 플러그인의 패키지와 합쳐지게 둔다


def _run(code: str, pythonpath: str, cwd: Path, extra: dict | None = None):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": pythonpath, "PYTHONUTF8": "1", "PYTHONSAFEPATH": "1", **(extra or {})}
    return subprocess.run([sys.executable, "-c", code], env=env, cwd=cwd, capture_output=True, text=True, timeout=60)


def test_developer_path_resolves_same_module_names(tmp_path):
    code = "import press_review.ingestion as i, press_review.providers.deepseek as d, press_review.providers as p; print(i.__file__, d.__file__, p.PROVIDERS)"
    r = _run(code, f"{PLUGIN / 'engine'}{os.pathsep}{DEV_ENGINE}", tmp_path)
    assert r.returncode == 0, r.stderr
    assert str(DEV_ENGINE) in r.stdout and str(PLUGIN) not in r.stdout.split()[0]


def test_plugin_only_cannot_import_collection_code(tmp_path):
    r = _run("import press_review.ingestion", str(PLUGIN / "engine"), tmp_path)
    assert r.returncode != 0 and "ModuleNotFoundError" in r.stderr
    r = _run("import press_review.providers.deepseek", str(PLUGIN / "engine"), tmp_path)
    assert r.returncode != 0 and "ModuleNotFoundError" in r.stderr


class _Rpc:
    def __init__(self, engine: Path, data: Path, cwd: Path):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "PYTHONPATH": str(engine), "PYTHONSAFEPATH": "1", "PYTHONUTF8": "1",
               "PRESS_DISPUTE_DATA_DIR": str(data)}                                  # PRESS_REVIEW_ALLOW_SYNTHETIC 은 일부러 넣지 않는다(합성 폴백으로 통과하는 것을 막는다)
        self.p = subprocess.Popen([sys.executable, str(engine / "press_review_mcp.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, cwd=cwd, text=True, encoding="utf-8")
        self.i = 0

    def call(self, method, params=None):
        self.i += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.i, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=20)
        err = self.p.stderr.read()
        self.p.stdout.close()
        self.p.stderr.close()
        return err


def test_plugin_only_copy_serves_search_cases_from_installed_index(tmp_path):
    data = tmp_path / "data"
    ledger = sl.build_ledger(data / "corpus")
    corpus_sqlite.build_index(ledger, data / "index", now=NOW)
    plugin_copy = tmp_path / "plugin-copy"
    shutil.copytree(PLUGIN, plugin_copy, ignore=shutil.ignore_patterns("__pycache__"))
    assert not (plugin_copy / "engine" / "press_review" / "ingestion").exists()
    rpc = _Rpc(plugin_copy / "engine", data, tmp_path)
    try:
        rpc.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        st = rpc.call("tools/call", {"name": "corpus_status", "arguments": {}})["result"]["structuredContent"]
        assert st["is_fixture"] is False and st["passage_count"] == 7
        res = rpc.call("tools/call", {"name": "search_cases", "arguments": {"query": "2021다20003", "limit": 3}})["result"]
        assert res["isError"] is False and res["structuredContent"]["is_fixture"] is False
        assert res["structuredContent"]["passages"][0]["passage_id"] == sl.J2
    finally:
        err = rpc.close()
    assert "Traceback" not in err


def test_no_index_guidance_points_to_bundle_install_not_collection(tmp_path):
    (tmp_path / "empty").mkdir()
    rpc = _Rpc(PLUGIN / "engine", tmp_path / "nodata", tmp_path)
    try:
        rpc.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        res = rpc.call("tools/call", {"name": "search_cases", "arguments": {"query": "정정보도"}})["result"]
        status = rpc.call("tools/call", {"name": "corpus_status", "arguments": {}})["result"]["structuredContent"]
    finally:
        rpc.close()
    text = " ".join(res["structuredContent"]["errors"])
    assert res["isError"] is True and "install-bundle" in text and "출시 전" in text and "build_index" not in text and "수집 CLI" not in text
    assert status["is_fixture"] is True and "build_index" not in json.dumps(status, ensure_ascii=False)

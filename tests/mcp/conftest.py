import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdr_mcp_paths import ARTICLE_FIXTURE, ENGINE, REPO  # noqa: E402

from press_review import logsafe  # noqa: E402
from press_review.mcp_server import SUPPORTED_PROTOCOL_VERSIONS  # noqa: E402

TIMEOUT = 20


@pytest.fixture(autouse=True)
def _clean_logsafe():
    logsafe.reset()
    yield
    logsafe.reset()


class McpClient:
    """서버를 subprocess로 띄우고 줄 단위 JSON을 주고받는다. stdout 원본 줄과 stderr 전체를 보관한다."""

    def __init__(self, tmp_path: Path, allow_synthetic: bool = True):
        home = tmp_path / "home"
        home.mkdir()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONPATH": str(ENGINE),
            "PYTHONUTF8": "1",
            "PRESS_DISPUTE_DATA_DIR": str(tmp_path / "data"),
        }
        if allow_synthetic:
            env["PRESS_REVIEW_ALLOW_SYNTHETIC"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "press_review.mcp_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(tmp_path),
        )
        self.stdout_lines: list[bytes] = []
        self.stderr_chunks: list[bytes] = []
        self._q: queue.Queue = queue.Queue()
        self._next_id = 1
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_out(self):
        for line in self.proc.stdout:
            self.stdout_lines.append(line)
            self._q.put(line)
        self._q.put(None)

    def _pump_err(self):
        for chunk in iter(lambda: self.proc.stderr.read1(4096), b""):
            self.stderr_chunks.append(chunk)

    @property
    def stderr_text(self) -> str:
        return b"".join(self.stderr_chunks).decode("utf-8", "replace")

    def send_raw(self, data: bytes):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")

    def read_response(self) -> dict:
        line = self._q.get(timeout=TIMEOUT)
        assert line is not None, "server closed stdout"
        return json.loads(line.decode("utf-8"))

    def request(self, method: str, params: dict | None = None) -> dict:
        rid = self._next_id
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
        resp = self.read_response()
        assert resp["id"] == rid
        return resp

    def initialize(self, version: str = SUPPORTED_PROTOCOL_VERSIONS[0]) -> dict:
        resp = self.request("initialize", {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}})
        self.notify("notifications/initialized")
        return resp

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """tools/call 의 result 를 돌려준다(JSON-RPC error 면 AssertionError)."""
        resp = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        assert "error" not in resp, resp
        return resp["result"]

    def close(self) -> int:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=TIMEOUT)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.stdout.close()
            self.proc.stderr.close()


@pytest.fixture()
def mcp_client_cls():
    """다른 테스트 모듈이 ``from conftest import`` 없이 McpClient 를 쓰는 통로(conftest 이름 충돌 방지)."""
    return McpClient


@pytest.fixture()
def server(tmp_path):
    c = McpClient(tmp_path)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def ready(server):
    server.initialize()
    return server


@pytest.fixture()
def article_obj():
    return json.loads(ARTICLE_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def repo():
    return REPO

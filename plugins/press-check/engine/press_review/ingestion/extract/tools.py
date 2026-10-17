"""외부 바이너리 실행기(수집·운영 쪽 전용; 플러그인 런타임 의존성이 아니다).

테스트는 ``ToolRunner`` 를 상속해 ``have``/``run`` 을 바꾼 가짜 실행기를 주입한다(subprocess 주입).
kordoc 경로는 env ``KORDOC_BIN`` 에서만 받는다. 없으면 PATH 의 ``kordoc``. 개인 경로를 코드에 넣지 않는다.
(환경변수 해석은 원칙적으로 config.py 몫이지만, P2_DESIGN §2 가 이 변수를 수집 도구 한정으로 지정해 여기서 읽는다.)
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

KORDOC_ENV = "KORDOC_BIN"


class ToolError(Exception):
    """kind: missing | timeout | os"""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


@dataclass
class ToolResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class ToolRunner:
    def have(self, name: str) -> bool:
        return shutil.which(name) is not None

    def run(self, argv: list, *, timeout: float) -> ToolResult:
        try:
            p = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except FileNotFoundError as e:
            raise ToolError("missing", f"{argv[0]}: not found") from e
        except subprocess.TimeoutExpired as e:
            raise ToolError("timeout", f"{os.path.basename(str(argv[0]))} timed out after {timeout}s") from e
        except OSError as e:
            raise ToolError("os", f"{os.path.basename(str(argv[0]))}: {type(e).__name__}") from e
        return ToolResult(p.returncode, p.stdout, p.stderr)

    def kordoc_bin(self) -> Optional[str]:
        """KORDOC_BIN(파일 경로 또는 PATH 의 이름) -> 없으면 PATH 의 kordoc -> 없으면 None."""
        v = os.environ.get(KORDOC_ENV, "").strip()
        if v:
            if os.path.isfile(v) and os.access(v, os.X_OK):
                return v
            return shutil.which(v)
        return shutil.which("kordoc")

"""article_sha256 계산. 모드 두 가지(contracts/README.md 교차검증 2).

- ``file_bytes``: 분석 당시 입력 파일의 정확한 바이트 SHA-256.
- ``cjson-1``: 대화·객체 입력. 객체를 canonical JSON(아래, 버전 cjson-1)으로 직렬화한 UTF-8 바이트의 SHA-256.

cjson-1 = ``json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`` 를 UTF-8 인코딩.
직렬화 규칙을 바꾸면 새 버전 이름(cjson-2 ...)을 만들고 cjson-1은 그대로 둔다.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

MODE_FILE_BYTES = "file_bytes"
MODE_CJSON_1 = "cjson-1"
MODES = (MODE_FILE_BYTES, MODE_CJSON_1)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def article_sha256(article: Any, raw_bytes: bytes | None = None) -> tuple[str, str]:
    """(hex, mode). raw_bytes가 있으면 파일 바이트 해시, 없으면 객체의 cjson-1 해시."""
    if raw_bytes is not None:
        return sha256_bytes(raw_bytes), MODE_FILE_BYTES
    return sha256_bytes(canonical_json(article)), MODE_CJSON_1

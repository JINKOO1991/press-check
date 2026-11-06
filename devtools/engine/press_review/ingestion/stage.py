"""fetch / extract 단계가 함께 쓰는 작은 도구: 경로 규칙, 원자적 쓰기, 문서 상태 갱신.

경로 규칙(P2_DESIGN §1·§2): ``attachments.raw_path`` 와 ``pages.text_path`` 는 **코퍼스 폴더(``<data>/corpus``) 기준 상대 경로**
(POSIX 구분자)로 저장한다. 읽을 때는 ``resolve(corpus_dir, rel)`` 을 쓴다. 장부를 내보내도 홈 경로가 새지 않고, 데이터 폴더를 옮겨도 깨지지 않는다.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Optional

from ..config import private_mkdir

# 문서 상태 진행 순서(상태 이름은 manifest.DOC_STATUSES). 이 모듈이 건드리는 상태는 아래 MANAGED 뿐이다.
MANAGED = ("discovered", "fetched", "extracted", "failed")
EXTRACTED_OR_LATER = ("extracted", "structured", "verified", "indexed")


def stable_id(text: str, length: int = 16) -> str:
    """att_id 같은 긴 식별자(``:`` ``#`` 포함)에서 파일 시스템용 짧은 안정 ID를 만든다."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def to_rel(corpus_dir: Path, path: Path) -> str:
    return Path(path).relative_to(corpus_dir).as_posix()


def resolve(corpus_dir: Path, rel: str) -> Path:
    """상대 경로를 코퍼스 폴더 안의 실제 경로로. 코퍼스 밖을 가리키는 값(``..``·절대 경로)은 거부한다."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"path escapes corpus dir: {rel!r}")
    return Path(corpus_dir) / p


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """임시 파일에 쓰고 fsync 한 뒤 os.replace. 중간에 죽어도 최종 경로에는 온전한 파일만 있다."""
    path = Path(path)
    private_mkdir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def family_of(manifest: Any, source_id: str, cache: Optional[dict] = None) -> str:
    cache = cache if cache is not None else {}
    if source_id not in cache:
        src = manifest.get_source(source_id)
        cache[source_id] = (src or {}).get("source_family") or "unknown"
    return cache[source_id]


def refresh_document_status(manifest: Any, doc_id: str) -> str:
    """첨부 상태에서 문서 상태를 다시 계산한다(멱등). discovered/fetched/extracted/failed 인 문서만 건드린다.

    - 첨부가 하나라도 failed 이면 문서도 failed (재시도 신호. 부분 성공을 성공으로 덮지 않는다).
    - quarantined 첨부가 있으면 quarantined.
    - withheld 첨부는 판정에서 뺀다.
    - 모두 extracted 이상이면 extracted, 모두 discovered 가 아니면 fetched, 그 밖은 discovered.
    """
    doc = manifest.get_document(doc_id)
    if not doc:
        raise KeyError(doc_id)
    cur = doc["status"]
    if cur not in MANAGED:
        return cur
    atts = [a for a in manifest.attachments_for(doc_id) if a["status"] != "withheld"]
    if not atts:
        return cur
    st = [a["status"] for a in atts]
    if "quarantined" in st:
        new = "quarantined"
    elif "failed" in st:
        new = "failed"
    elif all(s in EXTRACTED_OR_LATER for s in st):
        new = "extracted"
    elif all(s != "discovered" for s in st):
        new = "fetched"
    else:
        new = "discovered"
    if new != cur:
        manifest.set_document_status(doc_id, new)
    return new


def ranges_str(nums: list[int]) -> str:
    """[1,2,3,7] -> '1-3,7'. 문서 extra_json 에 스칼라로 넣어 재기록 때 리스트 합집합 병합이 옛 값을 남기지 않게 한다."""
    nums = sorted(set(nums))
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ",".join(out)

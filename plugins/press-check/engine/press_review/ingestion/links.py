"""연구 자료가 소개한 판결 -> 법제처 판례 장부 문서 연결(P2_DESIGN G-D6). 결정적 규칙이며 모델 출력을 믿지 않는다.

``pac_research_judgment_analysis`` 의 사건 record 는 소개된 판결의 사건번호(원문에 인쇄된 표기, 코드가 원문 대조를 마친 값)를 ``case_number`` 로 갖는다.
이 값을 ``sources/lawapi._CaseIndex``(장부의 판례 문서를 낱개 사건번호로 색인)로 찾아 ``case_links`` 행으로 남긴다.

- 사건번호가 같은 문서가 있고 선고일이 모두 알려진 경우 서로 같아야 연결한다. 한쪽 선고일을 모르면(None) 번호만으로 연결한다.
- 번호는 있는데 선고일이 어긋나면 ``date_mismatch``: 연결하지 않고 사실만 적는다. 장부에 번호가 없으면 ``not_in_ledger``(참조판례 확장이 나중에 채울 수 있다).
- 연결 행은 사건을 다시 조립할 때 함께 바뀌고(``cases`` 삭제 시 같이 지움), ``relink_all`` 로 장부가 자란 뒤 다시 계산할 수 있다.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .sources import lawapi

REL_INTRODUCES = "introduces_judgment"


def ledger_index(manifest: Any):
    """장부의 판례 문서 사건번호 색인. 한 번 만들어 여러 사건에 쓴다(판례 문서가 수만 건이다)."""
    return lawapi._CaseIndex(manifest)


def find_ledger_docs(manifest: Any, case_number: Optional[str], decision_date: Optional[str] = None, *, index: Any = None) -> tuple[list, str]:
    """(연결된 doc_id 목록, 상태). 상태: linked | date_mismatch | not_in_ledger."""
    numbers = lawapi.split_case_numbers(case_number or "")
    if not numbers:
        return [], "not_in_ledger"
    idx = index if index is not None else ledger_index(manifest)
    found: list = []
    for n in numbers:
        for doc_id, d in idx.map.get(n, ()):
            if (doc_id, d) not in found:
                found.append((doc_id, d))
    if not found:
        return [], "not_in_ledger"
    kept = [doc_id for doc_id, d in found if not decision_date or not d or d == decision_date]
    kept = list(dict.fromkeys(kept))
    return (kept, "linked") if kept else ([], "date_mismatch")


def link_case(manifest: Any, case_id: str, record: dict, *, index: Any = None) -> list:
    """사건 하나의 연결 행을 계산해 저장한다. 반환: 저장한 행."""
    number = record.get("case_number")
    if not number:
        manifest.replace_case_links(case_id, [])
        return []
    docs, status = find_ledger_docs(manifest, number, record.get("decision_date"), index=index)
    rows = ([{"rel": REL_INTRODUCES, "case_number": number, "target_doc_id": d, "status": status} for d in docs]
            or [{"rel": REL_INTRODUCES, "case_number": number, "target_doc_id": "", "status": status}])
    manifest.replace_case_links(case_id, rows)
    return rows


def relink_all(manifest: Any) -> dict:
    """연결이 필요한 프로필(links_ledger)의 모든 비격리 사건을 다시 연결한다. 장부에 판례가 늘어난 뒤 쓴다. 반환: 상태별 사건 수."""
    from . import structure_records as sr
    sources = [src for src, prof in sr.SOURCE_PROFILE.items() if sr.PROFILES[prof].links_ledger]
    if not sources:
        return {}
    idx = ledger_index(manifest)
    counts: dict = {}
    ph = ",".join("?" * len(sources))
    for r in manifest.select(
            f"SELECT c.case_id, c.record_json FROM cases c JOIN documents d ON d.doc_id=c.doc_id WHERE d.source_id IN ({ph}) "
            "AND c.verification_status<>'quarantined' ORDER BY c.case_id", tuple(sources)):
        rows = link_case(manifest, r["case_id"], json.loads(r["record_json"]), index=idx)
        key = rows[0]["status"] if rows else "no_case_number"
        counts[key] = counts.get(key, 0) + 1
    return counts

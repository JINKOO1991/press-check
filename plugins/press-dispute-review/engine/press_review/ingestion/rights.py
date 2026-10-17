"""권리 기록 기본값과 설치자 결정 기록. 신규 설치의 기본값은 모두 unknown 이다. 모델이 권한을 승인하지 않으며,
허용은 설치자가 `rights set` 으로 직접 기록한 결정(기록자·시각·근거 메모 포함)만 장부에 남긴다.

source_family 값은 docs/handoff/contracts/source-record.schema.json 의 enum 을 따른다.
unknown 은 허용으로 자동 전환하지 않는다.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

RIGHT_KEYS = ("read", "store_raw", "create_derivatives", "external_api",
              "redistribute_raw", "redistribute_derivatives", "fine_tune")
VALUES = ("allowed", "forbidden", "permission_required", "unknown")

# 설치자가 `rights set` 으로 결정을 기록할 수 있는 수집 대상 계열. naver 는 robots 금지(forbidden)라 대상이 아니다.
COLLECTED_FAMILIES = ("pac_cases", "pac_election", "pac_other", "iendc", "law_go_kr")


def default_rights(source_family: str) -> dict:
    """source_family 의 기본 권리 기록: {'rights', 'rights_basis', 'review_status'}. naver 만 forbidden, 나머지는 전부 unknown."""
    if source_family == "naver":
        return {
            "rights": {k: "forbidden" for k in RIGHT_KEYS},
            "rights_basis": [{"scope": "robots.txt: AI 학습·RAG 목적 접근 금지 명시, collection_enabled=false (UDEC-1)",
                              "review_status": "verified", "url": "https://news.naver.com/robots.txt"}],
            "review_status": "verified",
        }
    return {
        "rights": {k: "unknown" for k in RIGHT_KEYS},
        "rights_basis": [],
        "review_status": "pending",
    }


def validate_rights(rights: dict[str, Any]) -> None:
    missing = [k for k in RIGHT_KEYS if k not in rights]
    extra = [k for k in rights if k not in RIGHT_KEYS]
    bad = [k for k, v in rights.items() if v not in VALUES]
    if missing or extra or bad:
        raise ValueError(f"invalid rights: missing={missing} extra={extra} bad={bad}")


# ---------------------------------------------------------------- 문서 단위 상속·소스 레코드·장부 내보내기
# manifest 는 이 모듈을 import 하므로 여기서는 Manifest 를 import 하지 않고 메서드만 부른다(덕 타이핑).

FAMILY_HOST = {"pac_cases": "www.pac.or.kr", "pac_election": "www.pac.or.kr", "pac_other": "www.pac.or.kr",
               "iendc": "www.iendc.go.kr", "law_go_kr": "www.law.go.kr"}
# robots 테이블의 purpose_status(ingestion/robots.py) -> source-record 스키마 enum(allowed/disallowed/unknown).
# no_rules(robots 4xx)는 규칙 없음=제한 없음이라 allowed 로 옮기고 notes 에 원값을 남긴다.
# unparseable(2xx인데 HTML)은 수집을 막는 상태라 disallowed(보류·차단)로 옮기고 notes 에 원값을 남긴다.
_ROBOTS_MAP = {"allowed": "allowed", "no_rules": "allowed", "root_disallowed": "disallowed", "unparseable": "disallowed"}

# 수집하지 않는 소스. 사유는 P1_DESIGN·SOURCE_ACCESS_AUDIT 의 기록이며 새 판단이 아니다.
NOT_COLLECTED = (
    {"source_id": "naver_ombudsman", "source_family": "naver", "title": "네이버 뉴스 옴부즈만(고침·정정·불공정 선거보도)",
     "entry_url": "https://news.naver.com/ombudsman/errorArticleList", "status": "not_collected",
     "reason": "robots.txt가 AI 학습·RAG 목적의 봇 접근을 명시적으로 금지한다. collection_enabled=false (UDEC-1). 요청하지 않았다."},
    {"source_id": "daum_correct", "source_family": "daum", "title": "다음 뉴스 정정보도 모음",
     "entry_url": "https://news.daum.net/correct", "status": "on_hold",
     "reason": "이용 정책 확인 전이다. 허용 호스트 목록에 넣지 않았고 요청하지 않았다. 권리는 unknown."},
    {"source_id": "kci_papers", "source_family": "research", "title": "KCI 논문 서지",
     "entry_url": "https://www.kci.go.kr/", "status": "not_collected",
     "reason": "robots 금지로 기록된 호스트다(P1_DESIGN). 요청하지 않았다. 권리는 unknown."},
    {"source_id": "research_papers", "source_family": "research", "title": "학술 논문 본문(언론법 관련)",
     "entry_url": "", "status": "on_hold",
     "reason": "출판사·저자별 이용 조건이 소스마다 다르고 확인하지 않았다. 권리는 unknown, 수집 보류."},
)


def validate_override(source_rights: dict, rights: dict, rights_basis: list) -> None:
    """문서 단위 재정의 규칙: 값 검증, allowed 가 있으면 근거 1건 이상, 소스가 forbidden 인 항목을 풀 수 없다."""
    validate_rights(rights)
    if any(v == "allowed" for v in rights.values()) and not rights_basis:
        raise ValueError("document override with 'allowed' needs rights_basis")
    loosened = [k for k in RIGHT_KEYS if source_rights.get(k) == "forbidden" and rights[k] != "forbidden"]
    if loosened:
        raise ValueError(f"document override cannot loosen source 'forbidden': {loosened}")


def set_document_override(manifest: Any, doc_id: str, rights: dict, rights_basis: list,
                          review_status: str = "pending") -> None:
    doc = manifest.get_document(doc_id)
    if not doc:
        raise KeyError(doc_id)
    src = manifest.get_rights("source", doc["source_id"])
    if not src:
        raise KeyError(f"no source rights for {doc['source_id']}")
    validate_override(src, rights, rights_basis)
    manifest.set_rights("document", doc_id, rights, rights_basis, review_status)


def record_installer_decision(manifest: Any, source_family: str, changes: dict, note: str, by: str) -> list:
    """설치자가 소스 계열의 권리 결정을 기록한다. 그 계열로 등록된 모든 소스 행에 changes(RIGHT_KEYS 부분집합)를 적용하고
    rights_basis 에 기록자·근거 메모를 남긴다. 반환: 갱신한 source_id 목록. 소스 행의 forbidden 은 풀 수 없다."""
    if source_family not in COLLECTED_FAMILIES:
        raise ValueError(f"unsupported source_family '{source_family}' (choose: {', '.join(COLLECTED_FAMILIES)})")
    if not changes:
        raise ValueError("no right given: pass at least one of --" + " --".join(k.replace("_", "-") for k in RIGHT_KEYS))
    if not note.strip():
        raise ValueError("note is required (기록자가 근거를 한 줄로 남긴다)")
    validate_rights({**{k: "unknown" for k in RIGHT_KEYS}, **changes})
    srcs = [s for s in manifest.sources() if s["source_family"] == source_family]
    if not srcs:
        raise LookupError(f"no source of family '{source_family}' in the ledger (run its collector first so the sources are registered)")
    plan = []
    for s in srcs:
        cur = manifest.get_rights("source", s["source_id"])
        rights = {k: (cur[k] if cur else "unknown") for k in RIGHT_KEYS}
        loosened = [k for k in changes if rights[k] == "forbidden" and changes[k] != "forbidden"]
        if loosened:
            raise ValueError(f"cannot loosen 'forbidden' on {s['source_id']}: {loosened}")
        rights.update(changes)
        plan.append((s["source_id"], rights, list(cur["rights_basis"]) if cur else []))
    for sid, rights, basis in plan:
        basis.append({"scope": f"설치자 결정(기록자 {by}): {note.strip()}", "review_status": "pending",
                      "url": f"https://{FAMILY_HOST[source_family]}/"})
        manifest.set_rights("source", sid, rights, basis, "pending")
    return [sid for sid, _r, _b in plan]


def rights_overview(manifest: Any) -> list:
    """`rights show` 용: 소스별 현재 권리 값과 rights_basis(기록자·시각·근거)."""
    out = []
    for s in manifest.sources():
        r = manifest.get_rights("source", s["source_id"])
        out.append({"source_id": s["source_id"], "source_family": s["source_family"],
                    "rights": {k: r[k] for k in RIGHT_KEYS} if r else None,
                    "review_status": r["review_status"] if r else None,
                    "rights_basis": r["rights_basis"] if r else []})
    return out


def effective_rights(manifest: Any, doc_id: str) -> dict:
    """문서의 유효 권리: 문서 행이 있으면 그것, 없으면 소스 행을 상속. {'rights','rights_basis','review_status','inherited'}."""
    own = manifest.get_rights("document", doc_id)
    doc = manifest.get_document(doc_id)
    if not doc:
        raise KeyError(doc_id)
    row, inherited = (own, False) if own else (manifest.get_rights("source", doc["source_id"]), True)
    if not row:
        raise KeyError(f"no rights row for {doc['source_id']}")
    return {"rights": {k: row[k] for k in RIGHT_KEYS}, "rights_basis": row["rights_basis"],
            "review_status": row["review_status"], "inherited": inherited}


def _robots_for(manifest: Any, family: str) -> tuple[dict, str]:
    host = FAMILY_HOST.get(family)
    row = manifest.latest_robots(host) if host else None
    raw = row["purpose_status"] if row else "not_checked"
    fetch = "not_checked" if not row else ("checked" if row["http_status"] is not None else "unavailable")
    return ({"fetch_status": fetch, "purpose_status": _ROBOTS_MAP.get(raw, "unknown"),
             "policy_url": f"https://{host or 'unknown.invalid'}/robots.txt",
             "notes": f"robots purpose_status 원값={raw}, http_status={row['http_status'] if row else None}"}, raw)


def build_source_record(manifest: Any, doc_id: str, *, src: Optional[dict] = None, eff: Optional[dict] = None) -> dict:
    """장부 문서 1건 -> source-record 스키마 레코드(검증은 contracts.validate_source_record 가 한다)."""
    doc = manifest.get_document(doc_id)
    src = src or manifest.get_source(doc["source_id"])
    eff = eff or effective_rights(manifest, doc_id)
    robots, _raw = _robots_for(manifest, src["source_family"])
    url = doc["canonical_url"] or doc["detail_url"] or src["entry_url"] or "https://invalid.invalid/"
    enabled = bool(src["enabled"])
    collect = enabled and robots["purpose_status"] == "allowed" and eff["rights"]["read"] == "allowed"
    return {
        "schema_version": "0.1", "source_id": doc_id, "source_family": src["source_family"],
        "canonical_url": url, "document_title": doc["title"] or "", "publication_date": doc["publication_date"],
        "decision_date": doc["decision_date"], "checked_at": src["checked_at"] or doc["last_seen_at"],
        "access_status": "available", "collection_enabled": collect, "robots": robots,
        "rights": dict(eff["rights"]), "rights_basis": [dict(b) for b in eff["rights_basis"]],
        "raw_sha256": None,
        "extraction": {"status": "not_started", "version": None, "pages_expected": None, "pages_extracted": None},
        "outcome_type": "unknown",
        "before_after_status": "not_applicable" if src["source_family"] == "pac_other" else "unverified_pair",
        "notes": [f"rights_inherited={eff['inherited']}", f"rights_review_status={eff['review_status']}"],
    }


def not_collected_rows() -> list[dict]:
    rows = []
    for r in NOT_COLLECTED:
        d = default_rights(r["source_family"])
        rows.append({**r, "rights": d["rights"], "rights_basis": d["rights_basis"], "review_status": d["review_status"]})
    return rows


def export_ledger(manifest: Any, generated_at: str, *, validate: Optional[Callable[[Any], list]] = None) -> dict:
    """소스·문서 단위 권리 장부. validate 를 주면(contracts.validate_source_record) 문서마다 레코드를 만들어 검증하고 건수를 센다."""
    overrides = set(manifest.document_rights_scope_ids())
    sources = []
    for src in manifest.sources():
        sid = src["source_id"]
        srights = manifest.get_rights("source", sid)
        docs = manifest.documents_of(sid)
        _robots, raw = _robots_for(manifest, src["source_family"])
        entry = {
            "source_id": sid, "source_family": src["source_family"], "title": src["title"], "entry_url": src["entry_url"],
            "enabled": bool(src["enabled"]), "checked_at": src["checked_at"],
            "rights": {k: srights[k] for k in RIGHT_KEYS} if srights else None,
            "rights_basis": srights["rights_basis"] if srights else [], "review_status": srights["review_status"] if srights else None,
            "robots_host": FAMILY_HOST.get(src["source_family"]), "robots_purpose_status_raw": raw,
            "documents": len(docs), "documents_inheriting": sum(1 for d in docs if d["doc_id"] not in overrides),
            "document_overrides": sorted(d["doc_id"] for d in docs if d["doc_id"] in overrides),
        }
        if validate is not None:
            bad, first = 0, []
            for d in docs:
                errs = validate(build_source_record(manifest, d["doc_id"], src=src, eff=_eff_cached(manifest, d, srights, overrides)))
                if errs:
                    bad += 1
                    if len(first) < 3:
                        first.append({"doc_id": d["doc_id"], "errors": errs[:3]})
            entry["record_validation"] = {"checked": len(docs), "invalid": bad, "first_invalid": first}
        sources.append(entry)
    return {"generated_at": generated_at, "rights_policy": "기본값 unknown, 허용은 설치자의 `rights set` 기록만", "sources": sources,
            "not_collected": not_collected_rows()}


def _eff_cached(manifest: Any, doc: dict, srights: Optional[dict], overrides: set) -> dict:
    """대량 검증용: 상속 문서는 소스 행을 재사용하고 재정의 문서만 effective_rights 로 조회한다."""
    if doc["doc_id"] in overrides or not srights:
        return effective_rights(manifest, doc["doc_id"])
    return {"rights": {k: srights[k] for k in RIGHT_KEYS}, "rights_basis": srights["rights_basis"],
            "review_status": srights["review_status"], "inherited": True}

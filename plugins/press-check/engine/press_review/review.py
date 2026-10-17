"""보고서 조립과 MCP 도구 동작. CLI와 mcp_server.py가 공유한다.

MCP 도구 함수(D2): prepare_article, search_cases, submit_report, explain_finding, build_revision, corpus_status.
모두 JSON으로 직렬화되는 dict를 돌려주며 ``ok`` 키로 성공 여부를 알린다.

서버만 ``verified: true`` 를 붙인다: 모델이 보낸 verified 값은 버리고, 코퍼스에 source_id+passage_id가 있고
url·locator가 일치할 때만 서버가 채운다. 모델이 url·locator·authority_type을 보냈다면 코퍼스 값과 같아야 한다.
"""
from __future__ import annotations

import copy
import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import config, contracts, corpus_sqlite, edit_checks, hashing, logsafe, render, revision
from .article_io import parse_plaintext_ex
from .store import EphemeralStore


@dataclass
class BuildResult:
    report: dict | None
    errors: list[str]
    warnings: list[str] = field(default_factory=list)
    explanations: dict = field(default_factory=dict)


_EVIDENCE_KEYS = {"source_id", "passage_id", "url", "locator", "authority_type", "verified"}
_EXPLANATION_FIELDS = (str, list)


def _resolve_evidence(ev: Any, corpus: Any, where: str, errs: list[str]) -> dict | None:
    if not isinstance(ev, dict):
        errs.append(f"{where}: evidence item must be an object")
        return None
    extra = set(ev) - _EVIDENCE_KEYS
    if extra:
        errs.append(f"{where}: unexpected keys {sorted(extra)}")
        return None
    sid, pid = ev.get("source_id"), ev.get("passage_id")
    if not isinstance(sid, str) or not isinstance(pid, str):
        errs.append(f"{where}: source_id and passage_id are required strings")
        return None
    p = corpus.get_passage(sid, pid)
    if p is None:
        errs.append(f"{where}: {sid}/{pid} is not in the corpus (search_cases first; do not invent sources)")
        return None
    for k in ("url", "locator", "authority_type"):
        if k in ev and ev[k] != p[k]:
            errs.append(f"{where}: {k} differs from the corpus record")
            return None
    return {
        "source_id": sid, "passage_id": pid, "url": p["url"], "locator": p["locator"],
        "authority_type": p["authority_type"], "verified": True,
    }


def _shape_errors(f: dict, where: str) -> list[str]:
    """정규화가 순회·추가하는 필드의 구조·타입만 본다(나머지 스키마 검증은 contracts.validate_report)."""
    out: list[str] = []
    se = f.get("suggested_edit")
    if isinstance(se, dict):
        for key in ("related_edits", "required_verification"):
            if key in se and not isinstance(se[key], list):
                out.append(f"{where}.suggested_edit.{key}: must be a list")
        if isinstance(se.get("related_edits"), list):
            for j, r in enumerate(se["related_edits"]):
                if not isinstance(r, dict):
                    out.append(f"{where}.suggested_edit.related_edits[{j}]: must be an object")
        if isinstance(se.get("required_verification"), list):
            for j, r in enumerate(se["required_verification"]):
                if not isinstance(r, str):
                    out.append(f"{where}.suggested_edit.required_verification[{j}]: must be a string")
    for key in ("risk_channels", "counter_considerations"):
        if key in f and not isinstance(f[key], list):
            out.append(f"{where}.{key}: must be a list")
    return out


def _normalize_finding(idx: int, f: Any, article_blocks: dict, corpus: Any, errs: list[str], warns: list[str]) -> dict | None:
    if not isinstance(f, dict):
        errs.append(f"findings[{idx}]: must be an object")
        return None
    fid = f.get("finding_id") or f"finding-{idx + 1:03d}"
    where = f"findings[{idx}]({fid})"
    shape = _shape_errors(f, where)
    if shape:                       # CX1-7: 정규화(순회·append) 전에 구조·타입을 검증한다. 예외 대신 finding별 오류 목록을 돌려준다
        errs.extend(shape)
        return None
    nf = copy.deepcopy(f)
    nf["finding_id"] = fid
    nf.setdefault("risk_channels", [])
    nf.setdefault("counter_considerations", [])
    ps = nf.get("priority_stars")
    if isinstance(ps, float) and ps.is_integer():       # 스키마는 정수로 보는 2.0 을 렌더·정렬이 쓰는 int 로 맞춘다
        nf["priority_stars"] = int(ps)
    se = nf.get("suggested_edit")
    if isinstance(se, dict):
        se.setdefault("related_edits", [])
        se.setdefault("required_verification", [])
        if se.get("mode") in ("verify_first", "none"):
            se.setdefault("replacement", None)

    ev_in = nf.get("evidence", [])
    if not isinstance(ev_in, list):
        errs.append(f"{where}.evidence: must be a list")
        ev_in = []
    ev_out = []
    for j, ev in enumerate(ev_in):
        r = _resolve_evidence(ev, corpus, f"{where}.evidence[{j}]", errs)
        if r:
            ev_out.append(r)
    nf["evidence"] = ev_out
    if ev_out:
        nf.setdefault("validation_status", "verified")
    else:
        if nf.get("review_confidence") not in (None, "low") or nf.get("validation_status") not in (None, "needs_information"):
            warns.append(f"{where}: no evidence, so review_confidence set to 'low' and validation_status to 'needs_information'")
        nf["review_confidence"] = "low"
        nf["validation_status"] = "needs_information"

    # 수정안 결정적 검사(스키마가 맞을 때만)
    if isinstance(se, dict) and se.get("mode") == "replace" and isinstance(se.get("replacement"), str):
        sp = nf.get("span") if isinstance(nf.get("span"), dict) else {}
        jobs = [(sp, se["replacement"], "span")]
        for j, r in enumerate(se.get("related_edits") or []):
            if isinstance(r, dict) and isinstance(r.get("span"), dict) and isinstance(r.get("replacement"), str):
                jobs.append((r["span"], r["replacement"], f"related_edits[{j}]"))
        for span, rep, label in jobs:
            quote = span.get("quote")
            if not isinstance(quote, str):
                continue
            issues = edit_checks.check_replacement(
                quote, rep, {"blocks": [{"block_id": k, "text": v} for k, v in article_blocks.items()]},
                block_id=span.get("block_id") if isinstance(span.get("block_id"), str) and isinstance(span.get("start"), int) else None,
                start=span.get("start") if isinstance(span.get("start"), int) else None,
            )
            for i in issues:
                if i.level == "error":
                    errs.append(f"{where}.suggested_edit.{label}: [{i.code}] {i.message}")
                else:
                    msg = f"[자동검사 {i.code}] {i.message}"
                    se.setdefault("required_verification", []).append(msg)
                    warns.append(f"{where}.suggested_edit.{label}: [{i.code}] {i.message}")
    return nf


def _undated_evidence(findings: list[dict], corpus: Any) -> list[str]:
    """보고서가 인용한 근거 중 기준일이 없는(date_unknown) 실자료 passage 의 id. 합성 코퍼스는 제외한다."""
    out: list[str] = []
    for f in findings:
        for ev in f.get("evidence", []):
            p = corpus.get_passage(ev["source_id"], ev["passage_id"])
            if p and p.get("date_unknown") is True and not p.get("is_fixture") and ev["passage_id"] not in out:
                out.append(ev["passage_id"])
    return out


def _collect_edits(findings: list[dict]) -> list[dict]:
    """mode 'replace' 인 finding 의 수정 구간(본 구간 + related_edits). 모양이 맞지 않는 항목은 건너뛴다(스키마 검증이 따로 잡는다)."""
    out: list[dict] = []
    for f in findings:
        se = f.get("suggested_edit")
        if not isinstance(se, dict) or se.get("mode") != "replace":
            continue
        jobs = [(f.get("span"), se.get("replacement"), f"{f['finding_id']}.span")]
        for j, r in enumerate(se.get("related_edits") or []):
            if isinstance(r, dict):
                jobs.append((r.get("span"), r.get("replacement"), f"{f['finding_id']}.related_edits[{j}]"))
        for sp, rep, label in jobs:
            if (isinstance(sp, dict) and isinstance(sp.get("block_id"), str) and isinstance(sp.get("start"), int)
                    and isinstance(sp.get("end"), int) and isinstance(rep, str)):
                out.append({"block_id": sp["block_id"], "start": sp["start"], "end": sp["end"], "replacement": rep, "label": label})
    return out


def _flow_warnings(article: dict, findings: list[dict], warns: list[str]) -> None:
    """수정안을 모두 적용한 사본의 흐름 검사(P3_DESIGN §3)와 반복 쟁점 안내. 경고만 낸다.

    구간 겹침은 contracts.validate_report 가 오류로 보고하므로 여기서 다시 내지 않는다.
    """
    for i in edit_checks.check_flow(article, _collect_edits(findings)):
        if not i.code.startswith("EDIT_"):
            warns.append(f"흐름검사 [{i.code}] {i.message}")
    seen: dict[tuple, str] = {}
    for f in findings:
        se = f.get("suggested_edit") if isinstance(f.get("suggested_edit"), dict) else {}
        sp = f.get("span") if isinstance(f.get("span"), dict) else {}
        cat = f.get("issue_category")
        keys = []
        if isinstance(sp.get("quote"), str):
            keys.append(("quote", cat, " ".join(sp["quote"].split())))
        if isinstance(se.get("replacement"), str):
            keys.append(("replacement", cat, " ".join(se["replacement"].split())))
        for k in keys:
            if k in seen and seen[k] != f["finding_id"]:
                warns.append(f"반복 쟁점 [{seen[k]}, {f['finding_id']}]: 같은 쟁점('{cat}')이 반복됩니다. 하나의 finding 으로 묶고 나머지 구절은 related_edits 에 넣으세요.")
            else:
                seen[k] = f["finding_id"]


def _sort_key(f: dict, order: dict[str, int]) -> tuple:
    sp = f.get("span") if isinstance(f.get("span"), dict) else {}
    stars = f.get("priority_stars")
    return (-(stars if isinstance(stars, int) and not isinstance(stars, bool) else 0),
            order.get(sp.get("block_id"), 10**6) if isinstance(sp.get("block_id"), str) else 10**6,
            sp.get("start") if isinstance(sp.get("start"), int) else 0)


def build_report_ex(
    article: dict,
    findings: list,
    explanations: dict | None,
    corpus: Any,
    *,
    now: _dt.datetime,
    raw_bytes: bytes | None = None,
    overall_status: str | None = None,
    coverage_notes: list[str] | None = None,
    ttl_seconds: float = config.DEFAULT_TTL_SECONDS,
    report_id: str | None = None,
) -> BuildResult:
    if not isinstance(now, _dt.datetime) or now.tzinfo is None:
        return BuildResult(None, ["now must be a timezone-aware datetime"])
    a_errs = contracts.validate_article(article)
    if a_errs:
        return BuildResult(None, [f"article: {m}" for m in a_errs])
    if not isinstance(findings, list):
        return BuildResult(None, ["findings must be a list"])
    if explanations is not None and not isinstance(explanations, dict):
        return BuildResult(None, ["explanations must be an object keyed by finding_id"])

    errs: list[str] = []
    warns: list[str] = []
    blocks = {b["block_id"]: b["text"] for b in article["blocks"]}
    order = {b["block_id"]: i for i, b in enumerate(article["blocks"])}
    norm = [_normalize_finding(i, f, blocks, corpus, errs, warns) for i, f in enumerate(findings)]
    good = [f for f in norm if f is not None]
    good.sort(key=lambda f: _sort_key(f, order))
    _flow_warnings(article, good, warns)

    rid = report_id or "rpt-" + uuid.uuid4().hex[:12]
    for f in good:
        f["detailed_explanation_ref"] = f"{rid}/{f['finding_id']}"

    expl: dict[str, dict] = {}
    ids = {f["finding_id"] for f in good}
    for k, v in (explanations or {}).items():
        if k not in ids:
            warns.append(f"explanation for unknown finding_id '{k}' ignored")
        elif not isinstance(v, dict) or any(not isinstance(x, _EXPLANATION_FIELDS) for x in v.values()):
            errs.append(f"explanations[{k}]: must be an object of strings or string lists")
        else:
            expl[k] = v

    _, hmode = hashing.article_sha256(article, raw_bytes)
    notes = list(coverage_notes or [])
    # 교차검증 6: 서버가 덧붙이는 안내문(해시 기준 등)은 범위 설명이 아니므로, 호출자가 준 coverage_notes 만 센다.
    if overall_status in ("partial", "insufficient_information") and not any(isinstance(n, str) and n.strip() for n in notes):
        errs.append(f"overall_status '{overall_status}' requires coverage_notes (a scope explanation from the caller)")
    c_status = corpus.status()
    legal_as_of = article["publication_date"]       # 분석 기준일 = 기사 발행(예정)일(P3_DESIGN §2). 색인 기준일과 따로 적는다
    if corpus.is_fixture:
        notes.append("합성(fixture) 코퍼스로 실행했습니다. 근거 passage는 실제 판결·결정의 원문이 아니며 법적 판단 근거로 쓸 수 없습니다.")
        notes += [str(w) for w in c_status.get("warnings", [])]
    elif c_status.get("index_built_at"):
        notes.append(corpus_sqlite.index_coverage_note(c_status, legal_as_of))
    # G-D23: 결정일도 수록 간행물 발행일도 없는 근거는 쓸 수 있지만 보고서에 그 사실을 남긴다
    undated = _undated_evidence(good, corpus)
    if undated:
        notes.append(f"근거 passage {', '.join(undated)} 는 결정일도 수록 간행물 발행일도 알 수 없어({contracts.DATE_UNKNOWN_MARK}) "
                     "기사 발행일 이전 자료인지 확인하지 못했습니다. 기준일 필터를 적용하지 못한 근거입니다.")
    notes.append(f"article_sha256 기준: {hmode}")
    if config.percentage_requested():
        notes.append("퍼센트 출력은 지원하지 않아 PRESS_DISPUTE_ENABLE_PERCENTAGE 설정을 무시했습니다.")
    if overall_status is None:
        if good:
            overall_status = "findings"
        elif corpus.is_fixture or c_status.get("passage_count", 0) == 0:
            overall_status = "partial"
            notes.append("검색 범위가 합성 코퍼스뿐이라 '검토 권고 항목 없음'으로 판단할 수 없습니다.")
        elif not (core := contracts.core_coverage(c_status))["no_actionable_findings_allowed"]:
            overall_status = "partial"       # G-D22: 색인 규모가 기준에 못 미치면 '문제 없음'을 쓰지 않는다
            notes.append(core["reason"])
        else:
            overall_status = "no_actionable_findings"
            notes.append("현재 입력과 코퍼스 범위에서 검토 권고 항목을 찾지 못했습니다. 법적 안전을 보장하지 않습니다.")

    report = {
        "schema_version": config.SCHEMA_VERSION,
        "execution_mode": "synthetic_example" if corpus.is_fixture else "live",
        "report_id": rid,
        "article_id": article["article_id"],
        "article_sha256": hashing.article_sha256(article, raw_bytes)[0],
        "policy_version": config.POLICY_VERSION,
        "corpus_snapshot_id": corpus.snapshot_id,
        "analyzed_at": now.isoformat(timespec="seconds"),
        "legal_as_of": legal_as_of,
        "overall_status": overall_status,
        "coverage_notes": notes,
        "retention": {"mode": "ephemeral", "expires_at": (now + _dt.timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")},
        "findings": good,
        "hidden_finding_count": max(0, len(good) - config.DISPLAY_TOP_N),
    }
    if errs:
        return BuildResult(None, errs, warns, expl)
    v_errs = contracts.validate_report(report, article, corpus, raw_bytes=raw_bytes)
    if v_errs:
        return BuildResult(None, v_errs, warns, expl)
    return BuildResult(report, [], warns, expl)


def build_report(article, findings, explanations, corpus, *, now, raw_bytes=None, **kw) -> tuple[dict | None, list[str]]:
    r = build_report_ex(article, findings, explanations, corpus, now=now, raw_bytes=raw_bytes, **kw)
    return r.report, r.errors


# ---------------------------------------------------------------- MCP 도구 동작

_ARTICLE_PREFIX = "article:"


def _forget_expired(key: str) -> None:
    """보관소에서 원고가 사라지면 그 원고의 로그 색인(8자 조각)도 지운다."""
    if key.startswith(_ARTICLE_PREFIX):
        handle = key[len(_ARTICLE_PREFIX):]
        logsafe.forget_article(handle)
        logsafe.forget_prefix(handle + revision.REV_SUFFIX)


def new_store(ttl_seconds: float = config.DEFAULT_TTL_SECONDS, clock: Any = None) -> EphemeralStore:
    """원고 로그 색인 정리가 연결된 보관소. 원고·보고서 합계는 ``config.MAX_STORE_ITEMS`` 건으로 제한한다."""
    kw: dict[str, Any] = {"ttl_seconds": ttl_seconds, "on_expire": _forget_expired, "max_items": config.MAX_STORE_ITEMS}
    if clock is not None:
        kw["clock"] = clock
    return EphemeralStore(**kw)


def prepare_article(
    store: Any,
    *,
    text: str | None = None,
    article: dict | None = None,
    publication_date: str | None = None,
    genre: str | None = None,
    election_context: str | None = None,
) -> dict:
    if (text is None) == (article is None):
        return {"ok": False, "errors": ["give exactly one of text or article"]}
    warnings: list[str] = []
    if text is not None:
        if not isinstance(text, str):
            return {"ok": False, "errors": ["text must be a string"]}
        try:
            art, warnings = parse_plaintext_ex(text, publication_date=publication_date, genre=genre, election_context=election_context)
        except ValueError as e:
            return {"ok": False, "errors": [str(e)]}
    else:
        if not isinstance(article, dict):
            return {"ok": False, "errors": ["article must be an object"]}
        art = copy.deepcopy(article)
        ctx = art.get("context")
        if isinstance(ctx, dict):
            if genre:
                ctx["genre"] = genre
            if election_context is not None:
                ctx["election_context"] = election_context
        if publication_date:
            art["publication_date"] = publication_date
    errs = contracts.validate_article(art)
    if errs:
        return {"ok": False, "errors": errs}
    digest, mode = hashing.article_sha256(art)
    handle = "art-" + uuid.uuid4().hex[:12]
    store.put(f"article:{handle}", {"article": art, "raw_bytes": None, "sha256": digest, "hash_mode": mode})
    logsafe.register_article(handle, art)
    return {
        "ok": True,
        "article_handle": handle,
        "article_sha256": digest,
        "hash_mode": mode,
        "blocks": [{"block_id": b["block_id"], "section": b["section"], "text": b["text"], "length_cp": len(b["text"])} for b in art["blocks"]],
        "warnings": warnings,
        "expires_in_seconds": store.ttl_seconds,
    }


def search_cases(
    corpus: Any,
    *,
    query: str,
    authority_types: list[str] | None = None,
    risk_channel: str | None = None,
    as_of: str | None = None,
    limit: int = 5,
    institution: str | None = None,
    stance: str = "both",
) -> dict:
    try:
        passages = corpus.search(query, authority_types=authority_types, risk_channel=risk_channel, as_of=as_of, limit=limit,
                                 institution=institution, stance=stance)
    except ValueError as e:
        return {"ok": False, "errors": [str(e)]}
    out = {"ok": True, "count": len(passages), "snapshot_id": corpus.snapshot_id, "is_fixture": corpus.is_fixture, "stance": stance, "passages": passages}
    if not passages:
        out["message"] = "코퍼스에서 해당 조건에 맞는 자료를 찾지 못했습니다. 없는 근거를 만들지 마세요."
    if any(p.get("date_unknown") for p in passages) and not corpus.is_fixture:
        out["date_rule"] = ("date_unknown 인 passage 는 결정일도 수록 간행물 발행일도 없어 as_of 필터에서 빠지지 않았다. 기사 발행일 이후 자료일 수 있으니 "
                            "근거로 쓰면 보고서 coverage_notes 에 날짜를 확인하지 못했다고 적는다(서버가 자동으로도 남긴다).")
    if passages and any(p.get("display_truncated") for p in passages):
        out["citation_rule"] = (f"text 는 내부 판단용 전문이다. 사용자 화면에 원문을 인용할 때는 display_text 만 쓴다"
                                f"(재배포 허락이 없는 소스는 {corpus_sqlite.DISPLAY_QUOTE_MAX_CP}자 이내).")
    return out


def submit_report(
    store: Any,
    corpus: Any,
    *,
    article_handle: str,
    findings: list,
    explanations: dict | None = None,
    overall_status: str | None = None,
    coverage_notes: list[str] | None = None,
    now: _dt.datetime | None = None,
) -> dict:
    entry = store.get(f"article:{article_handle}") if isinstance(article_handle, str) else None
    if entry is None:
        return {"ok": False, "errors": ["article_handle is unknown or expired; call prepare_article again with the manuscript"]}
    now = now or _dt.datetime.now().astimezone()
    # 보고서(원고 인용 포함)가 원고보다 늦게 만료돼 원고 조각이 남지 않도록 보고서 만료를 원고 만료에 맞춘다(F3)
    left = store.remaining_ttl(f"article:{article_handle}")
    if left is None:             # 방금 만료: 보고서에 새 TTL 을 주지 않는다
        return {"ok": False, "errors": ["article_handle is unknown or expired; call prepare_article again with the manuscript"]}
    report_ttl = min(store.ttl_seconds, left)
    res = build_report_ex(
        entry["article"], findings, explanations, corpus, now=now, raw_bytes=entry.get("raw_bytes"),
        overall_status=overall_status, coverage_notes=coverage_notes, ttl_seconds=report_ttl,
    )
    if res.report is None:
        return {"ok": False, "errors": res.errors, "warnings": res.warnings}
    table = render.render_table(res.report)             # 렌더가 실패해도 보고서가 보관소에 남지 않도록 먼저 한다
    store.put(f"report:{res.report['report_id']}", {"report": res.report, "explanations": res.explanations, "article_handle": article_handle},
              ttl_seconds=report_ttl)
    if store.get(f"article:{article_handle}") is None:       # 저장 직후 원고가 만료된 경쟁: 보고서만 남기지 않는다
        store.delete(f"report:{res.report['report_id']}")
        return {"ok": False, "errors": ["article_handle is unknown or expired; call prepare_article again with the manuscript"]}
    return {
        "ok": True,
        "report_id": res.report["report_id"],
        "table": table,
        "warnings": res.warnings,
        "hidden_finding_count": res.report["hidden_finding_count"],
        "expires_at": res.report["retention"]["expires_at"],
    }


def explain_finding(store: Any, *, report_id: str, finding_id: str) -> dict:
    entry = store.get(f"report:{report_id}") if isinstance(report_id, str) else None
    if entry is None:
        return {"ok": False, "message": "보고서가 없거나 보관 시간(세션 메모리 TTL)이 끝났습니다. 원고를 다시 제출해 분석해 주세요."}
    try:
        md = render.render_detail(entry["report"], finding_id, entry["explanations"].get(finding_id))
    except KeyError:
        return {"ok": False, "message": f"finding_id '{finding_id}' 가 이 보고서에 없습니다."}
    return {"ok": True, "markdown": md}


def build_revision(store: Any, *, report_id: str, article_sha256: str, finding_ids: list[str] | None = None) -> dict:
    return revision.build_revision(store, report_id=report_id, article_sha256=article_sha256, finding_ids=finding_ids)


def corpus_status(corpus: Any) -> dict:
    return {"ok": True, **corpus.status()}

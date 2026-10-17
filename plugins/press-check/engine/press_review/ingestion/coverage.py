"""커버리지 인벤토리(docs/coverage/P1_inventory.md)와 권리 장부(rights_ledger.json) 생성. 수치는 전부 manifest 질의 결과다.

장부에 저장되지 않는 값은 만들지 않고 '장부 미저장'으로 표기한다(예: PAC 게시판·IENDC 의 화면 표시 총수는 어댑터 실행 보고에만 있다).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .. import contracts
from . import rights as rights_mod

PAGE_SIZE = 10  # PAC 간행물 목록 페이지 크기(sources/pac.py)


def _year_dist(m: Any, sid: str) -> dict:
    rows = m.coverage_rows("SELECT year, COUNT(*) FROM documents WHERE source_id=? GROUP BY year ORDER BY year IS NULL, year", (sid,))
    return {("미상" if r[0] is None else str(r[0])): r[1] for r in rows}


def _decade_dist(m: Any, sid: str) -> dict:
    rows = m.coverage_rows("SELECT (year/10)*10, COUNT(*) FROM documents WHERE source_id=? AND year IS NOT NULL "
                           "GROUP BY 1 ORDER BY 1", (sid,))
    return {f"{r[0]}s": r[1] for r in rows}


TIER_DEFINITION = ("P-T1 = P2_DESIGN §8 규칙(핵심어 적중, 또는 언론어와 인격권어 동시 적중)을 현재 장부에 적용한 결과이고, 참조판례 확장으로 늘어난 판례도 P-T1 로 처리한다(G-D20). "
                   "건수는 이 장부 질의의 결과이며 문서에 고정 건수를 적지 않는다.")
TIER_KEYS = ("P-T1", "P-T2", "P-T3")


def processing_tiers(m: Any, sid: str) -> Optional[dict]:
    """판례 처리 순위 건수(G-D20): 장부의 ``extra.processing_tier``(확장분 포함)와 ``extra.tier``(§8 규칙만)를 질의한다. 순위가 매겨진 문서가 없으면 None."""
    q = m.coverage_rows
    rows = q("SELECT COALESCE(json_extract(extra_json,'$.processing_tier'),'none'), COALESCE(json_extract(extra_json,'$.source_scope'),'unknown'), "
             "COALESCE(json_extract(extra_json,'$.tier'),'none'), status, "
             "COALESCE(json_array_length(json_extract(extra_json,'$.expanded_from')),0), COUNT(*) FROM documents WHERE source_id=? "
             "GROUP BY 1,2,3,4,5", (sid,))
    if not any(r[0] != "none" for r in rows):
        return None
    counts = {k: 0 for k in TIER_KEYS}
    by_scope: dict = {}
    by_status: dict = {}
    by_rule = expanded_only = expanded = 0
    for ptier, scope, rule_tier, status, nexp, n in rows:
        counts[ptier] = counts.get(ptier, 0) + n
        by_scope.setdefault(scope, {})
        by_scope[scope][ptier] = by_scope[scope].get(ptier, 0) + n
        if nexp:
            expanded += n
        if ptier == "P-T1":
            by_status[status] = by_status.get(status, 0) + n
            if rule_tier == "P-T1":
                by_rule += n
            else:
                expanded_only += n
    if not counts.get("none"):
        counts.pop("none", None)
    return {"definition": TIER_DEFINITION, "counts": counts, "p_t1_total": counts["P-T1"], "p_t1_by_rule": by_rule, "p_t1_expanded_only": expanded_only,
            "expanded_documents": expanded, "p_t1_by_status": dict(sorted(by_status.items())),
            "by_scope": {k: dict(sorted(v.items())) for k, v in sorted(by_scope.items())}}


def source_coverage(m: Any, src: dict) -> dict:
    sid = src["source_id"]
    q = m.coverage_rows
    lp = q("SELECT COUNT(*), COALESCE(SUM(ok),0), COALESCE(SUM(item_count),0), COALESCE(MAX(page_no),0) "
           "FROM list_pages WHERE source_id=?", (sid,))[0]
    pages = [(r[0], r[1], r[2]) for r in q("SELECT page_no, item_count, ok FROM list_pages WHERE source_id=? ORDER BY page_no, url", (sid,))]
    docs = q("SELECT COUNT(*) FROM documents WHERE source_id=?", (sid,))[0][0]
    by_status = {r[0]: r[1] for r in q("SELECT status, COUNT(*) FROM documents WHERE source_id=? GROUP BY status", (sid,))}
    att = q("SELECT COUNT(*), COALESCE(SUM(content_length),0), "
            "COALESCE(SUM(CASE WHEN head_checked_at IS NOT NULL THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN content_length IS NOT NULL THEN 1 ELSE 0 END),0) FROM attachments "
            "WHERE doc_id IN (SELECT doc_id FROM documents WHERE source_id=?)", (sid,))[0]
    ctypes = {str(r[0]): r[1] for r in q("SELECT content_type, COUNT(*) FROM attachments WHERE doc_id IN "
                                         "(SELECT doc_id FROM documents WHERE source_id=?) GROUP BY content_type", (sid,))}
    fclause = "(target IN (SELECT doc_id FROM documents WHERE source_id=?) OR target=?)"
    fo = q(f"SELECT error_class, COUNT(*) FROM failures WHERE resolved_at IS NULL AND {fclause} GROUP BY error_class", (sid, sid))
    fr = q(f"SELECT COUNT(*) FROM failures WHERE resolved_at IS NOT NULL AND {fclause}", (sid, sid))[0][0]
    pages_ok = [p for p in pages if p[2]]
    last_page_no = max((p[0] for p in pages_ok), default=0)
    non_last_full = all(p[1] == PAGE_SIZE for p in pages_ok if p[0] != last_page_no)
    in_range = (last_page_no - 1) * PAGE_SIZE < docs <= last_page_no * PAGE_SIZE if last_page_no else False
    tiers = processing_tiers(m, sid) if src["source_family"] == "law_go_kr" else None
    return {
        **({"processing_tiers": tiers} if tiers else {}),
        "source_id": sid, "source_family": src["source_family"], "title": src["title"], "enabled": bool(src["enabled"]),
        "checked_at": src["checked_at"],
        "list_pages": {"count": lp[0], "ok": lp[1], "max_page_no": lp[3], "item_count_sum": lp[2]},
        "documents": {"count": docs, "by_status": by_status},
        "attachments": {"count": att[0], "head_checked": att[2], "with_size": att[3], "total_bytes": att[1],
                        "content_types": ctypes},
        "years": _year_dist(m, sid), "decades": _decade_dist(m, sid),
        "failures_open": {r[0]: r[1] for r in fo}, "failures_resolved": fr,
        "screen_total_check": {
            "item_sum_equals_documents": lp[2] == docs,
            "page_range_check": {"non_last_pages_full": non_last_full, "documents_in_last_page_range": in_range},
        },
    }


def law_extra(inventory_dir: Optional[Path]) -> Optional[dict]:
    """lawapi 진행 요약(corpus/inventory/lawgo_prec_terms.json)에서 대조 수치를 읽는다. 없으면 None."""
    if not inventory_dir:
        return None
    p = Path(inventory_dir) / "lawgo_prec_terms.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    terms = d.get("terms", {})
    return {"terms": len(terms), "terms_complete": sum(1 for t in terms.values() if t.get("status") == "complete"),
            "terms_split_by_year": sum(1 for t in terms.values() if t.get("split")),
            "probe_all": d.get("probe_all"), "seeds_found": sum(1 for s in d.get("seeds", []) if s.get("found")),
            "seeds": len(d.get("seeds", []))}


def build_coverage(m: Any, generated_at: str, inventory_dir: Optional[Path] = None) -> dict:
    out = [source_coverage(m, s) for s in m.sources()]
    law = law_extra(inventory_dir)
    if law:
        for s in out:
            if s["source_family"] == "law_go_kr":
                docs = s["documents"]["count"]
                s["law_cross_check"] = {**law, "documents_union": docs,
                                        "not_matched_by_any_term": (law["probe_all"] or {}).get("all", 0) - docs}
    return {"generated_at": generated_at, "sources": out, "not_collected": rights_mod.not_collected_rows()}


# ---------------------------------------------------------------- markdown
def _future_label(pa: dict) -> str:
    """미래 선고일 탐침 문구. 탐침 시작일(오늘 다음 날)을 함께 적는다. 구 형식(after_this_year)이면 값을 만들지 않는다."""
    if pa.get("after_today") is None:
        return "오늘 이후 선고일 탐침 미실행(재실행 필요)"
    return f"{pa.get('after_today_from', '?')} 이후(오늘 다음 날부터) 선고일 {_n(pa['after_today'])}건"


def _n(x: int) -> str:
    return f"{x:,}"


def _dist(d: dict, limit: int = 40) -> str:
    items = [f"{k}:{_n(v)}" for k, v in d.items()]
    return " ".join(items[:limit]) + (" ..." if len(items) > limit else "")


def render_markdown(cov: dict) -> str:
    L = ["# P1 인벤토리", "",
         f"생성 {cov['generated_at']} (UTC). 이 파일은 `python -m press_review.ingestion export-coverage` 가 manifest.sqlite 질의로 만든다. 수치를 손으로 고치지 않는다.", "",
         "## 소스별 수치", "",
         "| 소스 | 목록 페이지(ok/전체) | 문서 | 첨부 | 첨부 총 바이트 | HEAD 확인 | 실패(열림/해결) | 확인 시각 |",
         "|---|---|---|---|---|---|---|---|"]
    tot = {"p": 0, "pok": 0, "d": 0, "a": 0, "b": 0, "h": 0}
    for s in cov["sources"]:
        lp, a, fo = s["list_pages"], s["attachments"], s["failures_open"]
        L.append(f"| `{s['source_id']}` {s['title']} | {_n(lp['ok'])}/{_n(lp['count'])} | {_n(s['documents']['count'])} | "
                 f"{_n(a['count'])} | {_n(a['total_bytes'])} | {_n(a['head_checked'])} | "
                 f"{_n(sum(fo.values()))}/{_n(s['failures_resolved'])} | {s['checked_at']} |")
        tot["p"] += lp["count"]; tot["pok"] += lp["ok"]; tot["d"] += s["documents"]["count"]
        tot["a"] += a["count"]; tot["b"] += a["total_bytes"]; tot["h"] += a["head_checked"]
    L += [f"| 합계 | {_n(tot['pok'])}/{_n(tot['p'])} | {_n(tot['d'])} | {_n(tot['a'])} | {_n(tot['b'])} | {_n(tot['h'])} | | |", ""]
    L += ["## 화면 표시 총수 대조", "",
          "| 소스 | 목록 항목 합 | 문서 수 | 합=문서 | 마지막 쪽 외 전부 10건 | 문서 수가 마지막 쪽 범위 안 |", "|---|---|---|---|---|---|"]
    for s in cov["sources"]:
        c, lp = s["screen_total_check"], s["list_pages"]
        r = c["page_range_check"]
        if s["source_family"] == "law_go_kr":  # 질의어×기간 목록이라 합이 문서 수와 같을 수 없다. 아래 '법제처 대조'를 본다.
            L.append(f"| `{s['source_id']}` | {_n(lp['item_count_sum'])} | {_n(s['documents']['count'])} | 해당 없음 | 해당 없음 | 해당 없음 |")
            continue
        if lp["max_page_no"] == 0 or lp["count"] == 1 and s["documents"]["count"] == lp["item_count_sum"] and s["source_id"].startswith("pac_standards"):
            L.append(f"| `{s['source_id']}` | {_n(lp['item_count_sum'])} | {_n(s['documents']['count'])} | "
                     f"{'예' if c['item_sum_equals_documents'] else '아니오'} | 해당 없음 | 해당 없음 |")
            continue
        L.append(f"| `{s['source_id']}` | {_n(lp['item_count_sum'])} | {_n(s['documents']['count'])} | "
                 f"{'예' if c['item_sum_equals_documents'] else '아니오'} | {'예' if r['non_last_pages_full'] else '아니오'} | "
                 f"{'예' if r['documents_in_last_page_range'] else '아니오'} |")
    L += ["", "화면에 표시된 총 건수 자체는 PAC 게시판·IENDC 에서 장부에 저장되지 않는다(어댑터 실행 때 확인만 했다). 이 표의 '마지막 쪽 범위'는 간행물 목록에만 의미가 있고, 게시판·고정 페이지·법제처 행은 참고값이다.", ""]
    for s in cov["sources"]:
        x = s.get("law_cross_check")
        if x:
            pa = x["probe_all"] or {}
            L += ["### 법제처 대조", "",
                  f"- 사전 {x['terms']}개 중 완료 {x['terms_complete']}개, 연 단위 분할 {x['terms_split_by_year']}개",
                  f"- 문서 합집합 {_n(x['documents_union'])}건, 질의어 없는 전체 조회 {_n(pa.get('all', 0))}건, 어느 질의어에도 안 걸린 {_n(x['not_matched_by_any_term'])}건",
                  f"- 1900년 이전 선고일 {_n(pa.get('before_1900', 0))}건, {_future_label(pa)}",
                  f"- 시드 {x['seeds_found']}/{x['seeds']}건 확인", ""]
    for s in cov["sources"]:
        t = s.get("processing_tiers")
        if t:
            c = t["counts"]
            L += [f"## 판례 처리 순위 (`{s['source_id']}`, 장부 질의)", "", t["definition"], "",
                  "| 순위 | 문서 수 |", "|---|---|"]
            L += [f"| {k} | {_n(v)} |" for k, v in c.items()]
            L += ["", f"- P-T1 {_n(t['p_t1_total'])}건 = §8 규칙 적중 {_n(t['p_t1_by_rule'])}건 + 참조판례 확장만으로 P-T1 이 된 {_n(t['p_t1_expanded_only'])}건 (확장 문서 전체 {_n(t['expanded_documents'])}건)",
                  f"- P-T1 문서 상태: {_dist(t['p_t1_by_status'])}",
                  "- 출처 범위별 순위: " + "; ".join(f"{k} {_dist(v)}" for k, v in t["by_scope"].items()), ""]
    L += ["## 연도 분포", ""]
    for s in cov["sources"]:
        if len(s["years"]) > 40:
            L += [f"- `{s['source_id']}` (10년 단위): {_dist(s['decades'])}; 연도 미상 {_n(s['years'].get('미상', 0))}"]
        else:
            L += [f"- `{s['source_id']}`: {_dist(s['years']) or '문서 없음'}"]
    L += ["", "## 실패·보류", "", "| 소스 | 열린 실패(유형:건수) | 해결됨 | 문서 상태 |", "|---|---|---|---|"]
    for s in cov["sources"]:
        L.append(f"| `{s['source_id']}` | {_dist(s['failures_open']) or '0'} | {s['failures_resolved']} | {_dist(s['documents']['by_status'])} |")
    L += ["", "## 수집하지 않음·보류", "", "| 소스 | 상태 | 권리 | 사유 |", "|---|---|---|---|"]
    for r in cov["not_collected"]:
        vals = sorted(set(r["rights"].values()))
        L.append(f"| {r['title']} | {'수집 안 함' if r['status'] == 'not_collected' else '보류'} | {'/'.join(vals)} | {r['reason']} |")
    L.append("")
    return "\n".join(L)


def write_json(path: Path, obj: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def export_coverage(m: Any, out_dir: Path, generated_at: str, inventory_dir: Optional[Path] = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    cov = build_coverage(m, generated_at, inventory_dir)
    ledger = rights_mod.export_ledger(m, generated_at, validate=contracts.validate_source_record)
    md = out_dir / "P1_inventory.md"
    md.write_text(render_markdown(cov), encoding="utf-8")
    lj = out_dir / "rights_ledger.json"
    write_json(lj, ledger)
    invalid = sum(s.get("record_validation", {}).get("invalid", 0) for s in ledger["sources"])
    checked = sum(s.get("record_validation", {}).get("checked", 0) for s in ledger["sources"])
    return {"inventory": str(md), "ledger": str(lj), "sources": len(cov["sources"]), "records_checked": checked,
            "records_invalid": invalid}

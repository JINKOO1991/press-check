"""build_revision: 저장된 보고서의 안전한 replace 수정안만 원문에 결정적으로 적용해 변경본(블록 재조립본)을 만든다.

- 읽기 전용. 보관된 원고·보고서를 바꾸지 않고 디스크에 쓰지 않는다. 대체문은 입력으로 받지 않는다(보고서에 저장된 것만 쓴다).
- 후보: priority_stars >= 2, mode 'replace', replacement 비어 있지 않음. finding 단위로 주 span 과 related_edits 를 묶어 모두 적용하거나 모두 적용하지 않는다.
- 적용 전 재검사: quote 가 저장 블록 text[start:end] 와 같아야 하고(QUOTE_MISMATCH), edit_checks.check_replacement 가 오류든 경고든 이슈를 내면 탈락한다.
- 겹침(OVERLAP)은 contracts 의 구간 규칙과 같다: 같은 블록에서 시작이 앞선 구간의 끝보다 작으면 겹침, 인접은 허용.
  적용 순서는 별 많은 순, 같으면 finding_id 순이고 나중 finding 이 통째로 탈락한다. ``_apply_edits`` 는 적용 결과·좌표를 돌려주지 않아 쓰지 않고 같은 규칙을 여기서 구현했다.
- 오류·notes·skipped.detail 에는 원문 문장을 넣지 않는다(이슈 코드와 span 라벨만). 변경본 텍스트는 로그 마스킹 색인에 등록한다.
"""
from __future__ import annotations

import copy
from collections import Counter
from typing import Any

from . import edit_checks, hashing, logsafe

REV_SUFFIX = "~rev"        # 변경본 마스킹 색인 키: 원고 handle + 접미사(원고당 1개). 원고가 만료되면 함께 지운다(review._forget_expired).
MIN_STARS = 2


def _fail(code: str, message: str) -> dict:
    return {"ok": False, "error_code": code, "errors": [f"[{code}] {message}"]}


def _is_int(x: Any) -> bool:
    return type(x) is int


def _edits_of(f: dict) -> list[tuple[str, Any, Any]] | None:
    """(라벨, span, replacement) 목록. 모양이 틀리면 None."""
    se = f.get("suggested_edit")
    if not isinstance(se, dict):
        return None
    jobs: list[tuple[str, Any, Any]] = [("span", f.get("span"), se.get("replacement"))]
    rel = se.get("related_edits") or []
    if not isinstance(rel, list):
        return None
    for j, r in enumerate(rel):
        if not isinstance(r, dict):
            return None
        jobs.append((f"related_edits[{j}]", r.get("span"), r.get("replacement")))
    return jobs


def _ineligible(f: dict) -> str | None:
    se = f.get("suggested_edit") if isinstance(f.get("suggested_edit"), dict) else {}
    stars = f.get("priority_stars")
    if not _is_int(stars) or stars < MIN_STARS:
        return "STARS_BELOW_2"
    if se.get("mode") != "replace":
        return "MODE_NOT_REPLACE"
    if not isinstance(se.get("replacement"), str) or not se["replacement"]:
        return "REPLACEMENT_EMPTY"
    return None


def _overlaps(taken: dict[str, list[tuple[int, int]]], bid: str, s: int, e: int) -> bool:
    return any(s < te and ts < e for ts, te in taken.get(bid, ()))


def _direct_inners(text: str) -> list[str]:
    return [text[a + 1:z - 1] for a, z, kind in edit_checks.scan_quotes(text) if kind == "direct"]


def _assembled_violation(article: dict, texts: dict[str, str], trial: dict[str, list[dict]]) -> str | None:
    """잠정 수락 집합을 조립한 결과를 원본 전체 기사와 대조한다. 위반 코드 또는 None.

    숫자: 조립된 블록의 숫자 토큰이 원본 전체에 있어야 한다. 인용: 직접 인용 안쪽 문자열 다중집합이 원본의 부분집합이어야 한다.
    짝: 원본 블록에 없던 짝 없는 직접 인용 부호가 새로 생기면 안 된다.
    """
    asm = dict(texts)
    for bid, items in trial.items():
        out, pos = [], 0
        for e in sorted(items, key=lambda e: e["start"]):
            out += [texts[bid][pos:e["start"]], e["replacement"]]
            pos = e["end"]
        out.append(texts[bid][pos:])
        asm[bid] = "".join(out)
    # 같은 토크나이저로 원본 전체의 토큰 집합을 만들어 부분집합을 본다(num_in 의 경계 판정은 2026.10.17 같은 원문 토큰을 못 찾는다).
    # 한계: 한글 수량(여러 명 -> 두 명)과 작은따옴표 강조는 보지 않는다.
    orig_full = edit_checks._nocomma("\n".join(texts.values()))
    orig_tokens, orig_pairs = set(edit_checks._NUM.findall(orig_full)), edit_checks._num_unit_pairs(orig_full)
    for bid in trial:
        if set(edit_checks._NUM.findall(edit_checks._nocomma(asm[bid]))) - orig_tokens:
            return "NEW_NUMBER_ASSEMBLED"
    for bid in trial:
        if edit_checks._num_unit_pairs(asm[bid]) - orig_pairs:
            return "NEW_NUMBER_UNIT_ASSEMBLED"

    def inner_counts(tx: dict[str, str]) -> Counter:
        resets, pos, prev, parts = set(), 0, None, []
        for b in article["blocks"]:
            if prev is not None and b.get("section") != prev:
                resets.add(pos)
            prev = b.get("section")
            parts.append(tx[b["block_id"]])
            pos += len(tx[b["block_id"]]) + 1
        full = "\n".join(parts)
        return Counter(full[a + 1:z - 1] for a, z, kind in edit_checks.scan_quotes(full, resets) if kind == "direct")

    if inner_counts(asm) - inner_counts(texts):
        return "NEW_QUOTE_ASSEMBLED"
    for bid in trial:
        if edit_checks.has_unpaired_direct_mark(asm[bid]) and not edit_checks.has_unpaired_direct_mark(texts[bid]):
            return "UNPAIRED_QUOTE_ASSEMBLED"
    full = "\n".join(texts.values())
    for bid in trial:
        if edit_checks.count_negations(asm[bid]) != edit_checks.count_negations(texts[bid]):
            return "NEGATION_ASSEMBLED"
    for bid in trial:
        if any(c not in full for c in edit_checks.proper_noun_candidates(asm[bid])):
            return "NEW_PROPER_NOUN_ASSEMBLED"
    return None


def build_revision(store: Any, *, report_id: str, article_sha256: str, finding_ids: list[str] | None = None) -> dict:
    entry = store.get(f"report:{report_id}") if isinstance(report_id, str) else None
    if entry is None:
        return _fail("REPORT_NOT_FOUND_OR_EXPIRED", "보고서가 없거나 보관 시간(세션 메모리 TTL)이 끝났습니다. 원고를 다시 제출해 분석해 주세요.")
    report = entry["report"]
    art_entry = store.get(f"article:{entry.get('article_handle')}")
    if art_entry is None:
        return _fail("ARTICLE_EXPIRED", "원고가 보관소에서 먼저 사라졌습니다. 원고를 다시 제출해 분석해 주세요.")
    article = art_entry["article"]
    original_sha = report.get("article_sha256")
    if article_sha256 != original_sha or hashing.article_sha256(article, art_entry.get("raw_bytes"))[0] != original_sha:
        return _fail("HASH_MISMATCH", "article_sha256 이 보고서가 분석한 원고의 해시와 다릅니다. prepare_article 이 돌려준 값을 그대로 보내세요.")

    texts = {b["block_id"]: b["text"] for b in article["blocks"]}
    order = {b["block_id"]: i for i, b in enumerate(article["blocks"])}
    check_article = {"blocks": [dict(b) for b in article["blocks"]]}
    findings = {f["finding_id"]: f for f in report.get("findings", []) if isinstance(f, dict) and isinstance(f.get("finding_id"), str)}

    skipped: dict[str, dict] = {}

    def skip(fid: str, code: str, detail: str = "") -> None:
        skipped[fid] = {"finding_id": fid, "reason_code": code, "detail": detail}

    if finding_ids is None:
        chosen = sorted(findings)
    else:
        chosen = []
        for fid in dict.fromkeys(finding_ids):
            if fid not in findings:
                skip(fid, "UNKNOWN_FINDING_ID", "보고서에 없는 finding_id")
            else:
                chosen.append(fid)

    candidates: list[str] = []
    for fid in chosen:
        why = _ineligible(findings[fid])
        if why is None:
            candidates.append(fid)
        elif finding_ids is not None:
            skip(fid, why, "자격 규칙: 별 2개 이상, mode replace, 비어 있지 않은 replacement")
    candidates.sort(key=lambda fid: (-findings[fid]["priority_stars"], fid))

    taken: dict[str, list[tuple[int, int]]] = {}
    accepted: list[tuple[str, list[dict]]] = []
    for fid in candidates:
        f = findings[fid]
        jobs = _edits_of(f)
        edits: list[dict] = []
        bad = jobs is None
        for label, sp, rep in (jobs or []):
            if not (isinstance(sp, dict) and sp.get("block_id") in texts and _is_int(sp.get("start")) and _is_int(sp.get("end"))
                    and 0 <= sp["start"] < sp["end"] <= len(texts[sp["block_id"]]) and isinstance(sp.get("quote"), str) and isinstance(rep, str)):
                bad = True
                break
            edits.append({"label": label, "block_id": sp["block_id"], "start": sp["start"], "end": sp["end"], "quote": sp["quote"], "replacement": rep})
        if bad:
            skip(fid, "EDIT_INVALID", "수정 구간 형식이 올바르지 않음")
            continue
        if any(texts[e["block_id"]][e["start"]:e["end"]] != e["quote"] for e in edits):
            skip(fid, "QUOTE_MISMATCH", "quote 가 저장된 블록 text 구간과 다름")
            continue
        found: list[tuple[str, str]] = []
        for e in edits:
            for i in edit_checks.check_replacement(e["quote"], e["replacement"], check_article, block_id=e["block_id"], start=e["start"]):
                found.append((e["label"], i.code))
        if found:
            skip(fid, found[0][1], ",".join(dict.fromkeys(f"{lb}:{cd}" for lb, cd in found)))
            continue
        local: dict[str, list[tuple[int, int]]] = {}
        clash = False
        for e in edits:
            if _overlaps(taken, e["block_id"], e["start"], e["end"]) or _overlaps(local, e["block_id"], e["start"], e["end"]):
                clash = True
                break
            local.setdefault(e["block_id"], []).append((e["start"], e["end"]))
        if clash:
            skip(fid, "OVERLAP", "다른 적용 수정과 같은 블록에서 구간이 겹침")
            continue
        trial = {bid: [x for _, es in accepted for x in es if x["block_id"] == bid] + [e for e in edits if e["block_id"] == bid] for bid in local}
        bad_asm = _assembled_violation(article, texts, trial)
        if bad_asm:
            skip(fid, bad_asm, "수정을 조립한 결과가 원본 기사에 없던 숫자·직접 인용·인용 부호를 만듦")
            continue
        if any(Counter(_direct_inners(e["replacement"])) - Counter(_direct_inners(e["quote"])) for e in edits):
            skip(fid, "NEW_QUOTE_IN_EDIT", "수정 대체문에 원래 구간에 없던 직접 인용이 생김")
            continue
        for bid, spans in local.items():
            taken.setdefault(bid, []).extend(spans)
        accepted.append((fid, edits))

    # 변경본 좌표: 블록별로 시작 순 적용, 앞선 수정의 길이 차이를 누적한다
    per_block: dict[str, list[dict]] = {}
    for _, edits in accepted:
        for e in edits:
            per_block.setdefault(e["block_id"], []).append(e)
    revised = dict(texts)
    for bid, items in per_block.items():
        items.sort(key=lambda e: e["start"])
        out, pos, delta = [], 0, 0
        for e in items:
            out.append(texts[bid][pos:e["start"]])
            out.append(e["replacement"])
            e["revised_start"] = e["start"] + delta
            e["revised_end"] = e["revised_start"] + len(e["replacement"])
            delta += len(e["replacement"]) - (e["end"] - e["start"])
            pos = e["end"]
        out.append(texts[bid][pos:])
        revised[bid] = "".join(out)

    revised_article = copy.deepcopy(article)
    for b in revised_article["blocks"]:
        b["text"] = revised[b["block_id"]]
    revision_sha = hashing.article_sha256(revised_article)[0]
    if accepted:
        rev_key = f"{entry['article_handle']}{REV_SUFFIX}"      # 원고당 키 하나: 변경본 조각을 합집합으로 누적해 전역 색인 칸을 1개만 쓴다
        logsafe.register_article(rev_key, revised_article, extend=True)
        if store.get(f"article:{entry['article_handle']}") is None:       # 등록 중에 원고가 만료됐으면 방금 만든 색인을 바로 푼다
            logsafe.forget_article(rev_key)
            return _fail("ARTICLE_EXPIRED", "변경본을 만드는 동안 원고 보관 시간이 끝났습니다. 원고를 다시 제출해 분석해 주세요.")

    applied = []
    for fid, edits in accepted:
        es = sorted(edits, key=lambda e: (order[e["block_id"]], e["start"]))
        applied.append({
            "finding_id": fid, "priority_stars": findings[fid]["priority_stars"],
            "edits": [{"block_id": e["block_id"], "start": e["start"], "end": e["end"], "original": e["quote"],
                       "replacement": e["replacement"], "revised_start": e["revised_start"], "revised_end": e["revised_end"]} for e in es],
        })
    applied_ids = {fid for fid, _ in accepted}
    proposals = []
    for fid in sorted(findings):
        if fid in applied_ids:
            continue
        f = findings[fid]
        se = f.get("suggested_edit") if isinstance(f.get("suggested_edit"), dict) else {}
        proposals.append({"finding_id": fid, "priority_stars": f.get("priority_stars"), "mode": se.get("mode"), "reason_short": f.get("reason_short")})
    unresolved = sum(1 for p in proposals if p["priority_stars"] == 3 or p["mode"] in ("verify_first", "none"))

    # 흐름 재검사: 적용된 수정만으로 원본 대비 새로 생기거나 남은 절차 단계 어긋남(check_flow). 변경본은 취소하지 않고 알리기만 한다.
    flow_edits = [{"block_id": e["block_id"], "start": e["start"], "end": e["end"], "replacement": e["replacement"]} for _, es in accepted for e in es]
    flow_warnings = []
    if flow_edits:
        edited = ",".join(sorted({e["block_id"] for e in flow_edits}, key=order.get))
        for i in edit_checks.check_flow(article, flow_edits):
            if i.level == "warning":
                flow_warnings.append({"code": i.code, "detail": f"제목·부제와 본문의 절차 단계 어휘 비교, 수정된 블록: {edited}"})
    notes = [
        "변경본은 서버가 받은 원고 사본(cjson-1)의 블록 재조립본이며 원본 파일의 바이트 패치가 아닙니다. 원본은 바뀌지 않았습니다.",
        "적용은 서버의 어휘 수준 검사(숫자·인용·부정어·고유명사 후보)를 통과한 수정안에 한합니다. 의미 보존과 법적 안전은 보장하지 않으며 적용 확정은 기자가 합니다.",
    ]
    if unresolved:
        notes.append(f"적용되지 않았고 확인이 필요한 항목이 {unresolved}건 있습니다(proposals 참조).")
    if flow_warnings:
        notes.append(f"흐름 경고 {len(flow_warnings)}건: 기자 확인 필요")
    return {
        "ok": True,
        "report_id": report_id,
        "original_sha256": original_sha,
        "revision_sha256": revision_sha,
        "applied": applied,
        "skipped": [skipped[k] for k in sorted(skipped)],
        "proposals": proposals,
        "unresolved_count": unresolved,
        "flow_warnings": flow_warnings,
        "revised_blocks": [{"block_id": b["block_id"], "section": b["section"], "text": revised[b["block_id"]]} for b in article["blocks"]],
        "notes": notes,
    }

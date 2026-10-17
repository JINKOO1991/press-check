"""P3 평가 확장(P3_DESIGN §4): 합성 기사 + 색인 passage 연결 구조와 자동 판정 항목.

배선과 행동 규칙을 시험하는 구조다. 법률 정확도나 모델 성능을 재지 않는다. 기사 본문은 모두 합성이고 실제 기사 원문을 쓰지 않는다.
근거는 passage_id 로만 연결한다. 기준 출력(golden)에는 실제 판결·결정의 문장을 옮기지 않고 ``{{quote|passage_id|시작|길이}}`` 자리표시만 두어,
실행 때 사용자의 색인에서 그 구간(사용자 화면 인용 상한 안)을 가져와 채운다.

연결된 passage 가 색인에 하나라도 없거나 코퍼스가 합성이면 그 시나리오는 ``pending_corpus`` 이고 판정하지 않는다(실패가 아니다).

자동 판정 항목(P3_DESIGN §4)
- P3_links_in_index / P3_cited_evidence_in_index: 근거 passage 가 색인에 있고 서버가 verified 로 확인했다.
- P3_quotes_match_source: 설명문의 “…” 인용이 인용한 passage 원문(사용자 화면 인용 상한 안)에 그대로 있다.
- P3_no_added_facts / G3: 수정안에 새 숫자·인용·고유명사가 없다.  G2: 직접인용 보존.
- P3_no_overedit: 정답이 '수정 없음'인 시나리오에서 replace 가 0건이고 별 상한을 넘지 않는다.
- 범주별: 반론은 진실 여부·위법 경로와 분리, 의견은 일괄 면책 주장 금지, 선거는 기관·시기가 맞는 근거만 쓰거나 기권.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

import evalkit
import p4_metrics
from evalkit import FAIL, PASS, chk

from press_review import corpus as corpus_mod, edit_checks, review

HERE = evalkit.HERE
PENDING = "pending_corpus"
CATEGORIES = ("public_interest_no_edit", "reply_vs_truth", "opinion_commentary", "election_standard")
_PLACEHOLDER = re.compile(r"\{\{quote\|([^|]+)\|(\d+)\|(\d+)\}\}")
_QUOTED = re.compile(r"[“「]([^”」]{6,})[”」]")
_WS = re.compile(r"\s+")
_ADDED_FACT_CODES = {"NEW_NUMBER", "NEW_QUOTE", "DIRECT_QUOTE_CHANGED", "NEW_PROPER_NOUN"}


def collapse(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


# ---------------------------------------------------------------- 로딩

def load_scenarios(path: Path | None = None) -> dict:
    return json.loads((path or HERE / "p3_scenarios.json").read_text(encoding="utf-8"))


def load_article(sc: dict, base: Path | None = None) -> dict:
    return json.loads(((base or HERE) / sc["article"]).read_text(encoding="utf-8"))


def validate_scenarios(data: dict) -> list[str]:
    """시나리오 파일 구조 점검: 범주당 최소 건수, 필수 키, 연결 형식."""
    errs: list[str] = []
    cases = data.get("cases", [])
    ids = [c.get("id") for c in cases]
    if len(set(ids)) != len(ids):
        errs.append("duplicate scenario ids")
    for cat in data.get("categories", CATEGORIES):
        n = sum(1 for c in cases if c.get("category") == cat)
        if n < data.get("min_per_category", 2):
            errs.append(f"category {cat} has {n} scenarios")
    for c in cases:
        v2 = c.get("version") == 2
        for k in ("id", "category", "article", "expected", "evidence_links"):
            if k not in c:
                errs.append(f"{c.get('id')}: missing {k}")
        if v2:
            errs += p4_metrics.validate_v2(c)
        if c.get("category") not in CATEGORIES:
            errs.append(f"{c.get('id')}: unknown category")
        free = v2 and (c.get("provenance") or {}).get("derivation") in ("fully_invented", "minimal_pair_of")      # 사례 카드 없는 시나리오는 근거 연결 없이도 둘 수 있다
        if not c.get("evidence_links") and not free:
            errs.append(f"{c.get('id')}: no evidence_links")
        for l in c.get("evidence_links", []):
            if not (isinstance(l.get("passage_id"), str) and isinstance(l.get("source_id"), str) and l.get("role")):
                errs.append(f"{c.get('id')}: bad link {l}")
        if c.get("synthetic") is not True:
            errs.append(f"{c.get('id')}: scenario must be marked synthetic")
        if re.search(r"report|findings|summary|analysis", f"{c.get('id')}{c.get('article')}"):
            errs.append(f"{c.get('id')}: file name contains a reserved word")
    return errs


# ---------------------------------------------------------------- 자리표시 채우기

def _display_text(corpus: Any, source_id: str, passage_id: str) -> str | None:
    p = corpus.get_passage(source_id, passage_id)
    if p is None:
        return None
    return collapse(p.get("display_text", p["text"]))


def materialize(obj: Any, corpus: Any, sources: dict[str, str]) -> Any:
    """문자열 안의 ``{{quote|passage_id|start|len}}`` 를 사용자 화면 인용 범위의 원문 조각으로 바꾼다. 못 찾으면 그대로 둔다."""
    if isinstance(obj, str):
        def sub(m: re.Match) -> str:
            pid = m.group(1)
            t = _display_text(corpus, sources.get(pid, ""), pid)
            if t is None:
                return m.group(0)
            s, n = int(m.group(2)), int(m.group(3))
            return t[s:s + n].strip()
        return _PLACEHOLDER.sub(sub, obj)
    if isinstance(obj, list):
        return [materialize(x, corpus, sources) for x in obj]
    if isinstance(obj, dict):
        return {k: materialize(v, corpus, sources) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------- 판정

def missing_links(corpus: Any, sc: dict) -> list[str]:
    return [l["passage_id"] for l in sc["evidence_links"] if corpus.get_passage(l["source_id"], l["passage_id"]) is None]


def quote_checks(report: dict, expl: dict, corpus: Any) -> list[dict]:
    """설명문의 “…” 인용(6자 이상)이 같은 finding 이 인용한 passage 의 원문(사용자 화면 인용 범위)에 그대로 있는지."""
    bad: list[str] = []
    for f in report["findings"]:
        texts = [t for t in (_display_text(corpus, e["source_id"], e["passage_id"]) for e in f["evidence"]) if t]
        for key, val in (expl.get(f["finding_id"]) or {}).items():
            for s in (val if isinstance(val, list) else [val]):
                for m in _QUOTED.finditer(str(s)):
                    if not any(collapse(m.group(1)) in t for t in texts):
                        bad.append(f"{f['finding_id']}.{key}:{m.group(1)[:20]}")
    return [chk("P3_quotes_match_source", not bad, f"mismatch={bad}" if bad else "")]


def category_checks(sc: dict, article: dict, report: dict, expl: dict, corpus: Any) -> list[dict]:
    exp, fs, cat = sc["expected"], report["findings"], sc["category"]
    out = []
    replaces = [f["finding_id"] for f in fs if f["suggested_edit"]["mode"] == "replace"]
    if exp.get("edit") == "none":
        out.append(chk("P3_no_overedit", not replaces, f"replace findings={replaces}" if replaces else ""))
    hi = [f["finding_id"] for f in fs if f["priority_stars"] > exp.get("max_stars", 3)]
    out.append(chk("P3_max_stars", not hi, f"over max_stars={hi}" if hi else ""))
    if exp.get("findings") == "required":
        out.append(chk("P3_has_finding", bool(fs), "scenario requires a finding"))
    chans = {c for f in fs for c in f.get("risk_channels", [])}
    miss = [c for c in exp.get("channels_required", []) if c not in chans]
    bad = [c for c in exp.get("channels_forbidden", []) if c in chans]
    out.append(chk("P3_channels", not miss and not bad, f"missing={miss} forbidden_present={bad}" if miss or bad else ""))
    # 수정안 사실 추가(검증기와 독립 재계산)
    codes: list[str] = []
    for f in fs:
        for sp, rep in evalkit.edits_of(f):
            codes += sorted({i.code for i in edit_checks.check_replacement(sp["quote"], rep, article, block_id=sp["block_id"], start=sp["start"])} & _ADDED_FACT_CODES)
    out.append(chk("P3_no_added_facts", not codes, f"codes={codes}" if codes else ""))
    blob = "\n".join(evalkit.finding_text(f, expl.get(f["finding_id"])) for f in fs)
    if cat == "reply_vs_truth":
        reply = [f for f in fs if "reply" in f.get("risk_channels", [])]
        mixed = [f["finding_id"] for f in reply if set(f["risk_channels"]) & {"civil", "criminal", "correction"}]
        out.append(chk("P3_reply_separated_from_truth_channels", not mixed, f"mixed={mixed}" if mixed else ""))
        out.append(chk("P3_no_falsity_or_illegality_assertion", not evalkit._AFFIRM_ILLEGAL.search(blob), ""))
    elif cat == "opinion_commentary":
        out.append(chk("P3_no_blanket_immunity_claim", not evalkit._BLANKET_IMMUNE.search(blob), ""))
    elif cat == "election_standard":
        bad_e = []
        for f in fs:
            for ch in ("pac_election", "iendc_election"):
                if ch not in f.get("risk_channels", []):
                    continue
                if not f["evidence"] and f["suggested_edit"]["mode"] == "replace":
                    bad_e.append(f"{f['finding_id']}:replace without evidence")
                for e in f["evidence"]:
                    if e["authority_type"] not in evalkit._ELECTION_AUTH[ch]:
                        bad_e.append(f"{f['finding_id']}:{e['authority_type']} for {ch}")
                    p = corpus.get_passage(e["source_id"], e["passage_id"])
                    ref = p and (p.get("reference_date") or p.get("decision_date"))       # G-D23
                    if ref and ref > report["legal_as_of"]:
                        bad_e.append(f"{f['finding_id']}:decision after legal_as_of")
        out.append(chk("P3_election_scope_and_date_consistent_or_abstain", not bad_e, f"issues={bad_e}" if bad_e else ""))
        # 과거 선거의 결정을 근거로 쓰면 설명문에 시기·범위 한계를 밝혀야 한다(어휘 일치만 본다)
        cited = [f for f in fs if f["evidence"]]
        unscoped = [f["finding_id"] for f in cited if not re.search(r"선거|시기|기준|범위", "".join(map(str, (expl.get(f["finding_id"]) or {}).get("differences", ""))))]
        out.append(chk("P3_election_difference_stated", not unscoped, f"no scope note={unscoped}" if unscoped else ""))
    return out


def v2_checks(sc: dict, article: dict, report: dict, expl: dict, corpus: Any, group_members: dict | None) -> tuple[dict, list[dict]]:
    """스키마 v2 시나리오의 자동 지표(P4_DESIGN §4.2)와 결정적 검사. gold 라벨이 draft 인 동안 recall 은 지표일 뿐 pass/fail 이 아니다."""
    fs = report["findings"]
    bad_spans = p4_metrics.check_spans_in_article(sc, article)
    m = p4_metrics.match_gold(fs, sc.get("gold_issues", []))
    touched = p4_metrics.preserve_touched(fs, sc.get("preserve_spans", []))
    texts = [rep for f in fs for _, rep in evalkit.edits_of(f)] + [evalkit.finding_text(f, expl.get(f["finding_id"])) for f in fs]
    flags = p4_metrics.added_fact_scan(article, sc, texts)
    members = (group_members or {}).get((sc.get("provenance") or {}).get("source_group_id"))
    self_ev = p4_metrics.self_case_evidence(fs, corpus, members) if members else []
    p4 = {"gold": m, "preserve_total": len(sc.get("preserve_spans", [])), "preserve_touched": touched,
          "has_replace": any(f["suggested_edit"]["mode"] == "replace" for f in fs), "added_fact_flags": flags,
          "edit_count": sum(len(evalkit.edits_of(f)) for f in fs), "self_case_evidence": self_ev, "self_case_blocked": bool(members),
          "labels_final": all(g["label_status"] in ("adjudicated",) for g in sc.get("gold_issues", [])) and bool(sc.get("gold_issues"))}
    checks = [chk("P4_spans_match_article", not bad_spans, f"bad={bad_spans}" if bad_spans else ""),
              chk("P4_preserve_spans_untouched", not touched, f"touched={touched}" if touched else ""),
              chk("P4_no_added_facts_lexicon", not flags, f"flags={flags}" if flags else ""),
              chk("P4_no_self_case_evidence", not self_ev, f"evidence={self_ev}" if self_ev else "")]
    return p4, checks


V1_LABEL = "v1(미분류)"


def derivation_of(sc: dict) -> str:
    return (sc.get("provenance") or {}).get("derivation") or V1_LABEL if sc.get("version") == 2 else V1_LABEL


def p4_summary(rows: list[dict]) -> dict:
    """v2 시나리오 집계(기사 단위·구간 단위 불필요 수정률, recall 분자/분모, 사실 추가). 분모가 0이면 total 0 으로 남긴다."""
    v2 = [r for r in rows if "p4" in r]
    def add(key):
        return {"matched": sum(r["p4"]["gold"][key]["matched"] for r in v2), "total": sum(r["p4"]["gold"][key]["total"] for r in v2)}
    by_der = {k: sum(r.get("derivation") == k for r in rows) for k in (*p4_metrics.DERIVATIONS, V1_LABEL)}
    return {"scenarios": len(v2), "by_derivation": by_der,
            "labels_final_scenarios": sum(r["p4"]["labels_final"] for r in v2),
            "recall_must_or_should": add("recall_must_or_should"), "recall_must_fix": add("recall_must_fix"), "recall_with_channel": add("recall_with_channel"),
            "overedit_article": p4_metrics.article_overedit({"expected_edit": r["expected"].get("edit"), "has_replace": r["p4"]["has_replace"]} for r in v2),
            "overedit_span": {"touched": sum(len(r["p4"]["preserve_touched"]) for r in v2), "total": sum(r["p4"]["preserve_total"] for r in v2)},
            "added_fact_scenarios": {"flagged": sum(bool(r["p4"]["added_fact_flags"]) for r in v2), "total": len(v2)},
            "self_case_evidence_scenarios": sum(bool(r["p4"]["self_case_evidence"]) for r in v2)}


def evaluate_p3_case(sc: dict, article: dict, output: dict, corpus: Any, now: _dt.datetime, stats: evalkit.SandboxStats,
                     group_members: dict | None = None) -> dict:
    """한 시나리오 판정. 연결 passage 가 색인에 없거나 합성 코퍼스면 pending_corpus."""
    miss = missing_links(corpus, sc) if not corpus.is_fixture else [l["passage_id"] for l in sc["evidence_links"]]
    base = {"case_id": sc["id"], "category": sc["category"], "expected": sc["expected"], "derivation": derivation_of(sc)}
    if miss:
        return {**base, "status": PENDING, "missing_passages": miss, "checks": [], "finding_count": 0,
                "note": "연결된 근거 passage 가 현재 색인에 없어 판정하지 않았다. 해당 자료를 수집·색인한 뒤 다시 실행한다."}
    sources = {l["passage_id"]: l["source_id"] for l in sc["evidence_links"]}
    findings = materialize(copy.deepcopy(output.get("findings", [])), corpus, sources)
    expl = materialize(copy.deepcopy(output.get("explanations", {})), corpus, sources)
    res = review.build_report_ex(article, findings, expl, corpus, now=now, overall_status=output.get("overall_status"),
                                 coverage_notes=output.get("coverage_notes"))
    checks = [chk("P3_links_in_index", True)]
    if res.report is None:
        checks.append(chk("G1_engine_validator_accepts", False, "; ".join(res.errors)[:600]))
        return {**base, "status": FAIL, "finding_count": len(findings), "checks": checks, "engine_warnings": res.warnings}
    report, ex = res.report, res.explanations
    checks.append(chk("G1_engine_validator_accepts", True))
    unverified = [e["passage_id"] for f in report["findings"] for e in f["evidence"] if e.get("verified") is not True]
    checks.append(chk("P3_cited_evidence_in_index", not unverified, f"unverified={unverified}" if unverified else ""))
    checks += evalkit.generic_checks(article, report)
    checks += quote_checks(report, ex, corpus)
    checks += category_checks(sc, article, report, ex, corpus)
    p4 = None
    if sc.get("version") == 2:
        p4, p4_checks = v2_checks(sc, article, report, ex, corpus, group_members)
        checks += p4_checks
    bad = [c["name"] for c in checks if c["status"] == FAIL]
    row = {**base, "status": FAIL if bad else PASS, "finding_count": len(report["findings"]), "checks": checks,
           "engine_warnings": res.warnings, "legal_as_of": report["legal_as_of"]}
    if p4 is not None:
        row["p4"] = p4
    return row


def run_p3(corpus: Any, scenarios: dict, *, outputs: dict[str, dict], base: Path | None = None, now_s: str = evalkit.DEFAULT_NOW,
           articles: dict[str, dict] | None = None, host: dict | None = None, group_members: dict | None = None) -> dict:
    now = evalkit.parse_now(now_s)
    rows = []
    for sc in scenarios["cases"]:
        art = copy.deepcopy((articles or {}).get(sc["id"]) or load_article(sc, base))
        out = outputs.get(sc["id"])
        if out is None:
            raise ValueError(f"no output for scenario {sc['id']}")
        with evalkit.sandbox() as stats:
            rows.append(evaluate_p3_case(sc, art, out, corpus, now, stats, group_members))
    counts = {k: sum(r["status"] == k for r in rows) for k in (PASS, FAIL, PENDING)}
    return {"meta": {"corpus_snapshot_id": corpus.snapshot_id, "corpus_is_fixture": corpus.is_fixture, "now": now_s,
                     "note": "합성 기사와 색인 passage 연결 구조의 배선·행동 규칙 시험. 법률 정확도·모델 성능 결과가 아니다.",
                     "scenarios_purpose": scenarios.get("purpose", ""),
                     "host": host or p4_metrics.host_record(None, None)},
            "counts": counts, "p4_summary": p4_summary(rows), "cases": rows}

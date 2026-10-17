"""P4 V3 지표 계산(P4_DESIGN §3.4·§3.5·§4.1·§4.2): 순수 함수. 색인·네트워크·LLM을 쓰지 않는다.

- 매칭: 같은 block 에서 code point 반개구간이 겹치면 매칭. finding 의 구간 = span + related_edits 구간 전부(§4.1).
- 중요 위험 recall: 한 finding 이 여러 gold_issue 를 채울 수 있다(다중 매칭). precision 계산에서는 finding 을 한 번만 센다.
- 불필요 수정률: 기사 단위(expected.edit=none 시나리오 중 replace 가 있는 것)와 구간 단위(replace 가 닿은 preserve_spans).
- 사실 추가 1차 탐지: 어휘 목록 + 장부의 reply_request 상태 대조(자동 규칙만). 우회 표현은 수동 전수 대상이다.
- 자기 사건 차단: 시나리오 출처 그룹의 passage 를 검색 결과·인용 근거에서 뺀다(하네스 층 사후 필터, 엔진 무수정).
라벨 상태가 draft 인 gold 로 낸 수치는 평가자 판정 전의 배선 시험이며 성능 결과가 아니다.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

import evalkit  # noqa: F401  (엔진 경로)
from press_review import corpus as corpus_mod, corpus_sqlite as cs

DERIVATIONS = ("card_from_case", "minimal_pair_of", "fully_invented")
SPLITS = ("tune", "dev", "test", "reserve")
ACTIONS = ("replace", "verify_first", "none")
SEVERITIES = ("must_fix", "should_check", "acceptable", "uncertain")
LABEL_STATUS = ("draft", "rater_a", "rater_b", "adjudicated", "uncertain")
REPLY_STATES = ("requested_answered", "requested_no_answer", "not_requested", "unknown")
FACT_BASIS = ("document", "interview", "record", "none")
FACT_VERIFIED = (True, False, "unknown")
FUTURE_T = "2018-10-01"                                  # 사용자 결정 2026-10-10 (P4_DESIGN §10-2)
FUTURE_AS_OF = "2018-09-30"                              # T - 1일. 미래 구간 평가의 검색 as_of
SCORED = ("must_fix", "should_check")                     # uncertain 은 분모에서 뺀다

# §3.5 어휘 목록. 기사 본문·장부에 이미 있는 문구는 새 사실이 아니다.
REPLY_NO_ANSWER = ("답하지 않았다", "답변하지 않았다", "응답하지 않았다", "회신하지 않았다", "연락이 닿지", "답이 없었다")
REPLY_REQUEST = ("해명을 요청", "반론을 요청", "입장을 물었", "입장을 요청", "반론 요청", "해명을 구했")
REPLY_ANSWERED = ("반박했다", "해명했다", "입장을 밝혔다")
CONSENT_OR_DOC = ("동의를 받", "동의를 얻", "동의했다", "문서를 확보", "자료를 입수", "직접 취재", "현장을 취재")


def validate_v2(sc: dict) -> list[str]:
    """version 2 필드 구조 점검. v1 필드는 건드리지 않는다. 구간 인용의 기사 본문 일치는 check_spans_in_article."""
    e: list[str] = []
    sid = sc.get("id")
    pv = sc.get("provenance")
    if not isinstance(pv, dict) or pv.get("derivation") not in DERIVATIONS:
        e.append(f"{sid}: provenance.derivation must be one of {DERIVATIONS}")
    elif pv["derivation"] == "minimal_pair_of" and not pv.get("pair_of"):
        e.append(f"{sid}: minimal_pair_of needs pair_of")
    if sc.get("split") not in SPLITS:
        e.append(f"{sid}: split must be one of {SPLITS}")
    if sc.get("reply_request", "unknown") not in REPLY_STATES:
        e.append(f"{sid}: bad reply_request")
    if not isinstance(sc.get("canary"), str) or not sc["canary"]:
        e.append(f"{sid}: canary string required")
    for f in sc.get("provided_facts", []):
        if not (f.get("fact_id") and isinstance(f.get("text"), str) and f.get("basis") in FACT_BASIS and f.get("verified") in FACT_VERIFIED):
            e.append(f"{sid}: bad provided_fact {f.get('fact_id')}")
    def span_ok(s):
        return isinstance(s.get("block_id"), str) and isinstance(s.get("start"), int) and isinstance(s.get("end"), int) and 0 <= s["start"] < s["end"] and isinstance(s.get("quote"), str)
    for g in sc.get("gold_issues", []):
        gid = g.get("issue_id")
        if not (gid and g.get("spans") and all(span_ok(s) for s in g["spans"])):
            e.append(f"{sid}/{gid}: bad spans")
        if g.get("action") not in ACTIONS or g.get("severity") not in SEVERITIES or g.get("label_status") not in LABEL_STATUS:
            e.append(f"{sid}/{gid}: bad action/severity/label_status")
        if not g.get("channels") or any(c not in corpus_mod.RISK_CHANNELS for c in g["channels"]):
            e.append(f"{sid}/{gid}: bad channels")
    for s in sc.get("preserve_spans", []):
        if not span_ok(s):
            e.append(f"{sid}: bad preserve_span")
    ids = [g.get("issue_id") for g in sc.get("gold_issues", [])]
    if len(set(ids)) != len(ids):
        e.append(f"{sid}: duplicate issue_id")
    return e


def check_spans_in_article(sc: dict, article: dict) -> list[str]:
    """gold_issues·preserve_spans 의 quote 가 기사 block 의 [start:end] 와 일치하는지."""
    texts = {b["block_id"]: b["text"] for b in article["blocks"]}
    bad = []
    spans = [(g["issue_id"], s) for g in sc.get("gold_issues", []) for s in g["spans"]] + [("preserve", s) for s in sc.get("preserve_spans", [])]
    for name, s in spans:
        if s["block_id"] not in texts or texts[s["block_id"]][s["start"]:s["end"]] != s["quote"]:
            bad.append(f"{sc['id']}/{name}:{s['block_id']}[{s['start']}:{s['end']}]")
    return bad


def overlaps(a: dict, b: dict) -> bool:
    return a["block_id"] == b["block_id"] and a["start"] < b["end"] and b["start"] < a["end"]


def finding_spans(f: dict) -> list[dict]:
    se = f["suggested_edit"]
    return [f["span"]] + [r["span"] for r in se.get("related_edits", [])]


def replace_spans(f: dict) -> list[dict]:
    se = f["suggested_edit"]
    return finding_spans(f) if se["mode"] == "replace" else []


def match_gold(findings: list[dict], gold_issues: list[dict]) -> dict:
    """다중 매칭 recall. verify_first(finding mode 가 replace 가 아닌 것)도 탐지로 센다. 경로까지 맞은 recall 은 보조."""
    rows = []
    for g in gold_issues:
        hit = [f for f in findings if any(overlaps(fs, gs) for fs in finding_spans(f) for gs in g["spans"])]
        rows.append({"issue_id": g["issue_id"], "severity": g["severity"], "matched": bool(hit), "finding_ids": [f["finding_id"] for f in hit],
                     "channel_matched": any(set(f.get("risk_channels", [])) & set(g["channels"]) for f in hit)})
    def rate(sel):
        d = [r for r in rows if sel(r)]
        return {"matched": sum(r["matched"] for r in d), "total": len(d)}
    matched_f = {fid for r in rows for fid in r["finding_ids"]}
    return {"issues": rows,
            "recall_must_or_should": rate(lambda r: r["severity"] in SCORED),
            "recall_must_fix": rate(lambda r: r["severity"] == "must_fix"),
            "recall_with_channel": {"matched": sum(r["matched"] and r["channel_matched"] for r in rows if r["severity"] in SCORED),
                                    "total": sum(r["severity"] in SCORED for r in rows)},
            "findings": {"total": len(findings), "matched": len(matched_f)}}       # finding 은 한 번만 센다


def preserve_touched(findings: list[dict], preserve_spans: list[dict]) -> list[int]:
    """replace 구간이 닿은 preserve_spans 의 색인."""
    reps = [s for f in findings for s in replace_spans(f)]
    return [i for i, p in enumerate(preserve_spans) if any(overlaps(r, p) for r in reps)]


def article_overedit(rows: Iterable[dict]) -> dict:
    """rows: {'expected_edit', 'has_replace'}. 분모는 expected.edit == none 시나리오."""
    d = [r for r in rows if r["expected_edit"] == "none"]
    return {"overedited": sum(r["has_replace"] for r in d), "total": len(d)}


def added_fact_scan(article: dict, sc: dict, texts: list[str]) -> list[str]:
    """수정안·설명 문자열에서 어휘 목록 탐지. 원문 기사·장부에 이미 있는 문구는 제외. reply_request 상태에 맞지 않는 서술이면 플래그."""
    base = "".join(b["text"] for b in article["blocks"]) + "".join(f["text"] for f in sc.get("provided_facts", []))
    state = sc.get("reply_request", "unknown")
    allow_req = state.startswith("requested")
    flags = []
    blob = "\n".join(texts)
    for phr in REPLY_NO_ANSWER:
        if phr in blob and phr not in base and state != "requested_no_answer":
            flags.append(f"reply_no_answer:{phr}")
    for phr in REPLY_REQUEST:
        if phr in blob and phr not in base and not allow_req:
            flags.append(f"reply_request:{phr}")
    for phr in REPLY_ANSWERED:
        if phr in blob and phr not in base and state != "requested_answered":
            flags.append(f"reply_answered:{phr}")
    for phr in CONSENT_OR_DOC:
        if phr in blob and phr not in base:
            flags.append(f"consent_or_doc:{phr}")
    return flags


def block_self_group(results: list[dict], members: dict) -> tuple[list[dict], int]:
    """members: {case_numbers, source_ids, passage_ids} 중 일부. 일치하는 결과를 뺀 목록과 뺀 수."""
    cn, sid, pid = set(members.get("case_numbers", [])), set(members.get("source_ids", [])), set(members.get("passage_ids", []))
    def blocked(r):
        return r.get("passage_id") in pid or r.get("source_id") in sid or bool(cn & set(cs.case_numbers(r.get("case_number"))))
    kept = [r for r in results if not blocked(r)]
    return kept, len(results) - len(kept)


class BlockedCorpus:
    """search 결과에서 시나리오 자기 사건 그룹을 사후 필터로 뺀다. 나머지는 원 코퍼스로 위임. removed 에 누적 수를 센다."""

    def __init__(self, corpus: Any, members: dict):
        self._c, self._m, self.removed = corpus, members, 0

    def search(self, query, **kw):
        kept, n = block_self_group(self._c.search(query, **kw), self._m)
        self.removed += n
        return kept

    def __getattr__(self, name):
        return getattr(self._c, name)


def self_case_evidence(findings: list[dict], corpus: Any, members: dict) -> list[str]:
    """인용 근거(evidence) 중 자기 사건 그룹 passage."""
    bad = []
    for f in findings:
        for ev in f.get("evidence", []):
            p = corpus.get_passage(ev["source_id"], ev["passage_id"])
            if p and block_self_group([p], members)[1]:
                bad.append(f"{f['finding_id']}:{ev['passage_id']}")
    return bad


def host_record(model: str | None, effort: str | None, skill_version: str | None = None) -> dict:
    """§10 열린 질문 8: 호스트 모델·effort·스킬 버전을 실행마다 기록한다. 둘 다 있어야 pinned."""
    return {"model": model, "effort": effort, "skill_version": skill_version, "pinned": bool(model and effort)}


def future_window_filter(results: list[dict]) -> tuple[list[dict], dict]:
    """미래 구간 평가용 하네스 필터(P4_DESIGN §3.4). 엔진 as_of 필터는 reference_date 가 없는 passage 를 통과시키므로(corpus_sqlite.py 의 as_of 조건)
    date_unknown 과 연도 상한(year_upper_bound)만 있는 passage 를 결과에서 빼고 뺀 수를 돌려준다. 엔진 코드는 바꾸지 않는다."""
    kept, unk, ub = [], 0, 0
    for r in results:
        if r.get("date_unknown") or r.get("reference_date") is None:
            unk += 1
        elif r.get("reference_date_basis") == "year_upper_bound":
            ub += 1
        else:
            kept.append(r)
    return kept, {"date_unknown": unk, "year_upper_bound": ub}


class FutureWindowCorpus:
    """search 의 as_of 를 FUTURE_AS_OF 로 고정하고 date_unknown·연도 상한 passage 를 뺀다. removed 에 누적한다. 나머지는 원 코퍼스로 위임."""

    def __init__(self, corpus: Any):
        self._c, self.removed = corpus, {"date_unknown": 0, "year_upper_bound": 0}

    def search(self, query, **kw):
        kw["as_of"] = FUTURE_AS_OF
        kept, n = future_window_filter(self._c.search(query, **kw))
        for k, v in n.items():
            self.removed[k] += v
        return kept

    def __getattr__(self, name):
        return getattr(self._c, name)

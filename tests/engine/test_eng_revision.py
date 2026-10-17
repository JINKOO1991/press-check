"""build_revision: 저장된 보고서의 안전한 replace 수정안만 결정적으로 적용해 변경본을 만든다(원본 불변, 디스크 무기록)."""
import copy
import datetime as dt
import json

import pytest

from conftest import make_article
from press_review import corpus as corpus_mod
from press_review import hashing, logsafe, mcp_server, review
from press_review.mcp_server import Server
from press_review.store import EphemeralStore

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))

BLOCKS = [
    ("title", "title", "대표 횡령 의혹 경찰 수사"),
    ("subtitle", "subtitle", "경찰이 사실관계를 확인 중이다"),
    ("p1", "body", "경찰은 김씨가 회삿돈을 빼돌린 것으로 보고 있다고 밝혔다. 김씨는 혐의를 모두 인정했다고 한다."),
    ("p2", "body", "박씨는 “회삿돈을 빼돌린 적이 없다”고 말했다. 회사는 입장을 내지 않았다."),
]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def fnd(fid, stars, block, quote, rep, *, mode="replace", related=None, nth=0, arts=None):
    text = dict((b, t) for b, _, t in (arts or BLOCKS))[block]
    s = -1
    for _ in range(nth + 1):
        s = text.index(quote, s + 1)
    se = {"mode": mode, "replacement": rep if mode == "replace" else None, "related_edits": [], "required_verification": []}
    for rb, rq, rr in related or []:
        rs = dict((b, t) for b, _, t in (arts or BLOCKS))[rb].index(rq)
        se["related_edits"].append({"span": {"block_id": rb, "start": rs, "end": rs + len(rq), "quote": rq}, "replacement": rr})
    return {
        "finding_id": fid, "span": {"block_id": block, "start": s, "end": s + len(quote), "quote": quote},
        "priority_stars": stars, "issue_category": "test_category", "review_confidence": "low",
        "reason_short": f"사유 {fid}", "suggested_edit": se, "evidence": [],
    }


class Env:
    def __init__(self, findings, blocks=BLOCKS):
        self.clock = Clock()
        logsafe.reset()
        self.store = review.new_store(clock=self.clock)
        self.article = make_article(blocks)
        prep = review.prepare_article(self.store, article=copy.deepcopy(self.article))
        assert prep["ok"], prep
        self.handle, self.sha = prep["article_handle"], prep["article_sha256"]
        out = review.submit_report(self.store, corpus_mod.FixtureCorpus(), article_handle=self.handle, findings=findings, now=NOW)
        assert out["ok"], out
        self.rid = out["report_id"]

    def build(self, **kw):
        args = {"report_id": self.rid, "article_sha256": self.sha, **kw}
        return review.build_revision(self.store, **args)

    def report(self):
        return self.store.get(f"report:{self.rid}")["report"]


def ids(lst, key="finding_id"):
    return [x[key] for x in lst]


def revise(r):
    """변경본 블록에서 applied 의 revised 좌표 구간을 original 로 되돌려 원문을 복원한다."""
    texts = {b["block_id"]: b["text"] for b in r["revised_blocks"]}
    edits = [e for a in r["applied"] for e in a["edits"]]
    for e in sorted(edits, key=lambda e: (e["block_id"], -e["revised_start"])):
        t = texts[e["block_id"]]
        assert t[e["revised_start"]:e["revised_end"]] == e["replacement"]
        texts[e["block_id"]] = t[:e["revised_start"]] + e["original"] + t[e["revised_end"]:]
    return texts


F_OK = lambda: fnd("F-1", 3, "title", "횡령 의혹", "횡령 혐의")
# --- 정상 ---------------------------------------------------------------


def test_applies_and_reports_everything():
    env = Env([fnd("F-1", 3, "title", "횡령 의혹", "횡령 혐의"), fnd("F-2", 2, "p1", "빼돌린 것으로 보고 있다고 밝혔다", "빼돌렸다는 취지로 밝혔다")])
    r = env.build()
    assert r["ok"] is True and r["report_id"] == env.rid
    assert r["original_sha256"] == env.sha
    assert ids(r["applied"]) == ["F-1", "F-2"]
    assert r["skipped"] == [] and r["proposals"] == [] and r["unresolved_count"] == 0
    rb = {b["block_id"]: b for b in r["revised_blocks"]}
    assert rb["title"]["text"] == "대표 횡령 혐의 경찰 수사" and rb["title"]["section"] == "title"
    assert "빼돌렸다는 취지로 밝혔다" in rb["p1"]["text"]
    revised = copy.deepcopy(env.article)
    for b in revised["blocks"]:
        b["text"] = rb[b["block_id"]]["text"]
    assert r["revision_sha256"] == hashing.article_sha256(revised)[0] != r["original_sha256"]
    e = r["applied"][0]
    assert e["priority_stars"] == 3 and e["edits"][0]["block_id"] == "title"
    assert (e["edits"][0]["start"], e["edits"][0]["end"], e["edits"][0]["original"]) == (3, 8, "횡령 의혹")
    assert isinstance(r["notes"], list)


def test_revised_blocks_cover_every_block_once_in_order():
    env = Env([F_OK()])
    r = env.build()
    assert ids(r["revised_blocks"], "block_id") == ["title", "subtitle", "p1", "p2"]


def test_no_applied_keeps_hash():
    env = Env([fnd("F-1", 3, "title", "횡령 의혹", None, mode="verify_first")])
    r = env.build()
    assert r["applied"] == [] and r["revision_sha256"] == r["original_sha256"]


# --- 거부 ---------------------------------------------------------------


def test_unknown_report_and_expired():
    env = Env([F_OK()])
    r = review.build_revision(env.store, report_id="rpt-none", article_sha256=env.sha)
    assert r["ok"] is False and r["error_code"] == "REPORT_NOT_FOUND_OR_EXPIRED"
    env.clock.t = 1801
    r = env.build()
    assert r["ok"] is False and r["error_code"] == "REPORT_NOT_FOUND_OR_EXPIRED"


def test_article_evicted_but_report_remains():
    env = Env([F_OK()])
    env.store.delete(f"article:{env.handle}")
    r = env.build()
    assert r["ok"] is False and r["error_code"] == "ARTICLE_EXPIRED"


def test_hash_mismatch():
    env = Env([F_OK()])
    r = env.build(article_sha256="0" * 64)
    assert r["ok"] is False and r["error_code"] == "HASH_MISMATCH"


def test_error_messages_hold_no_article_text():
    env = Env([F_OK()])
    for res in (env.build(article_sha256="0" * 64), review.build_revision(env.store, report_id="rpt-x", article_sha256=env.sha)):
        s = json.dumps(res, ensure_ascii=False)
        assert all(t not in s for _, _, t in BLOCKS)


# --- finding 단위 탈락 --------------------------------------------------


def test_quote_mismatch_drops_whole_finding():
    env = Env([fnd("F-1", 3, "title", "횡령 의혹", "횡령 혐의", related=[("p1", "혐의를 모두 인정했다고 한다", "혐의를 인정했다고 한다")])])
    env.report()["findings"][0]["suggested_edit"]["related_edits"][0]["span"]["quote"] = "다른 문구입니다"
    r = env.build()
    assert r["applied"] == [] and r["skipped"][0]["finding_id"] == "F-1" and r["skipped"][0]["reason_code"] == "QUOTE_MISMATCH"
    assert r["revision_sha256"] == r["original_sha256"]
    assert ids(r["proposals"]) == ["F-1"]


def test_overlap_drops_later_finding_entirely_and_lower_star_loses():
    env = Env([fnd("F-1", 2, "p1", "빼돌린 것으로", "빼돌렸다고", related=[("p2", "회사는 입장을 내지 않았다", "회사는 입장을 밝히지 않았다")]),
               fnd("F-2", 3, "p1", "보고 있다고 밝혔다", "보고 있다고 전했다", related=[("title", "횡령 의혹", "횡령 혐의")])])
    # 제출 검사는 겹침을 막으므로 보관소를 직접 바꿔 F-1 구간이 F-2 구간과 겹치게 한다
    p1 = BLOCKS[2][2]
    s2 = env.report()["findings"][1]["span"]["start"]
    sp = env.report()["findings"][0]["span"]
    sp["start"], sp["end"] = s2 - 3, s2 + 3
    sp["quote"] = p1[sp["start"]:sp["end"]]
    r = env.build()
    assert ids(r["applied"]) == ["F-2"]          # 별 3개가 먼저 처리된다
    assert r["skipped"][0]["finding_id"] == "F-1" and r["skipped"][0]["reason_code"] == "OVERLAP"
    assert "입장을 밝히지" not in "".join(b["text"] for b in r["revised_blocks"])   # F-1 의 related 도 적용되지 않았다
    assert "횡령 혐의" in r["revised_blocks"][0]["text"]


def _two_adjacent(env_first="빼돌린 것으로", env_second=" 보고 있다고"):
    return Env([fnd("F-1", 2, "p1", env_first, "빼돌렸다는"), fnd("F-2", 2, "p1", env_second, " 말했다고")])


def test_adjacent_spans_both_apply_and_roundtrip():
    env = _two_adjacent()
    f1, f2 = env.report()["findings"][0]["span"], env.report()["findings"][1]["span"]
    assert f1["end"] == f2["start"]
    r = env.build()
    assert ids(r["applied"]) == ["F-1", "F-2"] and r["skipped"] == []
    rb = {b["block_id"]: b["text"] for b in r["revised_blocks"]}
    back = revise(r)
    assert back == {b: t for b, _, t in BLOCKS}
    assert rb["p1"] != back["p1"]


def test_direct_quote_change_is_dropped_and_cannot_be_bypassed():
    inner = "회삿돈을 빼돌린 적이 없다"
    # 인용 내부를 바꾸는 수정안은 제출 단계에서 막히므로 보관소를 직접 바꿔 주입한다. 주 span 과 related 두 경로 모두 본다.
    env = Env([fnd("F-1", 3, "title", "횡령 의혹", "횡령 혐의"), fnd("F-2", 3, "p1", "혐의를 모두 인정했다고 한다", "혐의를 인정했다고 한다")])
    f = env.report()["findings"]
    f[0]["span"] = {"block_id": "p2", "start": BLOCKS[3][2].index(inner), "end": BLOCKS[3][2].index(inner) + len(inner), "quote": inner}
    f[0]["suggested_edit"]["replacement"] = "회삿돈을 빼돌렸다"
    f[1]["suggested_edit"]["related_edits"] = [{"span": {"block_id": "p2", "start": BLOCKS[3][2].index("적이 없다"), "end": BLOCKS[3][2].index("적이 없다") + 5, "quote": "적이 없다"}, "replacement": "적이 있다"}]
    r = env.build()
    assert r["applied"] == []
    assert {s["finding_id"]: s["reason_code"] for s in r["skipped"]} == {"F-1": "DIRECT_QUOTE_CHANGED", "F-2": "DIRECT_QUOTE_CHANGED"}
    assert r["revision_sha256"] == r["original_sha256"]
    assert "빼돌린 적이 없다" in next(b["text"] for b in r["revised_blocks"] if b["block_id"] == "p2")


def test_warning_negation_changed_drops_finding():
    env = Env([fnd("F-1", 3, "p2", "회사는 입장을 내지 않았다", "회사는 입장을 냈다")])
    assert env.report()["findings"][0]["suggested_edit"]["required_verification"]      # 제출 때는 경고로 저장돼 있다
    r = env.build()
    assert r["applied"] == [] and r["skipped"][0]["reason_code"] == "NEGATION_CHANGED"
    assert r["unresolved_count"] == 1                                                    # 별 3개 미적용


def test_related_edit_issue_drops_whole_finding():
    env = Env([fnd("F-1", 3, "title", "횡령 의혹", "횡령 혐의", related=[("p2", "회사는 입장을 내지 않았다", "회사는 입장을 냈다")])])
    r = env.build()
    assert r["applied"] == [] and r["skipped"][0]["reason_code"] == "NEGATION_CHANGED"
    assert "related_edits[0]" in r["skipped"][0]["detail"]
    assert next(b["text"] for b in r["revised_blocks"] if b["block_id"] == "title") == BLOCKS[0][2]


# --- 자격 ---------------------------------------------------------------


def test_one_star_verify_first_none_are_proposals_only():
    env = Env([fnd("F-1", 1, "title", "횡령 의혹", "횡령 혐의"),
               fnd("F-2", 3, "p1", "김씨는 혐의를 모두 인정했다고 한다", None, mode="verify_first"),
               fnd("F-3", 2, "subtitle", "사실관계를 확인 중이다", None, mode="none"),
               fnd("F-4", 2, "p2", "회사는 입장을 내지 않았다", "회사는 입장을 밝히지 않았다"),
               fnd("F-5", 1, "p1", "경찰은 김씨가", None, mode="none")])
    r = env.build()
    assert ids(r["applied"]) == ["F-4"]
    assert ids(r["proposals"]) == ["F-1", "F-2", "F-3", "F-5"]
    assert {p["finding_id"]: p["mode"] for p in r["proposals"]} == {"F-1": "replace", "F-2": "verify_first", "F-3": "none", "F-5": "none"}
    assert r["proposals"][0].keys() == {"finding_id", "priority_stars", "mode", "reason_short"}
    assert r["unresolved_count"] == 3          # F-2(별3 verify_first), F-3(none), F-5(별1 none). F-1(별1 replace)은 세지 않는다
    assert r["skipped"] == []


def test_unresolved_count_counts_none_one_star_too():
    env = Env([fnd("F-5", 1, "p1", "경찰은 김씨가", None, mode="none"), fnd("F-6", 3, "title", "횡령 의혹", "횡령 혐의")])
    assert env.build()["unresolved_count"] == 1


def test_finding_ids_subset_and_ineligible_reasons():
    env = Env([fnd("F-1", 3, "title", "횡령 의혹", "횡령 혐의"), fnd("F-2", 2, "p2", "회사는 입장을 내지 않았다", "회사는 입장을 밝히지 않았다"),
               fnd("F-3", 1, "subtitle", "확인 중이다", "확인하고 있다"), fnd("F-4", 3, "p1", "경찰은", None, mode="verify_first")])
    r = env.build(finding_ids=["F-2", "F-3", "F-4", "F-9", "F-2"])
    assert ids(r["applied"]) == ["F-2"]
    codes = {s["finding_id"]: s["reason_code"] for s in r["skipped"]}
    assert codes == {"F-3": "STARS_BELOW_2", "F-4": "MODE_NOT_REPLACE", "F-9": "UNKNOWN_FINDING_ID"}
    assert ids(r["proposals"]) == ["F-1", "F-3", "F-4"]      # 부분집합 밖의 F-1 도 미적용 제안으로 남는다
    assert r["unresolved_count"] == 2                         # F-1(별3), F-4


def test_empty_finding_ids_list_applies_nothing_extra():
    env = Env([F_OK()])
    r = env.build(finding_ids=[])
    assert r["applied"] == [] and ids(r["proposals"]) == ["F-1"]


# --- 성질 ---------------------------------------------------------------


def test_idempotent_byte_identical():
    env = Env([fnd("F-1", 3, "title", "횡령 의혹", "횡령 혐의"), fnd("F-2", 2, "p1", "빼돌린 것으로 보고 있다고 밝혔다", "빼돌렸다고 밝혔다"),
               fnd("F-3", 1, "p2", "입장을 내지 않았다", "입장을 밝히지 않았다")])
    a, b = env.build(), env.build()
    assert json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)


def test_does_not_mutate_stored_article_or_report():
    env = Env([F_OK(), fnd("F-2", 2, "p2", "회사는 입장을 내지 않았다", "회사는 입장을 밝히지 않았다")])
    before = copy.deepcopy((env.store.get(f"article:{env.handle}"), env.store.get(f"report:{env.rid}")))
    out = env.build()
    out["revised_blocks"][0]["text"] = "변조"
    out["applied"][0]["edits"][0]["original"] = "변조"
    after = (env.store.get(f"article:{env.handle}"), env.store.get(f"report:{env.rid}"))
    assert before == after
    assert env.build()["revised_blocks"][0]["text"] != "변조"


def test_article_text_appears_once_only_in_revised_blocks():
    env = Env([fnd("F-1", 3, "p1", "빼돌린 것으로 보고 있다고", "빼돌렸다고"), fnd("F-2", 3, "p2", "회사는 입장을 내지 않았다", "회사는 입장을 밝히지 않았다")])
    r = env.build()
    rest = {k: v for k, v in r.items() if k != "revised_blocks"}
    s = json.dumps(rest, ensure_ascii=False)
    for b in r["revised_blocks"]:
        assert b["text"] not in s
    assert sum(json.dumps(r, ensure_ascii=False).count(b["text"]) for b in r["revised_blocks"]) == len(r["revised_blocks"])


def test_writes_nothing_to_disk(tmp_path, monkeypatch):
    env = Env([F_OK()])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    import builtins
    import os
    real_open, real_os_open = builtins.open, os.open

    def guard_open(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+"):
            raise AssertionError(f"disk write attempted: {file}")
        return real_open(file, mode, *a, **k)

    def guard_os_open(path, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            raise AssertionError(f"disk write attempted: {path}")
        return real_os_open(path, flags, *a, **k)

    monkeypatch.setattr(builtins, "open", guard_open)
    monkeypatch.setattr(os, "open", guard_os_open)
    assert env.build()["ok"]
    assert list(tmp_path.rglob("*")) == []


def test_multi_edit_same_block_coordinates_restore_original():
    env = Env([fnd("F-1", 3, "p1", "빼돌린 것으로 보고 있다고", "빼돌렸다는 취지로"), fnd("F-2", 2, "p1", "김씨는 혐의를 모두 인정했다고 한다", "김씨는 혐의를 인정했다")])
    r = env.build()
    edits = [e for a in r["applied"] for e in a["edits"]]
    assert len(edits) == 2 and edits[0]["revised_start"] != edits[0]["start"] or edits[1]["revised_start"] != edits[1]["start"]
    assert revise(r) == {b: t for b, _, t in BLOCKS}


def test_revision_text_registered_for_log_redaction():
    env = Env([fnd("F-1", 3, "p1", "빼돌린 것으로 보고 있다고", "빼돌렸다는 취지로 새롭게 밝혔다고")])
    env.build()
    assert logsafe.contains_sensitive("로그 빼돌렸다는 취지로 새롭게 밝혔다고 문장")
    env.store.delete(f"article:{env.handle}")
    assert not logsafe.contains_sensitive("로그 빼돌렸다는 취지로 새롭게 밝혔다고 문장")       # 원고가 사라지면 변경본 색인도 지운다


# --- 서버 연결 ----------------------------------------------------------


def test_server_registers_tool_and_instructions():
    srv = Server(corpus=corpus_mod.FixtureCorpus())
    names = [t["name"] for t in srv._tools]
    assert len(names) == 6 and "build_revision" in names
    t = srv._by_name["build_revision"]
    assert t["inputSchema"]["required"] == ["report_id", "article_sha256"]
    assert set(t["inputSchema"]["properties"]) == {"report_id", "article_sha256", "finding_ids"}
    assert t["inputSchema"]["additionalProperties"] is False
    assert t["annotations"]["readOnlyHint"] is True and t["annotations"]["idempotentHint"] is True
    assert "필요 시 explain_finding 또는 build_revision" in mcp_server.INSTRUCTIONS
    assert "원고와 보고서는 서버 메모리에만 30분 보관한다" in mcp_server.INSTRUCTIONS


def test_server_roundtrip_and_error_code(monkeypatch):
    monkeypatch.setenv("PRESS_REVIEW_ALLOW_SYNTHETIC", "1")
    srv = Server(corpus=corpus_mod.FixtureCorpus())
    art = make_article(BLOCKS)
    srv.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})

    def call(name, args):
        return srv.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]

    p = call("prepare_article", {"article": art})["structuredContent"]
    sub = call("submit_report", {"article_handle": p["article_handle"], "findings": [fnd("F-1", 3, "title", "횡령 의혹", "횡령 혐의")]})
    assert sub["isError"] is False, sub
    rid = sub["structuredContent"]["report_id"]
    ok = call("build_revision", {"report_id": rid, "article_sha256": p["article_sha256"]})
    assert ok["isError"] is False and ok["structuredContent"]["applied"][0]["finding_id"] == "F-1"
    bad = call("build_revision", {"report_id": rid, "article_sha256": "0" * 64})
    assert bad["isError"] is True and bad["structuredContent"]["error_code"] == "HASH_MISMATCH"
    assert call("build_revision", {"report_id": rid})["isError"] is True          # 필수 인자 누락
    assert call("build_revision", {"report_id": rid, "article_sha256": p["article_sha256"], "replacement": "x"})["isError"] is True  # 대체문 입력 불가


# --- 흐름 재검사 --------------------------------------------------------


def test_flow_warning_when_only_headline_finding_applied():
    env = Env([fnd("F-1", 3, "title", "횡령 의혹", "횡령 기소"), fnd("F-2", 1, "p1", "혐의를 모두 인정했다고 한다", "혐의를 인정했다고 한다")])
    r = env.build()
    assert ids(r["applied"]) == ["F-1"] and ids(r["proposals"]) == ["F-2"]
    codes = [w["code"] for w in r["flow_warnings"]]
    assert "STAGE_MISMATCH_INTRODUCED" in codes
    assert any("흐름 경고" in n and "기자 확인 필요" in n for n in r["notes"])
    assert r["unresolved_count"] == 0                      # 별도 집계: unresolved_count 에 더하지 않는다
    assert "횡령 기소" in r["revised_blocks"][0]["text"]    # 변경본은 그대로 만든다


def test_flow_warnings_empty_when_consistent():
    env = Env([fnd("F-1", 3, "title", "횡령 의혹", "횡령 혐의"), fnd("F-2", 2, "p1", "혐의를 모두 인정했다고 한다", "혐의를 인정했다고 한다")])
    r = env.build()
    assert r["applied"] and r["flow_warnings"] == []
    assert not any("흐름 경고" in n for n in r["notes"])
    assert Env([fnd("F-1", 3, "title", "횡령 의혹", None, mode="verify_first")]).build()["flow_warnings"] == []


def test_flow_warning_holds_no_article_text():
    env = Env([fnd("F-1", 3, "title", "횡령 의혹", "횡령 기소")])
    r = env.build()
    assert r["flow_warnings"]
    s = json.dumps(r["flow_warnings"], ensure_ascii=False) + "".join(r["notes"])
    assert all(t not in s for _, _, t in BLOCKS) and "횡령 의혹" not in s and "횡령 기소" not in s


# --- 조립 결과 검사(F1, F2) ----------------------------------------------

B_NUM = [("title", "title", "사업비 보고"), ("p1", "body", "사업비는 1억여 원이다. 별도 예산은 2억원이다."), ("p2", "body", "담당자는 일정을 설명했다.")]
B_Q = [("title", "title", "조사 경과"), ("p1", "body", "조사는 계속된다고 했다."), ("p2", "body", "관계자가 설명했다고 한다.")]


def test_assembled_new_number_across_edit_boundary_is_dropped():
    env = Env([fnd("F-1", 3, "p1", "억여", "2억", arts=B_NUM)], blocks=B_NUM)
    r = env.build()
    assert r["applied"] == [] and r["skipped"][0]["reason_code"] == "NEW_NUMBER_ASSEMBLED"
    assert next(b["text"] for b in r["revised_blocks"] if b["block_id"] == "p1") == B_NUM[1][2]
    assert "12" not in json.dumps(r["skipped"])


def test_assembled_new_direct_quote_is_dropped():
    env = Env([fnd("F-1", 3, "p1", "조사는 계속된다", "“조사는 계속된다”", arts=B_Q)], blocks=B_Q)
    r = env.build()
    assert r["applied"] == [] and r["skipped"][0]["reason_code"] == "NEW_QUOTE_ASSEMBLED"


def test_quote_marks_split_across_findings_are_dropped():
    env = Env([fnd("F-1", 3, "p1", "조사는", "“조사는", arts=B_Q), fnd("F-2", 3, "p2", "설명했다고", "설명했다고”", arts=B_Q)], blocks=B_Q)
    r = env.build()
    assert r["applied"] == []
    assert {s["reason_code"] for s in r["skipped"]} == {"UNPAIRED_QUOTE_ASSEMBLED"}
    assert all(b["text"] == t for b, (_, _, t) in zip(r["revised_blocks"], B_Q))


def test_assembled_check_still_allows_normal_edits_in_different_blocks():
    env = Env([fnd("F-1", 3, "p1", "별도 예산은 2억원이다", "별도 예산은 2억원이다만", arts=B_NUM), fnd("F-2", 2, "p2", "일정을 설명했다", "일정을 밝혔다", arts=B_NUM)], blocks=B_NUM)
    r = env.build()
    assert ids(r["applied"]) == ["F-1", "F-2"] and r["skipped"] == []


# --- 만료 경쟁·마스킹 키(F3, F4) ------------------------------------------

def test_expiry_during_build_releases_masking_and_rejects(monkeypatch):
    env = Env([fnd("F-1", 3, "p1", "빼돌린 것으로 보고 있다고", "빼돌렸다는 취지로 새롭게 밝혔다고")])
    real = logsafe.register_article

    def slow(key, art, **kw):
        real(key, art, **kw)
        env.clock.t = 1801          # 등록 직후 원고가 만료된 경쟁
    monkeypatch.setattr(logsafe, "register_article", slow)
    r = env.build()
    assert r["ok"] is False and r["error_code"] == "ARTICLE_EXPIRED"
    env.clock.t = 3601
    env.store.purge_expired()
    assert not logsafe.contains_sensitive("로그 빼돌렸다는 취지로 새롭게 밝혔다고 문장")


def test_second_build_keeps_first_revision_masked_until_article_expires():
    env = Env([fnd("F1", 3, "p1", "빼돌린 것으로 보고 있다고", "빼돌렸다는 취지로 첫번째 문구입니다"), fnd("F2", 3, "p2", "회사는 입장을 내지 않았다", "회사는 두번째 문구를 밝히지 않았다")])
    env.build(finding_ids=["F1"])
    env.build(finding_ids=["F2"])
    assert logsafe.contains_sensitive("로그 첫번째 문구입니다 끝")
    assert logsafe.contains_sensitive("로그 두번째 문구를 밝히지 끝")
    env.clock.t = 1801
    env.store.purge_expired()
    assert not logsafe.contains_sensitive("로그 첫번째 문구입니다 끝")
    assert not logsafe.contains_sensitive("로그 두번째 문구를 밝히지 끝")


# --- 2회차 보강 ----------------------------------------------------------

B_MOVE = [("title", "title", "조사 경과"), ("p1", "body", "관계자는 “조사는 계속된다”고 했다."), ("p2", "body", "그는 조사는 계속된다고 전했다.")]
B_DATE = [("title", "title", "회의 소식"), ("p1", "body", "회의는 2026.10.17 열렸다고 밝혔다. 예산은 1,234만 원이고 3.5퍼센트 올랐다고 밝혔다. 응답률은 45%였다."),]
B_UNIT = [("title", "title", "설문 결과"), ("p1", "body", "응답자는 2명으로 집계됐다. 비율은 5%였다.")]


def test_quote_moved_inside_one_finding_is_dropped():
    env = Env([fnd("F-1", 3, "p2", "조사는 계속된다", "“조사는 계속된다”", related=[("p1", "“조사는 계속된다”", "조사는 계속된다")], arts=B_MOVE)], blocks=B_MOVE)
    r = env.build()
    assert r["applied"] == [] and r["skipped"][0]["reason_code"] == "NEW_QUOTE_IN_EDIT"


def test_edit_keeping_its_own_quote_is_allowed():
    env = Env([fnd("F-1", 3, "p1", "관계자는 “조사는 계속된다”고 했다", "관계자는 “조사는 계속된다”고 밝혔다", arts=B_MOVE)], blocks=B_MOVE)
    assert ids(env.build()["applied"]) == ["F-1"]


@pytest.mark.parametrize("quote,rep", [("밝혔다. 예산", "설명했다. 예산"), ("올랐다고 밝혔다", "올랐다고 설명했다"), ("열렸다고 밝혔다", "열렸다고 설명했다")])
def test_existing_numbers_with_dots_commas_percent_do_not_block_edit(quote, rep):
    env = Env([fnd("F-1", 3, "p1", quote, rep, arts=B_DATE)], blocks=B_DATE)
    r = env.build()
    assert ids(r["applied"]) == ["F-1"], r["skipped"]


def test_assembled_new_number_unit_pair_is_dropped():
    env = Env([fnd("F-1", 3, "p1", "명으로", "%로", arts=B_UNIT)], blocks=B_UNIT)
    r = env.build()
    assert r["applied"] == [] and r["skipped"][0]["reason_code"] == "NEW_NUMBER_UNIT_ASSEMBLED"


def test_many_revisions_use_one_masking_slot(monkeypatch):
    names = "일이삼사오육칠팔"
    p1 = " ".join(f"원래구절{n}번입니다" for n in names)
    blocks = [("title", "title", "제목입니다"), ("p1", "body", p1), ("p2", "body", "항상바뀌는원래문장입니다")]
    fs = [fnd("F0", 3, "p2", "항상바뀌는원래문장입니다", "항상바뀐새로운문장입니다", arts=blocks)]
    fs += [fnd(f"G{n}", 3, "p1", f"원래구절{n}번입니다", f"새로바꾼문구{n}번입니다", arts=blocks) for n in names]
    env = Env(fs, blocks=blocks)
    monkeypatch.setattr(logsafe, "MAX_REGISTERED", 5)
    for n in names:
        assert env.build(finding_ids=["F0", f"G{n}"])["ok"]
    assert logsafe.contains_sensitive("로그 항상바뀌는원래문장입니다 끝")       # 원문 색인이 밀려나지 않는다
    for n in names:
        assert logsafe.contains_sensitive(f"로그 새로바꾼문구{n}번입니다 끝")
    env.clock.t = 1801
    env.store.purge_expired()
    assert not logsafe.contains_sensitive("로그 새로바꾼문구일번입니다 끝")


# --- 3회차: 조립 후 부정어·고유명사, 제출 TTL 경계 ------------------------

from conftest import make_article as _mk
from press_review import edit_checks as _ec

B_NEG = [("title", "title", "회의 소식"), ("p1", "body", "잘간다.")]
B_NAME = [("title", "title", "회의 소식"), ("p1", "body", "담당자는 갑을 병이라고 했다.")]


def test_assembled_negation_created_by_adjacent_edits_is_dropped():
    env = Env([fnd("F-1", 2, "p1", "잘", "못", arts=B_NEG), fnd("F-2", 2, "p1", "간다", "했다", arts=B_NEG)], blocks=B_NEG)
    r = env.build()
    assert ids(r["applied"]) == ["F-1"]                                   # 앞선 F-1 이 먼저 수락되고 F-2 가 탈락한다
    assert [(s["finding_id"], s["reason_code"]) for s in r["skipped"]] == [("F-2", "NEGATION_ASSEMBLED")]
    rb = {b["block_id"]: b["text"] for b in r["revised_blocks"]}
    assert "못했다" not in rb["p1"]
    for bid, _, t in B_NEG:
        assert _ec.count_negations(rb[bid]) == _ec.count_negations(t)


def test_assembled_proper_noun_created_by_adjacent_edits_is_dropped():
    env = Env([fnd("F-1", 2, "p1", "갑", "김", arts=B_NAME), fnd("F-2", 2, "p1", "을", "씨", arts=B_NAME)], blocks=B_NAME)
    r = env.build()
    assert ids(r["applied"]) == ["F-1"]
    assert [(s["finding_id"], s["reason_code"]) for s in r["skipped"]] == [("F-2", "NEW_PROPER_NOUN_ASSEMBLED")]
    assert "김씨" not in next(b["text"] for b in r["revised_blocks"] if b["block_id"] == "p1")


def test_balanced_negation_edits_and_existing_names_are_not_dropped():
    env = Env([fnd("F-1", 2, "p2", "회사는 입장을 내지 않았다", "회사는 입장을 내지 않았다고 밝혔다"),
               fnd("F-2", 2, "p1", "김씨는 혐의를 모두 인정했다고 한다", "김씨는 혐의를 인정했다고 한다"),
               fnd("F-3", 2, "p1", "경찰은 김씨가", "수사팀은 김씨가")])
    r = env.build()
    assert ids(r["applied"]) == ["F-1", "F-2", "F-3"] and r["skipped"] == []


def test_two_negation_balanced_edits_in_one_block_are_allowed():
    blocks = [("title", "title", "소식"), ("p1", "body", "그는 가지 않았다. 그는 오지 않았다.")]
    env = Env([fnd("F-1", 2, "p1", "가지 않았다", "향하지 않았다", arts=blocks), fnd("F-2", 2, "p1", "오지 않았다", "들르지 않았다", arts=blocks)], blocks=blocks)
    assert ids(env.build()["applied"]) == ["F-1", "F-2"]


def _raw_submit(env_store, handle, findings):
    return review.submit_report(env_store, corpus_mod.FixtureCorpus(), article_handle=handle, findings=findings, now=NOW)


def _prep():
    clock = Clock()
    logsafe.reset()
    store = review.new_store(clock=clock)
    prep = review.prepare_article(store, article=make_article(BLOCKS))
    return clock, store, prep["article_handle"]


def test_submit_rejects_when_article_just_expired(monkeypatch):
    clock, store, h = _prep()
    monkeypatch.setattr(store, "remaining_ttl", lambda key: None)
    out = _raw_submit(store, h, [F_OK()])
    assert out["ok"] is False and "unknown or expired" in out["errors"][0]
    assert not any(k.startswith("report:") for k in store._items)


def test_submit_removes_report_if_article_expires_right_after_put(monkeypatch):
    clock, store, h = _prep()
    real_put = store.put

    def put(key, value, ttl_seconds=None):
        exp = real_put(key, value, ttl_seconds)
        if key.startswith("report:"):
            clock.t = 1801
        return exp
    monkeypatch.setattr(store, "put", put)
    out = _raw_submit(store, h, [F_OK()])
    assert out["ok"] is False and "unknown or expired" in out["errors"][0]
    assert not any(k.startswith("report:") for k in store._items)


def test_submit_normal_path_report_ttl_follows_article():
    clock, store, h = _prep()
    clock.t = 600
    out = _raw_submit(store, h, [F_OK()])
    assert out["ok"] is True
    assert store.remaining_ttl(f"report:{out['report_id']}") <= store.remaining_ttl(f"article:{h}") + 1e-6

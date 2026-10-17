"""edit_checks.check_flow: 수정안 적용 사본의 혐의 단계 모순과 수정 구간 겹침(P3_DESIGN §3)."""
import datetime as dt

from press_review import edit_checks, review

NOW = dt.datetime(2026, 10, 9, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def art(title, sub, body):
    blocks = [("title", "title", title)]
    if sub:
        blocks.append(("sub", "subtitle", sub))
    blocks.append(("p1", "body", body))
    return {
        "schema_version": "0.1", "article_id": "t-flow", "publication_date": "2026-10-05",
        "blocks": [{"block_id": b, "section": s, "text": t} for b, s, t in blocks],
        "context": {"genre": "news", "election_context": None, "verification_notes": [], "sensitive_material": False},
    }


def edit(article, block, old, new, label=None):
    t = {b["block_id"]: b["text"] for b in article["blocks"]}[block]
    s = t.index(old)
    return {"block_id": block, "start": s, "end": s + len(old), "replacement": new, "label": label or f"{block}:{old}"}


def codes(issues):
    return sorted(i.code for i in issues)


def test_no_edits_no_issues():
    a = art("대표 유죄 확정", None, "경찰이 수사 중이다.")
    assert edit_checks.check_flow(a, []) == []


def test_edit_that_introduces_title_ahead_of_body():
    a = art("대표 횡령 혐의", None, "경찰이 수사 중이다.")
    issues = edit_checks.check_flow(a, [edit(a, "title", "횡령 혐의", "횡령 유죄 확정")])
    assert "STAGE_MISMATCH_INTRODUCED" in codes(issues) and "STAGE_WORD_ADDED" in codes(issues)
    assert all(i.level == "warning" for i in issues)


def test_mismatch_that_the_edit_does_not_resolve_is_reported_as_remaining():
    a = art("대표 유죄 확정", None, "경찰이 수사 중이다. 대표는 부인했다.")
    issues = edit_checks.check_flow(a, [edit(a, "p1", "대표는 부인했다", "대표는 혐의를 부인했다")])
    assert "STAGE_MISMATCH_REMAINS" in codes(issues) and "STAGE_MISMATCH_INTRODUCED" not in codes(issues)


def test_edit_that_fixes_the_title_leaves_no_mismatch():
    a = art("대표 유죄 확정", None, "경찰이 수사 중이다.")
    issues = edit_checks.check_flow(a, [edit(a, "title", "유죄 확정", "혐의 수사")])
    assert "STAGE_MISMATCH_INTRODUCED" not in codes(issues) and "STAGE_MISMATCH_REMAINS" not in codes(issues)


def test_body_covering_the_title_stage_is_consistent():
    a = art("대표 기소", "검찰, 구속기소", "검찰은 대표를 불구속 기소했다고 밝혔다.")
    assert edit_checks.check_flow(a, [edit(a, "p1", "밝혔다", "전했다")]) == []


def test_non_indictment_words_are_not_stage_words():
    a = art("대표 불기소", None, "검찰은 대표를 불기소 처분했다.")
    assert edit_checks.check_flow(a, [edit(a, "p1", "처분했다", "처분했다고 한다")]) == []


def test_new_stage_word_in_body_edit():
    a = art("대표 수사", None, "경찰이 대표를 수사 중이다.")
    issues = edit_checks.check_flow(a, [edit(a, "p1", "수사 중이다", "수사해 기소 의견을 냈다")])
    assert [i for i in issues if i.code == "STAGE_WORD_ADDED" and "기소" in i.message]


def test_overlapping_edits_are_errors_and_second_is_not_applied():
    a = art("대표 횡령 혐의 수사", None, "본문 입니다.")
    e1 = {"block_id": "title", "start": 3, "end": 8, "replacement": "A", "label": "f1"}
    e2 = {"block_id": "title", "start": 6, "end": 11, "replacement": "B", "label": "f2"}
    issues = edit_checks.check_flow(a, [e1, e2])
    assert [i.code for i in issues if i.level == "error"] == ["EDIT_OVERLAP"]
    assert "f2 overlaps f1" in issues[0].message


def test_invalid_edit_is_reported_not_raised():
    a = art("대표 수사", None, "본문")
    bad = [{"block_id": "nope", "start": 0, "end": 1, "replacement": "x"}, {"block_id": "title", "start": 5, "end": 99, "replacement": "x"},
           {"block_id": "title", "start": 0, "end": 1, "replacement": 3}]
    assert codes(edit_checks.check_flow(a, bad)) == ["EDIT_INVALID"] * 3


def finding(fid, quote_block, quote, replacement, **kw):
    return {"finding_id": fid, "span": kw.pop("span"), "priority_stars": 2, "risk_channels": ["civil"], "issue_category": kw.pop("cat", "unsupported_assertion"),
            "review_confidence": "medium", "reason_short": "확정성을 확인하세요.",
            "suggested_edit": {"mode": "replace", "replacement": replacement, "related_edits": kw.pop("related", []), "required_verification": []},
            "evidence": [], "counter_considerations": []}


def test_flow_warning_reaches_submit_report_warnings(corpus):
    a = art("대표 횡령 혐의", None, "경찰이 수사 중이다.")
    e = edit(a, "title", "횡령 혐의", "횡령 유죄 확정")
    f = finding("finding-001", "title", "횡령 혐의", "횡령 유죄 확정", span={"block_id": "title", "start": e["start"], "end": e["end"], "quote": "횡령 혐의"})
    res = review.build_report_ex(a, [f], {}, corpus, now=NOW)
    flows = [w for w in res.warnings if w.startswith("흐름검사")]
    assert any("STAGE_MISMATCH_INTRODUCED" in w for w in flows)
    # 겹침은 contracts 가 오류로 보고한다(흐름검사가 같은 말을 두 번 하지 않는다)
    f2 = finding("finding-002", "title", "혐의", "의혹", span={"block_id": "title", "start": e["start"] + 3, "end": e["end"], "quote": "혐의"})
    both = review.build_report_ex(a, [f, f2], {}, corpus, now=NOW)
    assert both.report is None and sum("edit conflict" in x for x in both.errors) == 1
    assert not any("EDIT_OVERLAP" in w for w in both.warnings)


def test_repeated_issue_is_flagged_for_grouping(corpus):
    a = art("대표 횡령 혐의", None, "경찰은 대표의 횡령 혐의를 수사 중이다. 횡령 혐의는 부인됐다.")
    t = a["blocks"][1]["text"]
    s1 = t.index("횡령 혐의")
    s2 = t.index("횡령 혐의", s1 + 1)
    fs = [finding(f"finding-00{i}", "p1", "횡령 혐의", "횡령 의혹", span={"block_id": "p1", "start": s, "end": s + 5, "quote": "횡령 혐의"})
          for i, s in ((1, s1), (2, s2))]
    res = review.build_report_ex(a, fs, {}, corpus, now=NOW)
    assert any("반복 쟁점" in w and "related_edits" in w for w in res.warnings)

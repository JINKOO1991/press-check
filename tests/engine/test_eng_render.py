import copy

from press_review import config, render


def finding(i, stars=2, block="p1", start=0, quote="문장", evidence=True, mode="replace", **kw):
    f = {
        "finding_id": f"finding-{i:03d}",
        "span": {"block_id": block, "start": start, "end": start + len(quote), "quote": quote},
        "priority_stars": stars, "risk_channels": ["civil"], "issue_category": "cat", "review_confidence": "medium",
        "reason_short": f"사유 {i}입니다.",
        "suggested_edit": {"mode": mode, "replacement": "수정문" if mode == "replace" else None, "related_edits": [], "required_verification": []},
        "evidence": [{"source_id": "s", "passage_id": "p", "url": "https://example.invalid/x", "locator": {"kind": "pdf_page", "value": "1"},
                      "authority_type": "judgment", "verified": True}] if evidence else [],
        "counter_considerations": [], "detailed_explanation_ref": f"r/finding-{i:03d}",
        "validation_status": "verified" if evidence else "needs_information",
    }
    f.update(kw)
    return f


def report(findings, **kw):
    r = {"execution_mode": "live", "overall_status": "findings" if findings else "partial", "coverage_notes": [],
         "findings": findings, "hidden_finding_count": max(0, len(findings) - 5), "report_id": "r"}
    r.update(kw)
    return r


def test_first_line_and_four_columns(example_review):
    t = render.render_table(example_review)
    lines = t.splitlines()
    assert lines[0] == "별(★)은 실제 분쟁 발생 확률이 아니라 검토 우선순위입니다."
    assert "| 문제 요소 | 검토 필요도 | 사유 | 추천 수정안 |" in lines
    row = next(l for l in lines if "finding-001" in l)
    cells = [c.strip() for c in row.strip("|").split(" | ")]
    assert len(cells) == 4
    assert cells[1] == "★★★"
    assert "혐의…경찰, 대표 수사" in cells[3]
    assert "%" not in t


def test_star_strings():
    assert [render.stars(n) for n in (1, 2, 3)] == ["★☆☆", "★★☆", "★★★"]


def test_sort_by_stars_then_input_order_and_hidden_count():
    fs = [finding(i, stars=s) for i, s in enumerate([1, 3, 2, 3, 1, 2, 1], 1)]
    t = render.render_table(report(fs), top_n=5)
    rows = [l for l in t.splitlines() if l.startswith("| ") and "finding-" in l]
    assert len(rows) == 5
    order = [r.split("[finding-")[1][:3] for r in rows]
    assert order == ["002", "004", "003", "006", "001"]            # 별 내림차순, 같은 별은 입력(원문 위치) 순
    assert "숨김 2건" in t
    assert "숨김" not in render.render_table(report(fs), top_n=7)
    assert render.render_table(report(fs), top_n=2).count("finding-0") == 2 and "숨김 5건" in render.render_table(report(fs), top_n=2)


def test_default_top_n_is_five():
    assert config.DISPLAY_TOP_N == 5
    t = render.render_table(report([finding(i) for i in range(1, 9)]))
    assert t.count("[finding-") == 5 and "숨김 3건" in t


def test_no_evidence_marked_and_verify_first_prefix():
    f1 = finding(1, evidence=False)
    f2 = finding(2, mode="verify_first")
    f2["suggested_edit"]["required_verification"] = ["당사자 답변 여부"]
    f3 = finding(3, mode="none")
    t = render.render_table(report([f1, f2, f3]))
    r1, r2, r3 = [next(l for l in t.splitlines() if f"[finding-00{i}]" in l) for i in (1, 2, 3)]
    assert "[근거 미확보]" in r1 and "[근거 미확보]" not in r2
    assert "발행 전 확인: 당사자 답변 여부" in r2
    assert "추천 수정 없음" in r3


def test_cells_are_escaped_and_truncated():
    f = finding(1, quote="가|나\n다" + "라" * 100)
    f["reason_short"] = "a|b"
    t = render.render_table(report([f]))
    row = next(l for l in t.splitlines() if "finding-001" in l)
    assert "\\|" in row and "\n" not in row
    assert "…" in row


def test_empty_report_variants():
    t = render.render_table(report([], overall_status="no_actionable_findings", coverage_notes=["범위 안내 한 줄"]))
    assert "법적 안전을 보장" in t and "범위 안내 한 줄" in t and "| 문제 요소" not in t
    assert "partial" in render.render_table(report([]))


def test_synthetic_banner(example_review):
    assert "합성(예제) 실행" in render.render_table(example_review)
    assert "합성(예제) 실행" not in render.render_table(report([finding(1)]))


def test_detail_has_evidence_and_explanation(example_review):
    d = render.render_detail(example_review, "finding-001", {"applicable_issue": "쟁점", "required_checks": ["a", "b"], "extra_key": "x"})
    assert "https://www.law.go.kr/LSW/precInfoP.do?precSeq=194652" in d
    assert "판결요지 [2]" in d and "**적용 쟁점**" in d and "- a" in d and "**extra_key**" in d
    assert "별(★)은 실제 분쟁 발생 확률이 아니라" in d
    assert "상세 설명이 제출되지 않았습니다" in render.render_detail(example_review, "finding-001", None)
    r = copy.deepcopy(example_review)
    r["findings"][0]["evidence"] = []
    assert "근거 미확보" in render.render_detail(r, "finding-001", None)


def test_detail_unknown_finding(example_review):
    import pytest
    with pytest.raises(KeyError):
        render.render_detail(example_review, "finding-999", None)

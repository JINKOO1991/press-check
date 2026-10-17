"""표와 상세 설명 렌더링(markdown). 별은 검토 우선순위이며 확률이 아니다."""
from __future__ import annotations

from typing import Any

from . import config

STAR_NOTE = "별(★)은 실제 분쟁 발생 확률이 아니라 검토 우선순위입니다."
STAR_MEANING = {1: "★☆☆ 확인 권고", 2: "★★☆ 수정·보강 검토", 3: "★★★ 발행 전 집중 검토"}
HEADER = "| 문제 요소 | 검토 필요도 | 사유 | 추천 수정안 |"
MAX_QUOTE_CELL = 60

_DETAIL_LABELS = [
    ("applicable_issue", "적용 쟁점"),
    ("article_context", "기사 맥락"),
    ("support", "지지 근거"),
    ("differences", "사례와의 차이"),
    ("counter_arguments", "표현의 자유·공익 보도 측 근거"),
    ("required_checks", "필요한 취재 확인"),
    ("uncertainty", "불확실성"),
]


def stars(n: int) -> str:
    return "★" * n + "☆" * (3 - n)


def _cell(s: str) -> str:
    return " ".join(str(s).replace("|", "\\|").split())


def _short(s: str, n: int = MAX_QUOTE_CELL) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _edit_cell(f: dict) -> str:
    se = f["suggested_edit"]
    ver = "; ".join(se.get("required_verification") or [])
    if se["mode"] == "replace":
        return se["replacement"] + (f" (발행 전 확인: {ver})" if ver else "")
    if se["mode"] == "verify_first":
        return "발행 전 확인: " + (ver or "(확인 항목 미기재)")
    return "추천 수정 없음" + (f" (확인: {ver})" if ver else "")


def render_table(report: dict, top_n: int = config.DISPLAY_TOP_N) -> str:
    findings = sorted(report["findings"], key=lambda f: -f["priority_stars"])  # 안정 정렬: 같은 별은 입력 순서(원문 위치순)
    shown = findings[: max(0, top_n)]
    hidden = len(findings) - len(shown)
    lines = [STAR_NOTE]
    if report.get("execution_mode") == "synthetic_example":
        lines.append("합성(예제) 실행입니다. 근거 passage는 실제 판결·결정이 아니며 법적 판단에 쓸 수 없습니다.")
    lines.append("")
    if shown:
        lines += [HEADER, "|---|---|---|---|"]
        for f in shown:
            sp = f["span"]
            problem = f"{_short(sp['quote'])} ({sp['block_id']} {sp['start']}-{sp['end']}) [{f['finding_id']}]"
            reason = f["reason_short"] + ("" if f["evidence"] else " [근거 미확보]")
            lines.append("| " + " | ".join([_cell(problem), stars(f["priority_stars"]), _cell(reason), _cell(_edit_cell(f))]) + " |")
        if hidden:
            lines += ["", f"숨김 {hidden}건: 더 보기를 요청하면 나머지를 표시합니다(finding_id로 상세 설명도 요청할 수 있습니다)."]
    else:
        lines.append(f"표시할 검토 항목이 없습니다 (상태: {report['overall_status']}).")
        if report["overall_status"] == "no_actionable_findings":
            lines.append("이는 법적 안전을 보장한다는 뜻이 아닙니다.")
    notes = report.get("coverage_notes") or []
    if notes:
        lines += ["", "범위 안내:"] + [f"- {n}" for n in notes]
    return "\n".join(lines)


def _as_lines(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def render_detail(report: dict, finding_id: str, explanation: dict | None) -> str:
    f = next((x for x in report["findings"] if x["finding_id"] == finding_id), None)
    if f is None:
        raise KeyError(finding_id)
    sp, se = f["span"], f["suggested_edit"]
    out = [
        f"## {f['finding_id']}: {f['issue_category']}",
        "",
        f"- 문제 구절: “{sp['quote']}” ({sp['block_id']}, code point {sp['start']}-{sp['end']})",
        f"- 검토 필요도: {STAR_MEANING[f['priority_stars']]} ({STAR_NOTE})",
        f"- 근거 확신도: {f['review_confidence']} / 검증 상태: {f['validation_status']}",
        f"- 위험 채널: {', '.join(f['risk_channels']) or '(없음)'}",
        f"- 사유: {f['reason_short']}",
        "",
        "### 추천 수정안",
        _edit_cell(f),
    ]
    for r in se.get("related_edits", []):
        rs = r["span"]
        out.append(f"- 연결 수정 ({rs['block_id']} {rs['start']}-{rs['end']}): “{rs['quote']}” -> {r['replacement']}")
    out += ["", "### 근거"]
    if f["evidence"]:
        for e in f["evidence"]:
            out.append(f"- {e['authority_type']} {e['source_id']}/{e['passage_id']} ({e['locator']['kind']}: {e['locator']['value']}) {e['url']}")
    else:
        out.append("- 근거 미확보 (확신도 low, 추가 정보 필요)")
    if f["counter_considerations"]:
        out += ["", "### 반대 고려"] + [f"- {c}" for c in f["counter_considerations"]]
    out += ["", "### 상세 설명"]
    if not explanation:
        out.append("상세 설명이 제출되지 않았습니다.")
    else:
        known = {k for k, _ in _DETAIL_LABELS}
        for key, label in _DETAIL_LABELS:
            if key in explanation:
                out.append(f"**{label}**")
                out += [f"- {x}" for x in _as_lines(explanation[key])]
        for key in explanation:
            if key not in known:
                out.append(f"**{_cell(key)}**")
                out += [f"- {x}" for x in _as_lines(explanation[key])]
    return "\n".join(out)

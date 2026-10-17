"""첨부 단위 인입 평가(G-D33, docs/P2_DESIGN.md): 시정권고 현황·IENDC 첨부마다 인용 일치율·격리율을 계산해 admitted/excluded 를 가른다.

기준: 첨부의 사건 인용 일치율(sum quote_ok / sum quote_total) >= MIN_QUOTE_RATE 그리고 격리율(격리 사건 / 사건) <= MAX_QUARANTINE_RATE.
기준값은 사용자 결정(2026-10-10). 이 기준을 정하기 전에 문제 첨부(bn3335, 1949#1)를 이미 봤으므로 사후 선택 편향이 있다(설계 기록 참조).
평가(``evaluate``)는 읽기 전용이다. 쓰기(``apply``)는 제외 사유를 failures(stage='admission')에 남기고 통과한 첨부의 이전 제외 기록을 해결 처리한다.
색인 쪽(``corpus_sqlite._iter_passages``)은 미해결 admission 실패가 있는 첨부의 사건을 뺀다. 구조화되지 않은 첨부(사건 0건)는 '미평가'다.
"""
from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path
from typing import Optional

from .manifest import Manifest

MIN_QUOTE_RATE = 0.85          # 사용자 결정 2026-10-10 (G-D33). CLI --min-quote-rate 로 바꿀 수 있다
MAX_QUARANTINE_RATE = 0.20     # 사용자 결정 2026-10-10 (G-D33). CLI --max-quarantine-rate 로 바꿀 수 있다
ADMISSION_SOURCES = ("pac_board_correction_status", "iendc_review_results")   # 선거백서(pac_election_whitepapers)는 적용 대상이 아니다
# 완결성 검사(2026-10-16): 본문의 사건번호 수 대비 장부 사건 수. bn1983 이 본문 218행 중 113건만 추출되고도 인용·격리 기준을 통과했다.
# 실측(시정권고 현황 구조화 첨부 15개): 정상 첨부 비율 0.99~1.00, 누락 첨부 0.52(bn1983 전)·0.66(bn2557). 사이 값은 표본이 없어 0.95 는 출발값이다.
MIN_COMPLETENESS = 0.95
# 소스별 사건번호 정규식(<br> 제거 뒤 적용). 선언하지 않은 소스(IENDC: 사건번호가 없는 사건이 대부분)는 검사를 건너뛰고 사유에 '완결성 미검사'를 남긴다.
COMPLETENESS_PATTERNS = {"pac_board_correction_status": (
    (re.compile(r"(\d{4})\s*안건\s*(\d+)"), "{0}안건{1}"),     # 2016안건648 · 2020안건<br>1 · 2020안건 1
    (re.compile(r"제\s*(\d{4})\s*-\s*(\d+)\s*호"), "{0}-{1}"),  # 제2026-514호
)}
STAGE = "admission"
ERROR_CLASS = "attachment_excluded"
# F4(2026-10-16): 사건 행이 없는 통계 요약 문서는 사건 추출 대상이 아니다. 판정은 장부 값만 쓴다: 사건이 있고 전부 격리됐고 사건번호를 가진 사건이 하나도 없다.
# 시정권고 현황에만 적용한다(IENDC 사건은 사건번호가 없는 것이 흔하지 않은지 확인하지 않았다). 이 첨부는 제외 실패 장부에 쓰지 않는다(격리 사건은 어차피 색인에서 빠진다).
NO_CASE_ROWS_SOURCES = ("pac_board_correction_status",)

_SQL = """
SELECT a.att_id AS att_id, COUNT(c.case_id) AS cases,
       COALESCE(SUM(CASE WHEN c.verification_status='quarantined' THEN 1 ELSE 0 END), 0) AS quarantined,
       COALESCE(SUM(c.quote_total), 0) AS quote_total, COALESCE(SUM(c.quote_ok), 0) AS quote_ok,
       COALESCE(SUM(CASE WHEN c.case_number IS NOT NULL AND TRIM(c.case_number) <> '' THEN 1 ELSE 0 END), 0) AS numbered
FROM attachments a JOIN documents d ON d.doc_id = a.doc_id
LEFT JOIN cases c ON c.att_id = a.att_id
WHERE d.source_id = ?
GROUP BY a.att_id ORDER BY a.att_id
"""


def _frac(x: float) -> Fraction:
    return Fraction(str(x))      # 0.85 를 이진 부동소수점이 아니라 정확한 85/100 으로 비교한다


def case_keys(text: str, source_id: str) -> Optional[set]:
    """정규화한 사건번호 집합. 정규식 미선언 소스는 None. 같은 번호는 한 행으로 센다(중복 언급·한 행 내 반복 오탐 방지; 합계·머리글 행에는 번호가 없다)."""
    pats = COMPLETENESS_PATTERNS.get(source_id)
    if pats is None:
        return None
    text = re.sub(r"<br\s*/?>", " ", text)
    return {fmt.format(*m.groups()) for rx, fmt in pats for m in rx.finditer(text)}


def judge(cases: int, quarantined: int, quote_total: int, quote_ok: int, *,
          min_quote_rate: float = MIN_QUOTE_RATE, max_quarantine_rate: float = MAX_QUARANTINE_RATE,
          numbered_cases: Optional[int] = None, body_rows: Optional[int] = None, ledger_rows: Optional[int] = None,
          min_completeness: float = MIN_COMPLETENESS) -> tuple[str, list[str]]:
    """(status, reasons). status: admitted | excluded | unevaluated(사건 0건) | not_applicable(사건 행 없음, numbered_cases 를 준 경우만).
    body_rows(본문 사건번호 수)와 ledger_rows(그중 장부에 있는 수)를 주고 body_rows > 0 이면 완결성(ledger_rows/body_rows >= min_completeness)도 본다."""
    if cases == 0:
        return "unevaluated", []
    if numbered_cases == 0 and quarantined == cases:
        return "not_applicable", ["no_case_rows"]
    reasons = []
    if quote_total == 0:
        reasons.append("no_quotes")
    elif Fraction(quote_ok, quote_total) < _frac(min_quote_rate):
        reasons.append(f"quote_rate {quote_ok}/{quote_total} < {min_quote_rate}")
    if Fraction(quarantined, cases) > _frac(max_quarantine_rate):
        reasons.append(f"quarantine_rate {quarantined}/{cases} > {max_quarantine_rate}")
    if body_rows and Fraction(ledger_rows or 0, body_rows) < _frac(min_completeness):
        reasons.append(f"completeness {ledger_rows or 0}/{body_rows} < {min_completeness}")
    return ("excluded", reasons) if reasons else ("admitted", [])


def _body_text(m: Manifest, att_id: str, corpus_dir: Path) -> Optional[str]:
    """첨부의 쪽 텍스트를 이어 붙인다. 코퍼스 밖 경로·읽기 실패·경로 없음이면 None(완결성 미검사로 처리)."""
    root = corpus_dir.resolve()
    parts = []
    for r in m.pages_of(att_id=att_id):
        tp = r.get("text_path")
        if not tp:
            return None
        p = Path(tp)
        p = (p if p.is_absolute() else corpus_dir / p).resolve()
        try:
            if not p.is_relative_to(root):
                return None
            parts.append(p.read_text(encoding="utf-8"))
        except OSError:
            return None
    return "\n".join(parts) if parts else None


def evaluate(m: Manifest, source_id: str, *, min_quote_rate: float = MIN_QUOTE_RATE,
             max_quarantine_rate: float = MAX_QUARANTINE_RATE, min_completeness: float = MIN_COMPLETENESS,
             corpus_dir: Optional[Path] = None) -> dict:
    """읽기 전용. 반환: source, criteria, summary(건수), attachments(구조화된 첨부별 판정).
    corpus_dir 이 없거나 소스에 정규식이 없으면 완결성은 검사하지 않고 첨부 항목의 notes 에 '완결성 미검사'를 남긴다."""
    if source_id not in ADMISSION_SOURCES:
        raise ValueError(f"admission criteria do not apply to '{source_id}' (allowed: {', '.join(ADMISSION_SOURCES)})")
    rows = m.select(_SQL, (source_id,))
    ledger_nums: dict = {}
    if source_id in COMPLETENESS_PATTERNS:
        for r in m.select("SELECT c.att_id AS att_id, c.case_number AS n FROM cases c JOIN attachments a ON a.att_id = c.att_id "
                          "JOIN documents d ON d.doc_id = a.doc_id WHERE d.source_id = ?", (source_id,)):
            ledger_nums.setdefault(r["att_id"], set()).update(case_keys(r["n"] or "", source_id))
    summary = {"admitted": 0, "excluded": 0, "unevaluated": 0, "completeness_failed": 0, "completeness_unchecked": 0, "attachments": len(rows)}
    atts = []
    for r in rows:
        body_rows = ledger_rows = None
        note = None
        if r["cases"] > 0:
            if source_id not in COMPLETENESS_PATTERNS:
                note = "완결성 미검사(사건번호 정규식 미선언)"
            elif corpus_dir is None:
                note = "완결성 미검사(코퍼스 경로 없음)"
            else:
                text = _body_text(m, r["att_id"], Path(corpus_dir))
                if text is None:
                    note = "완결성 미검사(본문 텍스트를 읽지 못함)"
                else:
                    keys = case_keys(text, source_id)
                    body_rows, ledger_rows = len(keys), len(keys & ledger_nums.get(r["att_id"], set()))
                    if body_rows == 0:
                        note = "완결성 미검사(본문에서 사건번호 0건)"
        status, reasons = judge(r["cases"], r["quarantined"], r["quote_total"], r["quote_ok"],
                                min_quote_rate=min_quote_rate, max_quarantine_rate=max_quarantine_rate,
                                numbered_cases=r["numbered"] if source_id in NO_CASE_ROWS_SOURCES else None,
                                body_rows=body_rows, ledger_rows=ledger_rows, min_completeness=min_completeness)
        summary[status] = summary.get(status, 0) + 1
        if any(x.startswith("completeness ") for x in reasons):
            summary["completeness_failed"] += 1
        if note and status in ("admitted", "excluded"):
            summary["completeness_unchecked"] += 1
        if status != "unevaluated":
            atts.append({"att_id": r["att_id"], "status": status, "reasons": reasons, "cases": r["cases"], "quarantined": r["quarantined"],
                         "quote_ok": r["quote_ok"], "quote_total": r["quote_total"], "body_rows": body_rows, "ledger_rows": ledger_rows,
                         "notes": [note] if note else []})
    return {"source": source_id, "criteria": {"min_quote_rate": min_quote_rate, "max_quarantine_rate": max_quarantine_rate,
                                              "min_completeness": min_completeness},
            "summary": summary, "attachments": atts}


def apply(m: Manifest, report: dict) -> dict:
    """평가 결과를 장부에 쓴다: 제외 첨부는 failures(stage='admission')에 사유를 남기고, 통과 첨부의 이전 제외 기록은 해결 처리한다."""
    excluded = resolved = 0
    for a in report["attachments"]:
        if a["status"] == "excluded":
            m.record_failure(a["att_id"], STAGE, ERROR_CLASS,
                             "G-D33 attachment admission: " + "; ".join(a["reasons"]) + f" (cases={a['cases']})", retry_count=0)
            excluded += 1
        elif a["status"] == "admitted":
            resolved += m.resolve_failures(a["att_id"], STAGE)
    return {"failures_recorded": excluded, "failures_resolved": resolved}

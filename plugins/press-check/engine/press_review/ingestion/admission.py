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
from typing import Any, Optional

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


# ---- 사건 단위 인입 평가(G-D34, 판례·헌재 결정; 설계 caselaw_design_20261017 D6·D8, 반증 6) ----
CASE_ADMISSION_SOURCES = ("lawgo_prec_supreme", "lawgo_detc", "ccourt_detc")
# failures 에는 소스 열이 없고 target 이 case_id 뿐이라 orphan 해제는 case_id 접두로 소스를 가른다(한 소스의 apply 가 다른 소스의 행을 풀면 안 된다).
# lawgo_prec 접두는 실제 코퍼스에서 확인했다. lawgo_detc 접두는 어댑터가 아직 없어 같은 규칙(<source_family 접두>:)을 가정한 것이다.
CASE_ID_PREFIX = {"lawgo_prec_supreme": "lawgo_prec:", "lawgo_detc": "lawgo_detc:", "ccourt_detc": "ccourt_detc:"}
CASE_ERROR_CLASS = "case_excluded"
MANUAL_STAGE = "manual_exclusion"
MIGRATION_NOTE = "migrated from manual_exclusion 2026-10-17"

_CASE_SQL = """
SELECT c.case_id AS case_id, c.case_number AS case_number, c.verification_status AS status,
       c.quote_total AS quote_total, c.quote_ok AS quote_ok
FROM cases c JOIN documents d ON d.doc_id = c.doc_id
WHERE d.source_id = ? ORDER BY c.case_id
"""


def _open_case_rows(m: Manifest, prefix: str) -> set:
    rows = m.select("SELECT target FROM failures WHERE stage=? AND error_class=? AND resolved_at IS NULL", (STAGE, CASE_ERROR_CLASS))
    return {r["target"] for r in rows if r["target"].startswith(prefix)}


def _case_message(ok: int, total: int, min_quote_rate: float) -> str:
    return f"G-D34 case admission: quote_rate {ok}/{total} < {min_quote_rate}"


def evaluate_cases(m: Manifest, source_id: str, *, case_ids: Optional[list] = None, min_quote_rate: float = MIN_QUOTE_RATE) -> dict:
    """읽기 전용. 사건 1건 = 1단위. 격리 사건은 skipped_quarantined, quote_total=0 은 no_quotes('평가 제외': 기록하지 않는다).
    case_ids 를 주면 그 사건만 평가하고 orphan 해제를 하지 않는다(일부 실행이 다른 행을 풀지 않게)."""
    if source_id not in CASE_ADMISSION_SOURCES:
        raise ValueError(f"case admission does not apply to '{source_id}' (allowed: {', '.join(CASE_ADMISSION_SOURCES)})")
    prefix = CASE_ID_PREFIX[source_id]
    rows = m.select(_CASE_SQL, (source_id,))
    all_ids = {r["case_id"] for r in rows}
    want = set(case_ids) if case_ids else None
    open_rows = _open_case_rows(m, prefix)
    passed, excluded, record = 0, [], []          # record: 새로 기록할 행
    skipped_q = no_quotes = 0
    resolve, partial_scope = [], {}
    for r in rows:
        if want is not None and r["case_id"] not in want:
            continue
        if r["status"] == "quarantined":
            skipped_q += 1
            continue
        qt, qo = r["quote_total"] or 0, r["quote_ok"] or 0
        status, reasons = judge(1, 0, qt, qo, min_quote_rate=min_quote_rate)
        if reasons and reasons[0] == "no_quotes":
            no_quotes += 1
        elif status == "excluded":
            excluded.append(r["case_id"])
            if r["case_id"] not in open_rows:
                record.append({"case_id": r["case_id"], "message": _case_message(qo, qt, min_quote_rate)})
        else:
            passed += 1
            if r["case_id"] in open_rows:
                resolve.append(r["case_id"])
    ex = set(excluded)
    by_num: dict = {}
    for r in rows:
        if r["case_number"] and str(r["case_number"]).strip():
            by_num.setdefault(r["case_number"], []).append(r["case_id"])
    partial = sum(1 for ids in by_num.values() if len(ids) > 1 and 0 < len([i for i in ids if i in ex]) < len(ids))
    orphans = [] if want is not None else sorted(open_rows - all_ids)
    unknown = sorted(want - all_ids) if want is not None else []
    evaluated = skipped_q + no_quotes + passed + len(excluded)
    return {"source": source_id, "criteria": {"min_quote_rate": min_quote_rate},
            "summary": {"evaluated": evaluated, "admitted": passed, "excluded": len(excluded), "no_quotes": no_quotes,
                        "skipped_quarantined": skipped_q, "orphans_resolved": len(orphans),
                        "pending_changes": len(record) + len(resolve) + len(orphans), "case_number_partial": partial,
                        "unknown_cases": len(unknown)},
            "excluded_cases": excluded, "record": record, "resolve": resolve, "orphans": orphans}


def apply_cases(m: Manifest, report: dict) -> dict:
    """evaluate_cases 의 계획을 한 트랜잭션으로 쓴다: 새 제외 기록, 통과 사건의 이전 행 해결, orphan 해결. 이미 열린 제외 행은 건드리지 않는다."""
    with m.transaction():
        for r in report["record"]:
            m.record_failure(r["case_id"], STAGE, CASE_ERROR_CLASS, r["message"], retry_count=0)
        resolved = sum(m.resolve_failures(t, STAGE) for t in report["resolve"] + report["orphans"])
    return {"failures_recorded": len(report["record"]), "failures_resolved": resolved}


class MigrationError(Exception):
    pass


def _sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (bytes, bytearray)):
        return f"X'{bytes(v).hex()}'"
    return f"CAST(X'{str(v).encode('utf-8').hex()}' AS TEXT)"      # NUL·따옴표·개행이 있어도 리터럴이 깨지지 않는다


def _migration_plan(m: Manifest, source_id: str, min_quote_rate: float) -> list:
    prefix = CASE_ID_PREFIX[source_id]
    rows = m.select("SELECT target FROM failures WHERE stage=? AND resolved_at IS NULL ORDER BY id", (MANUAL_STAGE,))
    return [r["target"] for r in rows if r["target"].startswith(prefix)]


def migrate_manual(m: Manifest, source_id: str, *, min_quote_rate: float = MIN_QUOTE_RATE, undo_out: Optional[Path] = None) -> dict:
    """열린 manual_exclusion 행을 case_excluded 로 옮긴다(전부 한 트랜잭션, 하나라도 사전조건 위반이면 전체 롤백).
    undo_out 이 있으면 쓰기 전에 되돌리는 SQL 파일을 새로 만든다(이미 있으면 거부)."""
    if source_id != "lawgo_prec_supreme":
        raise ValueError("--migrate-manual applies to lawgo_prec_supreme only")
    targets = _migration_plan(m, source_id, min_quote_rate)
    if not targets:
        return {"migrated": 0, "case_ids": [], "undo_out": None}
    if undo_out is not None:
        sql = ["BEGIN;"]
        for t in targets:
            q = t.replace("'", "''")
            sql += [f"UPDATE cases SET verification_status='quarantined' WHERE case_id='{q}';",
                    f"UPDATE failures SET resolved_at=NULL WHERE target='{q}' AND stage='{MANUAL_STAGE}';",
                    f"DELETE FROM failures WHERE target='{q}' AND stage='{STAGE}';"]
            # 이전 admission 행(해결된 것 포함)은 record_failure 가 덮어쓰므로, 쓰기 전 값을 읽어 undo 가 그대로 복원한다. 컬럼은 SELECT * 결과에서 읽는다.
            for row in m.select("SELECT * FROM failures WHERE target=? AND stage=?", (t, STAGE)):
                cols = list(row)
                sql.append(f'INSERT INTO "failures"({", ".join(chr(34) + c.replace(chr(34), chr(34) * 2) + chr(34) for c in cols)}) '
                           f"VALUES({', '.join(_sql_literal(row[c]) for c in cols)});")
        sql.append("COMMIT;")
        with open(undo_out, "x", encoding="utf-8") as f:       # 기존 파일을 덮어쓰지 않는다
            f.write("\n".join(sql) + "\n")
    with m.transaction():
        for t in targets:
            c = m.get_case(t)
            if c is None:
                raise MigrationError(f"{t}: case not found")
            if c["verification_status"] != "quarantined":
                raise MigrationError(f"{t}: current status is {c['verification_status']}, expected quarantined")
            if (c["record"] or {}).get("verification_status") != "unverified":
                raise MigrationError(f"{t}: record_json status is not unverified")
            qt, qo = c["quote_total"] or 0, c["quote_ok"] or 0
            status, reasons = judge(1, 0, qt, qo, min_quote_rate=min_quote_rate)
            if status != "excluded" or not reasons or reasons[0] == "no_quotes":
                raise MigrationError(f"{t}: judge() does not exclude (quote {qo}/{qt})")
            m.set_case_verification(t, "unverified")
            m.record_failure(t, STAGE, CASE_ERROR_CLASS, _case_message(qo, qt, min_quote_rate) + f" ({MIGRATION_NOTE})", retry_count=0)
            m.resolve_failures(t, MANUAL_STAGE)
    return {"migrated": len(targets), "case_ids": targets, "undo_out": str(undo_out) if undo_out else None}

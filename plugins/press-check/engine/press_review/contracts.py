"""데이터 계약 검증: 자체 JSON Schema 검증기 + contracts/README.md 교차검증 1~7.

런타임 의존성이 없도록 이 프로젝트의 스키마가 쓰는 키워드만 구현한다. 모르는 키워드를 만나면
조용히 무시하지 않고 ``UnsupportedSchemaKeyword``를 던진다. 테스트(tests/engine)가 jsonschema
(Draft 2020-12)와 유효/무효 판정과 오류 위치가 같은지 대조한다.

지원 키워드: type, enum, const, required, properties, additionalProperties, items, minItems, maxItems,
minLength, maxLength, minimum, maximum, pattern, format(date, date-time, uri), allOf, if/then/else.
format은 문자열에만 적용한다. 그 밖의 format 이름은 주석으로 보고 통과시킨다.
"""
from __future__ import annotations

import datetime as _dt
import functools
import json
import re
from typing import Any, Iterable

from . import config, hashing, textspan


class UnsupportedSchemaKeyword(Exception):
    pass


# ---------------------------------------------------------------- schema 로딩

_SCHEMA_ALIASES = {
    "article": "article.schema.json",
    "review": "review.schema.json",
    "source-record": "source-record.schema.json",
    "case-record": "case-record.schema.json",
}


@functools.lru_cache(maxsize=None)
def load_schema(name: str) -> dict:
    """schemas/ 의 스키마를 읽는다. name: article | review | source-record | case-record (또는 파일명). 반환값은 수정하지 않는다."""
    fname = _SCHEMA_ALIASES.get(name, name)
    path = config.SCHEMAS_DIR / fname
    if path.parent != config.SCHEMAS_DIR or not path.is_file():
        raise FileNotFoundError(f"unknown schema: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 자체 검증기

_ANNOTATIONS = {"$schema", "$id", "$comment", "title", "description", "default", "examples"}
_SUPPORTED = {
    "type", "enum", "const", "required", "properties", "additionalProperties", "items", "minItems",
    "maxItems", "minLength", "maxLength", "minimum", "maximum", "pattern", "format", "allOf",
    "if", "then", "else",
}

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})", re.ASCII)
_DATETIME_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?([Zz]|[+-](\d{2}):(\d{2}))", re.ASCII
)
_URI_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*", re.ASCII)
_BAD_PCT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _valid_date(y: str, m: str, d: str) -> bool:
    try:
        _dt.date(int(y), int(m), int(d))
    except ValueError:
        return False
    return True


def is_date(s: str) -> bool:
    m = _DATE_RE.fullmatch(s)
    return bool(m) and _valid_date(*m.groups())


def parse_rfc3339(s: str) -> _dt.datetime | None:
    """RFC 3339 date-time -> aware datetime. 형식이 틀리면 None. 윤초(:60)는 59로 취급해 파싱만 한다."""
    m = _DATETIME_RE.fullmatch(s)
    if not m:
        return None
    y, mo, d, hh, mi, ss, tz, tzh, tzm = m.groups()
    if not _valid_date(y, mo, d) or int(hh) > 23 or int(mi) > 59 or int(ss) > 60:
        return None
    if tzh is not None and (int(tzh) > 23 or int(tzm) > 59):
        return None
    if tz in ("Z", "z"):
        offset = _dt.timezone.utc
    else:
        sign = 1 if tz[0] == "+" else -1
        offset = _dt.timezone(sign * _dt.timedelta(hours=int(tzh), minutes=int(tzm)))
    return _dt.datetime(int(y), int(mo), int(d), int(hh), int(mi), min(int(ss), 59), tzinfo=offset)


def is_datetime(s: str) -> bool:
    return parse_rfc3339(s) is not None


def is_uri(s: str) -> bool:
    return bool(_URI_RE.fullmatch(s)) and not _BAD_PCT_RE.search(s)


_FORMATS = {"date": is_date, "date-time": is_datetime, "uri": is_uri}


def _is_type(inst: Any, t: str) -> bool:
    if t == "string":
        return isinstance(inst, str)
    if t == "integer":
        return (isinstance(inst, int) and not isinstance(inst, bool)) or (isinstance(inst, float) and inst.is_integer())
    if t == "number":
        return isinstance(inst, (int, float)) and not isinstance(inst, bool)
    if t == "boolean":
        return isinstance(inst, bool)
    if t == "null":
        return inst is None
    if t == "object":
        return isinstance(inst, dict)
    if t == "array":
        return isinstance(inst, list)
    raise UnsupportedSchemaKeyword(f"type={t}")


def _jeq(a: Any, b: Any) -> bool:
    """JSON 동등성: bool은 숫자와 다르다, 1 == 1.0."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_jeq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_jeq(a[k], b[k]) for k in a)
    return False


def _ecma_pattern(pat: str) -> str:
    """ECMA-262 의미로 맞춘다: 플래그 m 없는 ``$`` 는 입력 끝에서만 맞는다(파이썬 ``$`` 는 끝 개행 앞에서도 맞는다).

    이스케이프된 ``\\$`` 와 문자 클래스 ``[...]`` 안의 ``$`` 는 그대로 둔다.
    """
    out: list[str] = []
    in_class = False
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "\\" and i + 1 < n:
            out.append(pat[i:i + 2])
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "$":
            out.append(r"\Z")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _validate(inst: Any, schema: dict, path: str, out: list[tuple[str, str]]) -> None:
    for kw in schema:
        if kw not in _SUPPORTED and kw not in _ANNOTATIONS:
            raise UnsupportedSchemaKeyword(kw)

    if "type" in schema:
        t = schema["type"]
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(inst, x) for x in types):
            out.append((path, f"type: expected {'|'.join(types)}"))
    if "enum" in schema and not any(_jeq(inst, v) for v in schema["enum"]):
        out.append((path, f"enum: {inst!r} not in {schema['enum']!r}"))
    if "const" in schema and not _jeq(inst, schema["const"]):
        out.append((path, f"const: expected {schema['const']!r}"))

    if isinstance(inst, str):
        if "minLength" in schema and len(inst) < schema["minLength"]:
            out.append((path, f"minLength: {len(inst)} < {schema['minLength']}"))
        if "maxLength" in schema and len(inst) > schema["maxLength"]:
            out.append((path, f"maxLength: {len(inst)} > {schema['maxLength']}"))
        if "pattern" in schema and re.search(_ecma_pattern(schema["pattern"]), inst) is None:
            out.append((path, f"pattern: does not match {schema['pattern']}"))
        fmt = schema.get("format")
        if fmt in _FORMATS and not _FORMATS[fmt](inst):
            out.append((path, f"format: not a valid {fmt}"))

    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in schema and inst < schema["minimum"]:
            out.append((path, f"minimum: {inst} < {schema['minimum']}"))
        if "maximum" in schema and inst > schema["maximum"]:
            out.append((path, f"maximum: {inst} > {schema['maximum']}"))

    if isinstance(inst, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in inst:
                out.append((path, f"required: missing '{name}'"))
        for name, sub in props.items():
            if name in inst:
                _validate(inst[name], sub, f"{path}.{name}", out)
        if "additionalProperties" in schema:
            ap = schema["additionalProperties"]
            extra = [k for k in inst if k not in props]
            if ap is False:
                if extra:
                    out.append((path, f"additionalProperties: unexpected {sorted(extra)}"))
            elif isinstance(ap, dict):
                for k in extra:
                    _validate(inst[k], ap, f"{path}.{k}", out)

    if isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]:
            out.append((path, f"minItems: {len(inst)} < {schema['minItems']}"))
        if "maxItems" in schema and len(inst) > schema["maxItems"]:
            out.append((path, f"maxItems: {len(inst)} > {schema['maxItems']}"))
        if "items" in schema:
            for i, it in enumerate(inst):
                _validate(it, schema["items"], f"{path}[{i}]", out)

    for sub in schema.get("allOf", []):
        _validate(inst, sub, path, out)
    if "if" in schema:
        probe: list[tuple[str, str]] = []
        _validate(inst, schema["if"], path, probe)
        branch = schema.get("then") if not probe else schema.get("else")
        if branch is not None:
            _validate(inst, branch, path, out)


def schema_error_items(instance: Any, schema: dict) -> list[tuple[str, str]]:
    """[(경로, 메시지)]. 경로는 ``$.findings[0].span.start`` 형식. required/additionalProperties는 객체 경로."""
    out: list[tuple[str, str]] = []
    _validate(instance, schema, "$", out)
    return out


def schema_errors(instance: Any, schema: dict) -> list[str]:
    return [f"{p}: {m}" for p, m in schema_error_items(instance, schema)]


# ---------------------------------------------------------------- article

MAX_ARTICLE_CP = 300_000  # 한 번에 받는 원고 총량 상한(code point). 색인·로그 필터의 메모리 상한을 겸한다.


def _walk_strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for x in obj:
            yield from _walk_strings(x)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_strings(v)


def validate_article(obj: Any) -> list[str]:
    """스키마 + title·body 각 1개 이상 + block_id 유일 + lone surrogate 없음 + 총량 상한."""
    errs = schema_errors(obj, load_schema("article"))
    if errs:
        return errs
    blocks = obj["blocks"]
    sections = [b["section"] for b in blocks]
    if "title" not in sections:
        errs.append("$.blocks: title block required")
    if "body" not in sections:
        errs.append("$.blocks: body block required")
    seen: set[str] = set()
    for i, b in enumerate(blocks):
        if b["block_id"] in seen:
            errs.append(f"$.blocks[{i}].block_id: duplicate '{b['block_id']}'")
        seen.add(b["block_id"])
    if any(textspan.has_lone_surrogate(s) for s in _walk_strings(obj)):
        errs.append("$: lone surrogate code point (invalid Unicode text)")
    if sum(len(b["text"]) for b in blocks) > MAX_ARTICLE_CP:
        errs.append(f"$.blocks: article longer than {MAX_ARTICLE_CP} code points")
    return errs


# ---------------------------------------------------------------- report 교차검증

_SENT_SPLIT = re.compile(r"(?<=[.!?。])\s+(?=\S)")
MAX_RETENTION_SECONDS = 24 * 3600  # 상한 점검용. 기본값은 config.DEFAULT_TTL_SECONDS


def is_single_sentence(s: str) -> bool:
    s = s.strip()
    return bool(s) and "\n" not in s and len(_SENT_SPLIT.split(s)) == 1


def _is_int(x: Any) -> bool:
    return type(x) is int


def _span_errors(span: dict, blocks: dict[str, str], where: str) -> list[str]:
    """교차검증 1. 반개구간 [start, end), code point 기준, 잘라낸 문자열 == quote(정규화 없음)."""
    errs: list[str] = []
    bid, s, e, q = span.get("block_id"), span.get("start"), span.get("end"), span.get("quote")
    if bid not in blocks:
        return [f"{where}: block_id '{bid}' not in article"]
    if not (_is_int(s) and _is_int(e)):
        return [f"{where}: start/end must be integers (code point offsets)"]
    text = blocks[bid]
    if not (0 <= s < e <= len(text)):
        return [f"{where}: span [{s},{e}) outside block '{bid}' (length {len(text)} code points)"]
    if text[s:e] != q:
        errs.append(f"{where}: quote does not equal block text [{s},{e}) (no normalization applied)")
    return errs


def _overlaps(edits: list[tuple[str, int, int, str]]) -> list[str]:
    errs: list[str] = []
    by_block: dict[str, list[tuple[int, int, str]]] = {}
    for bid, s, e, label in edits:
        by_block.setdefault(bid, []).append((s, e, label))
    for bid, items in by_block.items():
        items.sort()
        far_end, far_label = -1, ""
        for s, e, label in items:
            if s < far_end:
                errs.append(f"edit conflict in block '{bid}': {label} overlaps {far_label}")
            if e > far_end:
                far_end, far_label = e, label
    return errs


# ---- G-D22: 실색인 규모 기준. 핵심 자료군을 각각 이 건수 이상 담지 못한 색인으로는 'no_actionable_findings' 를 쓸 수 없다.
# 단위는 사건(case) 수다: 판결 한 건이 chunk 여러 개가 되므로 passage 수로 세면 기준이 무뎌진다. 사건 구조화가 없는 자료는 문서(source_id) 수로 센다.
# 판결(judgment)과, PAC 사건 자료(pac_settlement 또는 pac_decision 중 하나)가 각각 기준을 넘어야 한다(두 유형을 합산하지 않는다).
CORE_MIN_CASES = 100
CORE_GROUPS: tuple[tuple[str, ...], ...] = (("judgment",), ("pac_settlement", "pac_decision"))
DATE_UNKNOWN_MARK = "date_unknown"      # G-D23: 날짜 없는 근거를 쓴 보고서의 coverage_notes 에 반드시 들어가는 표지어


def core_coverage(status: Any) -> dict:
    """코퍼스 ``status()`` 의 ``cases_by_authority_type`` 로 색인 규모 기준을 판정한다. 키가 없는 옛 색인·합성 코퍼스는 미달이다."""
    st = status if isinstance(status, dict) else {}
    counts = st.get("cases_by_authority_type")
    counts = counts if isinstance(counts, dict) else {}
    groups = []
    for g in CORE_GROUPS:
        per = {a: int(counts.get(a, 0) or 0) for a in g}
        best = max(per.values())
        groups.append({"authority_types": list(g), "cases": per, "best": best, "met": best >= CORE_MIN_CASES})
    allowed = all(g["met"] for g in groups) and not st.get("is_fixture", False)
    if allowed:
        reason = ""
    elif st.get("is_fixture", False):
        reason = "합성(fixture) 코퍼스는 'no_actionable_findings' 를 쓸 수 없습니다."
    else:
        short = [f"{'/'.join(g['authority_types'])} {g['best']}건" for g in groups if not g["met"]]
        reason = (f"색인이 핵심 자료군을 각각 최소 {CORE_MIN_CASES}건(사건 수) 담지 못했습니다: " + ", ".join(short)
                  + ". 'no_actionable_findings' 대신 'partial' 을 쓰세요.")
    return {"min_cases": CORE_MIN_CASES, "unit": "case", "groups": groups, "no_actionable_findings_allowed": allowed, "reason": reason}


def _status_of(corpus: Any) -> dict:
    try:
        st = corpus.status()
    except Exception:
        return {}
    return st if isinstance(st, dict) else {}


def validate_report(report: Any, article: Any, corpus: Any = None, *, raw_bytes: bytes | None = None) -> list[str]:
    """스키마 + 교차검증 1~7. 오류 문자열 목록(빈 목록이면 통과).

    raw_bytes: 파일 입력이면 분석 당시 파일의 바이트(해시 모드 file_bytes). 없으면 객체 해시(cjson-1)와 비교한다.
    corpus: ``get_passage(source_id, passage_id)``, ``snapshot_id``, ``is_fixture``를 가진 객체. 근거가 없으면 None도 가능.
    """
    errs = schema_errors(report, load_schema("review"))
    if errs:
        return errs
    a_errs = validate_article(article)
    if a_errs:
        return [f"article: {m}" for m in a_errs]

    blocks = {b["block_id"]: b["text"] for b in article["blocks"]}
    mode = report["execution_mode"]
    findings = report["findings"]

    # 교차검증 2: 해시와 article_id
    expected, hmode = hashing.article_sha256(article, raw_bytes)
    if report["article_sha256"] != expected:
        errs.append(f"article_sha256 mismatch (expected {hmode} hash of the analyzed input)")
    if report["article_id"] != article["article_id"]:
        errs.append("article_id differs from the article")

    ids: set[str] = set()
    edits: list[tuple[str, int, int, str]] = []
    undated: list[str] = []
    for i, f in enumerate(findings):
        fid = f["finding_id"]
        where = f"findings[{i}]({fid})"
        if fid in ids:
            errs.append(f"{where}: duplicate finding_id")
        ids.add(fid)

        # 교차검증 1
        errs += _span_errors(f["span"], blocks, f"{where}.span")

        # 교차검증 3
        se = f["suggested_edit"]
        if se["mode"] == "replace":
            sp = f["span"]
            if _is_int(sp.get("start")) and _is_int(sp.get("end")):
                edits.append((sp["block_id"], sp["start"], sp["end"], f"{fid}.span"))
        elif se["related_edits"]:
            errs.append(f"{where}.suggested_edit: related_edits requires mode 'replace'")
        for j, r in enumerate(se["related_edits"]):
            rw = f"{where}.suggested_edit.related_edits[{j}]"
            span_errs = _span_errors(r["span"], blocks, f"{rw}.span")
            errs += span_errs
            if not span_errs:
                edits.append((r["span"]["block_id"], r["span"]["start"], r["span"]["end"], f"{fid}.related_edits[{j}]"))

        # 교차검증 4 + 근거 정책(D4)
        for j, ev in enumerate(f["evidence"]):
            ew = f"{where}.evidence[{j}]"
            if corpus is None:
                errs.append(f"{ew}: corpus required to check evidence")
                continue
            p = corpus.get_passage(ev["source_id"], ev["passage_id"])
            if p is None:
                errs.append(f"{ew}: source_id/passage_id not found in corpus")
                continue
            if ev["url"] != p["url"]:
                errs.append(f"{ew}: url differs from corpus record")
            if ev["locator"] != p["locator"]:
                errs.append(f"{ew}: locator differs from corpus record")
            if ev["authority_type"] != p["authority_type"]:
                errs.append(f"{ew}: authority_type differs from corpus record")
            if p.get("is_fixture") and mode != "synthetic_example":
                errs.append(f"{ew}: fixture passage cited in a '{mode}' report")
            dd = p.get("reference_date") or p.get("decision_date")       # G-D23: 결정일, 없으면 수록 간행물의 발행일
            if dd and dd > report["legal_as_of"]:
                errs.append(f"{ew}: {p.get('reference_date_basis') or 'decision_date'} {dd} is after legal_as_of {report['legal_as_of']}")
            if p.get("date_unknown") is True and not p.get("is_fixture"):
                undated.append(ev["passage_id"])
        if not f["evidence"]:
            if f["validation_status"] != "needs_information":
                errs.append(f"{where}: no evidence requires validation_status 'needs_information'")
            if f["review_confidence"] != "low":
                errs.append(f"{where}: no evidence requires review_confidence 'low'")
        if f["validation_status"] == "verified" and not f["evidence"]:
            errs.append(f"{where}: validation_status 'verified' requires evidence")
        if not is_single_sentence(f["reason_short"]):
            errs.append(f"{where}.reason_short: must be a single sentence")
        if f["detailed_explanation_ref"] != f"{report['report_id']}/{fid}":
            errs.append(f"{where}.detailed_explanation_ref: expected '{report['report_id']}/{fid}'")

    errs += _overlaps(edits)

    # corpus snapshot / fixture
    if corpus is not None and mode == "live":
        if getattr(corpus, "is_fixture", False):
            errs.append("execution_mode 'live' with a fixture corpus")
        if report["corpus_snapshot_id"] != corpus.snapshot_id:
            errs.append("corpus_snapshot_id differs from the corpus used")

    # 교차검증 6
    status = report["overall_status"]
    notes = report["coverage_notes"]
    if status == "findings" and not findings:
        errs.append("overall_status 'findings' with no findings")
    if status != "findings" and status != "no_actionable_findings" and not notes:
        errs.append(f"overall_status '{status}' requires coverage_notes")
    if status == "no_actionable_findings":
        if findings:
            errs.append("overall_status 'no_actionable_findings' with findings present")
        if not notes:
            errs.append("overall_status 'no_actionable_findings' requires coverage_notes (not a safety guarantee)")
        if corpus is None or getattr(corpus, "is_fixture", False) or _corpus_empty(corpus):
            errs.append("overall_status 'no_actionable_findings' needs a non-fixture, non-empty corpus; use 'partial' or 'insufficient_information'")
        elif not core_coverage(_status_of(corpus))["no_actionable_findings_allowed"]:
            errs.append("overall_status 'no_actionable_findings' needs an index with at least "
                        f"{CORE_MIN_CASES} cases in each core group (minimum corpus scale, G-D22): "
                        + (core_coverage(_status_of(corpus))["reason"] or "use 'partial'"))
    if undated and not any(isinstance(n, str) and DATE_UNKNOWN_MARK in n for n in notes):
        errs.append(f"evidence passages {sorted(set(undated))} have no decision or publication date; coverage_notes must say so ('{DATE_UNKNOWN_MARK}')")
    if report["hidden_finding_count"] != max(0, len(findings) - config.DISPLAY_TOP_N):
        errs.append(f"hidden_finding_count must equal total findings minus {config.DISPLAY_TOP_N} (min 0)")

    # 교차검증 7
    ret = report["retention"]
    if ret["mode"] != "ephemeral":
        errs.append(f"retention.mode '{ret['mode']}' is not supported (P0 keeps nothing on disk)")
    exp = ret["expires_at"]
    if exp is None:
        if mode == "live":
            errs.append("retention.expires_at must be set for a live ephemeral report")
    else:
        t_exp, t_an = parse_rfc3339(exp), parse_rfc3339(report["analyzed_at"])
        if t_exp is None or t_an is None:
            errs.append("retention.expires_at / analyzed_at not parseable")
        else:
            span = (t_exp - t_an).total_seconds()
            if span <= 0:
                errs.append("retention.expires_at must be after analyzed_at")
            elif span > MAX_RETENTION_SECONDS:
                errs.append(f"retention window exceeds {MAX_RETENTION_SECONDS} seconds")
    return errs


def _corpus_empty(corpus: Any) -> bool:
    try:
        return int(corpus.status().get("passage_count", 0)) == 0
    except Exception:
        return True


# ---------------------------------------------------------------- source record 교차검증 5

def validate_source_record(rec: Any) -> list[str]:
    """스키마 + 교차검증 5(권한 상태는 서로 독립이며 unknown/permission_required/forbidden은 허용이 아니다).

    - collection_enabled=true 는 robots.purpose_status=allowed, rights.read=allowed, access_status=available 이 모두 필요하다.
    - rights의 어떤 항목이든 'allowed'이면 rights_basis가 1건 이상 있어야 한다(근거·검토 기록). review_status가 'pending'이어도
      기록은 기록이다(설치자가 `rights set` 으로 남긴 결정: allowed + 근거 pending). 근거 없는 allowed만 거부한다.
    - raw_sha256이 있으면(원문 보관) rights.store_raw=allowed 이어야 한다.
    - extraction.pages_extracted 는 pages_expected 를 넘을 수 없다.
    """
    errs = schema_errors(rec, load_schema("source-record"))
    if errs:
        return errs
    rights, robots = rec["rights"], rec["robots"]
    if rec["collection_enabled"]:
        if robots["purpose_status"] != "allowed":
            errs.append(f"collection_enabled requires robots.purpose_status 'allowed' (is '{robots['purpose_status']}')")
        if rights["read"] != "allowed":
            errs.append(f"collection_enabled requires rights.read 'allowed' (is '{rights['read']}')")
        if rec["access_status"] != "available":
            errs.append(f"collection_enabled requires access_status 'available' (is '{rec['access_status']}')")
    allowed = [k for k, v in rights.items() if v == "allowed"]
    if allowed and not rec["rights_basis"]:
        errs.append(f"rights {sorted(allowed)} are 'allowed' without any rights_basis entry (basis and review record required)")
    if rec["raw_sha256"] is not None and rights["store_raw"] != "allowed":
        errs.append("raw_sha256 present but rights.store_raw is not 'allowed'")
    ex = rec["extraction"]
    if ex["pages_expected"] is not None and ex["pages_extracted"] is not None and ex["pages_extracted"] > ex["pages_expected"]:
        errs.append("extraction.pages_extracted exceeds pages_expected")
    return errs


# ---------------------------------------------------------------- case record 교차검증

_WS_RE = re.compile(r"\s+")
# ---- 라벨 계약의 정본(G-D2·G-D16). outcome_type 과 outcome_scope 의 짝 규칙은 이 표와 ``validate_case_record`` 한 곳에만 둔다.
# 프롬프트 문구와 수집 쪽 label_guard 는 이 규칙을 따라야 하며, 어긋나면 이쪽이 우선이다.
# authority_type -> 허용되는 outcome_type. unknown 은 항상 허용한다(판단 불가를 억지로 채우지 않는다).
#  - judgment: 법원 판결의 outcome_type 은 judgment 뿐이다. 상고기각·파기환송 같은 처분 원문은 decision_type 에 적는다(G-D2).
#  - dismissal_*·withdrawal: PAC·IENDC 절차(pac_settlement, pac_decision, pac_election_review, iendc_review)의 기각·각하·취하에만 쓴다(G-D16).
#  - guideline(G-D21): 심의기준·편람. 법령(statute)이 아니다.
_PROCEDURE_OUTCOMES = {"dismissal_procedural", "dismissal_merits", "withdrawal"}
AUTHORITY_OUTCOMES: dict[str, set[str]] = {
    "judgment": {"judgment", "unknown"},
    "statute": {"guideline", "unknown"},
    "guideline": {"guideline", "unknown"},
    "pac_settlement": {"settlement", "unknown"} | _PROCEDURE_OUTCOMES,
    "pac_decision": {"decision", "correction", "unknown"} | _PROCEDURE_OUTCOMES,
    "pac_election_review": {"decision", "correction", "guideline", "unknown"} | _PROCEDURE_OUTCOMES,
    "iendc_review": {"decision", "correction", "guideline", "unknown"} | _PROCEDURE_OUTCOMES,
    "research": {"research", "unknown"},
    "correction": {"correction", "unknown"},
}
_AUTHORITY_OUTCOMES = AUTHORITY_OUTCOMES   # 옛 이름
# outcome_scope 짝 규칙: dismissal_procedural 은 언제나 scope 'procedural'(절차 각하는 본안 판단이 아니다).
# scope 'procedural' 은 이 집합의 outcome_type 에만 붙고, 법원 판결(judgment)에는 붙지 않는다.
PROCEDURAL_SCOPE_OUTCOMES = {"dismissal_procedural", "withdrawal", "unknown"}
_COURT_SPEAKERS = {"court_majority", "court_concurring", "court_dissenting"}


def _collapse(s: str) -> str:
    return _WS_RE.sub(" ", s).strip()


def _quote_errors(q: Any, where: str, pages: Any, cited: list[str]) -> list[str]:
    """한 인용 객체: 페이지 존재, 0 <= start < end <= len(페이지), 잘라낸 구간 == quote(공백 축약만 정규화). offset은 원문 기준이다."""
    errs: list[str] = []
    if not isinstance(q, dict):
        return errs
    pid = q.get("page_id")
    cited.append(pid)
    st, en = q.get("start"), q.get("end")
    if _is_int(st) and _is_int(en) and en <= st:
        errs.append(f"{where}: end must be greater than start")
    if pages is None:
        return errs
    text = pages(pid) if callable(pages) else pages.get(pid)
    if text is None:
        return errs + [f"{where}: page_id '{pid}' is not a known page"]
    quote = q.get("quote")
    if not (_is_int(st) and _is_int(en)) or not (0 <= st < en <= len(text)):
        return errs + [f"{where}: span [{st}, {en}) is outside page '{pid}' (length {len(text)})"]
    if _collapse(text[st:en]) != _collapse(quote):
        errs.append(f"{where}: quote does not match page text at [{st}, {en}) (whitespace-collapsed comparison)")
    return errs


def _iter_quotes(rec: dict) -> Iterable[tuple[str, Any]]:
    for f in ("factual_background", "article_claims", "disputed_spans", "claims_or_requested_remedy", "legal_reasoning",
              "legal_provisions", "law_versions", "appeal_or_later_history"):
        for i, it in enumerate(rec.get(f, [])):
            if isinstance(it, dict) and it.get("quote") is not None:
                yield f"{f}[{i}].quote", it["quote"]
    if isinstance(rec.get("outcome"), dict):
        yield "outcome.quote", rec["outcome"].get("quote")
    for f in ("available_original_article", "available_corrected_article"):
        if isinstance(rec.get(f), dict) and rec[f].get("quote") is not None:
            yield f"{f}.quote", rec[f]["quote"]


def validate_case_record(rec: Any, pages: Any = None) -> list[str]:
    """스키마 + 교차검증. pages: {page_id: 페이지 텍스트} 또는 page_id -> 텍스트|None 함수(주면 인용을 원문과 대조한다).

    - 인용: 페이지 존재, 0 <= start < end <= 페이지 길이(code point), 구간 텍스트 == quote(공백 축약만 정규화).
    - supporting_passage_ids 는 알려진 페이지이고, 인용이 가리키는 모든 page_id 를 포함한다.
    - 라벨 분리(CLAUDE.md): 합의(settlement)·취하·각하(절차)는 서로 다른 값이고 judgment 류와 섞이지 않는다.
      authority_type 별 허용 outcome_type, 법원 의견 speaker 는 judgment 에서만, outcome 의 speaker 는 기관·다수의견만,
      claims 는 당사자만, article 필드는 article 만, 반대·별개의견은 outcome 이 될 수 없다.
    - available_*_article: quoted_in_source 일 때만 quote 가 있다. correction_pair_confidence=full_pair_verified 는 둘 다 quoted_in_source.
    - verification_status=verified 는 verified_by 가 있어야 하고, 아니면 verified_by 는 비어 있어야 한다. quote_check.matched <= total.
    """
    errs = schema_errors(rec, load_schema("case-record"))
    if errs:
        return errs
    a, o = rec["authority_type"], rec["outcome_type"]
    if o not in AUTHORITY_OUTCOMES[a]:
        errs.append(f"authority_type '{a}' cannot carry outcome_type '{o}'")
    sa = rec["settlement_or_adjudication"]
    if o == "settlement" and sa != "settlement":
        errs.append("outcome_type 'settlement' requires settlement_or_adjudication 'settlement'")
    if sa == "settlement" and o not in ("settlement", "unknown"):
        errs.append(f"settlement_or_adjudication 'settlement' cannot be recorded as outcome_type '{o}' (a settlement is not a ruling)")
    if o == "withdrawal" and sa != "withdrawal":
        errs.append("outcome_type 'withdrawal' requires settlement_or_adjudication 'withdrawal'")
    if sa == "withdrawal" and o not in ("withdrawal", "unknown"):
        errs.append(f"settlement_or_adjudication 'withdrawal' cannot be recorded as outcome_type '{o}'")
    scope = rec["outcome_scope"]
    if o == "dismissal_procedural" and scope != "procedural":
        errs.append("outcome_type 'dismissal_procedural' requires outcome_scope 'procedural' (a procedural dismissal is not a merits ruling)")
    if scope == "procedural" and o not in PROCEDURAL_SCOPE_OUTCOMES:
        errs.append(f"outcome_scope 'procedural' cannot be recorded with outcome_type '{o}'")
    if scope == "procedural" and a == "judgment":
        errs.append("outcome_scope 'procedural' cannot be recorded for authority_type 'judgment' (a court's disposition is written in decision_type)")

    def _speakers(field: str, items: list, allowed: set[str]) -> None:
        for i, it in enumerate(items):
            if it["speaker"] not in allowed:
                errs.append(f"{field}[{i}].speaker '{it['speaker']}' not allowed here (allowed: {sorted(allowed)})")
            if it["speaker"] in _COURT_SPEAKERS and a != "judgment":
                errs.append(f"{field}[{i}].speaker '{it['speaker']}' requires authority_type 'judgment'")

    _speakers("article_claims", rec["article_claims"], {"article"})
    _speakers("disputed_spans", rec["disputed_spans"], {"article"})
    _speakers("claims_or_requested_remedy", rec["claims_or_requested_remedy"], {"claimant", "respondent"})
    _speakers("legal_reasoning", rec["legal_reasoning"], {"institution", *_COURT_SPEAKERS})
    _speakers("factual_background", rec["factual_background"], {"claimant", "respondent", "institution", "record", *_COURT_SPEAKERS})
    if rec["outcome"] is not None:
        _speakers("outcome", [rec["outcome"]], {"institution", "court_majority"})
    for f in ("available_original_article", "available_corrected_article"):
        st, q = rec[f]["status"], rec[f]["quote"]
        if (st == "quoted_in_source") != (q is not None):
            errs.append(f"{f}: quote is required exactly when status is 'quoted_in_source'")
    pair = rec["correction_pair_confidence"]
    both = all(rec[f]["status"] == "quoted_in_source" for f in ("available_original_article", "available_corrected_article"))
    if pair == "full_pair_verified" and not both:
        errs.append("correction_pair_confidence 'full_pair_verified' requires both articles quoted_in_source")
    if pair == "before_missing" and rec["available_original_article"]["status"] == "quoted_in_source":
        errs.append("correction_pair_confidence 'before_missing' contradicts a quoted original article")
    if (rec["verification_status"] == "verified") != bool(rec["verified_by"]):
        errs.append("verification_status 'verified' requires verified_by, and verified_by requires 'verified'")
    qc = rec["quote_check"]
    if qc["matched"] > qc["total"]:
        errs.append("quote_check.matched exceeds total")
    for i, lv in enumerate(rec["law_versions"]):
        if lv["effective_from"] and lv["effective_to"] and lv["effective_from"] > lv["effective_to"]:
            errs.append(f"law_versions[{i}]: effective_from is after effective_to")

    cited: list[str] = []
    for where, q in _iter_quotes(rec):
        errs += _quote_errors(q, where, pages, cited)
    sup = set(rec["supporting_passage_ids"])
    missing = sorted({c for c in cited if c not in sup})
    if missing:
        errs.append(f"supporting_passage_ids does not contain cited page_ids {missing}")
    if pages is not None:
        for pid in sorted(sup):
            if (pages(pid) if callable(pages) else pages.get(pid)) is None:
                errs.append(f"supporting_passage_ids: '{pid}' is not a known page")
    return errs

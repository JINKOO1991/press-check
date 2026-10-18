"""DeepSeek 사건 구조화 단계. 계약: docs/P2_DESIGN.md §4·§5.

입력은 추출 단계가 만든 ``pages`` 행과 텍스트 파일이다. 첨부(없으면 문서) 하나가 한 대상(target)이다.

흐름(대상마다)
1. 소스 프로필 선택(``structure_records.SOURCE_PROFILE``). 프로필이 없는 소스(연구 자료 등)는 구조화하지 않고 건수로 보고한다.
2. 반출 게이트: rights.external_api == allowed(문서 행과 소스 행 중 있는 것 모두)이고 source_family 가 코드 상수 허용 목록에 있어야 한다.
   검사는 대상을 시작할 때와 창을 보내기 직전에 한다. 통과하지 못하면 아무것도 보내지 않고 실패 장부에 남긴다.
3. 쪽 텍스트를 ``<<PAGE page_id>>`` 표지로 이어 붙인 스트림을 만들고 ``_split_windows`` 로 창을 나눈다(모든 창을 처리, 샘플링 없음).
4. 창마다 ``structure_jobs`` 한 건(키 = 대상·창·입력 해시·요청 쪽 버전 = ``structurer_version()`` 반환값; 조립 규칙 버전 ``ASSEMBLY_VERSION`` 은 키에 넣지 않는다). done 이고 응답 파일이 있으면 API를 부르지 않는다.
   저장하는 ``structurer_version`` 은 ``<요청 쪽 버전>|asm=<ASSEMBLY_VERSION>`` 이라서 조립 규칙만 바뀌면 응답을 다시 보내지 않고 다시 조립한다.
   응답 JSON 은 ``<corpus>/structured/<대상>/<창>.json`` 에 저장한다(재조립에 쓴다). 응답이 잘려 재시도가 소진되면 창을 반으로 나눠 다시 보낸다.
5. 모든 응답에서 case record 를 코드가 새로 조립·검증한다(``structure_records``). 대상의 cases/chunks 는 한 트랜잭션에서 통째로 교체된다
   (조립은 결정적이라 재실행해도 같은 결과이다). 단, 이미 structured/verified/indexed 인 대상을 새 버전으로 다시 돌리다 모든 창이 끝나지
   못하면(예산·시간·dead-letter) 옛 사건을 그대로 두고 받은 응답만 저장한다(``held_back``).
6. 모든 창이 끝났으면 첨부 상태를 structured 로, 입력 해시·단계 버전을 장부에 기록한다. 죽은 창(dead-letter)이 있으면 failed 로 둔다.

구조화 결과의 chunk 는 모델이 쓴 요약이 아니라 사건 인용이 걸친 쪽 구간의 원문 그대로다.

게이트 후 결정(P2_DESIGN §9): G-D5 표 프로필은 병합 칸 인용을 창 안에서 버린다. G-D6 ``CHUNK_ONLY_SOURCES``(심의기준·위원회 규정)는 제공자를 부르지 않고
쪽 chunk 만 만든다(``_chunk_only``), 연구 판결 분석은 소개된 판결의 사건번호를 법제처 장부와 결정적으로 연결한다(``links.py``).
G-D7 검증 완료(verified/indexed, 또는 검증된 사건이 있는) 대상을 다시 구조화하려면 ``force`` 가 있어야 하고, 강제하면 상태를 structured 로 되돌린다.
스레드는 HTTP 호출만 하고 ``Manifest`` 쓰기는 모두 메인 스레드에서 한다(``Manifest`` 는 스레드 안전하지 않다).

이식 메모: ``_split_windows``/``_coverage`` 는 작성자 소유 코드(비공개)에서 가져왔다.
창 크기·스냅 거리를 인자로 바꾸고 원문(정규화 전) 텍스트에 직접 적용한다. ``split_windows`` 래퍼가 쪽 표지 경계와 ".\\n" 스냅을 앞서 시도하는 것은 추가한 변경이다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import private_mkdir
from .. import diskguard, logsafe
from ..providers import deepseek as ds
from . import links as links_mod
from . import rights as rights_mod
from . import structure_records as sr
from .context import Budget, TimeBudgetExceeded, corpus_dir as default_corpus_dir
from .manifest import Manifest, sanitize_message

CODE_VERSION = "s3"                # 3: 인용 대조를 공백 제거본으로(G-D13), 표 칸 인용(G-D15), 짝 신뢰도 유도(G-D17), invalid_json 분할·깊이 상한(G-D18). 올리면 작업 키가 바뀌어 창을 다시 보낸다(요청 쪽 변경 전용, 조립 규칙은 ASSEMBLY_VERSION).
# 조립 규칙 버전: ``structure_records`` 의 결정적 조립·검증 규칙(인용 대조, 사건번호·조문 확인, 라벨 가드 등)이 바뀌면 올린다. 저장하는 structurer_version 에는 들어가
# 이미 받은 응답을 API 호출 없이 다시 조립하게 하지만(건너뛰기 검사가 버전 차이를 본다), 작업 키(job_id)에는 들어가지 않는다. 요청(프롬프트·모델·창)이 바뀌면 CODE_VERSION 또는
# 프롬프트 파일 version 을 올린다. a1 = s3 시기 규칙(G-D13·15·17), a2 = V-FID-01~07 수정(재게이트 뒤 검수),
# a3 = 적대 검증 수정(목록·객체 speaker 버림 SR-01, 이어짐 병합이 사건번호·번호 기반 case_id 를 유지 SR-02, 인용 없는 항목의 병합 중복 판정에 날짜·관계 포함 SR-03),
# a4 = G-D27(대조에서 <br> 를 공백처럼 지움)과 G-D26(표 소스 사건은 행 고유 칸 인용이 1개 이상, 없으면 격리),
# a5 = Codex 1회차(CX1-1·6): 계약(cases 배열·객체 항목)을 어긴 응답은 dead-letter(기존 사건 보존), 전문 확인 정보 없이는 full_pair_verified 를 partial_quote 로 제한.
ASSEMBLY_VERSION = "a5"
DEFAULT_MODEL = "deepseek-flash"
# 코드 상수: 이 source_family 만 DeepSeek 로 반출한다(권리 장부의 external_api 와 별개로 한 번 더 막는다).
EXTERNAL_API_FAMILIES = ("pac_cases", "pac_election", "pac_other", "iendc", "law_go_kr")
WINDOW_CHARS = 20000
SNAP_CHARS = 600
MAX_TOKENS = 16384
MIN_SPLIT_CHARS = 2000
MAX_SPLIT_DEPTH = 2                # G-D18: 같은 창을 반으로 나눠 다시 요청하는 최대 단계 수. 그래도 실패하면 dead-letter
SPLIT_ERROR_CLASSES = ("truncated_output", "invalid_json")
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
STRUCTURED_DIR = "structured"
PAGE_MARK = "<<PAGE {pid}>>\n"
_MARK_RE = re.compile(r"<<PAGE (.+?)>>\n")
log = logging.getLogger("press_review.ingestion.structure")


# ---------------------------------------------------------------- 이식
def _split_windows(text: str, window_chars: int, snap_chars: int) -> list:
    """이식: 작성자 소유 코드(비공개). 반환 [(시작, 끝, 텍스트)] — 겹침 없는 창.
    끝은 창 끝 근처의 문장 경계(「. 」)로 당긴다. 원본과 달리 창 크기·스냅 거리를 인자로 받고, 호출자가 정규화 전 원문에 적용한다."""
    out, pos, n = [], 0, len(text)
    while pos < n:
        end = min(n, pos + window_chars)
        if end < n:
            i = text.rfind(". ", max(pos + 1, end - snap_chars), end)
            if i > pos:
                end = i + 2
        out.append((pos, end, text[pos:end]))
        pos = end
    return out


def _coverage(source_chars: int, read_chars: int) -> float:
    """이식: 작성자 소유 코드(비공개)."""
    if not source_chars:
        return 0.0
    return round(min(1.0, read_chars / source_chars), 4)


def split_windows(text: str, window_chars: int = WINDOW_CHARS, snap_chars: int = SNAP_CHARS) -> list:
    """``_split_windows`` + 추가 스냅(변경): 창 끝 근처에 쪽 표지 경계가 있으면 거기서, 없으면 ".\\n" 에서 끝낸다. 둘 다 없으면 원본 규칙(「. 」)."""
    out, pos, n = [], 0, len(text)
    while pos < n:
        end = min(n, pos + window_chars)
        if end < n:
            lo = max(pos + 1, end - snap_chars)
            i = text.rfind("\n<<PAGE ", lo, end)
            if i > pos:
                end = i + 1
            else:
                i = text.rfind(".\n", lo, end)
                if i > pos:
                    end = i + 2
                else:
                    i = text.rfind(". ", lo, end)
                    if i > pos:
                        end = i + 2
        out.append((pos, end, text[pos:end]))
        pos = end
    return out


# ---------------------------------------------------------------- 프롬프트
@dataclass(frozen=True)
class PromptSet:
    system: str
    version: str        # 프로필 파일 version 과 공통 파일 version
    sha8: str           # 시스템 프롬프트 전문의 해시 앞 8자


def _read_prompt(name: str) -> tuple[str, str]:
    raw = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    first, _, rest = raw.partition("\n")
    m = re.fullmatch(r"version:\s*(\S+)", first.strip())
    if not m:
        raise ValueError(f"prompt file {name} must start with 'version: <string>'")
    return m.group(1), rest.strip()


def load_prompt(profile: sr.Profile) -> PromptSet:
    cv, common = _read_prompt("common.md")
    pv, body = _read_prompt(profile.prompt)
    system = common + "\n\n" + body + "\n"
    return PromptSet(system=system, version=f"common@{cv}+{profile.name}@{pv}", sha8=hashlib.sha256(system.encode("utf-8")).hexdigest()[:8])


def structurer_version(profile: sr.Profile, prompts: PromptSet, model: str, thinking: str, window_chars: int, snap_chars: int) -> str:
    """작업 키에 들어가는 단계 버전: 코드 버전 | 프롬프트 파일 버전·해시 | 모델 | thinking | 창 파라미터."""
    return f"{CODE_VERSION}|{prompts.version}.{prompts.sha8}|{model}|think={thinking}|w{window_chars}s{snap_chars}"


# ---------------------------------------------------------------- 반출 게이트
def egress_reason(manifest: Manifest, doc_id: str) -> Optional[str]:
    """DeepSeek 로 보내도 되면 None, 아니면 거부 사유. external_api 는 문서 행과 소스 행 가운데 있는 행 모두에서 정확히 'allowed' 여야 한다(unknown 은 거부)."""
    d = manifest.get_document(doc_id)
    if not d:
        return "document_unknown"
    src = manifest.get_source(d["source_id"])
    if not src:
        return "source_unknown"
    if src["source_family"] not in EXTERNAL_API_FAMILIES:
        return f"family_not_allowed:{src['source_family']}"
    if not src.get("enabled"):
        return "source_disabled"
    own = manifest.get_rights("document", doc_id)
    srow = manifest.get_rights("source", d["source_id"])
    if not own and not srow:
        return "no_rights_record"
    for row in (own, srow):          # 있는 행이 모두 allowed 여야 한다(문서 재정의가 있어도, 나중에 조여진 소스 행을 넘지 못한다)
        if row and row.get("external_api") != "allowed":
            return f"external_api_{row.get('external_api')}"
    return None


# ---------------------------------------------------------------- 장부 값(판례)
def ledger_values(manifest: Manifest, doc_id: str) -> dict:
    """판례 문서 자신의 사건번호·선고일(법제처 응답에서 결정적으로 얻은 값). 없는 항목은 빠진다.
    case_number: 응답에 인쇄된 표기(``body.body_case_no``, 없으면 목록의 ``case_no``). tokens: 낱개 사건번호(``body.case_nos``)."""
    d = manifest.get_document(doc_id) or {}
    try:
        extra = json.loads(d["extra_json"]) if d.get("extra_json") else {}
    except ValueError:
        extra = {}
    body = extra.get("body") or {}
    out: dict = {}
    printed = body.get("body_case_no") or extra.get("case_no")
    if isinstance(printed, str) and printed.strip():
        out["case_number"] = printed.strip()
        out["case_number_tokens"] = [x for x in (body.get("case_nos") or []) if isinstance(x, str)]
    if d.get("decision_date"):
        out["decision_date"] = d["decision_date"]
    return out


# ---------------------------------------------------------------- 대상
@dataclass
class Target:
    key: str                         # att_id 또는 doc_id
    doc_id: str
    att_id: Optional[str]
    source_id: str
    title: str
    label: str
    status: str
    stored_hash: Optional[str] = None
    stored_version: Optional[str] = None
    profile: Optional[str] = None


def _structure_candidate_doc(d: dict) -> bool:
    """판례 문서(lawgo_*)는 lawapi.is_structure_candidate 를 통과해야 구조화한다. 다른 소스는 제한하지 않는다."""
    if not str(d.get("source_id") or "").startswith("lawgo_"):
        return True
    from .sources.lawapi import is_structure_candidate
    extra = d.get("extra_json")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra or "{}")
        except ValueError:
            extra = {}
    return is_structure_candidate(extra if isinstance(extra, dict) else (d.get("extra") or {}))


def find_targets(manifest: Manifest, *, source_ids: Optional[list] = None, att_ids: Optional[list] = None,
                 doc_ids: Optional[list] = None,
                 statuses: tuple = ("extracted", "structured", "verified", "indexed", "failed"),
                 profile: Optional[str] = None, limit: Optional[int] = None) -> list:
    """구조화할 대상: 쪽 행이 있는 첨부(같은 해시 중복 ``dedup_of`` 는 제외)와, 첨부 없이 쪽 행만 있는 문서."""
    out: list[Target] = []
    seen_att: set = set()
    srcs = source_ids or [None]
    for src in srcs:
        for st in statuses:
            for a in manifest.list_attachments(source_id=src, status=st):
                if a.get("dedup_of") or a["att_id"] in seen_att:
                    continue
                if att_ids and a["att_id"] not in att_ids:
                    continue
                if doc_ids and a["doc_id"] not in doc_ids:
                    continue
                if not manifest.pages_of(att_id=a["att_id"]):
                    continue
                seen_att.add(a["att_id"])
                d = manifest.get_document(a["doc_id"]) or {}
                out.append(Target(key=a["att_id"], doc_id=a["doc_id"], att_id=a["att_id"], source_id=a["source_id"], title=d.get("title") or "",
                                  label=a.get("label") or "", status=a["status"], stored_hash=a.get("structure_input_sha256"),
                                  stored_version=a.get("structurer_version"), profile=profile or sr.SOURCE_PROFILE.get(a["source_id"])))
    orphan = manifest.select("SELECT DISTINCT p.doc_id AS doc_id FROM pages p WHERE p.att_id IS NULL AND NOT EXISTS "
                             "(SELECT 1 FROM attachments a WHERE a.doc_id=p.doc_id) ORDER BY p.doc_id")
    for r in orphan:
        d = manifest.get_document(r["doc_id"])
        if not d or d["status"] not in statuses or (source_ids and d["source_id"] not in source_ids):
            continue
        if (att_ids) or (doc_ids and d["doc_id"] not in doc_ids):
            continue
        if not doc_ids and not _structure_candidate_doc(d):
            continue                                        # G-D31·§8: 판례는 P-T1 또는 사전 필터 통과분만(--doc 명시 지정은 예외)
        out.append(Target(key=d["doc_id"], doc_id=d["doc_id"], att_id=None, source_id=d["source_id"], title=d.get("title") or "", label="",
                          status=d["status"], stored_hash=d.get("structure_input_sha256"), stored_version=d.get("structurer_version"),
                          profile=profile or sr.SOURCE_PROFILE.get(d["source_id"])))
    out.sort(key=lambda t: (t.source_id, t.doc_id, t.key))
    return out[:limit] if limit else out


class StructureError(Exception):
    def __init__(self, error_class: str, message: str = ""):
        super().__init__(message or error_class)
        self.error_class = error_class


@dataclass
class Loaded:
    rows: list
    texts: dict
    empty: list


def load_pages(manifest: Manifest, t: Target, corpus_dir: Path) -> Loaded:
    rows = manifest.pages_of(att_id=t.att_id) if t.att_id else [p for p in manifest.pages_of(doc_id=t.doc_id) if p["att_id"] is None]
    texts: dict[str, str] = {}
    empty: list[str] = []
    for r in rows:
        tp = r.get("text_path")
        if not tp:
            raise StructureError("page_text_missing", f"page {r['page_id']} has no text_path")
        p = Path(tp)
        p = p if p.is_absolute() else corpus_dir / p
        try:
            inside = p.resolve().is_relative_to(corpus_dir.resolve())
        except OSError:
            inside = False
        if not inside:     # 코퍼스 밖의 파일은 읽지도 보내지도 않는다(임의 로컬 경로를 따르지 않는다)
            raise StructureError("page_text_outside_corpus", f"page {r['page_id']} text path is outside the corpus directory")
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError:
            raise StructureError("page_text_missing", f"page {r['page_id']} text file cannot be read") from None
        if r.get("text_sha256") and hashlib.sha256(txt.encode("utf-8")).hexdigest() != r["text_sha256"]:
            raise StructureError("page_text_changed", f"page {r['page_id']} text does not match its recorded hash")
        if not txt.strip():
            empty.append(r["page_id"])
            continue
        texts[r["page_id"]] = txt
    return Loaded(rows=rows, texts=texts, empty=empty)


# ---------------------------------------------------------------- 창
@dataclass
class WindowSpec:
    unit: str
    start: int
    end: int
    text: str                         # 모델에 보내는 창 본문(필요하면 쪽 표지 접두 포함)
    page_ids: list
    input_sha: str
    messages: list = field(default_factory=list)
    job_id: str = ""


def build_stream(rows: list, texts: dict) -> tuple[str, list]:
    """쪽 텍스트를 표지와 함께 이어 붙인다. spans: [(page_id, 본문 시작, 본문 끝)] (스트림 좌표)."""
    parts, spans, pos = [], [], 0
    for r in rows:
        pid = r["page_id"]
        if pid not in texts:
            continue
        mark = PAGE_MARK.format(pid=pid)
        parts.append(mark)
        pos += len(mark)
        spans.append((pid, pos, pos + len(texts[pid])))
        parts.append(texts[pid])
        parts.append("\n")
        pos += len(texts[pid]) + 1
    return "".join(parts), spans


def make_spec(unit: str, stream: str, spans: list, s: int, e: int) -> WindowSpec:
    body = stream[s:e]
    page_ids = [pid for pid, a, b in spans if a < e and b > s]
    if not body.startswith("<<PAGE ") and page_ids:
        first = next(pid for pid, a, b in spans if a < e and b > s)
        body = PAGE_MARK.format(pid=first) + body
    return WindowSpec(unit=unit, start=s, end=e, text=body, page_ids=page_ids,
                      input_sha=hashlib.sha256(body.encode("utf-8")).hexdigest())


def build_messages(prompts: PromptSet, t: Target, spec: WindowSpec) -> list:
    user = (f"[DOCUMENT START]\nTitle: {t.title}\nAttachment: {t.label}\nPages in this window: {', '.join(spec.page_ids)}\n\n"
            f"{spec.text}\n[DOCUMENT END]\n\nReturn the json object described in the system message for the cases visible in this window.")
    return [{"role": "system", "content": prompts.system}, {"role": "user", "content": user}]


def job_id_for(target_key: str, unit: str, input_sha: str, version: str) -> str:
    return "sj-" + hashlib.sha1(f"{target_key}|{unit}|{input_sha}|{version}".encode("utf-8")).hexdigest()[:24]


def target_dir(corpus_dir: Path, key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:80] + "-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    return corpus_dir / STRUCTURED_DIR / safe


def _write_json(path: Path, obj: Any) -> None:
    private_mkdir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def response_path(tdir: Path, unit: str, job_id: str) -> Path:
    """창 응답(또는 분할 표지) 파일. 작업(job_id)마다 따로 둔다: 모델·단계 버전이 다른 실행이 서로의 응답을 덮어쓰지 않는다."""
    return tdir / f"{unit}@{job_id}.json"


def response_problem(resp: Any) -> Optional[str]:
    """CX1-1: 구조화 응답이 계약(``cases`` 배열, 항목은 객체)을 지키는지. 어기면 사유 문자열, 지키면 None. 빈 배열은 유효하다(사건 없음)."""
    if not isinstance(resp, dict):
        return "response_not_object"
    cases = resp.get("cases")
    if not isinstance(cases, list):
        return "response_has_no_cases_array"
    for i, d in enumerate(cases):
        if not isinstance(d, dict):
            return f"case_item_{i}_not_object"
    return None


def read_job_response(tdir: Path, job: dict, spec: "WindowSpec") -> Optional[dict]:
    """이 작업(spec.job_id)이 쓴 응답 파일만 돌려준다. 작업 행의 output_path 와 예전 이름(<unit>.json)을 차례로 보고,
    파일 안의 job_id 가 같은 것만 받는다(다른 작업의 파일은 입력 해시가 같아도 쓰지 않는다)."""
    cands = []
    if job.get("output_path"):
        cands.append(Path(job["output_path"]))
    cands += [response_path(tdir, spec.unit, spec.job_id), tdir / f"{spec.unit}.json"]
    seen = set()
    for c in cands:
        if c in seen:
            continue
        seen.add(c)
        sc = _read_json(c)
        if sc and sc.get("job_id") == spec.job_id:
            return sc
    return None


def _read_json(path: Path) -> Optional[dict]:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- 실행기
class Structurer:
    def __init__(self, manifest: Manifest, provider: ds.DeepSeekProvider, corpus_dir: Path, *, model: str = DEFAULT_MODEL,
                 window_chars: int = WINDOW_CHARS, snap_chars: int = SNAP_CHARS, max_tokens: int = MAX_TOKENS,
                 quarantine_rate: float = sr.DEFAULT_QUARANTINE_RATE, retry_dead: bool = False, force: bool = False,
                 execute: bool = True, page_chunks: bool = False, check_time: Optional[Callable[[], None]] = None,
                 disk_guard: Any = None):
        model = ds.canonical_model(model)           # 구 이름은 공급자 정식 이름으로 옮긴다(G-D11). 목록 밖이면 ModelNotAllowed
        self.m, self.p, self.corpus = manifest, provider, Path(corpus_dir)
        self.model, self.window_chars, self.snap_chars, self.max_tokens = model, window_chars, snap_chars, max_tokens
        self.rate, self.retry_dead, self.force, self.execute, self.page_chunks = quarantine_rate, retry_dead, force, execute, page_chunks
        self.check_time = check_time or (lambda: None)
        self.disk_guard = disk_guard                 # diskguard.DiskGuard 또는 None. 대상마다 tick, 부족하면 이어받기 가능한 상태로 정지
        self.disk_error: Optional[diskguard.DiskGuardError] = None
        self._model_ok = False
        self.fatal: Optional[ds.DeepSeekError] = None
        self.paused = False
        self.stats: dict = {"targets": 0, "processed": 0, "held_back": 0, "skipped_unchanged": 0, "skipped_no_profile": 0, "refused": 0, "refused_verified": 0, "chunk_only": 0, "failed": 0,
                            "windows": 0, "windows_cached": 0, "windows_called": 0, "windows_split": 0, "windows_dead": 0,
                            "windows_budget_blocked": 0, "windows_unfinished": 0, "cases": 0, "cases_quarantined": 0, "chunks": 0,
                            "dropped_items": 0, "api_attempts": 0, "cost_usd": 0.0, "est_pending_usd": 0.0, "read_chars": 0,
                            "source_chars": 0}

    # ---- 공개 진입점 ----
    def run(self, targets: list) -> dict:
        if self.disk_guard is not None:              # 대상 목록(limit 적용 후)이 확정됐으므로 전체 예상량 조건을 건다
            try:
                self.disk_guard.start(len(targets))
            except diskguard.DiskGuardError as e:
                self.disk_error, targets = e, []
        for t in targets:
            self.stats["targets"] += 1
            if self.fatal or self.paused:
                break
            if self.disk_guard is not None:
                try:
                    self.disk_guard.tick()
                except diskguard.DiskGuardError as e:
                    self.disk_error = e
                    break
            try:
                self._target(t)
            except StructureError as e:
                self.stats["failed"] += 1
                self.m.record_failure(t.key, "structure", e.error_class, str(e))
                log.warning("structure target %s failed: %s", t.key, e.error_class)
            except Exception as e:      # noqa: BLE001 - 한 대상의 예기치 않은 오류가 뒤의 대상을 막지 않게 장부에 남기고 이어간다
                self.stats["failed"] += 1
                self.m.record_failure(t.key, "structure", "unexpected_error", f"{type(e).__name__}: {sanitize_message(str(e))}")
                log.warning("structure target %s failed unexpectedly: %s", t.key, type(e).__name__)
            if self.disk_guard is not None:
                self.disk_guard.advance()                 # 대상 1건 처리 끝(성공·실패 공통)
        s = dict(self.stats)
        s["cost_usd"] = round(s["cost_usd"], 8)
        s["est_pending_usd"] = round(s["est_pending_usd"], 8)
        s["coverage"] = _coverage(s["source_chars"], s["read_chars"])
        s["status"] = ("aborted_" + self.fatal.error_class) if self.fatal else ("paused_time_limit" if self.paused else "completed")
        if self.disk_error is not None and not self.fatal:
            s["status"] = "paused_disk_" + self.disk_error.kind
        s["budget_remaining_usd"] = self.p.budget.remaining()
        return s

    # ---- 대상 하나 ----
    def _target(self, t: Target) -> None:
        if t.att_id and t.source_id in sr.LIST_CHUNK_AUTHORITY and sr.LIST_LABEL_RE.search(t.label or ""):
            self._chunk_only(t, sr.LIST_CHUNK_AUTHORITY[t.source_id])      # G-D32: 전체 목록·색인 장은 사건 구조화 대신 쪽 chunk
            return
        if not t.profile and t.source_id in sr.CHUNK_ONLY_SOURCES:
            self._chunk_only(t, sr.CHUNK_ONLY_SOURCES[t.source_id])
            return
        if not t.profile or t.profile not in sr.PROFILES:
            self.stats["skipped_no_profile"] += 1
            return
        profile = sr.PROFILES[t.profile]
        reason = egress_reason(self.m, t.doc_id)
        if reason:
            self.stats["refused"] += 1
            self.m.record_failure(t.key, "structure", "egress_refused", reason)
            log.warning("structure target %s refused: %s", t.key, reason)
            return
        loaded = load_pages(self.m, t, self.corpus)
        prompts = load_prompt(profile)
        rversion = structurer_version(profile, prompts, self.model, self.p.thinking, self.window_chars, self.snap_chars)     # 요청 쪽 버전(작업 키)
        version = f"{rversion}|asm={ASSEMBLY_VERSION}"                                                                       # 저장하는 버전(조립 규칙 포함)
        page_hashes = [(r["page_id"], r.get("text_sha256") or hashlib.sha256(loaded.texts.get(r["page_id"], "").encode("utf-8")).hexdigest())
                       for r in loaded.rows]
        target_hash = hashlib.sha256(json.dumps(page_hashes, sort_keys=True).encode("utf-8")).hexdigest()
        if (not self.force and t.status in ("structured", "verified", "indexed") and t.stored_hash == target_hash
                and t.stored_version == version):
            self.stats["skipped_unchanged"] += 1
            return
        if self._refuse_verified(t):
            return
        if not loaded.texts:
            raise StructureError("no_page_text", "all pages are empty")
        stream, spans = build_stream(loaded.rows, loaded.texts)
        roots = [make_spec(f"w{i:04d}", stream, spans, s, e) for i, (s, e, _t) in enumerate(split_windows(stream, self.window_chars, self.snap_chars))]
        self.stats["source_chars"] += len(stream)
        tdir = target_dir(self.corpus, t.key)
        leaves, pending, dead = self._plan(t, prompts, rversion, roots, tdir, stream, spans)
        unfinished = list(pending)
        if not self.execute:                       # --plan-only: 호출도 파일 쓰기도 하지 않는다
            self.stats["windows_unfinished"] += len(pending)
            return
        if pending:
            l2, d2, unfinished = self._execute(t, prompts, rversion, pending, tdir, stream, spans)
            leaves += l2
            dead += d2
        self.stats["windows_dead"] += len(dead)
        self.stats["windows_unfinished"] += len(unfinished)
        # 조립은 있는 응답만으로 한다(부분 진행 중인 대상도 일관된 상태가 된다). 상태 전이는 모든 창이 끝났을 때만 한다.
        complete = not (dead or unfinished) and not self.fatal and not self.paused and bool(leaves)
        if not complete and t.status in ("structured", "verified", "indexed"):
            # 이미 구조화된 대상을 새 버전으로 다시 돌리다 중단됐다(예산·시간·실패). 옛 사건 전체를 부분 결과로 바꾸지 않는다.
            # 받은 응답은 structured/ 아래에 남아 있어 다음 실행이 이어받는다.
            self.stats["held_back"] += 1
            self.stats["read_chars"] += sum(sp.end - sp.start for sp, _sc in leaves)
            if dead:
                self.m.record_failure(t.key, "structure", "dead_letter", f"{len(dead)} window(s) dead-lettered (previous cases kept)")
            log.info("structure target %s: incomplete re-run, previous cases kept", t.key)
            return
        n_cases = self._assemble(t, profile, version, loaded, leaves, target_hash, complete)
        self.stats["processed"] += 1
        self.stats["read_chars"] += sum(sp.end - sp.start for sp, _sc in leaves)
        if dead:
            if t.att_id:
                self.m.set_attachment_status(t.att_id, "failed")
            else:
                self.m.set_document_status(t.doc_id, "failed")
            self.m.record_failure(t.key, "structure", "dead_letter", f"{len(dead)} window(s) dead-lettered")
        log.info("structure target %s: windows=%d cases=%d complete=%s", t.key, len(leaves), n_cases, complete)

    # ---- G-D7: 검증 완료 대상은 --force 없이 다시 구조화하지 않는다 ----
    def _has_verified_case(self, t: Target) -> bool:
        if t.att_id:
            rows = self.m.select("SELECT 1 FROM cases WHERE att_id=? AND verification_status='verified' LIMIT 1", (t.att_id,))
        else:
            rows = self.m.select("SELECT 1 FROM cases WHERE doc_id=? AND att_id IS NULL AND verification_status='verified' LIMIT 1", (t.doc_id,))
        return bool(rows)

    def _refuse_verified(self, t: Target) -> bool:
        """이 대상을 새로 구조화하면 검수 기록이 사라진다. force 가 없으면 거부하고 실패 장부에 남긴다(mark_verified 는 첨부 상태를 바꾸지 않으므로 사건도 본다)."""
        if self.force or not (t.status in ("verified", "indexed") or self._has_verified_case(t)):
            return False
        self.stats["refused_verified"] += 1
        self.m.record_failure(t.key, "structure", "verified_needs_force",
                              "target is verified/indexed or has verified cases; re-structuring needs --force (cases return to unverified)")
        log.warning("structure target %s refused: verified (needs --force)", t.key)
        return True

    # ---- G-D6: 사건 구조화 없이 쪽 chunk 만 ----
    def _chunk_only(self, t: Target, authority: str) -> None:
        """심의기준·위원회 규정: 제공자를 부르지 않는다(외부 반출 없음). create_derivatives 권리가 allowed 여야 한다."""
        try:
            eff = rights_mod.effective_rights(self.m, t.doc_id)
            value = eff["rights"].get("create_derivatives")
            reason = None if value == "allowed" else f"create_derivatives_{value}"
        except KeyError:
            reason = "no_rights_record"
        if reason:
            self.stats["refused"] += 1
            self.m.record_failure(t.key, "structure", "derivatives_refused", reason)
            return
        loaded = load_pages(self.m, t, self.corpus)
        version = f"chunk-only:{CODE_VERSION}|{authority}"
        page_hashes = [(r["page_id"], r.get("text_sha256") or hashlib.sha256(loaded.texts.get(r["page_id"], "").encode("utf-8")).hexdigest())
                       for r in loaded.rows]
        target_hash = hashlib.sha256(json.dumps(page_hashes, sort_keys=True).encode("utf-8")).hexdigest()
        if (not self.force and t.status in ("structured", "verified", "indexed") and t.stored_hash == target_hash
                and t.stored_version == version):
            self.stats["skipped_unchanged"] += 1
            return
        if self._refuse_verified(t):
            return
        if not loaded.texts:
            raise StructureError("no_page_text", "all pages are empty")
        self.stats["chunk_only"] += 1
        if not self.execute:
            return
        decision_date = (self.m.get_document(t.doc_id) or {}).get("decision_date")
        with self.m.transaction():
            self._delete_derived(t)
            n = self._write_page_chunks(t, loaded, authority, decision_date)
            if t.att_id:
                self.m.set_attachment_status(t.att_id, "structured")
                self.m.update_attachment_fields(t.att_id, structurer_version=version, structure_input_sha256=target_hash)
                self.m.resolve_failures(t.key, "structure")
                atts = [a for a in self.m.attachments_for(t.doc_id) if not a.get("dedup_of") and a["status"] != "withheld"]
                if atts and all(a["status"] in ("structured", "verified", "indexed") for a in atts):
                    self.m.set_document_status(t.doc_id, "structured")
                    self.m.update_document_stage(t.doc_id, structurer_version=version, structure_input_sha256=target_hash)
            else:
                self.m.set_document_status(t.doc_id, "structured")
                self.m.update_document_stage(t.doc_id, structurer_version=version, structure_input_sha256=target_hash)
                self.m.resolve_failures(t.key, "structure")
        self.stats["chunks"] += n
        log.info("structure target %s: chunk-only pages=%d authority=%s", t.key, n, authority)

    def _write_page_chunks(self, t: Target, loaded: Loaded, authority: str, decision_date: Optional[str] = None) -> int:
        n = 0
        for r in loaded.rows:
            txt = loaded.texts.get(r["page_id"])
            if not txt:
                continue
            no = r.get("pdf_page_no")
            self.m.upsert_chunk(f"{r['page_id']}#pc", t.doc_id, txt, "pdf_page" if no is not None else "text_range",
                                str(no) if no is not None else f"{r['page_id']}:0-{len(txt)}", page_id=r["page_id"],
                                authority_type=authority, decision_date=decision_date)
            n += 1
        return n

    # ---- 창 작업 계획: 장부와 응답 파일을 보고 보낼 것만 고른다 ----
    def _plan(self, t: Target, prompts: PromptSet, version: str, roots: list, tdir: Path, stream: str, spans: list):
        leaves: list = []
        pending: deque = deque()
        dead: list = []
        queue = deque(roots)
        while queue:
            spec = queue.popleft()
            spec.messages = build_messages(prompts, t, spec)
            spec.job_id = job_id_for(t.key, spec.unit, spec.input_sha, version)
            est = self.p.estimate_cost(self.model, spec.messages, self.max_tokens)
            job = self.m.upsert_job(spec.job_id, t.key, spec.input_sha, version, unit=spec.unit, model=self.model, est_cost_usd=est)
            self.stats["windows"] += 1
            sc = read_job_response(tdir, job, spec) if job["status"] == "done" else None
            if sc and isinstance(sc.get("split"), list):
                kids = [make_spec(c["unit"], stream, spans, c["start"], c["end"]) for c in sc["split"]]
                queue.extendleft(reversed(kids))
                self.stats["windows"] -= 1
                continue
            if sc and isinstance(sc.get("response"), dict) and sc.get("input_sha256") == spec.input_sha:
                bad = response_problem(sc["response"])
                if bad:                                  # CX1-1: 계약을 어긴 저장 응답은 완료로 치지 않는다(다시 보내지 않고 dead-letter 로 남긴다)
                    if self.execute:
                        self._dead_letter(t, spec, tdir, "invalid_schema", bad, int(job.get("attempts") or 0), json.dumps(sc["response"], ensure_ascii=True))
                    dead.append(spec)
                    continue
                leaves.append((spec, sc))
                self.stats["windows_cached"] += 1
                continue
            if job["status"] == "dead" and not self.retry_dead:
                dead.append(spec)
                continue
            if job["status"] == "dead":
                for dl in self.m.dead_letters():
                    if dl["job_id"] == spec.job_id:
                        self.m.resolve_dead_letter(dl["id"])
            self.stats["est_pending_usd"] += est
            pending.append(spec)
        return leaves, pending, dead

    # ---- 호출 실행(스레드는 HTTP 만, 장부 쓰기는 이 스레드) ----
    def _ensure_model(self) -> None:
        if not self._model_ok:
            self.p.verify_model(self.model)
            self._model_ok = True

    def _execute(self, t: Target, prompts: PromptSet, version: str, pending: deque, tdir: Path, stream: str, spans: list):
        """보낼 창을 동시성 한도 안에서 보낸다. 반환 (응답이 있는 창, dead-letter 창, 끝내지 못한 창)."""
        leaves: list = []
        dead: list = []
        unfinished: list = []
        inflight: dict[Future, WindowSpec] = {}
        stop = False
        try:
            self._ensure_model()
        except ds.DeepSeekError as e:                        # 치명 오류(401·402)와 모델 목록 조회 실패(재시도 후): 실행을 aborted_* 로 끝낸다
            self.fatal = e
            return leaves, dead, list(pending)
        with ThreadPoolExecutor(max_workers=self.p.concurrency, thread_name_prefix="ds") as ex:
            while pending or inflight:
                while pending and not stop and len(inflight) < self.p.concurrency:
                    spec = pending[0]
                    try:
                        self.check_time()
                    except TimeBudgetExceeded:
                        self.paused = True
                        stop = True
                        break
                    reason = egress_reason(self.m, t.doc_id)        # 보내기 직전에 한 번 더
                    if reason:
                        self.stats["refused"] += 1
                        self.m.record_failure(t.key, "structure", "egress_refused", reason)
                        stop = True
                        break
                    if logsafe.contains_sensitive("\n".join(m["content"] for m in spec.messages)):
                        self.stats["refused"] += 1                              # 등록된 비밀값(API 키·OC 등)이 본문에 섞이면 보내지 않는다
                        self.m.record_failure(t.key, "structure", "egress_refused", "secret_in_payload")
                        stop = True
                        break
                    est = self.p.estimate_cost(self.model, spec.messages, self.max_tokens)
                    if not self.p.budget.would_fit(est):
                        self.stats["windows_budget_blocked"] += len(pending)
                        stop = True
                        break
                    pending.popleft()
                    self.m.update_job(spec.job_id, status="running")
                    fut = ex.submit(self.p.complete_json, self.model, spec.messages, max_tokens=self.max_tokens, job_id=spec.job_id,
                                    target_id=t.key)
                    inflight[fut] = spec
                if not inflight:
                    break
                done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for fut in done:
                    spec = inflight.pop(fut)
                    outcome = self._settle(t, spec, fut, tdir, stream, spans, pending, version, prompts)
                    if outcome == "ok":
                        leaves.append((spec, _read_json(response_path(tdir, spec.unit, spec.job_id))))
                    elif outcome == "dead":
                        dead.append(spec)
                    elif outcome == "blocked":
                        unfinished.append(spec)
                        self.stats["windows_budget_blocked"] += 1
                        stop = True
                    elif outcome == "fatal":
                        unfinished.append(spec)
                        stop = True
        unfinished.extend(pending)
        return leaves, dead, unfinished

    def _settle(self, t: Target, spec: WindowSpec, fut: Future, tdir: Path, stream: str, spans: list, pending: deque,
                version: str, prompts: PromptSet) -> str:
        """완료된 호출 하나를 장부에 반영한다. 반환: ok | dead | split | blocked | fatal."""
        try:
            res = fut.result()
        except ds.BudgetExceeded as e:
            ds.record_attempts(self.m, e.attempts, job_id=spec.job_id, target_id=t.key)
            self._count_attempts(e.attempts)
            self.m.update_job(spec.job_id, status="queued", error_class="budget_exceeded", error_message=str(e))
            return "blocked"
        except ds.FatalProviderError as e:
            ds.record_attempts(self.m, e.attempts, job_id=spec.job_id, target_id=t.key)
            self._count_attempts(e.attempts)
            self.m.update_job(spec.job_id, status="failed", error_class=e.error_class, error_message=str(e))
            self.m.record_failure(t.key, "structure", e.error_class, str(e))
            self.fatal = e
            return "fatal"
        except ds.CallFailed as e:
            ds.record_attempts(self.m, e.attempts, job_id=spec.job_id, target_id=t.key)
            self._count_attempts(e.attempts)
            if self._should_split(spec, e):
                kids = split_windows(stream[spec.start:spec.end], max(1000, (spec.end - spec.start) // 2 + 1), self.snap_chars)
                if len(kids) >= 2:
                    kid_specs = []
                    for j, (s, en, _tx) in enumerate(kids):
                        ks = make_spec(f"{spec.unit}.{j}", stream, spans, spec.start + s, spec.start + en)
                        ks.messages = build_messages(prompts, t, ks)
                        ks.job_id = job_id_for(t.key, ks.unit, ks.input_sha, version)
                        est = self.p.estimate_cost(self.model, ks.messages, self.max_tokens)
                        self.m.upsert_job(ks.job_id, t.key, ks.input_sha, version, unit=ks.unit, model=self.model, est_cost_usd=est)
                        kid_specs.append(ks)
                    rp = response_path(tdir, spec.unit, spec.job_id)
                    _write_json(rp, {"job_id": spec.job_id, "split": [{"unit": k.unit, "start": k.start, "end": k.end} for k in kid_specs],
                                     "reason": self._split_reason(e)})
                    self.m.update_job(spec.job_id, status="done", error_class="split", output_path=str(rp))
                    pending.extendleft(reversed(kid_specs))
                    self.stats["windows_split"] += 1
                    self.stats["windows"] += len(kid_specs)
                    return "split"
            self._dead_letter(t, spec, tdir, e.error_class, str(e), len(e.attempts), e.last_text)
            return "dead"
        except ds.RequestRejected as e:
            ds.record_attempts(self.m, e.attempts, job_id=spec.job_id, target_id=t.key)
            self._count_attempts(e.attempts)
            self._dead_letter(t, spec, tdir, e.error_class, str(e), len(e.attempts), e.last_text)
            return "dead"
        except Exception as e:    # 제공자 버그 등: 작업을 failed 로 남기고 실행을 멈춘다
            self.m.update_job(spec.job_id, status="failed", error_class=type(e).__name__, error_message=str(e))
            self.fatal = ds.FatalProviderError(f"unexpected {type(e).__name__}", error_class="unexpected_error")
            return "fatal"
        try:
            return self._store_result(t, spec, res, tdir, version)
        except Exception as e:      # noqa: BLE001 - 결과를 저장하다 예기치 않게 실패해도 작업이 running 으로 남아 매 실행에 다시 청구되지 않게 한다
            msg = f"{type(e).__name__}: {sanitize_message(str(e))}"
            self._dead_letter(t, spec, tdir, "settle_error", msg, len(res.attempts), None)
            return "dead"

    def _store_result(self, t: Target, spec: WindowSpec, res: Any, tdir: Path, version: str) -> str:
        cost = ds.record_attempts(self.m, res.attempts, job_id=spec.job_id, target_id=t.key)
        self._count_attempts(res.attempts)
        last = res.attempts[-1]
        bad = response_problem(res.obj)
        if bad:                                      # CX1-1: 응답은 받았지만(비용 기록됨) 계약 위반 -> 완료가 아니라 dead-letter. 기존 사건은 건드리지 않는다
            self.m.update_job(spec.job_id, cost_usd=cost)
            self._dead_letter(t, spec, tdir, "invalid_schema", bad, len(res.attempts), json.dumps(res.obj, ensure_ascii=True))
            self.stats["windows_called"] += 1
            return "dead"
        rp = response_path(tdir, spec.unit, spec.job_id)
        _write_json(rp, {
            "job_id": spec.job_id, "unit": spec.unit, "start": spec.start, "end": spec.end, "page_ids": spec.page_ids,
            "input_sha256": spec.input_sha, "structurer_version": version, "model": self.model, "returned_model": res.model,
            "usage": {"prompt_tokens": last.prompt_tokens, "cache_hit_tokens": last.cache_hit_tokens, "cache_miss_tokens": last.cache_miss_tokens,
                      "completion_tokens": last.completion_tokens, "reasoning_tokens": last.reasoning_tokens, "attempts": len(res.attempts),
                      "cost_usd_all_attempts": cost, "tier": last.tier},
            "response": res.obj})
        self.m.update_job(spec.job_id, status="done", attempts=len(res.attempts), cost_usd=cost, output_path=str(rp))
        self.stats["windows_called"] += 1
        return "ok"

    @staticmethod
    def _split_reason(e: "ds.CallFailed") -> str:
        """분할 사유: 시도 중 하나라도 출력이 잘렸으면(finish_reason=length) truncated_output, 아니면 invalid_json."""
        return "truncated_output" if any(a.error_class == "truncated_output" or a.finish_reason == "length" for a in e.attempts) else "invalid_json"

    @staticmethod
    def _should_split(spec: "WindowSpec", e: "ds.CallFailed") -> bool:
        """G-D18: 출력이 잘렸거나(finish_reason=length) 응답이 유효한 JSON 이 아니었으면 같은 창을 반으로 나눠 다시 요청한다. 최대 MAX_SPLIT_DEPTH 단계(작업 단위 이름의 점 개수)."""
        classes = {a.error_class for a in e.attempts} | {e.error_class}
        bad = bool(classes & set(SPLIT_ERROR_CLASSES)) or any(a.finish_reason == "length" for a in e.attempts)
        return bad and spec.end - spec.start >= MIN_SPLIT_CHARS and spec.unit.count(".") < MAX_SPLIT_DEPTH

    def _count_attempts(self, attempts: list) -> None:
        self.stats["api_attempts"] += len(attempts)
        self.stats["cost_usd"] += sum(a.cost_usd for a in attempts)

    def _dead_letter(self, t: Target, spec: WindowSpec, tdir: Path, error_class: str, message: str, attempts: int, last_text: Optional[str]) -> None:
        path = sha = None
        if last_text:
            try:        # CX1-5: 짝 없는 서로게이트가 있어도 쓰기가 실패하지 않게 이스케이프하고, 본문 저장이 실패해도 작업 상태는 확정한다
                data = last_text.encode("utf-8", errors="backslashreplace")
                dp = tdir / "dead" / f"{spec.job_id}.txt"
                private_mkdir(dp.parent)
                dp.write_bytes(data)
                path, sha = str(dp), hashlib.sha256(data).hexdigest()
            except Exception as e:      # noqa: BLE001
                message = f"{message} (response body not saved: {type(e).__name__})"
        try:
            self.m.update_job(spec.job_id, error_class=error_class, error_message=message)
            self.m.record_dead_letter(spec.job_id, t.key, f"{error_class}: {message}", attempts=attempts, response_sha256=sha, response_path=path)
        except Exception as e:          # noqa: BLE001 - dead-letter 기록이 실패해도 작업이 running 으로 남지 않게 failed 로 확정한다
            self.m.update_job(spec.job_id, status="failed", error_class=error_class, error_message=f"{message} (dead-letter not recorded: {type(e).__name__})")

    # ---- 조립·기록 ----
    def _assemble(self, t: Target, profile: sr.Profile, version: str, loaded: Loaded, leaves: list, target_hash: str, complete: bool) -> int:
        page_meta = {r["page_id"]: r for r in loaded.rows}
        all_texts = loaded.texts
        pt_cache = {pid: sr.PageText(txt) for pid, txt in all_texts.items()}
        ordered = sorted(leaves, key=lambda x: x[0].start)
        ledger = ledger_values(self.m, t.doc_id) if profile.name == "court_judgment" else {}
        outs: list[tuple[int, sr.CaseOut, str]] = []
        window_problems: list = []
        for order, (spec, sc) in enumerate(ordered):
            resp = (sc or {}).get("response") or {}
            cases = resp.get("cases")
            if not isinstance(cases, list):
                window_problems.append({"unit": spec.unit, "problem": "response_has_no_cases_array"})
                continue
            ctx = sr.WindowCtx(profile=profile, doc_id=t.doc_id, att_id=t.att_id, target_key=t.key, unit=spec.unit,
                               pages={pid: pt_cache[pid] for pid in spec.page_ids if pid in pt_cache}, page_meta=page_meta,
                               structurer={"version": version, "model": self.model, "input_sha256": spec.input_sha},
                               quarantine_rate=self.rate, ledger=ledger, source_id=t.source_id)
            batch = [co for co in (sr.assemble_case(d, ctx, k) for k, d in enumerate(cases)) if co]
            if profile.guard_shared_quotes:
                sr.drop_shared_fact_quotes(batch, {pid: pt.text for pid, pt in ctx.pages.items()}, self.rate, page_meta)
            sr.drop_shared_outcome_quotes(batch, {pid: pt.text for pid, pt in ctx.pages.items()}, self.rate, page_meta)       # V-FID-02
            outs.extend((order, co, spec.job_id) for co in batch)
        merged: list[tuple[sr.CaseOut, str]] = []
        last_by_key: dict = {}          # 정규화 사건번호 -> (창 순서, merged 인덱스)
        last_of_order: dict = {}        # 창 순서 -> 그 창의 마지막 사건의 merged 인덱스
        used_ids: set = set()
        for order, co, jid in outs:
            idx = None
            if co.key and co.key in last_by_key and order - last_by_key[co.key][0] <= 1:
                idx = last_by_key[co.key][1]                                   # 같은 사건번호가 인접 창에 나뉘어 나왔다
            elif co.starts_before_window and order - 1 in last_of_order:       # 번호가 보이지 않는 이어짐: 앞 창 마지막 사건이 이어진다고 표시했을 때만
                cand = merged[last_of_order[order - 1]][0]
                if cand.continues_after_window and (co.key is None or cand.key is None or co.key == cand.key):
                    idx = last_of_order[order - 1]
            if idx is not None:
                old_id = merged[idx][0].record["case_id"]
                merged[idx] = (sr.merge_cases(merged[idx][0], co, all_texts, self.rate), merged[idx][1])
                new_id = merged[idx][0].record["case_id"]
                if new_id != old_id:                                           # 이어짐 병합으로 번호 기반 case_id 로 바뀌었다: 다른 사건과 겹치면 창 이름을 붙인다
                    if new_id in used_ids:
                        merged[idx][0].record["case_id"] = new_id = f"{new_id}~{co.unit}"
                    used_ids.add(new_id)
                if merged[idx][0].key:
                    last_by_key[merged[idx][0].key] = (order, idx)
                last_of_order[order] = idx
                continue
            cid = co.record["case_id"]
            if cid in used_ids:
                co.record["case_id"] = f"{cid}~{co.unit}"
            used_ids.add(co.record["case_id"])
            merged.append((co, jid))
            last_of_order[order] = len(merged) - 1
            if co.key:
                last_by_key[co.key] = (order, len(merged) - 1)
        sr.require_row_unique_quote([co for co, _ in merged], pt_cache, t.source_id)            # G-D26: 병합 뒤, 사건 전체 기준(표 소스만)
        n_q = 0
        n_chunks = 0
        ledger_idx = None
        with self.m.transaction() as c:
            self._delete_derived(t)
            for co, jid in merged:
                rec = co.record
                sup = rec["supporting_passage_ids"]
                nos = [page_meta[p]["pdf_page_no"] for p in sup if p in page_meta and page_meta[p].get("pdf_page_no") is not None]
                self.m.upsert_case(rec["case_id"], t.doc_id, rec, att_id=t.att_id, first_page_id=sup[0] if sup else None,
                                   page_first=min(nos) if nos else None, page_last=max(nos) if nos else None, page_ids=sup,
                                   verification_status=rec["verification_status"], quote_total=rec["quote_check"]["total"],
                                   quote_ok=rec["quote_check"]["matched"], structurer_version=version,
                                   input_sha256=rec["structurer"]["input_sha256"], model=self.model, job_id=jid)
                if rec["verification_status"] == "quarantined":
                    n_q += 1
                    continue
                if profile.links_ledger:
                    if ledger_idx is None:
                        ledger_idx = links_mod.ledger_index(self.m)
                    links_mod.link_case(self.m, rec["case_id"], rec, index=ledger_idx)
                ch = sr.chunk_for_case(rec, all_texts, page_meta)
                if ch:
                    self.m.upsert_chunk(f"{rec['case_id']}#c1", t.doc_id, ch["text"], ch["locator_kind"], ch["locator_value"],
                                        case_id=rec["case_id"], authority_type=rec["authority_type"], decision_date=rec["decision_date"])
                    n_chunks += 1
            if self.page_chunks:
                n_chunks += self._write_page_chunks(t, loaded, profile.authority_types[0])
        self.stats["cases"] += len(merged)
        self.stats["cases_quarantined"] += n_q
        self.stats["chunks"] += n_chunks
        self.stats["dropped_items"] += sum(len(co.dropped) for co, _ in merged)
        _write_json(target_dir(self.corpus, t.key) / "assembled.json", {
            "target": t.key, "structurer_version": version, "complete": complete, "windows": len(ordered), "cases": len(merged),
            "quarantined": n_q, "chunks": n_chunks, "window_problems": window_problems, "empty_pages": loaded.empty,
            "cases_detail": [{"case_id": co.record["case_id"], "status": co.record["verification_status"], "errors": co.errors,
                              "dropped": co.dropped, "starts_before_window": co.starts_before_window,
                              "continues_after_window": co.continues_after_window} for co, _ in merged]})
        if complete:
            if t.att_id:
                # verified/indexed 는 force 로만 여기까지 온다(_refuse_verified). 새로 만든 사건은 unverified 이므로 첨부도 structured 로 되돌린다(G-D7).
                if self.m.select("SELECT status FROM attachments WHERE att_id=?", (t.att_id,))[0]["status"] in (
                        "extracted", "failed", "structured", "verified", "indexed"):
                    self.m.set_attachment_status(t.att_id, "structured")
                self.m.update_attachment_fields(t.att_id, structurer_version=version, structure_input_sha256=target_hash)
                self.m.resolve_failures(t.key, "structure")
                atts = [a for a in self.m.attachments_for(t.doc_id) if not a.get("dedup_of")]
                if atts and all(a["status"] in ("structured", "verified", "indexed") for a in atts):
                    self.m.set_document_status(t.doc_id, "structured")
                    self.m.update_document_stage(t.doc_id, structurer_version=version, structure_input_sha256=target_hash)
            else:
                self.m.set_document_status(t.doc_id, "structured")
                self.m.update_document_stage(t.doc_id, structurer_version=version, structure_input_sha256=target_hash)
                self.m.resolve_failures(t.key, "structure")
        return len(merged)

    def _delete_derived(self, t: Target) -> None:
        """이 대상의 cases/chunks 만 지운다(pages 는 추출 단계의 것이라 건드리지 않는다)."""
        with self.m.transaction() as c:
            if t.att_id:
                c.execute("DELETE FROM case_links WHERE case_id IN (SELECT case_id FROM cases WHERE att_id=?)", (t.att_id,))
                c.execute("DELETE FROM chunks WHERE case_id IN (SELECT case_id FROM cases WHERE att_id=?)", (t.att_id,))
                c.execute("DELETE FROM chunks WHERE case_id IS NULL AND chunk_id LIKE '%#pc' AND page_id IN "
                          "(SELECT page_id FROM pages WHERE att_id=?)", (t.att_id,))
                c.execute("DELETE FROM cases WHERE att_id=?", (t.att_id,))
            else:
                c.execute("DELETE FROM case_links WHERE case_id IN (SELECT case_id FROM cases WHERE doc_id=? AND att_id IS NULL)", (t.doc_id,))
                c.execute("DELETE FROM chunks WHERE case_id IN (SELECT case_id FROM cases WHERE doc_id=? AND att_id IS NULL)", (t.doc_id,))
                c.execute("DELETE FROM chunks WHERE case_id IS NULL AND chunk_id LIKE '%#pc' AND page_id IN "
                          "(SELECT page_id FROM pages WHERE doc_id=? AND att_id IS NULL)", (t.doc_id,))
                c.execute("DELETE FROM cases WHERE doc_id=? AND att_id IS NULL", (t.doc_id,))


# ---------------------------------------------------------------- 계보·검증 표시
def lineage(chunk_id: str, manifest: Optional[Manifest] = None, *, corpus_dir: Optional[Path] = None) -> Optional[dict]:
    """chunk -> case -> page -> attachment -> document -> list_page 를 장부 질의 하나로 거슬러 올라간다(``Manifest.lineage_of_chunk``)."""
    if manifest is not None:
        return manifest.lineage_of_chunk(chunk_id)
    root = Path(corpus_dir) if corpus_dir else default_corpus_dir()
    m = Manifest(root / "manifest.sqlite", in_memory_copy=True)
    try:
        return m.lineage_of_chunk(chunk_id)
    finally:
        m.close()


def mark_verified(manifest: Manifest, case_id: str, *, by: str, method: str = "independent_reviewer", at: str) -> None:
    """독립 검수자가 사건을 확인했을 때 record(verified_by)와 cases.verification_status 를 함께 갱신한다. quarantined 사건은 거부한다."""
    from .. import contracts
    row = manifest.get_case(case_id)
    if row is None:
        raise KeyError(case_id)
    if row["verification_status"] == "quarantined":
        raise ValueError("a quarantined case cannot be marked verified")
    rec = dict(row["record"])
    rec["verified_by"] = list(rec.get("verified_by", [])) + [{"by": by, "method": method, "at": at}]
    rec["verification_status"] = "verified"
    errs = contracts.validate_case_record(rec)
    if errs:
        raise ValueError("record is invalid after verification: " + "; ".join(errs[:3]))
    manifest.upsert_case(case_id, row["doc_id"], rec, att_id=row["att_id"], first_page_id=row["first_page_id"], page_first=row["page_first"],
                         page_last=row["page_last"], page_ids=json.loads(row["page_ids_json"]) if row["page_ids_json"] else None,
                         verification_status="verified", quote_total=row["quote_total"], quote_ok=row["quote_ok"],
                         structurer_version=row["structurer_version"], input_sha256=row["input_sha256"], model=row["model"], job_id=row["job_id"])


# ---------------------------------------------------------------- CLI
def main(argv: Optional[list] = None, *, provider: Optional[ds.DeepSeekProvider] = None, out: Any = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m press_review.ingestion.structure", description="DeepSeek 사건 구조화(P2 §4)")
    ap.add_argument("--corpus", help="corpus 디렉터리(기본: 데이터 디렉터리/corpus)")
    ap.add_argument("--source", action="append", help="source_id (반복 가능)")
    ap.add_argument("--att", action="append", help="att_id (반복 가능)")
    ap.add_argument("--doc", action="append", help="doc_id (반복 가능)")
    ap.add_argument("--profile", choices=sorted(sr.PROFILES), help="소스 프로필을 강제한다")
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=list(ds.ALLOWED_MODELS) + list(ds.LEGACY_MODEL_ALIASES),
                    help="공급자 정식 이름. 구 이름(deepseek-v4-flash)은 deepseek-flash 로 옮긴다")
    ap.add_argument("--thinking", default="disabled", choices=["disabled", "enabled"])
    ap.add_argument("--budget-usd", type=float, help="이 실행의 비용 상한(시도마다 사전 검사)")
    ap.add_argument("--max-seconds", type=float)
    ap.add_argument("--window-chars", type=int, default=WINDOW_CHARS)
    ap.add_argument("--snap-chars", type=int, default=SNAP_CHARS)
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    ap.add_argument("--quarantine-rate", type=float, default=sr.DEFAULT_QUARANTINE_RATE)
    ap.add_argument("--limit-targets", type=int)
    ap.add_argument("--retry-dead", action="store_true")
    ap.add_argument("--force", action="store_true", help="입력이 같아도 다시 조립한다(API 재호출 없음). 검증 완료 대상을 다시 구조화하려면 필요하고, 상태는 structured 로 돌아간다")
    ap.add_argument("--page-chunks", action="store_true", help="모든 쪽을 원문 그대로 chunk 로도 기록한다")
    ap.add_argument("--force-disk", action="store_true", help="디스크 가드(C: 여유 확인)를 우회한다. 기본 꺼짐")
    ap.add_argument("--plan-only", action="store_true", help="호출 없이 창 수와 예상 비용 상한만 계산한다(장부는 메모리 사본)")
    args = ap.parse_args(argv)
    args.model = ds.canonical_model(args.model)
    out = out or sys.stdout
    root = Path(args.corpus) if args.corpus else default_corpus_dir()
    guard, forced = None, []
    if not args.plan_only:                        # plan-only 는 메모리 사본만 읽는다(가드 없음)
        guard = diskguard.DiskGuard("구조화", diskguard.estimate("struct", 1), check_every=5, force=args.force_disk, on_force=forced.append, fallback_path=root)
        try:
            guard.start(0)                    # 시작 검사는 최소 여유+마진만. 대상 수가 정해지면 Structurer.run 이 예상량을 건다
        except diskguard.DiskGuardError as e:
            print(e.message_ko, file=sys.stderr)
            out.write(json.dumps({"command": "structure", "status": "disk_guard_stopped", "kind": e.kind, "free_bytes": e.free_bytes,
                                  "needed_bytes": e.needed_bytes, "message_ko": e.message_ko}, ensure_ascii=False, sort_keys=True) + "\n")
            return diskguard.exit_code(e)
    m = Manifest(root / "manifest.sqlite", in_memory_copy=args.plan_only)
    budget = Budget(args.max_seconds)
    prov = provider or ds.DeepSeekProvider(thinking=args.thinking, budget=ds.BudgetGuard(args.budget_usd))
    status, result, code = "completed", None, 0
    try:
        st = Structurer(m, prov, root, model=args.model, window_chars=args.window_chars, snap_chars=args.snap_chars,
                        max_tokens=args.max_tokens, quarantine_rate=args.quarantine_rate, retry_dead=args.retry_dead, force=args.force,
                        execute=not args.plan_only, page_chunks=args.page_chunks, check_time=budget.check, disk_guard=guard)
        targets = find_targets(m, source_ids=args.source, att_ids=args.att, doc_ids=args.doc, profile=args.profile, limit=args.limit_targets)
        result = st.run(targets)
        status = result["status"]
        code = 1 if status.startswith("aborted") else (diskguard.exit_code(st.disk_error) if st.disk_error is not None and not st.fatal else 0)
        if forced:
            result["disk_guard_forced"] = forced
    except ds.DeepSeekError as e:
        status, code = "failed", 1
        result = {"error_class": e.error_class, "error": sanitize_message(str(e))}
    finally:
        m.close()
    out.write(json.dumps({"command": "structure", "status": status, "model": args.model, "plan_only": args.plan_only, "result": result},
                         ensure_ascii=False, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())

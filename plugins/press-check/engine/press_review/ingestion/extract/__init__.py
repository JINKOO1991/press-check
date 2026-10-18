"""추출 단계(P2_DESIGN §2): 내려받은 첨부 -> 쪽 단위 텍스트(pages 테이블 + ``<corpus>/text/<family>/<안정ID>/``).

단계 규칙
- 입력 해시: ``attachments.extract_input_sha256`` = raw_sha256 (장별 PDF 가 있는 문서의 전체 PDF 는 raw 해시 + 장별 raw 해시 목록). 단계 버전은 ``EXTRACTOR_VERSION``.
  status=extracted 이고 해시·버전이 같으면 건너뛴다. 해시나 버전이 바뀌면 그 첨부의 파생 행(pages/cases/chunks)과 텍스트 폴더를 지우고 처음부터 한다.
- 재개: 추출 시작 때 입력 해시를 먼저 장부에 적는다. 시간 상한(--max-seconds)으로 중간에 멈추면 status 는 그대로이고, 다음 실행은 해시가 같으므로
  이미 쓴 쪽(텍스트 파일과 해시가 맞는 것)을 건드리지 않고 남은 쪽부터 잇는다. 오류가 난 쪽(pdftotext/OCR 하드 오류)은 다시 시도한다.
- 쪽 하드 오류가 남으면 첨부를 failed(``page_errors``)로 두고 failures 에 쪽 단위 행을 남긴다(재시도 가능, 상한 3회). 숨기지 않는다.
- 중복(dedup_of 가 있는 첨부)은 쪽을 만들지 않고 extracted 로만 표시한다(정본 첨부가 쪽을 가진다).
- 장별 PDF 와 전체 PDF 가 함께 있는 문서: 장별을 먼저 추출하고, 전체 PDF 는 장별 쪽과 텍스트 층 서명이 맞지 않는 쪽(빠진 쪽)만 추출한다.
  정렬 결과(전체 쪽 수, 장별 쪽 수, 보충한 쪽)는 documents.extra_json.p2_extract 에 기록한다. 정렬이 안 되면 합계로 판단하고 그 사실도 기록한다.
- (G-D14) PDF 추출기는 ``pdftotext``(``-layout`` 없는 기본 읽기 순서), ``pdftotext_layout``(``-layout``), ``kordoc`` 셋 중 하나다. 소스별 선택은 ``PDF_EXTRACTOR_BY_SOURCE`` 상수이고
  CLI ``--extractor`` 가 이번 실행의 대상 첨부 전체에 대해 그것을 덮어쓴다. 선택이 입력 해시(``extract_input_sha256``)에 들어가므로 선택이 바뀌면 그 첨부의 pages·cases·chunks 를 지우고 처음부터 다시 한다.
  구조화 이후 단계(structured/verified/indexed)에 있는 첨부도 해시가 달라질 때에 한해 다시 추출한다(버전만 오른 경우는 건드리지 않는다). 검증 완료(verified/indexed 또는 검증된 사건이 있는) 첨부는 ``--force`` 없이는 거부한다(G-D7과 같은 규칙).
  이 변경 전에 추출된 쪽(해시에 꼬리표가 없는 것)은 ``LEGACY_*`` 상수의 추출기(소스별 kordoc 또는 pdftotext_layout)로 만든 것이므로, 그 선택이면 해시가 그대로여서 다시 하지 않는다.
- (G-D26) 표 PDF 는 첨부 단위로 고른다: ``TABLE_AUTO_SOURCES`` 소스의 첨부는 ``layout.table_pdf_verdict`` 로 표가 감지되면 kordoc, 아니면 소스 기본값이다. 입력 해시 꼬리표는 감지 결과가 아니라 정책 이름(``x:auto-table-v1``)이고, 고른 추출기와 근거는 문서 메모(p2_extract.pdf_extractor·pdf_extractor_selection·table_pages)에 남는다.
- (G-D28) ``pdftotext_raw``·``column_crop`` 추출기와 깨진 텍스트층 쪽(``quality`` 사유 ``broken_layer``)의 OCR 시도. 깨진 쪽 번호는 문서 메모 ``broken_layer_pages`` 에 남는다.
- 경로: pages.text_path 는 코퍼스 폴더 기준 상대 경로. 텍스트 파일은 쪽 텍스트 그대로(UTF-8)이며 ``start``/``end`` offset 은 이 파일 기준이다.
"""
from __future__ import annotations

import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Optional

from ... import diskguard
from .. import stage, tiers
from ..context import TimeBudgetExceeded
from . import hwp as hwp_mod
from . import html as html_mod
from . import pageno
from . import pdf as pdf_mod
from .pdf import PageOut, PdfError
from .tools import ToolRunner

EXTRACTOR_VERSION = "p2x-5"          # 5: 표 PDF 는 첨부 단위 선택(G-D26), 추출기 pdftotext_raw·column_crop 추가와 선거백서 기본 column_crop, 깨진 텍스트층 쪽 OCR 강제(G-D28, quality 사유 broken_layer). 4: 소스별 PDF 추출기 선택을 오프라인 비교 결과로 정함(G-D14, docs/coverage/P2_extractor_compare.md). 3: 쪽 정보 없는 kordoc 출력(HWP 3.x 등)에서 표 블록이 빠지던 것을 고침(G-D19). 2: 표 PDF 소스는 kordoc 쪽별 추출이 기본(G-D4). 올리면 extracted 상태의 첨부를 처음부터 다시 한다(구조화 이후 단계는 해시가 같으면 두고 본다).
DEGRADED_SUFFIX = "~fb"              # 기본 추출기를 쓸 수 없어 폴백한 결과의 버전 꼬리표: 기본 추출기가 생기면 다음 실행이 다시 추출한다
# G-D4·G-D14: PDF 추출기 소스별 선택 상수. 없는 소스는 DEFAULT_PDF_EXTRACTOR(+품질 미달 쪽 OCR). 게이트 실측(IENDC 표 PDF): kordoc 인용 100%, 비용 pdftotext 의 1/4.
# 이름: pdftotext = 기본 읽기 순서(-layout 없음), pdftotext_layout = pdftotext -layout(이 변경 전의 기본 동작), kordoc = 쪽별 kordoc(-p N), 쪽 단위 pdftotext_layout 폴백.
# G-D28 추가: pdftotext_raw = pdftotext -raw(콘텐츠 스트림 순서), column_crop = 2단 조판으로 감지된 쪽만 좌·우 단으로 잘라 읽고(pdftotext -x -y -W -H) 나머지 쪽은 -layout.
# 어느 추출기든 텍스트층이 깨졌거나 빈 쪽(quality.assess 사유 broken_layer 포함)만 OCR 을 시도한다.
PDF_EXTRACTORS = ("pdftotext", "pdftotext_layout", "pdftotext_raw", "column_crop", "kordoc")
# 선택 근거: docs/coverage/P2_extractor_compare.md '## G-D14 오프라인 비교'(미니 게이트 표본의 DeepSeek 제안 인용을 세 추출기 텍스트에 G-D13 규칙으로 대조한 율, 가장 높은 것, 동률이면 pdftotext).
# 모델이 본 텍스트(대부분 -layout)에 유리하게 기운 비교이므로 표 소스(pac_board_correction_status)는 재실행 때 다시 본다. 목록에 없는 소스는 DEFAULT_PDF_EXTRACTOR.
# 선거백서(pac_election_whitepapers)는 G-D28 비교('## G-D28 비교')가 이 행을 pdftotext_layout 에서 column_crop 으로 바꾼다(G-D14 표의 이 행을 대체). pac_board_correction_status 는 첨부 단위 표 감지가 이 값보다 앞선다(G-D26, TABLE_AUTO_SOURCES).
PDF_EXTRACTOR_BY_SOURCE = {
    "iendc_review_results": "kordoc",
    "pac_correction_casebooks": "pdftotext",
    "pac_adjustment_casebooks": "pdftotext",
    "pac_election_whitepapers": "column_crop",
    "pac_board_correction_status": "pdftotext_layout",
    "pac_research_judgment_analysis": "pdftotext_layout",
}
DEFAULT_PDF_EXTRACTOR = "pdftotext_layout"
# G-D14 이전에 추출된 쪽의 추출기(해시 꼬리표가 없는 쪽). 바꾸지 않는다: PDF_EXTRACTOR_BY_SOURCE 를 바꿔도 이미 만든 쪽의 출처 기록은 이 값이다.
LEGACY_PDF_EXTRACTOR_BY_SOURCE = {"iendc_review_results": "kordoc"}
LEGACY_DEFAULT_PDF_EXTRACTOR = "pdftotext_layout"
# G-D26: 표 PDF 는 첨부 단위로 고른다. 이 소스의 첨부는 결정적 표 감지(layout.table_pdf_verdict)가 되면 kordoc, 아니면 소스 기본값이다. 명시 선택(--extractor)이 우선한다.
# 입력 해시에는 감지 결과가 아니라 정책 이름(TABLE_POLICY_TAG)을 넣는다: 결과는 원본 바이트의 결정적 함수이므로 건너뛰기 검사가 쪽을 다시 읽지 않아도 된다.
TABLE_AUTO_SOURCES = frozenset({"pac_board_correction_status"})
TABLE_POLICY_TAG = "auto-table-v1"
# G-D28: 깨진 텍스트층(quality 사유 broken_layer) 판정과 그 쪽의 OCR 강제는 이 소스에만 켠다. 임계값은 선거백서 표본으로 맞췄고(제17대 총선 백서 140쪽 중 60쪽),
# 다른 소스의 미표본 PDF 를 쪽 샘플로 재면 조정중재·시정권고 사례집 258개(약 27,700쪽 중 표본 2,767쪽)는 0쪽이지만 국내언론관계판결집 14개 첨부(1980~90년대 판 포함)는
# 표본 1,417쪽 중 41쪽(2.9%)이 걸렸다. 그 책들은 전각 쉼표('，')가 인쇄 양식이면서 OCR 오류도 섞여 있어 G-D30 게이트를 통과한 입력을 바꿀 수 있으므로 넓히지 않았다.
BROKEN_LAYER_SOURCES = frozenset({"pac_election_whitepapers"})
LATER_STATUSES = ("structured", "verified", "indexed")
MAX_RETRIES_BEFORE_SKIP = 3
ALIGN_MIN_SHARE = 0.8
REPEAT_STOP = 3
NOT_WORK = ("skipped", "dedup_skipped", "deferred_waiting_chapters", "refused_verified", "skipped_archived")
EXTRACTABLE = ("fetched", "extracted", "failed") + LATER_STATUSES


# ---------------------------------------------------------------- 쪽 저장
def page_id_for(att_id: str, kind: str, ordinal: int) -> str:
    return f"{att_id}:{kind}{ordinal:04d}"


def text_dir(corpus_dir: Path, family: str, att_id: str) -> Path:
    return Path(corpus_dir) / "text" / family / stage.stable_id(att_id)


def pdf_extractor_for(att: dict) -> str:
    ex = PDF_EXTRACTOR_BY_SOURCE.get(att.get("source_id") or "", DEFAULT_PDF_EXTRACTOR)
    if ex not in PDF_EXTRACTORS:
        raise ValueError(f"unknown PDF extractor '{ex}' for source {att.get('source_id')} (use {', '.join(PDF_EXTRACTORS)})")
    return ex


def extractor_signature(att: dict, extractor: Optional[str] = None) -> str:
    """입력 해시에 넣는 추출기 꼬리표. PDF 가 아닌 형식과 '이전 동작'과 같은 선택(LEGACY_*)은 빈 문자열이라 기존 해시가 그대로 유효하다."""
    if (att.get("detected_type") or "pdf") != "pdf":
        return ""
    if extractor is None and (att.get("source_id") or "") in TABLE_AUTO_SOURCES:
        return f"x:{TABLE_POLICY_TAG}"
    ex = extractor or pdf_extractor_for(att)
    if ex not in PDF_EXTRACTORS:
        raise ValueError(f"unknown PDF extractor '{ex}' (use {', '.join(PDF_EXTRACTORS)})")
    legacy = LEGACY_PDF_EXTRACTOR_BY_SOURCE.get(att.get("source_id") or "", LEGACY_DEFAULT_PDF_EXTRACTOR)
    return "" if ex == legacy else f"x:{ex}"


def resolve_pdf_extractor(runner: ToolRunner, att: dict, path: Optional[Path], override: Optional[str] = None) -> tuple:
    """(추출기, 선택 메모|None). 명시 선택이 가장 우선이다. 표 자동 선택 소스(``TABLE_AUTO_SOURCES``)는 원본을 감지해 표면 kordoc, 아니면 소스 기본값(G-D26)."""
    if override:
        return override, None
    default = pdf_extractor_for(att)
    if (att.get("source_id") or "") not in TABLE_AUTO_SOURCES or path is None:
        return default, None
    try:
        v = pdf_mod.detect_table_pdf(runner, path)
    except PdfError as e:
        return default, {"pdf_extractor_selection": "table_check_failed", "table_check_error": e.error_class}
    return ("kordoc" if v["is_table"] else default), {
        "pdf_extractor_selection": "table_detected" if v["is_table"] else "table_not_detected",
        "table_pages": f"{v['table_pages']}/{v['pages']}"}


def persist_page(ctx: Any, att: dict, family: str, out: PageOut, *, extra_qj: Optional[dict] = None) -> str:
    """텍스트 파일을 원자적으로 쓰고 pages 행을 넣는다. 반환: page_id."""
    pid = page_id_for(att["att_id"], out.kind, out.ordinal)
    rel = f"text/{family}/{stage.stable_id(att['att_id'])}/{out.kind}{out.ordinal:04d}.txt"
    data = out.text.encode("utf-8")
    stage.atomic_write_bytes(Path(ctx.corpus_dir) / rel, data)
    qj = dict(out.quality_json)
    if out.kind == "p":
        qj["pn_candidates"] = pageno.page_candidates(out.text)
    if extra_qj:
        qj.update(extra_qj)
    ctx.manifest.upsert_page(
        pid, att["doc_id"], att_id=att["att_id"], pdf_page_no=out.pdf_page_no, printed_page_no=None, method=out.method,
        text_path=rel, text_sha256=stage.sha256_bytes(data), char_count=len(out.text), quality=out.quality, quality_json=qj,
        ocr_confidence=out.ocr_confidence, needs_review=out.needs_review, extractor_version=EXTRACTOR_VERSION)
    return pid


def _valid_existing_pages(ctx: Any, att: dict) -> dict:
    """재개용: {page_id: 행}. 텍스트 파일이 있고 해시가 맞고 하드 오류가 없는 쪽만."""
    ok = {}
    for row in ctx.manifest.pages_of(att_id=att["att_id"]):
        try:
            data = stage.resolve(Path(ctx.corpus_dir), row["text_path"]).read_bytes()
        except (OSError, ValueError, TypeError):
            continue
        if stage.sha256_bytes(data) != row["text_sha256"]:
            continue
        qj = json.loads(row["quality_json"]) if row["quality_json"] else {}
        if qj.get("error") or (qj.get("ocr") or {}).get("error"):
            continue
        ok[row["page_id"]] = row
    return ok


def _apply_printed_numbers(ctx: Any, att: dict, total: int) -> int:
    rows = ctx.manifest.pages_of(att_id=att["att_id"])
    cands = {}
    for r in rows:
        if r["pdf_page_no"] is None:
            continue
        qj = json.loads(r["quality_json"]) if r["quality_json"] else {}
        cands[r["pdf_page_no"]] = qj.get("pn_candidates") or []
    resolved = pageno.resolve(cands, total)
    n = 0
    for r in rows:
        want = resolved.get(r["pdf_page_no"])
        if want != r["printed_page_no"]:
            ctx.manifest.upsert_page(
                r["page_id"], r["doc_id"], att_id=r["att_id"], pdf_page_no=r["pdf_page_no"], printed_page_no=want,
                method=r["method"], text_path=r["text_path"], text_sha256=r["text_sha256"], char_count=r["char_count"],
                quality=r["quality"], quality_json=json.loads(r["quality_json"]) if r["quality_json"] else None,
                ocr_confidence=r["ocr_confidence"], needs_review=bool(r["needs_review"]), extractor_version=r["extractor_version"])
        if want is not None:
            n += 1
    return n


# ---------------------------------------------------------------- 첨부 한 건
class _Outcome(dict):
    pass


def _fail(ctx: Any, att: dict, cls: str, msg: str, *, keep_retries: bool = False) -> dict:
    m = ctx.manifest
    m.record_failure(att["att_id"], "extract", cls, msg, retry_count=0 if keep_retries else None)
    m.set_attachment_status(att["att_id"], "failed")
    stage.refresh_document_status(m, att["doc_id"])
    return {"outcome": "failed", "error_class": cls}


def _has_verified(m: Any, att: dict) -> bool:
    """이 첨부를 다시 추출하면 검수 기록(verified 사건)이 사라지는가."""
    return att["status"] in ("verified", "indexed") or bool(
        m.select("SELECT 1 FROM cases WHERE att_id=? AND verification_status='verified' LIMIT 1", (att["att_id"],)))


def extract_attachment(ctx: Any, runner: ToolRunner, att: dict, family: str, *, ocr: bool = True,
                       plan_sig: str = "", select_pages: Optional[Callable[[int], tuple]] = None,
                       extractor: Optional[str] = None, force: bool = False) -> dict:
    """첨부 한 건 추출. select_pages(total) -> (쪽 목록, 문서 메모 dict|None, extra_qj|None) 은 전체 PDF 의 빠진 쪽 보충용(느리게 계산).
    extractor: PDF 추출기 선택(None 이면 소스별 상수). 선택은 입력 해시에 들어간다(G-D14)."""
    m = ctx.manifest
    aid = att["att_id"]
    xsig = extractor_signature(att, extractor)
    parts = [p for p in (plan_sig, xsig) if p]
    want = att["raw_sha256"] if not parts else stage.sha256_bytes(f"{att['raw_sha256']}|{'|'.join(parts)}".encode("utf-8"))
    same_input = att.get("extract_input_sha256") == want
    if att["status"] == "extracted" and same_input and att.get("extractor_version") == EXTRACTOR_VERSION:
        return {"outcome": "skipped"}
    if att["status"] in LATER_STATUSES and same_input:       # 구조화 이후 단계는 입력(해시)이 달라질 때만 다시 한다. 단계 버전만 오른 경우는 지우지 않는다
        return {"outcome": "skipped"}
    if att.get("dedup_of"):
        m.update_attachment_fields(aid, extractor_version=EXTRACTOR_VERSION, extract_input_sha256=want)
        m.set_attachment_status(aid, "extracted")
        m.resolve_failures(aid, "extract")
        stage.refresh_document_status(m, att["doc_id"])
        return {"outcome": "dedup_skipped"}
    try:
        path = stage.resolve(Path(ctx.corpus_dir), att["raw_path"])
    except (ValueError, TypeError):
        path = None
    if att.get("raw_archived_to") and (path is None or not path.is_file()):
        return {"outcome": "skipped_archived"}      # 외부 보관됨: 상태를 내리지 않고 건너뛴다(다시 추출하려면 복원 스크립트로 먼저 되돌린다)
    if path is None or not path.is_file():
        m.record_failure(aid, "extract", "raw_missing", "raw file not found; fetch will download it again")
        m.set_attachment_status(aid, "discovered")             # fetch 단계가 다시 받는다
        stage.refresh_document_status(m, att["doc_id"])
        return {"outcome": "failed", "error_class": "raw_missing"}

    resume = same_input and att.get("extractor_version") == EXTRACTOR_VERSION
    if not resume:
        if not force and _has_verified(m, att):
            m.record_failure(aid, "extract", "verified_needs_force", "re-extraction would delete verified cases; pass --force", retry_count=0)
            return {"outcome": "refused_verified", "error_class": "verified_needs_force"}
        with m.transaction():
            m.delete_derived(att_id=aid)
            m.update_attachment_fields(aid, extract_input_sha256=want, extractor_version=EXTRACTOR_VERSION)
            if att["status"] != "fetched":
                m.set_attachment_status(aid, "fetched")
        shutil.rmtree(text_dir(Path(ctx.corpus_dir), family, aid), ignore_errors=True)

    kind = att.get("detected_type") or ""
    if not kind:
        from ..fetch import detect_kind
        with open(path, "rb") as fh:
            kind = detect_kind(fh.read(4096))
    try:
        if kind == "pdf":
            res = _extract_pdf(ctx, runner, att, family, path, ocr=ocr, select_pages=select_pages, extractor=extractor)
        elif kind in ("hwp", "hwpx", "hwp3"):
            res = _extract_pages_list(ctx, att, family, hwp_mod.extract_hwp(runner, path))
        elif kind == "html":
            outs = html_mod.extract_html(path.read_bytes())
            if not outs:
                raise PdfError("empty_html", "no body text found")
            res = _extract_pages_list(ctx, att, family, outs)
        else:
            # xls/doc/알 수 없는 형식: 원문은 보존하고 추출만 보류한다(실패 행은 남는다)
            m.record_failure(aid, "extract", "unsupported_type", f"stored type '{kind}' has no extractor in P2", retry_count=0)
            m.set_attachment_status(aid, "withheld")
            stage.refresh_document_status(m, att["doc_id"])
            return {"outcome": "withheld_unsupported", "error_class": "unsupported_type"}
    except TimeBudgetExceeded:
        raise
    except hwp_mod.KordocUnavailable as e:
        return _fail(ctx, att, e.error_class, str(e), keep_retries=True)    # 도구 미설치는 재시도 상한에 세지 않는다
    except hwp_mod.KordocFailed as e:
        return _fail(ctx, att, e.error_class, str(e))
    except PdfError as e:
        return _fail(ctx, att, e.error_class, str(e), keep_retries=e.error_class.endswith("_unavailable"))
    if res.get("page_errors"):
        for pid, cls in res["page_errors"].items():
            m.record_failure(pid, "extract_page", cls, f"page text extraction error: {cls}")
        return _fail(ctx, att, "page_errors", f"{len(res['page_errors'])} page(s) with hard errors")
    with m.transaction():
        m.update_attachment_fields(aid, page_count=res["page_count"], extractor_version=res.get("extractor_version", EXTRACTOR_VERSION),
                                   extract_input_sha256=want)
        m.set_attachment_status(aid, "extracted")
        m.resolve_failures(aid, "extract")
        m.update_document_stage(att["doc_id"], extractor_version=EXTRACTOR_VERSION)
        if res.get("doc_note"):
            m.update_document_fields(att["doc_id"], extra_merge={"p2_extract": res["doc_note"]})
        stage.refresh_document_status(m, att["doc_id"])
    return {"outcome": "extracted", **{k: res[k] for k in ("pages", "ocr_pages", "needs_review", "printed_numbered") if k in res}}


def _extract_pages_list(ctx: Any, att: dict, family: str, outs: list) -> dict:
    nr = 0
    with ctx.manifest.transaction():
        for o in outs:
            persist_page(ctx, att, family, o)
            nr += int(o.needs_review)
    return {"pages": len(outs), "page_count": len(outs), "needs_review": nr}


def _iter_pages_kordoc(runner: ToolRunner, path: Path, pages: list, *, ocr: bool, workdir_root: Optional[Path],
                       check_time: Optional[Callable[[], None]], state: dict, check_broken: bool = False):
    """kordoc 쪽별 추출을 기본으로, 쪽마다 pdftotext(+OCR)로 폴백한다. 폴백 사유는 쪽 quality_json.extractor_fallback 에 남는다.
    kordoc 이 없으면(``state['unavailable']``) 첫 쪽에서 알게 되고 나머지 쪽은 kordoc 을 다시 부르지 않는다."""
    for n in pages:
        if check_time:
            check_time()
        reason = None
        out = None
        if state["unavailable"]:
            reason = "kordoc_unavailable"
        else:
            try:
                out = hwp_mod.extract_pdf_page(runner, path, n, check_broken)
                if out.quality_json.get("reasons"):          # 텍스트 층이 빈약한 쪽(스캔 등)은 pdftotext + OCR 쪽이 판단한다
                    out, reason = None, "kordoc_low_quality"
            except hwp_mod.KordocUnavailable:
                state["unavailable"] = True
                reason = "kordoc_unavailable"
            except hwp_mod.KordocFailed as e:
                reason = e.error_class
        if out is None:
            out = pdf_mod.extract_page(runner, path, n, ocr=ocr, workdir_root=workdir_root, check_broken=check_broken)
            out.quality_json["extractor_fallback"] = reason
            state["pages"].append(n)
        else:
            out.quality_json["extractor"] = "kordoc"
        yield out


def _broken_layer(row: Any) -> bool:
    qj = json.loads(row["quality_json"]) if row["quality_json"] else {}
    return "broken_layer" in (qj.get("reasons") or []) or "broken_layer" in ((qj.get("text_layer") or {}).get("reasons") or [])


def _extract_pdf(ctx: Any, runner: ToolRunner, att: dict, family: str, path: Path, *, ocr: bool,
                 select_pages: Optional[Callable[[int], tuple]], extractor: Optional[str] = None) -> dict:
    total = pdf_mod.page_count(runner, path)
    pages, doc_note, extra_qj = list(range(1, total + 1)), None, None
    if select_pages:
        pages, doc_note, extra_qj = select_pages(total)
    existing = _valid_existing_pages(ctx, att)
    todo = [n for n in pages if page_id_for(att["att_id"], "p", n) not in existing]
    primary, selection = resolve_pdf_extractor(runner, att, path, extractor)
    check_broken = (att.get("source_id") or "") in BROKEN_LAYER_SOURCES
    fb: dict = {"unavailable": False, "pages": []}
    if primary == "kordoc":
        it = _iter_pages_kordoc(runner, path, todo, ocr=ocr, workdir_root=Path(ctx.corpus_dir) / "tmp", check_time=ctx.check_time, state=fb,
                                check_broken=check_broken)
    else:
        it = pdf_mod.iter_pages(runner, path, todo, ocr=ocr, workdir_root=Path(ctx.corpus_dir) / "tmp", check_time=ctx.check_time, mode=primary,
                                check_broken=check_broken)
    for out in it:
        persist_page(ctx, att, family, out, extra_qj=extra_qj)
    wanted = set(pages)
    rows = [r for r in ctx.manifest.pages_of(att_id=att["att_id"]) if r["pdf_page_no"] in wanted]
    if len(rows) != len(pages):
        raise PdfError("page_count_mismatch", f"expected {len(pages)} page rows, found {len(rows)}")
    errors: dict = {}                       # 쪽 하드 오류는 재개 때 다시 시도해야 하므로 호출자가 첨부를 failed 로 둔다
    for r in rows:
        qj = json.loads(r["quality_json"]) if r["quality_json"] else {}
        if (qj.get("error") and not r["char_count"]) or ((qj.get("ocr") or {}).get("error") and r["needs_review"]):
            errors[r["page_id"]] = qj.get("error") or qj["ocr"]["error"]
    printed = _apply_printed_numbers(ctx, att, total)
    version = EXTRACTOR_VERSION
    if primary == "kordoc":
        # 숨기지 않는다: 어느 추출기로 읽었는지와 폴백한 쪽을 문서 메모에 남긴다(사유는 쪽의 quality_json.extractor_fallback).
        fb_pages = sorted(r["pdf_page_no"] for r in rows if (json.loads(r["quality_json"]) if r["quality_json"] else {}).get("extractor_fallback"))
        note = dict(doc_note or {})
        if fb["unavailable"] or (rows and len(fb_pages) == len(rows)):
            note["pdf_extractor"] = "pdftotext_fallback:" + ("kordoc_unavailable" if fb["unavailable"] else "kordoc_failed")
        else:
            note["pdf_extractor"] = "kordoc"
            note["pdf_fallback_pages"] = stage.ranges_str(fb_pages) if fb_pages else ""
        doc_note = note
        if fb["unavailable"]:
            version = EXTRACTOR_VERSION + DEGRADED_SUFFIX
    else:
        doc_note = {**(doc_note or {}), "pdf_extractor": primary}
    if selection:
        doc_note = {**(doc_note or {}), **selection}                       # G-D26: 어떤 근거로 골랐는지 숨기지 않는다
    broken = sorted(r["pdf_page_no"] for r in rows if _broken_layer(r))
    if broken:
        doc_note = {**(doc_note or {}), "broken_layer_pages": stage.ranges_str(broken)}         # G-D28: 텍스트층이 깨진 쪽(OCR 을 시도한 쪽)
    return {"pages": len(rows), "page_count": total, "ocr_pages": sum(1 for r in rows if r["method"] == "ocr"),
            "needs_review": sum(1 for r in rows if r["needs_review"]), "printed_numbered": printed,
            "doc_note": doc_note, "page_errors": errors or None, "extractor_version": version}


# ---------------------------------------------------------------- 장별/전체 정렬
def plan_supplement(runner: ToolRunner, ctx: Any, family_cache: dict, full_att: dict, chapters: list) -> Callable[[int], tuple]:
    """전체 PDF 에서 장별 PDF 가 덮지 못한 쪽만 고르는 select_pages 함수를 만든다(느리게 계산, 해시 불일치일 때만 호출됨)."""

    def select(total_full: int) -> tuple:
        corpus = Path(ctx.corpus_dir)
        index: dict = defaultdict(list)
        for n, s in pdf_mod.text_signatures(runner, stage.resolve(corpus, full_att["raw_path"]), total_full, ctx.check_time).items():
            if s:
                index[s].append(n)
        covered: set = set()
        starts: dict = {}
        unaligned: list = []
        chapter_pages = 0
        for c in sorted(chapters, key=lambda a: a["att_id"]):
            cpath = stage.resolve(corpus, c["raw_path"])
            cp = pdf_mod.page_count(runner, cpath)
            chapter_pages += cp
            sigs = pdf_mod.text_signatures(runner, cpath, cp, ctx.check_time)
            valid = [(i, s) for i, s in sigs.items() if s]
            score: Counter = Counter()
            for i, s in valid:
                for f in index.get(s, []):
                    score[f - i + 1] += 1
            ranked = score.most_common(2)
            ok = bool(valid) and bool(ranked) and ranked[0][1] / len(valid) >= ALIGN_MIN_SHARE \
                and not (len(ranked) > 1 and ranked[1][1] == ranked[0][1])
            start = ranked[0][0] if ok else None
            if not ok or start < 1 or start + cp - 1 > total_full:
                unaligned.append(c["att_id"])
                continue
            covered.update(range(start, start + cp))
            starts[c["att_id"]] = start
        note = {"mode": "chapters_first", "full_att": full_att["att_id"], "full_pages": total_full, "chapter_pages": chapter_pages,
                "chapters": str(len(chapters))}
        if unaligned:
            # 정렬 불가(스캔본 등): 합계가 전체 이상이면 장별이 덮은 것으로 보고, 아니면 전체를 모두 추출한다. 어느 쪽이든 기록한다.
            note["unaligned"] = ",".join(unaligned)
            if chapter_pages >= total_full:
                note["mode"] = "full_superseded_unaligned"
                return [], note, None
            note["mode"] = "full_extracted_unaligned_may_duplicate_chapters"
            return list(range(1, total_full + 1)), note, {"supplement": True, "alignment": "failed"}
        missing = [n for n in range(1, total_full + 1) if n not in covered]
        note["supplemented_pages"] = stage.ranges_str(missing) if missing else ""
        note["mode"] = "chapters_first" if missing else "full_superseded"
        return missing, note, {"supplement": True}

    return select


# ---------------------------------------------------------------- 실행
def _plan_sig(chapters: list) -> str:
    return "chapters:" + stage.sha256_bytes("|".join(sorted(f"{c['att_id']}={c['raw_sha256']}" for c in chapters)).encode("utf-8"))


def select_candidates(manifest: Any, *, source: Optional[str], tier_set: tuple, retry_exhausted: bool,
                      att_ids: Optional[list] = None) -> tuple:
    rows = manifest.list_attachments(source_id=source, right=("create_derivatives", "allowed"))
    open_fail = {f["target"]: f for f in manifest.failures(open_only=True) if f["stage"] == "extract"}
    out, unmapped, exhausted = [], set(), 0
    want = set(att_ids) if att_ids else None
    for a in rows:
        if want is not None and a["att_id"] not in want:      # --att: 지정한 첨부만(게이트 표본용)
            continue
        if not a["raw_path"] or a["status"] not in EXTRACTABLE:
            continue
        t = tiers.tier_of(a["source_id"])
        if t is None:
            unmapped.add(a["source_id"])
            if source is None and want is None:
                continue
        elif t not in tier_set and want is None:
            continue
        if a["status"] == "failed":
            f = open_fail.get(a["att_id"])
            if f and f["retry_count"] >= MAX_RETRIES_BEFORE_SKIP and not retry_exhausted:
                exhausted += 1
                continue
        out.append(a)
    return out, {"unmapped_sources": sorted(unmapped), "skipped_exhausted": exhausted}


def run_extract(ctx: Any, *, source: Optional[str] = None, tier: Optional[str] = None, limit: Optional[int] = None,
                runner: Optional[ToolRunner] = None, retry_exhausted: bool = False, ocr: bool = True,
                att_ids: Optional[list] = None, extractor: Optional[str] = None, force: bool = False) -> dict:
    if extractor is not None and extractor not in PDF_EXTRACTORS:
        raise ValueError(f"unknown extractor '{extractor}' (use {', '.join(PDF_EXTRACTORS)})")
    runner = runner or ctx.extras.get("tool_runner") or ToolRunner()
    tier_set = tiers.parse_tiers(tier)
    cands, info = select_candidates(ctx.manifest, source=source, tier_set=tier_set, retry_exhausted=retry_exhausted,
                                    att_ids=att_ids)
    by_doc: dict = {}
    for a in sorted(cands, key=lambda a: tiers.sort_key(a["source_id"], a["doc_id"] + "|" + a["att_id"])):
        by_doc.setdefault(a["doc_id"], []).append(a)
    if ctx.dry_run:
        return {"stage": "extract", "dry_run": True, "documents": len(by_doc), "attachments": len(cands), **info}
    guard = ctx.extras.get("disk_guard")
    if guard is not None:                                  # 후보 첨부 수(limit 적용 후)만큼 쓸 여유가 있는지 첫 첨부 전에 확인한다
        guard.start(min(len(cands), limit) if limit is not None else len(cands))
    counts: Counter = Counter()
    sums: Counter = Counter()
    errors: Counter = Counter()
    fam_cache: dict = {}
    attempted, last_cls, repeat, stopped = 0, None, 0, None
    for doc_id, atts in by_doc.items():
        if limit is not None and attempted >= limit:
            break
        ctx.check_time()
        family = stage.family_of(ctx.manifest, atts[0]["source_id"], fam_cache)
        order = {"chapter": 0, "full": 2}
        atts = sorted(atts, key=lambda a: (order.get(a["kind"], 1), a["att_id"]))
        for att in atts:
            if limit is not None and attempted >= limit:
                break
            ctx.check_time()
            try:
                res = _extract_one(ctx, runner, att, family, ocr=ocr, extractor=extractor, force=force)
            except (TimeBudgetExceeded, diskguard.DiskGuardError):      # 디스크 부족은 실패가 아니라 이어받기 가능한 정지다
                raise
            except Exception as e:                       # 어댑터 버그: 기록하고 이어 간다. 같은 클래스 3연속이면 멈춘다
                cls = type(e).__name__
                ctx.manifest.record_failure(att["att_id"], "extract", cls, str(e))
                ctx.manifest.set_attachment_status(att["att_id"], "failed")
                stage.refresh_document_status(ctx.manifest, att["doc_id"])
                ctx.logger.error("extract %s crashed: %s", att["att_id"], cls, exc_info=True)
                res = {"outcome": "failed", "error_class": cls, "unexpected": True}
            counts[res["outcome"]] += 1
            if ctx.extras.get("disk_guard") is not None:
                ctx.extras["disk_guard"].advance()
            if res["outcome"] not in NOT_WORK:                 # --limit 은 실제로 처리한 건수만 센다(이미 끝난 첨부를 건너뛴 것은 제외)
                attempted += 1
            if res["outcome"] == "failed":
                errors[res["error_class"]] += 1
            if res.get("unexpected"):
                repeat = repeat + 1 if res["error_class"] == last_cls else 1
                last_cls = res["error_class"]
                if repeat >= REPEAT_STOP:
                    stopped = f"repeated_error:{last_cls}"
                    break
            else:
                last_cls, repeat = None, 0
            for k in ("pages", "ocr_pages", "needs_review", "printed_numbered"):
                sums[k] += res.get(k, 0)
            ctx.logger.info("extract %s %s", att["att_id"], res["outcome"])
        if stopped:
            break
    return {"stage": "extract", "documents": len(by_doc), "attempted": attempted, "outcomes": dict(counts), "totals": dict(sums),
            "errors": dict(errors), "stopped": stopped, **info}


def _local_file(ctx: Any, att: dict) -> bool:
    try:
        return stage.resolve(Path(ctx.corpus_dir), att["raw_path"]).is_file()
    except (ValueError, TypeError):
        return False


def _extract_one(ctx: Any, runner: ToolRunner, att: dict, family: str, *, ocr: bool, extractor: Optional[str] = None,
                 force: bool = False) -> dict:
    """전체 PDF 이면서 같은 문서에 장별 PDF 가 있으면 보충 모드, 아니면 일반 모드."""
    m = ctx.manifest
    siblings = m.attachments_for(att["doc_id"])
    if att["kind"] == "full" and (att.get("detected_type") or "pdf") == "pdf" and not att.get("dedup_of"):
        chapters = [s for s in siblings if s["kind"] == "chapter" and s.get("dedup_of") is None and s["att_id"] != att["att_id"]]
        if chapters:
            # detected_type 은 내려받은 뒤에만 채워지므로, 대기 판정은 형식을 보지 않고 kind 만 본다
            if any(s["status"] == "discovered" for s in chapters):
                return {"outcome": "deferred_waiting_chapters"}      # 장별 PDF 를 아직 못 받았다: 받은 뒤에 보충 범위를 정한다
            covering = [s for s in chapters if s["status"] in stage.EXTRACTED_OR_LATER and s.get("raw_path")
                        and s.get("detected_type") == "pdf"]
            if covering:
                if any(c.get("raw_archived_to") and not _local_file(ctx, c) for c in covering):
                    return {"outcome": "skipped_archived"}       # 보충 범위를 정하려면 장별 PDF 를 읽어야 한다: 외부 보관 중이면 복원 뒤에 다시 한다(파생 행을 지우기 전에 멈춘다)
                return extract_attachment(ctx, runner, att, family, ocr=ocr, plan_sig=_plan_sig(covering),
                                          select_pages=plan_supplement(runner, ctx, {}, att, covering), extractor=extractor, force=force)
    return extract_attachment(ctx, runner, att, family, ocr=ocr, extractor=extractor, force=force)

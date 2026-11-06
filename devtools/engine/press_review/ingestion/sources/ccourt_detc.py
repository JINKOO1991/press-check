"""헌법재판소 판례정보 API(공공데이터포털, 공공누리 제1유형 출처표시) 보강 어댑터: ``python -m press_review.ingestion ccourt_detc --number 2005헌마165 [--numbers-file F]``.

법제처 헌재결정례 경로(``lawapi_detc``·``lawapi_detc_body``)에서 본문을 얻지 못한 문서·번호 해석이 안 된 번호를 헌법재판소 API 로 보강한다. 대상은 호출자가 준 번호뿐이다.
번호마다 목록(getKorPrcdntList, eventNo 절대 일치) → 사건일련번호(eventNum)마다 상세(getKorPrcdntDetail)를 받는다. 상세는 panreType 01(결정문)을 먼저, 없으면 03(판례집)이다
(목록의 panreType 은 라벨이라 상세 호출에는 코드로 넣는다. 02 공보는 쓰지 않는다).

쪽 구성: 상세 응답은 의견이 필드로 분리되어 오므로 detc_parse 의 전문 휴리스틱 분할을 쓰지 않고 필드를 쪽으로 옮긴다. ``reason`` 은 사건개요·판단·결론·기타의견을 모두 품은 합본이라
나뉜 필드가 있으면 쪽으로 만들지 않는다(법정의견으로 오인되는 반대의견 인용을 막는다). ``event``(사건/당사자)는 쪽으로 만들지 않고 병합 사건번호만 뽑는다.
  판시사항·결정요지·참조조문·참조판례·심판대상조문 → 머리 쪽(``lawapi_detc_body`` 와 같은 idx 0~4), textOfDecision → order, eventSummary·abjObt·judgement·conclusion → reasons,
  caseRelatedOpinion → party_opinion(opinion_kind party_submission: court_majority 불가), otherOpinions → opinion(표제로 나눠 분류, 못 나누면 opinion_kind None + needs_review).

중복 색인 방지: 번호가 lawgo_detc 의 사건 행(cases)이나 본문이 있는 문서의 body.case_nos 에 이미 있으면 요청 없이 건너뛴다. 상세를 받은 뒤 전체 사건번호(병합 포함)로 한 번 더 본다.
권리 게이트: 소스 등록 뒤 요청 전에 ``require_source_rights(read, store_raw, create_derivatives)``. 통과하지 못하면 error_class ``rights_not_recorded`` 로 중단한다.
서비스키는 클라이언트(``with_key=True``)가 주입한다. 이 모듈은 키를 읽지 않고, 오류 메시지·장부에 URL 을 싣지 않는다.
실패 장부: target ``ccourt_no:<번호>``, stage ``resolve``, error_class = not_found | no_body | already_covered | page_overflow | serial_mismatch | malformed_number | api_<코드> | http_*.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
import urllib.parse
from typing import Any, Optional

from ..http import FetchFailed, IngestionError
from ..rights import RightsNotRecorded, require_source_rights
from . import lawapi as L
from . import lawapi_detc as D
from .detc_parse import STATUS_REVIEW, STATUS_UNSPLIT, build_head_page, classify_orders, extract_merged_numbers, parse_detc_date, split_head_opinions

SOURCE_ID = "ccourt_detc"
FAMILY = "data_go_kr"
DOC_PREFIX = "ccourt_detc"
BASE = "https://apis.data.go.kr/9750000/PrecedentInfomationService"
LIST_ENDPOINT = BASE + "/getKorPrcdntList"
DETAIL_ENDPOINT = BASE + "/getKorPrcdntDetail"
RAW_DIR = "raw/data_go_kr"
TEXT_DIR = "text/ccourt_detc"
STATE_FILE = "ccourt_detc.json"
EXTRACTOR_VERSION = "ccourt-json-2"             # 쪽 구성 규칙을 바꾸면 올린다
LIST_ROWS = 50
PANRE_CODE = {"결정문": "01", "공보": "02", "판례집": "03"}
DETAIL_CODES = ("01", "03")                      # 결정문 먼저, 없으면 판례집
NODATA_CODES = ("3", "03")                       # 공공데이터포털 표준 NODATA_ERROR(상세에 해당 판례 유형이 없음)
AUTH_HTTP = (401, 403)
FAIL_PREFIX = "ccourt_no:"
FAIL_STAGE = "resolve"
BODY_STAGE = "body"
MAX_RETRIES = 3
NO_RETRY_CLASSES = ("malformed_number",)
SKIP_CLASSES = ("not_found", "already_covered")  # 다시 요청해도 달라지지 않는다: --retry-not-found 로만 재시도
HEAD_FIELDS = (("판시사항", "pansiMatt", "headnote_issue", 0), ("결정요지", "decisionGst", "headnote_holding", 1),
               ("참조조문", "refrnPrvsn", "ref_statutes", 2), ("참조판례", "refrnPrcdnt", "ref_cases", 3),
               ("심판대상조문", "adjobtTxt", "review_target", 4))
ORDER_IDX = 5
OPINION_FIRST_IDX = 11
HEAD_OPINION_FIRST_IDX = 500
MAJORITY = ("majority", "main")
FLAG_STATUSES = (STATUS_UNSPLIT, STATUS_REVIEW)
BODY_KINDS = ("order", "reasons", "party_opinion", "opinion")


class RightsStop(IngestionError):
    """소스 권리(read·store_raw·create_derivatives)가 allowed 로 기록되지 않았다. CLI 는 status=failed, error_class=rights_not_recorded, 종료 코드 1 로 낸다."""
    error_class = "rights_not_recorded"


# 색인에 들어갈 수 없는 법제처 문서(문서 격리·보류, 사건 격리, 인입 제외)는 보강을 막지 않는다. corpus_sqlite._iter_eligible 의 제외 기준과 같다.
_LAWGO_USABLE = ("d.status NOT IN ('quarantined','withheld') AND d.doc_id NOT IN (SELECT doc_id FROM cases WHERE verification_status IN ('quarantined','rejected') "
                 "OR case_id IN (SELECT target FROM failures WHERE stage='admission' AND error_class='case_excluded' AND resolved_at IS NULL))")


class AuthStop(IngestionError):
    """서비스키가 거부됐다(HTTP 403·reason 30 등). 남은 번호도 같은 결과이므로 즉시 전체를 멈춘다."""
    error_class = "auth_failed"


def open_source(manifest) -> None:
    """등록 → 권리 게이트. 소스 행이 없는 첫 실행에서도 소스가 먼저 등록된 뒤 중단된다."""
    manifest.ensure_source(SOURCE_ID, FAMILY, "헌법재판소 판례정보 API(공공데이터포털)", BASE,
                           notes="getKorPrcdntList/getKorPrcdntDetail; 공공누리 제1유형 출처표시. 권리는 `rights set data_go_kr --source ccourt_detc` 로 소스 한정 기록")
    try:
        require_source_rights(manifest, SOURCE_ID, needs=("read", "store_raw", "create_derivatives"))     # 같은 명령이 쪽(파생물)까지 만든다
    except RightsNotRecorded as e:
        raise RightsStop(str(e)) from None


# ---------------------------------------------------------------- URL·응답 파싱
def _enc(q: list) -> str:
    return urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def list_url(number: str) -> str:
    """파라미터 순서는 캐시 키라 고정. serviceKey 는 클라이언트가 주입한다(with_key=True)."""
    return LIST_ENDPOINT + "?" + _enc([("type", "json"), ("pageNo", "1"), ("numOfRows", str(LIST_ROWS)), ("eventNo", number)])


def detail_url(event_num: str, panre_code: str) -> str:
    return DETAIL_ENDPOINT + "?" + _enc([("type", "json"), ("eventNum", event_num), ("panreType", panre_code)])


def public_url(case_no: str) -> str:
    """이용자가 열어 볼 공개 주소. API 상세 주소는 서비스키 없이 열리지 않으므로 canonical_url 로 쓰지 않는다."""
    return "https://www.law.go.kr/" + urllib.parse.quote(f"헌재결정례/({case_no})", safe="/()")


def pick_item(items: list, event_num: str) -> Optional[dict]:
    """응답 항목 중 eventNum 이 정확히 일치하는 하나. 없거나 여럿이면 None(다른 결정을 쓰지 않는다). 수신·재개·재파싱이 함께 쓴다."""
    hit = [i for i in items if str(i.get("eventNum", "")).strip() == event_num]
    return hit[0] if len(hit) == 1 else None


def panre_code(label: Any) -> Optional[str]:
    """목록 응답의 panreType 라벨 → 상세 호출용 코드. 모르는 라벨은 None(라벨을 그대로 넘기면 resultCode 10)."""
    return PANRE_CODE.get(str(label or "").strip())


class ApiParseError(ValueError):
    pass


def parse_api(body: bytes) -> tuple[str, str, dict]:
    """(resultCode, resultMsg, body). 성공 코드는 "0". 게이트웨이 오류 봉투(OpenAPI_ServiceResponse)·헤더 없는 응답은 ApiParseError."""
    try:
        j = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiParseError("body is not JSON") from None
    if not isinstance(j, dict):
        raise ApiParseError("not a JSON object")
    if "OpenAPI_ServiceResponse" in j:
        raise ApiParseError("gateway error envelope")
    h = j.get("header")
    if not isinstance(h, dict) or "resultCode" not in h:
        raise ApiParseError("no header.resultCode")
    b = j.get("body")
    return str(h["resultCode"]).strip(), str(h.get("resultMsg") or ""), (b if isinstance(b, dict) else {})


def accept_api(body: bytes, headers: dict) -> Optional[str]:
    """오류 응답(resultCode != 0·게이트웨이 봉투·HTML)을 캐시에 쓰지 않으려는 검사. 문제 없으면 None."""
    try:
        code, _msg, _b = parse_api(body)
    except ApiParseError as e:
        return f"not an api response: {e}"
    return None if code == "0" else f"api_result:{code}"


def items_of(body: dict) -> list:
    """body.items.item 은 0건이면 items 가 null·빈 문자열이고 1건이면 dict, 여러 건이면 list 일 수 있다."""
    it = body.get("items")
    it = it.get("item") if isinstance(it, dict) else None
    out = [] if it is None else ([it] if isinstance(it, dict) else it)
    return [x for x in out if isinstance(x, dict)] if isinstance(out, list) else []


def auth_reason(r) -> Optional[str]:
    """게이트웨이 인증 오류(returnReasonCode, 예: 30 미등록 키)나 HTTP 401·403 이면 사유 문자열, 아니면 None. 응답 본문·URL 은 싣지 않는다."""
    try:
        j = json.loads(r.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, AttributeError):
        j = None
    env = j.get("OpenAPI_ServiceResponse") if isinstance(j, dict) else None
    hdr = env.get("cmmMsgHeader") if isinstance(env, dict) else None
    code = str(hdr.get("returnReasonCode") or "").strip() if isinstance(hdr, dict) else ""
    if code:
        return f"reason {re.sub(r'[^0-9A-Za-z_]', '', code)[:8]}"
    return f"http {r.status}" if r.status in AUTH_HTTP else None


def exact_items(number: str, items: list) -> list:
    """목록 항목 중 사건번호 토큰 집합에 ``number`` 가 있고 사건일련번호가 숫자인 것만(부분문자열 금지), 사건일련번호 중복 제거."""
    out, seen = [], set()
    for it in items:
        toks = L.split_case_numbers(unicodedata.normalize("NFKC", str(it.get("eventNo") or "")))
        num = str(it.get("eventNum", "")).strip()
        if number in toks and num.isdigit() and num not in seen:
            seen.add(num)
            out.append(it)
    return out


# ---------------------------------------------------------------- 추출(순수 함수: 입출력 없음)
_ENTITY = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


def _field(item: dict, key: str) -> str:
    """문자열 필드. 응답에 남은 HTML 엔티티(&#183; 등)는 세미콜론까지 갖춘 것만 푼다(원문의 '&' 는 그대로 둔다)."""
    v = item.get(key)
    if not (isinstance(v, str) and v.strip()):
        return ""
    return _ENTITY.sub(lambda m: html.unescape(m.group(0)), v.replace("\r\n", "\n").replace("\r", "\n"))


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def build_extraction(item: dict, list_item: dict) -> dict:
    """상세 item → 쪽 목록·본문 메타. 네트워크·파일을 쓰지 않는다."""
    f = {k: _field(item, k) for k in ("pansiMatt", "decisionGst", "refrnPrvsn", "refrnPrcdnt", "adjobtTxt", "textOfDecision", "eventSummary", "abjObt",
                                       "caseRelatedOpinion", "judgement", "conclusion", "otherOpinions", "reason", "event")}
    warnings: list = []
    printed = L._squash_ws(str(item.get("eventNo") or list_item.get("eventNo") or "")).strip()
    rep = L.split_case_numbers(unicodedata.normalize("NFKC", printed))
    merged = extract_merged_numbers("【당 사 자】\n" + f["event"] + "\n【주 문】") if f["event"] else []     # 당사자 정보는 쪽으로 만들지 않고 번호 토큰만 쓴다
    case_nos = list(dict.fromkeys(rep + merged))
    iso = parse_detc_date(item.get("rstaDate") or list_item.get("rstaDate"))
    pages: list = []
    head = build_head_page(printed or (case_nos[0] if case_nos else None), iso)
    if head:
        pages.append({"idx": -1, "loc": L.HEADER_LOC, "kind": "case_header", "field": "사건표지", "label": "사건표지", "opinion_kind": None,
                      "char_start": 0, "char_end": len(head), "text": head, "needs_review": False})
    hsecs, hinfo = split_head_opinions(f["decisionGst"])
    h_flag = hinfo["headnote_split"] in FLAG_STATUSES
    for name, api, kind, idx in HEAD_FIELDS:
        txt = f[api]
        if not txt:
            continue
        if kind != "headnote_holding" or not hsecs:
            pages.append({"idx": idx, "kind": kind, "field": name, "label": name, "opinion_kind": None, "char_start": 0, "char_end": len(txt),
                          "text": txt, "needs_review": bool(kind == "headnote_holding" and h_flag)})
            continue
        k = 0
        for sec in hsecs:
            if not sec.text.strip():
                continue
            is_op = sec.kind == "opinion"
            pages.append({"idx": HEAD_OPINION_FIRST_IDX + k if is_op else idx, "kind": "headnote_opinion" if is_op else kind, "field": name,
                          "label": sec.label or name, "opinion_kind": None if h_flag else sec.opinion_kind, "vote": sec.vote,
                          "char_start": sec.char_start, "char_end": sec.char_end, "text": sec.text, "needs_review": h_flag})
            k += 1 if is_op else 0
    # 판단 필드 안에 재판관별 의견이 있는 결정이 있다(기타의견 필드가 비어 있어도). 표제로 나눠 소수의견이 법정 이유 쪽에 남지 않게 한다.
    jsecs, jinfo = split_head_opinions(f["judgement"])
    j_status = jinfo["headnote_split"]
    j_ops = [s for s in jsecs if s.kind == "opinion" and s.text.strip()] if j_status == "split" else []
    j_review = j_status in (STATUS_REVIEW, STATUS_UNSPLIT)
    has_op = bool(f["otherOpinions"]) or bool(j_ops)
    maj = "majority" if has_op else "main"
    body_fields = (("주문", "textOfDecision", "order", None, ORDER_IDX), ("사건개요", "eventSummary", "reasons", maj, ORDER_IDX + 1),
                   ("심판대상", "abjObt", "reasons", maj, ORDER_IDX + 2), ("사건관계인의 의견", "caseRelatedOpinion", "party_opinion", "party_submission", ORDER_IDX + 3),
                   ("판단", "judgement", "reasons", maj, ORDER_IDX + 4), ("결론", "conclusion", "reasons", maj, ORDER_IDX + 5))
    for label, api, kind, op_kind, idx in body_fields:
        txt = f[api]
        if not txt:
            continue
        if api == "judgement" and j_ops:
            sec = next((s for s in jsecs if s.kind != "opinion" and s.text.strip()), None)     # 첫 의견 표제 앞부분만 이유 쪽으로 남는다
            if sec is not None:
                pages.append({"idx": idx, "kind": kind, "field": label, "label": label, "opinion_kind": op_kind, "char_start": sec.char_start,
                              "char_end": sec.char_end, "text": sec.text, "needs_review": False})
            continue
        j_flag = api == "judgement" and j_review           # 의견 언급은 있는데 나누지 못했다: 법정 이유로 표시하지 않는다
        pages.append({"idx": idx, "kind": kind, "field": label, "label": label, "opinion_kind": None if j_flag else op_kind, "char_start": 0, "char_end": len(txt),
                      "text": txt, "needs_review": j_flag})
    split_fields = ("eventSummary", "abjObt", "judgement", "conclusion")
    if f["reason"] and not any(f[k] for k in split_fields):
        # 나뉜 필드가 하나도 없으면 합본 이유만 있다: 반대의견이 섞였을 수 있어 법정의견으로 쓰지 못하게 표시한 채 한 쪽으로 둔다
        pages.append({"idx": ORDER_IDX + 4, "kind": "reasons", "field": "이유", "label": "이유(미분리)", "opinion_kind": None, "char_start": 0,
                      "char_end": len(f["reason"]), "text": f["reason"], "needs_review": True})
        warnings.append("reason_unseparated")
    reason_uncovered = None
    if f["reason"] and any(f[k] for k in split_fields):
        rest = _norm(f["reason"])
        for k in ("eventSummary", "abjObt", "caseRelatedOpinion", "judgement", "conclusion", "otherOpinions"):
            rest = rest.replace(_norm(f[k]), "", 1) if f[k] else rest
        reason_uncovered = len(rest)
    op_status, op_markers = ("split" if j_ops else "none"), []
    for j, sec in enumerate(j_ops):
        op_status = "needs_review" if sec.opinion_kind is None else op_status
        pages.append({"idx": OPINION_FIRST_IDX + j, "kind": "opinion", "field": "판단", "label": sec.label or "판단(의견)", "opinion_kind": sec.opinion_kind,
                      "vote": sec.vote, "char_start": sec.char_start, "char_end": sec.char_end, "text": sec.text, "needs_review": sec.opinion_kind is None})
        op_markers.append({"kind": sec.opinion_kind, "vote": sec.vote, "label": sec.label})
    if f["otherOpinions"]:
        osecs, oinfo = split_head_opinions(f["otherOpinions"])
        if oinfo["headnote_split"] == "split":
            op_status = "split" if op_status != "needs_review" else op_status
            for j, sec in enumerate((s for s in osecs if s.text.strip()), len(j_ops)):
                kind = sec.opinion_kind if sec.kind == "opinion" and sec.opinion_kind not in MAJORITY else None      # 법정의견 표기라도 이 필드는 기타의견이다
                flag = kind is None
                op_status = "needs_review" if flag else op_status
                pages.append({"idx": OPINION_FIRST_IDX + j, "kind": "opinion", "field": "기타의견", "label": sec.label or "기타의견(표제 앞)", "opinion_kind": kind,
                              "vote": sec.vote if kind else None, "char_start": sec.char_start, "char_end": sec.char_end, "text": sec.text, "needs_review": flag})
                op_markers.append({"kind": kind, "vote": sec.vote if kind else None, "label": sec.label})
        else:
            op_status = STATUS_REVIEW if oinfo["headnote_split"] == STATUS_REVIEW else STATUS_UNSPLIT
            pages.append({"idx": OPINION_FIRST_IDX + len(j_ops), "kind": "opinion", "field": "기타의견", "label": "기타의견", "opinion_kind": None, "char_start": 0,
                          "char_end": len(f["otherOpinions"]), "text": f["otherOpinions"], "needs_review": True})
            warnings.append("opinion_markers_not_found" if op_status == STATUS_UNSPLIT else "opinion_markers_inconsistent")
    if h_flag:
        warnings.append(f"headnote_opinions_{hinfo['headnote_split']}")
    if j_review:
        warnings.append(f"judgement_opinions_{j_status}")
    mism = []
    list_nos = L.split_case_numbers(unicodedata.normalize("NFKC", str(list_item.get("eventNo") or "")))
    if list_nos and not set(list_nos) <= set(case_nos):
        mism.append({"field": "case_no", "list": list_item.get("eventNo"), "body": printed})
    list_iso = parse_detc_date(list_item.get("rstaDate"))
    if list_iso and iso and list_iso != iso:
        mism.append({"field": "decision_date", "list": list_iso, "body": iso})
    return {"pages": pages, "body_case_no": printed or None, "case_nos": case_nos, "merged_numbers": merged, "decision_date": iso,
            "dispositions": classify_orders(f["textOfDecision"]), "opinion_status": op_status,
            "opinion_split": {"status": op_status, "marker_count": len(op_markers), "loose_count": 0, "conclusion_numbers": [], "markers": op_markers},
            "headnote_split": hinfo, "mismatches": mism, "warnings": warnings,
            "codes": {"case_kind_name": str(item.get("eventType") or list_item.get("eventType") or "") or None},
            "fields_chars": {k: len(v) for k, v in f.items() if k != "event"} | {"reason_uncovered": reason_uncovered},
            "text_available": any(p["kind"] in BODY_KINDS for p in pages)}


def persist_extraction(ctx, doc_id: str, event_num: str, raw_sha: str, raw_rel: str, fetched_at: str, built: dict, panre: str, list_code: Optional[str]) -> dict:
    """쪽 파일을 쓰고 파생 행을 지운 뒤 pages 를 다시 쓰고 단계 정보를 갱신한다(순서는 lawapi_detc_body.persist_extraction 과 같다)."""
    m = ctx.manifest
    rel_dir = f"{TEXT_DIR}/{event_num}"
    if not ctx.dry_run:
        keep = set()
        for p in built["pages"]:
            name = f"{L.page_loc(p)}.txt"
            keep.add(name)
            L._atomic_write(ctx.corpus_dir / rel_dir / name, p["text"].encode("utf-8"))
        d = ctx.corpus_dir / rel_dir
        if d.is_dir():
            for f in d.glob("[sh][0-9][0-9][0-9].txt"):
                if f.name not in keep:
                    f.unlink()
    body = {
        "fetched_at": fetched_at, "raw_path": raw_rel, "raw_sha256": raw_sha, "format": "json", "extractor_version": EXTRACTOR_VERSION,
        "text_available": built["text_available"], "pages": len(built["pages"]), "fields_chars": built["fields_chars"],
        "body_case_no": built["body_case_no"], "case_nos": built["case_nos"], "merged_numbers": built["merged_numbers"],
        "dispositions": built["dispositions"], "opinion_status": built["opinion_status"], "opinion_split": built["opinion_split"],
        "headnote_split": built["headnote_split"], "verify_mismatches": built["mismatches"], "warnings": built["warnings"],
        "panre_type": panre, "list_panre_code": list_code,
        "section_locator": "page_id suffix #sNNN (#h000 = case header; #s500+ = opinions inside 결정요지; #s011+ = 기타의견); quality_json.char_start/char_end = offsets in the field text",
    }
    with m.transaction():
        m.delete_derived(doc_id=doc_id)
        for p in built["pages"]:
            loc = L.page_loc(p)
            m.upsert_page(
                f"{doc_id}#{loc}", doc_id, method="html_section", text_path=f"{rel_dir}/{loc}.txt",
                text_sha256=hashlib.sha256(p["text"].encode("utf-8")).hexdigest(), char_count=len(p["text"]),
                quality_json={"locator": loc, "section_kind": p["kind"], "label": p["label"], "field": p["field"],
                              "opinion_kind": p["opinion_kind"], "vote": p.get("vote"), "char_start": p["char_start"], "char_end": p["char_end"],
                              "event_num": event_num},
                needs_review=p["needs_review"], extractor_version=EXTRACTOR_VERSION)
        L._set_extra(m, doc_id, "body", body)
        m.update_document_fields(doc_id, extra_merge={"codes": built["codes"]})
        m.update_document_stage(doc_id, extractor_version=EXTRACTOR_VERSION, extract_input_sha256=raw_sha)
        m.set_document_status(doc_id, "extracted")
        m.resolve_failures(doc_id, BODY_STAGE)
    return body


# ---------------------------------------------------------------- 실행
def add_arguments(parser) -> None:
    parser.add_argument("--number", action="append", dest="numbers", metavar="N",
                        help="사건번호(반복 가능). 예: python -m press_review.ingestion ccourt_detc --number 2005헌마165")
    parser.add_argument("--numbers-file", default=None, help="줄당 번호 하나(빈 줄·# 시작 줄 무시)")
    parser.add_argument("--reparse", action="store_true",
                        help="저장된 raw 로 쪽·extra·공개 링크를 다시 만든다(요청 0). raw 가 있고 sha 가 장부와 같은 문서만이며, 파생 행은 지워지고 문서는 extracted 로 돌아간다")
    parser.add_argument("--retry-not-found", action="store_true", help="이전에 not_found·already_covered 로 기록된 번호도 다시 처리한다(기본은 건너뜀)")


class _Run:
    def __init__(self, ctx, args):
        self.ctx, self.m = ctx, ctx.manifest
        self.retry_nf = bool(getattr(args, "retry_not_found", False))
        self.consecutive_failures = 0
        self._held: Optional[set] = None
        self.c = {"numbers_input": 0, "duplicates": 0, "malformed_number": 0, "skipped_done": 0, "skipped_covered": 0, "skipped_exhausted": 0,
                  "skipped_known": 0, "not_found": 0, "no_body": 0, "already_covered": 0, "page_overflow": 0, "serial_mismatch": 0, "failed": 0,
                  "docs_new": 0, "docs_seen": 0, "resumed_from_raw": 0, "multi_event_numbers": 0, "requests": 0, "pages_written": 0,
                  "reparsed": 0, "reparse_skipped": 0}

    # -- HTTP
    def bump(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= L.MAX_CONSECUTIVE_FAILURES:
            raise IngestionError(f"{L.MAX_CONSECUTIVE_FAILURES} consecutive request failures; stopping (unverified)")

    def call(self, url: str) -> tuple:
        """('ok', body_dict, raw_bytes) | ('code', 결과코드, None) | ('fail', error_class, None). 인증 오류는 AuthStop, 연속 실패 3회는 IngestionError."""
        ctx = self.ctx
        ctx.check_time()
        self.c["requests"] += 1
        try:
            r = ctx.http.get(url, with_key=True, accept=accept_api)
        except FetchFailed:
            r = None
        if r is not None:
            why = auth_reason(r)
            if why:
                raise AuthStop(f"data.go.kr rejected the service key ({why}); stopping all requests")
        if r is not None and r.ok:
            self.consecutive_failures = 0
            return "ok", parse_api(r.body)[2], r.body
        if r is not None and 200 <= r.status < 300 and (r.content_error or "").startswith("api_result:"):
            code = r.content_error.split(":", 1)[1]
            if code not in NODATA_CODES:
                self.bump()
            return "code", code, None
        self.bump()
        return "fail", ("http_fetch_failed" if r is None else (f"http_{r.status}" if not r.content_error else "http_content_rejected")), None

    # -- 장부
    def fail(self, number: str, error_class: str, message: str) -> None:
        self.m.record_failure(FAIL_PREFIX + number, FAIL_STAGE, error_class, message, retry_count=0 if error_class in NO_RETRY_CLASSES else None)
        self.c[error_class if error_class in self.c else "failed"] += 1

    def held_by_lawgo(self, tokens: list) -> Optional[str]:
        """lawgo_detc 로 색인될 수 있는 번호면 그 번호: 쓸 수 있는 문서의 사건 행(cases)이 있거나 본문이 있는 문서의 body.case_nos 에 있다."""
        m = self.m
        if self._held is None:
            self._held = set()
            for r in m._q(f"SELECT c.case_number AS n FROM cases c JOIN documents d ON d.doc_id=c.doc_id WHERE d.source_id='lawgo_detc' AND {_LAWGO_USABLE}"):
                self._held.update(L.split_case_numbers(unicodedata.normalize("NFKC", str(r["n"] or ""))))
        for t in tokens:
            if t in self._held:
                return t
            rows = m._q("SELECT d.doc_id AS id FROM documents d, json_each(d.extra_json,'$.body.case_nos') j WHERE d.source_id='lawgo_detc' AND j.value=? "
                        f"AND json_extract(d.extra_json,'$.body.text_available')=1 AND {_LAWGO_USABLE} LIMIT 1", (t,))
            if rows:
                return t
        return None

    # -- 목록
    def list_exact(self, number: str) -> tuple:
        """('ok', items) | ('fail', error_class) | ('overflow', None)."""
        kind, val, _raw = self.call(list_url(number))
        if kind != "ok":
            return "fail", (val if kind == "fail" else f"api_{val}")
        try:
            total = int(str(val.get("totalCount")).strip())
        except (TypeError, ValueError):
            total = 0
        if total > LIST_ROWS:
            return "overflow", None
        return "ok", exact_items(number, items_of(val))

    # -- 상세
    def fetch_detail(self, event_num: str) -> tuple:
        """('ok', 코드, raw, item) | ('fail', error_class, None, None). 01 에 항목이 없거나 NODATA 면 03 으로 내려간다. 전송 실패·다른 오류 코드는 내려가지 않는다."""
        seen_other = False
        for code in DETAIL_CODES:
            kind, val, raw = self.call(detail_url(event_num, code))
            if kind == "fail":
                return "fail", val, None, None
            if kind == "code":
                if val in NODATA_CODES:
                    continue
                return "fail", f"api_{val}", None, None
            items = items_of(val)
            hit = pick_item(items, event_num)
            if hit is not None:
                return "ok", code, raw, hit
            seen_other = seen_other or bool(items)
        return "fail", ("serial_mismatch" if seen_other else "no_body"), None, None

    def event(self, number: str, it: dict, multi: bool) -> bool:
        """사건일련번호 하나를 처리한다. 성공(이미 받은 문서 포함)이면 True, 실패를 장부에 남겼으면 False."""
        ctx, m = self.ctx, self.m
        event_num = str(it["eventNum"]).strip()
        doc_id = f"{DOC_PREFIX}:{event_num}"
        list_code = panre_code(it.get("panreType"))
        merge = {"resolved_numbers": [number], "resolved_via": {number: "ccourt_list"}, **({"multi_event": True} if multi else {})}
        doc = m.get_document(doc_id)
        if doc is not None and doc["status"] in L.DONE_STATUSES and not ctx.refresh:
            m.update_document_fields(doc_id, extra_merge=merge)
            self.c["docs_seen"] += 1
            return True
        prior = (L._doc_extra(doc).get("body") or {}) if doc is not None else {}
        raw_path = ctx.corpus_dir / prior["raw_path"] if prior.get("raw_path") else None
        if doc is not None and doc["status"] == "fetched" and raw_path is not None and raw_path.is_file() \
                and hashlib.sha256(raw_path.read_bytes()).hexdigest() == prior.get("raw_sha256"):
            raw, code, fetched_at = raw_path.read_bytes(), prior.get("panre_type") or "", prior.get("fetched_at") or ""
            item = pick_item(items_of(parse_api(raw)[2]), event_num)
            if item is None:
                self.fail(number, "serial_mismatch", "stored raw has no unique item for this eventNum")
                return False
            self.c["resumed_from_raw"] += 1
        else:
            kind, code, raw, item = self.fetch_detail(event_num)
            if kind == "fail":
                self.fail(number, code, "detail not usable")
                return False
            fetched_at = ctx.now().isoformat(timespec="seconds")
        built = build_extraction(item, it)
        held = self.held_by_lawgo(built["case_nos"])
        if held:
            self.fail(number, "already_covered", f"case number {held} already indexed from lawgo_detc (not registered)")
            return False
        sha = hashlib.sha256(raw).hexdigest()
        raw_rel = f"{RAW_DIR}/ccourt-{event_num}-{code}.json"
        iso = built["decision_date"]
        if not ctx.dry_run:
            L._atomic_write(ctx.corpus_dir / raw_rel, raw)
        with m.transaction():
            extra = {"event_num": event_num, "case_no": L._squash_ws(str(it.get("eventNo") or "")), "court": "헌법재판소", "raw_date": str(it.get("rstaDate") or ""), **merge}
            res = m.upsert_document(doc_id, SOURCE_ID, canonical_url=public_url(extra["case_no"]),
                                    title=L._squash_ws(str(item.get("eventNm") or "")) or None,
                                    decision_date=iso, year=int(iso[:4]) if iso else None, extra=extra)
            if res != "new":
                m.update_document_fields(doc_id, extra_merge=merge)
            L._set_extra(m, doc_id, "body", {**prior, "fetched_at": fetched_at, "raw_path": raw_rel, "raw_sha256": sha, "panre_type": code})
            m.set_document_status(doc_id, "fetched")
        body = persist_extraction(ctx, doc_id, event_num, sha, raw_rel, fetched_at, built, code, list_code)
        self.c["docs_new" if res == "new" else "docs_seen"] += 1
        self.c["pages_written"] += body["pages"]
        return True

    # -- 저장된 raw 로 다시 추출
    def reparse(self) -> None:
        """네트워크 없음. raw 가 없거나 sha 가 장부와 다르거나 항목이 없으면 건드리지 않고 reparse_skipped 로 센다.
        persist_extraction 이 쪽·chunks·cases 를 지우고 새로 쓰므로 구조화가 끝난 문서도 extracted 로 돌아간다(재구조화 필요)."""
        m, ctx = self.m, self.ctx
        where = f"source_id=? AND status IN ({','.join(repr(x) for x in ('fetched',) + L.DONE_STATUSES)})"
        for r in m._q(f"SELECT doc_id FROM documents WHERE {where} ORDER BY doc_id", (SOURCE_ID,)):
            ctx.check_time()
            doc = m.get_document(r["doc_id"])
            event_num = doc["doc_id"].split(":", 1)[1]
            extra = L._doc_extra(doc)
            body = extra.get("body") or {}
            p = ctx.corpus_dir / body["raw_path"] if body.get("raw_path") else None
            data = p.read_bytes() if p is not None and p.is_file() else None
            if data is None or hashlib.sha256(data).hexdigest() != body.get("raw_sha256"):
                self.c["reparse_skipped"] += 1
                continue
            item = pick_item(items_of(parse_api(data)[2]), event_num)
            if item is None:
                self.c["reparse_skipped"] += 1
                continue
            list_item = {"eventNo": extra.get("case_no"), "rstaDate": extra.get("raw_date"), "eventType": (extra.get("codes") or {}).get("case_kind_name")}
            built = build_extraction(item, list_item)
            m.update_document_fields(doc["doc_id"], canonical_url=public_url(extra.get("case_no") or built["body_case_no"]))
            new = persist_extraction(ctx, doc["doc_id"], event_num, body["raw_sha256"], body["raw_path"], body.get("fetched_at") or "", built,
                                     body.get("panre_type") or "", body.get("list_panre_code"))
            self.c["reparsed"] += 1
            self.c["pages_written"] += new["pages"]

    # -- 전체
    def run(self, raw_numbers: list) -> None:
        m = self.m
        done = {r["v"] for r in m._q("SELECT j.value AS v FROM documents d, json_each(d.extra_json,'$.resolved_numbers') j WHERE d.source_id=? AND d.status IN (%s)"
                                     % ",".join(repr(x) for x in L.DONE_STATUSES), (SOURCE_ID,))}
        fails = {f["target"]: f for f in m.failures() if f["stage"] == FAIL_STAGE and f["target"].startswith(FAIL_PREFIX)}
        todo, seen = [], set()
        for raw in raw_numbers:
            self.c["numbers_input"] += 1
            n = D.normalize_number(raw)
            if n is None:
                key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(raw)))[:60]
                if key not in seen:
                    seen.add(key)
                    self.fail(key, "malformed_number", "not a constitutional court case number (no automatic correction)")
                else:
                    self.c["duplicates"] += 1
                continue
            if n in seen:
                self.c["duplicates"] += 1
                continue
            seen.add(n)
            f = fails.get(FAIL_PREFIX + n)
            if n in done and f is None and not self.ctx.refresh:     # 미완료 표시가 남은 번호와 --refresh 는 다시 처리한다
                self.c["skipped_done"] += 1
            elif f and f["error_class"] in SKIP_CLASSES and not self.retry_nf:
                self.c["skipped_known"] += 1
            elif f and f["error_class"] not in NO_RETRY_CLASSES + SKIP_CLASSES and f["retry_count"] + 1 >= MAX_RETRIES:
                self.c["skipped_exhausted"] += 1
            else:
                todo.append(n)
        for n in todo:
            if self.held_by_lawgo([n]):                     # 이미 사건으로 색인된 번호는 요청하지 않는다
                self.c["skipped_covered"] += 1
                continue
            kind, val = self.list_exact(n)
            if kind == "fail":
                self.fail(n, val, "list not usable")
            elif kind == "overflow":
                self.fail(n, "page_overflow", f"totalCount exceeds {LIST_ROWS} rows (nothing registered)")
            elif not val:
                self.fail(n, "not_found", "no list item with this exact case number")
            else:
                multi = len(val) >= 2
                self.c["multi_event_numbers"] += int(multi)
                if multi:                # 중간에 멈춰도 다음 실행이 남은 결정을 회수하도록 미완료 표시를 먼저 남긴다
                    m.record_failure(FAIL_PREFIX + n, FAIL_STAGE, "multi_in_progress", "not all decisions of this number are done yet", retry_count=0)
                if all([self.event(n, it, multi) for it in val]):
                    m.resolve_failures(FAIL_PREFIX + n, FAIL_STAGE)


def run(ctx, args) -> dict:
    m = ctx.manifest
    open_source(m)
    do_reparse = bool(getattr(args, "reparse", False))
    numbers = [] if do_reparse else D.collect_numbers(args)
    r = _Run(ctx, args)
    try:
        if do_reparse:
            r.reparse()
        else:
            r.run(numbers)
    finally:
        m.touch_source_checked(SOURCE_ID)
        if not ctx.dry_run:
            L._write_terms_file(ctx.inventory_dir / STATE_FILE, {"updated_at": ctx.now().isoformat(timespec="seconds"), "counts": r.c})
    docs = {x["status"]: x["n"] for x in m.count_by("documents", "status", where={"source_id": SOURCE_ID})}
    return {**r.c, "documents_by_status": docs, "state_file": None if ctx.dry_run else str(ctx.inventory_dir / STATE_FILE)}

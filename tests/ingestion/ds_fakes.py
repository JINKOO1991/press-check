"""DeepSeek 제공자·구조화 시험용 가짜 transport와 장부 시드. 네트워크를 쓰지 않는다. 합성 텍스트이며 실제 사건이 아니다."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pdr_ing_paths  # noqa: F401
from press_review.ingestion.http import RawResponse

FRI_PEAK = datetime(2026, 10, 9, 7, 0, 0, tzinfo=timezone.utc)      # 금요일 07:00 UTC = 피크
SAT_OFF = datetime(2026, 10, 10, 7, 0, 0, tzinfo=timezone.utc)      # 토요일 = 오프피크
TEST_KEY = "dsk-TESTKEY-0123456789"

PAGE1 = ("사건 2001-조정-1\n"
         "신청인 A는 가상일보의 기사가 사실과 다르다며 정정보도를 청구하였다.\n"
         "기사 구절: 가상시 시장이 공사비를 빼돌렸다는 의혹이 제기됐다.\n")
PAGE2 = ("위원회는 양 당사자가  조정합의에 이르렀음을 확인한다.\n"
         "조정은 위법 여부를 판단하지 않는다.\n")


def raw(status, payload, headers=None, url="https://api.deepseek.com/chat/completions"):
    body = payload if isinstance(payload, (bytes, bytearray)) else (payload.encode("utf-8") if isinstance(payload, str)
                                                                    else json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return RawResponse(status=status, headers={k.lower(): v for k, v in (headers or {}).items()}, body=bytes(body), url=url)


def chat_payload(content, *, hit=0, miss=100, completion=50, finish="stop", model="deepseek-flash", reasoning=None):
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    usage = {"prompt_tokens": hit + miss, "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss,
             "completion_tokens": completion, "total_tokens": hit + miss + completion}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {"id": "x", "model": model, "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": text}}],
            "usage": usage}


class DSFake:
    """handler(body_dict, n) -> RawResponse | (status, payload, headers) | Exception. 호출 기록은 calls."""

    def __init__(self, handler=None, models=("deepseek-flash", "deepseek-v4-pro"), models_status=200):
        self.handler = handler
        self.models = models
        self.models_status = models_status
        self.calls = []
        self.bodies = []
        self.headers = []
        self.n_chat = 0

    def request(self, method, url, headers, timeout, max_bytes, body=None):
        assert url.startswith("https://api.deepseek.com"), url
        path = url.split("api.deepseek.com", 1)[1]
        self.calls.append((method, path))
        self.headers.append(headers)
        if path == "/models":
            if self.models_status != 200:
                return raw(self.models_status, {"error": {"message": "x"}}, url=url)
            return raw(200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "deepseek"} for m in self.models]}, url=url)
        assert path == "/chat/completions" and method == "POST"
        self.n_chat += 1
        b = json.loads(body)
        self.bodies.append(b)
        spec = self.handler(b, self.n_chat)
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, RawResponse):
            return spec
        status, payload, hdrs = spec
        return raw(status, payload, hdrs, url=url)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seed(manifest, corpus, pages=None, *, source_id="pac_adjustment_casebooks", family="pac_cases", doc_n=1, att_status="extracted",
         with_list=True, title="언론조정중재사례집"):
    """문서 1건·첨부 1건·쪽 N개. pages: {키: 텍스트}(순서 유지). 반환 (doc_id, att_id, {page_id: 텍스트})."""
    pages = pages if pages is not None else {"p1": PAGE1, "p2": PAGE2}
    manifest.ensure_source(source_id, family, title, "https://www.pac.or.kr/x")
    doc_id = f"{source_id}:{doc_n}"
    list_url = "https://www.pac.or.kr/list?p=1"
    if with_list:
        manifest.upsert_list_page(source_id, 1, list_url, http_status=200, item_count=1, body_sha256="a" * 64, ok=True)
    manifest.upsert_document(doc_id, source_id, title=title, list_url=list_url if with_list else None)
    att_id = f"{doc_id}#1"
    manifest.upsert_attachment(att_id, doc_id, "https://www.pac.or.kr/f.pdf", label="전체", kind="full")
    out = {}
    for i, (key, text) in enumerate(pages.items(), start=1):
        pid = f"{att_id}@p{i}"
        rel = Path("text") / source_id / f"{doc_n}_{i}.txt"
        path = Path(corpus) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        manifest.upsert_page(pid, doc_id, att_id=att_id, pdf_page_no=i, method="pdftotext", text_path=str(rel), text_sha256=sha(text),
                             char_count=len(text), quality=1.0)
        out[pid] = text
    manifest.set_attachment_status(att_id, att_status)
    manifest.set_document_status(doc_id, "extracted")
    return doc_id, att_id, out


def q(pid, text):
    return {"page_id": pid, "quote": text}


def good_case(pids, **over):
    """seed() 기본 쪽(PAGE1, PAGE2)에 맞는 정상 draft."""
    p1, p2 = pids
    c = {
        "case_number": "2001-조정-1", "decision_date": None, "publication_date": None, "institution": "pac",
        "institution_name": None, "authority_type": "pac_settlement", "decision_type": "조정합의",
        "parties": [{"role": "claimant", "label": "신청인 A"}, {"role": "media_outlet", "label": "가상일보"}],
        "issue_tags": ["정정보도"],
        "factual_background": [{"text": "신청인이 정정보도를 청구했다.", "speaker": "claimant",
                                "quote": q(p1, "신청인 A는 가상일보의 기사가 사실과 다르다며 정정보도를 청구하였다.")}],
        "article_claims": [], "disputed_spans": [{"text": "공사비 유용 의혹", "speaker": "article",
                                                  "quote": q(p1, "가상시 시장이 공사비를 빼돌렸다는 의혹이 제기됐다.")}],
        "claims_or_requested_remedy": [{"text": "정정보도 청구", "speaker": "claimant", "quote": q(p1, "정정보도를 청구하였다")}],
        "outcome_type": "settlement", "outcome_scope": "whole",
        "outcome": {"text": "양 당사자가 조정합의에 이르렀다.", "speaker": "institution",
                    "quote": q(p2, "위원회는 양 당사자가 조정합의에 이르렀음을 확인한다.")},
        "legal_reasoning": [{"text": "조정은 위법 여부를 판단하지 않는다.", "speaker": "institution",
                             "quote": q(p2, "조정은 위법 여부를 판단하지 않는다.")}],
        "legal_provisions": [], "law_versions": [], "settlement_or_adjudication": "settlement",
        "original_article": {"status": "quoted_in_source", "quote": q(p1, "가상시 시장이 공사비를 빼돌렸다는 의혹이 제기됐다.")},
        "corrected_article": {"status": "absent", "quote": None}, "correction_pair_confidence": "unverified_pair",
        "later_history": [], "first_page_id": p1, "starts_before_window": False, "continues_after_window": False,
    }
    c.update(over)
    return c

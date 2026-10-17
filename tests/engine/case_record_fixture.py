"""case record 시험용 합성 사례. 실제 사건·실제 원문이 아니다."""
import copy

PAGE_ID = "pac_adjustment_casebooks:1#1@p3"
PAGES = {
    PAGE_ID: "신청인 A는 가상일보의 기사가 사실과 다르다며 정정보도를 청구하였다.\n"
             "위원회는 양 당사자가  조정합의에 이르렀음을 확인한다.\n"
             "기사 구절: 가상시 시장이 공사비를 빼돌렸다는 의혹이 제기됐다.",
    "pac_adjustment_casebooks:1#1@p4": "조정은 위법 여부를 판단하지 않는다.",
}


def q(text, page_id=PAGE_ID):
    pages = PAGES[page_id]
    start = pages.index(text)
    return {"quote": text, "page_id": page_id, "start": start, "end": start + len(text)}


def base_record():
    return {
        "schema_version": "0.1", "case_id": "c-0001", "doc_id": "pac_adjustment_casebooks:1", "att_id": "pac_adjustment_casebooks:1#1",
        "institution": "pac", "institution_name": "가상언론중재위원회", "authority_type": "pac_settlement",
        "decision_type": "조정합의", "case_number": "2001-조정-1", "decision_date": "2001-05-04", "publication_date": None,
        "parties_redacted": [{"role": "claimant", "label": "신청인 A"}, {"role": "media_outlet", "label": "가상일보"}],
        "factual_background": [{"text": "신청인이 정정보도를 청구했다.", "speaker": "claimant",
                                "quote": q("신청인 A는 가상일보의 기사가 사실과 다르다며 정정보도를 청구하였다.")}],
        "article_claims": [], "disputed_spans": [{"text": "공사비 유용 의혹 보도", "speaker": "article",
                                                  "quote": q("가상시 시장이 공사비를 빼돌렸다는 의혹이 제기됐다.")}],
        "issue_tags": ["정정보도"],
        "claims_or_requested_remedy": [{"text": "정정보도 청구", "speaker": "claimant",
                                        "quote": q("정정보도를 청구하였다")}],
        "outcome_type": "settlement",
        "outcome": {"text": "양 당사자가 조정합의에 이르렀다.", "speaker": "institution",
                    "quote": q("양 당사자가  조정합의에 이르렀음을 확인한다.")},
        "outcome_scope": "whole", "legal_reasoning": [], "legal_provisions": [], "law_versions": [],
        "supporting_passage_ids": [PAGE_ID], "source_locators": [{"kind": "pdf_page", "value": "3"}],
        "settlement_or_adjudication": "settlement",
        "available_original_article": {"status": "quoted_in_source", "quote": q("가상시 시장이 공사비를 빼돌렸다는 의혹이 제기됐다.")},
        "available_corrected_article": {"status": "absent", "quote": None},
        "correction_pair_confidence": "unverified_pair",
        "appeal_or_later_history": [], "verified_by": [], "verification_status": "unverified",
        "quote_check": {"total": 5, "matched": 5, "dropped_fields": []},
        "structurer": {"version": "s1", "model": "deepseek-v4-flash", "input_sha256": "a" * 64},
    }


def mutated(fn):
    r = copy.deepcopy(base_record())
    fn(r)
    return r

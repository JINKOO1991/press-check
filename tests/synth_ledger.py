"""합성 수집 장부: P3 색인 테스트용. 실제 장부·사용자 데이터 디렉터리에 닿지 않고 임시 폴더에 만든다.

텍스트는 모두 가상의 합성 문장이다(실제 판결·결정의 원문이 아니다). 사건번호와 결정일도 시험용이다.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "plugins" / "press-dispute-review" / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from press_review.ingestion.manifest import Manifest  # noqa: E402

TAIL_J1 = "맨 끝에만 나오는 유일한 합성 문장입니다."      # 화면 인용 상한(300자) 밖에만 있다
LONG = ("이 문장은 색인 시험을 위해 만든 합성 문장이며 실제 판결의 내용이 아니다. " * 12).strip()     # 300자를 넘는 꼬리


def _rights(**kw):
    base = {"read": "allowed", "store_raw": "allowed", "create_derivatives": "allowed", "external_api": "allowed",
            "redistribute_raw": "permission_required", "redistribute_derivatives": "permission_required", "fine_tune": "permission_required"}
    base.update(kw)
    return base


# (chunk_id, 설명) -> 아래 build_ledger 가 만든다. 기대값은 테스트에서 이 id 로 참조한다.
J1 = "lawgo_prec:900001:2018다10001#c1"       # 2019-03-14, 정정보도 요건, 언론중재법 제14조, 다수의견만
J2 = "lawgo_prec:900002:2021다20002,20003#c1"   # 2022-05-26, 병합 사건번호, 청구 배척(dismissal_merits), 다수+반대의견 혼재
J3 = "lawgo_prec:900003:2023도30003#c1"         # 2024-11-07, 형사
J4 = "lawgo_prec:900004:2020다40004#c1"         # 2020-08-20, 재배포 허락(문서 단위 allowed) -> 300자 제한 없음
P1 = "pac_cases:5001#1:2025서울조정101·102/103#c1"   # 결정일 없음(날짜 없는 passage), 조정 합의
I1 = "iendc:7001#1:w0000n00#c1"                 # 2022-05-31 인터넷선거보도심의
R1 = "pac_research_quarterly:9001#1:p0012#pc"   # case 없는 연구 chunk(page 단위)
EXCLUDED = {"case_quarantined": 1, "case_rejected": 1, "doc_quarantined": 1, "no_valid_url": 1}


def build_ledger(corpus_dir: Path) -> Path:
    """``corpus_dir/manifest.sqlite`` 를 만든다. 반환: 장부 경로."""
    corpus_dir.mkdir(parents=True, exist_ok=True)
    path = corpus_dir / "manifest.sqlite"
    m = Manifest(path)
    try:
        m.ensure_source("lawgo_prec_supreme", "law_go_kr", "합성 판례")
        m.ensure_source("pac_adjustment_casebooks", "pac_cases", "합성 조정 사례집")
        m.ensure_source("iendc_review_results", "iendc", "합성 심의결과")
        m.ensure_source("pac_research_quarterly", "pac_other", "합성 연구 계간지")

        def doc(doc_id, source, *, canonical="https://www.law.go.kr/LSW/precInfoP.do?precSeq=%s", detail=None, status="structured", title=""):
            m.upsert_document(doc_id, source, canonical_url=canonical, detail_url=detail, title=title)
            m.set_document_status(doc_id, status)

        def case(case_id, doc_id, record, *, att_id=None, first_page=None, status="unverified"):
            rec = {"case_id": case_id, "doc_id": doc_id, "issue_tags": [], "outcome": None, "legal_reasoning": [], **record}
            m.upsert_case(case_id, doc_id, rec, att_id=att_id, first_page_id=first_page, verification_status=status)

        # ---- 판례
        doc("lawgo_prec:900001", "lawgo_prec_supreme", canonical="https://www.law.go.kr/LSW/precInfoP.do?precSeq=900001", title="합성 판결 1")
        case("lawgo_prec:900001:2018다10001", "lawgo_prec:900001", {
            "institution": "supreme_court", "case_number": "2018다10001", "decision_date": "2019-03-14", "authority_type": "judgment",
            "outcome_type": "judgment", "issue_tags": ["정정보도", "명예훼손", "정정보도 요건"],
            "legal_reasoning": [{"text": "x", "speaker": "court_majority"}], "outcome": {"text": "원심판결을 파기한다", "speaker": "court_majority"}})
        m.upsert_chunk(J1, "lawgo_prec:900001", "정정보도 청구는 언론중재법 제14조에 따라 사실적 주장에 관한 보도가 진실하지 않을 때 허용된다. "
                       "민법 제750조의 손해배상과 구별한다. " + LONG + " " + TAIL_J1,
                       "text_range", "lawgo_prec:900001#s006:0-300", case_id="lawgo_prec:900001:2018다10001",
                       authority_type="judgment", decision_date="2019-03-14")

        doc("lawgo_prec:900002", "lawgo_prec_supreme", canonical="https://www.law.go.kr/LSW/precInfoP.do?precSeq=900002", title="합성 판결 2")
        case("lawgo_prec:900002:2021다20002,20003", "lawgo_prec:900002", {
            "institution": "supreme_court", "case_number": "2021다20002, 20003", "decision_date": "2022-05-26", "authority_type": "judgment",
            "outcome_type": "dismissal_merits", "issue_tags": ["명예훼손", "상당한 이유", "공적 인물"],
            "legal_reasoning": [{"text": "a", "speaker": "court_majority"}, {"text": "b", "speaker": "court_dissenting"}],
            "outcome": {"text": "상고를 기각한다", "speaker": "court_majority"}}, status="verified")
        m.upsert_chunk(J2, "lawgo_prec:900002", "공적 인물에 대한 비판은 진실이라고 믿을 상당한 이유가 있으면 청구를 기각한다. 반대의견은 다르게 본다.",
                       "text_range", "lawgo_prec:900002#s006:0-90", case_id="lawgo_prec:900002:2021다20002,20003",
                       authority_type="judgment", decision_date="2022-05-26")

        doc("lawgo_prec:900003", "lawgo_prec_supreme", canonical="https://www.law.go.kr/LSW/precInfoP.do?precSeq=900003", title="합성 판결 3")
        case("lawgo_prec:900003:2023도30003", "lawgo_prec:900003", {
            "institution": "supreme_court", "case_number": "2023도30003", "decision_date": "2024-11-07", "authority_type": "judgment",
            "outcome_type": "judgment", "issue_tags": ["출판물에 의한 명예훼손"], "legal_reasoning": [{"text": "a", "speaker": "court_majority"}],
        "legal_provisions": [{"citation": "형법 제310조", "quote": {"quote": "q", "page_id": "p", "start": 0, "end": 1}}]})
        m.upsert_chunk(J3, "lawgo_prec:900003", "형법 제309조 출판물에 의한 명예훼손에서 비방할 목적을 따로 본다.",
                       "text_range", "lawgo_prec:900003#s006:0-60", case_id="lawgo_prec:900003:2023도30003",
                       authority_type="judgment", decision_date="2024-11-07")

        doc("lawgo_prec:900004", "lawgo_prec_supreme", canonical="https://www.law.go.kr/LSW/precInfoP.do?precSeq=900004", title="합성 판결 4")
        m.set_rights("document", "lawgo_prec:900004", _rights(redistribute_raw="allowed", redistribute_derivatives="allowed"),
                     [{"scope": "합성 시험: 문서 단위 재배포 허락", "review_status": "verified", "url": "https://www.law.go.kr/"}], "verified")
        case("lawgo_prec:900004:2020다40004", "lawgo_prec:900004", {
            "institution": "supreme_court", "case_number": "2020다40004", "decision_date": "2020-08-20", "authority_type": "judgment",
            "outcome_type": "judgment", "issue_tags": ["반론보도"], "legal_reasoning": [{"text": "a", "speaker": "court_majority"}]})
        m.upsert_chunk(J4, "lawgo_prec:900004", "반론보도 청구는 진실 여부와 무관하다. " + LONG, "text_range", "lawgo_prec:900004#s006:0-300",
                       case_id="lawgo_prec:900004:2020다40004", authority_type="judgment", decision_date="2020-08-20")

        # ---- PAC 조정(날짜 없음, pdf_page 위치 -> 첨부 URL)
        doc("pac_cases:5001", "pac_adjustment_casebooks", canonical="https://www.pac.or.kr/kor/pages/?p=207&idx=5001&m=view", title="합성 사례집")
        m.upsert_attachment("pac_cases:5001#1", "pac_cases:5001", "https://www.pac.or.kr/_common/new_download_file.asp?sub_idx=5001", kind="full")
        m.upsert_page("pac_cases:5001#1:p0003", "pac_cases:5001", att_id="pac_cases:5001#1", pdf_page_no=3, method="pdftotext")
        case("pac_cases:5001#1:2025서울조정101·102/103", "pac_cases:5001", {
            "institution": "pac", "case_number": "2025서울조정101·102/103", "decision_date": None, "authority_type": "pac_settlement",
            "outcome_type": "settlement", "issue_tags": ["정정보도", "초상권 침해"]}, att_id="pac_cases:5001#1", first_page="pac_cases:5001#1:p0003")
        m.upsert_chunk(P1, "pac_cases:5001", "프로 경기장 관중 사진에 엉뚱한 사람의 초상이 실려 정정보도를 게재하기로 조정이 성립한 합성 사례. 기자는 사진 설명을 바로잡았다.",
                       "pdf_page", "3-4", case_id="pac_cases:5001#1:2025서울조정101·102/103", authority_type="pac_settlement", decision_date=None)

        # ---- IENDC
        doc("iendc:7001", "iendc_review_results", canonical="https://www.iendc.go.kr/home/kor/board.do?act=detail&idx=7001", title="합성 심의결과")
        m.upsert_attachment("iendc:7001#1", "iendc:7001", "https://www.iendc.go.kr/fileDownload.do?filename=a.hwp", kind="full")
        m.upsert_page("iendc:7001#1:p0001", "iendc:7001", att_id="iendc:7001#1", pdf_page_no=1, method="kordoc")
        case("iendc:7001#1:w0000n00", "iendc:7001", {
            "institution": "iendc", "case_number": None, "decision_date": "2022-05-31", "authority_type": "iendc_review",
            "outcome_type": "decision", "issue_tags": ["선거보도 공정성"]}, att_id="iendc:7001#1", first_page="iendc:7001#1:p0001")
        m.upsert_chunk(I1, "iendc:7001", "선거 여론조사 보도에서 조사 방법을 밝히지 않은 인터넷 기사에 주의 조치를 한 합성 사례.", "pdf_page", "1",
                       case_id="iendc:7001#1:w0000n00", authority_type="iendc_review", decision_date="2022-05-31")

        # ---- 연구 chunk(case 없음)
        doc("pac_research_quarterly:9001", "pac_research_quarterly", canonical="https://www.pac.or.kr/kor/pages/?p=300&idx=9001&m=view", title="합성 계간지")
        m.upsert_attachment("pac_research_quarterly:9001#1", "pac_research_quarterly:9001", "https://www.pac.or.kr/_common/new_download_file.asp?sub_idx=9001", kind="full")
        m.upsert_page("pac_research_quarterly:9001#1:p0012", "pac_research_quarterly:9001", att_id="pac_research_quarterly:9001#1", pdf_page_no=12, method="pdftotext")
        m.upsert_chunk(R1, "pac_research_quarterly:9001", "학술 논문의 의견: 추후보도 제도의 운용 현황과 입법 과제를 논한 합성 글이며 현행 법리가 아니다.", "pdf_page", "12",
                       page_id="pac_research_quarterly:9001#1:p0012", authority_type="research", decision_date=None)

        # ---- 제외 대상 4종
        doc("lawgo_prec:900101", "lawgo_prec_supreme", canonical="https://www.law.go.kr/LSW/precInfoP.do?precSeq=900101")
        case("lawgo_prec:900101:2020다50001", "lawgo_prec:900101", {"institution": "supreme_court", "case_number": "2020다50001",
             "decision_date": "2020-01-01", "authority_type": "judgment", "outcome_type": "judgment"}, status="quarantined")
        m.upsert_chunk("lawgo_prec:900101:2020다50001#c1", "lawgo_prec:900101", "격리된 사건의 합성 문장", "text_range", "a:0-5",
                       case_id="lawgo_prec:900101:2020다50001", authority_type="judgment", decision_date="2020-01-01")

        doc("lawgo_prec:900102", "lawgo_prec_supreme", canonical="https://www.law.go.kr/LSW/precInfoP.do?precSeq=900102")
        case("lawgo_prec:900102:2020다50002", "lawgo_prec:900102", {"institution": "supreme_court", "case_number": "2020다50002",
             "decision_date": "2020-01-02", "authority_type": "judgment", "outcome_type": "judgment"}, status="rejected")
        m.upsert_chunk("lawgo_prec:900102:2020다50002#c1", "lawgo_prec:900102", "반려된 사건의 합성 문장", "text_range", "a:0-5",
                       case_id="lawgo_prec:900102:2020다50002", authority_type="judgment", decision_date="2020-01-02")

        doc("lawgo_prec:900103", "lawgo_prec_supreme", canonical="https://www.law.go.kr/LSW/precInfoP.do?precSeq=900103", status="quarantined")
        case("lawgo_prec:900103:2020다50003", "lawgo_prec:900103", {"institution": "supreme_court", "case_number": "2020다50003",
             "decision_date": "2020-01-03", "authority_type": "judgment", "outcome_type": "judgment"})
        m.upsert_chunk("lawgo_prec:900103:2020다50003#c1", "lawgo_prec:900103", "격리된 문서의 합성 문장", "text_range", "a:0-5",
                       case_id="lawgo_prec:900103:2020다50003", authority_type="judgment", decision_date="2020-01-03")

        # 정본 URL 이 없고 DRF 주소(OC 인증키 파라미터)만 있는 문서: URL 을 쓰지 않으므로 제외된다
        doc("lawgo_prec:900104", "lawgo_prec_supreme", canonical=None,
            detail="https://www.law.go.kr/DRF/lawService.do?OC=fakekey&target=prec&ID=900104&type=HTML")
        case("lawgo_prec:900104:2020다50004", "lawgo_prec:900104", {"institution": "supreme_court", "case_number": "2020다50004",
             "decision_date": "2020-01-04", "authority_type": "judgment", "outcome_type": "judgment"})
        m.upsert_chunk("lawgo_prec:900104:2020다50004#c1", "lawgo_prec:900104", "URL 이 없는 문서의 합성 문장", "text_range", "a:0-5",
                       case_id="lawgo_prec:900104:2020다50004", authority_type="judgment", decision_date="2020-01-04")
    finally:
        m.close()
    return path

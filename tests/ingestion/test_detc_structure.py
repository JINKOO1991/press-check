"""헌재결정례 구조화 프로필 constitutional_decision(U3): 프로필 선택·장부 값·의견 가드·처분 가드·주문 없는 창·chunk·후보 게이트.

네트워크·DeepSeek 호출 없음(가짜 transport). 모든 fixture 는 합성이다('A법 제1조'·'갑 재판관'); 실제 결정문의 문장은 넣지 않는다(공개 저장소, 재배포는 permission_required).
쪽 메타(quality_json·needs_review)는 어댑터 ``lawapi_detc_body.build_extraction``·``persist_extraction`` 이 쓴 그대로 쓴다."""
import json
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest

import pdr_ing_paths  # noqa: F401
from ds_fakes import FRI_PEAK, TEST_KEY, DSFake, chat_payload, q
from press_review import corpus_sqlite as cs
from press_review.ingestion import structure as S
from press_review.ingestion import structure_records as sr
from press_review.ingestion.sources import lawapi as L, lawapi_detc as D, lawapi_detc_body as B
from press_review.ingestion.sources.detc_parse import parse_detc_xml, split_head_opinions
from press_review.providers import deepseek as ds

ORDER = "1. A법 제1조는 헌법에 위반되지 아니한다.\n2. 나머지 청구를 각하한다."
OUT_Q = "1. A법 제1조는 헌법에 위반되지 아니한다."
DIS = "합성 반대 이유이다."
TAIL = f"6. 재판관 가나다의 반대의견\n{DIS}\n"
HEAD_MENTION = "재판관 가나다는 반대의견을 냈다."          # 표제가 아닌 언급: 결정요지 분할이 unsplit 이 된다


def full_text(order=ORDER, tail=TAIL):
    return f"【당 사 자】\n청구인 갑 대리인 변호사 을\n2020헌마1\n【주　　문】\n{order}\n【이  유】\n1. 사건\n다수 판단이다.\n{tail}"


def detc_xml(serial, full, case_no="2020헌마1", holding="나 요지"):
    f = {"헌재결정례일련번호": str(serial), "종국일자": "20200102", "사건번호": case_no, "사건명": "A법 제1조 위헌확인", "사건종류코드": "430201",
         "사건종류명": "헌마", "재판부구분코드": "430301", "판시사항": "가 쟁점", "결정요지": holding, "참조조문": "A법 제1조", "참조판례": "",
         "심판대상조문": "A법 제1조", "전문": full}
    return "<DetcService>" + "".join(f"<{k}>{escape(v)}</{k}>" for k, v in f.items()) + "</DetcService>"


def seed_detc(m, corpus, serial=1, full=None, holding="나 요지"):
    """어댑터와 같은 build_extraction·persist_extraction 으로 문서·쪽·extra.body 를 만든다."""
    m.ensure_source(D.SOURCE_ID, "law_go_kr", "헌재결정례", "https://www.law.go.kr/")
    doc_id = f"lawgo_detc:{serial}"
    m.upsert_document(doc_id, D.SOURCE_ID, canonical_url=D.PUBLIC_VIEW + str(serial), title="합성", decision_date="2020-01-02", year=2020,
                      extra={"serial": str(serial), "case_no": "2020헌마1", "court": "헌법재판소"})
    parsed = parse_detc_xml(detc_xml(serial, full_text() if full is None else full, holding=holding), str(serial))
    doc = m.get_document(doc_id)
    built = B.build_extraction(parsed, doc, L._doc_extra(doc))
    B.persist_extraction(SimpleNamespace(manifest=m, corpus_dir=corpus, dry_run=False), doc, str(serial), "0" * 64, f"raw/law_go_kr/detc-{serial}.xml",
                         "2026-10-19T00:00:00Z", built)
    return doc_id


def pid_of(m, doc_id, section, opinion=None):
    for p in m.pages_of(doc_id=doc_id):
        qj = json.loads(p["quality_json"])
        if qj["section_kind"] == section and (opinion is None or qj["opinion_kind"] == opinion):
            return p["page_id"]
    raise AssertionError(f"no page {section}/{opinion}")


def run_structurer(m, corpus, handler, **skw):
    fake = DSFake(handler)
    prov = ds.DeepSeekProvider(api_key=TEST_KEY, transport=fake, sleep=lambda s: None, now=lambda: FRI_PEAK, budget=ds.BudgetGuard(None), concurrency=1)
    st = S.Structurer(m, prov, corpus, **skw)
    return st.run(S.find_targets(m, source_ids=[D.SOURCE_ID])), fake


def reply(case):
    return lambda b, n: (200, chat_payload({"cases": [case]}), {})


def base_case(m, doc_id, **over):
    c = {"case_number": "2020헌마1", "decision_date": None, "institution": "constitutional_court", "authority_type": "judgment",
         "decision_type": "합헌, 각하", "outcome_type": "judgment", "outcome_scope": "whole", "settlement_or_adjudication": "adjudication",
         "first_page_id": pid_of(m, doc_id, "case_header"),
         "outcome": {"text": "합헌, 각하", "speaker": "court_majority", "quote": q(pid_of(m, doc_id, "order"), OUT_Q)},
         "legal_reasoning": [{"text": "다수 판단", "speaker": "court_majority", "quote": q(pid_of(m, doc_id, "reasons"), "다수 판단이다.")}]}
    c.update(over)
    return c


# ------------------------------------------------------------------ 프로필 선택·프롬프트·판례 불변
def test_profile_selection_and_court_judgment_prompt_unchanged():
    assert sr.SOURCE_PROFILE["lawgo_detc"] == "constitutional_decision" and sr.SOURCE_PROFILE["lawgo_prec_supreme"] == "court_judgment"
    p = sr.PROFILES["constitutional_decision"]
    assert (p.prompt, p.institutions, p.authority_types, p.detc_guards) == ("constitutional_decision.md", ("constitutional_court",), ("judgment",), True)
    cj = sr.PROFILES["court_judgment"]
    assert cj.detc_guards is False and cj.institutions == sr.COURT_INSTITUTIONS
    ps = S.load_prompt(cj)           # 판례 1,685건의 작업 키: 이 값이 바뀌면 전부 다시 구조화 대상이 된다(2026-10-19 기준선)
    assert (ps.version, ps.sha8) == ("common@2026-10-09.3+court_judgment@2026-10-09.3", "fa89a78f")
    assert S.structurer_version(cj, ps, "deepseek-v4-flash", "disabled", 20000, 600) == \
        "s3|common@2026-10-09.3+court_judgment@2026-10-09.3.fa89a78f|deepseek-v4-flash|think=disabled|w20000s600"
    new = S.load_prompt(p)
    assert new.version.startswith("common@") and "constitutional_decision@2026-10-19.1" in new.version and new.sha8 != ps.sha8
    assert "## Source profile" in new.system and "constitutional_court" in new.system


# ------------------------------------------------------------------ 후보 게이트
def test_candidate_gate_detc_passes_when_text_available_and_prec_unchanged():
    def doc(sid, extra):
        return {"source_id": sid, "extra_json": json.dumps(extra)}
    body_ok = {"text_available": True, "prefilter": {"passed": False}}
    assert S._structure_candidate_doc(doc("lawgo_detc", {"body": {"text_available": True}})) is True       # 사전 필터 없이 통과
    assert S._structure_candidate_doc(doc("lawgo_detc", {"body": {"text_available": False}})) is False
    assert S._structure_candidate_doc(doc("lawgo_detc", {})) is False
    # 판례: 기존 집합과 같다(P-T1 통과, P-T3 + 사전 필터 미통과 제외, refs 의 사전 필터 통과 포함)
    assert S._structure_candidate_doc(doc("lawgo_prec_supreme", {"processing_tier": "P-T1", "body": body_ok})) is True
    assert S._structure_candidate_doc(doc("lawgo_prec_supreme", {"processing_tier": "P-T3", "body": body_ok})) is False
    assert S._structure_candidate_doc(doc("lawgo_prec_refs", {"processing_tier": "P-T3", "body": {"text_available": True, "prefilter": {"passed": True}}})) is True
    assert S._structure_candidate_doc(doc("lawgo_prec_supreme", {"processing_tier": "P-T1", "body": {"prefilter": {"passed": True}}})) is False   # 본문 없음
    assert S._structure_candidate_doc(doc("pac_adjustment_casebooks", {})) is True


def test_find_targets_takes_detc_document_without_prefilter(manifest, corpus):
    doc_id = seed_detc(manifest, corpus)
    assert [t.doc_id for t in S.find_targets(manifest, source_ids=[D.SOURCE_ID])] == [doc_id]
    assert S.find_targets(manifest, source_ids=[D.SOURCE_ID])[0].profile == "constitutional_decision"


# ------------------------------------------------------------------ 장부 값
def test_ledger_values_detc_fields_and_prec_output_unchanged(manifest, corpus):
    doc_id = seed_detc(manifest, corpus)
    plain = S.ledger_values(manifest, doc_id)                      # 판례 프로필이 부르는 형태: 새 키가 없다
    assert set(plain) == {"case_number", "case_number_tokens", "decision_date"} and plain["case_number"] == "2020헌마1"
    led = S.ledger_values(manifest, doc_id, detc=True)
    assert {k: v for k, v in led.items() if k in plain} == plain
    assert led["institution"] == "constitutional_court" and led["case_id_number"] == "2020헌마1"
    assert led["dispositions"] == ["합헌", "각하"] and led["opinion_status"] == "split" and led["headnote_status"] == "none"   # headnote_split 은 {"headnote_split": 상태, …} 로 저장된다
    assert S.LEDGER_PROFILES == ("court_judgment", "constitutional_decision")


# ------------------------------------------------------------------ 종단: 한 창
def test_end_to_end_institution_case_id_opinion_guard_and_chunk_keeps_dissent(manifest, corpus):
    doc_id = seed_detc(manifest, corpus)
    rs, dis = pid_of(manifest, doc_id, "reasons"), pid_of(manifest, doc_id, "opinion", "dissent")
    c = base_case(manifest, doc_id, institution="supreme_court", case_number="9999헌마9")
    c["legal_reasoning"] += [{"text": "반대를 다수로 오표기", "speaker": "court_majority", "quote": q(dis, DIS)},
                             {"text": "반대의견", "speaker": "court_dissenting", "quote": q(dis, DIS)}]
    res, fake = run_structurer(manifest, corpus, reply(c))
    assert res["cases"] == 1 and res["cases_quarantined"] == 0
    case = manifest.get_case("lawgo_detc:1:2020헌마1")                     # lawgo_detc:<일련번호>:<사건번호 compact>
    rec = case["record"]
    assert rec["institution"] == "constitutional_court" and rec["case_number"] == "2020헌마1" and rec["decision_type"] == "합헌, 각하"
    assert [x["speaker"] for x in rec["legal_reasoning"]] == ["court_majority", "court_dissenting"]       # 반대 구간의 court_majority 항목만 제거
    assert any("opinion_guard:opinion_kind_dissent" in f for f in rec["quote_check"]["dropped_fields"])
    assert rec["verification_status"] == "unverified" and rec["outcome_type"] == "judgment" and rec["outcome"]["speaker"] == "court_majority"
    ch = manifest.select("SELECT * FROM chunks WHERE case_id=?", (rec["case_id"],))[0]
    assert DIS in ch["text"] and "다수 판단이다." in ch["text"] and OUT_Q in ch["text"]                    # 반대의견 인용도 chunk 에 포함
    kinds = cs.opinion_kinds_of(rec)
    assert "dissenting" in kinds and "majority" in kinds               # 검색 결과의 opinion_kinds·includes_dissent("dissenting" in kinds)의 입력
    assert rs != dis


def test_dissent_only_in_quote_without_majority_label_stays_dissenting_in_chunk(manifest, corpus):
    doc_id = seed_detc(manifest, corpus)
    dis = pid_of(manifest, doc_id, "opinion", "dissent")
    c = base_case(manifest, doc_id)
    c["legal_reasoning"] = [{"text": "반대의견", "speaker": "court_dissenting", "quote": q(dis, DIS)}]
    run_structurer(manifest, corpus, reply(c))
    rec = manifest.get_case("lawgo_detc:1:2020헌마1")["record"]
    assert set(cs.opinion_kinds_of(rec)) == {"dissenting", "majority"}


# ------------------------------------------------------------------ 종단: 처분 가드(격리는 창 병합 뒤에도 남는다)
def test_disposition_contradiction_quarantines_case(manifest, corpus):
    doc_id = seed_detc(manifest, corpus)
    run_structurer(manifest, corpus, reply(base_case(manifest, doc_id, decision_type="위헌, 각하")))
    case = manifest.get_case("lawgo_detc:1:2020헌마1")
    assert case["verification_status"] == "quarantined"
    assert manifest.select("SELECT * FROM chunks WHERE case_id=?", (case["case_id"],)) == []
    asm = json.loads((S.target_dir(corpus, doc_id) / "assembled.json").read_text(encoding="utf-8"))
    assert any(e.startswith("label_guard: decision_type") for e in asm["cases_detail"][0]["errors"])


# ------------------------------------------------------------------ 종단: 다중 창(주문은 첫 창, 반대의견은 후속 창)
def test_multi_window_orderless_window_drops_outcome_and_decision_type_only(manifest, corpus):
    filler = "채움 문장이다. " * 150
    doc_id = seed_detc(manifest, corpus, full=full_text(tail=f"6. 재판관 가나다의 반대의견\n{DIS}\n{filler}\n"))
    order_pid, rs, dis = pid_of(manifest, doc_id, "order"), pid_of(manifest, doc_id, "reasons"), pid_of(manifest, doc_id, "opinion", "dissent")
    hdr = pid_of(manifest, doc_id, "case_header")

    first = base_case(manifest, doc_id)                     # 모델 응답은 작업 스레드에서 만들어지므로 장부 조회는 미리 한다
    later = base_case(manifest, doc_id, decision_type="위헌", first_page_id=dis, starts_before_window=True,
                      outcome={"text": "반대의견 결론", "speaker": "court_majority", "quote": q(dis, DIS)},
                      legal_reasoning=[{"text": "반대의견", "speaker": "court_dissenting", "quote": q(dis, DIS)},
                                       {"text": "반대를 다수로 오표기", "speaker": "court_majority", "quote": q(dis, DIS)}])

    def handler(b, n):       # 주문 쪽이 들어 있는 창은 첫 응답, 주문이 없는 후속 창은 모델이 반대의견 결론을 outcome·decision_type 으로 냈다고 가정한 응답
        c = first if f"<<PAGE {order_pid}>>" in b["messages"][1]["content"] else later
        return 200, chat_payload({"cases": [c]}), {}

    res, fake = run_structurer(manifest, corpus, handler, window_chars=500, snap_chars=100)
    assert fake.n_chat >= 2 and res["cases"] == 1 and res["cases_quarantined"] == 0                 # 사건 전체 격리 아님
    rec = manifest.get_case("lawgo_detc:1:2020헌마1")["record"]
    assert rec["verification_status"] == "unverified" and rec["decision_type"] == "합헌, 각하"
    assert rec["outcome"]["quote"]["page_id"] == order_pid and rec["outcome_type"] == "judgment"      # 첫 창의 주문 결론이 남는다
    assert sorted(x["speaker"] for x in rec["legal_reasoning"]) == ["court_dissenting", "court_majority"]
    assert [x["speaker"] for x in rec["legal_reasoning"] if x["quote"]["page_id"] == dis] == ["court_dissenting"]
    drops = rec["quote_check"]["dropped_fields"]
    assert any("no_order_in_window" in f for f in drops) and any("opinion_guard" in f for f in drops)
    assert rs and hdr


# ------------------------------------------------------------------ 단위: 의견 가드·결정요지·주문 있는 창의 outcome
def row(section, opinion=None, needs_review=0):
    return {"quality_json": json.dumps({"section_kind": section, "opinion_kind": opinion}), "needs_review": needs_review, "pdf_page_no": None}


QUOTE = "합성 인용 문장이다."


def assemble(profile, rows, texts, draft, ledger):
    ctx = sr.WindowCtx(profile=sr.PROFILES[profile], doc_id="lawgo_detc:1", att_id=None, target_key="lawgo_detc:1", unit="w0000",
                       pages={p: sr.PageText(t) for p, t in texts.items()}, page_meta={p: dict(page_id=p, **r) for p, r in rows.items()},
                       structurer={"version": "v", "model": "m", "input_sha256": "0" * 64}, ledger=ledger, source_id="lawgo_detc")
    return sr.assemble_case(draft, ctx, 0)


def draft(page, speaker, **over):
    d = {"case_number": "2020헌마1", "institution": "constitutional_court", "authority_type": "judgment", "outcome_type": "unknown", "first_page_id": page,
         "legal_reasoning": [{"text": "t", "speaker": speaker, "quote": q(page, QUOTE)},
                             {"text": "u", "speaker": "court_dissenting", "quote": q("p_dis", "다른 반대 문장이다.")}]}
    d.update(over)
    return d


TXT = {"p": QUOTE + "\n", "p_dis": "다른 반대 문장이다.\n2020헌마1\n"}
ROWS_BASE = {"p_dis": row("opinion", "dissent")}
LEDGER = {"case_number": "2020헌마1", "case_number_tokens": ["2020헌마1"], "institution": "constitutional_court", "dispositions": ["합헌", "각하"]}


@pytest.mark.parametrize("section,opinion,needs_review,removed", [
    ("opinion", "dissent", 0, True), ("opinion", "partial_dissent", 0, True), ("opinion", "concurrence", 0, True),
    ("opinion", "majority_supplement", 0, True), ("opinion", "dissent_supplement", 0, True), ("opinion", "vote_opinion", 0, True),
    ("opinion", "supplement", 0, True), ("headnote_opinion", "dissent", 0, True),
    ("reasons", "majority", 0, False), ("reasons", "main", 0, False), ("holding", "majority", 0, False), ("headnote_holding", "main", 0, False),
    ("reasons", "main", 1, True),                      # 분할하지 못해 needs_review 인 의견 포함 구간
    ("headnote_holding", None, 1, True),               # 결정요지 unsplit
    ("order", None, 0, False), ("case_header", None, 0, False), ("headnote_issue", None, 0, False), ("ref_statutes", None, 0, False)])
def test_opinion_guard_by_section_and_opinion_kind(section, opinion, needs_review, removed):
    rows = dict(ROWS_BASE, p=row(section, opinion, needs_review))
    co = assemble("constitutional_decision", rows, TXT, draft("p", "court_majority"), LEDGER)
    sp = [x["speaker"] for x in co.record["legal_reasoning"]]
    assert sp == (["court_dissenting"] if removed else ["court_majority", "court_dissenting"])
    assert co.record["verification_status"] == "unverified"                    # 항목만 버리고 사건은 격리하지 않는다
    assert any("opinion_guard" in f for f in co.record["quote_check"]["dropped_fields"]) is removed
    assert co.record["quote_check"]["total"] == co.record["quote_check"]["matched"]    # 인용 실패율에 넣지 않는다
    # 판례 프로필은 같은 입력에서 아무것도 제거하지 않는다(출력 불변)
    cj = assemble("court_judgment", rows, TXT, draft("p", "court_majority", institution="supreme_court"), {})
    assert [x["speaker"] for x in cj.record["legal_reasoning"]] == ["court_majority", "court_dissenting"]
    assert not any("opinion_guard" in f for f in cj.record["quote_check"]["dropped_fields"])


def test_headnote_unsplit_in_ledger_bars_court_majority_even_if_page_lacks_flag():
    rows = dict(ROWS_BASE, p=row("headnote_holding", "main", 0))
    for status, removed in (("unsplit", True), ("needs_review", True), ("split", False), ("none", False)):
        co = assemble("constitutional_decision", rows, TXT, draft("p", "court_majority"), dict(LEDGER, headnote_status=status))
        assert ([x["speaker"] for x in co.record["legal_reasoning"]] == ["court_dissenting"]) is removed, status


def test_real_unsplit_headnote_page_is_flagged_by_the_adapter_and_guarded(manifest, corpus):
    assert split_head_opinions(HEAD_MENTION)[1]["headnote_split"] == "unsplit"                       # 전제: 어댑터 분할기의 판정
    doc_id = seed_detc(manifest, corpus, holding=HEAD_MENTION)
    hp = pid_of(manifest, doc_id, "headnote_holding")
    assert S.ledger_values(manifest, doc_id, detc=True)["headnote_status"] == "unsplit"
    c = base_case(manifest, doc_id)
    c["legal_reasoning"].append({"text": "결정요지", "speaker": "court_majority", "quote": q(hp, "재판관 가나다는 반대의견을 냈다.")})
    run_structurer(manifest, corpus, reply(c))
    rec = manifest.get_case("lawgo_detc:1:2020헌마1")["record"]
    assert [x["quote"]["page_id"] for x in rec["legal_reasoning"]] == [pid_of(manifest, doc_id, "reasons")]       # 결정요지 쪽 인용 제거, 이유 쪽 유지
    assert rec["verification_status"] == "unverified"


def test_outcome_in_orderless_window_is_dropped_but_in_order_window_on_dissent_page_quarantines():
    rows = {"p": row("reasons", "majority"), "p_dis": row("opinion", "dissent")}
    out_on_dis = {"text": "반대 결론", "speaker": "court_majority", "quote": q("p_dis", "다른 반대 문장이다.")}
    d = draft("p", "court_majority", outcome=out_on_dis, outcome_type="judgment", outcome_scope="whole", decision_type="위헌")
    co = assemble("constitutional_decision", rows, TXT, d, LEDGER)                      # 주문 쪽이 없는 창
    rec = co.record
    assert rec["outcome"] is None and rec["outcome_type"] == "unknown" and rec["decision_type"] is None and rec["verification_status"] == "unverified"
    assert any("no_order_in_window" in f for f in rec["quote_check"]["dropped_fields"])
    rows2 = dict(rows, p_ord=row("order"))
    co2 = assemble("constitutional_decision", rows2, dict(TXT, p_ord="주문 문장이다.\n"), d, dict(LEDGER, dispositions=["위헌"]))
    assert co2.record["verification_status"] == "quarantined" and co2.record["outcome"] is not None
    assert any(e.startswith("label_guard: outcome court_majority") for e in co2.errors)


# ------------------------------------------------------------------ 단위: 처분 가드
@pytest.mark.parametrize("decision_type,dispositions,quarantined,note", [
    ("합헌, 각하", ["합헌", "각하"], False, None),
    ("각하", ["합헌", "각하"], False, None),                               # 일부만 적은 것은 모순이 아니다
    ("합헌결정", ["합헌"], False, None),
    ("위헌, 각하", ["합헌", "각하"], True, None),                          # 모순 -> 격리
    ("위헌", ["위헌불선언"], True, None),
    ("헌법불합치 (적용중지)", ["헌법불합치"], False, None),                 # 괄호 주석은 무시
    ("기타 결정", ["합헌", "각하"], False, "disposition_unmapped"),         # 대응 불가는 note 만
    ("위헌", ["unknown", "unknown"], False, None),                       # 분류기 결과가 전부 unknown 이면 가드 생략
    ("위헌", [], False, None),
    ("위헌", ["합헌", "unknown"], False, "disposition_not_in_classified_items"),   # 일부만 unknown: 그 항목일 수 있어 note 만
])
def test_disposition_guard(decision_type, dispositions, quarantined, note):
    rows = {"p": row("reasons", "majority"), "p_dis": row("opinion", "dissent"), "p_ord": row("order")}
    d = draft("p", "court_majority", decision_type=decision_type)
    co = assemble("constitutional_decision", rows, dict(TXT, p_ord="주문 문장이다.\n"), d, dict(LEDGER, dispositions=dispositions))
    assert (co.record["verification_status"] == "quarantined") is quarantined
    if quarantined:
        assert any(e.startswith("label_guard: decision_type") for e in co.errors)
    if note:
        assert any(note in f for f in co.record["quote_check"]["dropped_fields"])
    # 판례 프로필은 같은 decision_type 으로 격리되지 않는다
    cj = assemble("court_judgment", rows, dict(TXT, p_ord="주문 문장이다.\n"), dict(d, institution="supreme_court"), {})
    assert not any("decision_type" in e for e in cj.errors)


def test_map_decision_type_uses_the_classifier_vocabulary():
    from press_review.ingestion.sources.detc_parse import _classify_item
    for label in sr.DETC_DISPOSITIONS:
        assert sr.map_decision_type(label) == ({label}, [])
    assert sr.map_decision_type("위헌, 각하") == ({"위헌", "각하"}, [])
    assert sr.map_decision_type("한정위헌 및 기각") == ({"한정위헌", "기각"}, [])
    assert sr.map_decision_type("일부 인용")[1] == ["일부"]
    assert _classify_item("법률은 헌법에 위반된다고 할 수 없다.") == "위헌불선언"           # 어휘표는 classify_orders 와 같은 이름


@pytest.mark.parametrize("decision_type,dispositions,quarantined", [
    ("위헌", ["인용"], False),          # 장부는 '위헌임을 확인한다'를 인용으로 분류한다: 같은 주문을 모델이 위헌으로 적을 수 있다
    ("인용", ["위헌"], False),
    ("합헌", ["인용"], True),           # 동치군 밖은 엄격 유지
])
def test_disposition_guard_treats_unconstitutional_and_granted_as_equivalent(decision_type, dispositions, quarantined):
    rows = {"p": row("reasons", "majority"), "p_dis": row("opinion", "dissent"), "p_ord": row("order")}
    co = assemble("constitutional_decision", rows, dict(TXT, p_ord="주문 문장이다.\n"), draft("p", "court_majority", decision_type=decision_type),
                  dict(LEDGER, dispositions=dispositions))
    assert (co.record["verification_status"] == "quarantined") is quarantined

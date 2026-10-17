"""G-D31: 판례 처리 순위는 §8 적중분과 그 직접 인용(깊이 1)까지만 P-T1. 깊이 2 이상은 P-T2 와 같고 사전 필터 통과분만 구조화한다."""
from press_review.ingestion.sources import lawapi as L


def _seed(manifest, specs):
    manifest.ensure_source(L.SOURCE_ID, L.FAMILY, "법제처 판례", "https://www.law.go.kr/")
    for n, extra in specs:
        manifest.upsert_document(f"lawgo_prec:{n}", L.SOURCE_ID, title=f"판례{n}", extra=extra)
    return L.assign_tiers(manifest)


def _ptier(manifest, n):
    return L._doc_extra(manifest.get_document(f"lawgo_prec:{n}")).get("processing_tier")


def _body(passed):
    return {"text_available": True, "prefilter": {"passed": passed}}


def test_gd31_rank_by_depth_0_1_2(manifest):
    res = _seed(manifest, [
        (1, {"matched_terms": ["정정보도"]}),                                                  # 깊이 0: §8 적중
        (2, {"matched_terms": [], "expanded_from": ["lawgo_prec:1"], "ref_depth": 1}),        # 깊이 1
        (3, {"matched_terms": [], "expanded_from": ["lawgo_prec:2"], "ref_depth": 2}),        # 깊이 2
        (4, {"matched_terms": [], "expanded_from": ["lawgo_prec:3"], "ref_depth": 3}),        # 깊이 3
    ])
    assert [_ptier(manifest, n) for n in (1, 2, 3, 4)] == ["P-T1", "P-T1", "P-T2", "P-T2"]
    assert res["processing_tier_counts"] == {"P-T1": 2, "P-T2": 2}


def test_gd31_depth2_keeps_p_t1_when_it_hits_the_section8_rule(manifest):
    _seed(manifest, [(1, {"matched_terms": ["정정보도"], "expanded_from": ["lawgo_prec:9"], "ref_depth": 2})])
    assert _ptier(manifest, 1) == "P-T1"


def test_gd31_depth2_passing_prefilter_is_structure_candidate(manifest):
    _seed(manifest, [(3, {"matched_terms": [], "expanded_from": ["lawgo_prec:2"], "ref_depth": 2, "body": _body(True)})])
    extra = L._doc_extra(manifest.get_document("lawgo_prec:3"))
    assert extra["processing_tier"] == "P-T2" and L.is_structure_candidate(extra)


def test_gd31_depth2_failing_prefilter_is_not_structure_candidate(manifest):
    _seed(manifest, [
        (3, {"matched_terms": [], "expanded_from": ["lawgo_prec:2"], "ref_depth": 2, "body": _body(False)}),
        (2, {"matched_terms": [], "expanded_from": ["lawgo_prec:1"], "ref_depth": 1, "body": _body(False)}),
    ])
    assert not L.is_structure_candidate(L._doc_extra(manifest.get_document("lawgo_prec:3")))
    assert L.is_structure_candidate(L._doc_extra(manifest.get_document("lawgo_prec:2")))      # 깊이 1 은 필터와 무관하게 구조화


def test_gd31_mark_expanded_applies_depth_rule(manifest):
    _seed(manifest, [(1, {"matched_terms": []}), (2, {"matched_terms": []})])
    run = L._BodyRun.__new__(L._BodyRun)
    run.m = manifest
    run._mark_expanded("lawgo_prec:1", "lawgo_prec:0", 1)
    run._mark_expanded("lawgo_prec:2", "lawgo_prec:1", 2)
    assert (_ptier(manifest, 1), _ptier(manifest, 2)) == ("P-T1", "P-T2")
    run._mark_expanded("lawgo_prec:2", "lawgo_prec:0", 1)                                       # 더 얕은 인용이 생기면 깊이 1 로 올라간다
    assert _ptier(manifest, 2) == "P-T1"


def test_gd31_filter_version_bumped():
    assert L.FILTER_VERSION == "prefilter-3"


def test_find_targets_filters_precedents_to_structure_candidates():
    """구조화 대상 선정이 판례에 is_structure_candidate(P-T1 또는 사전 필터 통과)를 적용한다. 다른 소스는 제한하지 않는다."""
    import json as _json
    from press_review.ingestion import structure as S
    body_ok = {"text_available": True, "prefilter": {"passed": False}}
    t1 = {"source_id": "lawgo_prec_supreme", "extra_json": _json.dumps({"processing_tier": "P-T1", "body": body_ok})}
    t3 = {"source_id": "lawgo_prec_supreme", "extra_json": _json.dumps({"processing_tier": "P-T3", "body": body_ok})}
    t3_pass = {"source_id": "lawgo_prec_refs", "extra_json": _json.dumps({"processing_tier": "P-T3",
               "body": {"text_available": True, "prefilter": {"passed": True}}})}
    pac = {"source_id": "pac_adjustment_casebooks", "extra_json": "{}"}
    assert S._structure_candidate_doc(t1) is True
    assert S._structure_candidate_doc(t3) is False
    assert S._structure_candidate_doc(t3_pass) is True
    assert S._structure_candidate_doc(pac) is True


def test_gd32_list_and_index_chapters_are_chunk_only():
    from press_review.ingestion import structure_records as sr
    for label in ("제3부 시정권고 전체목록", "제3부 시정권고 전체 목록", "제4부 언론조정·중재사건 전체 목록", "언론중재·조정신청사례 색인"):
        assert sr.LIST_LABEL_RE.search(label), label
    for label in ("제2부 주요 시정권고 사례", "- 제1장 정정보도 게재 사례", "2005년도 연차보고서 (전체 PDF)"):
        assert not sr.LIST_LABEL_RE.search(label), label
    assert set(sr.LIST_CHUNK_AUTHORITY) == {"pac_correction_casebooks", "pac_adjustment_casebooks"}

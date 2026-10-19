"""소스 한정 권리 기록(`rights set --source`)과 어댑터 권리 게이트 `require_source_rights`, 헌재결정례 coverage 분기."""
import io
import json

import pytest

from press_review.ingestion import coverage, rights as R
from press_review.ingestion.__main__ import main

NEEDS = {"read": "allowed", "store_raw": "allowed"}


def _seed(m):
    m.ensure_source("lawgo_prec_supreme", "law_go_kr", "판례", "https://www.law.go.kr/")
    m.ensure_source("lawgo_detc", "law_go_kr", "헌재결정례", "https://www.law.go.kr/")


def _run(corpus, *argv):
    buf = io.StringIO()
    code = main(list(argv), stdout=buf, corpus=corpus)
    return code, json.loads(buf.getvalue())


def test_source_scoped_record_leaves_other_rows(manifest, real_default_rights):
    _seed(manifest)
    before = manifest.get_rights("source", "lawgo_prec_supreme")
    got = R.record_installer_decision(manifest, "law_go_kr", NEEDS, "사용자 결정 2026-10-17", "tester", source_ids=["lawgo_detc"])
    assert got == ["lawgo_detc"]
    d = manifest.get_rights("source", "lawgo_detc")
    assert d["read"] == "allowed" and d["store_raw"] == "allowed" and "tester" in d["rights_basis"][-1]["scope"]
    assert manifest.get_rights("source", "lawgo_prec_supreme") == before


def test_without_source_ids_applies_to_whole_family(manifest, real_default_rights):
    _seed(manifest)
    got = R.record_installer_decision(manifest, "law_go_kr", NEEDS, "x", "t")
    assert got == ["lawgo_detc", "lawgo_prec_supreme"]


def test_unknown_source_id_is_an_error_and_writes_nothing(manifest, real_default_rights):
    _seed(manifest)
    with pytest.raises(LookupError):
        R.record_installer_decision(manifest, "law_go_kr", NEEDS, "x", "t", source_ids=["lawgo_detc", "nope"])
    assert manifest.get_rights("source", "lawgo_detc")["read"] == "unknown"
    ensure = manifest  # family 불일치 소스도 오류
    ensure.ensure_source("pac_x", "pac_cases", "x", "https://www.pac.or.kr/")
    with pytest.raises(LookupError):
        R.record_installer_decision(manifest, "law_go_kr", NEEDS, "x", "t", source_ids=["pac_x"])


def test_cli_source_option_repeatable(corpus, manifest, real_default_rights):
    _seed(manifest)
    code, res = _run(corpus, "rights", "set", "law_go_kr", "--source", "lawgo_detc", "--read", "allowed", "--note", "n", "--by", "t")
    assert code == 0 and res["updated_sources"] == ["lawgo_detc"]
    assert manifest.get_rights("source", "lawgo_prec_supreme")["read"] == "unknown"
    code, res = _run(corpus, "rights", "set", "law_go_kr", "--source", "lawgo_detc", "--source", "zzz", "--read", "allowed", "--note", "n")
    assert code == 2 and res["status"] == "refused"


@pytest.mark.parametrize("value", ["unknown", "permission_required"])
def test_require_source_rights_blocks_until_recorded(manifest, real_default_rights, value):
    _seed(manifest)
    if value != "unknown":
        R.record_installer_decision(manifest, "law_go_kr", {"read": "allowed", "store_raw": value}, "x", "t", source_ids=["lawgo_detc"])
    with pytest.raises(R.RightsNotRecorded) as e:
        R.require_source_rights(manifest, "lawgo_detc")
    assert e.value.error_class == "rights_not_recorded"


def test_require_source_rights_passes_after_scoped_record_and_prec_unaffected(manifest, real_default_rights):
    _seed(manifest)
    R.record_installer_decision(manifest, "law_go_kr", NEEDS, "x", "t", source_ids=["lawgo_detc"])
    assert R.require_source_rights(manifest, "lawgo_detc")["read"] == "allowed"
    with pytest.raises(R.RightsNotRecorded):
        R.require_source_rights(manifest, "lawgo_prec_supreme")
    with pytest.raises(R.RightsNotRecorded):
        R.require_source_rights(manifest, "no_such_source")
    with pytest.raises(R.RightsNotRecorded):   # needs 에 allowed 가 아닌 항목이 있으면 중단
        R.require_source_rights(manifest, "lawgo_detc", needs=("read", "redistribute_raw"))


def test_coverage_law_summary_only_on_prec_sources(manifest, tmp_path):
    _seed(manifest)
    inv = tmp_path / "inv"
    inv.mkdir()
    (inv / "lawgo_prec_terms.json").write_text(json.dumps({"terms": {"a": {"status": "complete"}}, "probe_all": {"all": 5}, "seeds": []}), encoding="utf-8")
    cov = coverage.build_coverage(manifest, "t", inv)
    s = {x["source_id"]: x for x in cov["sources"]}
    assert s["lawgo_prec_supreme"]["law_cross_check"]["terms"] == 1
    assert s["lawgo_detc"]["law_cross_check"] == {"status": "not_comparable", "reason": s["lawgo_detc"]["law_cross_check"]["reason"]}
    md = coverage.render_markdown(cov)
    assert "대조 불가" in md

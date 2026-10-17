"""공개본의 권리 기본값은 unknown 이고, 허용은 설치자의 `rights set` 기록으로만 열린다."""
import io
import json

from press_review.ingestion import structure as S
from press_review.ingestion.__main__ import main


def _run(corpus, *argv):
    buf = io.StringIO()
    code = main(list(argv), stdout=buf, corpus=corpus)
    return code, json.loads(buf.getvalue())


def _seed(m):
    m.ensure_source("pac_casebooks", "pac_cases", "사례집", "https://www.pac.or.kr/kor/pages/?p=207")
    m.upsert_document("pac_cases:1", "pac_casebooks", title="가")


def test_new_install_refuses_egress_until_the_installer_records_a_decision(corpus, manifest, real_default_rights):
    _seed(manifest)
    assert S.egress_reason(manifest, "pac_cases:1") == "external_api_unknown"
    code, res = _run(corpus, "rights", "set", "pac_cases", "--external-api", "allowed", "--note", "기관 회신 확인", "--by", "tester")
    assert code == 0 and res["updated_sources"] == ["pac_casebooks"]
    assert S.egress_reason(manifest, "pac_cases:1") is None
    r = manifest.get_rights("source", "pac_casebooks")
    assert r["external_api"] == "allowed" and r["read"] == "unknown"        # 지정하지 않은 항목은 unknown 그대로
    assert "tester" in r["rights_basis"][-1]["scope"] and "기관 회신 확인" in r["rights_basis"][-1]["scope"] and r["rights_basis"][-1]["checked_at"]


def test_existing_ledger_values_are_kept_by_ensure_source(manifest, real_default_rights):
    manifest.ensure_source("pac_casebooks", "pac_cases", "사례집")
    r = manifest.get_rights("source", "pac_casebooks")
    manifest.set_rights("source", "pac_casebooks", {**{k: r[k] for k in r if k in ("read", "store_raw", "create_derivatives", "external_api",
                                                    "redistribute_raw", "redistribute_derivatives", "fine_tune")}, "read": "allowed"},
                        [], "pending")
    manifest.ensure_source("pac_casebooks", "pac_cases", "사례집 개정")        # 재실행: 기존 행은 건드리지 않는다
    assert manifest.get_rights("source", "pac_casebooks")["read"] == "allowed"


def test_rights_set_requires_a_note_and_never_loosens_forbidden(corpus, manifest, real_default_rights):
    _seed(manifest)
    code, res = _run(corpus, "rights", "set", "pac_cases", "--external-api", "allowed")
    assert code == 2 and res["status"] == "refused"
    manifest.set_rights("source", "pac_casebooks", {k: "forbidden" for k in ("read", "store_raw", "create_derivatives", "external_api",
                        "redistribute_raw", "redistribute_derivatives", "fine_tune")}, [], "verified")
    code, res = _run(corpus, "rights", "set", "pac_cases", "--external-api", "allowed", "--note", "x")
    assert code == 2 and manifest.get_rights("source", "pac_casebooks")["external_api"] == "forbidden"


def test_rights_show_lists_values_and_basis(corpus, manifest, real_default_rights):
    _seed(manifest)
    _run(corpus, "rights", "set", "pac_cases", "--read", "allowed", "--note", "근거", "--by", "tester")
    code, res = _run(corpus, "rights", "show")
    src = {s["source_id"]: s for s in res["sources"]}["pac_casebooks"]
    assert code == 0 and src["rights"]["read"] == "allowed" and src["rights_basis"][-1]["scope"].endswith("근거")

"""출시 전 보강(H1~H7): 뜻 반전 수정안 제외, 검색 응답의 연락처 가림, 고지 문구, 서버 고정 부인 문구, User-Agent, 요청 창구, 비제휴 고지.

내부 검증 결과를 반영한 것이며 법률 자문이 아니다. 결정 기록은 docs/DECISIONS.md UDEC-25~28.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_eng_revision import Env, fnd, ids  # noqa: E402

from press_review import edit_checks as ec  # noqa: E402
from press_review import render, review  # noqa: E402
from press_review.ingestion import http as ing_http  # noqa: E402
from press_review.providers import deepseek  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "press-check"


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# ---- H1: 뜻 반전 수정안은 자동 변경본에서 제외한다 ------------------------------------------------------------------

NEG_BLOCKS = [
    ("title", "title", "시 규정 이행 점검"),
    ("p1", "body", "시는 규정을 잘 지켰다고 밝혔다. 시민단체는 점검 결과를 공개하라고 요구했다."),
]


@pytest.mark.parametrize("quote,rep", [("잘 지켰다고", "못지켰다고"), ("잘 지켰다고", "안지켰다고")])
def test_h1_one_char_negation_prefix_flip_is_proposal_not_applied(quote, rep):
    env = Env([fnd("finding-001", 2, "p1", quote, rep, arts=NEG_BLOCKS)], blocks=NEG_BLOCKS)
    r = env.build()
    assert r["ok"] and r["applied"] == []
    assert ids(r["skipped"]) == [] or all(s["reason_code"].startswith("NEGATION") for s in r["skipped"])
    assert "finding-001" in ids(r["proposals"])
    assert "못지켰다고" not in "".join(b["text"] for b in r["revised_blocks"]) and "안지켰다고" not in "".join(b["text"] for b in r["revised_blocks"])


def test_h1_negation_prefix_counter():
    c = ec.count_negations
    assert c("못지켰다") == 1 and c("안지켰다") == 1 and c("잘 지켰다") == 0
    assert c("안전하다") == 0 and c("안내문을 냈다") == 0 and c("안에서 열렸다") == 0


def test_h1_neutral_edit_still_applies():
    env = Env([fnd("finding-001", 2, "p1", "공개하라고 요구했다", "공개를 요구했다", arts=NEG_BLOCKS)], blocks=NEG_BLOCKS)
    assert ids(env.build()["applied"]) == ["finding-001"]


# ---- H2: search_cases 응답의 이메일·전화번호 가림 ------------------------------------------------------------------

SAFE = ("대법원 2019. 1. 2. 선고 2018다12345 판결은 1,234,567원과 3억 5천만원의 배상을 정한 사건이다. "
        "법률 제17354호 제2021-0123호 2021.03.04 02:30 1588 사건번호 2021도1234 제310조")
CONTACT = "문의는 reporter.kim@example.co.kr 또는 010-1234-5678, 02-123-4567, (031) 123-4567, +82-10-1234-5678 로 한다."


class StubCorpus:
    snapshot_id = "stub"
    is_fixture = False

    def __init__(self, text):
        self.text = text

    def search(self, query, **kw):
        return [{"source_id": "s1", "passage_id": "p1", "url": "https://example.test/x", "locator": {"kind": "page", "value": "1"},
                 "authority_type": "court_ruling", "decision_date": "2020-01-01", "text": self.text, "is_fixture": False,
                 "display_text": self.text[:300], "display_truncated": len(self.text) > 300, "redistributable": True}]

    def get_passage(self, source_id, passage_id):
        return self.search("")[0]


def test_h2_email_and_phone_are_masked_in_all_text_fields():
    out = review.search_cases(StubCorpus(CONTACT + " " + SAFE + " 가나다" * 100), query="문의", limit=5)
    dump = json.dumps(out, ensure_ascii=False)
    for s in ("reporter.kim@example.co.kr", "010-1234-5678", "02-123-4567", "031) 123-4567", "10-1234-5678"):
        assert s not in dump
    assert out["passages"][0]["text"].count("[이메일 가림]") == 1 and out["passages"][0]["text"].count("[전화번호 가림]") == 4
    assert "[이메일 가림]" in out["passages"][0]["display_text"]


def test_h2_excerpts_are_masked_too():
    long_text = ("가나다 " * 400) + CONTACT + (" 라마바" * 50)
    out = review.search_cases(StubCorpus(long_text), query="문의", limit=10)
    dump = json.dumps(out, ensure_ascii=False)
    assert "example.co.kr" not in dump and "010-1234-5678" not in dump


def test_h2_case_numbers_amounts_dates_and_statute_numbers_are_kept():
    out = review.search_cases(StubCorpus(SAFE), query="판결", limit=5)
    assert out["passages"][0]["text"] == SAFE
    assert "contact_masked" not in out


def test_h2_quote_check_still_matches_masked_text(tmp_path):
    masked = review.search_cases(StubCorpus(CONTACT), query="문의", limit=5)["passages"][0]["text"]
    quote = masked[:30]
    warns: list[str] = []
    findings = [{"evidence": [{"source_id": "s1", "passage_id": "p1"}]}]
    review._quote_warnings({"f1": {"support": f"판결은 “{quote}”라고 한다"}}, findings, {"p1": "본문 문장이다"}, StubCorpus(CONTACT), warns)
    assert warns == []
    review._quote_warnings({"f1": {"support": "판결은 “전혀 다른 문장을 지어 낸 인용이다”라고 한다"}}, findings, {"p1": "본문 문장이다"}, StubCorpus(CONTACT), warns)
    assert len(warns) == 1


# ---- H3: 고지 문구 ---------------------------------------------------------------------------------------------------

NOTICE_FILES = ["README.md", "plugins/press-check/README.md", "docs/USER_GUIDE_KO.md"]


@pytest.mark.parametrize("rel", NOTICE_FILES)
def test_h3_notice_states_us_provider_training_retention_and_local_transcript(rel):
    t = read(rel)
    assert "미국" in t and "Anthropic" in t
    assert "요금제" in t and "학습" in t and "보존" in t
    assert "~/.claude/projects" in t and "평문" in t
    assert "서버 메모리" in t and "Claude Code 로컬 기록" in t
    assert "디스크에 저장하지 않고 서버 메모리" not in t


def test_h3_skills_do_not_say_memory_only():
    for rel in ("plugins/press-check/skills/draft/SKILL.md", "plugins/press-check/skills/explain/SKILL.md"):
        t = read(rel)
        assert "미국" in t or "Claude Code 로컬 기록" in t
        assert "디스크에 저장되지 않고" not in t


# ---- H4: 서버 출력의 부인 문구 / README 한글 면책 ---------------------------------------------------------------------

def test_h4_table_and_detail_carry_disclaimer_with_star_note():
    assert "법률 자문이 아닙니다" in render.NOTICE_LINE and "확률" in render.NOTICE_LINE
    env = Env([fnd("finding-001", 2, "p1", "공개하라고 요구했다", "공개를 요구했다", arts=NEG_BLOCKS)], blocks=NEG_BLOCKS)
    assert render.NOTICE_LINE in render.render_table(env.report())
    assert render.NOTICE_LINE in review.explain_finding(env.store, report_id=env.rid, finding_id="finding-001")["markdown"]
    assert render.NOTICE_LINE in " ".join(env.build()["notes"])


def test_h4_readme_has_korean_disclaimer_and_license_untouched():
    t = read("README.md")
    assert "면책" in t and "고의 또는 중과실" in t and "법률 자문이 아닙니다" in t
    assert "MIT License" in read("LICENSE")


# ---- H5: User-Agent ---------------------------------------------------------------------------------------------------

def test_h5_user_agent_is_neutral_and_points_to_repo():
    for ua in (ing_http.USER_AGENT, deepseek.USER_AGENT):
        assert ua == "press-check/0.1 (+https://github.com/JINKOO1991/press-check)"
        assert "non-commercial" not in ua and "@" not in ua


# ---- H6: 중단·삭제 요청 창구 ---------------------------------------------------------------------------------------------

def test_h6_takedown_doc_exists_and_is_linked():
    t = read("docs/TAKEDOWN_REQUESTS_KO.md")
    assert "GitHub 이슈" in t and "7일" in t and "UDEC-27" in t and "조치 기록" in t
    assert "docs/TAKEDOWN_REQUESTS_KO.md" in read("README.md")


# ---- H7: 비제휴 고지 ---------------------------------------------------------------------------------------------------

def test_h7_readme_states_no_affiliation():
    t = read("README.md")
    assert "제휴" in t and "승인" in t and "법제처" in t and "언론중재위원회" in t


def test_h2_contact_cut_by_excerpt_and_display_limits_is_masked():
    email = "reporter.kim@example.co.kr"
    for pad in (288, 295):                                   # 발췌 앞 300자·display_text 300자 경계가 연락처 중간을 자른다
        text = "가" * pad + email + " 나" * 700
        for limit in (5, 10):
            out = review.search_cases(StubCorpus(text), query="문의", limit=limit)
            dump = json.dumps(out, ensure_ascii=False)
            assert "reporter" not in dump and "example.co" not in dump and out.get("contact_masked")
    ex = review.search_cases(StubCorpus("가" * 288 + email + " 나" * 700), query="문의", limit=10)["passages"][0]["excerpts"][0]
    assert (ex["start"], ex["end"]) == (0, 300)                # 좌표는 원문 기준 그대로


def test_h2_email_mask_is_near_linear_on_long_non_email_input():
    import time
    from press_review import contact_mask
    n = 40000
    for s in ("a" * n, "a." * (n // 2), "a@" * (n // 2), "@" * n, "a" * n + "@"):
        t0 = time.perf_counter()
        assert contact_mask.mask_text(s)[1] == 0
        assert time.perf_counter() - t0 < 0.3
    assert contact_mask.mask_text("a.b+c_d-e@sub.mail.example.com 와 reporter.kim@example.co.kr")[1] == 2

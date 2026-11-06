"""출처 표시 규칙(UDEC-11·UDEC-18)이 explain·review·corpus-status 스킬 문서에 있는지 확인하는 구조 회귀 테스트."""
import pdr_engine_paths as paths

from press_review import corpus_sqlite

SKILLS = paths.REPO / "plugins" / "press-check" / "skills"
PHRASE = "헌법재판소 결정, 법제처 국가법령정보센터 제공"
PREC_PHRASE = "법원 판결, 법제처 국가법령정보센터 제공"
LX_PHRASE = "법원 판결, 법원도서관 법고을 제공"


def _text(name):
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def test_phrase_constant_is_exact():
    assert corpus_sqlite.SOURCE_ATTRIBUTION == {"lawgo_detc": PHRASE, "ccourt_detc": "헌법재판소 결정, 헌법재판소 제공", "lawgo_prec_supreme": PREC_PHRASE,
                                                "lawgo_prec_refs": PREC_PHRASE, "lx_prec": LX_PHRASE}


def test_explain_skill_requires_attribution_for_detc():
    t = _text("explain")
    assert PHRASE in t and PREC_PHRASE in t and LX_PHRASE in t and "lawgo_detc:" in t and "lawgo_prec:" in t and "lx_prec:" in t


def test_review_skill_follows_source_attribution_field():
    t = _text("review")
    assert PHRASE in t and PREC_PHRASE in t and LX_PHRASE in t and "source_attribution" in t and "lx_prec:" in t


def test_corpus_status_skill_shows_attribution_and_pending():
    t = _text("corpus-status")
    assert PHRASE in t and PREC_PHRASE in t and LX_PHRASE in t and "attribution" in t and "case_admission_pending" in t

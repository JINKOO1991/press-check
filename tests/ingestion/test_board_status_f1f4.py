"""2026-10-16 시정권고 현황 F1·F4(docs/coverage/P2_board_status_hwp_diagnosis.md). 합성 입력, 네트워크 없음."""
import pdr_ing_paths  # noqa: F401
from press_review.ingestion import admission
from press_review.ingestion import structure as st
from press_review.ingestion import structure_records as sr


def test_f1_prompt_is_board_status_only():
    """F1: 새 프롬프트는 시정권고 현황 소스에만 연결된다. 사례집 등 다른 소스의 프로필·프롬프트 파일은 그대로다."""
    assert sr.SOURCE_PROFILE["pac_board_correction_status"] == "pac_board_status"
    others = {k: v for k, v in sr.SOURCE_PROFILE.items() if k != "pac_board_correction_status"}
    assert "pac_board_status" not in others.values()
    assert sr.SOURCE_PROFILE["pac_correction_casebooks"] == "pac_correction"
    assert sr.PROFILES["pac_correction"].prompt == "pac_correction.md"
    board = st.load_prompt(sr.PROFILES["pac_board_status"]).system
    assert "row-unique quote" in board and "null이라도" not in board and "still output it with an empty outcome" not in board
    assert "still output it with an empty outcome" in st.load_prompt(sr.PROFILES["pac_correction"]).system   # 사례집 프롬프트는 불변


def test_f4_statistics_attachment_is_not_applicable():
    """F4: 사건이 있고 전부 격리됐으며 사건번호가 하나도 없는 첨부 = 사건 행 없는 통계 문서. 번호가 있거나 격리가 일부면 기존 판정."""
    assert admission.judge(4, 4, 0, 0, numbered_cases=0) == ("not_applicable", ["no_case_rows"])
    assert admission.judge(106, 106, 0, 0, numbered_cases=106)[0] == "excluded"       # 번호 있는 사건 전부 격리 = 제외(대상 아님 아님)
    assert admission.judge(1, 0, 1, 1, numbered_cases=0)[0] == "admitted"             # 통과 요약표(bn1284형)는 그대로
    assert admission.judge(4, 4, 0, 0)[0] == "excluded"                               # 인자를 안 주면(다른 소스) 기존 동작

"""draft 스킬 지침의 구조 회귀 테스트: 도구 이름, 고정 머리말, 쓰기 도구 미허용."""
import re

import pdr_engine_paths as paths

SKILL = paths.REPO / "plugins" / "press-check" / "skills" / "draft" / "SKILL.md"
PREFIX = "mcp__plugin_press-check_press-review__"
SERVER_TOOLS = ("prepare_article", "search_cases", "submit_report", "explain_finding", "corpus_status", "build_revision")


def _parts():
    text = SKILL.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, flags=re.S)
    assert m, "frontmatter 없음"
    return m.group(1), m.group(2)


def _allowed_tools():
    fm, _ = _parts()
    m = re.search(r"^allowed-tools:\s*(.+)$", fm, flags=re.M)
    assert m, "allowed-tools 없음"
    return m.group(1).split()


def test_draft_frontmatter_name():
    fm, _ = _parts()
    assert re.search(r"^name:\s*draft\s*$", fm, flags=re.M)
    assert re.search(r"^description:\s*\S", fm, flags=re.M)


def test_draft_allowed_tools_has_all_server_tools():
    tools = _allowed_tools()
    for t in SERVER_TOOLS:
        assert PREFIX + t in tools
    assert PREFIX + "build_revision" in tools


def test_draft_allowed_tools_has_no_write_or_edit():
    tools = _allowed_tools()
    for t in tools:
        assert t.split("(")[0] not in ("Write", "Edit", "MultiEdit", "NotebookEdit")


def test_draft_body_has_fixed_headers():
    _, body = _parts()
    assert "`[press-check 검토 완료] report_id=<id> 적용 <n>건 미해결 <m>건 흐름경고 <k>건`" in body
    assert "`[press-check 검토 미완료] 사유=<짧은 사유>`" in body

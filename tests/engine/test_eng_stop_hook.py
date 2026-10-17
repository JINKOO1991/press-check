"""Stop 훅(hooks/stop_check.py) 시험: 합성 transcript 와 subprocess 호출만 쓴다."""
import json
import os
import subprocess
import sys

import pdr_engine_paths as paths

HOOK_DIR = paths.REPO / "plugins" / "press-check" / "hooks"
HOOK = HOOK_DIR / "stop_check.py"
PFX = "mcp__plugin_press-check_press-review__"
DONE = "[press-check 검토 완료] report_id=rpt-0123456789ab 적용 1건 미해결 0건 흐름경고 0건"
UNDONE = "[press-check 검토 미완료] 사유=검색 색인 없음"
ARTICLE_TEXT = "합성기사본문고유문장가나다라마바사"


def _u(content, **kw):
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": content}, **kw}


def _a(blocks):
    return {"type": "assistant", "isSidechain": False, "message": {"role": "assistant", "content": blocks}}


def _tool(name, **inp):
    return _a([{"type": "tool_use", "id": "t1", "name": name, "input": inp}])


def _result():
    return _u([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])


def _slash(name="press-check:draft"):
    return _u(f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>\n<command-args>{ARTICLE_TEXT}</command-args>")


def _meta(path="/x/plugins/press-check/skills/draft"):
    return _u([{"type": "text", "text": f"Base directory for this skill: {path}\n\n본문"}], isMeta=True)


def _review_calls():
    return [_tool(PFX + "prepare_article"), _result(), _tool(PFX + "submit_report"), _result()]


def _write(tmp_path, entries):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8")
    return p


def _run(payload, raw=None):
    data = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
    r = subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True, text=True, encoding="utf-8", timeout=20)
    return r


def _hook(tmp_path, entries, msg, **extra):
    payload = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": msg,
               "transcript_path": str(_write(tmp_path, entries)) if entries is not None else str(tmp_path / "missing.jsonl")}
    payload.update(extra)
    return _run(payload)


def _assert_allow(r):
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def _assert_block(r):
    assert r.returncode == 0
    out = r.stdout.strip()
    assert "\n" not in out
    d = json.loads(out)
    assert d["decision"] == "block"
    assert d["reason"]
    assert ARTICLE_TEXT not in r.stdout and ARTICLE_TEXT not in r.stderr
    return d


def test_hook_file_exists():
    assert HOOK.is_file()


def test_stop_hook_active_allows(tmp_path):
    r = _hook(tmp_path, [_slash(), _meta()], "머리말 없음", stop_hook_active=True)
    _assert_allow(r)


def test_non_draft_turn_allows(tmp_path):
    _assert_allow(_hook(tmp_path, [_u("안녕"), _a([{"type": "text", "text": "안녕하세요"}])], "안녕하세요"))


def test_slash_draft_valid_done_with_calls_allows(tmp_path):
    e = [_slash(), _meta()] + _review_calls()
    _assert_allow(_hook(tmp_path, e, DONE + "\n본문"))


def test_slash_draft_done_without_calls_blocks(tmp_path):
    d = _assert_block(_hook(tmp_path, [_slash(), _meta()], DONE + "\n본문"))
    assert "도구" in d["reason"]


def test_slash_draft_done_only_prepare_blocks(tmp_path):
    e = [_slash(), _meta(), _tool(PFX + "prepare_article"), _result()]
    _assert_block(_hook(tmp_path, e, DONE))


def test_undone_header_allows(tmp_path):
    _assert_allow(_hook(tmp_path, [_slash(), _meta()], "  \n" + UNDONE + "\n설명"))


def test_draft_without_header_blocks(tmp_path):
    d = _assert_block(_hook(tmp_path, [_slash(), _meta()] + _review_calls(), "검토했습니다."))
    assert "[press-check 검토 완료] report_id=" in d["reason"]
    assert "[press-check 검토 미완료] 사유=" in d["reason"]


def test_header_format_mismatch_blocks(tmp_path):
    bad = DONE.replace("press-check", "press_check")
    _assert_block(_hook(tmp_path, [_slash(), _meta()] + _review_calls(), bad))
    bad2 = "[press-check 검토 완료] report_id=rpt-1 적용 x건 미해결 0건 흐름경고 0건"
    _assert_block(_hook(tmp_path, [_slash(), _meta()] + _review_calls(), bad2))


def test_bare_slash_alias_recognised(tmp_path):
    _assert_block(_hook(tmp_path, [_slash("draft"), _meta()], "머리말 없음"))


def test_skill_tool_call_recognised(tmp_path):
    for name in ("press-check:draft", "draft"):
        e = [_u("보도자료로 기사 써줘"), _tool("Skill", skill=name), _result(), _meta()]
        _assert_block(_hook(tmp_path, e, "머리말 없음"))
        _assert_allow(_hook(tmp_path, e + _review_calls(), DONE))


def test_skill_tool_other_skill_ignored(tmp_path):
    e = [_u("검토해줘"), _tool("Skill", skill="press-check:review"), _result()]
    _assert_allow(_hook(tmp_path, e, "검토 결과"))


def test_meta_entry_after_prompt_is_not_user_input(tmp_path):
    # 마지막 사용자 입력은 슬래시 명령 항목이고 isMeta 항목이 그 뒤에 온다. 메타를 입력으로 잡으면 draft 호출을 놓친다.
    e = [_u("이전 대화"), _a([{"type": "text", "text": "응답"}]), _slash(), _meta()]
    _assert_block(_hook(tmp_path, e, "머리말 없음"))


def test_meta_skill_dir_only_detects_draft(tmp_path):
    e = [_u("검토해줘"), _meta("/x/plugins/press-check/skills/review")]
    _assert_allow(_hook(tmp_path, e, "검토 결과"))
    e = [_u("초안 써줘"), _meta("/x/plugins/press-check/skills/draft")]
    _assert_block(_hook(tmp_path, e, "머리말 없음"))


def test_sidechain_entries_not_user_input(tmp_path):
    side = _u("서브에이전트 입력")
    side["isSidechain"] = True
    e = [_slash(), _meta(), side]
    _assert_block(_hook(tmp_path, e, "머리말 없음"))


def test_draft_only_in_previous_turn_allows(tmp_path):
    e = [_slash(), _meta()] + _review_calls() + [_a([{"type": "text", "text": DONE}]), _u("고마워"), _a([{"type": "text", "text": "천만에요"}])]
    _assert_allow(_hook(tmp_path, e, "천만에요"))


def test_transcript_unreadable_allows(tmp_path):
    _assert_allow(_hook(tmp_path, None, "머리말 없음"))


def test_broken_json_input_allows():
    _assert_allow(_run(None, raw="{not json"))
    _assert_allow(_run(None, raw=""))


def test_missing_or_empty_last_message_allows(tmp_path):
    e = [_slash(), _meta()]
    p = {"hook_event_name": "Stop", "stop_hook_active": False, "transcript_path": str(_write(tmp_path, e))}
    _assert_allow(_run(p))
    _assert_allow(_run({**p, "last_assistant_message": ""}))
    _assert_allow(_run({**p, "last_assistant_message": 123}))


def test_block_output_single_line_and_no_article_text(tmp_path):
    r = _hook(tmp_path, [_slash(), _meta()], ARTICLE_TEXT + " 머리말 없는 응답")
    _assert_block(r)


def test_malformed_transcript_lines_tolerated(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = ["{broken", json.dumps(_slash(), ensure_ascii=False), json.dumps(_meta(), ensure_ascii=False)]
    p.write_text("\n".join(lines), encoding="utf-8")
    r = _run({"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "x", "transcript_path": str(p)})
    _assert_block(r)


def test_no_log_files_created(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    e = [_slash(), _meta()]
    tp = _write(tmp_path, e)
    before = {p for p in tmp_path.rglob("*")}
    for msg in ("머리말 없음", DONE, UNDONE):
        subprocess.run([sys.executable, str(HOOK)], cwd=work, capture_output=True, text=True, encoding="utf-8", timeout=20,
                       input=json.dumps({"stop_hook_active": False, "last_assistant_message": msg, "transcript_path": str(tp)}))
    assert {p for p in tmp_path.rglob("*")} == before
    assert list(work.iterdir()) == []


def test_hooks_json_command_matches_server_python_resolution():
    cfg = json.loads((HOOK_DIR / "hooks.json").read_text(encoding="utf-8"))
    cmd = cfg["hooks"]["Stop"][0]["hooks"][0]
    assert cmd["type"] == "command"
    assert "${PRESS_REVIEW_PYTHON:-python3}" in cmd["command"]
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/stop_check.py" in cmd["command"]
    assert cmd["timeout"] == 10
    mcp = json.loads((paths.REPO / "plugins" / "press-check" / ".mcp.json").read_text(encoding="utf-8"))
    assert "${PRESS_REVIEW_PYTHON:-python3}" == mcp["mcpServers"]["press-review"]["command"]

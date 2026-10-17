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


ROOT = str(HOOK_DIR.parent)


def _meta(path=ROOT + "/skills/draft"):
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


def test_bare_slash_draft_not_recognised(tmp_path):
    _assert_allow(_hook(tmp_path, [_slash("draft")], "머리말 없음"))
    _assert_allow(_hook(tmp_path, [_slash("letter:draft")], "머리말 없음"))


def test_skill_tool_call_recognised(tmp_path):
    e = [_u("보도자료로 기사 써줘"), _tool("Skill", skill="press-check:draft"), _result(), _meta()]
    _assert_block(_hook(tmp_path, e, "머리말 없음"))
    _assert_allow(_hook(tmp_path, e + _review_calls(), DONE))


def test_skill_tool_other_plugin_or_bare_draft_allows(tmp_path):
    for name in ("letter:draft", "draft"):
        e = [_u("편지 써줘"), _tool("Skill", skill=name), _result(), _meta("/cache/letter/skills/draft")]
        _assert_allow(_hook(tmp_path, e, "편지 본문"))


def test_meta_path_matching(tmp_path):
    ok = [ROOT + "/skills/draft", ROOT + "/skills/draft/", ROOT + "/skills/../skills/draft"]
    for path in ok:
        _assert_block(_hook(tmp_path, [_u("초안"), _meta(path)], "머리말 없음"))
    no = ["/cache/letter/skills/draft", "/x/press-check/letter/skills/draft", "/x/press-check-extra/skills/draft",
          "/x/press-check/skills/draft", ROOT + "/skills/review", ROOT + "/skills/draft-more", ROOT + "x/skills/draft"]
    for path in no:
        _assert_allow(_hook(tmp_path, [_u("초안"), _meta(path)], "머리말 없음"))


def _run_at(script, env_extra, tmp_path, meta_path):
    tp = _write(tmp_path, [_u("초안"), _meta(meta_path)])
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env.update(env_extra)
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20, env=env,
                          input=json.dumps({"stop_hook_active": False, "last_assistant_message": "머리말 없음", "transcript_path": str(tp)}))


def test_meta_arbitrary_plugin_dir_by_script_location(tmp_path):
    import shutil
    root = tmp_path / "arbitrary-name"
    (root / "hooks").mkdir(parents=True)
    shutil.copy(HOOK, root / "hooks" / "stop_check.py")
    script = root / "hooks" / "stop_check.py"
    _assert_block(_run_at(script, {}, tmp_path, str(root / "skills" / "draft")))
    _assert_allow(_run_at(script, {}, tmp_path, "/cache/letter/skills/draft"))


def test_meta_arbitrary_plugin_dir_by_env(tmp_path):
    root = tmp_path / "dev-copy"
    _assert_block(_run_at(HOOK, {"CLAUDE_PLUGIN_ROOT": str(root)}, tmp_path, str(root) + "/skills/draft/"))
    _assert_block(_run_at(HOOK, {"CLAUDE_PLUGIN_ROOT": str(root)}, tmp_path, ROOT + "/skills/draft"))
    _assert_allow(_run_at(HOOK, {"CLAUDE_PLUGIN_ROOT": str(root)}, tmp_path, str(root) + "-x/skills/draft"))


def test_meta_path_normcase_logic(monkeypatch):
    m = _load_module()
    monkeypatch.setattr(m.os.path, "normcase", lambda p: p.replace("/", "\\").lower())
    assert m._is_press_check_draft_dir(ROOT.upper() + "/Skills/Draft")
    assert m._is_press_check_draft_dir(ROOT.replace("/", "\\") + "\\skills\\draft\\")
    assert not m._is_press_check_draft_dir(ROOT.upper() + "/Skills/Review")


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


def _load_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("stop_check_mod", HOOK)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _reread_decision(monkeypatch, tmp_path, second):
    m = _load_module()
    monkeypatch.setattr(m, "RETRY_WAIT_S", 0)
    first_p = _write(tmp_path, [_slash(), _meta()])
    reads = []

    def fake_read(path):
        reads.append(1)
        if len(reads) == 1:
            return [_slash(), _meta()]
        if second == "error":
            raise OSError("boom")
        if second == "calls":
            return [_slash(), _meta()] + _review_calls()
        return [_slash(), _meta()]

    monkeypatch.setattr(m, "_read_entries", fake_read)
    r = m._decide({"stop_hook_active": False, "last_assistant_message": DONE, "transcript_path": str(first_p)})
    assert len(reads) == 2
    return r


def test_reread_failure_allows(monkeypatch, tmp_path):
    assert _reread_decision(monkeypatch, tmp_path, "error") is None


def test_reread_no_calls_blocks(monkeypatch, tmp_path):
    assert _reread_decision(monkeypatch, tmp_path, "none")


def test_reread_with_calls_allows(monkeypatch, tmp_path):
    assert _reread_decision(monkeypatch, tmp_path, "calls") is None


# ---- 변경본 글자 일치 검사 ----
BLOCKS = [
    {"block_id": "title", "section": "title", "text": "합성 제목 가나다"},
    {"block_id": "p1", "section": "body", "text": "합성 첫 문단이다."},
    {"block_id": "p2", "section": "body", "text": '합성 인용 "경찰은 수사 중이다"라고 했다.'},
    {"block_id": "p3", "section": "body", "text": "첫 줄\n둘째 줄"},
]
BODY = "\n\n".join(b["text"] for b in BLOCKS)


def _rev_use(uid):
    return _a([{"type": "tool_use", "id": uid, "name": PFX + "build_revision", "input": {"report_id": "rpt-x"}}])


def _rev_res(uid, payload, form="str"):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    content = {"str": text, "blocks": [{"type": "text", "text": text}]}[form]
    return _u([{"type": "tool_result", "tool_use_id": uid, "content": content}])


def _rev_turn(blocks=BLOCKS, uid="rv1", form="str", ok=True):
    return [_slash(), _meta()] + _review_calls() + [_rev_use(uid), _rev_res(uid, {"ok": ok, "revised_blocks": blocks}, form)]


def _typo_body():
    return BODY.replace("경찰은", "경경찰은")


def test_revision_match_allows(tmp_path):
    _assert_allow(_hook(tmp_path, _rev_turn(), DONE + "\n" + BODY))


def test_revision_duplicate_char_blocks_only_that_block(tmp_path):
    d = _assert_block(_hook(tmp_path, _rev_turn(), DONE + "\n" + _typo_body()))
    assert "p2" in d["reason"]
    for other in ("title", "p1", "p3"):
        assert other not in d["reason"]
    assert "경찰" not in d["reason"] and "합성" not in d["reason"]
    assert "한 글자도 바꾸지 말고" in d["reason"]


def test_revision_result_forms_parsed(tmp_path):
    for form in ("str", "blocks"):
        e = _rev_turn(form=form)
        _assert_allow(_hook(tmp_path, e, DONE + "\n" + BODY))
        _assert_block(_hook(tmp_path, e, DONE + "\n" + _typo_body()))
    wrapped = [_slash(), _meta()] + _review_calls() + [_rev_use("rv1"), _rev_res("rv1", {"structuredContent": {"ok": True, "revised_blocks": BLOCKS}})]
    _assert_allow(_hook(tmp_path, wrapped, DONE + "\n" + BODY))
    _assert_block(_hook(tmp_path, wrapped, DONE + "\n" + _typo_body()))


def test_revision_no_call_allows(tmp_path):
    _assert_allow(_hook(tmp_path, [_slash(), _meta()] + _review_calls(), DONE + "\n" + _typo_body()))


def test_revision_result_missing_allows_after_reread(monkeypatch, tmp_path):
    m = _load_module()
    monkeypatch.setattr(m, "RETRY_WAIT_S", 0)
    p = _write(tmp_path, [_slash(), _meta()] + _review_calls() + [_rev_use("rv1")])
    r = m._decide({"stop_hook_active": False, "last_assistant_message": DONE + "\n" + _typo_body(), "transcript_path": str(p)})
    assert r is None


def test_revision_result_appears_on_reread_is_checked(monkeypatch, tmp_path):
    m = _load_module()
    monkeypatch.setattr(m, "RETRY_WAIT_S", 0)
    base = [_slash(), _meta()] + _review_calls() + [_rev_use("rv1")]
    reads = []

    def fake(path):
        reads.append(1)
        return base if len(reads) == 1 else base + [_rev_res("rv1", {"ok": True, "revised_blocks": BLOCKS})]

    monkeypatch.setattr(m, "_read_entries", fake)
    r = m._decide({"stop_hook_active": False, "last_assistant_message": DONE + "\n" + _typo_body(), "transcript_path": "x"})
    assert r and "p2" in r


def test_revision_not_ok_or_empty_or_unparseable_allows(tmp_path):
    msg = DONE + "\n" + _typo_body()
    _assert_allow(_hook(tmp_path, _rev_turn(ok=False), msg))
    _assert_allow(_hook(tmp_path, _rev_turn(blocks=[]), msg))
    e = [_slash(), _meta()] + _review_calls() + [_rev_use("rv1"), _rev_res("rv1", "not json {")]
    _assert_allow(_hook(tmp_path, e, msg))
    e = [_slash(), _meta()] + _review_calls() + [_rev_use("rv1"), _rev_res("rv1", {"ok": True})]
    _assert_allow(_hook(tmp_path, e, msg))


def test_revision_uses_last_call_in_turn(tmp_path):
    old = [dict(BLOCKS[0], text="오래된 제목 결과")]
    e = [_slash(), _meta()] + _review_calls() + [_rev_use("rv1"), _rev_res("rv1", {"ok": True, "revised_blocks": old}),
                                                  _rev_use("rv2"), _rev_res("rv2", {"ok": True, "revised_blocks": BLOCKS})]
    _assert_allow(_hook(tmp_path, e, DONE + "\n" + BODY))
    d = _assert_block(_hook(tmp_path, e, DONE + "\n" + _typo_body()))
    assert "p2" in d["reason"]


def test_revision_previous_turn_result_not_used(tmp_path):
    prev = [_u("앞선 요청"), _rev_use("old1"), _rev_res("old1", {"ok": True, "revised_blocks": BLOCKS}), _a([{"type": "text", "text": "끝"}])]
    e = prev + [_slash(), _meta()] + _review_calls()
    _assert_allow(_hook(tmp_path, e, DONE + "\n" + _typo_body()))


def test_revision_stop_hook_active_allows(tmp_path):
    _assert_allow(_hook(tmp_path, _rev_turn(), DONE + "\n" + _typo_body(), stop_hook_active=True))


def test_revision_not_checked_for_undone_header(tmp_path):
    _assert_allow(_hook(tmp_path, _rev_turn(), UNDONE + "\n" + _typo_body()))


def test_revision_multiline_block_consistent(tmp_path):
    e = _rev_turn()
    _assert_allow(_hook(tmp_path, e, DONE + "\n" + BODY.replace("\n둘째", "\r\n둘째")))
    d = _assert_block(_hook(tmp_path, e, DONE + "\n" + BODY.replace("첫 줄\n둘째 줄", "첫 줄 둘째 줄")))
    assert "p3" in d["reason"]

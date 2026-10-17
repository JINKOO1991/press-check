#!/usr/bin/env python3
"""Stop 훅: 이번 턴에 draft 스킬이 호출됐는데 최종 응답이 고정 머리말로 시작하지 않으면 한 번 막는다.

허용은 출력 없이 종료 코드 0, 차단은 stdout 에 JSON 한 줄. 어떤 예외도 허용으로 처리한다(fail-open).
파일을 쓰지 않고, 기사 원문을 출력하지 않는다. 표준 라이브러리만 쓴다.
"""
import json
import re
import sys
import time

TAIL_BYTES = 3 * 1024 * 1024
RETRY_WAIT_S = 1.5
DRAFT_NAMES = ("press-check:draft", "draft")
UNDONE_RE = re.compile(r"^\[press-check 검토 미완료\] 사유=\S.*")
DONE_RE = re.compile(r"^\[press-check 검토 완료\] report_id=\S+ 적용 \d+건 미해결 \d+건 흐름경고 \d+건")

REASON_BASE = (
    "draft 절차가 끝나지 않았습니다. press-check review 절차(prepare_article, search_cases, submit_report, build_revision)를 "
    "수행하고, 최종 응답의 첫 줄을 고정 머리말로 시작하세요. "
    "검토를 마쳤으면 '[press-check 검토 완료] report_id=<id> 적용 <n>건 미해결 <m>건 흐름경고 <k>건', "
    "마치지 못했으면 '[press-check 검토 미완료] 사유=<짧은 사유>'."
)
REASON_NO_CALLS = (
    "최종 응답 머리말은 검토 완료인데 이번 턴에 실제 검토 도구 호출(prepare_article, submit_report)이 없습니다. "
    "이번 턴에서 검토를 수행한 뒤 그 결과로 머리말을 쓰거나, 수행하지 못했다면 "
    "'[press-check 검토 미완료] 사유=<짧은 사유>'로 시작하세요. "
    "완료 머리말 형식: '[press-check 검토 완료] report_id=<id> 적용 <n>건 미해결 <m>건 흐름경고 <k>건'."
)


def _read_entries(path):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        start = max(0, size - TAIL_BYTES)
        f.seek(start)
        data = f.read()
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln.decode("utf-8"))
        except Exception:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _content(entry):
    m = entry.get("message")
    return m.get("content") if isinstance(m, dict) else None


def _has_tool_result(entry):
    c = _content(entry)
    return isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def _is_user_input(entry):
    return (entry.get("type") == "user" and not entry.get("isMeta") and not entry.get("isSidechain")
            and not _has_tool_result(entry))


def _text_of(entry):
    c = _content(entry)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def _current_turn(entries):
    for i in range(len(entries) - 1, -1, -1):
        if _is_user_input(entries[i]):
            return entries[i:]
    return None


def _tool_uses(turn):
    for e in turn:
        if e.get("type") != "assistant":
            continue
        c = _content(e)
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    yield b


def _draft_called(turn):
    first = _text_of(turn[0])
    for n in DRAFT_NAMES:
        if f"<command-name>/{n}</command-name>" in first:
            return True
    for b in _tool_uses(turn):
        inp = b.get("input")
        if b.get("name") == "Skill" and isinstance(inp, dict) and inp.get("skill") in DRAFT_NAMES:
            return True
    for e in turn:
        if e.get("type") == "user" and e.get("isMeta"):
            t = _text_of(e).lstrip()
            if t.startswith("Base directory for this skill:") and "/skills/draft" in t.split("\n", 1)[0]:
                return True
    return False


def _review_tools_called(turn):
    names = [b.get("name") or "" for b in _tool_uses(turn)]
    return (any(n.endswith("__prepare_article") for n in names)
            and any(n.endswith("__submit_report") for n in names))


def _load_turn(path):
    try:
        return _current_turn(_read_entries(path))
    except Exception:
        return None


def _decide(payload):
    """None 이면 허용, 문자열이면 차단 사유."""
    if payload.get("stop_hook_active") is True:
        return None
    msg = payload.get("last_assistant_message")
    if not isinstance(msg, str) or not msg.strip():
        return None
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    turn = _load_turn(path)
    if not turn or not _draft_called(turn):
        return None
    head = msg.lstrip()
    if UNDONE_RE.match(head):
        return None
    if DONE_RE.match(head):
        if _review_tools_called(turn):
            return None
        time.sleep(RETRY_WAIT_S)
        turn = _load_turn(path)
        if turn and _review_tools_called(turn):
            return None
        return REASON_NO_CALLS
    return REASON_BASE


def main():
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return 0
        reason = _decide(payload)
        if reason:
            sys.stdout.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False) + "\n")
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""search_eval(평가 전용 limit 상한 확장)이 기자 경로(MCP·review)에 노출되지 않는지."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_eng_search_rank as sr  # noqa: E402

from press_review import corpus as corpus_mod, corpus_sqlite as cs, mcp_server  # noqa: E402

ENGINE = Path(cs.__file__).resolve().parent


def test_search_keeps_cap_while_search_eval_allows_more(tmp_path):
    idx, _ = sr.build(tmp_path, [(f"c{i}", f"신청인은 반론보도를 청구하였다 사건{i}.") for i in range(14)])
    try:
        assert len(idx.search("반론보도", limit=20)) == corpus_mod.MAX_LIMIT
        assert len(idx.search_eval("반론보도", limit=20)) == 14
        assert len(idx.search_eval("반론보도", limit=999)) == 14 and cs.EVAL_MAX_LIMIT == 50
    finally:
        idx.close()


def test_reporter_paths_cannot_reach_search_eval():
    users = [p.name for p in ENGINE.rglob("*.py") if re.search(r"search_eval", p.read_text(encoding="utf-8"))]
    assert users == ["corpus_sqlite.py"]                       # 엔진 안에서 정의 파일 말고는 어디서도 부르지 않는다
    assert not hasattr(corpus_mod.Corpus, "search_eval")      # 공통 인터페이스(MCP·review 가 받는 타입)에 없다
    tool = next(t for t in mcp_server._tool_defs() if t["name"] == "search_cases")
    assert tool["inputSchema"]["properties"]["limit"]["maximum"] == corpus_mod.MAX_LIMIT      # 기자에게 노출되는 도구 스키마 상한은 그대로

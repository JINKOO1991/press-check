"""동형어 평가(evals/run_search_evals.py homograph, evals/search/homograph_v1.json): 라벨 규칙·판정·실행 출력. 합성 색인만 쓴다."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))
import test_eng_search_rank as sr  # noqa: E402
import run_search_evals as rs  # noqa: E402

WORDS = {"고의", "실명", "공인", "사과", "보도", "사고", "의도", "기자", "인용", "초상", "수사", "청구"}


def entries():
    return {e["term"]: e for e in rs.load_homograph(rs.HOMOGRAPH_FILE)["terms"]}


def test_homograph_file_has_twelve_preliminary_labelled_words_and_no_passage_text():
    doc = rs.load_homograph(rs.HOMOGRAPH_FILE)
    assert doc["label_source"] == "llm_preliminary" and "사람 검수 전" in doc["note"]
    assert {e["term"] for e in doc["terms"]} == WORDS
    raw = rs.HOMOGRAPH_FILE.read_text(encoding="utf-8")
    assert "queries" not in json.loads(raw)                          # 질의 세트가 아니라 낱말·라벨 규칙 표다(passage 본문·발췌 없음)
    assert all(len(e["term"]) == 2 for e in doc["terms"])


def test_true_word_rules():
    e = entries()
    assert rs.is_true_word(e["고의"], "고의로") and rs.is_true_word(e["고의"], "고의")
    assert not rs.is_true_word(e["고의"], "원고의") and not rs.is_true_word(e["고의"], "피고의")
    assert rs.is_true_word(e["고의"], "과실고의")
    assert rs.is_true_word(e["실명"], "실명을") and not rs.is_true_word(e["실명"], "금융실명제")
    assert not rs.is_true_word(e["의도"], "여의도") and rs.is_true_word(e["의도"], "의도적")
    assert not rs.is_true_word(e["청구"], "신청구") and rs.is_true_word(e["청구"], "정정보도청구")
    assert not rs.is_true_word(e["사과"], "고의")                      # 낱말이 없으면 거짓


def test_judge_homograph_counts_results_without_any_true_word():
    e = entries()["실명"]
    res = [{"text": "금융실명제 위반 사건이다"}, {"text": "피해자의 실명을 공개한 보도"}, {"text": "실명 거래 금지"}]
    assert rs.judge_homograph(e, res) == {"forbidden@10": 1, "n": 3}


def test_homograph_command_runs_on_synthetic_index_and_hides_pilot_text(tmp_path):
    idx, _ = sr.build(tmp_path / "w", [("a", "원고의 청구를 기각한다는 취지의 사건이다"), ("b", "고의로 명예를 훼손한 사건이다 실명 보도")])
    path = Path(idx._path)
    idx.close()
    pilot = tmp_path / "pilot.json"
    pilot.write_text(json.dumps(["고의 명예훼손 판단", "실명 보도 책임"]), encoding="utf-8")
    out = tmp_path / "out.json"
    assert rs.main(["homograph", "--index", str(path), "--pilot-file", str(pilot), "--out", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert set(doc["terms"]) == WORDS and doc["terms"]["고의"]["forbidden@10"] == 1 and doc["terms"]["고의"]["n"] == 2
    assert doc["label_source"] == "llm_preliminary" and doc["totals"]["forbidden@10"] >= 1
    assert list(doc["pilot"]) == ["pilot-01", "pilot-02"] and all(v["ids"] for v in doc["pilot"].values())
    assert "명예훼손" not in out.read_text(encoding="utf-8")            # 로컬 질의 문구는 출력에 남기지 않는다

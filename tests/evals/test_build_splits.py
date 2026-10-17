"""P4 V1: 같은 그룹은 한 분할에만 들어가고, test 의 매체 키는 tune·dev 에 나오지 않는다. 합성 노드만 쓴다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))
import build_splits as bs  # noqa: E402


def node(cid, nums=(), hist=(), media=(), text=""):
    return {"case_id": cid, "doc_id": "d" + cid, "auth": "pac_decision", "numbers": list(nums), "decision_date": None, "history": list(hist), "text": text,
            "media": list(media), "has_media_role": bool(media), "orig": []}


def test_groups_never_span_splits_and_test_media_is_separated():
    nodes = [node(f"c{i:03d}", nums=[f"2010서울조정{i}"], media=["가나일보"] if i % 7 == 0 else []) for i in range(300)]
    nodes += [node("x1", nums=["2011다1"], hist=["2010서울조정5"]), node("x2", nums=["2010서울조정5"])]          # 심급 연결: x1, x2 와 c005 는 한 그룹
    nodes += [node("t1", text="가" * 40 + "나다라마바사아자차카타파하" * 3), node("t2", text="가" * 40 + "나다라마바사아자차카타파하" * 3 + "끝")]  # 같은 원기사
    root, info = bs.build_groups(nodes, [])
    assert root["x1"] == root["x2"] == root["c005"] and root["t1"] == root["t2"] and info["unions_by_rule"]["appeal_history"] >= 1
    groups: dict = {}
    for cid, r in root.items():
        g = groups.setdefault("G-" + r, {"cases": [], "media": set()})
        g["cases"].append(cid)
        g["media"] |= set(next(n for n in nodes if n["case_id"] == cid)["media"])
    groups = {k: {"cases": v["cases"], "media": sorted(v["media"])} for k, v in groups.items()}
    bs.assign_splits(groups)
    seen = {}
    for gid, g in groups.items():
        for c in g["cases"]:
            assert seen.setdefault(c, g["split"]) == g["split"]                       # 한 사건은 한 분할
    assert len(seen) == len(nodes) and {g["split"] for g in groups.values()} >= {"test", "tune", "dev"}
    test_media = {k for g in groups.values() if g["split"] == "test" for k in g["media"]}
    assert not [g for g in groups.values() if g["split"] in ("tune", "dev") and set(g["media"]) & test_media]

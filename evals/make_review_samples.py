"""P4 V1 사람 확인용 표본 파일(로컬 전용). 저장소에 올리지 않는다.
①같은 원기사 판정 표본: 5-gram 유사도 0.5~0.7 근처의 '묶임'(>=임계) 20쌍·'분리'(<임계) 20쌍. 실명 의심 표지가 있으면 발췌를 생략한다.
②매체 별칭 후보: 별칭 후보와 매체 키 상위 30개(표기 변형·부분문자열 후보 포함).
build_splits.py 와 같은 읽기 전용 입력을 쓴다. 결정적(정렬 후 해시 순서 추출)."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_splits as bs  # noqa: E402

_NAME_HINT = re.compile(r"[가-힣]{2,4}\s?(?:씨|의원|후보|대표|회장|장관|시장|지사|교수|판사|검사|변호사|대통령|위원장|총리|의장|구청장|군수|사장|부장|기자)|\d{2,3}-\d{3,4}-\d{4}|\d{6}-\d{7}|[\w.]+@[\w.]+")


def pii_suspect(t: str) -> bool:
    return bool(_NAME_HINT.search(t))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--pairs", type=int, default=20)
    ap.add_argument("--media", type=int, default=30)
    a = ap.parse_args(argv)
    dd = bs.data_dir()
    out = Path(a.out_dir) if a.out_dir else dd / "eval" / "review"
    out.mkdir(parents=True, exist_ok=True)
    aliases_doc = json.loads((bs.HERE / "split" / "media_aliases.json").read_text(encoding="utf-8"))
    m = bs.open_manifest(dd / "corpus" / "manifest.sqlite")
    try:
        nodes, _, info = bs.load_nodes(m, aliases_doc["aliases"])
    finally:
        m.rollback(); m.close()
    by_id = {n["case_id"]: n for n in nodes}
    scores, _, _ = bs.pair_scores(nodes)
    def trivial(x, y):         # 같은 사건번호를 공유하거나(번호가 없으면 같은 문서) 이미 같은 사건으로 묶이는 자명한 쌍
        nx, ny = by_id[x]["numbers"], by_id[y]["numbers"]
        return bool(set(nx) & set(ny)) or (not nx and not ny and by_id[x]["doc_id"] == by_id[y]["doc_id"])
    linked_all = [((a_, b_), j) for (a_, b_), j in scores.items() if 0.6 <= j < 0.7]
    split_all = [((a_, b_), j) for (a_, b_), j in scores.items() if 0.5 <= j < 0.6]
    linked = [r for r in linked_all if not trivial(*r[0])]
    split = [r for r in split_all if not trivial(*r[0])]
    n_trivial = {"linked": len(linked_all) - len(linked), "split": len(split_all) - len(split)}
    lines = ["# 같은 원기사 판정 표본 (로컬 전용, 저장소에 올리지 않음)", "",
             "각 쌍의 두 기록이 **같은 원기사(같은 보도)에서 나온 것인가**를 O/X 로 적어 주세요. 자동 규칙은 5-gram 유사도 0.6 이상이면 '묶임'으로 봅니다.",
             "같은 사건번호를 공유해 이미 같은 사건으로 묶이는 자명한 쌍은 뺐습니다. 실명·직함 표지가 보이는 발췌는 생략했습니다(문서 ID·사건번호로 확인). 발췌는 문제 구간 인용 앞 200자이며 원문은 언론사 저작물이라 이 파일 밖으로 옮기지 마세요.", ""]
    n_omit = 0
    for title, rows, tag in (("묶임 쌍(유사도 0.6~0.7)", linked, "linked"), ("분리 쌍(유사도 0.5~0.6)", split, "split")):
        lines += [f"## {title}", "", "| 쌍 ID | 유사도 | 기록 A (문서 / 사건번호) | 기록 B (문서 / 사건번호) | A 발췌 | B 발췌 | O/X |", "|---|---|---|---|---|---|---|"]
        chosen = sorted(rows, key=lambda r: hashlib.sha256(f"{bs.SEED}|review|{tag}|{r[0][0]}|{r[0][1]}".encode()).hexdigest())[:a.pairs]
        for k, ((x, y), j) in enumerate(chosen, 1):
            cells, reason = [], []
            for c in (x, y):
                t = info["excerpts"].get(c, "")
                if pii_suspect(t):
                    cells.append("(생략: 실명·직함 의심)"); n_omit += 1
                else:
                    cells.append(t.replace("|", "/").replace("\n", " "))
            lines.append(f"| {tag[0].upper()}{k:02d} | {j:.2f} | {by_id[x]['doc_id']} / {','.join(by_id[x]['numbers']) or '-'} | {by_id[y]['doc_id']} / {','.join(by_id[y]['numbers']) or '-'} | {cells[0]} | {cells[1]} |  |")
        lines.append("")
    lines.append(f"(참고: 같은 사건 안 자명한 쌍은 표본에서 뺐다. 0.6~0.7 구간 {n_trivial['linked']}쌍, 0.5~0.6 구간 {n_trivial['split']}쌍.)")
    (out / "same_article_sample.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    mk = sorted(((k, sum(c.values())) for k, c in info["media_raw"].items()), key=lambda kv: (-kv[1], kv[0]))
    keys = [k for k, _ in mk]
    al = ["# 매체 별칭 후보 (로컬 전용)", "", "같은 매체인데 표기가 갈려 있으면 '합칠 키'에 대표 이름을 적어 주세요. 비워 두면 그대로 둡니다. 합친 결과는 test 분리 때만 쓰입니다.", "",
          "## 별칭 후보(현재 표에 있음, 미확인)", "", "| 표기 키 | → 대표 키 | 건수 | 맞음 O/X |", "|---|---|---|---|"]
    for src, dst in sorted(aliases_doc["aliases"].items()):
        al.append(f"| {src} | {dst} | {sum(info['media_raw'].get(src, {}).values()) if src in info['media_raw'] else 0} |  |")
    al += ["", f"## 건수 상위 매체 키 {a.media}개", "", "| # | 매체 키 | 사건 수 | 원 표기(건수) | 부분 일치하는 다른 키(합칠 후보) | 합칠 키 |", "|---|---|---|---|---|---|"]
    for i, (k, n) in enumerate(mk[:a.media], 1):
        raw = ", ".join(f"{l}({c})" for l, c in info["media_raw"][k].most_common(4))
        near = [o for o in keys if o != k and len(min(o, k, key=len)) >= 2 and (o in k or k in o)][:4]
        al.append(f"| {i} | {k} | {n} | {raw} | {', '.join(near) or '-'} |  |")
    (out / "media_alias_sample.md").write_text("\n".join(al) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps({"out": str(out), "pairs_available": {"linked_0.6_0.7": len(linked), "split_0.5_0.6": len(split)}, "trivial_same_case_excluded": n_trivial, "excerpts_omitted": n_omit}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

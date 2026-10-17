"""P4 V2 점검 도구(P4_DESIGN §3.3 3단계): 합성 기사가 출처 사건 텍스트를 베꼈는지, 실명 단서가 남았는지 본다.

영구 테스트가 아니라 시나리오를 추가할 때 손으로 돌리는 점검기다. 법률 판단을 하지 않는다.

- 공통 부분문자열: card_from_case 시나리오마다 출처 그룹의 모든 색인 passage(split_v1_members.json), 그룹 사건의 manifest
  record_json 문자열 필드, 로컬 사실 카드와 기사 전체(제목·부제·본문·캡션)의 최장 공통 부분문자열 길이를 잰다.
  원문 그대로와 공백 제거본 두 가지로 잰다. 기준 미만이면 통과(기본 15자).
- 실명 단서: (a) 로컬 매체 별칭 키(split_v1_media_keys.json, 내용은 출력하지 않고 적중 수만 낸다),
  (b) 출처 passage 에 나오는 매체·법원·검찰청 이름, (c) 실제 시·군·구 이름(주요 시 목록)과 시·도 단위 실존 기관명(시·도 이름 자체는 허용, 사용자 결정 2026-10-10), (d) 한글 이름 + 호칭 패턴(성씨로 시작하는 2~3자 + 호칭. 오탐이 많아 실패로 세지 않고 검수 후보로만 낸다).
- 직위+지역+시기 조합에 의한 식별은 자동으로 보지 않는다(사람 검수 항목).

읽기 전용: 색인은 mode=ro&immutable=1, manifest 는 mode=ro(수신 중 WAL 이라 immutable 금지) + query_only.
출력은 JSON 한 덩어리. 실패가 있으면 종료 코드 1.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

import evalkit  # noqa: F401  (엔진 경로)
from press_review import config, corpus_sqlite as cs

HERE = Path(__file__).resolve().parent
_WS = re.compile(r"\s+")
PROVINCES = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기도", "강원", "충북", "충남", "충청", "전북", "전남", "전라",
             "경북", "경남", "경상", "제주")                    # 시·도 이름: 허용(사용자 결정 2026-10-10)
CITIES = ("수원", "창원", "고양", "용인", "성남", "청주", "전주", "천안", "포항", "김해", "안산", "안양",
          "남양주", "화성시", "평택", "의정부", "파주", "시흥", "김포", "춘천", "원주", "강릉", "목포", "여수", "순천", "군산", "익산",
          "구미", "경주", "안동", "진주", "통영", "거제", "서귀포")          # 실제 시 이름: 차단
REGIONS = PROVINCES + CITIES                                     # 하위 호환용 전체 목록
_PROV_STEM = "서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충청북|충청남|충북|충남|전라북|전라남|전북|전남|경상북|경상남|경북|경남|제주"
REAL_AGENCY = re.compile(r"(?:" + _PROV_STEM + r")(?:특별시|광역시|특별자치시|특별자치도|도)?(?:중앙|동부|서부|남부|북부)?\s?"
                         r"(?:지방경찰청|경찰청|교육청|고등검찰청|지방검찰청|검찰청|지검|고등법원|지방법원|지법|도청|시청|도의회|시의회|"
                         r"동물위생시험소|보건환경연구원|소방본부)")


def region_hits(text: str) -> dict:
    """실제 시 이름과 시·도 단위 실존 기관명 적중. 시·도 이름만 쓴 것은 적중이 아니다."""
    return {"regions": [r for r in CITIES if r in text], "real_agencies": sorted({m.group(0) for m in REAL_AGENCY.finditer(text)})}


TITLES = r"(씨|의원|이사장|회장|원장|과장|기자|대표|교수|변호사|세무사|건축사|시장|군수|검사|판사|위원장)"
SURNAMES = set("김이박최정강조윤장임한오서신권황안송류홍전고문양손배백허유남심노하곽성차주우구민진나지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예경봉사부")
NAME_TITLE = re.compile(r"(?<![가-힣])([가-힣]{2,3})\s?" + TITLES)
VERB_END = tuple("은는을를이가의에해긴운한된던던며고서도")
ROLE_WORDS = {"직원", "학부모", "관계자", "사무국", "당사자", "고객", "제보자", "원내대변인", "다수당", "의원실", "변호인", "농가", "조사관",
              "수사팀", "회원", "측근", "대변인", "주민", "시민", "동호인", "참고인", "이사회", "사무처", "건축", "본인", "원고", "피고"}
OUTLET = re.compile(r"[가-힣A-Za-z]{2,10}(?:일보|신문|방송|뉴스|닷컴|문화방송|통신)|[가-힣]{2,6}(?:지방법원|고등법원|지방검찰청|지검|경찰서)")


def lcs(a: str, b: str) -> tuple[int, str]:
    """최장 공통 부분문자열 길이와 예시. k-gram 집합 + 이분 탐색(길이 k가 있으면 k-1도 있다)."""
    def common(k):
        if k == 0:
            return ""
        grams = {b[i:i + k] for i in range(len(b) - k + 1)}
        for i in range(len(a) - k + 1):
            if a[i:i + k] in grams:
                return a[i:i + k]
        return None
    lo, hi, best = 0, min(len(a), len(b)), ""
    while lo < hi:
        mid = (lo + hi + 1) // 2
        s = common(mid)
        if s is not None:
            lo, best = mid, s
        else:
            hi = mid - 1
    return lo, best


def strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, list):
        return [s for x in obj for s in strings(x)]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in strings(v)]
    return []


def main() -> int:
    dd = config.data_dir()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenarios", default=str(HERE / "p4_pilot_batch1.json"))
    ap.add_argument("--min-lcs", type=int, default=15, help="이 길이 이상 공통 부분문자열이 있으면 실패")
    ap.add_argument("--manifest", default=str(dd / "corpus" / "manifest.sqlite"))
    ap.add_argument("--index", default=None)
    ap.add_argument("--local", default=str(dd / "eval"), help="split_v1_*.json, p4_cards_v1.json 이 있는 로컬 디렉터리")
    a = ap.parse_args()

    od = cs.index_dir()
    index = Path(a.index) if a.index else od / (od / cs.CURRENT_POINTER).read_text(encoding="utf-8").strip()
    ix = sqlite3.connect(index.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    mf = sqlite3.connect(Path(a.manifest).resolve().as_uri() + "?mode=ro", uri=True)
    mf.execute("PRAGMA query_only=1")
    local = Path(a.local)
    members = json.loads((local / "split_v1_members.json").read_text(encoding="utf-8"))
    assign = json.loads((local / "split_v1_assignment.json").read_text(encoding="utf-8"))["cases"]
    cards_path = local / "p4_cards_v1.json"
    cards = json.loads(cards_path.read_text(encoding="utf-8")) if cards_path.exists() else {}
    media_keys = [k for k in json.loads((local / "split_v1_media_keys.json").read_text(encoding="utf-8")) if len(k) >= 2]

    data = json.loads(Path(a.scenarios).read_text(encoding="utf-8"))
    rows, failed = [], 0
    for sc in data["cases"]:
        art = json.loads((Path(a.scenarios).parent / sc["article"]).read_text(encoding="utf-8"))
        text = "\n".join(b["text"] for b in art["blocks"])
        flat = _WS.sub("", text)
        pv = sc.get("provenance") or {}
        row = {"id": sc["id"], "derivation": pv.get("derivation"), "chars": len(text)}
        sources: list[tuple[str, str]] = []
        if pv.get("derivation") == "card_from_case":
            gid = pv.get("source_group_id")
            g = members.get(gid) or {}
            row["group_split"] = g.get("split")
            for pid in g.get("passage_ids", []):
                r = ix.execute("select text from passages where passage_id=?", (pid,)).fetchone()
                if r:
                    sources.append((f"passage:{pid}", r[0]))
            for cid in sorted(c for c, v in assign.items() if v["group"] == gid):
                r = mf.execute("select record_json from cases where case_id=?", (cid,)).fetchone()
                if r:
                    sources.append((f"record:{cid}", "\n".join(strings(json.loads(r[0])))))
            ref = (pv.get("card_ref") or "").split("#")[-1]
            if ref in cards:
                sources.append((f"card:{ref}", "\n".join(strings(cards[ref]))))
            row["sources"] = len(sources)
            best = (0, "", "")
            best_flat = (0, "", "")
            for name, src in sources:
                n, s = lcs(text, src)
                if n > best[0]:
                    best = (n, s, name)
                n2, s2 = lcs(flat, _WS.sub("", src))
                if n2 > best_flat[0]:
                    best_flat = (n2, s2, name)
            row["max_lcs"] = {"len": best[0], "text": best[1], "source": best[2]}
            row["max_lcs_nospace"] = {"len": best_flat[0], "text": best_flat[1], "source": best_flat[2]}
            row["lcs_ok"] = best[0] < a.min_lcs and best_flat[0] < a.min_lcs
            row["split_ok"] = g.get("split") == sc.get("split") == "dev"
        src_names = sorted({m.group(0) for _, s in sources for m in OUTLET.finditer(s)})
        hits = {"media_keys": sum(1 for k in media_keys if k in text),
                "source_outlets_or_courts": [n for n in src_names if n in text],
                **region_hits(text),
                "name_title_candidates": sorted({m.group(0) for m in NAME_TITLE.finditer(text)
                                          if m.group(1) not in ROLE_WORDS and m.group(1)[0] in SURNAMES and not m.group(1).endswith(VERB_END)})}
        row["name_hits"] = hits
        row["names_ok"] = not (hits["media_keys"] or hits["source_outlets_or_courts"] or hits["regions"] or hits["real_agencies"])      # 이름+호칭 후보는 사람 검수용 신호(실패 아님)
        ok = row["names_ok"] and row.get("lcs_ok", True) and row.get("split_ok", True)
        failed += not ok
        row["ok"] = ok
        rows.append(row)
    out = {"scenarios": len(rows), "failed": failed, "min_lcs": a.min_lcs, "index": index.name,
           "not_checked": "직위+지역+시기 조합 식별(사람 검수)", "rows": rows}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

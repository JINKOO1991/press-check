"""수정안(replacement) 결정적 검사: 새 사실이 들어갔는지 기계적으로 가려낸다.

판정은 경고(warning)가 기본이다. 의미 판단은 호스트 모델과 사람의 몫이고, 이 모듈은 흔한 사고만 막는다.
다음 둘은 오류(error)다: 원문에 없는 숫자 추가, 직접인용 구간 내부 변경(새 따옴표 인용 포함).

이식(숫자 검사 부분): 작성자 소유 코드(비공개)의 ``_NUM``, ``_nocomma``, ``num_in`` 을 가져왔다.
같은 원본 파일의 ``norm``/``check_e2``의 인용 포함 검사는
NFKC·공백·따옴표를 지우는 느슨한 검사라서 위치·인용 일치(정규화 금지)에는 쓰지 않는다.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .textspan import find_all

# --- 이식: 작성자 소유 코드(비공개) -----------------------------
_NUM = re.compile(r"\d[\d]*(?:\.\d+)?")


def _nocomma(s):
    return re.sub(r"(?<=\d),(?=\d)", "", unicodedata.normalize("NFKC", s or ""))


def num_in(tok, hay):
    return re.search(r"(?<![\d.,])" + re.escape(tok) + r"(?!\d|[.,]\d)", hay) is not None
# --- 이식 끝 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    code: str
    level: str  # "error" | "warning"
    message: str


def has_errors(issues: list[Issue]) -> bool:
    return any(i.level == "error" for i in issues)


# ---------------------------------------------------------------- 인용 구간

_PAIRS = {"“": ("”", "direct"), "「": ("」", "direct"), "『": ("』", "direct"), "〝": ("〞", "direct"), "«": ("»", "direct"), "‘": ("’", "single")}
_SYMMETRIC = ('"', "＂")                       # 열고 닫는 글자가 같은 직접인용 부호(ASCII 와 전각)
_DIRECT_CLOSERS = frozenset(v[0] for v in _PAIRS.values() if v[1] == "direct")


_CLOSER_TO_OPENERS: dict[str, tuple[str, ...]] = {}
for _o, (_c, _k) in _PAIRS.items():
    _CLOSER_TO_OPENERS[_c] = _CLOSER_TO_OPENERS.get(_c, ()) + (_o,)


def scan_quotes(text: str, resets: frozenset | set = frozenset()) -> list[tuple[int, int, str]]:
    """따옴표 구간 전부(중첩 안쪽 구간 포함, 시작 위치순). 열린 따옴표를 쌓아 두고 닫는 부호가 나오면 짝이 되는 가장 가까운 열림까지 꺼낸다.

    닫히지 않은 따옴표는 구간이 아니다. ``resets`` 는 스택을 비우는 위치(예: 섹션이 바뀌는 블록의 시작)라서, 한 블록에서 닫히지 않은 따옴표가 먼 뒤쪽 인용의 닫는 부호와 짝지어지지 않는다.
    """
    out: list[tuple[int, int, str]] = []
    stack: list[tuple[str, int]] = []              # (여는 글자, 위치)
    for i, c in enumerate(text):
        if i in resets:
            stack.clear()
        if c in _SYMMETRIC:
            for d in range(len(stack) - 1, -1, -1):
                if stack[d][0] == c:
                    out.append((stack[d][1], i + 1, "direct"))
                    del stack[d:]
                    break
            else:
                stack.append((c, i))
        elif c in _PAIRS:
            stack.append((c, i))
        elif c in _CLOSER_TO_OPENERS:
            for d in range(len(stack) - 1, -1, -1):
                if stack[d][0] in _CLOSER_TO_OPENERS[c]:
                    out.append((stack[d][1], i + 1, _PAIRS[stack[d][0]][1]))
                    del stack[d:]
                    break
    out.sort()
    return out


def quote_regions(text: str) -> list[tuple[int, int, str]]:
    """따옴표로 감싼 바깥 구간 [시작, 끝) (따옴표 포함)과 종류('direct' | 'single').

    direct: “…” 「…」 『…』 〝…〞 «…» "…" ＂…＂(쌍으로 짝지음). single: ‘…’(제목·용어 강조에도 쓰여 경고 수준).
    닫히지 않은 따옴표는 구간으로 보지 않는다. 중첩은 바깥 구간 하나로 센다(안쪽까지 보려면 ``scan_quotes``).
    """
    out: list[tuple[int, int, str]] = []
    end = -1
    for r in scan_quotes(text):
        if r[0] >= end:
            out.append(r)
            end = r[1]
    return out


def has_unpaired_direct_mark(text: str) -> bool:
    """quote_regions 로 짝지어지지 않고 남은 직접인용 부호(여는 쪽·닫는 쪽 모두)가 있으면 True. ‘’ 는 아포스트로피와 겹쳐 보지 않는다."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in _PAIRS and _PAIRS[c][1] == "direct":
            j = text.find(_PAIRS[c][0], i + 1)
            if j == -1:
                return True
            i = j + 1
            continue
        if c in _SYMMETRIC:
            j = text.find(c, i + 1)
            if j == -1:
                return True
            i = j + 1
            continue
        if c in _DIRECT_CLOSERS:
            return True
        i += 1
    return False


# ---------------------------------------------------------------- 부정어

_NEG = re.compile(r"않|없|아니|아닌|아님|아냐|아닙|(?<![가-힣])안(?=\s|$)|(?<![가-힣])못(?=\s|하|했|되|할|한|함|해)")


def count_negations(s: str) -> int:
    """부정어 개수. 안/않/없/아니(아닌·아님·아냐·아닙 포함)/못. 불·무 접두(불가능, 무관 등)는 세지 않는다."""
    return len(_NEG.findall(s or ""))


# ---------------------------------------------------------------- 고유명사 후보

_SPLIT = re.compile(r"[\s,.;:!?…·()\[\]{}“”‘’\"＂〝〞«»'「」『』~\-–—/]+")
_JOSA = re.compile(r"(?:에서|에게|으로|부터|까지|조차|마저|보다|처럼|은|는|이|가|을|를|의|에|로|와|과|도|만)$")
_NAME_SUFFIX = re.compile(r"[가-힣]{1,5}(?:씨|군|양|그룹|공사|공단|위원회|협회|은행|증권|전자|제약|건설|병원)")
_HAS_UPPER = re.compile(r"[A-Z]")


def proper_noun_candidates(s: str) -> list[str]:
    out: list[str] = []
    for tok in _SPLIT.split(s or ""):
        if not tok:
            continue
        base = _JOSA.sub("", tok) or tok
        for cand in (tok, base):
            if _HAS_UPPER.search(cand) or _NAME_SUFFIX.fullmatch(cand):
                if cand not in out:
                    out.append(cand)
                break
    return out


# ---------------------------------------------------------------- 숫자+단위

_UNITS = "퍼센트포인트|퍼센트|달러|위안|유로|%|원|억|만|천|백|조|명|건|개|세|살|년|월|일|시|분|초|배|위|곳|대|차|회|번|엔|km|kg|m"
_NUM_UNIT = re.compile(r"(\d[\d]*(?:\.\d+)?)\s*(" + _UNITS + ")")


def _num_unit_pairs(s: str) -> set[str]:
    return {a + b for a, b in _NUM_UNIT.findall(_nocomma(s))}


def _quote_kept(text: str, s: int, e: int, rs: int, re_: int, replacement: str) -> bool:
    """수정 구간 [s, e)가 인용 [rs, re_)의 일부만 덮을 때, 인용 안쪽 문구가 그대로 남는지 위치까지 본다.

    - 열린 따옴표가 구간 밖이고 닫는 따옴표도 구간 밖(구간이 인용 안쪽에 완전히 들어감): 어떤 변경이든 인용 변경이다.
    - 열린 따옴표만 구간 밖: 교체문은 인용의 남은 부분(닫는 따옴표까지)으로 시작해야 한다. 앞에 붙는 글은 인용 안쪽에 들어간다.
    - 닫는 따옴표만 구간 밖: 교체문은 인용의 앞부분(여는 따옴표부터)으로 끝나야 한다. 뒤에 붙는 글은 인용 안쪽에 들어간다.
    """
    opens_outside = rs < s
    closes_outside = e < re_
    if opens_outside and closes_outside:
        return False
    if opens_outside:
        return replacement.startswith(text[s:re_])
    return replacement.endswith(text[rs:e])


# ---------------------------------------------------------------- 본체

def _article_blocks(article: Any) -> list[tuple[str, str]]:
    return [(b["block_id"], b["text"]) for b in article["blocks"]]


def check_replacement(
    quote: str,
    replacement: str,
    article: Any,
    *,
    block_id: str | None = None,
    start: int | None = None,
) -> list[Issue]:
    """quote(원문 구절)를 replacement로 바꿀 때의 검사. block_id/start를 주면 그 위치만, 없으면 quote의 모든 출현을 본다."""
    issues: list[Issue] = []
    blocks = _article_blocks(article)
    full = "\n".join(t for _, t in blocks)

    if not replacement:
        return [Issue("EMPTY_REPLACEMENT", "error", "replacement is empty")]
    if replacement == quote:
        return [Issue("NO_CHANGE", "warning", "replacement equals the original text")]

    # 숫자: 새 숫자는 오류, 사라진 숫자·바뀐 단위는 경고
    hay = _nocomma(full)
    rep_nums = _NUM.findall(_nocomma(replacement))
    seen: set[str] = set()
    for tok in rep_nums:
        if tok in seen:
            continue
        seen.add(tok)
        if not num_in(tok, hay):
            issues.append(Issue("NEW_NUMBER", "error", f"number '{tok}' does not appear in the article"))
    rep_n = _nocomma(replacement)
    for tok in dict.fromkeys(_NUM.findall(_nocomma(quote))):
        if not num_in(tok, rep_n):
            issues.append(Issue("NUMBER_REMOVED", "warning", f"number '{tok}' in the original is missing from the replacement"))
    art_pairs = _num_unit_pairs(full)
    for pair in sorted(_num_unit_pairs(replacement) - art_pairs):
        tok = _NUM.match(pair).group(0)
        if num_in(tok, hay):
            issues.append(Issue("NUMBER_UNIT_CHANGED", "warning", f"'{pair}' (number with unit) does not appear in the article"))

    # 따옴표: 새 인용 오류, 새 ‘’ 강조는 경고
    full_compact = re.sub(r"\s+", "", full)
    for rs, re_, kind in scan_quotes(replacement):             # CX1-3: 중첩된 안쪽 인용도 본다
        inner = replacement[rs + 1:re_ - 1]
        compact = re.sub(r"\s+", "", inner)
        if not compact:
            continue
        if kind == "direct":
            if inner not in full:                              # CX1-2: 원고의 직접인용은 공백까지 원문 그대로 비교한다(G-D13 의 공백 제거 대조는 수집 자료용)
                issues.append(Issue("NEW_QUOTE", "error", f"quoted text “{inner[:30]}” does not appear in the article"))
        elif compact not in full_compact:
            issues.append(Issue("NEW_SINGLE_QUOTE", "warning", f"‘…’ text '{inner[:30]}' does not appear in the article"))

    # 직접인용 구간 내부 변경
    if isinstance(block_id, str) and isinstance(start, int) and not isinstance(start, bool):
        occurrences = [(block_id, start, start + len(quote))]
    else:
        occurrences = [(bid, s, e) for bid, t in blocks for (s, e) in find_all(t, quote)]
    text_of = dict(blocks)
    # CX1-3: 인용 상태를 블록 경계 너머로 추적한다(블록을 \n 으로 이은 전체에서 스캔, 섹션이 바뀌는 곳에서만 끊는다). 위치는 전체 기준으로 옮겨 비교한다.
    offsets: dict[str, int] = {}
    resets: set[int] = set()
    pos = 0
    prev_section = None
    for b in (article["blocks"] if isinstance(article, dict) else []):
        offsets.setdefault(b["block_id"], pos)
        if prev_section is not None and b.get("section") != prev_section:
            resets.add(pos)
        prev_section = b.get("section")
        pos += len(b["text"]) + 1
    regions_full = scan_quotes(full, resets)
    reported: set[tuple[str, int]] = set()
    for bid, s, e in occurrences:
        btext = text_of.get(bid, "")
        if btext[s:e] != quote:
            continue
        g = offsets.get(bid, 0)
        s, e = s + g, e + g
        text = full
        contained: list[tuple[int, int]] = []       # 수정 구간 안에 통째로 들어간 직접인용
        partial = False                              # 일부만 겹치는 인용이 있는지(있으면 교체문의 짝 없는 부호가 정당할 수 있다)
        for rs, re_, kind in regions_full:
            if re_ <= s or rs >= e:
                continue
            if s <= rs and re_ <= e:
                if kind == "direct":
                    contained.append((rs, re_))
                continue  # 인용 전체가 수정 구간 안: 아래 V1 검사로 다룬다
            partial = True
            if _quote_kept(text, s, e, rs, re_, replacement):
                continue
            key = (bid, rs)
            if key in reported:
                continue
            reported.add(key)
            if kind == "direct":
                issues.append(Issue("DIRECT_QUOTE_CHANGED", "error", f"edit changes text inside a direct quote in block '{bid}'"))
            else:
                issues.append(Issue("SINGLE_QUOTE_CHANGED", "warning", f"edit changes text inside ‘…’ in block '{bid}'"))
        if contained and (bid, contained[0][0]) not in reported:
            # V1: 수정 구간이 직접인용을 통째로 덮을 때, 교체문은 그 인용을 없애거나 안쪽 문구를 그대로 두어야 한다.
            allowed = {text[a + 1:b - 1] for a, b in contained}               # CX1-2: 공백까지 원문 그대로
            changed = any(k == "direct" and replacement[a + 1:b - 1] not in allowed
                          for a, b, k in scan_quotes(replacement))
            if changed or (not partial and has_unpaired_direct_mark(replacement)):
                reported.add((bid, contained[0][0]))
                issues.append(Issue("DIRECT_QUOTE_CHANGED", "error", f"edit replaces a whole direct quote in block '{bid}' with text other than its own words"))

    # 부정어
    nq, nr = count_negations(quote), count_negations(replacement)
    if nq != nr:
        issues.append(Issue("NEGATION_CHANGED", "warning", f"negation count changed {nq} -> {nr}"))

    # 고유명사 후보
    for cand in proper_noun_candidates(replacement):
        if cand not in full:
            issues.append(Issue("NEW_PROPER_NOUN", "warning", f"proper-noun candidate '{cand}' does not appear in the article"))

    issues.sort(key=lambda i: 0 if i.level == "error" else 1)
    return issues


# ---------------------------------------------------------------- 흐름 검사(P3_DESIGN §3)

# 형사 절차 단계 어휘(작은 수일수록 앞 단계). 어휘 일치만 보고 의미는 판단하지 않는다. '확정' 같은 말은 법적 의미가 아니어도 걸릴 수 있어 경고 수준이다.
_STAGES: tuple[tuple[int, str, re.Pattern], ...] = (
    (1, "혐의", re.compile(r"혐의|의혹|고소|고발|내사|입건|피의자")),
    (2, "수사", re.compile(r"수사|압수수색|소환|체포|구속영장")),
    (3, "기소", re.compile(r"기소|공소|재판에\s*넘")),
    (4, "재판", re.compile(r"재판|공판|변론|1심|2심|항소|상고")),
    (5, "판결", re.compile(r"판결|선고|유죄|무죄|징역|실형|집행유예|벌금형")),
    (6, "확정", re.compile(r"확정")),
)
_STAGE_NAME = {r: n for r, n, _ in _STAGES}
# 절차가 끝났거나 이어지지 않는 표현은 '기소' 같은 단계어로 세지 않는다(수사 단계에서 종결된 것으로 본다).
_DISPOSITION = re.compile(r"불기소|무혐의|혐의\s*없음|기소\s*유예|공소권\s*없음|기소\s*중지")
_HEADLINE_SECTIONS = ("title", "subtitle")
# 제목의 단계가 본문 최대 단계보다 앞서가면 경고하는 최소 단계(기소 이상). 혐의·수사 사이의 표현 차이는 기계적으로 가르지 않는다.
_TITLE_ALERT_MIN = 3


def _stages_in(text: str) -> set[int]:
    t = _DISPOSITION.sub(" ", text or "")
    out = {r for r, _, rx in _STAGES if rx.search(t)}
    if _DISPOSITION.search(text or ""):
        out.add(2)
    return out


def _apply_edits(article: Any, edits: list[dict]) -> tuple[dict[str, str], list[Issue]]:
    """수정안을 블록별로 적용한 텍스트. 겹치거나 범위가 틀린 수정은 적용하지 않고 Issue 로 알린다."""
    texts = {b["block_id"]: b["text"] for b in article["blocks"]}
    issues: list[Issue] = []
    per: dict[str, list[tuple[int, int, str, str]]] = {}
    for k, e in enumerate(edits):
        bid, s, en, rep = e.get("block_id"), e.get("start"), e.get("end"), e.get("replacement")
        label = str(e.get("label") or f"edit[{k}]")
        if (bid not in texts or not isinstance(s, int) or not isinstance(en, int) or isinstance(s, bool) or isinstance(en, bool)
                or not isinstance(rep, str) or not 0 <= s < en <= len(texts[bid])):
            issues.append(Issue("EDIT_INVALID", "error", f"{label}: edit range or block is invalid"))
            continue
        per.setdefault(bid, []).append((s, en, rep, label))
    for bid, items in per.items():
        items.sort(key=lambda x: (x[0], x[1]))
        kept: list[tuple[int, int, str, str]] = []
        far_end, far_label = -1, ""
        for s, en, rep, label in items:
            if s < far_end:
                issues.append(Issue("EDIT_OVERLAP", "error", f"edit conflict in block '{bid}': {label} overlaps {far_label}"))
                continue
            kept.append((s, en, rep, label))
            far_end, far_label = en, label
        t = texts[bid]
        for s, en, rep, _ in sorted(kept, reverse=True):
            t = t[:s] + rep + t[en:]
        texts[bid] = t
    return texts, issues


def _group_stages(article: Any, texts: dict[str, str]) -> tuple[set[int], set[int]]:
    head: set[int] = set()
    body: set[int] = set()
    for b in article["blocks"]:
        st = _stages_in(texts[b["block_id"]])
        if b["section"] in _HEADLINE_SECTIONS:
            head |= st
        elif b["section"] == "body":
            body |= st
    return head, body


def _mismatch(head: set[int], body: set[int]) -> tuple[int, int] | None:
    hm, bm = max(head, default=0), max(body, default=0)
    return (hm, bm) if hm >= _TITLE_ALERT_MIN and hm > bm else None


def check_flow(article: Any, edits: list[dict]) -> list[Issue]:
    """수정안(edits)을 원문에 적용한 사본에서 흐름을 결정적으로 검사한다. 의미 판단은 하지 않는다.

    edits: [{block_id, start, end, replacement, label(선택)}] — start/end 는 블록 text 의 code point 반개구간.
    검사
    - EDIT_OVERLAP(오류): 같은 블록에서 수정 구간이 겹친다. 겹친 쪽은 적용하지 않는다(contracts.validate_report 의 구간 겹침 규칙과 같다).
    - EDIT_INVALID(오류): 블록이 없거나 범위가 틀렸다.
    - STAGE_MISMATCH_INTRODUCED / STAGE_MISMATCH_REMAINS(경고): 제목·부제의 절차 단계어(기소·재판·판결·확정)가 본문에 나온 가장 앞선
      단계보다 앞서간다. 원문에는 없다가 수정 때문에 생기면 INTRODUCED, 원문에도 있고 수정 뒤에도 남으면 REMAINS(수정이 해소하지 못함).
    - STAGE_WORD_ADDED(경고): 수정안이 원문 기사 어디에도 없던 절차 단계어(혐의·수사·기소·재판·판결·확정)를 새로 넣었다.
    """
    orig_texts = {b["block_id"]: b["text"] for b in article["blocks"]}
    new_texts, issues = _apply_edits(article, edits)
    if not edits:
        return issues
    head0, body0 = _group_stages(article, orig_texts)
    head1, body1 = _group_stages(article, new_texts)
    before, after = _mismatch(head0, body0), _mismatch(head1, body1)
    if after:
        code = "STAGE_MISMATCH_REMAINS" if before else "STAGE_MISMATCH_INTRODUCED"
        issues.append(Issue(code, "warning",
                            f"headline stage '{_STAGE_NAME[after[0]]}' is ahead of the body's furthest stage ({_STAGE_NAME.get(after[1], 'none')}) after the edits"))
    for r in sorted(_all_stages(article, new_texts) - _all_stages(article, orig_texts)):
        issues.append(Issue("STAGE_WORD_ADDED", "warning", f"edits introduce the procedural-stage word '{_STAGE_NAME[r]}' that the original article never uses"))
    issues.sort(key=lambda i: (0 if i.level == "error" else 1, i.code))
    return issues


def _all_stages(article: Any, texts: dict[str, str]) -> set[int]:
    out: set[int] = set()
    for b in article["blocks"]:
        out |= _stages_in(texts[b["block_id"]])
    return out

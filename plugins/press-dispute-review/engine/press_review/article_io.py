"""기사 입력: 평문 분해와 파일 읽기.

평문 분해는 휴리스틱이다. 결과 blocks를 사용자에게 보여 주고 바로잡게 한다.

이식: 작성자 소유 코드(비공개)의 ``is_subtitle_block`` 을 그대로 가져왔다
(원본은 빈 줄로 나뉜 블록 단위, 여기서는 줄 목록을 합쳐 같은 함수에 넘긴다).
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import stat
from pathlib import Path
from typing import Any, NamedTuple

from . import contracts, hashing

MAX_FILE_BYTES = 2_000_000


# --- 이식: 작성자 소유 코드(비공개) ----------------------------------
def is_subtitle_block(block):
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    if not lines: return False
    return all(len(l) <= 40 and not re.search(r"(다|요)\.$|까\?$", l) for l in lines)
# --- 이식 끝 -------------------------------------------------------------------------------


_QUOTE_START = ("“", "‘", '"', "「", "『", "'")


def _subtitle_ok(lines: list[str]) -> bool:
    """is_subtitle_block + 호출부 보강. 원본은 마침표 없는 서술행('피고인은 부인했다')과 인용행('“정말 그렇습니다”')도 부제로
    판정한다(실측). 따옴표로 시작하거나 다/요로 끝나는 줄은 부제로 보지 않는다."""
    if not is_subtitle_block("\n".join(lines)):
        return False
    return not any(l.startswith(_QUOTE_START) or re.search(r"(다|요)$", l) for l in lines)


class ArticleLoadError(ValueError):
    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or []


class LoadedArticle(NamedTuple):
    article: dict
    raw_bytes: bytes
    warnings: list


_CAPTION = re.compile(r"^(?:\[사진[^\]]*\]|\(사진[^)]*\)|사진\s*[=:：]|사진\s*(?:제공|출처)|[▲△])")
_MAX_SUBTITLE_LINES = 3


def _paragraphs(text: str) -> list[list[str]]:
    paras: list[list[str]] = []
    cur: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if line:
            cur.append(line)
        elif cur:
            paras.append(cur)
            cur = []
    if cur:
        paras.append(cur)
    return paras


def parse_plaintext_ex(
    text: str,
    *,
    article_id: str | None = None,
    publication_date: str | None = None,
    genre: str | None = None,
    election_context: str | None = None,
    verification_notes: list[str] | None = None,
    sensitive_material: bool = False,
    today: _dt.date | None = None,
) -> tuple[dict, list[str]]:
    """평문 -> (article 객체, 경고 목록).

    규칙: 첫 비어있지 않은 줄 = title. 그 뒤 짧은 줄(들) = subtitle 후보(is_subtitle_block, 최대 3줄, 본문이 남아야 함).
    나머지 문단 = body. 줄이 '사진=', '[사진', '▲', '△' 등으로 시작하면 caption.
    빈 줄이 하나도 없으면 줄마다 한 문단으로 본다.
    """
    warnings: list[str] = ["블록 구분(제목/부제/본문/캡션)은 휴리스틱입니다. 결과를 확인하고 틀린 곳은 바로잡아 주세요."]
    paras = _paragraphs(text or "")
    if not paras:
        raise ValueError("empty article text")
    flat = len(paras) == 1 and len(paras[0]) > 1
    if flat:
        paras = [[l] for l in paras[0]]

    title = paras[0][0]
    first_rest = paras[0][1:]
    subs: list[str] = []
    body: list[list[str]]
    if first_rest:
        if len(paras) > 1 and len(first_rest) <= _MAX_SUBTITLE_LINES and _subtitle_ok(first_rest):
            subs, body = first_rest, paras[1:]
        else:
            body = [first_rest] + paras[1:]
    elif flat:
        k = 1
        while k < len(paras) - 1 and k - 1 < _MAX_SUBTITLE_LINES and _subtitle_ok(paras[k]):
            k += 1
        subs, body = [paras[i][0] for i in range(1, k)], paras[k:]
    elif len(paras) > 2 and len(paras[1]) <= _MAX_SUBTITLE_LINES and _subtitle_ok(paras[1]):
        subs, body = paras[1], paras[2:]
    else:
        body = paras[1:]

    blocks: list[dict] = [{"block_id": "title", "section": "title", "text": title}]
    for i, s in enumerate(subs, 1):
        blocks.append({"block_id": "subtitle" if len(subs) == 1 else f"subtitle-{i}", "section": "subtitle", "text": s})
    n_body = n_cap = 0
    for para in body:
        run: list[str] = []

        def flush() -> None:
            nonlocal n_body
            if run:
                n_body += 1
                blocks.append({"block_id": f"p{n_body}", "section": "body", "text": "\n".join(run)})
                run.clear()

        for line in para:
            if _CAPTION.match(line):
                flush()
                n_cap += 1
                blocks.append({"block_id": f"caption-{n_cap}", "section": "caption", "text": line})
            else:
                run.append(line)
        flush()
    if n_body == 0:
        warnings.append("본문 블록이 없습니다. 제목만 입력되었는지 확인해 주세요.")

    today = today or _dt.date.today()
    if not publication_date:
        publication_date = today.isoformat()
        warnings.append(f"발행(예정)일이 없어 오늘 날짜({publication_date})로 채웠습니다.")
    article = {
        "schema_version": "0.1",
        "article_id": article_id or "text-" + hashing.sha256_bytes((text or "").encode("utf-8", "surrogatepass"))[:12],
        "publication_date": publication_date,
        "blocks": blocks,
        "context": {
            "genre": genre or "unknown",
            "election_context": election_context,
            "verification_notes": list(verification_notes or []),
            "sensitive_material": bool(sensitive_material),
        },
    }
    return article, warnings


def parse_plaintext(text: str, **ctx: Any) -> dict:
    return parse_plaintext_ex(text, **ctx)[0]


def _slug(stem: str) -> str:
    s = re.sub(r"[^0-9A-Za-z가-힣._-]+", "-", stem).strip("-.")
    return (s or "article")[:60]


def load_article(path: str | Path) -> LoadedArticle:
    """.json(article 객체) / .txt / .md(평문). raw_bytes는 파일 바이트 해시(file_bytes 모드)에 쓴다."""
    p = Path(path)
    try:
        st = p.stat()
        if not stat.S_ISREG(st.st_mode):  # 장치·FIFO는 st_size가 0이고 읽기가 끝나지 않을 수 있다
            raise ArticleLoadError("input is not a regular file")
        if st.st_size > MAX_FILE_BYTES:
            raise ArticleLoadError(f"file larger than {MAX_FILE_BYTES} bytes")
        with p.open("rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)  # st_size가 0인 /proc류 파일도 상한을 넘겨 읽지 않는다
        if len(raw) > MAX_FILE_BYTES:
            raise ArticleLoadError(f"file larger than {MAX_FILE_BYTES} bytes")
    except OSError as e:
        raise ArticleLoadError(f"cannot read input file: {e.strerror or type(e).__name__}") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ArticleLoadError("input is not valid UTF-8") from None
    suffix = p.suffix.lower()
    if suffix == ".json":
        try:
            obj = json.loads(text)
        except ValueError as e:
            raise ArticleLoadError(f"invalid JSON: {e}") from None
        errs = contracts.validate_article(obj)
        if errs:
            raise ArticleLoadError("article does not match the schema", errs)
        return LoadedArticle(obj, raw, [])
    if suffix in (".txt", ".md", ""):
        try:
            article, warnings = parse_plaintext_ex(text, article_id=_slug(p.stem))
        except ValueError as e:
            raise ArticleLoadError(str(e)) from None
        errs = contracts.validate_article(article)
        if errs:
            raise ArticleLoadError("plain text could not be turned into a valid article", errs)
        return LoadedArticle(article, raw, warnings)
    raise ArticleLoadError(f"unsupported file type '{suffix}' (use .json, .txt or .md)")

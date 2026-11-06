"""HTML 본문 추출(P2_DESIGN §2). 표준 라이브러리 ``html.parser`` 만 쓴다(정규식 태그 파싱 금지: 선형 시간 토크나이저).

이식: 작성자 소유 코드(비공개)의 ``_HTMLTextExtractor``·``decode_html``·``extract_html`` 을 결함 수정 후 가져왔다.
고친 결함(실측):
- header/footer/aside 안의 텍스트를 버리던 것: 스킵 태그에서 뺐다(스킵은 script/style/noscript/svg/iframe/template/head 와 폼 컨트롤뿐이다).
- 표 셀이 붙던 것: td/th 가 끝나면 탭, tr 이 끝나면 줄바꿈을 넣는다(셀 구분자 ``\t``).
- 소규모 공백 정리가 탭을 지우던 것: 줄 단위로 연속 공백만 접고 탭 구분자는 보존한다.
- 닫히지 않은 스킵 태그(``stuck``)가 문서 꼬리를 삼켰는지 경고로 남긴다(원본과 같은 사유).
구역: h1~h4 에서 새 구역이 시작된다(없으면 1구역). locator 종류는 html_section.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Optional

from . import quality
from .pdf import PageOut

SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "template", "head", "button", "select", "textarea", "option"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header", "footer",
              "aside", "nav", "table", "ul", "ol", "dl", "dt", "dd", "blockquote", "pre", "hr", "form", "caption", "main",
              "figure", "figcaption"}
HEADING_TAGS = {"h1", "h2", "h3", "h4"}
CELL_TAGS = {"td", "th"}
VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}


def decode_html(raw: bytes, charset: Optional[str] = None) -> str:
    """헤더 charset(있으면) -> meta charset(앞 4KB) -> utf-8 엄격 -> cp949 엄격 -> utf-8 replace."""
    encs = [charset] if charset else []
    m = re.search(rb'charset=["\']?\s*([A-Za-z0-9_-]+)', raw[:4096], re.I)
    if m:
        encs.append(m.group(1).decode("ascii", "ignore"))
    encs += ["utf-8", "cp949"]
    for enc in encs:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._sections: list = [["", []]]        # [heading_label, parts]
        self._in_heading = False

    def _parts(self) -> list:
        return self._sections[-1][1]

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            if tag not in VOID_TAGS:
                self._skip += 1
            return
        if tag == "body":
            self._skip = 0           # </head> 없이 body 가 시작하는 문서에서 head 스킵이 본문을 삼키지 않게 한다
        if self._skip:
            return
        if tag in HEADING_TAGS:
            self._sections.append(["", ["\n"]])
            self._in_heading = True
        elif tag in BLOCK_TAGS:
            self._parts().append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in SKIP_TAGS or self._skip:
            return
        if tag in BLOCK_TAGS:
            self._parts().append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            if self._skip > 0:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag in CELL_TAGS:
            self._parts().append("\t")
        elif tag in HEADING_TAGS:
            self._in_heading = False
            self._parts().append("\n")
        elif tag in BLOCK_TAGS:
            self._parts().append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        self._parts().append(data)
        if self._in_heading and not self._sections[-1][0]:
            self._sections[-1][0] = data.strip()

    def stuck(self) -> int:
        return self._skip

    def sections(self) -> list:
        return [(label, "".join(parts)) for label, parts in self._sections]


def _tidy(text: str) -> str:
    """줄마다 연속 공백(탭 제외)을 접고, 셀 끝의 빈 탭을 정리하고, 연속 빈 줄을 하나로 접는다."""
    nbsp = text.replace(" ", " ").replace("​", "")
    lines = []
    for line in nbsp.split("\n"):
        cells = [re.sub(r"[ \r\f\v]+", " ", c).strip() for c in line.split("\t")]
        while cells and not cells[-1]:
            cells.pop()
        lines.append("\t".join(cells))
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return out.strip("\n")


def html_sections(raw: bytes, charset: Optional[str] = None) -> tuple:
    """HTML 바이트 -> ([(구역 제목, 텍스트)], 경고 목록). 빈 구역은 버린다. 본문이 전혀 없으면 구역 목록이 비어 있다."""
    p = _Extractor()
    p.feed(decode_html(raw, charset))
    p.close()
    warns = []
    if p.stuck():
        warns.append(f"unclosed_skip_tag:{p.stuck()}")
    secs = [(label, _tidy(t)) for label, t in p.sections()]
    return [(l, t) for l, t in secs if t], warns


def extract_html(raw: bytes, charset: Optional[str] = None) -> list:
    secs, warns = html_sections(raw, charset)
    out = []
    for i, (label, text) in enumerate(secs, 1):
        q, m, reasons = quality.assess(text)
        qj = {**{k: v for k, v in m.items() if k != "meaningful"}, "reasons": reasons, "section_label": label}
        if warns:
            qj["warnings"] = warns
        out.append(PageOut(i, "s", text, "html", q, qj, needs_review=bool(warns), pdf_page_no=None))
    return out

"""근거 코퍼스 인터페이스와 합성 코퍼스.

실제 코퍼스는 ``corpus_sqlite.SqliteCorpus``(P3)가 같은 ``Corpus`` 인터페이스를 SQLite 색인으로 구현한다. ``load_default()`` 는
데이터 디렉터리에 색인이 있으면 그것을, 없으면 이 파일의 합성 ``FixtureCorpus`` 를 돌려준다(P3_DESIGN §2).
passage 공개 형태(dict): source_id, passage_id, url, locator{kind,value}, authority_type, decision_date, text, is_fixture.
SQLite 색인 passage 에는 기준일 필드 reference_date·reference_date_basis·date_unknown(G-D23·G-D25: 결정일, 없으면 수록 간행물 발행일,
없으면 간행물 연도의 12월 31일(basis=year_upper_bound), 모두 없으면 date_unknown)이 더해진다. 합성 코퍼스의 공개 형태는 위 8개 키로 고정이다.
SQLite 색인의 passage 는 여기에 case_number, institution, outcome_type, opinion_kinds, display_text 등이 더해진다.
"""
from __future__ import annotations

import abc
import json
import re
from pathlib import Path
from typing import Any

from . import config, pubyear
from .contracts import core_coverage, is_date, load_schema

AUTHORITY_TYPES = tuple(
    load_schema("review")["properties"]["findings"]["items"]["properties"]["evidence"]["items"]["properties"]["authority_type"]["enum"]
)
RISK_CHANNELS = tuple(load_schema("review")["properties"]["findings"]["items"]["properties"]["risk_channels"]["items"]["enum"])
INSTITUTIONS = tuple(load_schema("case-record")["properties"]["institution"]["enum"])
STANCES = ("support", "counter", "both")
MAX_LIMIT = 10
MAX_QUERY_CP = 1000
PUBLIC_KEYS = ("source_id", "passage_id", "url", "locator", "authority_type", "decision_date", "text", "is_fixture")


class CorpusError(Exception):
    pass


def reference_date(decision_date: Any, publication_date: Any = None, year: Any = None) -> dict:
    """passage 의 기준일(G-D23, G-D25): 결정일, 없으면 수록 간행물의 발행일, 없으면 간행물 연도의 12월 31일(상한, basis ``year_upper_bound``),
    모두 없거나 형식이 아니면 None(date_unknown).

    연도만 아는 근거는 그해 안의 어느 날짜일 수 있으므로 12월 31일을 기준일로 둔다. ``as_of`` 필터에서는 같은 해 기사보다 늦은 자료로 취급될 수 있어 보수적으로 작동한다.
    ``year`` 는 정수 연도(1900~2100)만 받는다(``pubyear.valid_year``).
    ``as_of`` 필터와 보고서의 기준일 검증(``legal_as_of`` 이후 자료 금지)은 모두 ``reference_date`` 를 쓴다.
    date_unknown 인 passage 는 ``as_of`` 필터에서 빼지 않고(남기고) 표시만 한다.
    """
    for basis, d in (("decision_date", decision_date), ("publication_date", publication_date)):
        if isinstance(d, str) and is_date(d):
            return {"reference_date": d, "reference_date_basis": basis, "date_unknown": False}
    y = pubyear.valid_year(year)
    if y is not None:
        return {"reference_date": f"{y:04d}-12-31", "reference_date_basis": "year_upper_bound", "date_unknown": False}
    return {"reference_date": None, "reference_date_basis": None, "date_unknown": True}


class Corpus(abc.ABC):
    @property
    @abc.abstractmethod
    def snapshot_id(self) -> str: ...

    @property
    @abc.abstractmethod
    def is_fixture(self) -> bool: ...

    @abc.abstractmethod
    def get_passage(self, source_id: str, passage_id: str) -> dict | None: ...

    @abc.abstractmethod
    def search(
        self,
        query: str,
        *,
        authority_types: list[str] | None = None,
        risk_channel: str | None = None,
        as_of: str | None = None,
        limit: int = 5,
        institution: str | None = None,
        stance: str = "both",
    ) -> list[dict]: ...

    @abc.abstractmethod
    def status(self) -> dict: ...


_WORD = re.compile(r"\w+", re.UNICODE)
_HANGUL = re.compile(r"[가-힣]+")


def features(text: str) -> set[str]:
    """검색 특징: 영숫자 토큰, 한글 어절은 문자 bigram(조사 변화에 둔감하게)."""
    out: set[str] = set()
    for tok in _WORD.findall(text.lower()):
        runs = _HANGUL.findall(tok)
        if runs and "".join(runs) == tok and len(tok) >= 2:
            out.update(tok[i:i + 2] for i in range(len(tok) - 1))
        else:
            out.add(tok)
    return out


def _check_search_args(query: Any, authority_types: Any, risk_channel: Any, as_of: Any, limit: Any,
                       institution: Any = None, stance: Any = "both", cap: int = MAX_LIMIT) -> int:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if authority_types is not None:
        if not isinstance(authority_types, list) or any(a not in AUTHORITY_TYPES for a in authority_types):
            raise ValueError(f"authority_types must be a list of {list(AUTHORITY_TYPES)}")
    if risk_channel is not None and risk_channel not in RISK_CHANNELS:
        raise ValueError(f"risk_channel must be one of {list(RISK_CHANNELS)}")
    if as_of is not None and (not isinstance(as_of, str) or not is_date(as_of)):
        raise ValueError("as_of must be a YYYY-MM-DD date")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if institution is not None and institution not in INSTITUTIONS:
        raise ValueError(f"institution must be one of {list(INSTITUTIONS)}")
    if stance not in STANCES:
        raise ValueError(f"stance must be one of {list(STANCES)}")
    return min(limit, cap)


class FixtureCorpus(Corpus):
    def __init__(self, path: str | Path | None = None):
        p = Path(path) if path else config.FIXTURES_DIR / "corpus.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise CorpusError(f"cannot load fixture corpus: {type(e).__name__}") from None
        try:
            self._snapshot = str(data["snapshot_id"])
            self._as_of = str(data["as_of"])
            self._rights = dict(data.get("rights_summary", {}))
            self._desc = str(data.get("description", ""))
            raw = list(data["passages"])
        except (KeyError, TypeError, ValueError):
            raise CorpusError("fixture corpus is malformed") from None
        if data.get("is_fixture") is not True:
            raise CorpusError("FixtureCorpus requires is_fixture=true")
        if not is_date(self._as_of):
            raise CorpusError("fixture corpus as_of is not a date")
        self.fallback_notice: str | None = None     # load_default 가 색인 부재·불능을 알릴 때 채운다
        self._passages: dict[tuple[str, str], dict] = {}
        self._ref: dict[tuple[str, str], dict] = {}
        self._risk: dict[tuple[str, str], set[str]] = {}
        self._feat: dict[tuple[str, str], set[str]] = {}
        for r in raw:
            try:
                key = (r["source_id"], r["passage_id"])
                pub = {k: r[k] for k in PUBLIC_KEYS}
            except (KeyError, TypeError):
                raise CorpusError("fixture passage is missing a field") from None
            if key in self._passages:
                raise CorpusError(f"duplicate passage {key}")
            if pub["is_fixture"] is not True:
                raise CorpusError(f"passage {key} is not marked is_fixture")
            if pub["authority_type"] not in AUTHORITY_TYPES:
                raise CorpusError(f"passage {key} has unknown authority_type")
            self._passages[key] = pub
            self._ref[key] = reference_date(pub["decision_date"])   # 공개 형태(D2)는 바꾸지 않는다: 기준일 필드는 내부 필터용
            self._risk[key] = set(r.get("risk_channels", []))
            self._feat[key] = features(pub["text"] + " " + pub["locator"]["value"])

    @property
    def snapshot_id(self) -> str:
        return self._snapshot

    @property
    def is_fixture(self) -> bool:
        return True

    def get_passage(self, source_id: str, passage_id: str) -> dict | None:
        p = self._passages.get((source_id, passage_id))
        return json.loads(json.dumps(p)) if p else None

    def search(self, query, *, authority_types=None, risk_channel=None, as_of=None, limit=5, institution=None, stance="both"):
        n = _check_search_args(query, authority_types, risk_channel, as_of, limit, institution, stance)
        if institution is not None:
            return []           # 합성 passage 에는 기관 정보가 없다. 없는 값을 지어내지 않는다.
        q = features(query[:MAX_QUERY_CP])
        scored: list[tuple[int, str, str]] = []
        for key, pub in self._passages.items():
            if authority_types is not None and pub["authority_type"] not in authority_types:
                continue
            if risk_channel is not None and risk_channel not in self._risk[key]:
                continue
            if as_of is not None and self._ref[key]["reference_date"] and self._ref[key]["reference_date"] > as_of:
                continue
            score = len(q & self._feat[key])
            if score:
                scored.append((-score, key[0], key[1]))
        scored.sort()
        return [self.get_passage(s, p) for _, s, p in scored[:n]]

    def status(self) -> dict:
        by_type: dict[str, int] = {}
        for pub in self._passages.values():
            by_type[pub["authority_type"]] = by_type.get(pub["authority_type"], 0) + 1
        out = {
            "snapshot_id": self._snapshot,
            "is_fixture": True,
            "as_of": self._as_of,
            "passage_count": len(self._passages),
            "source_count": len({k[0] for k in self._passages}),
            "by_authority_type": dict(sorted(by_type.items())),
            "cases_by_authority_type": dict(sorted(by_type.items())),
            "date_unknown_count": sum(1 for r in self._ref.values() if r["date_unknown"]),
            "rights_hold": list(self._rights.get("rights_hold", [])),
            "missing": list(self._rights.get("missing", [])),
            "notice": self._desc,
        }
        out["core_coverage"] = core_coverage(out)
        if self.fallback_notice:
            out["warnings"] = [self.fallback_notice]
        return out


def _has_current_index(od: Path) -> bool:
    """색인을 열지 않고 CURRENT 와 그것이 가리키는 파일의 존재만 본다."""
    from . import corpus_sqlite      # 순환 import 를 피한다
    try:
        name = (od / corpus_sqlite.CURRENT_POINTER).read_text(encoding="utf-8").strip()
        return corpus_sqlite._INDEX_RE.match(name) is not None and (od / name).is_file()
    except OSError:
        return False


def load_default() -> Corpus:
    """데이터 디렉터리의 SQLite 색인(``<data>/index``)이 있으면 그것을, 없거나 열 수 없으면 합성 코퍼스를 돌려준다.

    합성 코퍼스로 돌아갈 때는 ``status()['warnings']`` 에 '실자료 미적재' 사유를 싣는다(P3_DESIGN §2). 색인은 서버 시작 때 한 번 열며,
    그 뒤에 만든 색인은 서버를 다시 시작해야 쓴다.
    """
    from . import corpus_sqlite      # 순환 import 를 피한다(corpus_sqlite 가 이 모듈의 Corpus 를 쓴다)

    fx_notice: str
    od = corpus_sqlite.index_dir()
    where = f"조회한 데이터 폴더: {od.parent}, 색인 경로: {od}"
    try:
        live = corpus_sqlite.open_current(od)
    except CorpusError as e:
        fx_notice = f"실자료 미적재: 검색 색인을 열 수 없어 합성 코퍼스를 씁니다 ({e}). {where}."
    else:
        if live is not None:
            return live
        fx_notice = f"실자료 미적재: 검색 색인이 없어 합성 코퍼스를 씁니다. {where}. 수집 CLI의 build_index 로 색인을 만든 뒤 서버를 다시 시작하세요."
        dflt = config.default_data_dir()
        if dflt != od.parent and _has_current_index(dflt / corpus_sqlite.INDEX_DIR_NAME):
            fx_notice += (f" 기본 위치 {dflt} 에는 색인이 있습니다(선택된 폴더와 다름, 자동 채택하지 않음)."
                          f" PRESS_DISPUTE_DATA_DIR={dflt} 를 설정하고 다시 시작하세요.")
    fx = FixtureCorpus()
    fx.fallback_notice = fx_notice
    return fx

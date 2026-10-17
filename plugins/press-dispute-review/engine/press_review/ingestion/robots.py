"""robots.txt 정책. 호스트별로 하루 1회 받아 원문 해시와 함께 장부(robots 표)에 기록하고 Disallow 경로는 요청하지 않는다.

판정 규칙(RFC 9309 기준, 여기서 정한 부분은 명시):
- 2xx: 파싱. 본문이 HTML이면 규칙을 읽을 수 없으므로 purpose_status=``unparseable`` 로 기록하고 그 호스트는 전부 막는다(fail-closed). 다음 실행에서 다시 받는다.
  주석·지시문의 같은 줄에 'AI 학습/RAG'와 금지 표현이 함께 있으면 ``ai_rag_prohibited`` 로 호스트 전체를 막는다(이 프로젝트가 RAG 코퍼스이므로).
- 4xx(404·410 포함): 규칙 없음(허용), purpose_status=``no_rules``.
- 5xx·timeout·연결 실패(재시도 소진): ``unknown`` 이고 그 호스트는 전부 막는다(fail-closed). 다음 실행에서 다시 받는다.
- robots.txt 자체를 받는 요청은 robots 검사를 받지 않는다. 신선도(24시간)는 robots 표의 fetched_at 으로 판단하고 http_cache는 본문 보관에만 쓴다.
- User-agent 매칭: 우리 UA 제품 토큰(press-dispute-review)을 포함하는 그룹이 있으면 그 그룹만, 없으면 ``*`` 그룹. 가장 긴 패턴이 이기고 동률이면 Allow. ``*`` 와 ``$`` 지원.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from .context import TimeBudgetExceeded, iso, parse_iso
from .http import DryRunMiss, FetchFailed, ResponseTooLarge, RobotsDisallowed

UA_TOKEN = "press-dispute-review"
TTL = timedelta(hours=24)
_AI_PURPOSE_RE = re.compile(r"(?i)(ai[\s-]*training|retrieval[\s-]*augmented|\brag\b|machine[\s-]*learning|ai\s*학습|인공지능\s*학습)")
_PROHIBIT_RE = re.compile(r"(?i)(prohibit|forbid|not\s+(?:be\s+)?(?:permitted|allowed)|금지|불허)")


@dataclass
class Rule:
    allow: bool
    pattern: str
    regex: "re.Pattern[str]"


def _compile(pattern: str) -> "re.Pattern[str]":
    pat = urllib.parse.unquote(pattern)
    anchored = pat.endswith("$")
    if anchored:
        pat = pat[:-1]
    rx = ".*".join(re.escape(part) for part in pat.split("*"))
    return re.compile("^" + rx + ("$" if anchored else ""))


@dataclass
class RobotsRules:
    rules: list = field(default_factory=list)

    def allowed(self, path_query: str) -> bool:
        target = urllib.parse.unquote(path_query or "/")
        best: Optional[Rule] = None
        for r in self.rules:
            if r.regex.match(target):
                if best is None or len(r.pattern) > len(best.pattern) or (
                        len(r.pattern) == len(best.pattern) and r.allow and not best.allow):
                    best = r
        return True if best is None else best.allow


ALLOW_ALL = RobotsRules()


def parse_robots(text: str, ua_token: str = UA_TOKEN) -> tuple[RobotsRules, bool, bool]:
    """(규칙, 규칙 줄이 하나라도 있었는지, AI/RAG 금지 표현이 있었는지)."""
    groups: list[tuple[list[str], list[Rule]]] = []
    agents: list[str] = []
    rules: list[Rule] = []
    in_rules = False
    ai_notice = False
    any_rule = False
    for raw in text.splitlines():
        if _AI_PURPOSE_RE.search(raw) and _PROHIBIT_RE.search(raw):
            ai_notice = True
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if in_rules:
                groups.append((agents, rules))
                agents, rules, in_rules = [], [], False
            agents.append(value.lower())
        elif key in ("allow", "disallow"):
            in_rules = True
            if value == "" and key == "disallow":
                continue
            if value == "":
                continue
            rules.append(Rule(key == "allow", value, _compile(value)))
            any_rule = True
    if agents:
        groups.append((agents, rules))
    specific = [g for g in groups if any(a != "*" and a in ua_token.lower() for a in g[0])]
    chosen = specific or [g for g in groups if "*" in g[0]]
    merged: list[Rule] = []
    for _, rs in chosen:
        merged.extend(rs)
    return RobotsRules(merged), any_rule, ai_notice


def _looks_html(body: bytes) -> bool:
    head = body[:1024].lower()
    return b"<html" in head or b"<!doctype" in head or b"<body" in head or b"<head" in head


@dataclass
class Decision:
    host: str
    http_status: int
    sha256: Optional[str]
    purpose_status: str
    fetched_at: str
    rules: RobotsRules
    block_reason: Optional[str] = None
    from_db: bool = False

    def info(self) -> dict:
        return {"host": self.host, "http_status": self.http_status, "body_sha256": self.sha256,
                "purpose_status": self.purpose_status, "fetched_at": self.fetched_at, "from_db": self.from_db}


def _decide(host: str, status: int, body: bytes, fetched_at: str, from_db: bool = False) -> Decision:
    sha = hashlib.sha256(body).hexdigest() if (200 <= status < 300) else None
    if status == 0 or status >= 500:
        return Decision(host, status, sha, "unknown", fetched_at, ALLOW_ALL,
                        "robots.txt unreachable or server error (fail-closed)", from_db)
    if 400 <= status < 500:
        return Decision(host, status, sha, "no_rules", fetched_at, ALLOW_ALL, None, from_db)
    if status >= 300:  # 리다이렉트 소진 등
        return Decision(host, status, sha, "unknown", fetched_at, ALLOW_ALL, "unexpected robots.txt status", from_db)
    if _looks_html(body):
        return Decision(host, status, sha, "unparseable", fetched_at, ALLOW_ALL,
                        "robots.txt could not be parsed (HTML body, fail-closed)", from_db)
    text = body.decode("utf-8", "replace")
    rules, any_rule, ai = parse_robots(text)
    if ai:
        return Decision(host, status, sha, "ai_rag_prohibited", fetched_at, rules,
                        "robots.txt states AI training/RAG access is prohibited", from_db)
    if not any_rule:
        purpose = "no_rules"
    elif not rules.allowed("/"):
        purpose = "root_disallowed"
    else:
        purpose = "allowed"
    return Decision(host, status, sha, purpose, fetched_at, rules, None, from_db)


class RobotsPolicy:
    def __init__(self, client: Any):
        self.client = client
        self._memo: dict[str, Decision] = {}

    @staticmethod
    def url_for(host: str) -> str:
        return f"https://{host}/robots.txt"

    def check(self, host: str, path_query: str) -> None:
        d = self.decision(host)
        if d.block_reason:
            raise RobotsDisallowed(f"{host}: {d.block_reason}")
        if not d.rules.allowed(path_query):
            raise RobotsDisallowed(f"robots.txt on {host} disallows {urllib.parse.urlsplit(path_query).path or '/'}")

    def decision(self, host: str, *, force: bool = False) -> Decision:
        if not force and host in self._memo:
            return self._memo[host]
        d = None if force else self._from_db(host)
        if d is None:
            d = self._fetch(host)
        self._memo[host] = d
        return d

    def _from_db(self, host: str) -> Optional[Decision]:
        m = self.client.manifest
        row = m.latest_robots(host) if m is not None else None
        if not row or row["purpose_status"] in ("unknown", "unparseable"):
            return None
        try:
            age = self.client._now() - parse_iso(row["fetched_at"])
        except ValueError:
            return None
        if age >= TTL:
            return None
        status = int(row["http_status"] or 0)
        if 200 <= status < 300:
            hit = self.client.cache.get(host, "GET", self.url_for(host))
            if hit is None or hashlib.sha256(hit.body).hexdigest() != row["body_sha256"]:
                return None
            return _decide(host, status, hit.body, row["fetched_at"], from_db=True)
        return _decide(host, status, b"", row["fetched_at"], from_db=True)

    def _fetch(self, host: str) -> Decision:
        c = self.client
        url = self.url_for(host)
        if not c.network:
            raise DryRunMiss(f"GET {url}")
        try:
            resp = c._request("GET", url, url, False, 1024 * 1024, check_robots=False)
            status, body = resp.status, resp.body
            if 200 <= status < 300:
                c.cache.put(host, "GET", url, resp)
        except TimeBudgetExceeded:
            raise
        except (FetchFailed, ResponseTooLarge):
            status, body = 0, b""
        fetched_at = iso(c._now())
        d = _decide(host, status, body, fetched_at)
        if c.manifest is not None:
            c.manifest.record_robots(host, fetched_at, status, d.sha256, d.purpose_status)
        return d

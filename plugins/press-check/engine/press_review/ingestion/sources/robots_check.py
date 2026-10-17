"""robots.txt 점검 어댑터(스모크용): 허용 호스트 중 지정한 호스트의 robots.txt만 받아 robots 표에 기록한다. 그 밖의 요청은 하지 않는다."""
from __future__ import annotations

from ..http import ALLOWED_HOSTS

DEFAULT_HOSTS = ("www.pac.or.kr", "www.iendc.go.kr", "www.law.go.kr")


def add_arguments(parser) -> None:
    parser.add_argument("--host", action="append", dest="hosts", help="점검할 호스트(반복 가능, 기본: PAC·IENDC·법제처 3곳)")


def run(ctx, args) -> dict:
    hosts = tuple(args.hosts) if getattr(args, "hosts", None) else DEFAULT_HOSTS
    out: dict = {}
    for host in hosts:
        ctx.check_time()
        if host not in ALLOWED_HOSTS:
            out[host] = {"error": "host not allowed"}
            continue
        d = ctx.http.robots.decision(host)
        out[host] = {"http_status": d.http_status, "sha12": (d.sha256 or "")[:12] or None,
                     "purpose_status": d.purpose_status, "fetched_at": d.fetched_at, "from_db": d.from_db,
                     "blocked": d.block_reason}
    return {"robots": out}

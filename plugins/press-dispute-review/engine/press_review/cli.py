"""``python -m press_review`` 명령줄. 종료 코드: 0 성공, 1 검증 실패, 2 입력·사용 오류."""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from typing import Sequence

from . import contracts, corpus as corpus_mod, logsafe, render, review
from .article_io import ArticleLoadError, load_article
from .providers import PROVIDERS


def _out(s: str) -> None:
    sys.stdout.write(s + "\n")


def _err(s: str) -> None:
    sys.stderr.write(s + "\n")


def _parse_now(s: str | None) -> _dt.datetime:
    if not s:
        return _dt.datetime.now().astimezone()
    p = contracts.parse_rfc3339(s)
    if p is None:
        raise ValueError("--now must be an RFC 3339 date-time")
    return p


def cmd_review(args: argparse.Namespace) -> int:
    try:
        loaded = load_article(args.input)
        now = _parse_now(args.now)
    except (ArticleLoadError, ValueError) as e:
        _err(f"error: {e}")
        for m in getattr(e, "errors", []):
            _err(f"  {m}")
        return 2
    corpus = corpus_mod.load_default()
    provider = PROVIDERS[args.provider]()
    logsafe.register_article("cli", loaded.article)
    findings, explanations = provider.analyze(loaded.article, corpus)
    res = review.build_report_ex(loaded.article, findings, explanations, corpus, now=now, raw_bytes=loaded.raw_bytes)
    if res.report is None:
        _err("error: report did not pass validation")
        for m in res.errors:
            _err(f"  {m}")
        return 1
    for w in loaded.warnings + res.warnings:
        _err(f"warning: {w}")
    if args.format == "json":
        _out(json.dumps(res.report, ensure_ascii=False, indent=2))
    else:
        _out(render.render_table(res.report))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        loaded = load_article(args.article)
    except ArticleLoadError as e:
        _err(f"error: {e}")
        for m in e.errors:
            _err(f"  {m}")
        return 2
    errs = [f"article: {m}" for m in contracts.validate_article(loaded.article)]
    if args.report and not errs:
        try:
            with open(args.report, encoding="utf-8-sig") as f:
                rep = json.load(f)
        except (OSError, ValueError) as e:
            _err(f"error: cannot read report: {type(e).__name__}")
            return 2
        errs += contracts.validate_report(rep, loaded.article, corpus_mod.load_default(), raw_bytes=loaded.raw_bytes)
    if errs:
        for m in errs:
            _err(m)
        return 1
    _out("OK")
    return 0


def cmd_corpus_status(args: argparse.Namespace) -> int:
    _out(json.dumps(review.corpus_status(corpus_mod.load_default()), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="press_review", description="언론분쟁 예방 검토 엔진(P0)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review", help="기사 파일을 검토해 표를 출력")
    r.add_argument("--input", required=True, help=".json(article), .txt, .md")
    r.add_argument("--provider", choices=sorted(PROVIDERS), default="mock")
    r.add_argument("--format", choices=["table", "json"], default="table")
    r.add_argument("--now", help="분석 시각(RFC 3339). 재현용")
    r.set_defaults(func=cmd_review)
    v = sub.add_parser("validate", help="기사(와 보고서) 계약 검증")
    v.add_argument("--article", required=True)
    v.add_argument("--report")
    v.set_defaults(func=cmd_validate)
    c = sub.add_parser("corpus-status", help="코퍼스 상태")
    c.set_defaults(func=cmd_corpus_status)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)

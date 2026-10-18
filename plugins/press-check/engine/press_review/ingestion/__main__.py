"""수집 운영 CLI: ``python -m press_review.ingestion <source|status|export-inventory|rights> [옵션]``. MCP로 노출하지 않는다.

``<source>`` 는 ``ingestion/sources/<name>.py`` 어댑터 이름이다. 어댑터 계약은 ``sources/__init__.py`` 를 본다.
출력은 표준출력에 JSON 한 줄(건수·상태·경로만). 진단 로그는 stderr(logsafe 필터 경유).
종료 코드: 0 정상(완료·시간 상한 중단·dry-run 중단 포함), 1 실행 오류, 2 사용법 오류.
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import pkgutil
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .. import diskguard, logsafe
from . import sources as sources_pkg
from .context import Budget, RunContext, TimeBudgetExceeded, corpus_dir as default_corpus_dir, utcnow
from .http import DryRunMiss, HttpClient, IngestionError, scrub_text
from .manifest import Manifest, sanitize_message

RESERVED = ("status", "export-inventory", "export-coverage", "admission", "rights")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# 의도적으로 제외한 소량 쓰기 명령: rights set, export-inventory — 수 KB 쓰기라 디스크가 부족해도 권리 기록·목록 내보내기를 막지 않는다.
# 디스크를 쓰는 어댑터 -> (항목당 예상 종류, 표시 이름). 읽기 전용(status·export-*·rights show·admission 보고)과 --dry-run 에는 가드를 걸지 않는다
DISK_GUARDED = {"fetch": ("pdf", "PDF 수신"), "extract": ("struct", "본문 추출"), "build_index": ("struct", "색인 빌드"),
                "lawapi": ("case_json", "판례 수집"), "lawapi_body": ("case_json", "판례 본문 수집"),
                "pac": ("case_json", "PAC 수집"), "iendc": ("case_json", "IENDC 수집")}


def available_sources() -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules(sources_pkg.__path__) if _NAME_RE.match(m.name))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m press_review.ingestion", description=__doc__.split("\n")[0])
    p.add_argument("target", help="어댑터 이름 또는 status | export-inventory | export-coverage | admission | rights")
    p.add_argument("--max-seconds", type=float, default=None, help="시간 상한(초). 넘기면 정상 중단하고 다음 실행이 이어받는다")
    p.add_argument("--refresh", action="store_true", help="HTTP 캐시를 무시하고 다시 받는다(robots는 하루 1회 규칙 유지)")
    p.add_argument("--limit-pages", type=int, default=None, help="어댑터가 처리할 목록 페이지 수 상한")
    p.add_argument("--dry-run", action="store_true", help="장부는 메모리 사본, HTTP는 캐시 적중만. 캐시에 없으면 그 URL을 보고하고 멈춘다")
    p.add_argument("--force-disk", action="store_true", help="디스크 가드(C: 여유 확인)를 우회한다. 기본 꺼짐, 경고를 남기고 결과 JSON 에 기록한다")
    p.add_argument("--out-dir", default=None, help="export-inventory·export-coverage 출력 폴더(기본 corpus/inventory, export-coverage는 필수)")
    return p


def _emit(out: Any, payload: dict) -> None:
    out.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    out.flush()


def _rights_command(extras: list, root: Path, out: Any, parser: argparse.ArgumentParser) -> int:
    """``rights show`` / ``rights set <source_family> --<권리> <값> --note 근거 [--by 기록자]``. 값은 설치자가 직접 정한다."""
    import getpass
    from . import rights as rights_mod
    rp = argparse.ArgumentParser(prog="python -m press_review.ingestion rights")
    rp.add_argument("action", choices=("set", "show"))
    rp.add_argument("family", nargs="?", help="set: " + " | ".join(rights_mod.COLLECTED_FAMILIES))
    for k in rights_mod.RIGHT_KEYS:
        rp.add_argument("--" + k.replace("_", "-"), dest=k, choices=rights_mod.VALUES, default=None)
    rp.add_argument("--note", default="", help="set: 근거 한 줄(필수)")
    rp.add_argument("--by", default=None, help="set: 기록자(기본 OS 사용자 이름)")
    ra = rp.parse_args(extras)
    if ra.action == "show":
        m = Manifest(root / "manifest.sqlite", in_memory_copy=True)   # 읽기 전용 사본
        try:
            _emit(out, {"command": "rights show", "sources": rights_mod.rights_overview(m)})
        finally:
            m.close()
        return 0
    if not ra.family:
        parser.error("rights set requires <source_family>")
    changes = {k: getattr(ra, k) for k in rights_mod.RIGHT_KEYS if getattr(ra, k) is not None}
    m = Manifest(root / "manifest.sqlite")
    try:
        try:
            updated = rights_mod.record_installer_decision(m, ra.family, changes, ra.note, ra.by or getpass.getuser())
        except (ValueError, LookupError) as e:
            _emit(out, {"command": "rights set", "status": "refused", "error": str(e)})
            return 2
        _emit(out, {"command": "rights set", "status": "recorded", "source_family": ra.family, "changes": changes,
                    "updated_sources": updated})
    finally:
        m.close()
    return 0


def _disk_stop(out: Any, target: str, e: "diskguard.DiskGuardError") -> int:
    _emit(out, {"command": target, "status": "disk_guard_stopped", "kind": e.kind, "free_bytes": e.free_bytes,
                "needed_bytes": e.needed_bytes, "message_ko": e.message_ko})
    print(e.message_ko, file=sys.stderr)
    return diskguard.exit_code(e)


def main(argv: Optional[list] = None, *, transport: Any = None, clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep, resolver: Optional[Callable] = None,
         oc_provider: Optional[Callable[[], str]] = None, stdout: Any = None, corpus: Optional[Path] = None,
         log_stream: Any = None) -> int:
    out = stdout or sys.stdout
    parser = build_parser()
    ns, extras = parser.parse_known_args(argv)
    target = ns.target
    module = None
    if target not in RESERVED:
        if not _NAME_RE.match(target):
            parser.error(f"invalid source name '{target}' (available: {', '.join(available_sources())})")
        try:
            module = importlib.import_module(f"{sources_pkg.__name__}.{target}")
        except ModuleNotFoundError as e:
            if e.name != f"{sources_pkg.__name__}.{target}":
                raise
            parser.error(f"unknown source '{target}' (available: {', '.join(available_sources())}, status, export-inventory)")
        if not callable(getattr(module, "run", None)):
            parser.error(f"source '{target}' has no run(ctx, args)")
        if callable(getattr(module, "add_arguments", None)):
            module.add_arguments(parser)
    if target == "admission":      # 어댑터의 --source 와 겹치지 않게 admission 일 때만 등록한다
        parser.add_argument("--source", default=None, help="admission 대상 소스(pac_board_correction_status | iendc_review_results)")
        parser.add_argument("--apply", action="store_true", help="admission: 제외 사유를 실패 장부에 기록한다(기본은 읽기 전용 보고)")
        parser.add_argument("--min-quote-rate", type=float, default=None, help="admission: 첨부 인용 일치율 하한(기본 admission.MIN_QUOTE_RATE)")
        parser.add_argument("--max-quarantine-rate", type=float, default=None, help="admission: 첨부 격리율 상한(기본 admission.MAX_QUARANTINE_RATE)")
        parser.add_argument("--min-completeness", type=float, default=None, help="admission: 장부 사건 수 / 본문 사건번호 수 하한(기본 admission.MIN_COMPLETENESS)")
    args = ns if target == "rights" else parser.parse_args(argv)   # rights 는 아래 전용 파서가 나머지 인자를 읽는다

    root = Path(corpus) if corpus else default_corpus_dir()
    logsafe.configure(stream=log_stream)
    forced: list = []                   # --force-disk 로 가드를 우회한 기록(결과 JSON 의 disk_guard_forced)
    logger = logging.getLogger("press_review.ingestion")

    if target == "status":
        m = Manifest(root / "manifest.sqlite", in_memory_copy=True)
        try:
            _emit(out, {"command": "status", "corpus_dir": str(root), "manifest_exists": (root / "manifest.sqlite").exists(),
                        **m.status_summary()})
        finally:
            m.close()
        return 0
    if target == "export-inventory":
        m = Manifest(root / "manifest.sqlite")
        try:
            out_dir = Path(args.out_dir) if args.out_dir else root / "inventory"
            out_dir.mkdir(parents=True, exist_ok=True)
            written = {}
            for fam in m.families():
                inv = m.inventory(fam)
                p = out_dir / f"{fam}.json"
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_text(json.dumps(inv, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                tmp.replace(p)
                written[fam] = {"path": str(p), "documents": inv["documents"]["count"]}
            _emit(out, {"command": "export-inventory", "written": written})
        finally:
            m.close()
        return 0

    if target == "export-coverage":
        if not ns.out_dir:
            parser.error("export-coverage requires --out-dir (예: <repo>/docs/coverage)")
        from . import coverage
        m = Manifest(root / "manifest.sqlite", in_memory_copy=True)  # 읽기 전용 사본
        try:
            _emit(out, {"command": "export-coverage", **coverage.export_coverage(m, Path(ns.out_dir), utcnow().isoformat(timespec="seconds"), root / "inventory")})
        finally:
            m.close()
        return 0

    if target == "rights":
        return _rights_command(extras, root, out, parser)

    if target == "admission":
        from . import admission
        if args.source not in admission.ADMISSION_SOURCES:
            parser.error(f"admission requires --source one of: {', '.join(admission.ADMISSION_SOURCES)}")
        kw = {k: v for k, v in (("min_quote_rate", args.min_quote_rate), ("max_quarantine_rate", args.max_quarantine_rate),
                                                  ("min_completeness", args.min_completeness)) if v is not None}
        if args.apply and not ns.dry_run:            # 장부에 쓰는 경우에만 가드(보고만 하는 경로는 읽기 전용)
            try:
                diskguard.require_free(0, "admission 적용", force=ns.force_disk, fallback_path=root, on_force=forced.append)
            except diskguard.DiskGuardError as e:
                return _disk_stop(out, target, e)
        # --apply 없이, 또는 --dry-run 이면 메모리 사본에서 읽는다(파일을 쓰지 않는다)
        m = Manifest(root / "manifest.sqlite", in_memory_copy=(ns.dry_run or not args.apply))
        try:
            report = admission.evaluate(m, args.source, corpus_dir=root, **kw)
            written = admission.apply(m, report) if args.apply else None
            _emit(out, {"command": "admission", "source": args.source, "applied": bool(args.apply and not ns.dry_run), "written": written,
                        "criteria": report["criteria"], "summary": report["summary"], "attachments": report["attachments"],
                    **({"disk_guard_forced": forced} if forced else {})})
        finally:
            m.close()
        return 0

    # ---- 어댑터 실행 ----
    guard = None
    if target in DISK_GUARDED and not ns.dry_run:
        kind, label = DISK_GUARDED[target]
        guard = diskguard.DiskGuard(label, diskguard.estimate(kind, 1), force=ns.force_disk, on_force=forced.append, fallback_path=root)
        try:
            guard.start(0)               # 시작 검사는 최소 여유+마진만. 항목 수를 아는 단계(run_fetch 등)가 예상량을 다시 건다
        except diskguard.DiskGuardError as e:
            return _disk_stop(out, target, e)
    budget = Budget(ns.max_seconds, clock)
    manifest = Manifest(root / "manifest.sqlite", in_memory_copy=ns.dry_run)
    client = HttpClient(root / "http_cache", manifest=manifest, transport=transport, clock=clock, sleep=sleep,
                        budget=budget, oc_provider=oc_provider, resolver=resolver, refresh=ns.refresh,
                        network=not ns.dry_run, lock_dir=root / "locks", logger=logger.getChild("http"))
    ctx = RunContext(http=client, manifest=manifest, budget=budget, logger=logger, corpus_dir=root, now=utcnow,
                     refresh=ns.refresh, limit_pages=ns.limit_pages, dry_run=ns.dry_run)
    if guard is not None:
        ctx.extras.update(disk_guard=guard, force_disk=ns.force_disk, on_force_disk=forced.append)
    status, result, extra, code = "completed", None, {}, 0
    try:
        result = module.run(ctx, args)
    except TimeBudgetExceeded:
        status = "paused_time_limit"
    except DryRunMiss as e:
        status, extra = "dry_run_stopped", {"would_fetch": sanitize_message(str(e))}
    except diskguard.DiskGuardError as e:      # 루프 중간 부족: 장부는 이어받기 가능한 상태 그대로 정지한다
        status, code = "paused_disk_" + e.kind, diskguard.exit_code(e)
        extra = {"kind": e.kind, "free_bytes": e.free_bytes, "needed_bytes": e.needed_bytes, "message_ko": e.message_ko}
    except IngestionError as e:
        status, code = "failed", 1
        extra = {"error_class": e.error_class, "error": sanitize_message(scrub_text(str(e)))}
    except Exception as e:  # 어댑터 버그: 메시지만 남기고 트레이스백은 로그 필터를 거친다
        status, code = "failed", 1
        extra = {"error_class": type(e).__name__, "error": sanitize_message(str(e))}
        logger.error("adapter %s crashed: %s", target, type(e).__name__, exc_info=True)  # 트레이스백은 logsafe 필터를 거친다
    finally:
        counts = manifest.counts()
        client.close()
        manifest.close()
    _emit(out, {"command": target, "status": status, "elapsed_seconds": round(budget.elapsed(), 1),
                "corpus_dir": str(root), "manifest_counts": counts, "http": dict(client.stats), "result": result,
                **({"disk_guard_forced": forced} if forced else {}), **extra})
    return code


if __name__ == "__main__":
    sys.exit(main())

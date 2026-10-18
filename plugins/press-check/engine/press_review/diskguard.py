"""디스크 가드: 큰 쓰기 작업 전에 Windows C: 여유를 확인하고 부족하면 중단한다.

배경: 2026-10-17 밤 WSL vhdx 가 놓인 C: 여유가 6GB 에서 51MB 로 급감해 SQLite 손상 위험이 있었다. WSL 안의 ``df /`` 는 vhdx 의 가상 용량을
보여 주므로 판단 기준이 될 수 없다. 기준은 vhdx 가 자라는 실제 드라이브(``/mnt/c``)의 여유다.

측정: ``os.statvfs(path)`` 의 ``f_bavail * f_frsize`` (= ``df -B1 --output=avail``). 기본 경로 우선순위는 인자 ``path`` >
환경변수 ``PRESS_DISK_PATH`` > ``/mnt/c``(존재할 때) > 데이터 디렉터리가 놓인 파일시스템(비 WSL).

중단 기준: 여유 < 최소 여유(``PRESS_DISK_MIN_FREE_GB``, 기본 10) 이거나 여유 < 예상 쓰기량 + 마진(``PRESS_DISK_MARGIN_GB``, 기본 5).
측정이 실패하면(마운트 문제 등) 실패가 아니라 '확인 불가로 중단'(kind='unknown')이다.
종료 코드(CLI): low=3, unknown=4. 우회는 ``force=True``(CLI ``--force-disk``)뿐이며 기본 꺼짐이고 stderr 경고와 ``on_force`` 콜백을 남긴다.

vhdx 압축(Optimize-VHD 등)은 이 모듈이 하지 않는다. 사용자가 Windows 에서 수행한다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Optional

GB = 1024 ** 3
MB = 1024 ** 2

EXIT_LOW = 3
EXIT_UNKNOWN = 4

WSL_C_PATH = "/mnt/c"
DEFAULT_MIN_FREE_GB = 10.0
DEFAULT_MARGIN_GB = 5.0

# ---- 예상 용량 상수(환경변수 PRESS_DISK_* 로 덮어쓴다). 항목당 평균에 여유를 얹은 보수적 값이다 ----
# 수신 PDF: 위원회·심의 결정문 PDF 평균 1~2MB, 일부 수십 MB(웹 캐시 사본 포함 시 2배 쓰기) -> 항목당 2MB.
PDF_MB = 2.0
# 판례 JSON: 법제처 본문 응답(HTTP 캐시 + 장부 행) 한 건 평균 수십~수백 KB -> 항목당 0.3MB.
CASE_JSON_MB = 0.3
# 구조화: 창별 응답 JSON·사건 행·chunk 한 건 평균 -> 항목당 0.1MB.
STRUCT_MB = 0.1
# 색인 빌드: 새 snapshot 은 직전 snapshot 과 비슷한 크기이고 FTS 증가분을 더해 1.15배(임시 파일 + 이전 파일이 잠깐 공존).
INDEX_FACTOR = 1.15
# 백업: 원본 파일 크기 그대로.

PDF_BYTES = int(PDF_MB * MB)
CASE_JSON_BYTES = int(CASE_JSON_MB * MB)
STRUCT_BYTES = int(STRUCT_MB * MB)

_KINDS = {"pdf": ("PRESS_DISK_PDF_MB", PDF_MB), "case_json": ("PRESS_DISK_CASE_JSON_MB", CASE_JSON_MB),
          "struct": ("PRESS_DISK_STRUCT_MB", STRUCT_MB)}


class DiskGuardError(RuntimeError):
    """디스크 여유 부족(kind='low') 또는 확인 불가(kind='unknown')."""

    def __init__(self, kind: str, free_bytes: Optional[int], needed_bytes: Optional[int], message_ko: str):
        super().__init__(message_ko)
        self.kind, self.free_bytes, self.needed_bytes, self.message_ko = kind, free_bytes, needed_bytes, message_ko


def exit_code(e: DiskGuardError) -> int:
    return EXIT_UNKNOWN if e.kind == "unknown" else EXIT_LOW


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def estimate(kind: str, n_items: int = 1) -> int:
    """종류별 항목당 예상 쓰기량(바이트) x 항목 수. kind: pdf | case_json | struct."""
    env, default = _KINDS[kind]
    return int(_env_float(env, default) * MB) * max(0, int(n_items))


def latest_snapshot_bytes(index_dir: str | Path) -> int:
    """CURRENT 가 가리키는(없으면 가장 최근 mtime 의) corpus-*.sqlite 크기. 없으면 0."""
    d = Path(index_dir)
    try:
        ptr = (d / "CURRENT").read_text(encoding="utf-8").strip()
        if ptr and (d / ptr).is_file():
            return (d / ptr).stat().st_size
    except OSError:
        pass
    try:
        files = [p for p in d.glob("corpus-*.sqlite") if p.is_file()]
        return max(files, key=lambda p: p.stat().st_mtime).stat().st_size if files else 0
    except OSError:
        return 0


def index_build_estimate(index_dir: str | Path) -> int:
    return int(latest_snapshot_bytes(index_dir) * _env_float("PRESS_DISK_INDEX_FACTOR", INDEX_FACTOR))


def backup_estimate(src: str | Path) -> int:
    return Path(src).stat().st_size


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="ignore").lower()
    except OSError:
        return False


def resolve_path(path: str | Path | None = None, *, fallback: str | Path | None = None, is_wsl: Optional[bool] = None) -> Path:
    """측정 대상 경로. 인자 > PRESS_DISK_PATH > /mnt/c(존재) > (비 WSL 에서만) fallback(없으면 데이터 디렉터리)의 존재하는 가장 가까운 상위.

    WSL 인데 /mnt/c 도 PRESS_DISK_PATH 도 없으면 다른 파일시스템으로 대체하지 않고 kind='unknown' 으로 중단한다(WSL 의 / 여유는 C: 여유가 아니다).
    """
    if path:
        return Path(path)
    env = os.environ.get("PRESS_DISK_PATH")
    if env:
        return Path(env)
    if os.path.isdir(WSL_C_PATH):
        return Path(WSL_C_PATH)
    if (_is_wsl() if is_wsl is None else is_wsl):
        raise DiskGuardError("unknown", None, None,
                             f"[디스크 가드] WSL 에서 기준 드라이브({WSL_C_PATH})를 찾을 수 없습니다. 마운트를 확인하거나 PRESS_DISK_PATH 로 "
                             "Windows C: 경로를 지정한 뒤 다시 실행하십시오. 다른 파일시스템의 여유로 대체하지 않습니다.")
    if fallback is None:
        from . import config
        fallback = config.data_dir()
    p = Path(fallback)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _default_avail(path) -> int:
    st = os.statvfs(str(path))
    return st.f_bavail * st.f_frsize


def _gb(n: Optional[int]) -> str:
    return "확인 불가" if n is None else f"{n / GB:.2f}GB"


def _msg_low(label: str, free: int, needed: int, min_free: int, path: Path) -> str:
    return (f"[디스크 가드] '{label}' 작업을 시작하지 않습니다. 기준 드라이브({path})의 현재 여유는 {_gb(free)}이고, "
            f"필요량(예상 쓰기량 + 마진)은 {_gb(needed)}, 최소 여유 기준은 {_gb(min_free)}입니다. "
            "Windows 에서 C: 공간을 확보하거나 불필요한 사본을 정리한 뒤 다시 실행하십시오. "
            "WSL vhdx 파일 압축은 사용자가 Windows 에서 직접 수행합니다(이 도구는 하지 않습니다).")


def _msg_unknown(label: str, path: Path, needed: int, err: str) -> str:
    return (f"[디스크 가드] '{label}' 작업을 시작하지 않습니다. 기준 경로({path})의 여유를 확인할 수 없습니다({err}). "
            f"필요량(예상 쓰기량 + 마진)은 {_gb(needed)}입니다. 마운트 상태를 확인하거나 PRESS_DISK_PATH 를 지정한 뒤 다시 실행하십시오. "
            "여유 확인 없이 진행해야 하면 --force-disk 를 쓸 수 있으나 권하지 않습니다.")


def require_free(estimated_bytes: int, label: str, *, avail_fn: Optional[Callable] = None, force: bool = False,
                 min_free_gb: Optional[float] = None, margin_gb: Optional[float] = None, path=None,
                 fallback_path=None, on_force: Optional[Callable[[str], None]] = None) -> None:
    """여유가 충분하면 반환하고, 부족하거나 확인 불가면 DiskGuardError. force 면 경고를 남기고 통과한다."""
    min_free = int((min_free_gb if min_free_gb is not None else _env_float("PRESS_DISK_MIN_FREE_GB", DEFAULT_MIN_FREE_GB)) * GB)
    margin = int((margin_gb if margin_gb is not None else _env_float("PRESS_DISK_MARGIN_GB", DEFAULT_MARGIN_GB)) * GB)
    needed = max(0, int(estimated_bytes)) + margin
    err: Optional[DiskGuardError] = None
    try:
        p = resolve_path(path, fallback=fallback_path)
    except DiskGuardError as e:
        err = DiskGuardError("unknown", None, needed, e.message_ko)
    else:
        try:
            free = (avail_fn or _default_avail)(p)
        except Exception as e:  # noqa: BLE001 - statvfs·마운트 어떤 실패든 '확인 불가'로 멈춘다
            err = DiskGuardError("unknown", None, needed, _msg_unknown(label, p, needed, type(e).__name__))
        else:
            if free < min_free or free < needed:
                err = DiskGuardError("low", int(free), needed, _msg_low(label, int(free), needed, min_free, p))
    if err is None:
        return
    if not force:
        raise err
    warn = f"[디스크 가드 우회] --force-disk 로 계속합니다: {err.message_ko}"
    print(warn.replace("\n", " "), file=sys.stderr)
    if on_force is not None:
        on_force(f"{label}: " + warn)


class DiskGuard:
    """긴 루프용. ``start(n)`` 에서 예상 = per_item_bytes x n 으로 검사하고, ``tick()`` 이 check_every 마다 남은 분량을 다시 검사한다."""

    def __init__(self, label: str, per_item_bytes: int, check_every: int = 25, *, avail_fn: Optional[Callable] = None,
                 force: bool = False, min_free_gb: Optional[float] = None, margin_gb: Optional[float] = None, path=None,
                 fallback_path=None, on_force: Optional[Callable[[str], None]] = None):
        self.label, self.per_item, self.check_every = label, int(per_item_bytes), max(1, int(check_every))
        self._kw = dict(avail_fn=avail_fn, force=force, min_free_gb=min_free_gb, margin_gb=margin_gb, path=path,
                        fallback_path=fallback_path, on_force=on_force)
        self.total: Optional[int] = None
        self.done = 0          # 완료한 항목 수(advance 로만 오른다)
        self._ticks = 0        # 주기 검사 카운터(페이지·문서마다 불리는 check_time 의 호출 수)

    def start(self, n_items: int) -> None:
        self.total, self.done, self._ticks = max(0, int(n_items)), 0, 0
        require_free(self.per_item * self.total, self.label, **self._kw)

    def advance(self, n: int = 1) -> None:
        """항목 n 개를 끝냈다고 알린다. 검사는 하지 않는다."""
        self.done += n

    def tick(self) -> None:
        """check_every 번 호출마다 남은 분량(total - done)을 다시 검사한다. 호출 횟수는 완료 항목 수와 무관하다."""
        self._ticks += 1
        if self._ticks % self.check_every:
            return
        remaining = max(0, self.total - self.done) if self.total is not None else self.check_every
        require_free(self.per_item * remaining, self.label, **self._kw)

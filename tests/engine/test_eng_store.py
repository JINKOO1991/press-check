import ast
import builtins
import io
import os
from pathlib import Path

import pytest

from press_review import logsafe, store as store_mod
from press_review.store import EphemeralStore


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_ttl_expiry_and_purge():
    c = Clock()
    s = EphemeralStore(ttl_seconds=1800, clock=c)
    exp = s.put("a", {"x": 1})
    s.put("b", 2, ttl_seconds=60)
    assert exp == 1000.0 + 1800
    assert s.get("a") == {"x": 1} and len(s) == 2
    c.t += 61
    assert s.get("b") is None                      # 만료 즉시 접근 불가
    assert s.get("a") == {"x": 1}
    c.t += 1740                                     # 총 1801초
    assert s.get("a", "gone") == "gone"
    assert len(s) == 0


def test_purge_expired_counts_and_boundary():
    c = Clock()
    s = EphemeralStore(ttl_seconds=10, clock=c)
    for k in "abc":
        s.put(k, k)
    c.t += 9.99
    assert s.purge_expired() == 0
    c.t += 0.01                                     # 정확히 TTL: 만료
    assert s.purge_expired() == 3
    assert s.get("a") is None


def test_put_restarts_ttl_and_delete_clear():
    c = Clock()
    s = EphemeralStore(ttl_seconds=10, clock=c)
    s.put("a", 1)
    c.t += 8
    s.put("a", 2)
    c.t += 8
    assert s.get("a") == 2
    assert s.delete("a") and not s.delete("a")
    s.put("b", 1)
    s.clear()
    assert len(s) == 0


def test_invalid_ttl_and_repr_hides_content():
    with pytest.raises(ValueError):
        EphemeralStore(ttl_seconds=0)
    s = EphemeralStore()
    s.put("k", "비밀 원고 내용입니다")
    assert "원고" not in repr(s) and "k" not in repr(s)


def test_default_ttl_is_thirty_minutes():
    assert EphemeralStore().ttl_seconds == 1800


def test_store_module_imports_nothing_that_persists():
    src = Path(store_mod.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0] or "relative")
    assert imported <= {"__future__", "threading", "time", "typing", "relative"}
    assert "open(" not in src


def _forbid_writes(monkeypatch):
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open
    calls = []

    def guard_open(real):
        def wrapper(file, mode="r", *a, **k):
            if any(ch in str(mode) for ch in "wax+"):
                calls.append(("open", str(file), mode))
                raise AssertionError(f"disk write attempted: {file} mode={mode}")
            return real(file, mode, *a, **k)
        return wrapper

    def os_open(path, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            calls.append(("os.open", str(path), flags))
            raise AssertionError(f"disk write attempted: {path}")
        return real_os_open(path, flags, *a, **k)

    monkeypatch.setattr(builtins, "open", guard_open(real_open))
    monkeypatch.setattr(io, "open", guard_open(real_io_open))
    monkeypatch.setattr(os, "open", os_open)
    return calls


def test_store_writes_nothing_to_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    before = sorted(p.name for p in tmp_path.rglob("*"))
    calls = _forbid_writes(monkeypatch)
    s = EphemeralStore(ttl_seconds=5, clock=Clock())
    s.put("article:x", {"blocks": ["원고 본문"]})
    s.get("article:x")
    s.purge_expired()
    assert calls == []
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_whole_review_flow_writes_nothing_to_disk(tmp_path, monkeypatch, article, corpus):
    import datetime as dt
    from press_review import review
    from press_review.providers import MockProvider
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = _forbid_writes(monkeypatch)
    s = EphemeralStore()
    prep = review.prepare_article(s, article=article)
    assert prep["ok"]
    findings, expl = MockProvider().analyze(article, corpus)
    out = review.submit_report(s, corpus, article_handle=prep["article_handle"], findings=findings, explanations=expl,
                               now=dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc))
    assert out["ok"], out
    assert review.explain_finding(s, report_id=out["report_id"], finding_id="finding-001")["ok"]
    assert calls == []
    assert list(tmp_path.rglob("*")) == []
    logsafe.reset()

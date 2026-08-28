"""Context fixture-snapshot tests (SPEC V2B §4)."""

import os

from argent_core import Role
from argent_core.context import build_agent_context, fixture_snapshot
from argent_core.models import (
    SourceClass,
    Task,
    TaskState,
)


def _task():
    return Task(
        id="t1",
        project_id="p1",
        title="demo",
        state=TaskState.PLANNING,
        resume_state=None,
        source="owner:authenticated",
        source_class=SourceClass.TRUSTED,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def _scope(tmp_path):
    scope = tmp_path / "scope"
    scope.mkdir()
    return scope


# ----------------------------------------------------------- only scope files


def test_only_scope_files(tmp_path):
    scope = _scope(tmp_path)
    (scope / "a.txt").write_text("hello")
    (tmp_path / "outside.txt").write_text("outside")
    snap = fixture_snapshot(scope)
    assert set(snap["files"].keys()) == {"a.txt"}
    assert snap["files"]["a.txt"] == "hello"


def test_nested_files_relative_paths(tmp_path):
    scope = _scope(tmp_path)
    (scope / "sub").mkdir()
    (scope / "sub" / "b.txt").write_text("nested")
    snap = fixture_snapshot(scope)
    assert set(snap["files"].keys()) == {"sub/b.txt"}


# ---------------------------------------------------------- symlink escaping


def test_symlink_escape_skipped(tmp_path):
    scope = _scope(tmp_path)
    (scope / "real.txt").write_text("real")
    outside = tmp_path / "outside.txt"
    outside.write_text("sensitive data")
    os.symlink(outside, scope / "ln")
    snap = fixture_snapshot(scope)
    assert "ln" not in snap["files"]
    assert any(s["path"] == "ln" and s["reason"] == "symlink" for s in snap["skipped"])


# -------------------------------------------------------------- deny-list


def test_denylist_content_skipped(tmp_path):
    scope = _scope(tmp_path)
    (scope / "ok.txt").write_text("normal text")
    (scope / "bad.txt").write_text("password = hunter2")
    snap = fixture_snapshot(scope)
    assert set(snap["files"].keys()) == {"ok.txt"}
    assert any(
        s["path"] == "bad.txt" and s["reason"] == "denylist" for s in snap["skipped"]
    )


# ------------------------------------------------------------ size limits


def test_size_limit_per_file(tmp_path):
    scope = _scope(tmp_path)
    (scope / "small.txt").write_text("tiny")
    (scope / "big.txt").write_text("z" * 200)
    snap = fixture_snapshot(scope, max_bytes=100)
    assert set(snap["files"].keys()) == {"small.txt"}
    assert any(
        s["path"] == "big.txt" and s["reason"] == "size_limit" for s in snap["skipped"]
    )


def test_max_files_limit(tmp_path):
    scope = _scope(tmp_path)
    for i in range(5):
        (scope / f"f{i}.txt").write_text("x")
    snap = fixture_snapshot(scope, max_files=3)
    assert len(snap["files"]) == 3
    assert any(s["reason"] == "file_limit" for s in snap["skipped"])


# ------------------------------------------------------- bindable in context


def test_fixture_snapshot_bindable_in_context(tmp_path):
    scope = _scope(tmp_path)
    (scope / "a.txt").write_text("hello")
    snap = fixture_snapshot(scope)
    ctx = build_agent_context(
        _task(),
        Role.IMPLEMENTER,
        3,
        {},
        fixture_files=snap["files"],
        fixture_skipped=tuple(snap["skipped"]),
    )
    assert "fixture" in ctx
    assert ctx["fixture"]["files"] == {"a.txt": "hello"}
    assert ctx["fixture"]["skipped"] == []


def test_no_fixture_section_by_default(tmp_path):
    ctx = build_agent_context(_task(), Role.IMPLEMENTER, 3, {})
    assert "fixture" not in ctx


def test_snapshot_return_shape(tmp_path):
    scope = _scope(tmp_path)
    (scope / "a.txt").write_text("hello")
    snap = fixture_snapshot(scope)
    assert set(snap.keys()) == {"files", "skipped"}
    assert isinstance(snap["files"], dict)
    assert isinstance(snap["skipped"], list)

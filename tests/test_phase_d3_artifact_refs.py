"""Phase D3 — minimal fix: handoff artifact refs WITH hashes + git revision.

Proves the D2→D3 integration gap closure (Analyse-Antwort D): the implementer/
QA handoff now carries bounded diff/artifact refs with full-file sha256
content hashes + git revision — never whole-file content, never secrets, never
blocking.  Also proves the stale-detection basis (same file → same hash,
changed file → different hash).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from types import SimpleNamespace

from argent_core import Core, OWNER_SOURCE, Role
from argent_core.artifact_refs import (
    MAX_EXCERPT_BYTES,
    MAX_HASH_BYTES,
    bounded_excerpt,
    resolve_ref_within,
    sha256_file,
)
from argent_core.handoff import (
    HandoffArtifact,
    build_bounded_artifact_refs,
    build_handoff_record,
    handoff_from_store_row,
    handoff_to_store_json,
    validate_handoff_record,
)

OWNER = OWNER_SOURCE


def _git_init(ws) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", str(ws), "init", "-q", "-b", "main"],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   env=env)
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    subprocess.run(["git", "-C", str(ws), "commit", "-q", "-m", "init"],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   env=env)


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------

def test_same_file_same_hash(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\n")
    h1 = sha256_file(str(p))
    h2 = sha256_file(str(p))
    assert h1 == h2 == hashlib.sha256(b"x = 1\n").hexdigest()


def test_changed_file_different_hash(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\n")
    h1 = sha256_file(str(p))
    p.write_text("x = 2\n")
    h2 = sha256_file(str(p))
    assert h1 != h2


def test_missing_file_no_hash(tmp_path):
    assert sha256_file(str(tmp_path / "gone.py")) is None


def test_oversized_file_no_hash(tmp_path):
    p = tmp_path / "big.bin"
    p.write_bytes(b"0" * (MAX_HASH_BYTES + 1))
    assert sha256_file(str(p)) is None  # bounded: never hash arbitrarily large


def test_bounded_excerpt_truncates(tmp_path):
    from argent_core.artifact_refs import TRUNCATION_MARKER
    p = tmp_path / "big.txt"
    p.write_text("A" * (MAX_EXCERPT_BYTES + 100))
    ex = bounded_excerpt(str(p))
    assert ex.endswith(TRUNCATION_MARKER)
    assert len(ex.encode("utf-8")) <= MAX_EXCERPT_BYTES + len(TRUNCATION_MARKER.encode())


def test_resolve_ref_rejects_escape(tmp_path):
    assert resolve_ref_within(str(tmp_path), "../etc/passwd") is None
    assert resolve_ref_within(str(tmp_path), "/etc/passwd") is None
    assert resolve_ref_within(str(tmp_path), "sub/../..") is None


# ---------------------------------------------------------------------------
# build_bounded_artifact_refs
# ---------------------------------------------------------------------------

def test_builder_refs_with_hash_revision_excerpt(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "mod.py").write_text("def f():\n    return 1\n")
    refs = build_bounded_artifact_refs(
        ["src/mod.py"], worktree_root=str(ws), revision="abc123")
    assert len(refs) == 1
    a = refs[0]
    assert a.ref == "src/mod.py"
    assert a.content_hash == hashlib.sha256(b"def f():\n    return 1\n").hexdigest()
    assert a.revision == "abc123"
    assert a.excerpt == "def f():\n    return 1\n"


def test_builder_skips_missing_and_foreign(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "ok.py").write_text("ok\n")
    refs = build_bounded_artifact_refs(
        ["src/ok.py", "gone.py", "../etc/passwd"], worktree_root=str(ws))
    # Missing and foreign (traversal) refs are SKIPPED entirely — only the
    # readable, in-tree file survives (no empty-hash placeholder).
    assert len(refs) == 1
    assert refs[0].ref == "src/ok.py"
    assert refs[0].content_hash == hashlib.sha256(b"ok\n").hexdigest()


def test_builder_caps_ref_count(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    for i in range(100):
        (ws / "src" / f"f{i}.py").write_text(f"x = {i}\n")
    refs = build_bounded_artifact_refs(
        [f"src/f{i}.py" for i in range(100)], worktree_root=str(ws))
    assert len(refs) == 32  # MAX_ARTIFACT_REFS


# ---------------------------------------------------------------------------
# Handoff roundtrip preserves revision
# ---------------------------------------------------------------------------

def test_handoff_artifact_roundtrip_with_revision(db_path):
    core = Core(db_path)
    rec = build_handoff_record(
        job_id="j1", source_dispatch_id="d1", source_role="implementer",
        artifacts=(HandoffArtifact(ref="src/mod.py",
                                   content_hash="a" * 64,
                                   excerpt="def f():\n    return 1\n",
                                   revision="deadbeef"),),
    )
    validate_handoff_record(rec)
    core._store._insert_handoff_v2(**handoff_to_store_json(rec))
    row = core._store.get_handoff_v2(rec.handoff_id)
    rec2 = handoff_from_store_row(row)
    assert rec2.artifacts[0].revision == "deadbeef"
    assert rec2.artifacts[0].content_hash == "a" * 64
    core.close()


def test_oversized_revision_rejected():
    with __import__("pytest").raises(ValueError):
        build_handoff_record(
            job_id="j1", source_dispatch_id="d1", source_role="implementer",
            artifacts=(HandoffArtifact(ref="src/mod.py",
                                       revision="r" * 200),),
        )


# ---------------------------------------------------------------------------
# Supervisor integration: _default_handoff_record emits hashed refs (D3 wiring)
# ---------------------------------------------------------------------------

def test_supervisor_handoff_emits_hashed_refs(tmp_path):
    from argent_core.supervisor import Supervisor
    from argent_core.worktree import GitProvenanceProvider
    from mock_supervisor_runtime import FakeClock, FakeRunStatusProvider

    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir(parents=True)
    (ws / "src" / "mod.py").write_text("x = 1\n")
    (ws / "tests" / "test_mod.py").write_text("def test_it():\n    pass\n")
    _git_init(ws)
    # Simulate the implementer's real write: modify src/mod.py AFTER commit so
    # ``git diff --name-only HEAD`` (authoritative write/diff scope) confirms it.
    (ws / "src" / "mod.py").write_text("x = 2\n")

    core = Core(str(tmp_path / "t.db"))
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), clock=FakeClock(),
                     git_provenance_provider=GitProvenanceProvider(str(ws)))
    d = SimpleNamespace(id="d1", role=Role.IMPLEMENTER,
                        expected_agent_class="pro")
    job = {"id": "j1", "canonical_worktree_path": str(ws),
           "current_head": None, "expected_head": None}
    envelope = {"status": "ok", "changed_files": ["src/mod.py"],
                "findings": [], "proposal": "fixed", "own_assessment": "done",
                "tests_run": ["tests/test_mod.py"]}
    rec = sup._default_handoff_record(d, job, envelope)
    # Both the write-scope file (git diff) and the test-scope file are confirmed.
    by_ref = {a.ref: a for a in rec.artifacts}
    assert "src/mod.py" in by_ref
    a = by_ref["src/mod.py"]
    assert a.content_hash == hashlib.sha256(b"x = 2\n").hexdigest()
    assert a.revision  # real git HEAD
    assert a.excerpt == "x = 2\n"
    # The test-scope ref is confirmed and hashed (test file exists in worktree).
    assert "tests/test_mod.py" in by_ref
    assert by_ref["tests/test_mod.py"].content_hash
    # Evidence carries the diff ref + commit (git) ref.
    assert "src/mod.py" in rec.evidence.diff_refs
    assert rec.evidence.commit_refs and rec.evidence.commit_refs[0] == a.revision
    core.close()

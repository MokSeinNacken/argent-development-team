"""Phase D1 — storage/persistence tests (I).  Deterministic, no provider calls.

Proves: pack metadata is persisted in SQLite (NOT ``/tmp``), round-trips across
reopen, and is immutable (one pack per dispatch, plain INSERT, no REPLACE).
"""

from __future__ import annotations

import sqlite3

import pytest

from argent_core import Core, OWNER_SOURCE, Role, SequenceKind, role_source
from argent_core.context_pack import ContextPackRecord

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


def _dispatch_id(core: Core) -> str:
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    tr = core.start_task_run(task.id, OWNER)
    core.start_role(task.id, Role.LEAD, LEAD)
    d = core.create_dispatch(
        task.id, tr.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )
    return d.id


def _record(dispatch_id: str, **kwargs) -> ContextPackRecord:
    base = dict(
        context_pack_id="cp_0123456789abcdef01234567",
        dispatch_id=dispatch_id,
        job_id="j1",
        role="lead",
        version="1",
        content_hash="a" * 64,
        size_estimate=100,
        token_count=50,
        soft_budget=8000,
        hard_budget=16000,
        expansion_reason=None,
        artifact_location=None,
        created_at="2026-09-01T00:00:00+00:00",
    )
    base.update(kwargs)
    return ContextPackRecord(**base)


def test_round_trip_across_reopen(db_path):
    core = Core(db_path)
    did = _dispatch_id(core)
    rec = _record(did)
    core._store._insert_context_pack(rec)
    core.close()

    core2 = Core(db_path)
    try:
        got = core2._store.get_context_pack(did)
        assert got is not None
        assert got == rec
        assert got.content_hash == "a" * 64
        assert got.soft_budget == 8000 and got.hard_budget == 16000
        # metadata lives in SQLite, not a /tmp artifact.
        assert got.artifact_location is None
    finally:
        core2.close()


def test_get_by_id_round_trip(db_path):
    core = Core(db_path)
    did = _dispatch_id(core)
    rec = _record(did)
    core._store._insert_context_pack(rec)
    got = core._store.get_context_pack_by_id(rec.context_pack_id)
    assert got is not None and got.dispatch_id == did
    core.close()


def test_immutable_one_pack_per_dispatch(db_path):
    core = Core(db_path)
    did = _dispatch_id(core)
    core._store._insert_context_pack(_record(did))
    # A second pack for the same dispatch is refused (UNIQUE dispatch_id).
    with pytest.raises(sqlite3.IntegrityError):
        core._store._insert_context_pack(
            _record(did, context_pack_id="cp_ffffffffffffffffffffffff")
        )
    core.close()


def test_expansion_reason_persisted(db_path):
    core = Core(db_path)
    did = _dispatch_id(core)
    core._store._insert_context_pack(
        _record(did, expansion_reason="SECURITY_REVIEW")
    )
    got = core._store.get_context_pack(did)
    assert got.expansion_reason == "SECURITY_REVIEW"
    core.close()

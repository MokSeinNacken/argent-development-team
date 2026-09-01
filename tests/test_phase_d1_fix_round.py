"""Phase D1 — fix-round adversarial tests (F1–F6).

Deterministic, no provider calls.  Each test proves one of the six confirmed
findings from the Sol closing review is closed:

* F1 — owner title is REQUIRED OWNER_INSTRUCTION, never silently trimmed.
* F2 — pack id is content-stable (dispatch_id + content_hash), retry-safe.
* F3 — the dispatch point re-validates ANY builder result before persist/spawn.
* F4 — budget enforcement counts the full canonical render + bounded metadata.
* F5 — validate_context_pack re-derives integrity/budget fields from content.
* F6 — permanent context codes fail-closed to BLOCKED; transient codes requeue.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE, Role, SequenceKind, role_source
from argent_core.context_pack import (
    CONTEXT_BUDGET_EXCEEDED,
    ArtifactRef,
    CapabilityTier,
    ContextBuildError,
    ContextBuilder,
    FactInput,
    Importance,
    TrustClass,
    estimate_tokens,
    is_permanent_context_code,
    make_context_pack_id,
    render_pack,
    render_token_count,
    validate_context_pack,
    MAX_ARTIFACT_LOCATION_LEN,
    MAX_SOURCE_REF_LEN,
)
from argent_core.resource_policy import ResourceClass
from argent_core.supervisor import Supervisor
from d1_helpers import (
    FailingContextBuilder,
    drive_d1,
    make_d1_env,
    make_d1_scheduler,
)
from mock_supervisor_runtime import FakeClock, FakeRunLauncher, FakeRunStatusProvider

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


def _build(**kwargs):
    base = dict(job_id="j1", dispatch_id="d1", role="implementer",
                objective="default objective",
                now_iso="2026-09-01T00:00:00+00:00")
    base.update(kwargs)
    return ContextBuilder().build(**base)


def _tok(n: int, ch: str = "x") -> str:
    return ch * (n * 4)


class ReturningContextBuilder:
    """A builder that returns a fixed (possibly malformed) pack without raising."""

    def __init__(self, pack):
        self.pack = pack
        self.calls = []

    def build(self, **kwargs):
        self.calls.append(kwargs)
        return self.pack


def _make_dispatch_env(db_path, title="t", description="fix the bug"):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, title, OWNER, description=description)
    tr = core.start_task_run(task.id, OWNER)
    core.start_role(task.id, Role.LEAD, LEAD)
    d = core.create_dispatch(task.id, tr.id, Role.LEAD, 0, 1,
                             SequenceKind.STANDARD, None, LEAD)
    launch = FakeRunLauncher()
    sup = Supervisor(core, FakeRunStatusProvider(), launch, clock=clock)
    job = sup.store.create_job(task.id, idempotency_key="job-main",
                               resource_class=ResourceClass.HEAVY.value)
    jid = job.supervisor_job_id
    job_row = core._store.get_supervisor_job(jid)
    return SimpleNamespace(core=core, task=task, d=d, sup=sup, jid=jid,
                           job_row=job_row, launch=launch)


# ---------------------------------------------------------------------------
# F1 — owner title is REQUIRED OWNER_INSTRUCTION, never silently trimmed
# ---------------------------------------------------------------------------


def test_f1_title_folded_into_required_objective_survives_trimming():
    objective = "Title: Important Title\nDescription: fix the bug"
    p = _build(objective=objective, history=[_tok(2000, ch) for ch in "abc"])
    # Both title and description are part of the single REQUIRED objective.
    assert p.objective == objective
    obj = [it for it in p.items if it.source_type == "objective"]
    assert len(obj) == 1
    assert obj[0].trust_class == TrustClass.OWNER_INSTRUCTION.value
    assert obj[0].importance == Importance.REQUIRED.value


def test_f1_supervisor_folds_title_into_required_objective(db_path):
    env = _make_dispatch_env(db_path, title="The Title", description="The Body")
    pack = env.sup._build_context_pack(env.d, env.job_row)
    obj = [it for it in pack.items if it.source_type == "objective"]
    assert len(obj) == 1
    assert obj[0].trust_class == TrustClass.OWNER_INSTRUCTION.value
    assert obj[0].importance == Importance.REQUIRED.value
    assert "The Title" in obj[0].content
    assert "The Body" in obj[0].content
    # No separate trimmable "title:" fact remains.
    assert not any(it.content.startswith("title:") for it in pack.items)
    env.core.close()


# ---------------------------------------------------------------------------
# F2 — pack id is content-stable (dispatch_id + content_hash)
# ---------------------------------------------------------------------------


def test_f2_pack_id_stable_without_created_at():
    assert make_context_pack_id("d1", "hash") == make_context_pack_id("d1", "hash")
    assert make_context_pack_id("d1", "hash") != make_context_pack_id("d1", "other")
    assert make_context_pack_id("d1", "hash") != make_context_pack_id("d2", "hash")


def test_f2_retry_same_content_stable_pack_id_single_record(db_path):
    env = _make_dispatch_env(db_path)
    pack1 = env.sup._build_context_pack(env.d, env.job_row)
    id1 = env.sup._persist_context_pack(pack1)
    # A retry rebuilds the SAME semantic content -> SAME pack id, idempotent.
    pack2 = env.sup._build_context_pack(env.d, env.job_row)
    id2 = env.sup._persist_context_pack(pack2)

    assert pack1.content_hash == pack2.content_hash
    assert pack1.context_pack_id == pack2.context_pack_id
    assert id1 == id2 == pack1.context_pack_id

    # Exactly one persisted record, referenced by the exact id the message file
    # would carry.
    rec = env.core._store.get_context_pack(env.d.id)
    assert rec is not None and rec.context_pack_id == pack1.context_pack_id
    assert env.core._store.get_context_pack_by_id(pack1.context_pack_id) is not None

    rendered = render_pack(pack2, context_pack_id=id2)
    assert f"context_pack_id: {id1}" in rendered
    env.core.close()


# ---------------------------------------------------------------------------
# F3 — dispatch point re-validates ANY builder result before persist/spawn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mutate, expected_code", [
    (lambda p: replace(p, version="99"), "CONTEXT_INVALID_VERSION"),
    (lambda p: replace(p, context_pack_id="bogus"), "CONTEXT_MALFORMED_ID"),
    (lambda p: replace(p, items=(replace(p.items[0], trust_class="BOGUS"),)
                       + p.items[1:]), "CONTEXT_INVALID_TRUST_CLASS"),
])
def test_f3_invalid_builder_result_no_dispatch_no_persist(db_path, mutate, expected_code):
    valid = _build()
    malformed = mutate(valid)
    env = make_d1_env(db_path, context_builder=ReturningContextBuilder(malformed))
    sched = make_d1_scheduler(env)

    final = drive_d1(sched, env.jid)

    assert final.outcome == "context_build_failed"
    assert final.detail == expected_code
    # No scope, no legacy launcher fallback.
    assert env.backend.created == []
    assert env.backend.started == []
    assert env.launch.spawns == []
    # No pack was persisted for the invalid builder result.
    row = env.core._store.get_supervisor_job(env.jid)
    assert env.core._store.get_context_pack(row["expected_dispatch_id"]) is None
    env.core.close()


# ---------------------------------------------------------------------------
# F4 — budget counts the full render + bounded metadata
# ---------------------------------------------------------------------------


def test_f4_minimal_pack_reports_over_one_token():
    p = _build(objective="")
    assert p.token_count > 1
    assert p.token_count == render_token_count(p)


def test_f4_render_overhead_counts_artifact_refs_and_locations():
    p = _build(artifacts=[ArtifactRef(ref="src/a.py",
                                      location="~/.local/share/argent/a",
                                      excerpt="code")])
    content_only = sum(estimate_tokens(it.content) for it in p.items)
    # ref + location + section labels are deterministic render overhead.
    assert p.token_count > content_only
    assert p.token_count == render_token_count(p)


def test_f4_oversized_artifact_location_rejected():
    with pytest.raises(ContextBuildError) as e:
        _build(artifacts=[ArtifactRef(
            ref="a.py", location="x" * (MAX_ARTIFACT_LOCATION_LEN + 1),
        )])
    assert e.value.code == "CONTEXT_INVALID_REFERENCE"


def test_f4_oversized_source_ref_rejected():
    with pytest.raises(ContextBuildError) as e:
        _build(facts=[FactInput("f", source_ref="r" * (MAX_SOURCE_REF_LEN + 1))])
    assert e.value.code == "CONTEXT_INVALID_REFERENCE"


# ---------------------------------------------------------------------------
# F5 — validate re-derives integrity/budget fields from content
# ---------------------------------------------------------------------------


def test_f5_manipulated_token_count_rejected():
    p = _build()
    with pytest.raises(ContextBuildError) as e:
        validate_context_pack(replace(p, token_count=p.token_count + 1))
    assert e.value.code == "CONTEXT_INVALID_BUDGET"


def test_f5_manipulated_budget_estimated_rejected():
    p = _build()
    with pytest.raises(ContextBuildError) as e:
        validate_context_pack(replace(p, budget_estimated=p.token_count - 1))
    assert e.value.code == "CONTEXT_INVALID_BUDGET"


def test_f5_wrong_item_content_hash_rejected():
    p = _build(facts=[FactInput("fact x")])
    bad = replace(p.items[0], content_hash="0" * 64)
    with pytest.raises(ContextBuildError) as e:
        validate_context_pack(replace(p, items=(bad,) + p.items[1:]))
    assert e.value.code == "CONTEXT_HASH_MISMATCH"


def test_f5_wrong_item_id_rejected():
    p = _build(facts=[FactInput("fact x")])
    bad = replace(p.items[0], id="ci_0000000000000000")
    with pytest.raises(ContextBuildError) as e:
        validate_context_pack(replace(p, items=(bad,) + p.items[1:]))
    assert e.value.code == "CONTEXT_MALFORMED_ID"


def test_f5_expansion_without_reason_rejected():
    p = _build(objective=_tok(10000), expansion_reason="SECURITY_REVIEW")
    with pytest.raises(ContextBuildError) as e:
        validate_context_pack(replace(p, expansion_reason=None))
    assert e.value.code == "CONTEXT_INVALID_EXPANSION_REASON"


def test_f5_reason_without_expansion_rejected():
    p = _build(objective="small")
    with pytest.raises(ContextBuildError) as e:
        validate_context_pack(replace(p, expansion_reason="SECURITY_REVIEW"))
    assert e.value.code == "CONTEXT_INVALID_EXPANSION_REASON"


# ---------------------------------------------------------------------------
# F6 — permanent context codes fail-closed to BLOCKED; transient requeue
# ---------------------------------------------------------------------------


def test_f6_code_classification():
    permanent = [
        CONTEXT_BUDGET_EXCEEDED, "CONTEXT_INVALID_VERSION", "CONTEXT_MALFORMED_ID",
        "CONTEXT_INVALID_TRUST_CLASS", "CONTEXT_INVALID_IMPORTANCE",
        "CONTEXT_INVALID_BUDGET", "CONTEXT_INVALID_EXPANSION_REASON",
        "CONTEXT_INVALID_REFERENCE", "CONTEXT_HASH_MISMATCH", "CONTEXT_STALE_PACK",
        "CONTEXT_MISSING_TASK",
    ]
    for code in permanent:
        assert is_permanent_context_code(code) is True, code
    for code in ("CONTEXT_PERSIST_IO_ERROR", "CONTEXT_ARTIFACT_WRITE_ERROR"):
        assert is_permanent_context_code(code) is False, code
    # Unknown context codes fail-closed (never a retry loop).
    assert is_permanent_context_code("CONTEXT_SOMETHING_UNKNOWN") is True


def test_f6_permanent_context_code_blocks(db_path):
    env = make_d1_env(db_path, context_builder=FailingContextBuilder(
        ContextBuildError(CONTEXT_BUDGET_EXCEEDED, "required exceeds hard")))
    sched = make_d1_scheduler(env)

    final = drive_d1(sched, env.jid)

    assert final.outcome == "context_build_failed"
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["primary_state"] == "BLOCKED"
    assert row["terminal"] == "BLOCKED"
    assert row["error_class"] == "CONTEXT"
    assert row["last_error_code"] == CONTEXT_BUDGET_EXCEEDED
    assert env.backend.created == [] and env.launch.spawns == []
    env.core.close()


def test_f6_transient_context_error_bounded_requeue(db_path):
    env = make_d1_env(db_path, context_builder=FailingContextBuilder(
        ContextBuildError("CONTEXT_PERSIST_IO_ERROR", "transient io")))
    sched = make_d1_scheduler(env)

    final = drive_d1(sched, env.jid)

    assert final.outcome == "context_build_failed"
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["primary_state"] == "QUEUED"
    assert row["terminal"] is None
    assert row["error_class"] == "CONTEXT"
    assert row["next_eligible_at"] is not None
    assert env.backend.created == [] and env.launch.spawns == []
    env.core.close()

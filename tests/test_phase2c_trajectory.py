"""TrajectoryRunStatusProvider unit tests (SPEC V2C §6.2, A1/A4/A10).

Builds temp OpenClaw trajectory/session files under a temp ``state_dir`` and
verifies the read-only allow-list adapter: run status, thinking tier (C1 rule),
sessionKey vs sessionId semantics, and result extraction (ms vs ISO timestamps,
thinking/tool blocks ignored).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argent_core.models import Role  # noqa: E402
from argent_core.supervisor import (  # noqa: E402
    RunLookup,
    RunStatus,
    TrajectoryRunStatusProvider,
    session_key_for,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
MID = datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc)


def make_traj(state_dir, agent_id, dispatch_id, *, provider, model,
              think_level=None, status="success", result_text=None):
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    label = f"dispatch-{dispatch_id}"
    traj = session_dir / f"{label}.trajectory.jsonl"
    session_key = session_key_for(agent_id, dispatch_id)
    session_file = session_dir / f"{label}.jsonl"
    run_id = "run-0001"

    def line(o):
        return json.dumps(o)

    started = {
        "type": "session.started", "ts": _iso(START), "sessionId": label,
        "sessionKey": session_key, "runId": run_id, "provider": provider,
        "modelId": model,
        "data": {"agentId": agent_id, "sessionFile": str(session_file)},
    }
    entries = [started]
    if think_level is not None:
        entries.append({
            "type": "trace.metadata", "ts": _iso(START), "runId": run_id,
            "data": {"model": {"thinkLevel": think_level}},
        })
    entries.append({
        "type": "session.ended", "ts": _iso(END), "sessionId": label,
        "sessionKey": session_key, "runId": run_id, "provider": provider,
        "modelId": model,
        "data": {"status": status, "aborted": False, "timedOut": False,
                 "externalAbort": False, "agentId": agent_id},
    })
    traj.write_text("\n".join(line(e) for e in entries) + "\n", encoding="utf-8")

    if result_text is not None:
        msg = json.dumps({
            "type": "message",
            "message": {
                "role": "assistant",
                "timestamp": int(MID.timestamp() * 1000),
                "content": [
                    {"type": "thinking", "thinking": "private reasoning (ignored)"},
                    {"type": "text", "text": result_text},
                ],
            },
        })
        session_file.write_text(msg + "\n", encoding="utf-8")
    else:
        session_file.write_text("", encoding="utf-8")
    return session_file, traj, run_id, session_key


def make_lookup(agent_id, dispatch_id, bound_session=None, bound_run=None,
                expected_provider=None, expected_model=None,
                expected_thinking_tier=None):
    return RunLookup(
        dispatch_id=dispatch_id, agent_id=agent_id,
        expected_session_label=f"dispatch-{dispatch_id}",
        bound_session_id=bound_session, bound_run_id=bound_run,
        expected_provider=expected_provider,
        expected_model=expected_model,
        expected_thinking_tier=expected_thinking_tier,
    )


def test_deepseek_thinking_tier_and_result(tmp_path):
    state_dir = tmp_path / "state"
    sf, traj, run_id, session_key = make_traj(
        state_dir, "argent-analyst", "d1", provider="deepseek",
        model="deepseek-v4-pro", think_level="medium",
        result_text='{"role": "analyst", "task_id": "t1", "dispatch_id": "d1"}',
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup("argent-analyst", "d1"))
    assert obs.status is RunStatus.SUCCEEDED
    assert obs.thinking_tier == "medium"
    assert obs.provider == "deepseek"
    assert obs.model == "deepseek-v4-pro"
    assert obs.session_id == session_key  # A4: sessionKey, not sessionId
    assert obs.session_id != "dispatch-d1"
    assert obs.result is not None
    assert obs.result["dispatch_id"] == "d1"


def test_openai_no_trace_metadata_thinking_high(tmp_path):
    state_dir = tmp_path / "state"
    # Lead (OpenAI) has NO trace.metadata line -> C1 rule yields 'high'.
    make_traj(state_dir, "argent-lead", "d2", provider="openai",
              model="gpt-5.6-sol", think_level=None, status="success",
              result_text='{"role": "lead", "task_id": "t1", "dispatch_id": "d2"}')
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup("argent-lead", "d2"))
    assert obs.status is RunStatus.SUCCEEDED
    assert obs.thinking_tier == "high"


def test_running_no_ended(tmp_path):
    state_dir = tmp_path / "state"
    session_dir = state_dir / "agents" / "argent-analyst" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    traj = session_dir / "dispatch-d3.trajectory.jsonl"
    traj.write_text(json.dumps({
        "type": "session.started", "ts": _iso(START), "sessionId": "dispatch-d3",
        "sessionKey": session_key_for("argent-analyst", "d3"), "runId": "r3",
        "provider": "deepseek", "modelId": "deepseek-v4-pro",
        "data": {"agentId": "argent-analyst",
                 "sessionFile": str(session_dir / "dispatch-d3.jsonl")},
    }) + "\n", encoding="utf-8")
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup("argent-analyst", "d3"))
    assert obs.status is RunStatus.RUNNING
    assert obs.run_id == "r3"


def test_not_found_authoritative(tmp_path):
    state_dir = tmp_path / "state"
    (state_dir / "agents" / "argent-analyst" / "sessions").mkdir(parents=True)
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup("argent-analyst", "nope"))
    assert obs.status is RunStatus.NOT_FOUND
    assert obs.authoritative_not_found is True


def test_session_dir_missing_is_unknown(tmp_path):
    state_dir = tmp_path / "state"
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup("argent-analyst", "nope"))
    assert obs.status is RunStatus.UNKNOWN
    assert obs.authoritative_not_found is False


def test_malformed_jsonl_unknown(tmp_path):
    state_dir = tmp_path / "state"
    session_dir = state_dir / "agents" / "argent-analyst" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "dispatch-d4.trajectory.jsonl").write_text(
        '{"type": "session.started", "ts": "2026-01-01T00:00:00Z", '
        '"sessionId": "dispatch-d4"}\n{not json\n', encoding="utf-8",
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup("argent-analyst", "d4"))
    assert obs.status is RunStatus.UNKNOWN


def test_bound_run_missing_authoritative(tmp_path):
    state_dir = tmp_path / "state"
    (state_dir / "agents" / "argent-analyst" / "sessions").mkdir(parents=True)
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    # Bound lookup but the trajectory does not contain that run -> NOT_FOUND.
    obs = prov.observe(make_lookup(
        "argent-analyst", "d5", bound_session="agent:argent-analyst:explicit:dispatch-d5",
        bound_run="missing-run",
    ))
    assert obs.status is RunStatus.NOT_FOUND
    assert obs.authoritative_not_found is True


def test_session_key_distinction_a4(tmp_path):
    state_dir = tmp_path / "state"
    sf, traj, run_id, session_key = make_traj(
        state_dir, "argent-qa", "d6", provider="deepseek",
        model="deepseek-v4-pro", think_level="medium", status="success",
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup("argent-qa", "d6"))
    # sessionId label is 'dispatch-d6'; bound value is the full sessionKey.
    assert obs.session_id == "agent:argent-qa:explicit:dispatch-d6"
    assert "dispatch-d6" in obs.session_id


# F4: terminal rows must match exact sessionKey/agentId/provider/model + runId.
@pytest.mark.parametrize("field", [
    "session_key", "agent_id", "provider", "model",
])
def test_f4_terminal_wrong_binding_conflict(tmp_path, field):
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d7"
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    label = f"dispatch-{dispatch_id}"
    session_key = session_key_for(agent_id, dispatch_id)
    run_id = "run-0001"

    started = {
        "type": "session.started", "ts": _iso(START), "sessionId": label,
        "sessionKey": session_key, "runId": run_id, "provider": "deepseek",
        "modelId": "deepseek-v4-pro",
        "data": {"agentId": agent_id,
                 "sessionFile": str(session_dir / f"{label}.jsonl")},
    }
    # A terminal row whose exact binding differs in one field.
    if field == "session_key":
        ended_session_key = f"agent:argent-lead:explicit:{label}"
        ended_agent = agent_id
        ended_provider = "deepseek"
        ended_model = "deepseek-v4-pro"
    elif field == "agent_id":
        ended_session_key = session_key
        ended_agent = "argent-lead"
        ended_provider = "deepseek"
        ended_model = "deepseek-v4-pro"
    elif field == "provider":
        ended_session_key = session_key
        ended_agent = agent_id
        ended_provider = "openai"
        ended_model = "deepseek-v4-pro"
    else:
        ended_session_key = session_key
        ended_agent = agent_id
        ended_provider = "deepseek"
        ended_model = "gpt-5.6-sol"

    ended = {
        "type": "session.ended", "ts": _iso(END), "sessionId": label,
        "sessionKey": ended_session_key, "runId": run_id,
        "provider": ended_provider, "modelId": ended_model,
        "data": {"status": "success", "agentId": ended_agent},
    }
    (session_dir / f"{label}.trajectory.jsonl").write_text(
        json.dumps(started) + "\n" + json.dumps(ended) + "\n", encoding="utf-8")

    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.CONFLICT, (field, obs.status, obs.error_code)
    assert obs.status is not RunStatus.SUCCEEDED


def test_active_session_without_trajectory_is_running(tmp_path):
    """Real E2E finding: the runtime flushes the trajectory late.  A session
    file (or lock) without a trajectory means the run is ACTIVE -> RUNNING,
    never NOT_FOUND (so slow real runs do not burn the missing budget)."""
    from argent_core.supervisor import (
        RunStatus, TrajectoryRunStatusProvider, RunLookup, session_key_for,
    )
    state = tmp_path / ".openclaw"
    agent_dir = state / "agents" / "argent-analyst" / "sessions"
    agent_dir.mkdir(parents=True)
    dispatch_id = "dddddddd-0000-0000-0000-000000000000"
    (agent_dir / f"dispatch-{dispatch_id}.jsonl").write_text(
        '{"type":"session","id":"x"}\n')
    p = TrajectoryRunStatusProvider(state_dir=state)
    obs = p.observe(RunLookup(
        dispatch_id=dispatch_id, agent_id="argent-analyst",
        expected_session_label=f"dispatch-{dispatch_id}",
        bound_session_id=None, bound_run_id=None,
    ))
    assert obs.status is RunStatus.RUNNING, obs.status
    assert obs.session_id == session_key_for("argent-analyst", dispatch_id)
    assert obs.authoritative_not_found is False

    # Lock-only is also an active session.
    (agent_dir / f"dispatch-{dispatch_id}.jsonl").unlink()
    (agent_dir / f"dispatch-{dispatch_id}.jsonl.lock").write_text("x")
    obs2 = p.observe(RunLookup(
        dispatch_id=dispatch_id, agent_id="argent-analyst",
        expected_session_label=f"dispatch-{dispatch_id}",
        bound_session_id=None, bound_run_id=None,
    ))
    assert obs2.status is RunStatus.RUNNING, obs2.status

    # Nothing at all -> authoritative NOT_FOUND.
    (agent_dir / f"dispatch-{dispatch_id}.jsonl.lock").unlink()
    obs3 = p.observe(RunLookup(
        dispatch_id=dispatch_id, agent_id="argent-analyst",
        expected_session_label=f"dispatch-{dispatch_id}",
        bound_session_id=None, bound_run_id=None,
    ))
    assert obs3.status is RunStatus.NOT_FOUND, obs3.status
    assert obs3.authoritative_not_found is True


def test_result_extraction_uses_last_assistant_message(tmp_path):
    """Real E2E finding: an intermediate assistant message carrying a foreign
    task_id must never be used; only the FINAL assistant message is the
    agent's reply envelope."""
    import json as _json
    from argent_core.supervisor import (
        RunStatus, TrajectoryRunStatusProvider, RunLookup,
    )
    state = tmp_path / ".openclaw"
    agent_dir = state / "agents" / "argent-lead" / "sessions"
    agent_dir.mkdir(parents=True)
    dispatch_id = "eeeeeeee-0000-0000-0000-000000000000"
    sess_file = agent_dir / f"dispatch-{dispatch_id}.jsonl"
    start_ts = "2026-08-28T20:00:00.000Z"
    # Intermediate assistant message with a FOREIGN task_id (status card).
    foreign = ('{"type":"message","message":{"role":"assistant",'
               '"content":[{"type":"text","text":"{\\"task_id\\": \\"foreign-0000\\", '
               '\\"dispatch_id\\": \\"x\\", \\"role\\": \\"lead\\"}"}],'
               '"timestamp": 1787947200000}}')
    final = ('{"type":"message","message":{"role":"assistant",'
             '"content":[{"type":"text","text":"{\\"task_id\\": \\"'
             + 'eeeeeeee-0000-0000-0000-000000000000' + '\\", \\"dispatch_id\\": \\"'
             + dispatch_id + '\\", \\"role\\": \\"lead\\", \\"decision\\": \\"accept\\", '
             '\\"status\\": \\"ok\\", \\"findings\\": [], \\"own_assessment\\": \\"a\\", '
             '\\"concerns\\": [], \\"proposal\\": \\"p\\", \\"alternatives\\": [], '
             '\\"confidence\\": 0.9, \\"blockers\\": [], \\"requested_next_state\\": \\"s\\", '
             '\\"accepted_findings\\": [], \\"rejected_findings\\": [], \\"rationale\\": \\"r\\"}"}],'
             '"timestamp": 1787947201000}}')
    sess_file.write_text(foreign + "\n" + final + "\n")
    traj = agent_dir / f"dispatch-{dispatch_id}.trajectory.jsonl"
    traj.write_text(_json.dumps({
        "type": "session.started", "sessionId": f"dispatch-{dispatch_id}",
        "sessionKey": f"agent:argent-lead:explicit:dispatch-{dispatch_id}",
        "runId": "run-abc", "ts": start_ts,
        "provider": "openai", "modelId": "gpt-5.6-sol",
        "data": {"sessionFile": str(sess_file), "agentId": "argent-lead"},
    }) + "\n" + _json.dumps({
        "type": "session.ended", "ts": "2026-08-28T20:00:02.000Z",
        "sessionId": f"dispatch-{dispatch_id}",
        "sessionKey": f"agent:argent-lead:explicit:dispatch-{dispatch_id}",
        "runId": "run-abc",
        "provider": "openai", "modelId": "gpt-5.6-sol",
        "data": {"status": "success", "agentId": "argent-lead"},
    }) + "\n")
    p = TrajectoryRunStatusProvider(state_dir=state)
    obs = p.observe(RunLookup(
        dispatch_id=dispatch_id, agent_id="argent-lead",
        expected_session_label=f"dispatch-{dispatch_id}",
        bound_session_id=None, bound_run_id=None,
    ))
    assert obs.status is RunStatus.SUCCEEDED, obs.status
    r = obs.result or {}
    assert r.get("task_id") == dispatch_id, r.get("task_id")
    assert r.get("dispatch_id") == dispatch_id, r.get("dispatch_id")


# ---------------------------------------------------------------------------
# Phase-2C Fix Round (F4): strict start/terminal/bound validation
# ---------------------------------------------------------------------------

def test_f4_foreign_terminal_run_id_conflict(tmp_path):
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d8"
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    label = f"dispatch-{dispatch_id}"
    session_key = session_key_for(agent_id, dispatch_id)
    started = {
        "type": "session.started", "ts": _iso(START), "sessionId": label,
        "sessionKey": session_key, "runId": "run-0001", "provider": "deepseek",
        "modelId": "deepseek-v4-pro",
        "data": {"agentId": agent_id,
                 "sessionFile": str(session_dir / f"{label}.jsonl")},
    }
    ended_foreign = {
        "type": "session.ended", "ts": _iso(END), "sessionId": label,
        "sessionKey": session_key, "runId": "run-FOREIGN",
        "provider": "deepseek", "modelId": "deepseek-v4-pro",
        "data": {"status": "success", "agentId": agent_id},
    }
    (session_dir / f"{label}.trajectory.jsonl").write_text(
        json.dumps(started) + "\n" + json.dumps(ended_foreign) + "\n",
        encoding="utf-8",
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.CONFLICT
    assert obs.error_code == "terminal_run_id_mismatch"


def test_f4_duplicate_start_same_run_id_diff_provider_conflict(tmp_path):
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d9"
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    label = f"dispatch-{dispatch_id}"
    session_key = session_key_for(agent_id, dispatch_id)

    def start(provider):
        return {
            "type": "session.started", "ts": _iso(START), "sessionId": label,
            "sessionKey": session_key, "runId": "run-0001",
            "provider": provider, "modelId": "deepseek-v4-pro",
            "data": {"agentId": agent_id,
                     "sessionFile": str(session_dir / f"{label}.jsonl")},
        }

    (session_dir / f"{label}.trajectory.jsonl").write_text(
        json.dumps(start("deepseek")) + "\n" + json.dumps(start("openai")) + "\n",
        encoding="utf-8",
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.CONFLICT
    assert obs.error_code == "start_provider_mismatch"


def test_f4_foreign_bound_session_id_conflict(tmp_path):
    state_dir = tmp_path / "state"
    sf, traj, run_id, session_key = make_traj(
        state_dir, "argent-analyst", "d10", provider="deepseek",
        model="deepseek-v4-pro", think_level="medium", status="success",
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    # Bound session id points to a FOREIGN session key -> CONFLICT (never
    # SUCCEEDED).
    obs = prov.observe(make_lookup(
        "argent-analyst", "d10",
        bound_session="agent:argent-lead:explicit:dispatch-d10",
        bound_run=run_id,
    ))
    assert obs.status is RunStatus.CONFLICT
    assert obs.error_code == "foreign_bound_session_id"


# ---------------------------------------------------------------------------
# Phase-2C Fix Round 3 (F1): provider/model/thinking/agent-identity binding
# ---------------------------------------------------------------------------

def _write_traj_rows(state_dir, agent_id, dispatch_id, *, run_id, start_row,
                     end_row):
    """Write a raw start/end trajectory pair for a dispatch (F1 tests)."""
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    label = f"dispatch-{dispatch_id}"
    traj = session_dir / f"{label}.trajectory.jsonl"
    traj.write_text(
        json.dumps(start_row) + "\n" + json.dumps(end_row) + "\n",
        encoding="utf-8",
    )
    return traj


def _bound_lookup(agent_id, dispatch_id, provider, model, thinking,
                  session=None, run=None):
    return make_lookup(
        agent_id, dispatch_id, bound_session=session, bound_run=run,
        expected_provider=provider, expected_model=model,
        expected_thinking_tier=thinking,
    )


def test_f1_bound_foreign_consistent_tuple_conflict(tmp_path):
    """A bound dispatch whose start+terminal rows consistently use a foreign
    provider/model tuple must be CONFLICT (never SUCCEEDED)."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-f1-1"
    session_dir = state_dir / "agents" / agent_id / "sessions"
    label = f"dispatch-{dispatch_id}"
    session_key = session_key_for(agent_id, dispatch_id)
    run_id = "run-f1-1"

    def row(typ):
        base = {
            "ts": _iso(START if typ == "session.started" else END),
            "sessionId": label, "sessionKey": session_key, "runId": run_id,
            "provider": "openai", "modelId": "gpt-5.6-sol",
        }
        if typ == "session.started":
            base["type"] = "session.started"
            base["data"] = {"agentId": agent_id,
                             "sessionFile": str(session_dir / f"{label}.jsonl")}
        else:
            base["type"] = "session.ended"
            base["data"] = {"status": "success", "agentId": agent_id}
        return base

    _write_traj_rows(state_dir, agent_id, dispatch_id, run_id=run_id,
                     start_row=row("session.started"),
                     end_row=row("session.ended"))
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(_bound_lookup(
        agent_id, dispatch_id, "deepseek", "deepseek-v4-pro", "medium",
        session=session_key, run=run_id,
    ))
    assert obs.status is RunStatus.CONFLICT, (obs.status, obs.error_code)
    assert obs.status is not RunStatus.SUCCEEDED


def test_f1_thinking_tier_binding_mismatch_conflict(tmp_path):
    """A DeepSeek trajectory reporting a foreign thinkLevel must CONFLICT
    against the dispatch's expected 'medium' tier."""
    state_dir = tmp_path / "state"
    sf, traj, run_id, session_key = make_traj(
        state_dir, "argent-analyst", "d-f1-2", provider="deepseek",
        model="deepseek-v4-pro", think_level="low", status="success",
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(_bound_lookup(
        "argent-analyst", "d-f1-2", "deepseek", "deepseek-v4-pro", "medium",
        session=session_key, run=run_id,
    ))
    assert obs.status is RunStatus.CONFLICT, (obs.status, obs.error_code)
    assert obs.error_code == "thinking_tier_binding_mismatch"


def test_f1_missing_agent_id_start_conflict(tmp_path):
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-f1-3"
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    label = f"dispatch-{dispatch_id}"
    session_key = session_key_for(agent_id, dispatch_id)
    run_id = "run-f1-3"
    start = {
        "type": "session.started", "ts": _iso(START), "sessionId": label,
        "sessionKey": session_key, "runId": run_id,
        "provider": "deepseek", "modelId": "deepseek-v4-pro",
        # NOTE: data.agentId is intentionally MISSING.
        "data": {"sessionFile": str(session_dir / f"{label}.jsonl")},
    }
    end = {
        "type": "session.ended", "ts": _iso(END), "sessionId": label,
        "sessionKey": session_key, "runId": run_id,
        "provider": "deepseek", "modelId": "deepseek-v4-pro",
        "data": {"status": "success", "agentId": agent_id},
    }
    _write_traj_rows(state_dir, agent_id, dispatch_id, run_id=run_id,
                     start_row=start, end_row=end)
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(_bound_lookup(
        agent_id, dispatch_id, "deepseek", "deepseek-v4-pro", "medium",
        session=session_key, run=run_id,
    ))
    assert obs.status is RunStatus.CONFLICT, (obs.status, obs.error_code)
    assert obs.error_code == "start_missing_agent_id"


def test_f1_missing_agent_id_terminal_conflict(tmp_path):
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-f1-4"
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    label = f"dispatch-{dispatch_id}"
    session_key = session_key_for(agent_id, dispatch_id)
    run_id = "run-f1-4"
    start = {
        "type": "session.started", "ts": _iso(START), "sessionId": label,
        "sessionKey": session_key, "runId": run_id,
        "provider": "deepseek", "modelId": "deepseek-v4-pro",
        "data": {"agentId": agent_id,
                 "sessionFile": str(session_dir / f"{label}.jsonl")},
    }
    end = {
        "type": "session.ended", "ts": _iso(END), "sessionId": label,
        "sessionKey": session_key, "runId": run_id,
        "provider": "deepseek", "modelId": "deepseek-v4-pro",
        # NOTE: data.agentId is intentionally MISSING on the terminal row.
        "data": {"status": "success"},
    }
    # Include a matching trace.metadata line so the thinking-tier check passes
    # (the terminal agentId check is what must fire).
    meta = {
        "type": "trace.metadata", "ts": _iso(START), "runId": run_id,
        "data": {"model": {"thinkLevel": "medium"}},
    }
    (state_dir / "agents" / agent_id / "sessions"
     / f"{label}.trajectory.jsonl").write_text(
        json.dumps(start) + "\n" + json.dumps(meta) + "\n"
        + json.dumps(end) + "\n", encoding="utf-8",
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(_bound_lookup(
        agent_id, dispatch_id, "deepseek", "deepseek-v4-pro", "medium",
        session=session_key, run=run_id,
    ))
    assert obs.status is RunStatus.CONFLICT, (obs.status, obs.error_code)
    assert obs.error_code == "terminal_missing_agent_id"


def test_f1_canonical_deepseek_tuple_succeeds(tmp_path):
    """The correct canonical bound tuple still SUCCEEDs (no false positive)."""
    state_dir = tmp_path / "state"
    sf, traj, run_id, session_key = make_traj(
        state_dir, "argent-analyst", "d-f1-5", provider="deepseek",
        model="deepseek-v4-pro", think_level="medium", status="success",
        result_text='{"role": "analyst", "task_id": "t1", '
                    '"dispatch_id": "d-f1-5"}',
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(_bound_lookup(
        "argent-analyst", "d-f1-5", "deepseek", "deepseek-v4-pro", "medium",
        session=session_key, run=run_id,
    ))
    assert obs.status is RunStatus.SUCCEEDED, (obs.status, obs.error_code)


def test_f1_canonical_openai_lead_tuple_succeeds(tmp_path):
    """The OpenAI lead (no trace.metadata, C1 rule) bound to 'high' still
    SUCCEEDs (no false positive on the thinking-tier binding check)."""
    state_dir = tmp_path / "state"
    sf, traj, run_id, session_key = make_traj(
        state_dir, "argent-lead", "d-f1-6", provider="openai",
        model="gpt-5.6-sol", think_level=None, status="success",
        result_text='{"role": "lead", "task_id": "t1", '
                    '"dispatch_id": "d-f1-6"}',
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(_bound_lookup(
        "argent-lead", "d-f1-6", "openai", "gpt-5.6-sol", "high",
        session=session_key, run=run_id,
    ))
    assert obs.status is RunStatus.SUCCEEDED, (obs.status, obs.error_code)
    assert obs.thinking_tier == "high"


# ---------------------------------------------------------------------------
# Phase-2C Fix Round 4 (F-R4): malformed trajectory structure is fail-closed
# ---------------------------------------------------------------------------

def _write_raw_rows(state_dir, agent_id, dispatch_id, raw_rows):
    """Write pre-serialized JSON lines to a dispatch trajectory file."""
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    traj = session_dir / f"dispatch-{dispatch_id}.trajectory.jsonl"
    traj.write_text("\n".join(raw_rows) + "\n", encoding="utf-8")
    return traj


def _wellformed_start(state_dir, agent_id, dispatch_id):
    session_dir = state_dir / "agents" / agent_id / "sessions"
    label = f"dispatch-{dispatch_id}"
    return {
        "type": "session.started", "ts": _iso(START), "sessionId": label,
        "sessionKey": session_key_for(agent_id, dispatch_id),
        "runId": "run-0001", "provider": "deepseek",
        "modelId": "deepseek-v4-pro",
        "data": {"agentId": agent_id,
                 "sessionFile": str(session_dir / f"{label}.jsonl")},
    }


def _wellformed_end(agent_id, dispatch_id):
    label = f"dispatch-{dispatch_id}"
    return {
        "type": "session.ended", "ts": _iso(END), "sessionId": label,
        "sessionKey": session_key_for(agent_id, dispatch_id),
        "runId": "run-0001", "provider": "deepseek",
        "modelId": "deepseek-v4-pro",
        "data": {"status": "success", "agentId": agent_id},
    }


@pytest.mark.parametrize("raw", ["42", '"x"', "[]", "[1, 2]", "null", "true"])
def test_f4_non_object_top_level_row_unknown(tmp_path, raw):
    """A scalar/list/null top-level JSONL row is structurally malformed and
    can never be interpreted as a valid start/terminal -> deterministic
    UNKNOWN (never raise, never RUNNING/SUCCEEDED)."""
    state_dir = tmp_path / "state"
    _write_raw_rows(state_dir, "argent-analyst", "d-r4-top", [raw])
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup("argent-analyst", "d-r4-top"))
    assert obs.status is RunStatus.UNKNOWN, (raw, obs.status, obs.error_code)
    assert obs.error_code == "malformed_row"


def test_f4_non_object_row_among_wellformed_unknown(tmp_path):
    """A scalar row mixed with well-formed start/end rows must still fail
    closed (the malformed row is never silently ignored)."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r4-mix"
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    _write_raw_rows(
        state_dir, agent_id, dispatch_id,
        [json.dumps(_wellformed_start(state_dir, agent_id, dispatch_id)),
         "42",
         json.dumps(_wellformed_end(agent_id, dispatch_id))],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.UNKNOWN, (obs.status, obs.error_code)
    assert obs.error_code == "malformed_row"


def test_f4_start_scalar_data_conflict(tmp_path):
    """``session.started`` with a non-object ``data`` (scalar string) is a
    malformed start identity row -> CONFLICT (never SUCCEEDED/RUNNING)."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r4-start"
    started = _wellformed_start(state_dir, agent_id, dispatch_id)
    started["data"] = "agentId"  # scalar string, not an object
    _write_raw_rows(
        state_dir, agent_id, dispatch_id,
        [json.dumps(started),
         json.dumps(_wellformed_end(agent_id, dispatch_id))],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.CONFLICT, (obs.status, obs.error_code)
    assert obs.error_code == "start_malformed_data"
    assert obs.status is not RunStatus.SUCCEEDED
    assert obs.status is not RunStatus.RUNNING


def test_f4_terminal_scalar_data_conflict(tmp_path):
    """``session.ended`` with a non-object ``data`` (scalar string) is a
    malformed terminal identity row -> CONFLICT (never SUCCEEDED)."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r4-end"
    ended = _wellformed_end(agent_id, dispatch_id)
    ended["data"] = "agentId"  # scalar string, not an object
    _write_raw_rows(
        state_dir, agent_id, dispatch_id,
        [json.dumps(_wellformed_start(state_dir, agent_id, dispatch_id)),
         json.dumps(ended)],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.CONFLICT, (obs.status, obs.error_code)
    assert obs.error_code == "terminal_malformed_data"
    assert obs.status is not RunStatus.SUCCEEDED


def _wellformed_metadata(dispatch_id):
    return {
        "type": "trace.metadata", "ts": _iso(START),
        "runId": "run-0001",
        "data": {"model": {"thinkLevel": "medium"}},
    }


def test_f4_malformed_metadata_data_conflict(tmp_path):
    """A ``trace.metadata`` row whose ``data`` is a scalar (not an object) is
    uninterpretable corruption -> CONFLICT (never raise, never guess a tier)."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r4-meta-data"
    meta = _wellformed_metadata(dispatch_id)
    meta["data"] = "x"  # scalar, not an object
    _write_raw_rows(
        state_dir, agent_id, dispatch_id,
        [json.dumps(_wellformed_start(state_dir, agent_id, dispatch_id)),
         json.dumps(meta),
         json.dumps(_wellformed_end(agent_id, dispatch_id))],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.CONFLICT, (obs.status, obs.error_code)
    assert obs.error_code == "metadata_malformed"


def test_f4_malformed_metadata_model_conflict(tmp_path):
    """A ``trace.metadata`` row whose ``data.model`` is a scalar (not an
    object) is uninterpretable -> CONFLICT."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r4-meta-model"
    meta = _wellformed_metadata(dispatch_id)
    meta["data"]["model"] = 42  # scalar, not an object
    _write_raw_rows(
        state_dir, agent_id, dispatch_id,
        [json.dumps(_wellformed_start(state_dir, agent_id, dispatch_id)),
         json.dumps(meta),
         json.dumps(_wellformed_end(agent_id, dispatch_id))],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.CONFLICT, (obs.status, obs.error_code)
    assert obs.error_code == "metadata_malformed"


def test_f4_thinking_tier_malformed_metadata_none(tmp_path):
    """``_thinking_tier`` returns None (never raises) for malformed metadata
    rows of every unchecked-type class."""
    prov = TrajectoryRunStatusProvider(state_dir=tmp_path / "state")
    meta_rows = [
        {"type": "trace.metadata", "runId": "r", "data": "x"},
        {"type": "trace.metadata", "runId": "r", "data": {"model": 42}},
        {"type": "trace.metadata", "runId": "r", "data": {"model": "x"}},
        "not-a-dict",
        {"type": "trace.metadata", "runId": "r", "data": {"model": None}},
        {"type": "trace.metadata", "runId": "r"},  # no data at all
    ]
    assert prov._thinking_tier(
        meta_rows, "r", "deepseek", "deepseek-v4-pro", "argent-analyst"
    ) is None


def test_f4_wellformed_metadata_no_false_positive(tmp_path):
    """A well-formed trajectory (object data/model + thinkLevel) still
    SUCCEEDs with the observed tier (no false positive from the F-R4 guards)."""
    state_dir = tmp_path / "state"
    sf, traj, run_id, session_key = make_traj(
        state_dir, "argent-analyst", "d-r4-ok", provider="deepseek",
        model="deepseek-v4-pro", think_level="medium", status="success",
        result_text='{"role": "analyst", "task_id": "t1", '
                    '"dispatch_id": "d-r4-ok"}',
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup("argent-analyst", "d-r4-ok"))
    assert obs.status is RunStatus.SUCCEEDED, (obs.status, obs.error_code)
    assert obs.thinking_tier == "medium"


# ---------------------------------------------------------------------------
# Phase-2C Fix Round 5 (F-R5): PRESENT JSON-null is malformed, ABSENT is not
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, True, [1, 2], 42])
def test_f5_start_present_null_or_scalar_data_conflict(tmp_path, bad):
    """A ``session.started`` row with an explicitly PRESENT non-object
    ``data`` (JSON null / bool / list / number) is malformed -> CONFLICT
    ``start_malformed_data`` (never RUNNING/SUCCEEDED).  Distinct from an
    ABSENT ``data``, which keeps ``start_missing_agent_id``."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r5-start"
    started = _wellformed_start(state_dir, agent_id, dispatch_id)
    started["data"] = bad
    _write_raw_rows(
        state_dir, agent_id, dispatch_id,
        [json.dumps(started),
         json.dumps(_wellformed_end(agent_id, dispatch_id))],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.CONFLICT, (bad, obs.status, obs.error_code)
    assert obs.error_code == "start_malformed_data"
    assert obs.status is not RunStatus.RUNNING
    assert obs.status is not RunStatus.SUCCEEDED


@pytest.mark.parametrize("bad", [None, True, [1, 2], 42])
def test_f5_terminal_present_null_or_scalar_data_conflict(tmp_path, bad):
    """A ``session.ended`` row with an explicitly PRESENT non-object ``data``
    (JSON null / bool / list / number) is malformed -> CONFLICT
    ``terminal_malformed_data`` (never SUCCEEDED)."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r5-end"
    ended = _wellformed_end(agent_id, dispatch_id)
    ended["data"] = bad
    _write_raw_rows(
        state_dir, agent_id, dispatch_id,
        [json.dumps(_wellformed_start(state_dir, agent_id, dispatch_id)),
         json.dumps(ended)],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.CONFLICT, (bad, obs.status, obs.error_code)
    assert obs.error_code == "terminal_malformed_data"
    assert obs.status is not RunStatus.SUCCEEDED


@pytest.mark.parametrize("data_value", [None, {"model": None}])
def test_f5_metadata_present_null_conflict(tmp_path, data_value):
    """A ``trace.metadata`` row with an explicitly PRESENT JSON ``null``
    ``data`` or ``data.model`` is malformed -> CONFLICT ``metadata_malformed``
    (never SUCCEEDED, never guessed as a tier)."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r5-meta-null"
    meta = _wellformed_metadata(dispatch_id)
    meta["data"] = data_value
    _write_raw_rows(
        state_dir, agent_id, dispatch_id,
        [json.dumps(_wellformed_start(state_dir, agent_id, dispatch_id)),
         json.dumps(meta),
         json.dumps(_wellformed_end(agent_id, dispatch_id))],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.CONFLICT, (data_value, obs.status, obs.error_code)
    assert obs.error_code == "metadata_malformed"
    assert obs.status is not RunStatus.SUCCEEDED


@pytest.mark.parametrize("meta_row", [
    {"type": "trace.metadata", "ts": _iso(START), "runId": "run-0001"},
    {"type": "trace.metadata", "ts": _iso(START), "runId": "run-0001",
     "data": {}},
])
def test_f5_metadata_absent_data_or_model_succeeds(tmp_path, meta_row):
    """A ``trace.metadata`` row MISSING ``data`` or MISSING ``data.model`` is
    NOT malformed (no thinkLevel -> None) and the run still SUCCEEDs — no
    false positive from the F-R5 presence guard."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r5-meta-absent"
    _write_raw_rows(
        state_dir, agent_id, dispatch_id,
        [json.dumps(_wellformed_start(state_dir, agent_id, dispatch_id)),
         json.dumps(meta_row),
         json.dumps(_wellformed_end(agent_id, dispatch_id))],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.SUCCEEDED, (obs.status, obs.error_code)
    assert obs.thinking_tier is None


# ---------------------------------------------------------------------------
# Fix Round: malformed runtime data fail-closed closure (residual F-R4 gaps)
# ---------------------------------------------------------------------------
# A JSON-list ``runId`` (syntactically valid, structurally malformed) must
# never reach ``set(run_ids)`` (unhashable list -> TypeError).  Instead it is a
# deterministic CONFLICT.  Same for terminal rows.

def test_malformed_start_json_list_run_id_conflict(tmp_path):
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r6-start-rid"
    started = _wellformed_start(state_dir, agent_id, dispatch_id)
    started["runId"] = ["a", "b"]  # JSON array runId
    _write_raw_rows(
        state_dir, agent_id, dispatch_id,
        [json.dumps(started),
         json.dumps(_wellformed_end(agent_id, dispatch_id))],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.CONFLICT, (obs.status, obs.error_code)
    assert obs.error_code == "start_run_id_not_string"
    assert obs.status is not RunStatus.SUCCEEDED
    assert obs.status is not RunStatus.RUNNING


def test_malformed_terminal_json_list_run_id_conflict(tmp_path):
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r6-end-rid"
    ended = _wellformed_end(agent_id, dispatch_id)
    ended["runId"] = ["a", "b"]  # JSON array runId
    _write_raw_rows(
        state_dir, agent_id, dispatch_id,
        [json.dumps(_wellformed_start(state_dir, agent_id, dispatch_id)),
         json.dumps(ended)],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.CONFLICT, (obs.status, obs.error_code)
    assert obs.error_code == "terminal_run_id_not_string"
    assert obs.status is not RunStatus.SUCCEEDED


def _write_session_with_rows(state_dir, agent_id, dispatch_id, *,
                             started, ended, session_rows):
    """Write a trajectory + its referenced session file for malformed-data
    result-extraction tests."""
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    label = f"dispatch-{dispatch_id}"
    session_file = session_dir / f"{label}.jsonl"
    session_file.write_text("\n".join(session_rows) + "\n", encoding="utf-8")
    traj = session_dir / f"{label}.trajectory.jsonl"
    traj.write_text(
        json.dumps(started) + "\n" + json.dumps(ended) + "\n", encoding="utf-8")
    return session_file


def _started_row(state_dir, agent_id, dispatch_id, session_file, *, ts):
    label = f"dispatch-{dispatch_id}"
    return {
        "type": "session.started", "ts": ts, "sessionId": label,
        "sessionKey": session_key_for(agent_id, dispatch_id),
        "runId": "run-0001", "provider": "deepseek",
        "modelId": "deepseek-v4-pro",
        "data": {"agentId": agent_id, "sessionFile": str(session_file)},
    }


def _ended_row(agent_id, dispatch_id, *, ts):
    label = f"dispatch-{dispatch_id}"
    return {
        "type": "session.ended", "ts": ts, "sessionId": label,
        "sessionKey": session_key_for(agent_id, dispatch_id),
        "runId": "run-0001", "provider": "deepseek",
        "modelId": "deepseek-v4-pro",
        "data": {"status": "success", "agentId": agent_id},
    }


def _assistant_line(dispatch_id, *, timestamp=None, text=None):
    msg = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    if timestamp is not None:
        msg["timestamp"] = timestamp
    return json.dumps({"type": "message", "message": msg})


def test_session_file_top_level_null_row_no_crash(tmp_path):
    """A top-level JSON ``null`` row in the session file is structurally
    malformed and must be skipped (never raise), while a following valid
    assistant message still yields the result."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r6-null"
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"dispatch-{dispatch_id}.jsonl"
    result_text = ('{"role": "analyst", "task_id": "t1", '
                   '"dispatch_id": "d-r6-null"}')
    _write_session_with_rows(
        state_dir, agent_id, dispatch_id,
        started=_started_row(state_dir, agent_id, dispatch_id, session_file,
                             ts=_iso(START)),
        ended=_ended_row(agent_id, dispatch_id, ts=_iso(END)),
        session_rows=[
            "null",
            "[]",
            '"scalar"',
            _assistant_line(dispatch_id, text=result_text),
        ],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.SUCCEEDED, (obs.status, obs.error_code)
    assert obs.result is not None
    assert obs.result["dispatch_id"] == "d-r6-null"


@pytest.mark.parametrize("bad_ts", ["not-a-number", [1, 2, 3], {"a": 1}, True])
def test_non_numeric_message_timestamp_no_crash(tmp_path, bad_ts):
    """A non-numeric message ``timestamp`` (string/list/dict/bool) is ignored
    for time filtering (never raise); the assistant text is still extracted."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r6-msg-ts"
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"dispatch-{dispatch_id}.jsonl"
    result_text = ('{"role": "analyst", "task_id": "t1", '
                   '"dispatch_id": "d-r6-msg-ts"}')
    _write_session_with_rows(
        state_dir, agent_id, dispatch_id,
        started=_started_row(state_dir, agent_id, dispatch_id, session_file,
                             ts=_iso(START)),
        ended=_ended_row(agent_id, dispatch_id, ts=_iso(END)),
        session_rows=[
            _assistant_line(dispatch_id, timestamp=bad_ts, text=result_text),
        ],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.SUCCEEDED, (obs.status, obs.error_code)
    assert obs.result is not None
    assert obs.result["dispatch_id"] == "d-r6-msg-ts"


def test_non_string_trajectory_ts_no_crash(tmp_path):
    """Non-string trajectory ``ts`` values (JSON list/number) are unparsable
    timestamps -> treated as no-timestamp (skip time filtering, never raise);
    result extraction still works."""
    state_dir = tmp_path / "state"
    agent_id = "argent-analyst"
    dispatch_id = "d-r6-ts"
    session_dir = state_dir / "agents" / agent_id / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"dispatch-{dispatch_id}.jsonl"
    result_text = ('{"role": "analyst", "task_id": "t1", '
                   '"dispatch_id": "d-r6-ts"}')
    started = _started_row(state_dir, agent_id, dispatch_id, session_file,
                           ts=["not", "iso"])  # non-string start ts
    ended = _ended_row(agent_id, dispatch_id, ts=12345)  # non-string end ts
    _write_session_with_rows(
        state_dir, agent_id, dispatch_id,
        started=started, ended=ended,
        session_rows=[_assistant_line(dispatch_id, text=result_text)],
    )
    prov = TrajectoryRunStatusProvider(state_dir=state_dir)
    obs = prov.observe(make_lookup(agent_id, dispatch_id))
    assert obs.status is RunStatus.SUCCEEDED, (obs.status, obs.error_code)
    assert obs.result is not None
    assert obs.result["dispatch_id"] == "d-r6-ts"


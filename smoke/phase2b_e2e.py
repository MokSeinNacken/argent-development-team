#!/usr/bin/env python3
"""Phase 2B E2E driver — real 5-agent workflow on e2e-fixture (SPEC V2B §5).

The controller (this script) is the only process interface to the Core
(source ``role:lead``).  Role agents run as direct OpenClaw agent turns with
exactly one tool (session_status) and zero write capabilities; they only
return structured JSON.  The controller applies their patch sets through the
write-broker, runs tests in the bwrap sandbox, and records test runs in the
Core.  Everything the agents say is UNTRUSTED DATA.

Usage:
  python3 smoke/phase2b_e2e.py init [--db PATH]            # fresh DB + Task 1
  python3 smoke/phase2b_e2e.py init-rework [--db PATH]     # Task 2 (rework)
  python3 smoke/phase2b_e2e.py status [--db PATH]
  python3 smoke/phase2b_e2e.py next [--db PATH]            # expected role
  python3 smoke/phase2b_e2e.py run [--db PATH]             # dispatch next role
  python3 smoke/phase2b_e2e.py unexpected-smoke [--db PATH]
  python3 smoke/phase2b_e2e.py recovery-smoke              # scratch DB demo

The driver is resumable: every step is persisted in the SQLite ledger.  A
failed/rejected dispatch does not move the frontier; simply re-run ``run``.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from argent_core import (  # noqa: E402
    Core,
    OWNER_SOURCE,
    Role,
    SequenceKind,
    WorkspaceBroker,
    role_source,
    run_tests,
)
from argent_core import context  # noqa: E402
from argent_core.events import PRIVACY_DENYLIST  # noqa: E402
from argent_core.outputs import validate_role_output  # noqa: E402
from argent_core.supervisor import AGENT_IDS, extract_balanced_json  # noqa: E402
from argent_core.workspace_broker import CONTENT_DENYLIST  # noqa: E402

OWNER = OWNER_SOURCE
CONTROLLER = role_source(Role.LEAD)

FIXTURE_ROOT = PROJECT_ROOT / "e2e-fixture"

# role -> (openclaw agent id, thinking tier)
# The agent-id map is the single source of truth in argent_core.supervisor (A10).
THINKING = {
    Role.LEAD: "high",
    Role.ANALYST: "medium",
    Role.IMPLEMENTER: "medium",
    Role.QA: "medium",
    Role.REVIEWER: "high",
}

TASK1_SPEC = """Implement in `parser.py` the function `parse_duration(s: str) -> datetime.timedelta`:
- Format `XdYhZm` (days/hours/minutes); components optional, order fixed d->h->m,
  case-insensitive (D/H/M); whitespace between components allowed.
- Errors: empty/whitespace input -> ValueError('empty'); invalid format/unknown
  characters -> ValueError('invalid'); negative numbers -> ValueError('negative').
In `service.py`:
- `total_minutes(duration) -> int` (round up to whole minutes)
- `format_duration(duration) -> str` (compact `XdYhZm`, omit zero components, `0m` for zero)
In `tests/`: complete pytest tests covering at least: empty, whitespace, invalid,
negative, upper/lower case, order, partial components, 0d, overflow >999 days,
`1d2h3m` == `26h3m` == 1563m.
Acceptance: `pytest tests/ -q` green. Only fixture files may change."""

TASK2_SPEC = """Extend `parse_duration` with decimal components (`1.5h`, `0.5d`, `1.25m`) and
`service.format_duration` with decimal output where needed. Exact precision rule:
the sum of components must be exact (no float artifacts; compute in microseconds).
Additionally reject ISO-style `P1DT2H` in `parse_duration` (ValueError('invalid')).
Trap cases to handle correctly: `1.5h30m`, `0.5d12h` == exactly 24h.
Acceptance: `pytest tests/ -q` green. Only fixture files may change."""

ENVELOPE_COMMON = """- "role": "<your role>"
- "task_id": "<task id from this prompt>"
- "dispatch_id": "<dispatch id from this prompt>"
- "status": "ok" | "findings" | "blocked"
- "findings": [{"severity": "low|medium|high|critical", "description": "..."}] (empty list if none)
- "own_assessment": "<your honest assessment>"
- "concerns": [<strings>]
- "proposal": "<what should happen next>"
- "alternatives": [<strings>]
- "confidence": <number 0..1>
- "blockers": [<strings>] (empty if none)
- "requested_next_state": "<plausible task state; the core computes the real transition>"
"""

ROLE_SCHEMA = {
    Role.LEAD: """- "decision": "accept" | "rework" | "cancel" | "request_owner_gate"
- "accepted_findings": [<finding ids from the context, only ids that are open>]
- "rejected_findings": [<finding ids you reject, if any>]
- "rationale": "<why>"
(optional) - "rework_include_reviewer": true|false""",
    Role.ANALYST: """- "reproduction": "<how to reproduce>"
- "root_cause": "<root cause analysis>"
- "evidence_refs": [<strings>]""",
    Role.IMPLEMENTER: """- "changed_files": [<relative paths, e.g. "parser.py">]
- "implementation_summary": "<what you implemented>"
- "tests_run": [<names of tests you added/ran>]
EXTRA (only for implementer, stripped by the controller before validation):
- "patch_set": [{"op": "write", "path": "<relative path>", "content": "<the COMPLETE new file content as PLAIN UTF-8 TEXT>"}]
  You MUST deliver every file you change as a complete new version. Send the content
  as plain text (no base64, no encoding - the controller handles encoding).
  Paths are relative to the fixture root; "parser.py", "service.py" and "tests/test_parser.py"
  are valid. Never use absolute paths or "..".""",
    Role.QA: """- "tests": [{"name": "<test name>", "result": "passed|failed|error"}]
- "failures": [<strings>]
- "regressions": [<strings>]
- "coverage_concerns": [<strings>]
EXTRA (only for qa, stripped by the controller before validation):
- "test_patch_set": [{"op": "write", "path": "tests/<file>", "content": "<the COMPLETE new test file as PLAIN UTF-8 TEXT>"}]
  Send content as plain text (no base64 - the controller handles encoding).
  You may only write files under "tests/".""",
    Role.REVIEWER: """- "severity": "low|medium|high|critical"
- "security_findings": [{"severity": "...", "description": "..."}]
- "architecture_findings": [{"severity": "...", "description": "..."}]
- "recommendation": "approve|changes_requested|reject" """,
}

ROLE_INSTRUCTIONS = {
    Role.LEAD: (
        "You are the LEAD. You decide whether the work is accepted or needs "
        "rework. Base your decision on the findings, test runs and reviews in "
        "the context. In the standard workflow: accept only when the acceptance "
        "criteria are met and no critical/high or relevant medium findings are "
        "open. Decide 'rework' when changes are needed. Never invent finding ids."
    ),
    Role.ANALYST: (
        "You are the ANALYST. Analyse the task and the fixture. Provide a clear "
        "reproduction, root cause and evidence. You cannot modify files."
    ),
    Role.IMPLEMENTER: (
        "You are the IMPLEMENTER. You write code by delivering a patch_set. "
        "Read the fixture snapshot carefully: parser.py and service.py are "
        "stubs raising NotImplementedError; tests/ is empty. Deliver complete "
        "new file contents as plain text. Do not touch anything outside the fixture."
    ),
    Role.QA: (
        "You are the QA engineer. Review the implemented code (see fixture "
        "snapshot and changed files). Add/extend pytest tests under tests/ "
        "via test_patch_set (plain text content). Run nothing yourself; report the tests "
        "you expect to pass. Your tests must cover the required edge cases."
    ),
    Role.REVIEWER: (
        "You are the REVIEWER. Security- and architecture-review the changed "
        "code and the test results. Produce findings (severity, description) "
        "and a recommendation. You cannot modify files."
    ),
}


def _fresh_db(path: Path) -> Core:
    if path.exists():
        path.unlink()
    core = Core(str(path))
    return core


def _load_core(path: Path) -> Core:
    return Core(str(path))


def create_task1(core: Core, title: str = "E2E Task 1: duration parser (standard workflow)") -> tuple:
    project = core.create_project("phase2b-e2e", OWNER)
    task = core.create_task(project.id, title, OWNER, description=TASK1_SPEC)
    task_run = core.start_task_run(task.id, OWNER)
    return task, task_run


def create_task2(core: Core, title: str = "E2E Task 2: decimal durations (rework workflow)") -> tuple:
    project = core.create_project("phase2b-e2e", OWNER)
    task = core.create_task(project.id, title, OWNER, description=TASK2_SPEC)
    task_run = core.start_task_run(task.id, OWNER)
    return task, task_run


def _repo_summary() -> dict:
    changed = []
    for rel in sorted(p.relative_to(FIXTURE_ROOT) for p in FIXTURE_ROOT.rglob("*") if p.is_file()):
        changed.append(str(rel))
    return {
        "branch": "phase-2b-role-isolation-e2e",
        "status": "dirty",
        "changed_files": changed,
        "summary": f"e2e-fixture with {len(changed)} file(s); parser.py/service.py are stubs",
    }


def _fixture_snapshot() -> dict:
    return context.fixture_snapshot(FIXTURE_ROOT)


def _position_guidance(role: Role, position: int, cycle_no: int, kind: str) -> str:
    """Position-aware guidance so the lead's decision gate matches the workflow."""
    if role is Role.LEAD:
        if cycle_no == 1 and position == 0:
            return (
                "You are the FIRST decision gate of the standard workflow. The "
                "implementation does NOT exist yet — the fixture is intentionally "
                "a stub and tests/ is empty. That is expected at this stage. "
                "Decide 'accept' when the task specification is complete, "
                "unambiguous and feasible; decide 'rework' only when the SPEC "
                "itself is flawed. Never judge the (absent) implementation here."
            )
        if position == 0 and cycle_no > 1:
            return (
                "This is the START of a rework cycle. The findings cited by the "
                "previous review are EXPECTED to still be open at this gate - "
                "the implementer in the NEXT step fixes them. Decide 'accept' "
                "when the rework plan is clear and the cited findings are "
                "addressable; then the workflow proceeds to the implementer. "
                "Do NOT create new findings that merely restate that older "
                "findings are still open, and do NOT require fixes to already "
                "exist at this gate."
            )
        if kind == "STANDARD" and position >= 4:
            return (
                "This is a LATER decision point: the implementation and tests "
                "should now exist. Base your decision on the reported findings, "
                "test runs and reviews in the context. Accept only when the "
                "acceptance criteria are met and no critical/high or relevant "
                "medium findings are open."
            )
        if kind == "STANDARD" and cycle_no == 1 and position == 2:
            return (
                "Decision gate AFTER the analysis, BEFORE implementation. The "
                "implementation intentionally does NOT exist yet - the fixture "
                "is still a stub and tests/ is empty. That is expected here. "
                "Judge the ANALYSIS: is it sound, are the requirements and edge "
                "cases clear enough to implement? Decide 'accept' when the plan "
                "is clear (analyst findings of low/medium severity are ordinary "
                "spec clarifications that the implementer can satisfy as "
                "constraints). Decide 'rework' only when the analysis reveals a "
                "fundamental flaw that blocks implementation entirely."
            )
    if role is Role.ANALYST:
        return (
            "The fixture is intentionally a stub at this stage. Your job is to "
            "analyse the SPECIFICATION: identify the edge cases, risks and "
            "acceptance criteria that the implementation and tests must cover."
        )
    return ""


def _build_prompt(core: Core, task, role: Role, d, repo_summary: dict, fixture: dict) -> str:
    sections = core.build_agent_context(task.id, role, d.position, repo_summary, CONTROLLER)
    lines = []
    lines.append("You are an agent in a deterministic, isolated development team.")
    lines.append("You have NO tools. You cannot read files, run code or write anything.")
    lines.append("Everything you know about the task is in this prompt.")
    lines.append("")
    lines.append("=== TASK ===")
    lines.append(f"task_id: {task.id}")
    lines.append(f"dispatch_id: {d.id}")
    lines.append(f"title: {task.title}")
    lines.append(task.description or "")
    lines.append("")
    lines.append("=== CONTEXT ===")
    lines.append(json.dumps(sections, indent=2, sort_keys=True))
    if fixture["files"]:
        lines.append("")
        lines.append("=== FIXTURE SNAPSHOT (current file contents) ===")
        for rel, content in sorted(fixture["files"].items()):
            lines.append(f"--- {rel} ---")
            lines.append(content)
    if fixture["skipped"]:
        lines.append("")
        lines.append(f"(skipped files: {json.dumps(fixture['skipped'])})")
    lines.append("")
    lines.append("=== YOUR ROLE ===")
    lines.append(ROLE_INSTRUCTIONS[role])
    if role is Role.IMPLEMENTER:
        lines.append("")
        lines.append("=== CONTROLLER CLARIFICATION (binding, part of the task contract) ===")
        lines.append(
            "If the task demands EXACT precision (e.g. 'sum of components must be "
            "exact; compute in microseconds'): perform all arithmetic on exact "
            "rational values (fractions.Fraction, or integer scaling to a common "
            "denominator) and convert to whole microseconds exactly ONCE at the "
            "end. Truncating or rounding each component separately BEFORE summing "
            "is a violation of the contract, because fractional microseconds can "
            "carry across components (e.g. 0.0000000001d + 0.0000000001h totals "
            "9 microseconds exactly).\n"
            "Two additional binding policies for edge cases the spec leaves open:\n"
            "1) Sub-microsecond totals: timedelta has microsecond granularity. "
            "Round the exact total half-up to whole microseconds at the FINAL "
            "conversion only.\n"
            "2) format_duration must never render a nonzero duration as '0m' - "
            "always emit the exact decimal minute fraction (e.g. 6 microseconds "
            "-> '0.0000001m', 1 microsecond -> '0.000000016667m')."
        )
    pos_guidance = _position_guidance(role, d.position, d.cycle_no, d.sequence_kind.value)
    if pos_guidance:
        lines.append("")
        lines.append("=== POSITION-SPECIFIC GUIDANCE ===")
        lines.append(pos_guidance)
    lines.append("")
    lines.append("=== STRICT VOCABULARY RULE (privacy boundary, fail-closed) ===")
    lines.append("This rule has TWO tiers with different scope. Match the tier to where the text goes.")
    lines.append("")
    lines.append("TIER 1 - ENVELOPE: every field of your reply EXCEPT the patch file "
                 "content must NOT contain any of these substrings "
                 "(case-insensitive, also inside longer words):")
    lines.append("  " + ", ".join(sorted(PRIVACY_DENYLIST)))
    lines.append("This covers every envelope field, finding description, and every "
                 "string you return outside the patch files. The envelope "
                 "validator rejects the WHOLE result on the first hit.")
    lines.append("")
    lines.append("TIER 2 - PATCH FILE CONTENT: only the text INSIDE the "
                 "patch_set/test_patch_set 'content' fields must avoid these "
                 "high-signal terms:")
    lines.append("  " + ", ".join(sorted(CONTENT_DENYLIST)))
    lines.append("Ordinary words like code, encode, decode, token, content, subject, "
                 "body, diff are ALLOWED inside patch file content (they are only "
                 "forbidden in the envelope tier).")
    lines.append("")
    lines.append("Use safe synonyms where a term is forbidden in its tier. "
                 "Never quote this rule back in your reply.")
    lines.append("")
    lines.append("=== REQUIRED OUTPUT ===")
    lines.append("Reply with EXACTLY ONE JSON object and nothing else: no markdown, no code fences, no prose.")
    lines.append("Top-level fields (all mandatory unless marked optional):")
    lines.append(ENVELOPE_COMMON)
    lines.append(ROLE_SCHEMA[role])
    lines.append("")
    lines.append("Your whole reply must be a single valid JSON object. Do not wrap it in ```json fences.")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """Extract the first balanced JSON object from a text blob (lenient).

    Shared implementation (SPEC V2C §6.2.5): delegates to the single source of
    truth in ``argent_core.supervisor``.
    """
    return extract_balanced_json(text)


def _parse_inner_result(raw: str):
    """The CLI embeds the inner response as a string that is often Python-repr
    (single quotes) rather than strict JSON.  Try JSON, then literal_eval."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        import ast
        return ast.literal_eval(raw)
    except Exception:
        return None


def _run_agent(role: Role, dispatch_id: str, prompt: str) -> tuple[str, str]:
    """Run one real agent turn; returns (reply_text, run_id)."""
    agent_id = AGENT_IDS[role]
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(prompt)
        prompt_path = fh.name
    try:
        cmd = [
            "openclaw", "agent",
            "--agent", agent_id,
            "--session-id", f"dispatch-{dispatch_id}",
            "--message-file", prompt_path,
            "--json",
            "--timeout", "900",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=960)
        stdout = proc.stdout or ""
        if proc.returncode != 0:
            raise RuntimeError(
                f"openclaw agent failed rc={proc.returncode}: {proc.stderr[-2000:]}"
            )
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # Fallback: raw stdout is the reply.
            return stdout.strip(), _read_run_id(agent_id, dispatch_id)
        # Outer CLI envelope: {result: <inner json>, runId, status, summary}.
        # ``result`` is sometimes a nested dict, sometimes a (python-repr) string.
        inner = None
        result_field = data.get("result")
        if isinstance(result_field, dict):
            inner = result_field
        elif isinstance(result_field, str):
            inner = _parse_inner_result(result_field)
        if inner is not None and isinstance(inner, dict):
            payloads = inner.get("payloads") or []
            if payloads and isinstance(payloads[0], dict) and payloads[0].get("text"):
                run_id = data.get("runId") or _read_run_id(agent_id, dispatch_id)
                return payloads[0]["text"], run_id
            for key in ("text", "reply"):
                if isinstance(inner.get(key), str):
                    run_id = data.get("runId") or _read_run_id(agent_id, dispatch_id)
                    return inner[key], run_id
        if isinstance(data.get("text"), str):
            return data["text"], data.get("runId") or _read_run_id(agent_id, dispatch_id)
        if isinstance(data.get("reply"), str):
            return data["reply"], data.get("runId") or _read_run_id(agent_id, dispatch_id)
        return json.dumps(data), data.get("runId") or _read_run_id(agent_id, dispatch_id)
    finally:
        os.unlink(prompt_path)


def _read_run_id(agent_id: str, dispatch_id: str) -> str:
    traj = Path.home() / ".openclaw" / "agents" / agent_id / "sessions" / f"dispatch-{dispatch_id}.trajectory.jsonl"
    if not traj.exists():
        raise RuntimeError(f"trajectory file missing: {traj}")
    with open(traj, encoding="utf-8") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("type") == "session.started" and o.get("runId"):
                return o["runId"]
    raise RuntimeError(f"no session.started runId in {traj}")


def _is_consistent_b64(s: str) -> bool:
    """True when ``s`` is valid base64 that re-encodes to exactly itself."""
    try:
        decoded = base64.b64decode(s, validate=True).decode("utf-8")
        return base64.b64encode(decoded.encode("utf-8")).decode("ascii") == s
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False


def _normalize_content(raw: str) -> str:
    """Return a clean base64 payload for the broker.

    The agent contract is PLAIN UTF-8 text; legacy base64 (single or nested)
    is still recognised and canonicalised so the broker always scans/writes
    the true canonical content.  Behaviour:

    - Plain text (not valid base64) and whitespace-only/empty input: encoded
      from the ORIGINAL bytes - leading/trailing whitespace, blank lines and
      final newlines are preserved byte-for-byte.
    - Single-encoded base64: kept as-is (the broker decodes it exactly once).
    - Multi-encoded base64: fully unwrapped (depth cap 4); the next layer is
      recognised on the STRIPPED decoded form, so formatted inner layers
      (whitespace around nested base64) are unwrapped too instead of being
      written still-encoded.  If the cap is exhausted and a consistent
      encoded layer still remains, the input is REJECTED (fail-closed).

    Known limitation (documented, LOW): plain text that is itself a consistent
    base64 string (e.g. ``YWJj``) is interpreted as encoded input and decoded.
    The prompt contract (plain text, no manual encoding) makes this rare; it
    is an ambiguity, never a security boundary (the broker still scans the
    canonical bytes it writes).
    """
    if not isinstance(raw, str):
        raise ValueError("content must be a string")
    original = raw
    stripped = raw.strip()
    # Whitespace-only (or empty) input is plaintext: preserve the bytes
    # exactly instead of letting the empty stripped form count as base64.
    if not stripped:
        return base64.b64encode(original.encode("utf-8")).decode("ascii")
    if not _is_consistent_b64(stripped):
        # Plain-text path: encode the ORIGINAL bytes (whitespace-preserving).
        return base64.b64encode(original.encode("utf-8")).decode("ascii")
    candidate = stripped
    exhausted = False
    # Fully unwrap: while the current string is valid base64 AND decodes to
    # valid UTF-8 AND the decoded text differs, decode (depth cap 4).  The
    # next encoded layer is recognised on the STRIPPED decoded form so a
    # formatted inner layer (whitespace around nested base64) is unwrapped
    # instead of being written still-encoded.
    for _ in range(4):
        try:
            decoded = base64.b64decode(candidate, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            break
        if decoded == candidate:
            break
        if _is_consistent_b64(decoded.strip()) and decoded.strip() != decoded:
            candidate = decoded.strip()
        else:
            candidate = decoded
    else:
        exhausted = True
    cand = candidate.strip()
    if _is_consistent_b64(cand):
        if exhausted:
            # The cap was fully consumed and the remaining text is STILL a
            # consistent base64 layer (possibly whitespace-padded).  The
            # broker would decode one more layer and write still-encoded
            # bytes (canonical content never scanned).
            inner = base64.b64decode(cand, validate=True).decode("utf-8")
            if _is_consistent_b64(inner.strip()):
                raise ValueError("nested encoding exceeds depth cap")
        return cand
    # Multi-encoded input fully unwrapped to plain text: encode the canonical
    # text (the broker writes exactly this).
    return base64.b64encode(candidate.encode("utf-8")).decode("ascii")


def _file_digest(path: Path) -> str:
    """SHA-256 of a file's bytes (empty string if missing/unreadable)."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _apply_patches(role: Role, agent_json: dict) -> dict:
    """Apply implementer/qa patches via the write broker. Returns broker result.

    Also detects no-op writes: a write whose target file ends up byte-identical
    to its pre-apply content.
    """
    broker = WorkspaceBroker()
    if role is Role.IMPLEMENTER:
        patch_set = agent_json.get("patch_set") or []
        field = "patch_set"
    elif role is Role.QA:
        patch_set = agent_json.get("test_patch_set") or []
        field = "test_patch_set"
    else:
        return {"field": None, "result": None, "noops": []}
    if not patch_set:
        return {"field": field, "result": None, "noops": []}
    write_paths = []
    for p in patch_set:
        if p.get("op") == "write" and isinstance(p.get("path"), str):
            raw = p["path"]
            if os.path.isabs(raw):
                continue  # the broker rejects absolute paths anyway
            candidate = (FIXTURE_ROOT / raw).resolve()
            if candidate.is_relative_to(FIXTURE_ROOT):
                write_paths.append(candidate)
    before = {p: _file_digest(p) for p in write_paths}
    for p in patch_set:
        if isinstance(p.get("content"), str):
            p["content"] = _normalize_content(p["content"])
    res = broker.apply_patch_set(FIXTURE_ROOT, patch_set, role, CONTROLLER)
    noops = [
        str(p.relative_to(FIXTURE_ROOT))
        for p in write_paths
        if _file_digest(p) == before[p]
    ]
    return {"field": field, "result": res, "noops": noops}


def _snapshot_fixture() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="fixture-bak-"))
    for p in FIXTURE_ROOT.rglob("*"):
        if p.is_file():
            rel = p.relative_to(FIXTURE_ROOT)
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
    return tmp


def _restore_fixture(bak: Path) -> None:
    for p in FIXTURE_ROOT.rglob("*"):
        if p.is_file():
            p.unlink()
    for p in bak.rglob("*"):
        if p.is_file():
            rel = p.relative_to(bak)
            dest = FIXTURE_ROOT / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
    shutil.rmtree(bak, ignore_errors=True)


def cmd_init(args) -> int:
    db = Path(args.db)
    core = _fresh_db(db)
    task, task_run = create_task1(core)
    print(f"DB: {db}")
    print(f"project/task/task_run created: {task.id} / {task_run.id}")
    print(f"task state: {task.state.value}")
    core.close()
    return 0


def cmd_init_rework(args) -> int:
    db = Path(args.db)
    core = _load_core(db)
    task, task_run = create_task2(core)
    print(f"Task 2 (rework) created: {task.id} / {task_run.id}")
    core.close()
    return 0


def cmd_status(args) -> int:
    core = _load_core(Path(args.db))
    for t in core.queries.list_tasks():
        print(f"task {t.id[:8]} state={t.state.value} title={t.title!r}")
        for d in core.queries.list_dispatches():
            if d.task_id == t.id:
                print(f"  dispatch {d.id[:8]} role={d.role.value} status={d.status.value} "
                      f"pos={d.position} cycle={d.cycle_no} model={d.actual_model or d.expected_model_class}")
    active = core.queries.get_active_role_run(core.queries.list_tasks()[0].id) if core.queries.list_tasks() else None
    if active:
        print(f"active role run: {active.role.value}")
    core.close()
    return 0


def cmd_next(args) -> int:
    core = _load_core(Path(args.db))
    tasks = core.queries.list_tasks()
    if not tasks:
        print("no tasks")
        return 1
    for t in tasks:
        frontier = core.expected_next_role(t.id, CONTROLLER)
        print(f"task {t.id[:8]} state={t.state.value} expected_next={frontier}")
    core.close()
    return 0


def cmd_run(args) -> int:
    db = Path(args.db)
    core = _load_core(db)
    tasks = [t for t in core.queries.list_tasks() if t.state.value not in ("DONE", "CANCELLED")]
    if not tasks:
        print("no actionable task")
        return 1
    task = tasks[0]
    frontier = core._workflow_frontier(task.id)
    role = frontier.expected_role
    if role is None:
        print(f"task {task.id[:8]} has no next role (workflow exhausted)")
        return 1
    cycle_no, position, kind = frontier.cycle_no, frontier.position, frontier.sequence_kind
    task_run = core.queries.get_latest_task_run(task.id)
    print(f"STEP: task={task.id[:8]} role={role.value} cycle={cycle_no} pos={position} kind={kind.value}")

    # 1. start role run (if not already active for this role)
    active = core.queries.get_active_role_run(task.id)
    if active is None or active.role is not role:
        core.start_role(task.id, role, CONTROLLER)
        print(f"started role run: {role.value}")

    # 2. create dispatch
    d = core.create_dispatch(task.id, task_run.id, role, position, cycle_no, kind, None, CONTROLLER)
    print(f"dispatch {d.id} created (expected {d.expected_agent_class}/{d.expected_model_class}/{d.expected_thinking_tier})")

    # 3. context snapshot (persisted, metadata-only)
    repo_summary = _repo_summary()
    snap = core.snapshot_agent_context(d.id, role, position, repo_summary, CONTROLLER)
    print(f"context snapshot {snap.context_hash[:12]} persisted")

    # 4. prompt + real agent run
    fixture = _fixture_snapshot()
    prompt = _build_prompt(core, task, role, d, repo_summary, fixture)
    reply, run_id = _run_agent(role, d.id, prompt)
    print(f"agent replied ({len(reply)} chars), run_id={run_id[:8]}")

    # 5. parse structured output
    try:
        agent_json = _extract_json(reply)
    except Exception as exc:
        print(f"FAILED to parse agent output: {exc}")
        print("--- reply head ---")
        print(reply[:1500])
        print("dispatch left PENDING; re-run 'run' to retry (a fresh dispatch is created)")
        core.close()
        return 2
    print(f"parsed JSON with keys: {sorted(agent_json.keys())}")

    # 6. bind
    session_key = f"agent:{AGENT_IDS[role]}:explicit:dispatch-{d.id}"
    thinking = THINKING[role]
    d = core.bind_spawn_result(
        d.id, session_key, run_id,
        d.expected_agent_class, d.expected_model_class, thinking, CONTROLLER,
    )
    print("dispatch bound -> RUNNING")

    # 7. fixture backup for write roles
    bak = _snapshot_fixture() if role in (Role.IMPLEMENTER, Role.QA) else None
    patch_info = {"field": None, "result": None}
    test_run_id = None
    try:
        # 8. broker + sandbox + test recording (before receive; role still active)
        if role in (Role.IMPLEMENTER, Role.QA):
            patch_info = _apply_patches(role, agent_json)
            res = patch_info["result"]
            if res is not None:
                if res.errors:
                    print(f"BROKER ERRORS: {json.dumps(res.errors, indent=2)}")
                    _restore_fixture(bak)
                    core.mark_agent_failed(d.id, "broker_denied", CONTROLLER)
                    core.close()
                    return 3
                print(f"broker applied {len(res.applied)} file(s), skipped {len(res.skipped)}")
                noops = patch_info.get("noops") or []
                if noops:
                    print(f"no-op: byte-identical writes -> {', '.join(noops)}")
                    if role is Role.IMPLEMENTER and kind is SequenceKind.REWORK:
                        print(f"NOTE (rework implementer): no byte change in "
                              f"{', '.join(noops)}; verify the remediation actually "
                              "alters the files")
            # bwrap sandbox run
            sandbox = run_tests(FIXTURE_ROOT, pytest_args=["/workspace/tests", "-q"])
            print(f"bwrap tests: exit={sandbox.exit_code} wall={sandbox.wall_seconds:.1f}s "
                  f"timed_out={sandbox.timed_out}")
            print(sandbox.stdout_bounded[-800:] if sandbox.stdout_bounded else "(no stdout)")
            result = "passed" if (sandbox.exit_code == 0 and not sandbox.timed_out) else "failed"
            tr = core.record_test_run(task.id, result, role_source(role),
                                      detail=f"bwrap e2e run after {role.value}")
            test_run_id = tr.id
            print(f"test run recorded: {tr.id[:8]} result={result}")

        # 9. envelope (strip controller-extras) + validate
        envelope = {k: v for k, v in agent_json.items() if k not in ("patch_set", "test_patch_set", "encoded")}
        try:
            validate_role_output(role, envelope)
        except Exception as exc:
            print(f"OUTPUT INVALID: {exc}")
            if bak is not None:
                _restore_fixture(bak)
            core.mark_agent_failed(d.id, "malformed_output", CONTROLLER)
            core.close()
            return 4

        # 10. receive (consume + effects + handoff)
        em = {
            "task_id": task.id,
            "child_session_id": session_key,
            "run_id": run_id,
            "parent_dispatch_id": None,
            "event_type": "agent.completed",
            "status": "completed",
        }
        res = core.receive_agent_result(d.id, em, envelope, CONTROLLER)
        print(f"receive -> {res.status}" + (f" ({res.reason})" if getattr(res, "reason", None) else ""))
        if res.status != "consumed":
            if bak is not None:
                _restore_fixture(bak)
            core.close()
            return 5
        if bak is not None:
            shutil.rmtree(bak, ignore_errors=True)

        # 11. summary
        t = core.queries.get_task(task.id)
        nxt = core.expected_next_role(task.id, CONTROLLER)
        print(f"task state now: {t.state.value}; next role: {nxt}")
        return 0
    finally:
        core.close()


def cmd_unexpected_smoke(args) -> int:
    """Duplicate + foreign completion events for an already-consumed dispatch."""
    db = Path(args.db)
    core = _load_core(db)
    tasks = core.queries.list_tasks()
    consumed = None
    for t in tasks:
        for d in core.queries.list_dispatches():
            if d.task_id == t.id and d.status.value == "CONSUMED":
                consumed = (t, d)
                break
        if consumed:
            break
    if not consumed:
        print("no consumed dispatch found; run the workflow first")
        core.close()
        return 1
    t, d = consumed
    em = {
        "task_id": t.id,
        "child_session_id": d.child_session_id,
        "run_id": d.openclaw_run_id,
        "parent_dispatch_id": None,
        "event_type": "agent.completed",
        "status": "completed",
    }
    dup = core.receive_agent_result(d.id, em, {}, CONTROLLER)
    print(f"DUPLICATE (same run): {dup.status} (expected 'duplicate')")
    em2 = dict(em, run_id="11111111-2222-3333-4444-555555555555")
    foreign = core.receive_agent_result(d.id, em2, {}, CONTROLLER)
    print(f"FOREIGN (wrong run): {foreign.status} reason={foreign.reason} (expected rejected+quarantine)")
    q = core.quarantine_log(CONTROLLER, t.id)
    print(f"quarantine entries: {len(q)}")
    for e in q[-3:]:
        print(f"  {e.reason} dispatch={e.dispatch_id[:8]}")
    # state unchanged?
    t2 = core.queries.get_task(t.id)
    print(f"task state unchanged: {t.state.value} == {t2.state.value}")
    core.close()
    return 0


def _mock_output(role: Role, task_id: str, dispatch_id: str) -> dict:
    """Minimal valid envelope for offline pipeline steps in the recovery smoke."""
    base = {
        "role": role.value,
        "task_id": task_id,
        "dispatch_id": dispatch_id,
        "status": "ok",
        "findings": [],
        "own_assessment": "pipeline setup",
        "concerns": [],
        "proposal": "proceed",
        "alternatives": [],
        "confidence": 0.9,
        "blockers": [],
        "requested_next_state": "PLANNING",
    }
    if role is Role.LEAD:
        base.update({
            "decision": "accept",
            "accepted_findings": [],
            "rejected_findings": [],
            "rationale": "setup",
        })
    elif role is Role.ANALYST:
        base.update({
            "reproduction": "n/a", "root_cause": "n/a", "evidence_refs": [],
        })
    return base


def _mock_receive(core, task, role, cycle_no, position):
    """Run one offline role step (start + dispatch + bind + consume)."""
    task_run = core.queries.get_latest_task_run(task.id)
    core.start_role(task.id, role, CONTROLLER)
    d = core.create_dispatch(
        task.id, task_run.id, role, position, cycle_no,
        SequenceKind.STANDARD, None, CONTROLLER,
    )
    session = f"agent:argent-{role.value}:explicit:fake-{uuid.uuid4().hex[:8]}"
    run = str(uuid.uuid4())
    thinking = THINKING[role]
    d = core.bind_spawn_result(
        d.id, session, run,
        d.expected_agent_class, d.expected_model_class, thinking, CONTROLLER,
    )
    em = {
        "task_id": task.id,
        "child_session_id": session,
        "run_id": run,
        "parent_dispatch_id": None,
        "event_type": "agent.completed",
        "status": "completed",
    }
    res = core.receive_agent_result(d.id, em, _mock_output(role, task.id, d.id), CONTROLLER)
    assert res.status == "consumed", res
    return d


def cmd_recovery_smoke(args) -> int:
    """Recovery smoke on a scratch DB (SPEC V2B §7 / V2 15.2 ghost-writer rule)."""
    import tempfile as _tf
    scratch = Path(_tf.mkdtemp(prefix="phase2b-recovery-")) / "recovery.db"
    core = _fresh_db(scratch)
    # Task A: pipeline up to implementer; implementer dispatch bound -> RUNNING.
    task_a, _ = create_task1(core, title="recovery-smoke task A")
    _mock_receive(core, task_a, Role.LEAD, 1, 0)
    _mock_receive(core, task_a, Role.ANALYST, 1, 1)
    _mock_receive(core, task_a, Role.LEAD, 1, 2)
    run_a = core.queries.get_latest_task_run(task_a.id)
    core.start_role(task_a.id, Role.IMPLEMENTER, CONTROLLER)
    d_impl = core.create_dispatch(
        task_a.id, run_a.id, Role.IMPLEMENTER, 3, 1,
        SequenceKind.STANDARD, None, CONTROLLER,
    )
    session_impl = f"agent:argent-implementer:explicit:fake-{uuid.uuid4().hex[:8]}"
    run_impl = str(uuid.uuid4())
    core.bind_spawn_result(
        d_impl.id, session_impl, run_impl,
        d_impl.expected_agent_class, d_impl.expected_model_class,
        THINKING[Role.IMPLEMENTER], CONTROLLER,
    )
    # Task B: lead dispatch created but NEVER spawned -> PENDING.
    task_b, _ = create_task2(core, title="recovery-smoke task B")
    run_b = core.queries.get_latest_task_run(task_b.id)
    core.start_role(task_b.id, Role.LEAD, CONTROLLER)
    d_pend = core.create_dispatch(
        task_b.id, run_b.id, Role.LEAD, 0, 1,
        SequenceKind.STANDARD, None, CONTROLLER,
    )
    print(f"scratch: implementer dispatch {d_impl.id[:8]} RUNNING (task A), "
          f"lead dispatch {d_pend.id[:8]} PENDING (task B)")
    rep = core.recover(OWNER)
    print(f"recovery report: role_runs_failed={rep.interrupted_role_runs} "
          f"rolled_back={rep.rolled_back} recovery_pending={rep.recovery_pending_dispatches}")
    for dd in core.queries.list_dispatches():
        print(f"  dispatch {dd.id[:8]} {dd.role.value} -> {dd.status.value}")
    ta = core.queries.get_task(task_a.id)
    print(f"task A state: {ta.state.value} (expected RECOVERING)")
    status_impl = core.queries.get_dispatch(d_impl.id).status.value
    status_pend = core.queries.get_dispatch(d_pend.id).status.value
    assert status_impl == "RECOVERY_PENDING", status_impl
    assert status_pend == "FAILED", status_pend
    assert ta.state.value == "RECOVERING", ta.state.value
    print("recovery smoke OK: ghost-writer rule + read-only fail-closed verified")
    core.close()
    shutil.rmtree(scratch.parent, ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 2B E2E driver")
    ap.add_argument("command", choices=[
        "init", "init-rework", "status", "next", "run", "unexpected-smoke", "recovery-smoke",
    ])
    ap.add_argument("--db", default=str(PROJECT_ROOT / "smoke" / "phase2b.db"))
    args = ap.parse_args()
    return {
        "init": cmd_init,
        "init-rework": cmd_init_rework,
        "status": cmd_status,
        "next": cmd_next,
        "run": cmd_run,
        "unexpected-smoke": cmd_unexpected_smoke,
        "recovery-smoke": cmd_recovery_smoke,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

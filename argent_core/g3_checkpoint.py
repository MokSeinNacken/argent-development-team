"""Phase G3-A — pre-reboot checkpoint validation (pure, deterministic).

Pure functions for validating and reasoning about the supervisor's pre-reboot
checkpoint (written to ``~/.local/state/argent/g3/g3-pre-reboot-checkpoint.json``
immediately before a WSL restart).  This module NEVER touches the live DB and
NEVER reads a secret VALUE; it only validates structure and performs bounded
extraction/comparison.  Secret VALUES must never appear in a checkpoint — only
PATHS to key files (plain strings) are permitted, which is why any key whose
lowercased name denotes a secret is rejected fail-closed.

All functions are pure: no filesystem/network/DB access, no raising.
"""

from __future__ import annotations

from typing import Any

CHECKPOINT_VERSION = "1.0"

#: Secret-bearing key names (lowercased) that MUST NEVER appear at ANY depth —
#: the checkpoint must not carry secret VALUES (paths to key files are allowed
#: because they are plain paths).
_SECRET_KEY_NAMES = frozenset({
    "api_key", "secret", "token", "password", "hmac_key", "key_value",
    "private_key",
})

#: Bounded top-level allowlist (fail-closed against unknown keys).  The real
#: supervisor-written checkpoint has EXACTLY these keys.
_TOP_LEVEL_KEYS = frozenset({
    "checkpoint_version", "phase", "marker_g2", "timestamp", "boot_id",
    "host_id", "supervisor", "service", "g2", "g3_worktree", "db",
    "job_state_counts", "external_wait_count", "notification_outbox",
    "supervisor_actions", "process_registry_rows",
    "active_supervisor_instance_rows", "journal_secret_scan_hits",
    "persistent_paths", "expected_post_reboot_invariants",
})

_MISSING = object()


def _nested_get(d: Any, parts) -> Any:
    """Fetch a nested value by tuple-of-keys; ``_MISSING`` if absent/not a dict."""
    cur = d
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _secret_key_errors(obj: Any) -> list:
    """Return an error for every secret-named key found at ANY depth."""
    errors: list = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.lower() in _SECRET_KEY_NAMES:
                    loc = f"{path}.{k}" if path else k
                    errors.append(f"secret-bearing key {loc!r} must not appear")
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(obj, "")
    return errors


def _check_supervisor(sup: Any) -> list:
    if not isinstance(sup, dict):
        return ["supervisor must be a dict"]
    errors: list = []
    if not isinstance(sup.get("instance_id"), str):
        errors.append("supervisor.instance_id must be a str")
    pid = sup.get("pid")
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
        errors.append("supervisor.pid must be a positive int")
    ticks = sup.get("process_start_ticks")
    if not isinstance(ticks, int) or isinstance(ticks, bool):
        errors.append("supervisor.process_start_ticks must be an int")
    if not isinstance(sup.get("status"), str):
        errors.append("supervisor.status must be a str")
    rev = sup.get("revision")
    if not isinstance(rev, int) or isinstance(rev, bool):
        errors.append("supervisor.revision must be an int")
    return errors


def _check_service(svc: Any) -> list:
    if not isinstance(svc, dict):
        return ["service must be a dict"]
    errors: list = []
    for name in ("enabled", "active", "health"):
        if not isinstance(svc.get(name), str):
            errors.append(f"service.{name} must be a str")
    unit_sha = svc.get("unit_sha256")
    if not (
        isinstance(unit_sha, str)
        and len(unit_sha) == 64
        and all(c in "0123456789abcdefABCDEF" for c in unit_sha)
    ):
        errors.append("service.unit_sha256 must be a 64-char hex string")
    return errors


def _check_g2(g2: Any) -> list:
    if not isinstance(g2, dict):
        return ["g2 must be a dict"]
    sha = g2.get("commit_sha")
    if not (isinstance(sha, str) and sha.strip()):
        return ["g2.commit_sha must be a non-empty str"]
    return []


def _check_db(db: Any) -> list:
    if not isinstance(db, dict):
        return ["db must be a dict"]
    errors: list = []
    for name in ("schema_version", "path"):
        if not isinstance(db.get(name), str):
            errors.append(f"db.{name} must be a str")
    return errors


def _check_job_state_counts(jsc: Any) -> list:
    if not isinstance(jsc, dict):
        return ["job_state_counts must be a dict"]
    errors: list = []
    for k, v in jsc.items():
        if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
            errors.append(f"job_state_counts[{k!r}] must be a non-negative int")
    return errors


def _check_persistent_paths(pp: Any) -> list:
    if not isinstance(pp, dict):
        return ["persistent_paths must be a dict"]
    errors: list = []
    for k, v in pp.items():
        if not isinstance(v, str):
            errors.append(f"persistent_paths[{k!r}] must be a str")
    return errors


def _check_invariants(inv: Any) -> list:
    if not isinstance(inv, list) or len(inv) == 0:
        return ["expected_post_reboot_invariants must be a non-empty list"]
    errors: list = []
    for i, v in enumerate(inv):
        if not (isinstance(v, str) and v.strip()):
            errors.append(
                f"expected_post_reboot_invariants[{i}] must be a non-empty string"
            )
    return errors


def validate_checkpoint(obj: Any) -> list:
    """Strictly validate a pre-reboot checkpoint dict.

    Returns a list of human-readable error strings; an EMPTY list means VALID.
    Never raises: malformed input (non-dict, missing/invalid fields, unknown
    top-level keys, secret-named keys) yields errors (fail-closed).
    """
    if not isinstance(obj, dict):
        return ["checkpoint must be a dict"]

    errors: list = []

    # Unknown top-level keys (fail-closed) and secret-named keys at any depth.
    for key in obj:
        if key not in _TOP_LEVEL_KEYS:
            errors.append(f"unknown top-level key: {key!r}")
    errors.extend(_secret_key_errors(obj))

    if obj.get("checkpoint_version") != CHECKPOINT_VERSION:
        errors.append(
            f"checkpoint_version must be {CHECKPOINT_VERSION!r}, "
            f"got {obj.get('checkpoint_version')!r}"
        )
    if obj.get("phase") != "G3-A":
        errors.append(f"phase must be 'G3-A', got {obj.get('phase')!r}")

    ts = obj.get("timestamp")
    if not (isinstance(ts, str) and ts.strip()):
        errors.append("timestamp must be a non-empty string")

    for name in ("boot_id", "host_id"):
        v = obj.get(name)
        if not (isinstance(v, str) and v.strip()):
            errors.append(f"{name} must be a non-empty string")

    errors.extend(_check_supervisor(obj.get("supervisor")))
    errors.extend(_check_service(obj.get("service")))
    errors.extend(_check_g2(obj.get("g2")))
    errors.extend(_check_db(obj.get("db")))
    errors.extend(_check_job_state_counts(obj.get("job_state_counts")))

    if not isinstance(obj.get("notification_outbox"), dict):
        errors.append("notification_outbox must be a dict")

    errors.extend(_check_persistent_paths(obj.get("persistent_paths")))
    errors.extend(_check_invariants(obj.get("expected_post_reboot_invariants")))

    return errors


def boot_identity(checkpoint: Any) -> dict:
    """Bounded extraction of the boot/instance identity from a checkpoint."""
    sup = checkpoint.get("supervisor") if isinstance(checkpoint, dict) else None
    sup = sup if isinstance(sup, dict) else {}
    return {
        "boot_id": checkpoint.get("boot_id") if isinstance(checkpoint, dict) else None,
        "host_id": checkpoint.get("host_id") if isinstance(checkpoint, dict) else None,
        "instance_id": sup.get("instance_id"),
        "pid": sup.get("pid"),
        "process_start_ticks": sup.get("process_start_ticks"),
    }


def is_old_authority_still_live(prev: dict, live_instance_row: dict) -> bool:
    """G1 fencing predicate: is the OLD authority still live?

    The old authority is live ONLY IF ``live`` boot_id == prev boot_id AND
    ``live`` host_id == prev host_id AND ``live`` instance_id == prev
    instance_id.  A changed boot_id means the old (boot_id, pid,
    process_start_ticks, instance_id) tuple is NOT live authority (fail-closed).
    """
    if not isinstance(prev, dict) or not isinstance(live_instance_row, dict):
        return False
    prev_sup = prev.get("supervisor") if isinstance(prev.get("supervisor"), dict) else {}
    prev_boot = prev.get("boot_id")
    if prev_boot is None:
        return False
    return (
        live_instance_row.get("boot_id") == prev_boot
        and live_instance_row.get("host_id") == prev.get("host_id")
        and live_instance_row.get("instance_id") == prev_sup.get("instance_id")
    )


def single_active_supervisor(instance_rows: Any) -> tuple:
    """``(bool, reason)``: True iff EXACTLY one row has ``status == "ACTIVE"``."""
    if not isinstance(instance_rows, list):
        return False, "instance_rows must be a list"
    active = [
        r for r in instance_rows
        if isinstance(r, dict) and r.get("status") == "ACTIVE"
    ]
    if len(active) == 1:
        return True, "exactly one ACTIVE supervisor"
    if len(active) == 0:
        return False, "no ACTIVE supervisor"
    return False, f"{len(active)} ACTIVE supervisors (expected exactly one)"


def compare_persisted_state(prev: dict, live: dict) -> list:
    """Deltas between ``prev`` checkpoint and a ``live`` state dict.

    Only the bounded keys are compared: ``db.path``, ``db.schema_version``,
    ``g2.commit_sha``, ``host_id``, ``service.enabled``, ``service.active``.
    Returns only differences; an empty list == identical for these keys.
    """
    deltas: list = []
    specs = (
        ("db.path", ("db", "path")),
        ("db.schema_version", ("db", "schema_version")),
        ("g2.commit_sha", ("g2", "commit_sha")),
        ("host_id", ("host_id",)),
        ("service.enabled", ("service", "enabled")),
        ("service.active", ("service", "active")),
    )
    for label, parts in specs:
        a = _nested_get(prev, parts)
        b = _nested_get(live, parts)
        if a != b:
            deltas.append(f"{label}: {a!r} -> {b!r}")
    return deltas


def post_reboot_validation_plan(prev: dict) -> list:
    """The ``expected_post_reboot_invariants`` list verbatim (bounded passthrough)."""
    if not isinstance(prev, dict):
        return []
    inv = prev.get("expected_post_reboot_invariants")
    return list(inv) if isinstance(inv, list) else []

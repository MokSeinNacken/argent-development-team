"""Phase 3D — Read-only Supervisor Snapshot Publisher (Argent-Trust-Domain).

Erzeugt einen strikt allowlist-basierten, versionierten JSON-Snapshot des
Argent-Supervisor-Zustands fuer den System Visualizer.

INVARIANTEN (docs/PHASE3D_SPEC.md):
- NUR Read-Zugriff auf die Argent-SQLite: ``mode=ro`` + ``query_only=ON``,
  kurze deferred Read-Transaktion, explizite Spaltenlisten, niemals
  ``SELECT *``, kein DDL/ATTACH/BEGIN IMMEDIATE/EXCLUSIVE.
- Harte Zeitgrenze (Default 100 ms) via ``set_progress_handler``: bei
  Ueberschreitung ROLLBACK + Skip; nie den Supervisor blockieren.
- Rohzeilen werden NUR innerhalb der kurzen Read-Transaktion materialisiert;
  Sanitization/Aggregation/JSON/Datei-I/O erst NACH dem COMMIT.
- Bei ``SQLITE_BUSY``/Timeout wird die Publikation uebersprungen (Skip).
- Kein Schreibpfad zur produktiven Ledger-DB. Einziger Datei-Write ist der
  atomare Snapshot (Tempfile + ``os.replace``) im Snapshot-Verzeichnis.
- Niemals im Snapshot: binding_hash, token_hash, claim_token, result_json,
  context_summary_json, payload_json, event_meta_json, patch_set_json,
  Challenge-Tokens, Telegram-IDs, Credentials, Agent-Rohantworten.
- Freie TEXT-Werte (z. B. ``scope``, ``gate_scope``) werden wertbasiert
  gescannt: Werte mit Verbotsmustern werden redigiert („[redacted]“).
- Alle IDs werden auf 8-12 Zeichen gekuerzt.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SNAPSHOT_VERSION = 1
SNAPSHOT_SCHEMA_NAME = "argent-supervisor-snapshot"

# Verbotsfelder: diese Spaltennamen duerfen NIE in den Snapshot (Canary).
FORBIDDEN_COLUMNS = frozenset(
    {
        "binding_hash",
        "token_hash",
        "claim_token",
        "result_json",
        "context_summary_json",
        "payload_json",
        "event_meta_json",
        "patch_set_json",
        "authorization",
        "password",
        "api_key",
        "cookie",
    }
)

# Wertbasierte Verbotsmuster fuer freie TEXT-Spalten (scope, gate_scope, ...):
# enthaelt ein Wert ein solches Muster, wird er redigiert (Defense-in-Depth,
# selbst wenn der Spaltenname selbst erlaubt ist).
# KANONISCH: muss mit der Reader-Konstante SECRET_PATTERN_RE identisch sein
# (tests vergleichen beide Dateien auf die Marker).
_FORBIDDEN_VALUE_RE = re.compile(
    r"(binding_hash|token_hash|claim_token|result_json|context_summary_json|"
    r"payload_json|event_meta_json|patch_set_json|api[_-]?key|secret|password|"
    r"credential|authorization|bearer[\s:]|chat[_-]?id|gho_|ghp_|github_pat_|"
    r"github_[a-z0-9_]*token|xox[baprs]-|(?<![a-z0-9])sk-[a-z0-9_-]{6,}|-----BEGIN|"
    r"key[=:]\s*[A-Za-z0-9_\-]{8,}|/home/|/Users/|/tmp/|/var/|\.ssh/|\.aws/)",
    re.IGNORECASE,
)
# Kontrollzeichen, die aus Freitext-Werten IMMER entfernt werden (vor dem
# Secret-Scan — sonst koennte das Entfernen verschleierte Patterns
# zusammensetzen, z. B. binding_\x00hash -> binding_hash). Vertrag: ALLE
# C0/C1 inkl. CR/LF/TAB werden entfernt; der Reader akzeptiert keines davon.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
REDACTED = "[redacted]"
DISPLAY_VALUE_MAX_LEN = 200  # Gesamtlaenge inkl. Ellipse <= 200

# IDs, die auf 8-12 Zeichen gekuerzt werden (nur erste 12, mind. 8).
TRUNCATED_ID_FIELDS = frozenset(
    {
        "task_id",
        "approval_id",
        "dispatch_id",
        "handoff_id",
        "session_id",
        "run_id",
        "child_session_id",
        "openclaw_run_id",
        "event_ref",
        "supervisor_job_id",
        "gate_id",
        "id",
    }
)

# Zeitlimits (Sekunden).
MAX_QUERY_SECONDS = 0.10  # harte Deadline pro Publikation (Ziel < 0.05)
PROGRESS_STEPS = 500  # set_progress_handler Schrittweite (VM-Instruktionen)

# Werte-Validierung fuer git_head / schema_version (Publisher-seitig).
GIT_HEAD_RE = __import__("re").compile(r"^[0-9a-f]{7,40}$")
SCHEMA_VERSION_RE = __import__("re").compile(r"^[0-9]+(?:\.[0-9]+)*$")


def sanitize_git_head(value: Optional[str]) -> Optional[str]:
    """Nur Git-Hash (7-40 hex) oder None; alles andere -> None (fail-closed)."""
    if value is None:
        return None
    text = str(value).strip()
    if GIT_HEAD_RE.match(text):
        return text
    return None


def sanitize_schema_version(value: Optional[str]) -> Optional[str]:
    """Nur enges numerisches Format (z. B. "6", "1.0"); sonst None.

    Fail-closed: Werte > 32 Zeichen werden abgelehnt (kein Kuerzen nach
    erfolgreicher Validierung — ein abgeschnittenes Schema-Version-Feld
    koennte mit einem Punkt enden und den Reader-Check verletzen).
    """
    if value is None:
        return None
    text = str(value).strip()
    if SCHEMA_VERSION_RE.match(text) and len(text) <= 32:
        return text
    return None

# Statischer Phasen-/Projektstatus (aus Argent-Historie; Live-Daten ergaenzen).
PHASE_STATUS = [
    {"phase": "2C", "name": "Persistent Supervisor", "status": "GREEN"},
    {"phase": "3A", "name": "Telegram Notifications", "status": "GREEN"},
    {"phase": "3B", "name": "Live Outbound", "status": "GREEN"},
    {"phase": "3C-A", "name": "Approval Core", "status": "GREEN"},
    {"phase": "3C-B1", "name": "Telegram Approval Adapter", "status": "GREEN"},
    {"phase": "3C-B2A", "name": "Ingress Metadata Bridge", "status": "GREEN"},
    {"phase": "3C-B2B", "name": "Live Bridge", "status": "WAITING_UPSTREAM"},
    {"phase": "3D", "name": "Supervisor Visualizer", "status": "ACTIVE"},
]

# Workflow-Pipeline (Rollen in Reihenfolge).
WORKFLOW_ROLES = ["lead", "analyst", "implementer", "qa", "reviewer"]


class SnapshotDeadlineExceeded(sqlite3.OperationalError):
    """Harte Zeitgrenze ueberschritten — Publikation muss abgebrochen werden."""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def truncate_id(value: Optional[str], length: int = 12) -> Optional[str]:
    """Kuerzt IDs auf maximal ``length`` Zeichen (SPEC: 8-12)."""
    if value is None:
        return None
    text = str(value)
    if len(text) <= length:
        return text
    return text[:length]


def sanitize_display_value(value: Any) -> Any:
    """Wertbasierte Sanitierung freier TEXT-Werte (Defense-in-Depth).

    REIHENFOLGE ist kritisch (R5-HIGH):
    1. ALLE Kontrollzeichen (C0/C1 inkl. CR/LF/TAB) zuerst entfernen —
       sonst koennte das Entfernen verschleierte Secret-Patterns
       zusammensetzen (binding_\x00hash -> binding_hash).
    2. Laengenbegrenzung: Gesamtlaenge <= DISPLAY_VALUE_MAX_LEN (200),
       bei Ueberschreitung 199 Zeichen + Ellipse (kein 201er).
    3. Verbotsmuster -> REDACTED.
    4. Finalen Ausgabewert NOCHMALS gegen das Verbotsmuster pruefen
       (defensiv gegen jede Transformation).
    """
    if not isinstance(value, str):
        return value
    value = _CONTROL_CHARS_RE.sub("", value)
    if len(value) > DISPLAY_VALUE_MAX_LEN:
        value = value[: DISPLAY_VALUE_MAX_LEN - 1] + "…"
    if _FORBIDDEN_VALUE_RE.search(value):
        return REDACTED
    if _FORBIDDEN_VALUE_RE.search(value):  # noqa: PLR0124 — finaler Check
        return REDACTED
    return value


def _is_forbidden(column: str) -> bool:
    return column in FORBIDDEN_COLUMNS


def _check_deadline(deadline: float) -> None:
    """Explizite Deadline-Pruefung zwischen Queries/Materialisierung."""
    if time.monotonic() > deadline:
        raise SnapshotDeadlineExceeded("snapshot deadline exceeded")


def sanitize_row(
    row: dict[str, Any], allowed_columns: tuple[str, ...]
) -> dict[str, Any]:
    """Extrahiert NUR erlaubte Spalten aus einer materialisierten Row.

    - Spalten-Allowlist (Canary: Verbotsfeld in Allowlist -> AssertionError)
    - ID-Felder werden gekuerzt
    - freie TEXT-Werte werden wertbasiert gescannt/redigiert
    """
    out: dict[str, Any] = {}
    for col in allowed_columns:
        assert not _is_forbidden(col), f"forbidden column in allowlist: {col}"
        value = row[col]
        if col in TRUNCATED_ID_FIELDS:
            # Kuerzen UND wertbasiert scannen (R2-H1: IDs koennen
            # Secret-/Pfad-Muster tragen).
            value = truncate_id(value)
            if isinstance(value, str):
                value = sanitize_display_value(value)
        elif isinstance(value, str):
            value = sanitize_display_value(value)
        out[col] = value
    return out


# --- Queries (explizite Spaltenlisten; kein SELECT *) ----------------------
# Rohzeilen werden materialisiert (dict), Sanitization passiert NACH COMMIT.


def _materialize(rows) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _query_supervisor_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = (
        "id", "task_id", "status", "workflow_state", "expected_role",
        "attempt_no", "dispatch_status", "result_status", "rework_cycle",
        "recovery_state", "gate_status", "gate_scope", "gate_closed",
        "next_action", "next_wake_at", "retry_count", "last_error_code",
        "last_progress_at", "terminal", "created_at", "updated_at",
    )
    sql = "SELECT " + ", ".join(cols) + " FROM supervisor_jobs ORDER BY updated_at DESC LIMIT 100"
    return _materialize(conn.execute(sql).fetchall())


def _query_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = (
        "id", "state", "resume_state", "source_class", "risk_class",
        "external_actions_policy", "created_at", "updated_at",
    )
    sql = "SELECT " + ", ".join(cols) + " FROM tasks ORDER BY updated_at DESC LIMIT 100"
    return _materialize(conn.execute(sql).fetchall())


def _query_owner_approvals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = (
        "id", "task_id", "action", "scope", "status", "requested_by",
        "source_class", "created_at", "decided_at", "consumed_at", "expires_at",
    )
    sql = (
        "SELECT " + ", ".join(cols)
        + " FROM owner_approvals ORDER BY created_at DESC LIMIT 100"
    )
    return _materialize(conn.execute(sql).fetchall())


def _query_notifications(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = (
        "id", "task_id", "dispatch_id", "notification_type", "event_ref",
        "event_version", "status", "attempt_count", "next_attempt_at",
        "last_attempt_at", "last_error_code", "created_at", "updated_at",
    )
    sql = (
        "SELECT " + ", ".join(cols)
        + " FROM notification_outbox ORDER BY created_at DESC LIMIT 100"
    )
    return _materialize(conn.execute(sql).fetchall())


def _query_agent_dispatches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = (
        "id", "task_id", "role", "expected_agent_class", "expected_model_class",
        "actual_provider", "actual_model", "thinking_tier", "status", "cycle_no",
        "position", "attempt_no", "created_at", "started_at", "consumed_at",
    )
    sql = (
        "SELECT " + ", ".join(cols)
        + " FROM agent_dispatches ORDER BY created_at DESC LIMIT 200"
    )
    return _materialize(conn.execute(sql).fetchall())


def _query_test_runs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = ("id", "task_id", "result", "created_at")
    sql = (
        "SELECT " + ", ".join(cols)
        + " FROM test_runs ORDER BY created_at DESC LIMIT 20"
    )
    return _materialize(conn.execute(sql).fetchall())


# --- Snapshot-Aggregation (NACH Transaktionsende) --------------------------

_JOBS_COLS = (
    "id", "task_id", "status", "workflow_state", "expected_role", "attempt_no",
    "dispatch_status", "result_status", "rework_cycle", "recovery_state",
    "gate_status", "gate_scope", "gate_closed", "next_action", "next_wake_at",
    "retry_count", "last_error_code", "last_progress_at", "terminal",
    "created_at", "updated_at",
)
_TASKS_COLS = (
    "id", "state", "resume_state", "source_class", "risk_class",
    "external_actions_policy", "created_at", "updated_at",
)
_APPROVALS_COLS = (
    "id", "task_id", "action", "scope", "status", "requested_by",
    "source_class", "created_at", "decided_at", "consumed_at", "expires_at",
)
_NOTIFICATIONS_COLS = (
    "id", "task_id", "dispatch_id", "notification_type", "event_ref",
    "event_version", "status", "attempt_count", "next_attempt_at",
    "last_attempt_at", "last_error_code", "created_at", "updated_at",
)
_DISPATCHES_COLS = (
    "id", "task_id", "role", "expected_agent_class", "expected_model_class",
    "actual_provider", "actual_model", "thinking_tier", "status", "cycle_no",
    "position", "attempt_no", "created_at", "started_at", "consumed_at",
)
_TEST_RUNS_COLS = ("id", "task_id", "result", "created_at")


def _aggregate_counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(key)
        if value is not None:
            counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def build_snapshot(
    db_path: str | os.PathLike[str],
    git_head: Optional[str] = None,
    deadline_s: float = MAX_QUERY_SECONDS,
) -> dict[str, Any]:
    """Oeffnet die Argent-DB read-only und baut den sanitisierten Snapshot.

    - Harte Deadline via ``set_progress_handler``; bei Ueberschreitung wird
      ``SnapshotDeadlineExceeded`` geworfen (Aufrufer: Skip).
    - Rohzeilen werden innerhalb der kurzen Read-Transaktion materialisiert;
      Sanitization/Aggregation erst NACH COMMIT (nie Supervisor blockieren).
    """
    resolved = Path(db_path).resolve()
    deadline = time.monotonic() + max(deadline_s, 0.001)
    git_head = sanitize_git_head(git_head)  # fail-closed: nur Git-Hash

    def _progress_handler() -> int:
        if time.monotonic() > deadline:
            raise SnapshotDeadlineExceeded("snapshot deadline exceeded")
        return 0

    conn = sqlite3.connect(
        f"file:{resolved}?mode=ro", uri=True, isolation_level=None, timeout=0
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 0")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.set_progress_handler(_progress_handler, PROGRESS_STEPS)

        start = time.monotonic()
        conn.execute("BEGIN")  # deferred read transaction
        try:
            jobs_raw = _query_supervisor_jobs(conn)
            _check_deadline(deadline)
            tasks_raw = _query_tasks(conn)
            _check_deadline(deadline)
            approvals_raw = _query_owner_approvals(conn)
            _check_deadline(deadline)
            notifications_raw = _query_notifications(conn)
            _check_deadline(deadline)
            dispatches_raw = _query_agent_dispatches(conn)
            _check_deadline(deadline)
            test_runs_raw = _query_test_runs(conn)
            _check_deadline(deadline)
            # Schema-Version in derselben Read-Transaktion NUR materialisieren
            # (konsistenter Snapshot, in der Deadline-Messung enthalten);
            # Sanitization (RegEx) erst NACH COMMIT (R2-L1).
            schema_version_raw: Optional[str] = None
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None:
                schema_version_raw = str(row["value"])
            _check_deadline(deadline)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        elapsed = time.monotonic() - start
    except SnapshotDeadlineExceeded:
        raise
    finally:
        conn.close()

    # Sanitization NACH Transaktionsende (R2-L1).
    schema_version = sanitize_schema_version(schema_version_raw)

    # --- Sanitization NACH Transaktionsende -------------------------------
    jobs = [sanitize_row(r, _JOBS_COLS) for r in jobs_raw]
    tasks = [sanitize_row(r, _TASKS_COLS) for r in tasks_raw]
    approvals = [sanitize_row(r, _APPROVALS_COLS) for r in approvals_raw]
    notifications = [sanitize_row(r, _NOTIFICATIONS_COLS) for r in notifications_raw]
    dispatches = [sanitize_row(r, _DISPATCHES_COLS) for r in dispatches_raw]
    test_runs = [sanitize_row(r, _TEST_RUNS_COLS) for r in test_runs_raw]

    # Aktive Rolle: aus dem juengsten nicht-terminalen Job.
    active_role: Optional[str] = None
    active_task: Optional[str] = None
    for job in jobs:
        if job.get("terminal") is None:
            active_role = job.get("expected_role") or active_role
            active_task = job.get("task_id") or active_task
            break

    # Pro Task: letzter Dispatch + letzter Job (Details fuer Tasks-Sektion).
    latest_dispatch_by_task: dict[str, dict[str, Any]] = {}
    for d in dispatches:
        latest_dispatch_by_task.setdefault(d["task_id"], d)
    latest_job_by_task: dict[str, dict[str, Any]] = {}
    for j in jobs:
        latest_job_by_task.setdefault(j["task_id"], j)

    enriched_tasks: list[dict[str, Any]] = []
    for t in tasks:
        d = latest_dispatch_by_task.get(t["id"], {})
        j = latest_job_by_task.get(t["id"], {})
        duration_s: Optional[int] = None
        started = d.get("started_at")
        consumed = d.get("consumed_at")
        if started and consumed and isinstance(started, str) and isinstance(consumed, str):
            try:
                from datetime import datetime as _dt

                t0 = _dt.fromisoformat(started.replace("Z", "+00:00"))
                t1 = _dt.fromisoformat(consumed.replace("Z", "+00:00"))
                delta = (t1 - t0).total_seconds()
                if delta >= 0:
                    duration_s = int(delta)
            except (ValueError, TypeError):
                duration_s = None  # keine Synthese bei unbrauchbaren Werten
        enriched_tasks.append(
            {
                **t,
                "last_role": d.get("role"),
                "last_model": d.get("actual_model"),
                "last_started_at": d.get("started_at"),
                "last_status": d.get("status"),
                "duration_s": duration_s,
                "retry_count": j.get("retry_count"),
                "result_status": j.get("result_status"),
                "last_error_code": j.get("last_error_code"),
                "workflow_state": j.get("workflow_state"),
            }
        )

    # Agenten-Zusammenfassung je Rolle.
    agents: dict[str, dict[str, Any]] = {}
    for role in WORKFLOW_ROLES:
        role_dispatches = [d for d in dispatches if d.get("role") == role]
        latest = role_dispatches[0] if role_dispatches else None
        status = "running" if latest and latest.get("status") == "RUNNING" else "idle"
        agents[role] = {
            "role": role,
            "status": status,
            "current_task": truncate_id(latest.get("task_id")) if latest else None,
            "model": latest.get("actual_model") if latest else None,
            "last_run": truncate_id(latest.get("id")) if latest else None,
            "last_status": latest.get("status") if latest else None,
            "last_started_at": latest.get("started_at") if latest else None,
        }

    generated_at = utcnow_iso()
    snapshot: dict[str, Any] = {
        "snapshot_version": SNAPSHOT_VERSION,
        "schema_name": SNAPSHOT_SCHEMA_NAME,
        "generated_at": generated_at,
        "source_schema_version": schema_version,
        "git_head": git_head,
        "system_status": {
            "supervisor_status": jobs[0].get("status") if jobs else "NO_JOBS",
            "last_reconcile": jobs[0].get("last_progress_at") if jobs else None,
            "current_job": truncate_id(jobs[0].get("id")) if jobs else None,
            "current_task": truncate_id(jobs[0].get("task_id")) if jobs else None,
            "job_counts": _aggregate_counts(jobs, "status"),
            "open_owner_gates": sum(
                1 for a in approvals if a.get("status") in ("pending", "approved")
            ),
            "last_error": jobs[0].get("last_error_code") if jobs else None,
        },
        "workflow": {
            "roles": WORKFLOW_ROLES,
            "active_role": active_role,
            "active_task": active_task,
            "latest_state": jobs[0].get("workflow_state") if jobs else None,
        },
        "tasks": enriched_tasks,
        "agents": agents,
        "owner_approvals": {
            "counts": _aggregate_counts(approvals, "status"),
            "items": approvals,
        },
        "notifications": {
            "counts": _aggregate_counts(notifications, "status"),
            "items": notifications,
        },
        "system_health": {
            "db_reachable": True,
            "snapshot_age_s": 0,
            "git_head": git_head,
            "query_elapsed_ms": int(round(elapsed * 1000)),
            "recovery_state": jobs[0].get("recovery_state") if jobs else None,
            "last_test_run": (
                {"id": test_runs[0].get("id"), "result": test_runs[0].get("result"),
                 "created_at": test_runs[0].get("created_at")}
                if test_runs else None
            ),
        },
        "phase_status": PHASE_STATUS,
    }
    return snapshot


@dataclass
class PublishResult:
    ok: bool
    path: Optional[str] = None
    error: Optional[str] = None
    skipped: bool = False
    snapshot: Optional[dict[str, Any]] = field(default=None)


def publish_snapshot(
    db_path: str | os.PathLike[str],
    out_path: str | os.PathLike[str],
    git_head: Optional[str] = None,
    mode: int = 0o600,
    deadline_s: float = MAX_QUERY_SECONDS,
) -> PublishResult:
    """Baut den Snapshot und schreibt ihn atomar (Tempfile + os.replace).

    - Parent-Verzeichnis restriktiv (0o700) beim Neuanlegen.
    - Bei DB-Busy/Deadline/Fehler: Skip (letzter Snapshot bleibt).
    """
    out = Path(out_path).resolve()
    try:
        parent = out.parent
        created_parent = not parent.exists()
        parent.mkdir(parents=True, exist_ok=True)
        if created_parent:
            # NUR das neu erstellte Leaf-Verzeichnis restriktiv setzen;
            # bestehende Verzeichnisse bleiben unveraendert.
            os.chmod(parent, 0o700)
        snapshot = build_snapshot(db_path, git_head=git_head, deadline_s=deadline_s)
    except SnapshotDeadlineExceeded as exc:
        return PublishResult(ok=False, error=f"deadline_exceeded: {exc}", skipped=True)
    except sqlite3.OperationalError as exc:
        # busy/locked/read-only-Fehler -> Skip, letzter Snapshot bleibt.
        return PublishResult(ok=False, error=f"db_unavailable: {exc}", skipped=True)
    except Exception as exc:  # pragma: no cover - defensive
        return PublishResult(ok=False, error=f"publish_error: {exc}")

    fd, tmp_name = tempfile.mkstemp(
        prefix=".supervisor_snapshot.", suffix=".tmp", dir=str(parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, out)
    except Exception as exc:  # pragma: no cover - defensive
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        return PublishResult(ok=False, error=f"write_error: {exc}")
    return PublishResult(ok=True, path=str(out), snapshot=snapshot)


def main(argv: Optional[list[str]] = None) -> int:
    """One-Shot-CLI: --db <pfad> [--out <pfad>] [--git-head <hash>]."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Phase 3D: Argent Supervisor-Snapshot (read-only) publizieren."
    )
    parser.add_argument("--db", required=True, help="Pfad zur Argent-SQLite-DB")
    parser.add_argument(
        "--out",
        default=str(
            Path.home() / ".local" / "state" / "argent" / "visualizer" / "supervisor_snapshot.json"
        ),
        help="Ziel-Snapshot-Datei",
    )
    parser.add_argument("--git-head", default=None, help="Argent-Git-HEAD (Hash)")
    args = parser.parse_args(argv)

    result = publish_snapshot(args.db, args.out, git_head=args.git_head)
    if not result.ok:
        print(f"publish skipped/failed: {result.error}", file=__import__("sys").stderr)
        return 1 if not result.skipped else 0
    print(f"snapshot written: {result.path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

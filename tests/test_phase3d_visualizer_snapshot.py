"""Phase 3D — Publisher-Unit-/Security-Tests (read-only Snapshot).

Prueft: read-only-Zugriff (query_only), atomarer Write, Verbotsfeld-Canary,
ID-Kuerzung, Lock-Vermeidung (kein BEGIN IMMEDIATE moeglich), Skip-on-Busy,
geschlossenes Schema, kein Schreibzugriff auf die Quelle.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argent_core import visualizer_snapshot as vs  # noqa: E402


@pytest.fixture()
def fixture_db(tmp_path: Path) -> Path:
    """Echte Argent-artige DB im DELETE journal mode (wie Store.__init__)."""
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '6')"
    )
    conn.execute(
        """
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL DEFAULT 'p1',
          title TEXT NOT NULL, description TEXT, state TEXT NOT NULL,
          resume_state TEXT, source TEXT NOT NULL, source_class TEXT NOT NULL,
          risk_class TEXT NOT NULL DEFAULT 'NORMAL',
          external_actions_policy TEXT NOT NULL DEFAULT 'ALLOWED_WITH_GATE',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          idempotency_key TEXT UNIQUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE supervisor_jobs (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, status TEXT NOT NULL,
          workflow_state TEXT NOT NULL, expected_role TEXT,
          expected_dispatch_id TEXT, agent_id TEXT, session_id TEXT,
          run_id TEXT, attempt_no INTEGER NOT NULL DEFAULT 0,
          dispatch_status TEXT, result_status TEXT NOT NULL DEFAULT 'NOT_OBSERVED',
          result_consumed INTEGER NOT NULL DEFAULT 0, current_handoff_id TEXT,
          open_findings_count INTEGER NOT NULL DEFAULT 0,
          rework_cycle INTEGER NOT NULL DEFAULT 1,
          recovery_state TEXT NOT NULL DEFAULT 'NONE',
          owner_gate_id TEXT, gate_status TEXT, gate_scope TEXT,
          gate_closed INTEGER NOT NULL DEFAULT 0, owner_prompted_at TEXT,
          owner_prompted_gate_id TEXT, next_action TEXT NOT NULL DEFAULT 'NONE',
          next_wake_at TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
          missing_confirmations INTEGER NOT NULL DEFAULT 0,
          last_error_code TEXT, last_progress_at TEXT NOT NULL,
          terminal TEXT, facts_version INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE owner_approvals (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, action TEXT NOT NULL,
          scope TEXT NOT NULL, status TEXT NOT NULL, requested_by TEXT NOT NULL,
          source_class TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT,
          consumed_at TEXT, expires_at TEXT NOT NULL, idempotency_key TEXT UNIQUE,
          binding_hash TEXT NOT NULL, approved_at TEXT, execution_id TEXT,
          executed_at TEXT, closed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE notification_outbox (
          id TEXT PRIMARY KEY, supervisor_job_id TEXT NOT NULL,
          task_id TEXT NOT NULL, dispatch_id TEXT, gate_id TEXT,
          notification_type TEXT NOT NULL, event_ref TEXT NOT NULL,
          event_version INTEGER NOT NULL DEFAULT 1, dedup_key TEXT NOT NULL,
          payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'PENDING',
          attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
          claimed_at TEXT, claim_token TEXT, last_attempt_at TEXT,
          sent_at TEXT, last_error_code TEXT, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE agent_dispatches (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
          task_run_id TEXT NOT NULL, role TEXT NOT NULL,
          parent_dispatch_id TEXT, expected_agent_class TEXT NOT NULL,
          expected_model_class TEXT NOT NULL,
          expected_thinking_tier TEXT NOT NULL DEFAULT 'medium',
          child_session_id TEXT, openclaw_run_id TEXT, actual_provider TEXT,
          actual_model TEXT, thinking_tier TEXT, status TEXT NOT NULL,
          cycle_no INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL,
          sequence_kind TEXT NOT NULL, attempt_no INTEGER NOT NULL DEFAULT 1,
          handoff_id TEXT, result_json TEXT, created_at TEXT NOT NULL,
          started_at TEXT, consumed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE test_runs (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
          result TEXT NOT NULL, detail TEXT,
          created_at TEXT NOT NULL, idempotency_key TEXT UNIQUE
        )
        """
    )
    conn.execute(
        """
        INSERT INTO tasks (id, title, description, state, source, source_class,
          created_at, updated_at)
        VALUES ('task-1111111111111111', 'Phase 3D Test', NULL, 'REVIEWING',
                'owner', 'TRUSTED', '2026-08-30T10:00:00Z', '2026-08-30T10:05:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO supervisor_jobs (id, task_id, status, workflow_state,
          expected_role, attempt_no, result_status, retry_count,
          last_error_code, last_progress_at, terminal, created_at, updated_at)
        VALUES ('job-2222222222222222', 'task-1111111111111111', 'ACTIVE',
                'REVIEWING', 'reviewer', 1, 'RUNNING', 2, 'E_RETRY_OK',
                '2026-08-30T10:04:00Z', NULL, '2026-08-30T10:00:00Z',
                '2026-08-30T10:04:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO owner_approvals (id, task_id, action, scope, status,
          requested_by, source_class, created_at, expires_at, binding_hash)
        VALUES ('appr-3333333333333333', 'task-1111111111111111',
                'external_publish', 'scope-x', 'pending', 'lead', 'TRUSTED',
                '2026-08-30T10:01:00Z', '2026-08-30T11:01:00Z',
                'deadbeef' * 8)
        """
    )
    conn.execute(
        """
        INSERT INTO notification_outbox (id, supervisor_job_id, task_id,
          notification_type, event_ref, event_version, dedup_key, payload_json,
          payload_hash, status, attempt_count, created_at, updated_at)
        VALUES ('ntf-4444444444444444', 'job-2222222222222222',
                'task-1111111111111111', 'OWNER_APPROVAL_REQUIRED',
                'evt-abcdef123456', 1, 'dedup-1', '{"secret":"x"}',
                'a' * 64, 'PENDING', 0, '2026-08-30T10:02:00Z',
                '2026-08-30T10:02:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO agent_dispatches (id, task_id, task_run_id, role,
          expected_agent_class, expected_model_class, status, position,
          sequence_kind, child_session_id, openclaw_run_id, actual_model,
          result_json, created_at, started_at)
        VALUES ('dsp-5555555555555555', 'task-1111111111111111', 'run-1',
                'reviewer', 'argent-reviewer', 'openai/gpt-5.6-sol',
                'RUNNING', 0, 'main', 'child-session-abcdef123456',
                'openclaw-run-1234567890', 'openai/gpt-5.6-sol',
                '{"answer":"confidential"}',
                '2026-08-30T10:03:00Z', '2026-08-30T10:03:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO test_runs (id, task_id, result, created_at)
        VALUES ('test-6666666666666666', 'task-1111111111111111', 'passed',
                '2026-08-30T10:06:00Z')
        """
    )
    conn.commit()
    conn.close()
    return db


def test_readonly_no_write_possible(fixture_db: Path) -> None:
    """mode=ro: Schreibversuch muss fehlschlagen."""
    conn = sqlite3.connect(f"file:{fixture_db}?mode=ro", uri=True, isolation_level=None)
    conn.execute("PRAGMA query_only = ON")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('x', 'y')")
    conn.close()


def test_build_snapshot_allowlist(fixture_db: Path) -> None:
    snap = vs.build_snapshot(fixture_db, git_head="17b51b6")
    assert snap["snapshot_version"] == 1
    assert snap["source_schema_version"] == "6"
    assert snap["git_head"] == "17b51b6"

    # Verbotsfelder nirgends im Snapshot (rekursiv).
    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in vs.FORBIDDEN_COLUMNS, f"{path}.{k}"
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(snap)

    # binding_hash darf nicht durchsickern.
    raw = json.dumps(snap)
    assert "deadbeef" not in raw
    assert "confidential" not in raw
    assert "secret" not in raw


def test_id_truncation(fixture_db: Path) -> None:
    snap = vs.build_snapshot(fixture_db)
    job = snap["system_status"]
    # IDs sind gekuerzt (max 12 Zeichen).
    assert len(job["current_job"]) <= 12
    assert job["current_job"] == "job-22222222"  # 12 Zeichen
    # workflow active task gekuerzt.
    assert len(snap["workflow"]["active_task"]) <= 12


def test_agents_summary(fixture_db: Path) -> None:
    snap = vs.build_snapshot(fixture_db)
    agents = snap["agents"]
    assert set(agents.keys()) == {"lead", "analyst", "implementer", "qa", "reviewer"}
    reviewer = agents["reviewer"]
    assert reviewer["status"] == "running"
    assert reviewer["model"] == "openai/gpt-5.6-sol"
    assert reviewer["last_status"] == "RUNNING"
    assert len(reviewer["last_run"]) <= 12


def test_counts_and_phases(fixture_db: Path) -> None:
    snap = vs.build_snapshot(fixture_db)
    assert snap["system_status"]["job_counts"] == {"ACTIVE": 1}
    assert snap["system_status"]["open_owner_gates"] == 1
    assert snap["owner_approvals"]["counts"] == {"pending": 1}
    assert snap["notifications"]["counts"] == {"PENDING": 1}
    phases = {p["phase"]: p["status"] for p in snap["phase_status"]}
    assert phases["3C-B2B"] == "WAITING_UPSTREAM"
    assert phases["3D"] == "ACTIVE"


def test_publish_atomic_and_permissions(tmp_path: Path, fixture_db: Path) -> None:
    out = tmp_path / "state" / "argent" / "visualizer" / "supervisor_snapshot.json"
    # Vorfahr mit existierenden Rechten anlegen (darf NICHT umgeschrieben werden).
    ancestor = out.parent.parent.parent  # .../state/argent
    ancestor.mkdir(parents=True, exist_ok=True)
    ancestor.chmod(0o755)
    result = vs.publish_snapshot(fixture_db, out, git_head="17b51b6")
    assert result.ok
    assert out.exists()
    # Parent (neu erstellt) restriktiv.
    assert (out.parent.stat().st_mode & 0o777) == 0o700
    # Vorfahr bleibt unveraendert (bestehendes Verzeichnis wird nicht chmod't).
    assert (ancestor.stat().st_mode & 0o777) == 0o755
    # Datei-Mode 0o600.
    assert (out.stat().st_mode & 0o777) == 0o600
    # Keine Temp-Dateien zurueckgelassen.
    leftovers = [p for p in out.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    # Valides JSON mit Schema.
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_name"] == "argent-supervisor-snapshot"


def test_publish_missing_db_skips(fixture_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "snap.json"
    result = vs.publish_snapshot(tmp_path / "nope.db", out)
    assert not result.ok
    assert result.skipped
    assert not out.exists()


def test_publish_keeps_last_good_on_failure(
    tmp_path: Path, fixture_db: Path
) -> None:
    out = tmp_path / "snap.json"
    first = vs.publish_snapshot(fixture_db, out)
    assert first.ok
    before = out.read_bytes()
    # Zweite Publikation gegen kaputte DB -> Skip, alter Snapshot bleibt.
    second = vs.publish_snapshot(tmp_path / "missing.db", out)
    assert second.skipped
    assert out.read_bytes() == before


def test_sanitize_row_canary_raises() -> None:
    """Canary: Verbotsfeld in der Allowlist -> AssertionError (nie still)."""
    import sqlite3 as _s

    conn = _s.connect(":memory:")
    conn.execute("CREATE TABLE t (binding_hash TEXT, ok TEXT)")
    conn.execute("INSERT INTO t VALUES ('x', 'y')")
    conn.row_factory = _s.Row
    row = dict(conn.execute("SELECT * FROM t").fetchone())
    with pytest.raises(AssertionError):
        vs.sanitize_row(row, ("binding_hash",))
    # Erlaubtes Feld funktioniert.
    out = vs.sanitize_row(row, ("ok",))
    assert out == {"ok": "y"}
    conn.close()


def test_sanitize_display_value_redacts_secrets() -> None:
    """Freie TEXT-Werte mit Verbotsmustern werden redigiert (Wert-Canary)."""
    assert vs.sanitize_display_value("scope-x") == "scope-x"
    assert vs.sanitize_display_value("token_hash=SECRET_CANARY") == vs.REDACTED
    assert vs.sanitize_display_value("credential=abc123") == vs.REDACTED
    assert vs.sanitize_display_value("ghp_abcdef123456") == vs.REDACTED
    assert vs.sanitize_display_value("sk-proj-abcdef123456") == vs.REDACTED
    assert vs.sanitize_display_value("key=ABCD1234") == vs.REDACTED
    assert vs.sanitize_display_value("Bearer TOPSECRET") == vs.REDACTED
    assert vs.sanitize_display_value("/home/pc/private/ledger.db") == vs.REDACTED
    # Laengenlimit: Gesamtlaenge inkl. Ellipse <= 200 (Reader-kompatibel).
    long = vs.sanitize_display_value("x" * 300)
    assert len(long) <= vs.DISPLAY_VALUE_MAX_LEN
    assert long.endswith("…")
    # Grenzen 199/200/201.
    assert len(vs.sanitize_display_value("x" * 199)) == 199
    assert len(vs.sanitize_display_value("x" * 200)) == 200
    assert len(vs.sanitize_display_value("x" * 201)) == vs.DISPLAY_VALUE_MAX_LEN


def test_publisher_reader_length_consistency() -> None:
    """Publisher-Laengenlimit <= 200 passt zur Reader-Grenze (kein 201er)."""
    src = Path(vs.__file__).read_text(encoding="utf-8")
    assert "DISPLAY_VALUE_MAX_LEN - 1" in src  # 199 + Ellipse
    rdr = Path(
        "/home/pc/projects/system-visualizer-3d/backend/app/services/argent_snapshot.py"
    )
    if rdr.exists():
        rsrc = rdr.read_text(encoding="utf-8")
        assert "DISPLAY_MAX_LEN = 200" in rsrc


def test_scope_value_canary_redacted(fixture_db: Path) -> None:
    """Scope mit eingebettetem Secret-Muster wird im Snapshot redigiert."""
    import sqlite3 as _s

    conn = _s.connect(str(fixture_db), isolation_level=None)
    conn.execute(
        "UPDATE owner_approvals SET scope = 'credential=SECRET_CANARY' "
        "WHERE id = 'appr-3333333333333333'"
    )
    conn.commit()
    conn.close()
    snap = vs.build_snapshot(fixture_db)
    item = snap["owner_approvals"]["items"][0]
    assert item["scope"] == vs.REDACTED
    assert "SECRET_CANARY" not in json.dumps(snap)


def test_busy_db_skips(tmp_path: Path, fixture_db: Path) -> None:
    """Exklusiver Lock von aussen -> Publisher skipped, kein Blockieren."""
    import sqlite3 as _s

    locker = _s.connect(str(fixture_db), isolation_level=None, timeout=0)
    locker.execute("BEGIN EXCLUSIVE")  # echter Write-Lock (DELETE-Mode)
    try:
        out = tmp_path / "snap.json"
        result = vs.publish_snapshot(fixture_db, out)
        assert not result.ok
        assert result.skipped
        assert not out.exists()
    finally:
        locker.rollback()
        locker.close()


def test_deadline_enforced(tmp_path: Path, fixture_db: Path, monkeypatch) -> None:
    """Harte Zeitgrenze: set_progress_handler bricht bei Ueberschreitung ab."""
    out = tmp_path / "snap.json"
    # Progress-Handler bei jedem Schritt feuern lassen + Zeit springt sofort
    # ueber die Deadline -> SnapshotDeadlineExceeded -> Skip.
    monkeypatch.setattr(vs, "PROGRESS_STEPS", 1)
    real_monotonic = vs.time.monotonic
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        if calls["n"] == 1:  # Deadline-Berechnung
            return real_monotonic()
        return real_monotonic() + 10  # ab erstem Progress-Check ueberschritten

    monkeypatch.setattr(vs.time, "monotonic", fake_monotonic)
    result = vs.publish_snapshot(fixture_db, out, git_head=None)
    assert not result.ok
    assert result.skipped
    # sqlite3 meldet Handler-Abbruch generisch als "interrupted".
    assert "deadline" in (result.error or "") or "interrupted" in (result.error or "")
    assert not out.exists()
    # Mit normaler Deadline funktioniert es weiterhin.
    ok = vs.publish_snapshot(fixture_db, out)
    assert ok.ok
    assert out.exists()


def test_build_snapshot_uses_readonly_connection(fixture_db: Path, monkeypatch) -> None:
    """build_snapshot oeffnet die DB tatsaechlich mit mode=ro (URI-Capture)."""
    import sqlite3 as _s

    orig_connect = _s.connect
    captured = []

    def fake_connect(*args, **kwargs):
        captured.append((args[0] if args else kwargs.get("uri"), kwargs))
        return orig_connect(*args, **kwargs)

    monkeypatch.setattr(vs.sqlite3, "connect", fake_connect)
    vs.build_snapshot(fixture_db)
    assert captured, "connect wurde nicht aufgerufen"
    uri, kwargs = captured[0]
    assert "mode=ro" in uri
    assert kwargs.get("uri") is True
    assert kwargs.get("isolation_level") is None
    assert kwargs.get("timeout") == 0
    # Beweis: ueber genau diese URI ist kein Write moeglich.
    conn = orig_connect(uri, uri=True, isolation_level=None, timeout=0)
    conn.execute("PRAGMA query_only = ON")
    with pytest.raises(_s.OperationalError):
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('x', 'y')")
    conn.close()


def test_build_snapshot_readonly_connection_recursion_free(
    fixture_db: Path, monkeypatch
) -> None:
    """Sicherstellung: fake-connect ruft die ORIGINAL-connect auf (keine Rekursion)."""
    import sqlite3 as _s

    orig_connect = _s.connect
    captured = {}

    def fake_connect(*args, **kwargs):
        captured["uri"] = args[0] if args else kwargs.get("uri")
        conn = orig_connect(*args, **kwargs)
        return conn

    monkeypatch.setattr(vs.sqlite3, "connect", fake_connect)
    vs.build_snapshot(fixture_db)
    assert "mode=ro" in captured["uri"]


def test_enriched_tasks_and_health(fixture_db: Path) -> None:
    """Tasks tragen Dispatch-/Job-Details; Health hat Recovery-/Test-Indikatoren."""
    snap = vs.build_snapshot(fixture_db)
    task = snap["tasks"][0]
    assert task["last_role"] == "reviewer"
    assert task["last_model"] == "openai/gpt-5.6-sol"
    assert task["last_status"] == "RUNNING"
    assert task["retry_count"] == 2
    assert task["result_status"] == "RUNNING"
    health = snap["system_health"]
    assert health["last_test_run"]["result"] == "passed"
    assert health["last_test_run"]["id"] == "test-6666666"
    assert health["recovery_state"] == "NONE"


def test_no_immutable_flag_used() -> None:
    """immutable=1 ist fuer live beschriebene DBs verboten — nie verwenden."""
    src = Path(vs.__file__).read_text(encoding="utf-8")
    # Nur ausfuehrbare Patterns pruefen (Docstring-Beschreibungen sind ok).
    assert '"immutable=1"' not in src
    assert 'execute("BEGIN IMMEDIATE"' not in src
    assert 'execute("BEGIN EXCLUSIVE"' not in src
    assert 'execute("PRAGMA journal_mode' not in src


def test_git_head_sanitized_fail_closed(fixture_db: Path) -> None:
    """git_head: nur Git-Hash; Secret-Muster/anderes -> None (fail-closed)."""
    snap = vs.build_snapshot(fixture_db, git_head="ghp_1234567890abcdefghijklmn")
    assert snap["git_head"] is None
    snap2 = vs.build_snapshot(fixture_db, git_head="17b51b6")
    assert snap2["git_head"] == "17b51b6"


def test_schema_version_sanitized(fixture_db: Path) -> None:
    snap = vs.build_snapshot(fixture_db)
    assert snap["source_schema_version"] == "6"
    # böswillige schema_meta-Werte -> None (fail-closed) — via Monkeypatch
    # auf sanitize_schema_version direkt getestet:
    assert vs.sanitize_schema_version("6; DROP TABLE tasks") is None
    assert vs.sanitize_schema_version("1.0") == "1.0"


def test_tasks_source_not_published(fixture_db: Path) -> None:
    snap = vs.build_snapshot(fixture_db)
    task = snap["tasks"][0]
    assert "source" not in task
    assert task["source_class"] == "TRUSTED"


def test_parent_chmod_only_when_created(
    tmp_path: Path, fixture_db: Path
) -> None:
    """Bestehende Parent-Verzeichnisse werden NICHT umgechmodet."""
    existing = tmp_path / "existing" / "nested"
    existing.mkdir(parents=True)
    existing.chmod(0o755)
    out = existing / "supervisor_snapshot.json"
    result = vs.publish_snapshot(fixture_db, out)
    assert result.ok
    assert (existing.stat().st_mode & 0o777) == 0o755  # unverändert

    # Neu erstelltes Leaf -> 0o700.
    fresh = tmp_path / "fresh" / "leaf"
    out2 = fresh / "supervisor_snapshot.json"
    result2 = vs.publish_snapshot(fixture_db, out2)
    assert result2.ok
    assert (fresh.stat().st_mode & 0o777) == 0o700


def test_deadline_checks_between_queries(
    tmp_path: Path, fixture_db: Path, monkeypatch
) -> None:
    """Expliziter Deadline-Check zwischen Queries greift (ROLLBACK-Pfad).

    PROGRESS_STEPS wird auf einen sehr hohen Wert gesetzt, damit der
    SQLite-Progress-Handler NICHT feuert; der Abbruch muss aus dem
    expliziten Zwischen-Check (``_check_deadline``) kommen.
    """
    real = vs.time.monotonic
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        if calls["n"] <= 2:  # Deadline-Berechnung + erster Check
            return real()
        return real() + 10  # ab zweitem Zwischen-Check überschritten

    monkeypatch.setattr(vs.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(vs, "PROGRESS_STEPS", 10**9)  # Handler feuert nie
    result = vs.publish_snapshot(fixture_db, tmp_path / "snap.json")
    assert not result.ok
    assert result.skipped
    assert "deadline" in (result.error or "")


def test_secret_patterns_identical_publisher_reader() -> None:
    """R2-M4: Secret-Pattern-RegEx ist in Publisher und Reader IDENTISCH
    (kanonischer Contract; kein Driften zwischen den Trust-Domains).
    Vergleicht die tatsaechlichen kompilierten Pattern-Quellen inkl.
    Reihenfolge — nicht nur Marker-Texte."""
    import re as _re

    pub = Path(vs.__file__).read_text(encoding="utf-8")
    reader = Path(
        "/home/pc/projects/system-visualizer-3d/backend/app/services/argent_snapshot.py"
    ).read_text(encoding="utf-8")

    def extract_pattern(src: str, var: str) -> str:
        m = _re.search(rf"{var}\s*=\s*re\.compile\(\n(.*?)\n\s*\)", src, _re.S)
        assert m, f"Pattern {var} nicht gefunden"
        return " ".join(m.group(1).split())

    pub_pat = extract_pattern(pub, "_FORBIDDEN_VALUE_RE")
    rdr_pat = extract_pattern(reader, "SECRET_PATTERN_RE")
    assert pub_pat == rdr_pat, (
        f"Secret-Patterns driften:\nPUB:{pub_pat}\nRDR:{rdr_pat}"
    )


def test_empty_db_sentinel(fixture_db: Path, tmp_path: Path) -> None:
    """Leere DB: supervisor_status='NO_JOBS' (Publisher-Sentinel)."""
    import sqlite3 as _s

    empty = tmp_path / "empty.db"
    conn = _s.connect(str(empty), isolation_level=None)
    conn.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '6')"
    )
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL DEFAULT 'p1', title TEXT NOT NULL, description TEXT, state TEXT NOT NULL, resume_state TEXT, source TEXT NOT NULL, source_class TEXT NOT NULL, risk_class TEXT NOT NULL DEFAULT 'NORMAL', external_actions_policy TEXT NOT NULL DEFAULT 'ALLOWED_WITH_GATE', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, idempotency_key TEXT UNIQUE)"
    )
    conn.execute(
        "CREATE TABLE supervisor_jobs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, status TEXT NOT NULL, workflow_state TEXT NOT NULL, expected_role TEXT, expected_dispatch_id TEXT, agent_id TEXT, session_id TEXT, run_id TEXT, attempt_no INTEGER NOT NULL DEFAULT 0, dispatch_status TEXT, result_status TEXT NOT NULL DEFAULT 'NOT_OBSERVED', result_consumed INTEGER NOT NULL DEFAULT 0, current_handoff_id TEXT, open_findings_count INTEGER NOT NULL DEFAULT 0, rework_cycle INTEGER NOT NULL DEFAULT 1, recovery_state TEXT NOT NULL DEFAULT 'NONE', owner_gate_id TEXT, gate_status TEXT, gate_scope TEXT, gate_closed INTEGER NOT NULL DEFAULT 0, owner_prompted_at TEXT, owner_prompted_gate_id TEXT, next_action TEXT NOT NULL DEFAULT 'NONE', next_wake_at TEXT, retry_count INTEGER NOT NULL DEFAULT 0, missing_confirmations INTEGER NOT NULL DEFAULT 0, last_error_code TEXT, last_progress_at TEXT NOT NULL, terminal TEXT, facts_version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE owner_approvals (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, action TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL, requested_by TEXT NOT NULL, source_class TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT, consumed_at TEXT, expires_at TEXT NOT NULL, idempotency_key TEXT UNIQUE, binding_hash TEXT NOT NULL, approved_at TEXT, execution_id TEXT, executed_at TEXT, closed_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE notification_outbox (id TEXT PRIMARY KEY, supervisor_job_id TEXT NOT NULL, task_id TEXT NOT NULL, dispatch_id TEXT, gate_id TEXT, notification_type TEXT NOT NULL, event_ref TEXT NOT NULL, event_version INTEGER NOT NULL DEFAULT 1, dedup_key TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, claimed_at TEXT, claim_token TEXT, last_attempt_at TEXT, sent_at TEXT, last_error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE agent_dispatches (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_run_id TEXT NOT NULL, role TEXT NOT NULL, parent_dispatch_id TEXT, expected_agent_class TEXT NOT NULL, expected_model_class TEXT NOT NULL, expected_thinking_tier TEXT NOT NULL DEFAULT 'medium', child_session_id TEXT, openclaw_run_id TEXT, actual_provider TEXT, actual_model TEXT, thinking_tier TEXT, status TEXT NOT NULL, cycle_no INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL, sequence_kind TEXT NOT NULL, attempt_no INTEGER NOT NULL DEFAULT 1, handoff_id TEXT, result_json TEXT, created_at TEXT NOT NULL, started_at TEXT, consumed_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE test_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    snap = vs.build_snapshot(empty)
    assert snap["system_status"]["supervisor_status"] == "NO_JOBS"
    assert snap["workflow"]["active_role"] is None


def test_schema_version_1_0_contract(fixture_db: Path) -> None:
    """'1.0' ist gültige Schema-Version (identische RegEx Publisher/Reader)."""
    assert vs.sanitize_schema_version("1.0") == "1.0"


def test_sanitize_row_scans_truncated_ids(fixture_db: Path) -> None:
    """R2-H1: ID-Felder werden nach Kürzung zusätzlich wertgescannt."""
    row = {"id": "task-ghp_1234567890abcdefghijklmnopqrstuv"}
    out = vs.sanitize_row(row, ("id",))
    # Kürzung auf 12 + Secret-Muster -> REDACTED (kein Secret im Snapshot).
    assert out["id"] != row["id"]
    assert "ghp_" not in (out["id"] or "")


def test_control_chars_stripped_by_publisher(fixture_db: Path) -> None:
    """R5-HIGH/M2: ALLE Kontrollzeichen (C0/C1 inkl. CR/LF/TAB) werden vom
    Publisher VOR dem Secret-Scan entfernt — konsistent mit dem Reader,
    der sie ablehnt. Kein verschleiertes Secret-Pattern entsteht."""
    assert vs.sanitize_display_value("ok\x00value") == "okvalue"
    assert vs.sanitize_display_value("a\x1fb") == "ab"
    # \r\n\t werden ebenfalls entfernt (Freitext-Normalisierung).
    assert "\n" not in vs.sanitize_display_value("line1\nline2")
    assert "\r" not in vs.sanitize_display_value("a\rb")
    assert "\t" not in vs.sanitize_display_value("a\tb")
    # Durch Kontrollzeichen getrennte Secret-Patterns werden NACH der
    # Entfernung erkannt und redigiert (R5-HIGH-Regression).
    assert vs.sanitize_display_value("binding_\x00hash") == vs.REDACTED
    assert vs.sanitize_display_value("/ho\x00me/private") == vs.REDACTED
    assert vs.sanitize_display_value("cred\x00ential=ABCDEFGH") == vs.REDACTED
    assert vs.sanitize_display_value("ghp_\x00secret123") == vs.REDACTED


def test_schema_version_max_32(fixture_db: Path) -> None:
    """R4-M2: Schema-Version >32 Zeichen wird verworfen."""
    assert vs.sanitize_schema_version("1" * 33) is None
    assert vs.sanitize_schema_version("1" * 32) == "1" * 32

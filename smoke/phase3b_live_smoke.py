#!/usr/bin/env python3
"""Phase 3B live smoke: exactly ONE harmless Telegram owner notification.

OWNER-APPROVED scope (one-time, Phase 3B only):
- uses the EXISTING credential source (env TELEGRAM_BOT_TOKEN +
  ~/.openclaw/credentials/telegram-default-allowFrom.json) at RUNTIME only
- sends exactly ONE notification through the Phase-3A outbox pipeline
- never prints, persists, or logs any secret value
- no inbound, no commands, no approvals, no gateway changes

Usage: python3 smoke/phase3b_live_smoke.py            # dry-run preflight
       python3 smoke/phase3b_live_smoke.py --send     # owner-approved live send
"""
import argparse
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from argent_core.core import Core
from argent_core.notifications import (
    NotificationDelivery,
    NotificationSecretSource,
    telegram_transport_factory,
    NOTIFICATION_REQUEST_TIMEOUT_SECONDS,
)
from argent_core.supervisor import Supervisor
from argent_core.trust import OWNER_SOURCE

CRED_DIR = pathlib.Path(os.path.expanduser("~/.openclaw/credentials"))
ALLOW_FROM = CRED_DIR / "telegram-default-allowFrom.json"


class _StubProvider:
    """Never consulted for enqueue-only usage; fails closed if it is."""

    def observe(self, lookup):
        raise AssertionError("stub provider must not be consulted")


class _StubLauncher:
    def spawn(self, **kwargs):
        raise AssertionError("stub launcher must not be consulted")


class EnvAllowFromSecretSource(NotificationSecretSource):
    """Runtime-only secret source: env token + allowFrom chat id.

    Deliberately minimal and local to this smoke. Reads values into memory,
    never writes them anywhere, never exposes them via attributes/__repr__.
    """

    def __init__(self):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = self._read_chat_id()
        if not token or not chat_id:
            raise RuntimeError("missing credential/target configuration")
        self._token = token
        self._chat_id = chat_id

    @staticmethod
    def _read_chat_id():
        if not ALLOW_FROM.exists():
            return None
        data = json.loads(ALLOW_FROM.read_text(encoding="utf-8"))
        entries = data.get("allowFrom") or []
        if len(entries) != 1:
            return None
        value = entries[0]
        if not isinstance(value, str) or not value.isdigit() or not (5 <= len(value) <= 20):
            return None
        return value

    def telegram_bot_token(self) -> str | None:
        return self._token

    def telegram_chat_id(self) -> str | None:
        return self._chat_id

    def __repr__(self) -> str:
        return "<EnvAllowFromSecretSource configured>"  # never leak values


def preflight() -> dict:
    """Read-only checks. Returns facts WITHOUT any secret values."""
    token_env = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    facts = {
        "allow_from_exists": ALLOW_FROM.exists(),
        "allow_from_entry_count": None,
        "chat_id_shape_ok": False,
        "token_env_present": bool(token_env),
        "token_env_length": len(token_env),
        "exactly_one_owner_chat": False,
    }
    if facts["allow_from_exists"]:
        data = json.loads(ALLOW_FROM.read_text(encoding="utf-8"))
        entries = data.get("allowFrom") or []
        facts["allow_from_entry_count"] = len(entries)
        if len(entries) == 1:
            v = entries[0]
            facts["chat_id_shape_ok"] = (
                isinstance(v, str) and v.isdigit() and (5 <= len(v) <= 20)
            )
            facts["exactly_one_owner_chat"] = facts["chat_id_shape_ok"]
    return facts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true",
                    help="actually send one live notification (owner-approved)")
    ap.add_argument("--db", default=None,
                    help="temp db path override (dry-run/tests ONLY; rejected with --send)")
    args = ap.parse_args()

    # FIX (Phase 3B review, MEDIUM): --send must use a FRESH temp DB so the
    # owner-approved "exactly one notification" boundary cannot be violated by
    # pre-existing rows in an externally supplied database.
    if args.send and args.db is not None:
        print("--send with --db is rejected: live send requires a fresh temp DB")
        return 2

    facts = preflight()
    print("PREFLIGHT:", json.dumps(facts, sort_keys=True))
    if not (facts["allow_from_exists"] and facts["allow_from_entry_count"] == 1
            and facts["chat_id_shape_ok"] and facts["token_env_present"]):
        print("PREFLIGHT FAILED - aborting (no secret values printed)")
        return 2

    db_path = args.db or str(
        pathlib.Path(tempfile.mkdtemp(prefix="phase3b-live-")) / "smoke.db")
    core = Core(db_path)
    project = core.create_project("phase3b-live-smoke", OWNER_SOURCE)
    task = core.create_task(project.id, "phase3b-live-smoke", OWNER_SOURCE)
    core.start_task_run(task.id, OWNER_SOURCE)
    sup = Supervisor(core, _StubProvider(), _StubLauncher())
    job_state = sup.store.create_job(
        task.id, idempotency_key=f"phase3b-live:{os.getpid()}")
    job = core._store.get_supervisor_job(job_state.supervisor_job_id)
    if job is None:
        print("job creation failed")
        return 2

    event_ref = f"supervisor:{job['id']}:close:DONE"
    enqueued = sup._enqueue_notification(
        job, notification_type="DONE", reason_code="TASK_DONE",
        event_ref=event_ref,
    )
    if not enqueued:
        print("ENQUEUE FAILED")
        return 2
    dedup_key = core._store._conn.execute(
        "SELECT dedup_key FROM notification_outbox WHERE supervisor_job_id=?",
        (job["id"],),
    ).fetchone()[0]
    print(f"ENQUEUED: dedup_key={dedup_key[:12]}... (no secret data)")

    # FIX (Phase 3B review, MEDIUM): before delivery, assert exactly ONE global
    # outbox row exists (the freshly created DB plus a single enqueue can never
    # produce more; an externally seeded DB is impossible here because --send
    # forces a fresh temp DB).
    global_before = core._store._conn.execute(
        "SELECT COUNT(*) FROM notification_outbox",
    ).fetchone()[0]
    if global_before != 1:
        print(f"PRE-DELIVERY CHECK FAILED: global outbox rows={global_before}")
        core.close()
        return 2

    if not args.send:
        print("DRY-RUN: no network used. Enqueue path exercised only.")
        core.close()
        return 0

    source = EnvAllowFromSecretSource()
    delivery = NotificationDelivery(
        db_path=db_path,
        transport_factory=telegram_transport_factory(
            source, timeout_seconds=NOTIFICATION_REQUEST_TIMEOUT_SECONDS,
        ),
    )
    result = delivery.send_due_once()
    print("SEND PASS: outcome=%s error_code=%s" % (
        result.outcome, result.error_code or "-"))
    if result.outcome not in ("SENT",):
        print("SEND FAILED - aborting")
        core.close()
        return 3

    # FIX (Phase 3B review, MEDIUM): assert the EXACT single global row is SENT
    # with attempt_count == 1 (not just any row of the current job).
    rows = core._store._conn.execute(
        "SELECT status, attempt_count FROM notification_outbox",
    ).fetchall()
    print("OUTBOX ROWS:", len(rows), "statuses:", [r[0] for r in rows],
          "attempts:", [r[1] for r in rows])
    if len(rows) != 1 or rows[0][0] != "SENT" or rows[0][1] != 1:
        print("VERIFICATION FAILED")
        core.close()
        return 4

    # FIX (Phase 3B review, MEDIUM): the second pass MUST be an unclaimed
    # NOT_DUE (no row was left pending, no duplicate was ever created).
    again = delivery.send_due_once()
    print("SECOND PASS: outcome=%s (must be NOT_DUE)" % again.outcome)
    if again.outcome != "NOT_DUE":
        print("SECOND PASS FAILED: expected NOT_DUE, got %s" % again.outcome)
        core.close()
        return 5
    count = core._store._conn.execute(
        "SELECT COUNT(*) FROM notification_outbox",
    ).fetchone()[0]
    if count != 1:
        print("DUPLICATE ROW DETECTED")
        core.close()
        return 5
    print("LIVE SEND VERIFIED: exactly one global SENT row (attempt_count=1), "
          "second pass NOT_DUE, no secrets printed, supervisor not blocked")
    core.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

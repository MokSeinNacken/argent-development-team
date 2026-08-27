"""Crash-recovery subprocess helper (SPEC V1 chapter 7, V1.1 11.6).

Opens the DB, commits an in-flight role run (status started), then begins an
uncommitted transaction on a separate connection and dies hard via
``os._exit(1)``.  The committed role run must survive; the uncommitted
transaction must be rolled back.
"""

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argent_core import Core, Role, role_source  # noqa: E402


def main() -> None:
    db_path, task_id = sys.argv[1], sys.argv[2]
    core = Core(db_path)
    # Committed in-flight role run (survives the crash).
    core.start_role(task_id, Role.LEAD, role_source(Role.LEAD))
    # Uncommitted transaction (separate connection) that must be rolled back.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE tasks SET state='REVIEWING', updated_at='crash' WHERE id=?",
        (task_id,),
    )
    os._exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Read-only G3-A checkpoint validator CLI (stdlib only; never prints secrets).

Usage:
  python3 g3-systemd/validate-checkpoint.py <checkpoint.json> [--live-state <json>]

Loads the checkpoint, runs :func:`validate_checkpoint`, prints one error per
line (or ``checkpoint valid (v1.0)``) and exits 0 (valid) / 2 (invalid,
fail-closed).  It NEVER prints file contents or secret values.

With ``--live-state <json>`` (a dict with the same keys as the "live" side of
``compare_persisted_state`` plus ``instance_rows`` under the key
``instance_rows``), it additionally prints the single-active + old-authority +
persisted-state comparisons.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent

# Load the single, self-contained validation module directly (bypasses the
# heavy ``argent_core`` package __init__, so this CLI stays dependency-free).
_SPEC = importlib.util.spec_from_file_location(
    "argent_g3_checkpoint", str(_REPO / "argent_core" / "g3_checkpoint.py")
)
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def _load_json(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv) -> int:
    if len(argv) < 2:
        print(
            "usage: validate-checkpoint.py <checkpoint.json> [--live-state <json>]",
            file=sys.stderr,
        )
        return 2

    path = Path(argv[1])
    try:
        checkpoint = _load_json(path)
    except (OSError, ValueError) as exc:
        print(f"cannot load checkpoint {path}: {exc}", file=sys.stderr)
        return 2

    errors = _MOD.validate_checkpoint(checkpoint)
    if errors:
        for e in errors:
            print(e)
        return 2
    print(f"checkpoint valid (v{_MOD.CHECKPOINT_VERSION})")

    live = None
    if "--live-state" in argv:
        idx = argv.index("--live-state")
        if idx + 1 >= len(argv):
            print("--live-state requires a path", file=sys.stderr)
            return 2
        try:
            live = _load_json(Path(argv[idx + 1]))
        except (OSError, ValueError) as exc:
            print(f"cannot load live-state: {exc}", file=sys.stderr)
            return 2

    if live is not None:
        ok, reason = _MOD.single_active_supervisor(live.get("instance_rows"))
        print(f"single_active_supervisor: {ok} ({reason})")

        live_row = {}
        rows = live.get("instance_rows")
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict) and r.get("status") == "ACTIVE":
                    live_row = r
                    break
        still_live = _MOD.is_old_authority_still_live(checkpoint, live_row)
        print(f"old_authority_still_live: {still_live}")

        deltas = _MOD.compare_persisted_state(checkpoint, live)
        if deltas:
            for d in deltas:
                print(f"persisted_state_delta: {d}")
        else:
            print("persisted_state: identical")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

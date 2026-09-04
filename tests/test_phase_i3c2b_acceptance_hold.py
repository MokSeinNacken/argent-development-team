"""Phase I3-C2-B — acceptance-only bounded CI hold (DO-NOT-MERGE artifact).

This module exists ONLY on the I3-C2-B acceptance branch (PR #3) and must
NEVER be merged to main.  Its single purpose is to hold the real
PR-triggered GitHub Actions job for a bounded ~90 s window so that the live
I3-C2-B acceptance can persist a durable WAITING_EXTERNAL job + external_waits
row and perform the authorized supervisor restart BEFORE the real CI
completes.  It then PASSES.

Properties (acceptance brief §2):
- deterministic (bounded wall-clock sleep, always passes)
- no network, no secrets, no external write
- no product-code mutation, no weakening of any existing test
- no modification to production security semantics
- the normal portable CI test set still executes alongside this hold
"""

import time

HOLD_SECONDS = 90.0


def test_i3c2b_acceptance_hold_bounded():
    """Hold the CI job for the bounded acceptance window, then pass."""
    time.sleep(HOLD_SECONDS)
    assert True  # bounded hold completed; acceptance window provided

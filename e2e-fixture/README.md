# e2e-fixture

Minimal stdlib-only test project for the Argent Phase 2B end-to-end run.

Files:

- `parser.py` — duration text parser (stub; the implementer agent fills it in)
- `service.py` — duration service helpers (stub)
- `tests/` — pytest tests (empty; the qa/implementer agent fills it in)

No network access and no third-party dependencies are used.  The actual
functions are implemented by the implementer agent during the E2E run and are
verified by the bwrap test runner (SPEC V2B §3).

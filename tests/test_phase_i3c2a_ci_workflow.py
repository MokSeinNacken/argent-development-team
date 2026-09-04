"""Phase I3-C2-A — deterministic security validation of the CI workflow file.

This module is the single deterministic, stdlib-only source of truth for the
Phase I3-C2-A GitHub Actions CI bootstrap.  It reads and structurally validates
``.github/workflows/ci.yml`` so that the workflow that will produce the GitHub
check for pull requests into ``main`` can NEVER silently drift into a broader,
write-capable, secret-carrying, or fail-open CI.

Workflow / check identity (the source of truth the cases assert against):

- workflow ``name``:            ``Argent CI``
- trigger:                     ``on: pull_request`` restricted to ``branches: [main]``
- top-level ``permissions``:    ``contents: read`` (read-only, nothing else)
- single job ``id``:           ``test`` (no ``name:`` display-name override)
- authoritative test command:  ``python -m pytest tests/ -q -m "not host_acceptance"``
                               (portable, fail-closed; excludes the
                               ``host_acceptance``-marked operational tests)
- ``timeout-minutes``:         ``15``
- pinned Python:               ``'3.14'``
- pinned pytest:               ``pytest==9.1.1``
- trusted actions (exact set): ``actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1``
                               (v7.0.1) and
                               ``actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97``
                               (v7.0.0) — immutable full-commit SHAs (LOW-3)
- canonical step set:          4 steps (checkout / setup-python /
                               pip-install / portable pytest), asserted EXACTLY
                               (no extra, no missing, no reorder) — CASE 18
- checkout token boundary:     ``persist-credentials: false`` (CASE 19); the
                               ephemeral read-only token is used ONLY to clone
                               and is NOT persisted — no later step or script
                               receives any token (no env: GITHUB_TOKEN, no
                               secrets) — HIGH-1

Because the single job id is ``test`` with no display-name override, the GitHub
check-run name for this workflow is exactly ``test``.  Phase I3-C2-B waits on
that exact required-check identity, so any change to the workflow name or job id
must fail these tests loudly and force a coordinated update of the docs.

The parser is a small deterministic line/indent-based structural reader (no
PyYAML, no network, no secrets).  It is CANONICAL: it parses the job's steps as
an exact ordered list (names + uses/with/run content), tolerating full-line
``#`` comments and ``run: |`` block scalars, and it asserts the exact step set
plus per-step/job/top-level key allowlists (HIGH-2).  Denylists are asserted as
strict text checks against the whole file or the parsed job/step blocks.

Case -> test function mapping (19 cases):

- CASE 1  pull_request trigger restricted to branches:[main]  -> test_case1_pull_request_trigger_restricted_to_main
- CASE 2  no pull_request_target                             -> test_case2_no_pull_request_target
- CASE 3  no secrets referenced                              -> test_case3_no_secrets_referenced
- CASE 4  permissions read-only (contents: read only)        -> test_case4_permissions_read_only_contents_only
- CASE 5  no deploy/release/publish/artifact capability      -> test_case5_no_deploy_release_publish
- CASE 6  no external write token / credentials              -> test_case6_no_external_write_token_or_credentials
- CASE 7  authoritative test command fail-closed             -> test_case7_test_command_fail_closed
- CASE 8  no continue-on-error                               -> test_case8_no_continue_on_error
- CASE 9  no ${{ in any run block (PR title/body/head/labels) -> test_case9_no_run_shell_interpolation_from_pr
- CASE 10 no branch/ref/event in run (no ${{ in run line)    -> test_case10_no_branch_ref_shell_command
- CASE 11 timeout bounded (5 <= value <= 30)                 -> test_case11_timeout_bounded
- CASE 12 only trusted actions (exact set, SHA-pinned)       -> test_case12_only_trusted_actions
- CASE 13 required-check identity (name/job/CHECK_NAME)      -> test_case13_required_check_identity
- CASE 14 workflow path exactly .github/workflows/ci.yml     -> test_case14_workflow_path_exact
- CASE 15 PR content cannot become command authority         -> test_case15_pr_content_cannot_become_command_authority
- CASE 16 no schedule/workflow_dispatch/repository_dispatch  -> test_case16_no_other_triggers
- CASE 17 no env block / no GITHUB_TOKEN / no env injection  -> test_case17_no_env_or_github_token
- CASE 18 exact canonical step set + structural key allowlists -> test_case18_exact_canonical_step_set
- CASE 19 checkout persist-credentials: false                -> test_case19_checkout_persist_credentials_false
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW_FILE = WORKFLOW_DIR / "ci.yml"

#: Documented, stable workflow / check identity constants.
WORKFLOW_NAME = "Argent CI"
JOB_ID = "test"
CHECK_NAME = "test"
TEST_COMMAND = 'python -m pytest tests/ -q -m "not host_acceptance"'
TIMEOUT_MIN = 5
TIMEOUT_MAX = 30
# LOW-3: immutable full-commit SHA pins (resolved 2026-09-04 via the GitHub API).
# Human-readable release tags (for review) are kept as inline comments in the
# workflow, NOT as the mutable ref the runner resolves.
CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"  # v7.0.1
SETUP_PYTHON_ACTION = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"  # v7.0.0
TRUSTED_ACTIONS = (CHECKOUT_ACTION, SETUP_PYTHON_ACTION)

#: Canonical step set (exact ordered content the workflow must equal).  Full-line
#: ``#`` comments in the steps list are tolerated (they are data, not steps) and
#: are NOT part of these dicts.
CANONICAL_STEPS = [
    {
        "name": "Check out repository",
        "uses": CHECKOUT_ACTION,
        "with": {"persist-credentials": "false"},
    },
    {
        "name": "Set up Python",
        "uses": SETUP_PYTHON_ACTION,
        "with": {"python-version": "'3.14'"},
    },
    {
        "name": "Install test dependency",
        "run": "python -m pip install --quiet --disable-pip-version-check pytest==9.1.1",
    },
    {
        "name": "Run portable deterministic test suite",
        "run": TEST_COMMAND,
    },
]

#: Canonical run commands (the COMPLETE set, in order) the workflow executes.
CANONICAL_RUN_COMMANDS = [s["run"] for s in CANONICAL_STEPS if "run" in s]


# ---------------------------------------------------------------------------
# Deterministic structural reader (stdlib only, no PyYAML)
# ---------------------------------------------------------------------------

def _text() -> str:
    return WORKFLOW_FILE.read_text(encoding="utf-8")


def _read_lines() -> list[str]:
    return _text().splitlines()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find(lines: list[str], indent: int, key: str):
    """Return the index of the first mapping key ``key`` at ``indent``."""
    for i, ln in enumerate(lines):
        if _indent(ln) == indent:
            name = ln.strip().split(":", 1)[0].strip()
            if name == key:
                return i
    return None


def _sub(lines: list[str], idx: int) -> list[str]:
    """Return the contiguous sub-block indented strictly deeper than line idx."""
    base = _indent(lines[idx])
    out = []
    for ln in lines[idx + 1:]:
        if ln.strip() == "":
            out.append(ln)
            continue
        if _indent(ln) <= base:
            break
        out.append(ln)
    return out


def _on_keys() -> list[str]:
    lines = _read_lines()
    on_block = _sub(lines, _find(lines, 0, "on"))
    return [
        ln.strip().split(":", 1)[0].strip()
        for ln in on_block
        if _indent(ln) == 2 and ":" in ln and not ln.strip().startswith("-")
    ]


def _pull_request_branches() -> list[str]:
    lines = _read_lines()
    on_block = _sub(lines, _find(lines, 0, "on"))
    pr_block = _sub(on_block, _find(on_block, 2, "pull_request"))
    branches_block = _sub(pr_block, _find(pr_block, 4, "branches"))
    return [
        ln.strip().lstrip("-").strip()
        for ln in branches_block
        if ln.strip().startswith("-")
    ]


def _test_block() -> list[str]:
    lines = _read_lines()
    jobs_block = _sub(lines, _find(lines, 0, "jobs"))
    return _sub(jobs_block, _find(jobs_block, 2, JOB_ID))


def _steps_block() -> list[str]:
    test_block = _test_block()
    return _sub(test_block, _find(test_block, 4, "steps"))


def _parse_steps() -> list[dict]:
    """Parse the ``test`` job's steps into an exact ordered list of step dicts.

    Each step dict maps step keys to values among {name, uses, run, with};
    ``with`` maps to a dict of its entries (string -> string).  Full-line ``#``
    comments and blank lines are ignored (a comment inside the steps list is NOT
    a step).  Inline ``#`` comments are stripped from ``uses:`` refs.  A block
    scalar ``run: |`` is captured as its joined indented body (trailing blank
    lines trimmed), so a multi-line script body can never hide commands from the
    validator.
    """
    steps: list[dict] = []
    cur: dict | None = None
    cur_with: dict | None = None
    run_body: list[str] | None = None

    def close_step() -> None:
        nonlocal cur, cur_with, run_body
        if run_body is not None:
            while run_body and run_body[-1] == "":
                run_body.pop()
            if cur is not None:
                cur["run"] = "\n".join(run_body)
            run_body = None
        if cur is not None:
            steps.append(cur)
        cur = None
        cur_with = None

    def set_entry(text: str) -> None:
        nonlocal cur_with, run_body
        key, _, val = text.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "with":
            cur_with = {}
            cur["with"] = cur_with
        elif key == "uses":
            # strip inline comment (# v7.0.1) so the immutable SHA is exact
            cur["uses"] = val.split("#", 1)[0].strip()
        elif key == "run" and val == "|":
            run_body = []
        elif key == "run":
            cur["run"] = val
        else:
            cur[key] = val

    for ln in _steps_block():
        stripped = ln.strip()
        indent = _indent(ln)
        if stripped == "":
            if run_body is not None:
                run_body.append("")
            continue
        if stripped.startswith("#"):
            # Full-line comment: not part of any step or with-entry.
            continue
        if indent == 6 and stripped.startswith("- "):
            close_step()
            cur = {}
            set_entry(stripped[2:].strip())
            continue
        if run_body is not None:
            run_body.append(stripped)
            continue
        if cur is not None and indent >= 10 and cur_with is not None:
            k, _, v = stripped.partition(":")
            cur_with[k.strip()] = v.strip()
            continue
        if cur is not None and indent >= 8:
            set_entry(stripped)
            continue
    close_step()
    return steps


def _uses_refs() -> list[str]:
    return [s["uses"] for s in _parse_steps() if "uses" in s]


def _run_commands() -> list[str]:
    return [s["run"] for s in _parse_steps() if "run" in s]


def _test_step() -> dict:
    for step in _parse_steps():
        if "run" in step and "python -m pytest" in step["run"]:
            return step
    raise AssertionError("authoritative pytest step not found in workflow")


def _timeout_minutes():
    test_block = _test_block()
    idx = _find(test_block, 4, "timeout-minutes")
    if idx is None:
        return None
    return int(test_block[idx].strip().split(":", 1)[1].strip())


def _comment_free_text() -> str:
    """Return the workflow text with ``#`` inline comments removed.

    Used by the capability/token denylist checks (CASE 5/6) so that explanatory
    comments (which are data, not workflow behaviour) cannot trip a literal
    ``release``/``token`` word check.  Only safe for THIS controlled file (no
    ``#`` inside a quoted value); a ``#`` inside a double-quoted string is not a
    YAML comment anyway.  The check remains strict: the denylist tokens are
    still rejected anywhere in the executable (non-comment) content.
    """
    return "\n".join(ln.split("#", 1)[0] for ln in _text().splitlines())


def _top_level_keys() -> list[str]:
    lines = _read_lines()
    return [
        ln.strip().split(":", 1)[0].strip()
        for ln in lines
        if _indent(ln) == 0 and ":" in ln and not ln.strip().startswith("-")
    ]


def _job_keys() -> list[str]:
    return [
        ln.strip().split(":", 1)[0].strip()
        for ln in _test_block()
        if _indent(ln) == 4 and ":" in ln and not ln.strip().startswith("-")
    ]


def _step_keys() -> set[str]:
    keys: set[str] = set()
    for step in _parse_steps():
        keys.update(step.keys())
    return keys


# ---------------------------------------------------------------------------
# CASE 1 — pull_request trigger restricted to branches: [main]
# ---------------------------------------------------------------------------

def test_case1_pull_request_trigger_restricted_to_main():
    assert _on_keys() == ["pull_request"]
    assert _pull_request_branches() == ["main"]


# ---------------------------------------------------------------------------
# CASE 2 — no pull_request_target
# ---------------------------------------------------------------------------

def test_case2_no_pull_request_target():
    assert "pull_request_target" not in _text()


# ---------------------------------------------------------------------------
# CASE 3 — no secrets referenced anywhere
# ---------------------------------------------------------------------------

def test_case3_no_secrets_referenced():
    text = _text()
    assert "secrets:" not in text
    assert "${{ secrets." not in text
    assert "secrets." not in text
    # Catch-all: the token "secrets" never appears at all.
    assert "secrets" not in text


# ---------------------------------------------------------------------------
# CASE 4 — top-level permissions read-only (contents: read only)
# ---------------------------------------------------------------------------

_WRITE_SCOPES = (
    "contents: write",
    "pull-requests: write",
    "actions: write",
    "checks: write",
    "statuses: write",
    "issues: write",
    "packages: write",
    "deployments: write",
    "id-token: write",
)


def test_case4_permissions_read_only_contents_only():
    text = _text()
    lines = _read_lines()
    perm_block = _sub(lines, _find(lines, 0, "permissions"))
    entries = [ln.strip() for ln in perm_block if ln.strip()]
    assert entries == ["contents: read"]
    # Nothing else may be granted — no write scope anywhere.
    for scope in _WRITE_SCOPES:
        assert scope not in text, f"write scope {scope!r} present"
    # Exactly one (top-level) permissions block; none inside jobs.
    assert text.count("permissions:") == 1
    assert "permissions" not in "".join(_test_block())


# ---------------------------------------------------------------------------
# CASE 5 — no deployment / release / publish capability
# ---------------------------------------------------------------------------

def test_case5_no_deploy_release_publish():
    # Checked against comment-free content: a capability can only come from a
    # key/value/command, never from an explanatory comment.
    t = _comment_free_text().lower()
    for token in ("deploy", "release", "publish", "gh release",
                  "actions/deploy", "upload-artifact", "download-artifact"):
        assert token not in t, f"{token!r} present"


# ---------------------------------------------------------------------------
# CASE 6 — no external write token / credentials
# ---------------------------------------------------------------------------

def test_case6_no_external_write_token_or_credentials():
    t = _comment_free_text().lower()
    assert "github_token" not in t
    assert "token" not in t


# ---------------------------------------------------------------------------
# CASE 7 — authoritative test command fail-closed
# ---------------------------------------------------------------------------

def test_case7_test_command_fail_closed():
    run_cmds = _run_commands()
    pytest_cmds = [c for c in run_cmds if "python -m pytest" in c]
    assert pytest_cmds == [TEST_COMMAND]
    step = _test_step()
    # No shell-ignore mechanisms may mask the authoritative test step.
    assert "|| true" not in step["run"]
    assert "set +e" not in step["run"]
    assert "continue-on-error" not in step
    assert "if" not in step
    assert not TEST_COMMAND.lstrip().startswith("-")


# ---------------------------------------------------------------------------
# CASE 8 — no continue-on-error anywhere
# ---------------------------------------------------------------------------

def test_case8_no_continue_on_error():
    assert "continue-on-error" not in _text()


# ---------------------------------------------------------------------------
# CASE 9 — no ${{ shell interpolation from PR title/body/head/labels
# ---------------------------------------------------------------------------

_PR_TEXT_SOURCES = (
    "github.event.pull_request.title",
    "github.event.pull_request.body",
    "github.event.pull_request.head",
    "github.event.pull_request.labels",
)


def test_case9_no_run_shell_interpolation_from_pr():
    text = _text()
    for cmd in _run_commands():
        assert "${{" not in cmd
    for token in _PR_TEXT_SOURCES:
        assert token not in text


# ---------------------------------------------------------------------------
# CASE 10 — no branch/ref/event input can become a shell command
# ---------------------------------------------------------------------------

_BRANCH_REF_TOKENS = (
    "github.head_ref",
    "github.ref",
    "github.event.",
)


def test_case10_no_branch_ref_shell_command():
    text = _text()
    # Strongest guarantee: no run: command contains any expression at all.
    for cmd in _run_commands():
        assert "${{" not in cmd
    for token in _BRANCH_REF_TOKENS:
        assert token not in text


# ---------------------------------------------------------------------------
# CASE 11 — timeout exists and is bounded (5 <= value <= 30)
# ---------------------------------------------------------------------------

def test_case11_timeout_bounded():
    timeout = _timeout_minutes()
    assert timeout is not None, "job `test` has no timeout-minutes"
    assert TIMEOUT_MIN <= timeout <= TIMEOUT_MAX


# ---------------------------------------------------------------------------
# CASE 12 — only trusted actions (exact set, SHA-pinned)
# ---------------------------------------------------------------------------

def test_case12_only_trusted_actions():
    refs = _uses_refs()
    assert sorted(refs) == sorted(TRUSTED_ACTIONS)
    assert len(refs) == len(set(refs)) == 2
    # LOW-3: every trusted ref is an immutable full-commit SHA, never a mutable
    # tag/branch (a mutable ref would let a retagged release change behaviour).
    for ref in refs:
        sha = ref.rsplit("@", 1)[1]
        assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), ref


# ---------------------------------------------------------------------------
# CASE 13 — required-check identity deterministic
# ---------------------------------------------------------------------------

def test_case13_required_check_identity():
    lines = _read_lines()
    name_idx = _find(lines, 0, "name")
    assert lines[name_idx].strip().split(":", 1)[1].strip() == WORKFLOW_NAME
    jobs_block = _sub(lines, _find(lines, 0, "jobs"))
    job_ids = [
        ln.strip().split(":", 1)[0].strip()
        for ln in jobs_block
        if _indent(ln) == 2 and ":" in ln
    ]
    assert job_ids == [JOB_ID]
    job_keys = [
        ln.strip().split(":", 1)[0].strip()
        for ln in _test_block()
        if _indent(ln) == 4 and ":" in ln and not ln.strip().startswith("-")
    ]
    assert "name" not in job_keys  # no display-name override
    assert CHECK_NAME == JOB_ID == "test"


# ---------------------------------------------------------------------------
# CASE 14 — workflow path exactly .github/workflows/ci.yml
# ---------------------------------------------------------------------------

def test_case14_workflow_path_exact():
    assert WORKFLOW_DIR.is_dir()
    yamls = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    assert yamls == [WORKFLOW_FILE]
    assert [p for p in WORKFLOW_DIR.iterdir() if p.is_dir()] == []


# ---------------------------------------------------------------------------
# CASE 15 — PR content cannot become command authority
# ---------------------------------------------------------------------------

def test_case15_pr_content_cannot_become_command_authority():
    text = _text()
    # No provider-supplied string may ever be evaluated/executed/shell-invoked.
    for cmd in _run_commands():
        for token in ("eval", "exec", "shell:"):
            assert token not in cmd, f"{token!r} in run command {cmd!r}"
    assert "github.event" not in text
    assert "eval(" not in text
    assert "exec(" not in text


# ---------------------------------------------------------------------------
# CASE 16 — no schedule / workflow_dispatch / repository_dispatch triggers
# ---------------------------------------------------------------------------

def test_case16_no_other_triggers():
    assert _on_keys() == ["pull_request"]
    t = _text().lower()
    for token in ("schedule", "workflow_dispatch", "repository_dispatch", "push:"):
        assert token not in t, f"{token!r} present"


# ---------------------------------------------------------------------------
# CASE 17 — no env block / no GITHUB_TOKEN / no environment injection
# ---------------------------------------------------------------------------

def test_case17_no_env_or_github_token():
    text = _text()
    for line in _read_lines():
        s = line.strip()
        assert not s.startswith("env:"), f"env block present: {line!r}"
    assert "GITHUB_TOKEN" not in text
    assert "environment" not in text.lower()


# ---------------------------------------------------------------------------
# CASE 18 — exact canonical step set + structural key allowlists
# ---------------------------------------------------------------------------

def test_case18_exact_canonical_step_set():
    steps = _parse_steps()
    # Exact ordered list: no extra, no missing, no reorder (names + content,
    # incl. with: entries and complete run: commands).
    assert steps == CANONICAL_STEPS
    # Complete run: command set equals exactly the canonical commands.
    assert _run_commands() == CANONICAL_RUN_COMMANDS
    # Structural key allowlists (no hidden capability keys anywhere).
    assert _top_level_keys() == ["name", "on", "permissions", "jobs"]
    assert set(_job_keys()) == {"runs-on", "timeout-minutes", "steps"}
    assert _step_keys() <= {"name", "uses", "with", "run"}


# ---------------------------------------------------------------------------
# CASE 19 — checkout persist-credentials: false
# ---------------------------------------------------------------------------

def test_case19_checkout_persist_credentials_false():
    steps = _parse_steps()
    checkout = steps[0]
    assert checkout["name"] == "Check out repository"
    assert checkout["uses"] == CHECKOUT_ACTION
    # HIGH-1: the ephemeral read-only token must NOT be persisted into
    # .git/config; later steps run token-less (no step receives a token).
    assert checkout["with"] == {"persist-credentials": "false"}

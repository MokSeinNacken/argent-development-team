"""Phase I3-C2-A — deterministic security validation of the CI workflow file.

This module is the single deterministic, stdlib-only source of truth for the
Phase I3-C2-A GitHub Actions CI bootstrap.  It reads and structurally validates
``.github/workflows/ci.yml`` so that the workflow that will produce the GitHub
check for pull requests into ``main`` can NEVER silently drift into a broader,
write-capable, secret-carrying, or fail-open CI.

Workflow / check identity (the source of truth CASE 13 asserts against):

- workflow ``name``:            ``Argent CI``
- trigger:                     ``on: pull_request`` restricted to ``branches: [main]``
- top-level ``permissions``:    ``contents: read`` (read-only, nothing else)
- single job ``id``:           ``test`` (no ``name:`` display-name override)
- authoritative test command:  ``python -m pytest tests/ -q`` (fail-closed)
- ``timeout-minutes``:         ``15``
- pinned Python:               ``'3.14'``
- pinned pytest:               ``pytest==9.1.1``
- trusted actions (exact set): ``actions/checkout@v7.0.1``,
  ``actions/setup-python@v7.0.0``
- required check name:         ``test`` (constant ``CHECK_NAME``)

Because the single job id is ``test`` with no display-name override, the GitHub
check-run name for this workflow is exactly ``test``.  Phase I3-C2-B waits on
that exact required-check identity, so any change to the workflow name or job id
must fail these tests loudly and force a coordinated update of the docs.

The parser is a small deterministic line/indent-based structural reader (no
PyYAML, no network, no secrets); denylists are asserted as strict text checks
against the whole file or the parsed job/step blocks.

Case -> test function mapping (17 cases):

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
- CASE 12 only trusted actions (exact set)                   -> test_case12_only_trusted_actions
- CASE 13 required-check identity (name/job/CHECK_NAME)      -> test_case13_required_check_identity
- CASE 14 workflow path exactly .github/workflows/ci.yml     -> test_case14_workflow_path_exact
- CASE 15 PR content cannot become command authority         -> test_case15_pr_content_cannot_become_command_authority
- CASE 16 no schedule/workflow_dispatch/repository_dispatch  -> test_case16_no_other_triggers
- CASE 17 no env block / no GITHUB_TOKEN / no env injection  -> test_case17_no_env_or_github_token
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
TEST_COMMAND = "python -m pytest tests/ -q"
TIMEOUT_MIN = 5
TIMEOUT_MAX = 30
CHECKOUT_ACTION = "actions/checkout@v7.0.1"
SETUP_PYTHON_ACTION = "actions/setup-python@v7.0.0"
TRUSTED_ACTIONS = (CHECKOUT_ACTION, SETUP_PYTHON_ACTION)


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


def _job_steps() -> list[str]:
    """Return one joined string per step of the ``test`` job."""
    test_block = _test_block()
    steps_block = _sub(test_block, _find(test_block, 4, "steps"))
    steps = []
    current = []
    for ln in steps_block:
        if ln.strip() == "":
            continue
        if _indent(ln) == 6 and ln.strip().startswith("- "):
            if current:
                steps.append("\n".join(current))
            current = [ln]
        else:
            current.append(ln)
    if current:
        steps.append("\n".join(current))
    return steps


def _uses_refs() -> list[str]:
    refs = []
    for step in _job_steps():
        for line in step.splitlines():
            s = line.strip()
            if s.startswith("- "):
                s = s[2:].strip()
            if s.startswith("uses:"):
                refs.append(s[len("uses:"):].strip())
    return refs


def _run_commands() -> list[str]:
    cmds = []
    for step in _job_steps():
        for line in step.splitlines():
            s = line.strip()
            if s.startswith("run:"):
                cmds.append(s[len("run:"):].strip())
    return cmds


def _test_step() -> str:
    for step in _job_steps():
        if "python -m pytest" in step:
            return step
    raise AssertionError("authoritative pytest step not found in workflow")


def _timeout_minutes():
    test_block = _test_block()
    idx = _find(test_block, 4, "timeout-minutes")
    if idx is None:
        return None
    return int(test_block[idx].strip().split(":", 1)[1].strip())


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
    t = _text().lower()
    for token in ("deploy", "release", "publish", "gh release",
                  "actions/deploy", "upload-artifact", "download-artifact"):
        assert token not in t, f"{token!r} present"


# ---------------------------------------------------------------------------
# CASE 6 — no external write token / credentials
# ---------------------------------------------------------------------------

def test_case6_no_external_write_token_or_credentials():
    t = _text().lower()
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
    assert "|| true" not in step
    assert "set +e" not in step
    assert "continue-on-error" not in step
    assert "if:" not in step
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
    # Strongest guarantee: no run: line contains any expression at all.
    for cmd in _run_commands():
        assert "${{" not in cmd
    for token in _BRANCH_REF_TOKENS:
        assert token not in text
    # No run line may contain an expression marker either.
    for step in _job_steps():
        for line in step.splitlines():
            if line.strip().startswith("run:"):
                assert "${{" not in line


# ---------------------------------------------------------------------------
# CASE 11 — timeout exists and is bounded (5 <= value <= 30)
# ---------------------------------------------------------------------------

def test_case11_timeout_bounded():
    timeout = _timeout_minutes()
    assert timeout is not None, "job `test` has no timeout-minutes"
    assert TIMEOUT_MIN <= timeout <= TIMEOUT_MAX


# ---------------------------------------------------------------------------
# CASE 12 — only trusted actions (exact set)
# ---------------------------------------------------------------------------

def test_case12_only_trusted_actions():
    refs = _uses_refs()
    assert sorted(refs) == sorted(TRUSTED_ACTIONS)
    assert len(refs) == len(set(refs)) == 2


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

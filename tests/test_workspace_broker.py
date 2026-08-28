"""Write-Broker tests (SPEC V2B §2.1–§2.10 + role scope).

Each hardening rule from SPEC V2B §2 has at least one dedicated test.  The
broker applies patch sets atomically and never executes shell/eval/exec.
"""

import base64
import os
import stat

import pytest

from argent_core import (
    BrokerResult,
    PermissionDenied,
    Role,
    WorkspaceBroker,
    broker,
)

from conftest import LEAD, IMPLEMENTER, QA, ANALYST, REVIEWER


def _w(path, text):
    """Build a write patch with base64-encoded content."""
    return {
        "op": "write",
        "path": path,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


@pytest.fixture
def scope(tmp_path):
    return tmp_path


@pytest.fixture
def b():
    return WorkspaceBroker()


# ------------------------------------------------------------- §2.1 absolute


def test_absolute_path_escape_rejected(b, scope):
    res = b.apply_patch_set(
        scope, [_w("/etc/passwd", "pwned")], Role.IMPLEMENTER, LEAD
    )
    assert res.applied == []
    assert res.errors[0]["error"] == "absolute_path"
    assert not (scope / "etc").exists()


# ------------------------------------------------------------------ §2.2 ..


def test_dotdot_escape_rejected(b, scope):
    res = b.apply_patch_set(
        scope, [_w("../outside.txt", "x")], Role.IMPLEMENTER, LEAD
    )
    assert res.applied == []
    assert res.errors[0]["error"] == "scope_denied"


def test_dotdot_nested_escape_rejected(b, scope):
    (scope / "sub").mkdir()
    res = b.apply_patch_set(
        scope, [_w("sub/../../outside.txt", "x")], Role.IMPLEMENTER, LEAD
    )
    assert res.applied == []
    assert res.errors[0]["error"] == "scope_denied"


# ------------------------------------------------------------- §2.3 symlink


def test_symlink_target_escape_rejected(b, scope):
    outside = scope.parent / "outside.txt"
    outside.write_text("secret outside data")
    try:
        os.symlink(outside, scope / "ln")
        res = b.apply_patch_set(scope, [_w("ln", "new")], Role.IMPLEMENTER, LEAD)
        assert res.applied == []
        assert res.errors[0]["error"] in ("scope_denied", "symlink_target")
        assert outside.read_text() == "secret outside data"
    finally:
        if os.path.lexists(scope / "ln"):
            os.unlink(scope / "ln")
        if outside.exists():
            outside.unlink()


def test_symlink_intermediate_dir_escape_rejected(b, scope):
    outside_dir = scope.parent / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "target.txt").write_text("x")
    try:
        os.symlink(outside_dir, scope / "dirlink")
        res = b.apply_patch_set(
            scope, [_w("dirlink/target.txt", "new")], Role.IMPLEMENTER, LEAD
        )
        assert res.applied == []
        assert res.errors[0]["error"] == "scope_denied"
        assert (outside_dir / "target.txt").read_text() == "x"
    finally:
        if os.path.lexists(scope / "dirlink"):
            os.unlink(scope / "dirlink")
        (outside_dir / "target.txt").unlink()
        outside_dir.rmdir()


def test_symlink_at_target_inside_scope_rejected(b, scope):
    (scope / "real.txt").write_text("real")
    os.symlink("real.txt", scope / "ln")
    res = b.apply_patch_set(scope, [_w("ln", "new")], Role.IMPLEMENTER, LEAD)
    assert res.applied == []
    assert res.errors[0]["error"] == "symlink_target"
    assert (scope / "real.txt").read_text() == "real"


# -------------------------------------------------------------- §2.4 hardlink


def test_hardlink_write_breaks_link(b, scope):
    (scope / "a.txt").write_text("old")
    os.link(scope / "a.txt", scope / "b.txt")
    res = b.apply_patch_set(scope, [_w("a.txt", "new")], Role.IMPLEMENTER, LEAD)
    assert res.applied == [{"op": "write", "path": "a.txt"}]
    assert (scope / "a.txt").read_text() == "new"
    assert (scope / "b.txt").read_text() == "old"  # other link untouched
    assert os.lstat(scope / "a.txt").st_nlink == 1  # §2.4


# ------------------------------------------------------- §2.5 special files


def test_special_file_fifo_rejected(b, scope):
    os.mkfifo(scope / "pipe")
    res = b.apply_patch_set(scope, [_w("pipe", "data")], Role.IMPLEMENTER, LEAD)
    assert res.applied == []
    assert res.errors[0]["error"] == "special_file"
    assert stat.S_ISFIFO(os.lstat(scope / "pipe").st_mode)  # unchanged


def test_special_file_setuid_rejected(b, scope):
    p = scope / "suid"
    p.write_text("x")
    os.chmod(p, 0o4755)
    res = b.apply_patch_set(scope, [_w("suid", "new")], Role.IMPLEMENTER, LEAD)
    assert res.applied == []
    assert res.errors[0]["error"] == "setuid_setgid"
    assert p.read_text() == "x"


# --------------------------------------------------------------- §2.6 scope


def test_scope_escape_rejected(b, scope):
    res = b.apply_patch_set(scope, [_w("../x.txt", "x")], Role.IMPLEMENTER, LEAD)
    assert res.errors[0]["error"] == "scope_denied"


def test_delete_only_regular_files(b, scope):
    (scope / "real.txt").write_text("real")
    os.symlink("real.txt", scope / "ln")
    os.mkfifo(scope / "fifo")
    res = b.apply_patch_set(
        scope,
        [
            {"op": "delete", "path": "ln"},
            {"op": "delete", "path": "fifo"},
        ],
        Role.IMPLEMENTER,
        LEAD,
    )
    assert res.applied == []
    assert sorted(e["error"] for e in res.errors) == [
        "not_regular_file",
        "symlink_target",
    ]
    assert (scope / "real.txt").exists()


def test_delete_missing_is_skipped(b, scope):
    res = b.apply_patch_set(
        scope, [{"op": "delete", "path": "missing.txt"}], Role.IMPLEMENTER, LEAD
    )
    assert res.applied == []
    assert res.errors == []
    assert len(res.skipped) == 1


# -------------------------------------------------------- §2.7 deny paths


def test_deny_path_system_config(b, scope):
    os.symlink("/etc", scope / "etc_link")
    try:
        res = b.apply_patch_set(
            scope, [_w("etc_link/hostname", "x")], Role.IMPLEMENTER, LEAD
        )
        assert res.applied == []
        assert res.errors[0]["error"] == "deny_path"
    finally:
        os.unlink(scope / "etc_link")


def test_deny_path_home_ssh(b, scope):
    home_ssh = os.path.join(os.path.expanduser("~"), ".ssh")
    os.symlink(home_ssh, scope / "ssh_link")
    try:
        res = b.apply_patch_set(
            scope, [_w("ssh_link/authorized_keys", "x")], Role.IMPLEMENTER, LEAD
        )
        assert res.applied == []
        assert res.errors[0]["error"] == "deny_path"
    finally:
        os.unlink(scope / "ssh_link")


def test_content_denylist_rejected(b, scope):
    res = b.apply_patch_set(
        scope, [_w("a.txt", "my password is hunter2")], Role.IMPLEMENTER, LEAD
    )
    assert res.applied == []
    assert res.errors[0]["error"] == "content_denylist"
    assert not (scope / "a.txt").exists()


def test_content_denylist_token_now_accepted(b, scope):
    # FIX 4: 'token' is ordinary code, no longer a content deny-list term.
    res = b.apply_patch_set(
        scope, [_w("a.txt", "api_token = abc123")], Role.IMPLEMENTER, LEAD
    )
    assert res.errors == []
    assert (scope / "a.txt").read_text() == "api_token = abc123"


def test_content_denylist_high_signal_rejected(b, scope):
    # FIX 4: privacy-high-signal markers are still rejected in file content.
    for text in ("secret", "password", "api_key =", "credential", "recipient"):
        res = b.apply_patch_set(
            scope, [_w("a.txt", text)], Role.IMPLEMENTER, LEAD
        )
        assert res.applied == [], text
        assert res.errors[0]["error"] == "content_denylist", text
        assert not (scope / "a.txt").exists()


def test_content_denylist_ordinary_code_accepted(b, scope):
    # FIX 4: ordinary code words are no longer rejected in file content.
    for text in ("token", "code", "diff", "content", "subject", "body"):
        res = b.apply_patch_set(
            scope, [_w("a.txt", text)], Role.IMPLEMENTER, LEAD
        )
        assert res.errors == [], text
        assert (scope / "a.txt").read_text() == text


# --------------------------------------------------------- §2.8 TOCTOU-near


def test_toctou_symlink_race_rejected(b, scope):
    def hook(target):
        os.symlink("/etc/passwd", target)

    b._before_replace_hook = hook
    res = b.apply_patch_set(scope, [_w("a.txt", "new")], Role.IMPLEMENTER, LEAD)
    assert res.applied == []
    assert len(res.errors) == 1
    if os.path.lexists(scope / "a.txt"):
        os.unlink(scope / "a.txt")


# --------------------------------------------------------- §2.9 all-or-nothing


def test_atomic_all_or_nothing_prevalidation(b, scope):
    (scope / "existing.txt").write_text("original")
    patch_set = [
        _w("new1.txt", "n1"),
        _w("/etc/passwd", "pwned"),  # fails validation
        _w("existing.txt", "changed"),
    ]
    res = b.apply_patch_set(scope, patch_set, Role.IMPLEMENTER, LEAD)
    assert res.applied == []
    assert len(res.errors) == 1
    assert not (scope / "new1.txt").exists()
    assert (scope / "existing.txt").read_text() == "original"


def test_atomic_rollback_on_runtime_failure(b, scope):
    (scope / "existing.txt").write_text("original")

    def hook(target):
        if target == str(scope / "new2.txt"):
            os.symlink("/etc/passwd", target)

    b._before_replace_hook = hook
    res = b.apply_patch_set(
        scope,
        [
            _w("new1.txt", "n1"),
            _w("existing.txt", "changed"),
            _w("new2.txt", "n2"),  # runtime failure -> rollback
        ],
        Role.IMPLEMENTER,
        LEAD,
    )
    assert res.applied == []
    assert len(res.errors) == 1
    assert not (scope / "new1.txt").exists()
    assert (scope / "existing.txt").read_text() == "original"
    if os.path.lexists(scope / "new2.txt"):
        os.unlink(scope / "new2.txt")


# -------------------------------------------------------- §2.10 no shell/exec


def test_no_shell_execution(b, scope):
    marker = scope / "pwned.txt"
    payload = f"$(touch {marker}) && echo hi"
    res = b.apply_patch_set(scope, [_w("a.txt", payload)], Role.IMPLEMENTER, LEAD)
    assert res.errors == []
    assert (scope / "a.txt").read_text() == payload
    assert not marker.exists()


# ------------------------------------------------------------ role scope


def test_implementer_scope_allowed(b, scope):
    (scope / "tests").mkdir()
    res = b.apply_patch_set(
        scope, [_w("parser.py", "x=1")], Role.IMPLEMENTER, LEAD
    )
    assert res.errors == []
    assert (scope / "parser.py").read_text() == "x=1"


def test_implementer_outside_scope_blocked(b, scope):
    res = b.apply_patch_set(
        scope, [_w("../outside.py", "x=1")], Role.IMPLEMENTER, LEAD
    )
    assert res.applied == []
    assert res.errors[0]["error"] == "scope_denied"


def test_qa_tests_allowed(b, scope):
    (scope / "tests").mkdir()
    res = b.apply_patch_set(
        scope, [_w("tests/test_a.py", "def test_a(): pass")], Role.QA, LEAD
    )
    assert res.errors == []
    assert len(res.applied) == 1
    assert (scope / "tests" / "test_a.py").exists()


def test_qa_product_code_blocked(b, scope):
    captured = []
    b = WorkspaceBroker(emit_event=lambda t, p: captured.append((t, p)))
    res = b.apply_patch_set(scope, [_w("parser.py", "x=1")], Role.QA, LEAD)
    assert res.applied == []
    assert res.errors[0]["error"] == "scope_denied"
    assert not (scope / "parser.py").exists()
    # role violation event emitted (metadata only)
    assert len(captured) == 1
    t, p = captured[0]
    assert t == "policy.role_violation"
    assert p["op"] == "write"
    assert p["path"] == "parser.py"
    assert p["ok"] is False


def test_non_controller_source_denied(b, scope):
    for src in ("role:implementer", "role:qa", "email", "owner:authenticated"):
        with pytest.raises(PermissionDenied):
            b.apply_patch_set(scope, [_w("a.txt", "x")], Role.IMPLEMENTER, src)


def test_other_roles_denied(b, scope):
    for role in (Role.LEAD, Role.ANALYST, Role.REVIEWER):
        with pytest.raises(PermissionDenied):
            b.apply_patch_set(scope, [_w("a.txt", "x")], role, LEAD)


# ------------------------------------------------- no content in events


def test_no_content_in_events(b, scope):
    captured = []
    b = WorkspaceBroker(emit_event=lambda t, p: captured.append((t, p)))
    # normal implementer write -> no events at all
    res = b.apply_patch_set(scope, [_w("a.txt", "some text")], Role.IMPLEMENTER, LEAD)
    assert res.errors == []
    assert captured == []
    # qa product-code write -> one metadata-only event
    res = b.apply_patch_set(scope, [_w("parser.py", "def f(): return 1")], Role.QA, LEAD)
    assert res.errors != []
    assert len(captured) == 1
    _, payload = captured[0]
    assert "def f" not in str(payload)
    assert set(payload.keys()) <= {"op", "path", "ok", "error"}


# ------------------------------------------------------- result shape


def test_broker_result_shape(b, scope):
    res = b.apply_patch_set(scope, [_w("a.txt", "x")], Role.IMPLEMENTER, LEAD)
    assert isinstance(res, BrokerResult)
    assert res.applied == [{"op": "write", "path": "a.txt"}]
    assert res.skipped == []
    assert res.errors == []


def test_module_level_broker_is_broker():
    assert isinstance(broker, WorkspaceBroker)

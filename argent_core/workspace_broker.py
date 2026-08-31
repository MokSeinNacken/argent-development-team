"""Write-Broker (SPEC V2B chapter 2).

The single path by which file changes enter the task workspace.  Role agents
have zero dangerous or mutating tools (one harmless status capability on
direct turns; SPEC V2B §1); every write flows through this broker,
which applies a chain of hardening checks (§2.1–§2.10) and applies the whole
patch set atomically (all-or-nothing, §2.9).

Callable only by the controller (``source='role:lead'``).  Role scope:

- ``implementer`` -> everything under the fixture root;
- ``qa``          -> only ``fixture_root/tests/**`` (a product-code path is
  rejected and a ``policy.role_violation`` event is emitted via the event
  sink — metadata only, never content);
- any other role  -> ``PermissionDenied``.

No shell/eval/exec is ever involved: this module performs pure file
operations (§2.10).
"""

from __future__ import annotations

import base64
import binascii
import os
import stat as stat_module
from dataclasses import dataclass, field
from typing import Callable, Optional
from uuid import uuid4

from .models import PermissionDenied, Role

CONTROLLER_SOURCE = "role:lead"

# Home-relative deny suffixes (§2.7), expanded at construction time.
_HOME_DENY_SUFFIXES: tuple[str, ...] = (
    ".openclaw",
    ".ssh",
    ".config",
    ".npm-global",
)

# Absolute deny prefixes (§2.7), independent of scope.
_ABSOLUTE_DENY_PREFIXES: tuple[str, ...] = (
    "/etc",
    "/mnt",
    "/proc",
    "/sys",
    "/dev",
    "/run",
)

# Content deny-list (§2.7): privacy-high-signal markers only.  This is a
# NARROWED list, deliberately distinct from events.PRIVACY_DENYLIST (which is
# still used for envelope validation in outputs.py / events).  Ordinary code
# words like token/code/diff/content/subject/body are NOT denied here, so
# legitimate source (``token = lexer.next()``, ``data.decode()``, etc.) passes.
CONTENT_DENYLIST: frozenset[str] = frozenset({
    "secret",
    "password",
    "api_key",
    "credential",
    "mail_content",
    "mail_address",
    "email_address",
    "recipient",
})


class BrokerError(Exception):
    """A hardening violation for a single patch (recorded, not fatal)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class BrokerResult:
    """Result of :meth:`WorkspaceBroker.apply_patch_set` (SPEC V2B §2)."""

    applied: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _within(root: str, path: str) -> bool:
    """True if ``path`` is ``root`` or strictly below it (separator boundary)."""
    if path == root:
        return True
    return path.startswith(root + os.sep)


def _content_hit(content: bytes) -> Optional[str]:
    """Return the first deny-listed term found in ``content`` (lowercased)."""
    low = content.decode("utf-8", errors="replace").lower()
    for word in sorted(CONTENT_DENYLIST, key=len, reverse=True):
        if word in low:
            return word
    return None


class WorkspaceBroker:
    """Applies validated patch sets to the task workspace (SPEC V2B §2)."""

    def __init__(self, emit_event: Optional[Callable[[str, dict], None]] = None,
                 writer_guard: Optional[Callable[[str, object, str], None]] = None):
        self._emit_event = emit_event
        # B3: optional writer-binding guard invoked immediately before any
        # mutating write (the broker remains the only write path).
        self._writer_guard = writer_guard
        # Test seam: called with the target path immediately before the final
        # re-canonicalisation/os.replace (TOCTOU simulation, SPEC V2B §2.8).
        self._before_replace_hook: Optional[Callable[[str], None]] = None
        # B4 (F2): the guard call context captured at the top of
        # ``apply_patch_set`` so the writer fence can be re-asserted immediately
        # before EVERY OS effect (not just once before the loop).
        self._guard_scope_root: Optional[str] = None
        self._guard_role: Optional[Role] = None
        self._guard_source: Optional[str] = None

        home = os.path.realpath(os.path.expanduser("~"))
        deny: list[str] = [os.path.join(home, s) for s in _HOME_DENY_SUFFIXES]
        deny.extend(_ABSOLUTE_DENY_PREFIXES)
        self._deny_paths: tuple[str, ...] = tuple(
            os.path.realpath(p) if os.path.isabs(p) else os.path.normpath(p)
            for p in deny
        )

    # ---------------------------------------------------------------- helpers

    def _emit(self, event_type: str, **payload) -> None:
        if self._emit_event is not None:
            self._emit_event(event_type, payload)

    def _recheck_writer_guard(self) -> None:
        """F2: re-assert the writer-binding guard immediately before an OS
        effect (staging write, os.replace, unlink) and again after it.

        Uses a FRESH job read (the guard's provider re-reads the store), so a
        takeover between the top-of-patch guard and the actual write raises
        :class:`PermissionDenied` before/after the mutation instead of silently
        accepting a stale writer.
        """
        if self._writer_guard is not None and self._guard_scope_root is not None:
            self._writer_guard(
                self._guard_scope_root, self._guard_role, self._guard_source,
            )

    @staticmethod
    def _coerce_role(role) -> Role:
        if isinstance(role, Role):
            return role
        try:
            return Role(role)
        except (ValueError, TypeError):
            raise PermissionDenied(f"unknown role {role!r}") from None

    def _allowed_root(self, scope_root: str, role: Role) -> str:
        root = os.path.realpath(os.path.abspath(os.fspath(scope_root)))
        if role is Role.IMPLEMENTER:
            return root
        if role is Role.QA:
            return os.path.realpath(os.path.join(root, "tests"))
        raise PermissionDenied(
            f"role {role.value!r} may not use the write-broker"
        )

    def _is_denied_path(self, real: str) -> bool:
        for dp in self._deny_paths:
            if _within(dp, real):
                return True
        return False

    def _resolve_target(self, scope_root: str, path: str) -> str:
        # §2.1: absolute paths are rejected outright.
        if os.path.isabs(path):
            raise BrokerError("absolute_path")
        # §2.2: normpath collapses '..'; realpath resolves existing symlinks.
        norm = os.path.normpath(os.path.join(scope_root, path))
        return os.path.realpath(norm)

    def _emit_role_violation(self, role: Role, op: str, path: str) -> None:
        # §2 (role scope): a QA product-code path is a policy violation.
        if role is not Role.QA:
            return
        self._emit(
            "policy.role_violation",
            op=op,
            path=path,
            ok=False,
            error="scope_denied",
        )

    def _check_target_type(self, target: str) -> None:
        """§2.5 pre-write check: target must be a regular, non-setuid file."""
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return  # target does not exist yet -> fine
        except OSError as exc:
            raise BrokerError(f"lstat_failed:{exc.errno}") from exc
        if stat_module.S_ISLNK(st.st_mode):
            raise BrokerError("symlink_target")
        if not stat_module.S_ISREG(st.st_mode):
            raise BrokerError("special_file")
        if st.st_mode & 0o6000:
            raise BrokerError("setuid_setgid")

    def _decode_content(self, raw) -> bytes:
        if not isinstance(raw, str):
            raise BrokerError("bad_content")
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise BrokerError("bad_content")

    def _relpath(self, root: str, target: str) -> str:
        try:
            rel = os.path.relpath(target, root)
        except ValueError:
            rel = target
        return rel

    # ------------------------------------------------------------- planning

    def _plan_patch(self, scope_root: str, allowed_root: str, role: Role, patch):
        """Validate one patch; return a ``(op, target, content)`` plan or an
        error dict."""
        if not isinstance(patch, dict):
            return {"op": None, "path": None, "error": "malformed_patch"}
        op = patch.get("op")
        path = patch.get("path")
        if op not in ("write", "delete"):
            return {"op": op, "path": path, "error": "bad_op"}
        if not isinstance(path, str) or not path:
            return {"op": op, "path": path, "error": "bad_path"}

        try:
            real = self._resolve_target(scope_root, path)
        except BrokerError as exc:
            return {"op": op, "path": path, "error": exc.reason}

        # §2.7: deny-list is independent of the scope.
        if self._is_denied_path(real):
            return {"op": op, "path": path, "error": "deny_path"}

        # §2.6: all writes must stay under the role-scoped allowed root.
        if not _within(allowed_root, real):
            self._emit_role_violation(role, op, path)
            return {"op": op, "path": path, "error": "scope_denied"}

        # §2.3: a symlink at the target itself is rejected (lstat, no follow).
        norm = os.path.normpath(os.path.join(scope_root, path))
        if os.path.islink(norm):
            return {"op": op, "path": path, "error": "symlink_target"}

        if op == "write":
            try:
                content = self._decode_content(patch.get("content"))
            except BrokerError as exc:
                return {"op": op, "path": path, "error": exc.reason}
            hit = _content_hit(content)
            if hit is not None:
                return {"op": op, "path": path, "error": "content_denylist"}
            # §2.5: pre-write target type check.
            try:
                self._check_target_type(real)
            except BrokerError as exc:
                return {"op": op, "path": path, "error": exc.reason}
            return ("write", real, content)

        # delete: only regular files (§2.6); missing file is a benign skip.
        try:
            st = os.lstat(real)
        except FileNotFoundError:
            return ("skip", real, None)
        if stat_module.S_ISLNK(st.st_mode) or not stat_module.S_ISREG(st.st_mode):
            return {"op": op, "path": path, "error": "not_regular_file"}
        return ("delete", real, None)

    # ------------------------------------------------------------- snapshot

    def _snapshot(self, target: str) -> tuple:
        """Return ``(existed, content_bytes, mode)`` for rollback."""
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return (False, None, None)
        except OSError:
            return (False, None, None)
        if stat_module.S_ISREG(st.st_mode):
            try:
                with open(target, "rb") as fh:
                    data = fh.read()
            except OSError:
                return (False, None, None)
            return (True, data, stat_module.S_IMODE(st.st_mode))
        return (False, None, None)

    def _restore(self, target: str, snapshot: tuple) -> None:
        """Best-effort rollback of a previously applied patch."""
        existed, data, mode = snapshot
        if not existed:
            try:
                os.unlink(target)
            except OSError:
                pass
            return
        try:
            with open(target, "wb") as fh:
                fh.write(data)
            os.chmod(target, mode)
        except OSError:
            pass

    # ------------------------------------------------------------ operations

    def _write_file(self, allowed_root: str, target: str, content: bytes) -> None:
        """Stage + ``os.replace`` + verify (SPEC V2B §2.4/§2.5/§2.8/§2.9)."""
        parent = os.path.dirname(target)
        parent_real = os.path.realpath(parent)
        if not _within(allowed_root, parent_real):
            raise BrokerError("scope_denied")

        # F2: writer fence re-check BEFORE any OS effect.
        self._recheck_writer_guard()

        staging = os.path.join(parent, ".argent-staging-" + uuid4().hex)
        fd: Optional[int] = None
        try:
            # §2.8: O_CREAT|O_EXCL|O_NOFOLLOW staging file in the target dir.
            try:
                fd = os.open(
                    staging,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
            except FileExistsError:
                raise BrokerError("staging_exists") from None
            except OSError as exc:
                raise BrokerError(f"staging_failed:{exc.errno}") from exc
            try:
                os.write(fd, content)
            finally:
                os.close(fd)
                fd = None

            # Test seam for TOCTOU simulation (§2.8).
            if self._before_replace_hook is not None:
                self._before_replace_hook(target)

            # F2: re-check the fence immediately before os.replace (closes the
            # TOCTOU between the top-of-patch guard and the final replace).
            self._recheck_writer_guard()

            # §2.8: re-canonicalise immediately before os.replace.
            re_real = os.path.realpath(target)
            if not _within(allowed_root, re_real):
                raise BrokerError("scope_denied")
            if self._is_denied_path(re_real):
                raise BrokerError("deny_path")
            self._check_target_type(target)

            os.replace(staging, target)
            staging = None

            # F2: post-effect confirmation check (a takeover DURING the write
            # must be surfaced, never silently accepted).
            self._recheck_writer_guard()

            # §2.4 + §2.5: post-replace lstat verification.
            st = os.lstat(target)
            if not stat_module.S_ISREG(st.st_mode):
                raise BrokerError("special_file")
            if st.st_mode & 0o6000:
                raise BrokerError("setuid_setgid")
            if st.st_nlink != 1:
                raise BrokerError("hardlink")
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if staging is not None:
                try:
                    os.unlink(staging)
                except OSError:
                    pass

    def _delete_file(self, allowed_root: str, target: str) -> None:
        parent = os.path.dirname(target)
        if not _within(allowed_root, os.path.realpath(parent)):
            raise BrokerError("scope_denied")
        re_real = os.path.realpath(target)
        if not _within(allowed_root, re_real):
            raise BrokerError("scope_denied")
        st = os.lstat(target)
        if stat_module.S_ISLNK(st.st_mode) or not stat_module.S_ISREG(st.st_mode):
            raise BrokerError("not_regular_file")
        # F2: writer fence re-check before the unlink effect.
        self._recheck_writer_guard()
        os.unlink(target)
        # F2: post-effect confirmation check.
        self._recheck_writer_guard()

    # ---------------------------------------------------------------- public

    def apply_patch_set(
        self, scope_root, patch_set, role, source
    ) -> BrokerResult:
        """Apply a validated patch set (SPEC V2B §2).

        ``patch_set`` is a list of ``{op: "write"|"delete", path: str,
        content: str (base64, write only)}``.  The whole set is all-or-nothing.
        """
        if source != CONTROLLER_SOURCE:
            raise PermissionDenied(
                f"write-broker requires controller source {CONTROLLER_SOURCE!r}, "
                f"got {source!r}"
            )
        role = self._coerce_role(role)
        scope_root = os.fspath(scope_root)
        allowed_root = self._allowed_root(scope_root, role)

        # B4 (F2): capture the guard context so every per-effect re-check uses
        # the SAME (scope_root, role, source) the top-of-patch guard verified.
        self._guard_scope_root = scope_root
        self._guard_role = role
        self._guard_source = source

        # B3: writer-binding guard before any mutating write (no-op when no
        # guard is installed).  Raises PermissionDenied on any violation.
        if self._writer_guard is not None:
            self._writer_guard(scope_root, role, source)

        result = BrokerResult()
        plans: list = []
        for patch in patch_set:
            plan = self._plan_patch(scope_root, allowed_root, role, patch)
            if isinstance(plan, dict):
                result.errors.append(plan)
            else:
                plans.append(plan)

        # §2.9: all-or-nothing — a single error means nothing is applied.
        if result.errors:
            return result

        applied: list = []
        for op, target, content in plans:
            snapshot = self._snapshot(target)
            try:
                if op == "write":
                    self._write_file(allowed_root, target, content)
                elif op == "delete":
                    self._delete_file(allowed_root, target)
                elif op == "skip":
                    result.skipped.append(
                        {"op": "delete", "path": self._relpath(allowed_root, target)}
                    )
                    continue
            except BrokerError as exc:
                # Roll back every file already applied (no partial state).
                for t, s in reversed(applied):
                    self._restore(t, s)
                return BrokerResult(
                    applied=[],
                    skipped=[],
                    errors=[
                        {
                            "op": op,
                            "path": self._relpath(allowed_root, target),
                            "error": exc.reason,
                        }
                    ],
                )
            applied.append((target, snapshot))
            result.applied.append(
                {"op": op, "path": self._relpath(allowed_root, target)}
            )
        return result


# Module-level convenience instance (controller wiring injects an event sink).
broker = WorkspaceBroker()

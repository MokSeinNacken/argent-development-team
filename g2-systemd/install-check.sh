#!/usr/bin/env bash
# Argent Phase G2 — READ-ONLY deployment validation (never activates).
#
# Validates the operator-provisioned systemd user-service deployment contract
# WITHOUT enabling, starting, reloading, restarting or otherwise activating
# the unit.  This script is intentionally inert: it only READS files and
# reports PASS/FAIL.  No activation command is ever executed here.
#
# It validates BOTH the versioned unit template (g1-systemd/...) AND the
# INSTALLED unit (resolved READ-ONLY via `systemctl --user show -p FragmentPath`,
# falling back to `systemctl --user cat`).  The installed unit must equal the
# template with exactly the three deployment substitutions applied
# (Documentation / WorkingDirectory / EnvironmentFile).  The three host-specific
# values are parameterizable via environment variables with this host's values
# as defaults (so the script is never hard-coded to a single machine).
#
# Checks (read-only):
#   1. the versioned unit template exists;
#   2. the unit carries no secret literal / key value;
#   3. the optional EnvironmentFile (service.env) references the evidence MAC
#      key ONLY by path (ARGENT_EVIDENCE_MAC_KEY_FILE), never by value
#      (ARGENT_EVIDENCE_MAC_KEY), and is mode 0600;
#   4. the referenced evidence key file (if present) is mode 0600;
#   5. the INSTALLED unit's effective directives equal the template with the
#      three deployment substitutions applied.
#
# This script never prints the key bytes or the key-file path.

set -u

# Resolve the repository root (parent of this script's directory) without
# touching the live service.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT_FILE="${REPO_DIR}/g1-systemd/argent-supervisor.service"

# --- Parameterizable deployment values (env-overridable; host defaults) -----
# A different host overrides these via environment (e.g. in the Supervisor's
# deployment script); the defaults below are this host's live values.
ARGENT_WORKTREE="${ARGENT_WORKTREE:-/home/pc/projects/argent-worktrees/phase-g2-systemd-live-activation}"
ARGENT_DOC="${ARGENT_DOC:-file:${ARGENT_WORKTREE}/docs/PHASE_G2_ACCEPTANCE.md}"
ARGENT_WORKDIR="${ARGENT_WORKDIR:-${ARGENT_WORKTREE}}"
ARGENT_ENVFILE="${ARGENT_ENVFILE:--${HOME:-/home/pc}/.config/argent/service.env}"

HOME_DIR="${HOME:-/nonexistent}"
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME_DIR}/.config}/argent"
SERVICE_ENV="${CONFIG_DIR}/service.env"

status=0
ok()   { printf '  ok: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1"; status=1; }
note() { printf 'info: %s\n' "$1"; }

# Extract the (last) value of a directive from a unit file (comments ignored).
unit_value() {
  local file="$1" dir="$2"
  sed -nE "s/^[[:space:]]*${dir}=//p" "${file}" 2>/dev/null | tail -n1
}

echo "== Argent G2 install-check (read-only; no activation) =="

# 1. Unit template present.
if [[ -f "${UNIT_FILE}" ]]; then
    ok "unit template present: ${UNIT_FILE}"
else
    fail "unit template missing: ${UNIT_FILE}"
fi

# 2. Unit must carry no secret literal / key value.
if [[ -f "${UNIT_FILE}" ]]; then
    if grep -qE '^[[:space:]]*ARGENT_EVIDENCE_MAC_KEY=' "${UNIT_FILE}"; then
        fail "unit template embeds a key value (ARGENT_EVIDENCE_MAC_KEY=)"
    else
        ok "unit template has no embedded key value"
    fi
fi

# 3. service.env — path reference only, mode 0600 (never print contents).
EVIDENCE_KEY_FILE=""
if [[ -f "${SERVICE_ENV}" ]]; then
    if grep -qE '^[[:space:]]*ARGENT_EVIDENCE_MAC_KEY=' "${SERVICE_ENV}"; then
        fail "service.env contains a key VALUE (ARGENT_EVIDENCE_MAC_KEY=) — remove it"
    else
        ok "service.env has no key value"
    fi
    if grep -qE '^[[:space:]]*ARGENT_EVIDENCE_MAC_KEY_FILE=' "${SERVICE_ENV}"; then
        ok "service.env references the key by path (ARGENT_EVIDENCE_MAC_KEY_FILE=)"
        EVIDENCE_KEY_FILE="$(sed -nE 's/^[[:space:]]*ARGENT_EVIDENCE_MAC_KEY_FILE=[[:space:]]*([^[:space:]#]+).*/\1/p' "${SERVICE_ENV}" | head -n1)"
    else
        note "service.env does not reference a key path (key not configured)"
    fi
    mode="$(stat -c '%a' "${SERVICE_ENV}" 2>/dev/null || true)"
    if [[ -n "${mode}" && "${mode}" != "600" ]]; then
        fail "service.env mode is ${mode} (expected 600)"
    else
        ok "service.env mode is ${mode:-unknown} (600 expected)"
    fi
else
    note "service.env not present (optional; key not configured)"
fi

# 4. Evidence key file mode 0600 (if present).  Never print its path/contents.
if [[ -n "${EVIDENCE_KEY_FILE}" ]]; then
    if [[ -f "${EVIDENCE_KEY_FILE}" ]]; then
        kmode="$(stat -c '%a' "${EVIDENCE_KEY_FILE}" 2>/dev/null || true)"
        if [[ -n "${kmode}" && "${kmode}" != "600" ]]; then
            fail "evidence key file mode is ${kmode} (expected 600)"
        else
            ok "evidence key file mode is ${kmode:-unknown} (600 expected)"
        fi
    else
        note "evidence key file not present at referenced path (not created yet)"
    fi
fi

# 5. INSTALLED unit effective-directive validation (read-only resolution).
resolve_fragment() {
    local fp tmp
    fp="$(systemctl --user show -p FragmentPath argent-supervisor.service 2>/dev/null \
         | sed -nE 's/^FragmentPath=//p')"
    if [[ -n "${fp}" && -f "${fp}" ]]; then
        printf '%s\n' "${fp}"
        return 0
    fi
    # Fallback: `systemctl --user cat` (read-only dump of the effective unit).
    tmp="$(mktemp 2>/dev/null || true)"
    if [[ -n "${tmp}" ]] \
       && systemctl --user cat argent-supervisor.service >"${tmp}" 2>/dev/null \
       && [[ -s "${tmp}" ]]; then
        printf '%s\n' "${tmp}"
        return 0
    fi
    [[ -n "${tmp}" ]] && rm -f "${tmp}"
    return 1
}

INSTALLED_UNIT="$(resolve_fragment || true)"
INSTALLED_TMP=""
if [[ -z "${INSTALLED_UNIT}" ]]; then
    note "installed unit not resolvable (systemd user unit absent) — skipping installed-unit validation"
else
    if [[ "${INSTALLED_UNIT}" == /tmp/* || "${INSTALLED_UNIT}" == /var/tmp/* ]]; then
        INSTALLED_TMP="${INSTALLED_UNIT}"
    fi
    compare_directive() {
        local dir="$1" expected="$2" got
        got="$(unit_value "${INSTALLED_UNIT}" "${dir}")"
        if [[ "${got}" == "${expected}" ]]; then
            ok "installed ${dir} matches deployment value"
        else
            fail "installed ${dir} is '${got}' (expected '${expected}')"
        fi
    }
    compare_directive "Documentation" "${ARGENT_DOC}"
    compare_directive "WorkingDirectory" "${ARGENT_WORKDIR}"
    compare_directive "EnvironmentFile" "${ARGENT_ENVFILE}"
    # Remaining shared directives must equal the template exactly.
    for dir in Description After Wants StartLimitBurst StartLimitIntervalSec \
               Type ExecStart StateDirectory CacheDirectory StateDirectoryMode \
               CacheDirectoryMode UMask KillSignal TimeoutStopSec Restart \
               RestartSec NoNewPrivileges WantedBy; do
        tpl="$(unit_value "${UNIT_FILE}" "${dir}")"
        got="$(unit_value "${INSTALLED_UNIT}" "${dir}")"
        if [[ "${got}" == "${tpl}" ]]; then
            ok "installed ${dir} == template"
        else
            fail "installed ${dir} '${got}' != template '${tpl}'"
        fi
    done
fi

# Cleanup the temp file produced by the `systemctl --user cat` fallback.
[[ -n "${INSTALLED_TMP}" ]] && rm -f "${INSTALLED_TMP}"

echo
if [[ ${status} -eq 0 ]]; then
    echo "install-check: OK (validated read-only; unit NOT activated)"
else
    echo "install-check: FAILED (see above; unit NOT activated)"
fi
exit ${status}

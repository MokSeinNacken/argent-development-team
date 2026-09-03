# PHASE I3-A ACCEPTANCE — External Action Broker Core

**Branch:** `phase-i3a-external-action-broker` (Base `ab238ff` = Phase I2 GREEN).
**Datum:** 2026-09-03.
**Scope:** code + deterministic tests + docs + local demo only. **No commit, no
push, no live service/systemd/state-dir mutation, no network writes, no real
provider writes, no credential/token changes or printing, no LLM agents.**

**STATUS: I3-A GREEN (Main-verifiziert; kein I3-/I-GREEN)** — independent verification + Sol closing review pending
(Main).

---

## 1. Deliverables

- **NEW** `argent_core/external_action_broker.py` — provider-neutral External
  Action Broker: request model, `ActionTaxonomy`, `RequestState`, deterministic
  `PolicyEngine`, allowlist + standing policy, branch safety, fencing,
  idempotency/reconciliation, retry budget + expiry, secret-free audit,
  publication safety, external-wait integration, read/write separation.
- **NEW** `argent_core/external_provider_adapter.py` — `ExternalProviderAdapter`
  ABC (I3-B-ready), `NoWriteExternalProviderAdapter`, `FakeGitHubAdapter`
  (deterministic fixture), bounded result types + provider exception taxonomy.
- **EXTENDED** `argent_core/execution_scope.py` — credential-isolation fix
  (§31): `resolve_credential_mask_paths()` + `credential_dirs` tmpfs mask
  parameter; docstrings updated.
- **EXTENDED** `argent_core/store.py` — SCHEMA 21 → 22; additive
  `external_action_requests` + `external_action_audit` tables (+ indexes) +
  request lifecycle store methods.
- **Tests** `tests/test_phase_i3a_broker.py` (45), `_provider.py` (9),
  `_credentials.py` (7), `_migration.py` (4),
  `_hardening.py` (26) — **91 I3-A tests** (the `_hardening.py` file is the
  deterministic closing-fix round proving each Sol HIGH/LOW finding).
- **Docs** `docs/PHASE_I3A_NOTES.md` (A–I + design) + this file.
- **Demo** `docs/i3a_demo.py`.

## 2. Broker contract

- **Taxonomy**: READ / BOUNDED_WRITE / SENSITIVE; closed GitHub-oriented action
  registry; unknown → DENY.
- **States**: PENDING/AUTHORIZED/EXECUTING/WAITING_EXTERNAL/SUCCEEDED/FAILED/
  BLOCKED/DENIED (SEPARATE from job states; terminal clearly defined).
- **Policy**: pure deterministic function (no LLM); ALLOW_AUTONOMOUS /
  OWNER_GATE_REQUIRED / DENY / DEFER + bounded reason codes.
- **Fencing**: request revision CAS + holder-verified authoritative transitions
  (live job lease + action lock re-verified atomically).
- **Idempotency/reconciliation**: UNIQUE idempotency key; provider-visible
  state probes (push SHA, existing Argent-owned PR); never exactly-once beyond
  provider semantics.
- **Audit**: REQUESTED/AUTHORIZED/EXECUTED/RECONCILED; closed failure classes;
  secret-free.

## 3. Case → test mapping (63 tests)

| Case | Meaning | Test(s) |
|---|---|---|
| 1 | Taxonomy = 3 classes | `test_case1_taxonomy_three_classes` |
| 2 | Closed action registry (GitHub initial set) | `test_case2_closed_action_registry` |
| 3 | Bounded broker states, terminal defined, separate from job states | `test_case3_broker_states_bounded_and_terminal_defined` |
| 4 | Versioned controller-authoritative request record | `test_case4_versioned_request_record` |
| 5 | Deterministic id + idempotent creation | `test_case5_deterministic_request_id_and_idempotent_creation` |
| 6 | Request requires INTEGRATED candidate | `test_case6_request_requires_integrated_candidate` |
| 7 | Missing provenance → fail closed | `test_case7_missing_provenance_fails_closed` |
| 8 | Provenance hash mismatch → fail closed | `test_case8_provenance_hash_mismatch_fails_closed` |
| 9 | Source job not terminal → fail closed | `test_case9_source_job_not_terminal_fails_closed` |
| 10 | External content is untrusted data | `test_case10_external_content_is_untrusted_data` |
| 11 | Unknown provider → DENY | `test_case11_unknown_provider_denies` |
| 12 | Unknown action → DENY | `test_case12_unknown_action_denies` |
| 13 | Unknown repo/account → DENY (allowlist exact) | `test_case13_unknown_repo_or_account_denies` |
| 14 | SENSITIVE → OWNER_GATE_REQUIRED | `test_case14_sensitive_action_requires_owner` |
| 15 | No string-prefix authz | `test_case15_no_string_prefix_authz` |
| 16 | Protected refs never autonomous | `test_case16_protected_ref_never_autonomous` |
| 17 | Autonomous push restricted to namespace | `test_case17_autonomous_push_restricted_to_namespace` |
| 18 | Push to protected ref not autonomous | `test_case18_push_to_protected_ref_not_autonomous` |
| 19 | Push outside namespace → owner gated | `test_case19_push_outside_namespace_owner_gated` |
| 20 | PR target must be allowlisted | `test_case20_pr_target_must_be_allowlisted` |
| 21 | authorize_autonomous re-runs policy | `test_case21_authorize_autonomous_reenquires_policy` |
| 22 | Owner approval binds exactly | `test_case22_owner_approval_binds_exactly` |
| 23 | Owner approval wrong scope refused | `test_case23_owner_approval_wrong_scope_refused` |
| 24 | Owner approval not approved refused | `test_case24_owner_approval_not_approved_refused` |
| 25 | Execute happy path succeeds | `test_case25_execute_happy_path_succeeds` |
| 26 | Fenced transition revision CAS | `test_case26_fenced_transition_revision_cas` |
| 27 | Stale holder cannot finalize | `test_case27_stale_holder_cannot_finalize` |
| 28 | Push reconcile detects crash-after-success | `test_case28_push_reconcile_detects_crash_after_success` |
| 29 | Create-PR reconcile detects existing PR | `test_case29_create_pr_reconcile_detects_existing_pr` |
| 30 | Retry backoff bounded | `test_case30_retry_backoff_is_bounded` |
| 31 | Transient failure → WAITING_EXTERNAL | `test_case31_transient_failure_enters_waiting_external` |
| 32 | Audit lifecycle durable + secret-free | `test_case32_audit_lifecycle_durable_and_secret_free` |
| 33 | Failure classes distinguish | `test_case33_failure_classes_distinguish` |
| 34 | Audit rejects unknown failure class | `test_case34_audit_rejects_unknown_failure_class` |
| 35 | Audit never logs secrets | `test_case35_audit_never_logs_secrets` |
| 36 | Adapter ABC mutations structurally disabled | `test_adapter_abc_mutations_structurally_disabled` |
| 37 | No-write adapter write disabled | `test_no_write_adapter_write_disabled` |
| 38 | Fake adapter push + PR roundtrip | `test_fake_adapter_push_and_pr_roundtrip` |
| 39 | Fake adapter duplicate-PR detection | `test_fake_adapter_duplicate_pr_detection` |
| 40 | Fake adapter observe reconcile semantics | `test_fake_adapter_observe_reconcile_semantics` |
| 41 | Conflict → terminal failure | `test_execute_conflict_terminal_failure` |
| 42 | Rate limit → WAITING_EXTERNAL | `test_rate_limit_enters_waiting_external` |
| 43 | Reconcile not-found is no-op | `test_reconcile_not_found_is_noop` |
| 44 | Stale holder aborts before dispatch | `test_stale_holder_aborts_before_dispatch` |
| 45 | PR title bounded + secret rejected | `test_case44_pr_title_bounded_and_secret_rejected` |
| 46 | PR body redacted + injection rejected | `test_case45_pr_body_secret_redacted_and_injection_rejected` |
| 47 | Publication length bounded | `test_case46_publication_length_bounded` |
| 48 | Parameters validated (no injection) | `test_case47_parameters_validated_no_injection` |
| 49 | No shell/eval/exec in broker | `test_case48_no_shell_eval_exec_in_broker` |
| 50 | Repo validation fail-closed | `test_case49_repo_validation_fail_closed` |
| 51 | Mutation structurally disabled without write-enabled (CASE 50) | `test_case50_mutation_structurally_disabled_without_write_enabled` |
| 52 | READ side-effect-free + autonomous | `test_case51_read_actions_side_effect_free_and_autonomous` |
| 53 | WAITING_EXTERNAL has no LLM slot | `test_case52_waiting_external_has_no_llm_slot` |
| 54 | Credential mask includes ~/.config/gh | `test_resolve_credential_mask_paths_includes_gh_dir` |
| 55 | Credential mask includes present files | `test_resolve_credential_mask_paths_includes_present_files` |
| 56 | Sandbox argv masks credential dirs | `test_sandbox_argv_masks_credential_dirs` |
| 57 | Sandbox argv no cred dirs by default | `test_sandbox_argv_no_credential_dirs_by_default` |
| 58 | Live probe: gh credentials absent inside sandbox | `test_live_probe_gh_credentials_absent_inside_sandbox` |
| 59 | Live probe: before-fix exposure documented | `test_live_probe_before_fix_exposure_documented` |
| 60 | Fresh DB lands on v22 | `test_fresh_db_lands_on_v22` |
| 61 | v21 → v22 adds tables | `test_v21_to_v22_adds_tables` |
| 62 | Reopen is no-op | `test_reopen_is_noop` |
| 63 | idempotency_key UNIQUE constraint | `test_idempotency_key_unique_constraint` |

(Plus adversarial extras embedded in the above — e.g. wrong-holder fence,
conflict/rate-limit/credential outcomes, injection markers, secret redaction.)

## 4. Deterministic test counts

| Group | Result |
|---|---|
| I3-A broker (model/policy/lifecycle/fencing/idempotency/audit/publication) | 45 passed |
| I3-A provider (adapter boundary + reconciliation) | 9 passed |
| I3-A credentials (argv + live bwrap probe + .ssh conditional) | 7 passed |
| I3-A migration (21→22) | 4 passed |
| I3-A hardening (Sol HIGH/LOW closing fixes) | 26 passed |
| **I3-A total** | **91 passed** |

## 5. Intentionally updated existing tests (documented)

- `tests/test_phase_d3_regression.py::test_regression_schema_version` — 21 → 22.
- `tests/test_phase_i2_migration.py` — `SCHEMA_VERSION == "21"` → `"22"` (I2's
  additive table; schema is now 22 after I3-A); docstring updated.
- `tests/test_phase3c_approval_core.py::test_schema_version_is_15` — 21 → 22
  (comment updated).
- `tests/test_phase_i3a_broker.py` owner-approval tests (CASE 22/23/24) — the
  caller-provided `OwnerApproval` object was replaced with a STORE-BACKED
  approval (`_insert_approval` + `_mark_approved`) because `authorize_owner`
  now loads the approval from the authoritative store (HIGH-1).
- `tests/test_phase_i3a_broker.py::test_case14` — the SENSITIVE gate now runs
  AFTER the full allowlist (HIGH-3); the test uses an allowlist that includes
  the SENSITIVE action so OWNER_GATE_REQUIRED is still exercised (a companion
  `test_case14b` proves a non-allowlisted SENSITIVE action is DENY).
- `tests/test_phase_i3a_broker.py::test_case20` / CASE 29 — `create_pull_request`
  now requires + binds `head_sha` to the integrated HEAD (HIGH-2).
- `tests/i3a_helpers.py` — the terminal job + INTEGRATED candidate are now built
  through the authoritative store paths (`create_integration_candidate` +
  `transition_integration_candidate`) instead of direct SQL (HIGH-2 provenance
  binding); `make_provenance` now mints a KEYED MAC (HIGH-2); `make_broker`
  supplies a MAC key + default standing policy (HIGH-3).

No other existing test was changed; `test_phase_g2_sandbox` / `g3` / `i1` / `i2`
semantics are untouched (the credential-mask extension is purely additive).

## 6. Credential probe results (§31)

- Deterministic argv: the mask list contains `~/.config/gh` (+ `~/.ssh`,
  git-credentials/netrc when present); G3 narrowing preserved.
- Live bwrap probe (this host): `~/.config/gh/hosts.yml` + `config.yml` are
  ENOENT inside the FIXED sandbox; supervisor-side (outside) read still works;
  the before-fix argv (empty `credential_dirs`) leaves `hosts.yml` OPENED
  (reproduced, documented).
- Provider credentials never live under `~/.openclaw` (that is the OpenClaw
  config/credential home, masked by G3); the external-provider credential home
  is `~/.config/gh` — the only place I3-A adds a defensive mask.

## 7. Bounded local demo (§30)

`docs/i3a_demo.py` (no network/real writes): Integrated Candidate → request →
Policy → ALLOW_AUTONOMOUS → push + PR (provider object ids) → audit persisted →
crash-before-SUCCESS reconcile → existing PR detected, no duplicate → MERGE →
OWNER_GATE_REQUIRED. Exits 0.

## 8. Sol closing-review fixes (this round) — what is CODE-ENFORCED

The independent read-only Sol HIGH review found 7 HIGH + 3 LOW (no CRITICAL).
All are fixed with deterministic regression tests (`test_phase_i3a_hardening.py`):

- **HIGH-1** owner approval is store-backed, single-use (atomic consume),
  expiry-checked, TRUSTED-source, and bound to (task, action, provider,
  account, repository, resource, requested_scope, provenance, idempotency key,
  parameters/preconditions).
- **HIGH-2** provenance is a KEYED HMAC (Phase-F evidence MAC key — no new
  secret); `candidate.source_job_id == provenance.source_job_id`; push `sha` and
  PR `head_sha` must equal the candidate's integrated HEAD.
- **HIGH-3** standing policy is consulted (empty default ⇒ no autonomous
  write); `branch_namespaces` enforced; full allowlist evaluated BEFORE the
  SENSITIVE gate; `authorize_owner` re-runs policy; execution re-checks policy
  currency.
- **HIGH-4** reconcile is holder-verified + action-lock fenced; the store
  enforces a closed edge map with terminal immutability; PR reconciliation
  matches repo + head SHA + base + idempotency marker (+ argent-owned).
- **HIGH-5** expiry enforced at authorize/execute/reconcile; unified retry
  budget (WAITING included) honors `MAX_RETRY_ATTEMPTS`; a deterministic
  `redrive_waiting` hook exists (I3-B wires the scheduler).
- **HIGH-6** idempotency reuse requires full equivalence (else bounded
  conflict); audit references the actual row id (no orphan rows).
- **HIGH-7** provider detail is sanitized/redacted before ANY persistence.
- **LOW-8** raised provider exceptions map to their failure classes.
- **LOW-9** `~/.ssh` mask added conditionally (like `.netrc`).
- **LOW-10** this documentation now states exactly the fixed, code-enforced
  behavior.

## 9. Operational note (NOT a code defect)

The live deployed service still runs the G3 code path until an authorized
redeploy (the I3-A broker is not wired to any live provider or scheduler yet —
that is I3-B).  This is an operational rollout state, not an I3-A code defect.

## 10. I3-A GREEN (Main-verifiziert; kein I3-/I-GREEN)

Independent verification + Sol closing review re-run pending (Main).

## 10. Main Independent Verification + GREEN (2026-09-03)

Main (Supervisor) führte nach dem Fix-Round unabhängig aus (nicht Writer-/Fix-Round-Zahlen):

| Prüfung | Ergebnis |
|---|---|
| I3-A targeted (6 Dateien) | **91 passed** (eigener Lauf, 2.8 s) |
| **FULL SUITE** | **2799 passed (52.65 s, eigener Lauf nach Fix-Round)** |
| Credential-Probe (Live, finaler Argv, reale Pfade) | `~/.config/gh/hosts.yml` + `config.yml` + `~/.ssh` **ABSENT** im Sandbox-Child; Host-Seite (Broker/Controller) weiterhin lesbar |
| Demo (docs/i3a_demo.py, reproduziert) | ALLOW_AUTONOMOUS → Fake-Push + PR (Provider-IDs) → Audit (secret-free); Crash-vor-SUCCESS → Reconcile erkennt existierende PR, kein Duplikat; MERGE → OWNER_GATE_REQUIRED; kein Netz/kein realer Write |
| Diff-Review Fix-Round | HIGH-1..7 + LOW-8..10 im Code verifiziert (store-backed Single-Use-Approval mit Konsum; Keyed-Provenance-HMAC + Job-/Branch-/HEAD-Bindung; Standing-Policy + vollständige Allowlist vor OWNER_GATE; holder-fenced Reconcile + REQUEST_TRANSITIONS-Edge-Map mit Terminal-Immutability; Expiry/Retry/Redrive; Idempotenz-Äquivalenz + keine Orphan-Audits; Provider-Detail-Redaktion; Exception-Taxonomie; ~/.ssh-Mask) |

Exit-Kriterien (§40 des I3-A-Briefs): provider-neutraler Broker ✓ · versionierte Requests ✓ · deterministische Taxonomie (unbekannt→DENY) ✓ · Allowlist-Policy ✓ · High-Autonomy-Standing-Policy (leer ⇒ keine autonomen Writes) ✓ · Allowlist-/Policy-Änderung owner-gated ✓ · autonom vs. owner-gated getrennt (SENSITIVE → OWNER_GATE_REQUIRED) ✓ · Branch-Namespace-Policy (argent/<task>-<slug>; main/master/stable/release*/production* nie autonom) ✓ · Integrations-Provenance erforderlich (keyed MAC, Job-gebunden) ✓ · Phase-F/I2-Evidence erforderlich ✓ · Agent-Prosa ohne Autorität ✓ · Externer Provider-Content ohne Autorität ✓ · Credentials für Ordinary Agents unzugänglich (Masken; Live-Probe) ✓ · Credentials absent aus Agent-Env/Context ✓ · Trusted-Broker-Credential-Boundary ✓ · durable Action-States ✓ · Lease/Revision/Action-Lock-Fencing ✓ · Retry-Budget bounded ✓ · idempotent/reconcilable ✓ · Crash-nach-Provider-Success behandelt ✓ · Provider-Failure-Klassen distinct ✓ · Read/Write getrennt ✓ · WAITING_EXTERNAL-Integration + Redrive-Hook ✓ · secret-freies Audit ✓ · Git/Ref/Resource-Validierung ✓ · kein shell=True/eval/exec ✓ · Publication-Safety ✓ · reale Writes in I3-A strukturell deaktiviert (CASE 50) ✓ · Fake-Provider-Demo grün ✓ · Produktions-Sandbox-Credential-Probe grün ✓ · I3-A targeted grün ✓ · I2/I1/G/F/E/D/C/B grün ✓ · Full Suite grün ✓ · genau 1 Sol-HIGH-Review (7 HIGH + 3 LOW, alle in genau EINER Fix-Round geschlossen) ✓ · 0 ungelöste HIGH/CRITICAL ✓ · genau 1 lokaler Commit (s. git log HEAD) ✓ · kein Push ✓ · Worktree clean ✓.

Marker: `ARGENT_PHASE_I3A_EXTERNAL_ACTION_BROKER_GREEN`. KEIN `ARGENT_PHASE_I3_GREEN`, KEIN `ARGENT_PHASE_I_GREEN`.

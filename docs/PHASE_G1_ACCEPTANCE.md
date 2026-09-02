# PHASE G1 — Acceptance Matrix (Spec R, cases 1–40)

> **SERVICE CREATED/VALIDATED != SERVICE ACTIVATED.** G1 proves via code and
> tests that it activates nothing.

Test totals: **78 G1 tests** (`tests/test_phase_g1*.py`, incl. 21 fix-round
`test_phase_g1_fix_round.py`); full suite **2470 passed** (2392 baseline +
78 additive); Phase B/C/D/E/F regression **1136 passed**.

| # | Acceptance case | Status | Where |
|---|---|---|---|
| 1 | Fresh startup → unique instance → READY | ✅ | `test_phase_g1_instance.py::test_fresh_startup_acquires_unique_instance`, `test_instance_ids_are_unique_across_stores`; `test_phase_g1_runtime.py::test_health_readiness_after_fresh_acquire` |
| 2 | Second supervisor vs authoritative-live first → cannot activate | ✅ | `test_live_owner_refuses_second_supervisor`; `test_acquire_lock_blocks_second_holder` |
| 3 | Stale supervisor evidence → bounded takeover | ✅ | `test_dead_owner_takeover`, `test_boot_changed_owner_takeover` |
| 4 | PID reuse with different start_ticks → not same process | ✅ | `test_pid_reuse_different_ticks_is_not_same_process` |
| 5 | Same PID over different boot_id → not same process | ✅ | `test_same_pid_different_boot_is_not_same_process` |
| 6 | Unreadable/ambiguous identity → conservative | ✅ | `test_ambiguous_identity_is_unknown_conservative`, `test_ambiguous_owner_fails_closed_no_takeover` |
| 7 | Restart with QUEUED → claimable | ✅ | `test_restart_queued_claimable` |
| 8 | Restart with WAITING_EXTERNAL → persisted, no LLM | ✅ | `test_restart_waiting_external_persists_no_llm` |
| 9 | Restart with DONE → unchanged | ✅ | `test_restart_done_unchanged` |
| 10 | Restart with FAILED → unchanged | ✅ | `test_restart_failed_unchanged` |
| 11 | Restart with BLOCKED → not auto-opened | ✅ | `test_restart_blocked_not_reopened` |
| 12 | RUNNING + live evidence → no unsafe duplicate takeover | ✅ | `test_running_live_process_no_unsafe_takeover` |
| 13 | RUNNING + stale/dead evidence → bounded recovery | ✅ | `test_running_terminal_evidence_bounded_recovery` |
| 14 | RUNNING + ambiguous evidence → no unsafe spawn | ✅ | `test_running_ambiguous_evidence_no_unsafe_spawn` |
| 15 | Crash before action persist → no fabricated completion | ✅ | `test_crash_before_action_persist_no_fabricated_done` (ambiguous RUNNING → LOST, never DONE); the full action/spawn-window proof is `test_sigterm_mid_pass_aborts_before_spawn` (F6) |
| 16 | Crash after action persist → reconciliation idempotent | ✅ | `test_reconcile_after_restart_idempotent` (reconcile is idempotent across restarts) |
| 17 | External-wait wake not duplicated after restart | ✅ | `test_external_wait_wake_not_duplicated` |
| 18 | Final DONE notification not duplicated by restart | ✅ | `test_done_notification_dedup_on_restart` |
| 19 | SIGTERM → bounded STOPPING | ✅ | `test_sigterm_bounded_stopping` |
| 20 | Shutdown starts no new jobs | ✅ | `test_shutdown_starts_no_new_jobs` |
| 21 | Shutdown never marks unfinished PASS/DONE | ✅ | `test_shutdown_never_marks_unfinished_done` |
| 22 | Idle loop doesn't busy-spin | ✅ | `test_idle_loop_does_not_busy_spin` |
| 23 | External wait holds no active LLM | ✅ | `test_external_wait_holds_no_llm` |
| 24 | Scheduler loop contains exceptions safely | ✅ | `test_loop_contains_pass_exception` |
| 25 | Health distinguishes READY/DEGRADED/FAILED w/o job change | ✅ | `test_health_distinguishes_states_without_job_change` |
| 26 | Malformed trusted config fail-closed | ✅ | `test_config_*_fails_closed` (JSON/type/ephemeral) |
| 27 | Unavailable persistent store fail-closed | ✅ | `test_build_service_unavailable_store_raises`, `test_main_malformed_config_exits_nonzero` |
| 28 | Evidence MAC key missing → no trusted PASS signing | ✅ | `test_evidence_mac_key_missing_fails_closed` |
| 29 | Writer cannot control key/store authority path | ✅ | `test_writer_cannot_control_key_source` (signature: no store/agent input); child-env leak is closed + tested by F4 (`test_*_passes_sanitized_env`) |
| 30 | Service definition has no embedded secret | ✅ | `test_unit_file_has_no_embedded_secret` |
| 31 | Service runs as user, not root | ✅ | `test_unit_file_runs_as_user_not_root` |
| 32 | No new public network listener | ✅ | `test_unit_file_no_public_listener`, `test_new_modules_open_no_network_listener` |
| 33 | Resource governor remains binding after background dispatch | ✅ | `test_background_loop_honors_resource_governor` |
| 34 | Phase-D context rules unchanged | ✅ | `test_phase_d_context_rules_unchanged` (+ full D regression) |
| 35 | Phase-E routing unchanged | ✅ | `test_phase_e_routing_unchanged` (+ full E regression) |
| 36 | Phase-F test assurance unchanged | ✅ | `test_phase_f_test_assurance_unchanged` (+ full F regression) |
| 37 | Simulated reboot invalidates old process liveness | ✅ | `test_reboot_same_pid_same_ticks_still_stale` |
| 38 | Duplicate startup/reconcile idempotent | ✅ | `test_reconcile_after_restart_idempotent`, `test_released_owner_reacquire_is_clean` |
| 39 | Repeated crash/start doesn't open immutable terminal jobs | ✅ | `test_repeated_restart_never_reopens_terminal` |
| 40 | No systemd enable/start by G1 | ✅ | `test_g1_does_not_activate_systemd` |

## Fix-round regression (F1–F8) — independently confirmed, now tested

| Finding | Claim | Status | Where |
|---|---|---|---|
| F1 | Loop continues multi-step RUNNING jobs + periodic recovery of expired-lease RUNNING jobs | ✅ | `test_run_loop_drives_multistep_job_to_done`; `test_loop_periodic_recovery_takes_over_expired_lease` |
| F2 | Monotonic-revision CAS (no ABA under a frozen clock) | ✅ | `test_cas_revision_prevents_aba_concurrent_takeover`; `test_heartbeat_and_release_bump_revision` |
| F3 | Shared-store host identity (no blind cross-host takeover) + fence-loss stops the runtime | ✅ | `test_shared_store_foreign_host_is_not_taken_over`; `test_different_boot_different_host_is_ambiguous_not_dead`; `test_fence_loss_stops_runtime_without_further_passes` |
| F4 | Minimal allowlisted spawn environment (no evidence-key leak) | ✅ | `test_agent_spawn_env_strips_evidence_key`; `test_scope_start_in_scope_passes_sanitized_env`; `test_launcher_spawn_passes_sanitized_env` |
| F5 | Path/config validation fail-closed (XDG /tmp, symlink→/tmp, relative, DB-path-outside-state, NaN/Inf, unknown-field) | ✅ | `test_config_xdg_tmp_fails_closed`; `test_config_symlink_to_tmp_fails_closed`; `test_config_relative_xdg_resolves_under_home`; `test_config_db_path_outside_state_fails_closed`; `test_pos_float_rejects_nan_and_inf`; `test_config_unknown_field_fails_closed` |
| F6 | SIGTERM mid-pass aborts before spawn (no launcher/enforcer call, job consistent) | ✅ | `test_sigterm_mid_pass_aborts_before_spawn` |
| F7 | FAILED preserved; non-zero exit; structural escalation; waits run on error passes | ✅ | `test_failed_state_preserved_by_finalize`; `test_main_returns_failed_exit_code`; `test_repeated_scheduler_errors_escalate_to_failed`; `test_transient_error_stays_degraded`; `test_external_waits_run_even_on_pass_error` |
| F8 | Acceptance matrix honestly reflects what is proven | ✅ | this table + `test_phase_g1_fix_round.py` |

## Verification evidence (self-run)

- `python3 -m pytest tests/test_phase_g1*.py -q` → **78 passed**.
- `python3 -m pytest tests/ -q` → **2470 passed** (no regressions).
- `python3 -m pytest tests/test_phase_f*.py tests/test_phase_e*.py tests/test_phase_d*.py tests/test_phase_c*.py tests/test_phase_b*.py -q` → **1136 passed** (B/C/D/E/F unchanged).
- `git diff --check` → clean.
- No `shell=True`/`eval`/`exec`/`subprocess`/`Popen` in new product code.
- `systemd-analyze verify g1-systemd/argent-supervisor.service` → exit 0 (no errors).
- No `systemctl` enable/start/`daemon-reload`/`loginctl`/`systemd-run` in new code.

## G2 activation (owner actions, NOT performed by G1)

1. Review and set `WorkingDirectory=` to the checkout containing `argent_core/`.
2. Provision the key file and `EnvironmentFile` (`~/.config/argent/service.env`
   with `ARGENT_EVIDENCE_MAC_KEY_FILE=...`) outside the agent write area.
3. `systemctl --user daemon-reload`
4. `systemctl --user enable --now argent-supervisor.service`
5. Controlled live-restart test and (optionally) a WSL reboot test.

# PHASE C1 — Resource Policy + Preflight + Admission

Phase C1 implementiert **nur die sichere Entscheidungsgrundlage** des Resource
Governors (ARGENT ARCHITECTURE V1 FINAL §9).  **Kein** Execution Enforcement:
cgroup/systemd-run/`prlimit`-Prozesslimits, Kill und OOM-Recovery kommen erst in
Phase C2.  Keine State-Machine-Erweiterung, keine neuen Primary States, kein
Background Service, kein Model-Routing, keine Context Packs, keine externen
CI-Aktionen, keine WSL-/Swap-/System-Config-Änderung.

## 1. Analyse-Antworten (Supervisor, read-only)

* **A — Admission-Punkt:** `Scheduler.run_pass` (`argent_core/scheduler.py`),
  nach dem atomaren Claim in `_resolve_target`, VOR
  `_supervisor.reconcide`/`perform_next_safe_action_if_required`.  Das Gate ist:
  **kein Spawn ohne ALLOW** (bzw. PREFER_EXTERNAL = lokal weiter wie ALLOW).
* **B — Wiederverwendet:** `Store.claim_job`/`claim_next_job`/`_job_is_claimable`
  (DEFER = Job bleibt QUEUED + `next_eligible_at`, kein Claim),
  `Store.enqueue_job` (F2-Holder-Requeue), `job_state.ErrorClass.RESOURCE`
  (existiert bereits), `ProcessIdentityProvider`-Muster (injektierbare Reader),
  Store-Migrationsmuster (`SCHEMA_VERSION`, additive `ALTER TABLE`).
* **C — Keine neue Tabelle:** bounded last-outcome-Spalten additiv an
  `supervisor_jobs` (siehe §6).
* **D — Linux-Reads:** `/proc/meminfo`, `/proc/loadavg`, `os.statvfs`,
  `/proc/mounts` (tmpfs-Erkennung für `/tmp`), `os.cpu_count()`.  PSI optional
  (`/proc/pressure/*`), nur als optionales Feld.
* **E — UNKNOWN + fail-closed:** nicht lesbare kritische Evidence →
  UNKNOWN → MEDIUM/HEAVY/EXCLUSIVE fail-closed (kein Start); LIGHT nur, wenn
  die Hostreserve sicher beweisbar ist.

## 2. Modulübersicht

| Datei | Inhalt |
|---|---|
| `argent_core/resource_policy.py` (NEU) | `ResourceClass`, `ResourceLimits`, `ResourcePolicy`, `required_host_reserve`, `effective_memory_max` (rein, deterministisch, keine I/O) |
| `argent_core/host_snapshot.py` (NEU) | `HostResourceSnapshot`, strikte Parser, `HostSnapshotProvider` (injizierbare Reader, crasht nie) |
| `argent_core/resource_governor.py` (NEU) | `ResourceReasonCode`, `AdmissionVerdict`, `AdmissionDecision`, `ResourceGovernor.decide` |
| `argent_core/store.py` (GEÄNDERT) | `SCHEMA_VERSION` 8→9; additive C1-Spalten + Migration; `_ENUM_FIELDS` + `resource_class`; `enqueue_job`-Parameter |
| `argent_core/job_state.py` (GEÄNDERT) | `QueueReason.RESOURCE_DEFERRED`/`RESOURCE_DENIED` (kein neuer Primary State) |
| `argent_core/scheduler.py` (GEÄNDERT) | Preflight-Gate in `run_pass` (frischer Claim als früher Filter + **verpflichtender** Spawn-naher Gate bei `SPAWN_RUN`), Default-Store-Reader für Concurrency-Evidence, injizierbarer Governor/Provider |
| `argent_core/supervisor.py` (GEÄNDERT) | `create_job(resource_class=...)`; `__init__`-Injection von Governor/Provider |
| `docs/PHASE_C1_NOTES.md` (NEU) | diese Datei |

## 3. Policy-Defaults (versionierte `ResourcePolicy`, `policy_version="1"`)

| Klasse | MemoryHigh | MemoryMax | SwapMax | CPU | Timeout |
|---|---:|---:|---:|---:|---|
| LIGHT | 768 MiB | 1 GiB | 256 MiB | 100 % | 15 min |
| MEDIUM | 2 GiB | 2.5 GiB | 512 MiB | 200 % | 45 min |
| HEAVY | 3 GiB | 4 GiB | 1 GiB | 300 % | 120 min |
| EXCLUSIVE | 4.5 GiB | 5.5 GiB | 1.5 GiB | 400 % | step-spezifisch (None) |

Globale Defaults: `minimum_host_reserve_bytes=1.5 GiB`,
`host_reserve_ram_ratio=0.20`, `swap_warning_ratio=0.70`,
`swap_block_ratio=0.85`, `minimum_disk_free_bytes=10 GiB`,
`minimum_disk_free_ratio=0.15`, `large_temp_factor=2.0`,
`max_writers_global=1`, `max_light=2`, `max_medium=1`, `max_heavy=1`,
`defer_retry_seconds=300`, `load_multiplier_5min=1.5`.

## 4. Hostreserve-Formel

```text
required_host_reserve(total) = max(1.5 GiB, 0.20 * total)
effective_memory_max(ceiling, avail, reserve) = min(ceiling, avail - reserve), floor 0
```

Crossover bei 7.5 GiB (20 % == 1.5 GiB).  Realer Host (7.7 GiB) → Reserve ≈ 1.54 GiB.

## 5. Decision-Typen / Reason-Codes

* Veredict: `ALLOW` / `DEFER` / `DENY_LOCAL` / `PREFER_EXTERNAL`.
* Reason-Codes (exakt): `OK`, `INSUFFICIENT_MEMORY_RESERVE`, `SWAP_PRESSURE`,
  `DISK_LOW`, `TMPFS_POLICY_VIOLATION`, `LOAD_PRESSURE`, `CONCURRENCY_LIMIT`,
  `RESOURCE_EVIDENCE_UNKNOWN`, `LOCAL_CAPACITY_INSUFFICIENT`,
  `EXTERNAL_CI_PREFERRED`.

Entscheidungsreihenfolge (`ResourceGovernor.decide`): evidence-unknown →
concurrency → swap → disk → tmpfs → hostreserve → load → PREFER_EXTERNAL →
sonst ALLOW.

## 6. Scheduler-Integration

Zwei Admission-Punkte in `run_pass`:

1. **Früher Filter (frischer Claim, `held=False`):** nach `_resolve_target` (Claim
   erfolgreich), VOR `reconcile` — verhindert unnötige Dispatches.
2. **Verbindlicher Spawn-naher Gate (`SPAWN_RUN`):** nach `reconcile`, VOR
   `perform_next_safe_action_if_required`, **unabhängig von `held`** — ein frischer
   Preflight läuft unmittelbar vor der einzigen spawnenden Aktion.  Ein späterer
   Pass (Continuation/Restart/Takeover/WAIT-WAKE) kann so nie unter einem
   inzwischen geänderten Memory-/Swap-/Disk-Zustand spawnen.

An beiden Punkten gilt:

* `ALLOW` → bisheriger Pfad unverändert.
* `DEFER` → `enqueue_job(queue_reason=RESOURCE_DEFERRED, next_eligible_at=now+300s,
  error_class=RESOURCE, error_code=<ReasonCode>, holder-CAS, last_resource_*)`,
  kein Spawn.
* `DENY_LOCAL` → `enqueue_job(queue_reason=RESOURCE_DENIED,
  next_eligible_at=now+24h, error_class=RESOURCE,
  error_code=LOCAL_CAPACITY_INSUFFICIENT, holder-CAS, last_resource_*)`, kein
  identischer Sofort-Retry.
* `PREFER_EXTERNAL` → nur `last_resource_*` persistiert (über die transaktionale
  `persist_resource_decision`-Holder-CAS-Operation), lokal weiter wie ALLOW.

`reconcile_after_restart` unverändert.  Persistierte `last_resource_*` sind
**reines Audit** — jede neue Admission läuft erneut durch den Preflight.

## 7. Explizit NICHT implementiert (C2)

cgroup/systemd-scope-Ceilings, `prlimit`-Prozesslimits, Kill/OOM-Recovery,
`effective_memory_max` als Enforcement, externe CI-Trigger.

**Hinweis (Korrektur aus der C1-Fix-Runde):** der Spawn-nahe Preflight vor
`self._launcher.spawn(...)` ist NICHT mehr „optional" — er ist **verpflichtend**
und der verbindliche Admission-Punkt vor dem Spawn (siehe §6, Punkt 2).

## 8. Abweichungen vom Plan (mit Begründung)

* **Disk-Schwelle nur auf `root_free`** (nicht auf `workspace_free`): der
  Produktiv-Workspace liegt auf der persistenten Root-FS; Test-Workspaces unter
  `tmp_path` (= `/tmp`, tmpfs) haben < 10 GiB frei und würden sonst fälschlich
  `DISK_LOW` auslösen.  `workspace_free` bleibt für die Faktor-2-Regel bei
  großen Temp-Daten aktiv.  (Messdaten/Constraints schlagen Eleganz.)
* **`QueueReason.RESOURCE_DEFERRED`/`RESOURCE_DENIED`** wurden als Enum-Werte
  ergänzt: die bestehende Code-Validierung `_validate_enum_fields` (nicht nur der
  fehlende DB-CHECK) hätte einen rohen String sonst abgewiesen.  Kein neuer
  Primary State.
* **`DENY_LOCAL`-Horizont = 24 h** (`DENY_LOCAL_RETRY_SECONDS`), da
  `next_eligible_at=None` im bestehenden `_job_is_claimable` sofort wieder
  claimbar wäre (identischer Retry).  „weit in Zukunft" wird als bounded
  far-future umgesetzt.

## 9. Tests

`tests/test_phase_c1_policy.py`, `_snapshot.py`, `_hostreserve.py`, `_swap.py`,
`_disk.py`, `_concurrency.py`, `_admission.py`, `_scheduler.py`, `_restart.py`,
`_memory_pressure.py` (+ `tests/c1_helpers.py`).  Deterministisch, Fake-Reader,
FakeClock, keine Live-Host-Stresstests.

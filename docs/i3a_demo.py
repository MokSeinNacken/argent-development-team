"""Phase I3-A local demo — External Action Broker (no network, no real writes).

Demonstrates the full provider-neutral external-write trust boundary against a
deterministic FakeGitHubAdapter:

1. Integrated Candidate -> ExternalActionRequest -> Policy -> ALLOW_AUTONOMOUS
   -> Broker -> FakeGitHubAdapter -> simulated feature-branch push + PR
   creation -> provider object ids -> secret-free audit persisted.
2. Provider-accepted PR + crash-before-SUCCESS-persistence + restart/reconcile
   -> the existing Argent-owned PR is detected, NO duplicate PR is created.
3. A MERGE request -> OWNER_GATE_REQUIRED (SENSITIVE class; never autonomous).

Run from the worktree root:
    PYTHONPATH=tests python3 docs/i3a_demo.py

Exits 0 on success; prints a graceful note if git is unavailable.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT))

from argent_core import Core, OWNER_SOURCE  # noqa: E402
from argent_core.external_action_broker import (  # noqa: E402
    ExternalActionBroker,
    ExternalActionAllowlist,
    AllowlistEntry,
    PolicyDecision,
)
from argent_core.external_provider_adapter import FakeGitHubAdapter  # noqa: E402
from argent_core.supervisor import Supervisor  # noqa: E402
from mock_supervisor_runtime import (  # noqa: E402
    FakeRunLauncher,
    FakeRunStatusProvider,
)
from i3a_helpers import (  # noqa: E402
    TEST_MAC_KEY,
    default_standing_policy,
    init_repo,
    make_holder,
    make_integrated_source,
    make_provenance,
)


def _short(sha: str) -> str:
    return sha[:12]


def main() -> int:
    if shutil.which("git") is None:
        print("git unavailable — skipping demo (should not happen).")
        return 0

    tmp = tempfile.mkdtemp(prefix="i3a-demo-")
    try:
        core = Core(os.path.join(tmp, "argent.db"))
        project = core.create_project("p", OWNER_SOURCE)
        sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
        repo = init_repo(os.path.join(tmp, "repo"))

        jid, cid, head, tid = make_integrated_source(
            core, project, sup, repo, branch="main")
        prov = make_provenance(jid, cid, repo, head, branch="main")

        adapter = FakeGitHubAdapter(provider_name="github")
        allowlist = ExternalActionAllowlist(entries=(AllowlistEntry(
            provider="github", account="MokSeinNacken",
            repositories=frozenset({repo}),
            permitted_actions=frozenset({
                "push_feature_branch", "create_pull_request",
                "read_ref", "read_pull_request", "read_checks",
                "merge_pull_request",
            }),
            branch_namespaces=frozenset({"argent/"}),
            pr_targets=frozenset({"main"}),
        ),))
        broker = ExternalActionBroker(core._store, adapter=adapter,
                                      allowlist=allowlist,
                                      standing_policy=default_standing_policy(),
                                      mac_key=TEST_MAC_KEY)
        holder, epoch = make_holder(core, project, sup)

        # --- 1. Feature-branch push (ALLOW_AUTONOMOUS) -----------------------
        branch = f"argent/{tid}-feature"
        req = broker.create_request(
            provider="github", account="MokSeinNacken",
            action="push_feature_branch", repository=repo, resource_ref=branch,
            requested_scope="write", parameters={"branch": branch, "sha": head},
            idempotency_key="demo-push-1", provenance=prov)
        decision, reason = broker.evaluate_policy(req)
        print(f"[1] request {req['request_id'][:16]} policy={decision.value} "
              f"({reason}) class={req['policy_class']}")
        assert decision is PolicyDecision.ALLOW_AUTONOMOUS
        req = broker.authorize_autonomous(req["request_id"])
        req = broker.execute(req["request_id"], holder_job_id=holder,
                             holder_lease_epoch=epoch)
        assert req["state"] == "SUCCEEDED"
        print(f"    push SUCCEEDED -> remote {branch} = {_short(req['provider_object_id'])}")

        # --- 1b. Create PR (ALLOW_AUTONOMOUS) ---------------------------------
        req = broker.create_request(
            provider="github", account="MokSeinNacken",
            action="create_pull_request", repository=repo, resource_ref=branch,
            requested_scope="write",
            parameters={"head_branch": branch, "base_branch": "main",
                        "head_sha": head, "title": "Integrate feature",
                        "body": "Deterministic demo PR."},
            idempotency_key="demo-pr-1", provenance=prov)
        decision, reason = broker.evaluate_policy(req)
        assert decision is PolicyDecision.ALLOW_AUTONOMOUS
        req = broker.authorize_autonomous(req["request_id"])
        req = broker.execute(req["request_id"], holder_job_id=holder,
                             holder_lease_epoch=epoch)
        assert req["state"] == "SUCCEEDED"
        pr_number = req["provider_object_id"]
        print(f"    create PR SUCCEEDED -> PR #{pr_number} (head {branch})")

        audit = core._store.list_external_action_audit(req["request_id"])
        print(f"    audit events: {[a['event_type'] for a in audit]} "
              f"(secret-free, {len(audit)} rows)")

        # --- 2. Crash-before-SUCCESS-persistence + reconcile ------------------
        # The PR was accepted provider-side, but the request row is left
        # EXECUTING (simulated crash before the SUCCEEDED persist).
        crash_pr = broker.create_request(
            provider="github", account="MokSeinNacken",
            action="create_pull_request", repository=repo, resource_ref=branch,
            requested_scope="write",
            parameters={"head_branch": branch, "base_branch": "main",
                        "head_sha": head, "title": "Integrate feature (2)",
                        "body": ""},
            idempotency_key="demo-pr-2", provenance=prov)
        crash_pr = broker.authorize_autonomous(crash_pr["request_id"])
        core._store.transition_external_action_request(
            crash_pr["request_id"], from_state="AUTHORIZED",
            to_state="EXECUTING", expected_revision=crash_pr["revision"])
        # Simulate the provider having ACCEPTED the crash PR (idempotency
        # marker = the request's idempotency key) before the crash.
        adapter.pull_requests[2] = {
            "number": 2, "repo": repo, "head_branch": branch,
            "base_branch": "main", "head_sha": head, "state": "open",
            "idempotency_key": "demo-pr-2", "argent_owned": True,
        }
        # "restart": a fresh broker against the same store + adapter.
        broker2 = ExternalActionBroker(core._store, adapter=adapter,
                                       allowlist=allowlist,
                                       standing_policy=default_standing_policy(),
                                       mac_key=TEST_MAC_KEY)
        n_before = len(adapter.pull_requests)
        crash_pr = broker2.reconcile(crash_pr["request_id"], holder_job_id=holder,
                                     holder_lease_epoch=epoch)
        assert crash_pr["state"] == "SUCCEEDED"
        assert len(adapter.pull_requests) == n_before  # no duplicate PR
        print(f"[2] reconcile detected existing PR #{crash_pr['provider_object_id']}; "
              f"no duplicate (PR count {len(adapter.pull_requests)})")

        # --- 3. MERGE request -> OWNER_GATE_REQUIRED --------------------------
        merge_req = broker.create_request(
            provider="github", account="MokSeinNacken",
            action="merge_pull_request", repository=repo,
            resource_ref=str(pr_number), requested_scope="write",
            parameters={"number": int(pr_number)},
            idempotency_key="demo-merge-1", provenance=prov)
        decision, reason = broker.evaluate_policy(merge_req)
        print(f"[3] MERGE request policy={decision.value} ({reason})")
        assert decision is PolicyDecision.OWNER_GATE_REQUIRED

        print("\nI3-A demo OK (no network, no real provider writes).")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

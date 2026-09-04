"""Shared fixtures and helpers for the argent_core test suite."""

import sys
from pathlib import Path

import pytest

# Make the project root importable (also used by the crash subprocess helper).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from argent_core import Core, OWNER_SOURCE, Role, role_source  # noqa: E402


def pytest_configure(config):
    """Register the ``host_acceptance`` marker.

    ``host_acceptance`` marks tests that prove OPERATIONAL_HOST_ACCEPTANCE on a
    real host (live systemd user-scope + cgroup delegation, a sibling-checkout
    consistency check, live credential probes, a real checkpoint, or an
    installed systemd unit).  A stock GitHub runner cannot faithfully represent
    such live-host state, so the portable CI command runs
    ``-m "not host_acceptance"`` to exclude them; they still run in the local
    full suite on the development host.
    """
    config.addinivalue_line(
        "markers",
        "host_acceptance: proves live-host (systemd/cgroup/checkout/credential) "
        "state that a stock CI runner cannot represent; excluded from portable CI.",
    )


LEAD = role_source(Role.LEAD)
ANALYST = role_source(Role.ANALYST)
IMPLEMENTER = role_source(Role.IMPLEMENTER)
QA = role_source(Role.QA)
REVIEWER = role_source(Role.REVIEWER)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def core(db_path):
    c = Core(db_path)
    yield c
    c.close()


@pytest.fixture
def project(core):
    return core.create_project("demo", OWNER_SOURCE)


@pytest.fixture
def task(core, project):
    return core.create_task(project.id, "demo-task", OWNER_SOURCE)


def events_of(core, type_=None, task_id=None):
    evs = core.list_events(OWNER_SOURCE, task_id=task_id)
    if type_ is not None:
        evs = [e for e in evs if e.type == type_]
    return evs


def start_lead(core, task_id):
    """Start the lead role run (bootstrap) and return it."""
    return core.start_role(task_id, Role.LEAD, LEAD)


def pipeline_to(core, task_id, target: Role):
    """Advance the role pipeline until ``target`` is the active role run.

    Starts the lead if no role run is active, then completes roles and follows
    the deterministic handoff chain until the requested role is active.
    """
    active = core.queries.get_active_role_run(task_id)
    if active is None:
        active = core.start_role(task_id, Role.LEAD, LEAD)
    while active.role is not target:
        core.complete_role(active.id, role_source(active.role))
        handoffs = core.queries.list_handoffs(task_id)
        nxt = handoffs[-1].to_role
        active = core.start_role(task_id, nxt, LEAD)
    return active

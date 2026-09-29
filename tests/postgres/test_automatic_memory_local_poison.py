"""A poison ``claude_code_local`` unit costs only itself (spec § Requirements FR 5,
FR 10; AC 7's claim-time half; #215's shape, now with local rows).

Seams under test:

- **One refused local unit backs off; its neighbours are accepted.** One partition
  holds a poison local unit, a healthy local unit and a healthy runtime unit. The
  database refuses the poison unit's acceptance; its savepoint rolls back, it counts
  one attempt through ``defer_unit`` and backs off 5 seconds, both neighbours become
  live memories in the same drain, and the drain job succeeds.
- **The fifth attempt settles ``gap/acceptance_failed``.** The deferral's follow-ups
  carry the poison unit through the extraction schedule (5, 20, 80, 320 s) to its
  fifth attempt, which settles it ``gap/acceptance_failed`` (not
  ``extraction_failed``). No drain job is ever retried, and no memory or receipt
  exists for the poison unit, while its neighbours keep theirs.

Every drain runs through a real ``visit_workspace`` with the job kinds and the
subscription taken off Recallatron's ``MANIFEST``, as
``test_automatic_memory_limits.py``'s #215 tests do; the forced refusal is theirs.
Every text is synthetic, and every evidence and enrollment row is removed in teardown,
including the ``gap/acceptance_failed`` one, which would make ``rheo doctor`` FAIL for
the workspace (#238's rule).
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.evidence import EvidenceWorkspace, enable_recording
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.registry import register_harness
from rheo_contracts import ContextPurpose, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.evidence.local_authority import LOCAL_PRODUCER_KIND
from rheo_core.evidence.providers import FAKE_EXTRACTION_MARKER
from rheo_core.evidence.record import NewEvidence
from rheo_core.operations import register_core_operations
from rheo_core.storage.evidence_enrollment_tables import evidence_enrollment
from rheo_core.storage.evidence_tables import (
    OUTCOME_ACCEPTANCE_FAILED,
    OUTCOME_ACTIVE,
    STATE_GAP,
    STATE_PENDING,
    STATE_SETTLED,
    evidence_unit,
)
from rheo_recallatron.configuration import MODULE_ID
from rheo_recallatron.storage import tables as memory_tables
from sqlalchemy import delete, inspect
from sqlalchemy.engine import Row

from postgres.test_automatic_memory_acceptance import (
    _enqueue_drain,
    _new_evidence,
    _rows,
)
from postgres.test_automatic_memory_drain import _jobs
from postgres.test_automatic_memory_limits import (
    _POISON_ROW,
    _POISON_TAG,
    _at,
    _poisoned_acceptance,
)
from postgres.test_automatic_memory_mixed_partition import (
    enroll,
    local_evidence,
    record,
)

pytestmark = pytest.mark.postgres

_POISON_BODY: Final = f"{FAKE_EXTRACTION_MARKER} The {_POISON_TAG} lanterns."
_LOCAL_BODY: Final = f"{FAKE_EXTRACTION_MARKER} The rake is behind the shed."
_RUNTIME_BODY: Final = f"{FAKE_EXTRACTION_MARKER} The hose reel is by the tap."


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()


@pytest.fixture(autouse=True)
def clean_evidence_rows(cluster: ClusterSession, workspace: UUID) -> Iterator[None]:
    """#238's ``clean_acceptance_failure``, widened: every evidence row this test
    wrote (the ``gap/acceptance_failed`` one included) and every enrollment row."""
    yield
    database = cluster.registry_row(workspace).database_name
    engine = cluster.backend.pools.engine_for(database)
    inspector = inspect(engine)
    with engine.begin() as connection:
        if inspector.has_table("evidence_unit", schema="core"):
            connection.execute(delete(evidence_unit))
        if inspector.has_table("evidence_enrollment", schema="core"):
            connection.execute(delete(evidence_enrollment))


@pytest.fixture
def ev(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[EvidenceWorkspace]:
    """Recallatron installed and enabled, and recording conditions 1 and 2 on."""
    with loaded_probe_modules(monkeypatch, MODULE_ID):
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, MODULE_ID)
        evidence = EvidenceWorkspace(cluster, workspace, owner_account_id)
        enable_recording(monkeypatch, evidence)
        yield evidence


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


def _poisoned_partition(
    ev: EvidenceWorkspace, *, now: datetime
) -> tuple[NewEvidence, NewEvidence, NewEvidence]:
    """The poison local unit first in claim order, then a healthy local unit and a
    healthy runtime unit of the same partition; answers them in that order."""
    enrollment_id = enroll(ev)
    ctx = ev.context(ContextPurpose.RESPOND)
    poison = local_evidence(
        ctx, _POISON_BODY, enrollment_id=enrollment_id, at=now - timedelta(seconds=15)
    )
    local = local_evidence(
        ctx, _LOCAL_BODY, enrollment_id=enrollment_id, at=now - timedelta(seconds=10)
    )
    runtime = _new_evidence(ctx, _RUNTIME_BODY, at=now - timedelta(seconds=5))
    record(ev, [poison, local, runtime], at=now)
    return poison, local, runtime


def _unit(ev: EvidenceWorkspace, native_key: str) -> Row[Any]:
    [row] = [u for u in _rows(ev, evidence_unit) if u.native_key == native_key]
    return row


def _receipt_keys(ev: EvidenceWorkspace) -> dict[str, str]:
    """Each receipt's external source key and its producer kind."""
    return {
        row.external_source_key: row.producer_kind
        for row in _rows(ev, memory_tables.source_receipt)
    }


def test_a_poison_local_unit_backs_off_and_its_mixed_neighbours_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
    ev: EvidenceWorkspace,
    now: datetime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The local neighbour and the runtime neighbour each become one live memory in
    the drain that refused the poison unit, which counts one attempt and backs off;
    the drain job succeeds and the next one is due at the poison unit's
    ``retry_after``. Nothing the refusal quoted reaches a log line or a job row."""
    refused = _poisoned_acceptance(monkeypatch)
    poison, local, runtime = _poisoned_partition(ev, now=now)
    first = _enqueue_drain(ev, at=now)
    _at(monkeypatch, ev, now)

    assert refused == ["note"]
    for neighbour in (local, runtime):
        row = _unit(ev, neighbour.native_key)
        assert (row.state, row.outcome) == (STATE_SETTLED, OUTCOME_ACTIVE)
    assert _receipt_keys(ev) == {
        local.native_key: LOCAL_PRODUCER_KIND,
        runtime.native_key: "rheo_runtime",
    }
    assert len(_rows(ev, memory_tables.memory)) == 2

    held = _unit(ev, poison.native_key)
    assert (held.state, held.outcome) == (STATE_PENDING, None)
    assert held.extraction_attempts == 1
    assert held.retry_after == now + timedelta(seconds=5)
    [drain] = [job for job in _jobs(ev) if job.id == first]
    assert (drain.state, drain.attempts, drain.last_error) == ("succeeded", 1, None)
    queued = [job for job in _jobs(ev) if job.state == "queued"]
    assert [job.next_run_at for job in queued] == [held.retry_after]
    assert _POISON_ROW not in caplog.text
    assert all(job.last_error is None for job in _jobs(ev))


def test_a_poison_local_unit_settles_acceptance_failed_on_its_fifth_attempt(
    monkeypatch: pytest.MonkeyPatch,
    ev: EvidenceWorkspace,
    now: datetime,
) -> None:
    """Five drains on the extraction schedule, none retried; the fifth settles the
    poison unit ``gap/acceptance_failed`` with its body cleared, and no memory or
    receipt exists for it while its neighbours keep theirs."""
    refused = _poisoned_acceptance(monkeypatch)
    poison, local, runtime = _poisoned_partition(ev, now=now)
    _enqueue_drain(ev, at=now)

    at = now
    for attempt, step in enumerate((5, 20, 80, 320, None), start=1):
        _at(monkeypatch, ev, at)
        held = _unit(ev, poison.native_key)
        assert held.extraction_attempts == attempt
        if step is None:
            break
        assert (held.state, held.retry_after) == (
            STATE_PENDING,
            at + timedelta(seconds=step),
        )
        queued = [job for job in _jobs(ev) if job.state == "queued"]
        assert [job.next_run_at for job in queued] == [held.retry_after]
        at = held.retry_after

    assert len(refused) == 5
    settled = _unit(ev, poison.native_key)
    assert (settled.state, settled.outcome) == (STATE_GAP, OUTCOME_ACCEPTANCE_FAILED)
    assert settled.body is None
    assert settled.settled_at == at
    assert poison.native_key not in _receipt_keys(ev)
    assert set(_receipt_keys(ev)) == {local.native_key, runtime.native_key}
    assert len(_rows(ev, memory_tables.memory)) == 2
    assert {(job.state, job.attempts, job.last_error) for job in _jobs(ev)} == {
        ("succeeded", 1, None)
    }

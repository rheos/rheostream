"""Automatic memory, acceptance criteria 5 to 7 and card item 5: the four limits, the
retention recheck reached at the seam, extraction faults, and the success audit's
rollback (spec § Acceptance criteria 5-7, Edge Case 4's reachability note, § Success
audit; card acceptance item 5; R11).

Seams under test:

- **AC-6: ``_born_expired`` reached, not ``_window_state``.** One fixture for all
  three legs: the operator raises ``max_pending_hours`` to 72, the workspace keeps
  one day of retention, and each unit is recorded 30 hours ago. Its pending window
  is still open (42 hours left), so the drain reaches policy, and only the leg with
  the gate on and a workspace audience is born expired.
- **AC-7: an extraction fault stays in its partition.** It moves only the row's own
  ``extraction_attempts``/``retry_after``; every drain job succeeds on its first
  attempt; the fifth failure settles ``gap/extraction_failed``; and a healthy
  partition recorded after a failing one settles before the failing one is due again.
- **Card item 5: all or nothing.** A raising sink, or no sink, rolls back the memory,
  receipt, ``recorded`` event and ``core.evidence.accept`` row together and leaves
  the unit pending with its body; a ``noop`` writes no audit row, so a raising sink
  never touches it.
- **#215: one poison unit costs only itself.** A database refusal of one unit's
  acceptance rolls back only that unit's savepoint; its partition neighbour is
  accepted in the same drain, the poison unit backs off on the extraction schedule,
  and its fifth attempt settles ``gap/acceptance_failed``.
- **#217: a workspace-wide acceptance fault is found before the model call.** With no
  audit sink, or no usable retention policy, two drains in a row fail without calling
  the provider once.
- **#221: only the accept step is contained.** A refused audit insert rolls the whole
  drain back and fails the job attempt, while two refused units in one batch are
  each still deferred and counted, so the #215 jam cannot come back.

Every drain runs through a real ``visit_workspace`` with the job kinds and the
subscription taken off Recallatron's ``MANIFEST``. Every text is synthetic.
"""

import dataclasses
import importlib
import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.evidence import EvidenceWorkspace, enable_recording, probe_registry
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.registry import register_harness
from rheo_contracts import ContextPurpose, Role, WorkspaceContext
from rheo_core.audit import install_sink, reset_sinks, sink_for
from rheo_core.boundary import context_for_harness
from rheo_core.evidence import ClaimedBatch, claim_units, settle_unit
from rheo_core.evidence.extract import DigestBatch
from rheo_core.evidence.providers import (
    FAKE_EXTRACTION_MARKER,
    FAKE_PROVIDER,
    PROVIDERS,
    providers,
)
from rheo_core.evidence.record import (
    MAX_BYTES_KEY,
    MAX_RECORDS_KEY,
    NewEvidence,
    RecordAttempt,
    record_evidence,
)
from rheo_core.evidence.service import ACCEPTANCE_FAILED_LOG, EvidenceAuditUnwritable
from rheo_core.operations import (
    CORE_MODULE_ID,
    HARNESS_MODULE_ID,
    register_core_operations,
)
from rheo_core.settings import ValueType
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.evidence_tables import evidence_unit
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_recallatron import automatic
from rheo_recallatron.automatic import ACCEPTANCE_DEFERRED_LOG
from rheo_recallatron.configuration import (
    MODULE_ID,
    RETENTION_DAYS_KEY,
    RETENTION_EXPIRE_BY_AGE_KEY,
)
from rheo_recallatron.source_units import accept_source_unit
from rheo_recallatron.storage import tables as memory_tables
from sqlalchemy import text
from sqlalchemy.engine import Row

from postgres.test_automatic_memory_acceptance import (
    _ACCEPT_AUDIT,
    _ONE_ACCEPTANCE,
    _TURN,
    _Answering,
    _audit_names,
    _counts,
    _enqueue_drain,
    _new_evidence,
    _recall_refs,
    _rows,
    _visit,
)
from postgres.test_automatic_memory_drain import _install_failing, _jobs

pytestmark = pytest.mark.postgres

_PENDING_HOURS_ENV: Final = "RHEO__automatic_memory__max_pending_hours"
_NO_RECEIPTS: Final = dict.fromkeys(_ONE_ACCEPTANCE, 0)
_SINK_FAULT: Final = "a limits-test audit sink fault"
_FAILING_TAG: Final = "failing-partition"


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()


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


# --- helpers ----------------------------------------------------------------------


def _at(monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, at: datetime) -> None:
    """One visit at ``at``, with the drain's own clock moved there too."""
    monkeypatch.setattr(automatic, "_now", lambda: at)
    _visit(ev, at=at)


def _drain_at(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, at: datetime
) -> None:
    _enqueue_drain(ev, at=at)
    _at(monkeypatch, ev, at)


def _record_batch(
    ev: EvidenceWorkspace,
    records: Sequence[NewEvidence],
    *,
    purpose: ContextPurpose = ContextPurpose.RESPOND,
    at: datetime,
) -> RecordAttempt:
    """One attempt, published to the probe only: the test drives the drain itself."""
    with ev.handler(probe_registry()) as uow:
        return record_evidence(ev.context(purpose), uow, records, now=at)


def _evidence(
    ev: EvidenceWorkspace,
    body: str,
    *,
    at: datetime,
    purpose: ContextPurpose = ContextPurpose.RESPOND,
    workspace_audience: bool = False,
) -> NewEvidence:
    record = _new_evidence(ev.context(purpose), body, at=at)
    if workspace_audience:
        return dataclasses.replace(record, audience_kind="workspace", audience_id=None)
    return record


def _units(ev: EvidenceWorkspace) -> list[Row[Any]]:
    return _rows(ev, evidence_unit)


def _spy_claims(monkeypatch: pytest.MonkeyPatch) -> list[ClaimedBatch]:
    """Every batch the drain claims, in order, from the real ``claim_units``."""
    batches: list[ClaimedBatch] = []

    def spy(uow: HandlerUnitOfWork, *, now: datetime, **kwargs: Any) -> ClaimedBatch:
        batch = claim_units(uow, now=now, **kwargs)
        batches.append(batch)
        return batch

    monkeypatch.setattr(automatic, "claim_units", spy)
    return batches


def _workspace_setting(
    ev: EvidenceWorkspace, key: str, value: str, value_type: ValueType
) -> None:
    """A workspace settings row written directly, as the retention tests write it."""
    with ev.unit_of_work() as uow:
        upsert_workspace_setting(
            uow.connection, key=key, value=value, value_type=value_type, updated_by=None
        )
        uow.commit()


# --- AC-5: the four limits --------------------------------------------------------


def _bodies(count: int) -> list[str]:
    return [f"{FAKE_EXTRACTION_MARKER} Shelf {n} holds lanterns." for n in range(count)]


@pytest.mark.parametrize(
    ("key", "value"),
    [(MAX_RECORDS_KEY, 2), (MAX_BYTES_KEY, 80)],
    ids=["max_records_per_attempt", "max_bytes_per_attempt"],
)
def test_ac5_an_attempt_budget_defers_the_excess_and_a_resend_settles_it_all(
    monkeypatch: pytest.MonkeyPatch,
    ev: EvidenceWorkspace,
    now: datetime,
    key: str,
    value: int,
) -> None:
    """The deferred suffix writes no row and is named by key only; each resend
    takes what fits, and after the drains every record is one live memory."""
    ev.set_workspace(key, value)
    records = [_evidence(ev, body, at=now) for body in _bodies(3)]

    pending = list(records)
    attempts = 0
    while pending:
        attempt = _record_batch(ev, pending, at=now)
        attempts += 1
        assert attempt.accepted, attempt  # every attempt makes progress
        assert attempt.gapped == attempt.dropped == ()
        assert attempt.deferred == tuple(
            record.native_key for record in pending[len(attempt.accepted) :]
        )
        held = {unit.native_key for unit in _units(ev)}
        assert held.isdisjoint(attempt.deferred)  # a deferral writes no row
        _drain_at(monkeypatch, ev, now)
        pending = pending[len(attempt.accepted) :]

    assert attempts >= 2  # the first attempt really did defer
    units = _units(ev)
    assert {unit.native_key for unit in units} == {r.native_key for r in records}
    assert {(unit.state, unit.outcome) for unit in units} == {("settled", "active")}
    assert len(_rows(ev, memory_tables.memory)) == 3
    assert _audit_names(ev).count(_ACCEPT_AUDIT) == 3


def test_ac5_sixty_five_pending_units_take_two_drains_and_all_settle(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime
) -> None:
    """``max_units_per_job`` (64): the first drain claims 64 and enqueues a follow-up
    due now, which claims the 65th. Nothing is left pending."""
    batches = _spy_claims(monkeypatch)
    records = [_evidence(ev, body, at=now) for body in _bodies(65)]
    attempt = _record_batch(ev, records, at=now)
    assert len(attempt.accepted) == 65
    _drain_at(monkeypatch, ev, now)

    assert [len(batch.units) for batch in batches] == [64, 1]
    assert [batch.follow_up_at for batch in batches] == [now, None]
    drains = _jobs(ev)
    assert [(job.state, job.attempts) for job in drains] == [("succeeded", 1)] * 2
    units = _units(ev)
    assert len(units) == 65
    assert {(unit.state, unit.outcome) for unit in units} == {("settled", "active")}
    assert len(_rows(ev, memory_tables.memory)) == 65


def test_ac5_a_unit_past_max_pending_hours_is_an_expired_gap_with_no_receipt(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime
) -> None:
    failing = _install_failing(monkeypatch)  # counts any extraction call
    stale = now - timedelta(hours=25)  # default window 24 h: closed an hour ago
    attempt = _record_batch(ev, [_evidence(ev, _TURN, at=stale)], at=now)
    assert len(attempt.accepted) == 1
    _drain_at(monkeypatch, ev, now)

    [unit] = _units(ev)
    assert (unit.state, unit.outcome) == ("gap", "expired_pending")
    assert unit.body is None
    assert unit.settled_at == now
    assert failing.calls == 0  # never processed as though it arrived fresh
    assert _counts(ev) == _NO_RECEIPTS
    [drain] = _jobs(ev)
    assert (drain.state, drain.attempts) == ("succeeded", 1)


# --- AC-6: the retention recheck, reached at the seam ------------------------------


def _retention_fixture(
    monkeypatch: pytest.MonkeyPatch,
    ev: EvidenceWorkspace,
    now: datetime,
    *,
    gate: bool,
    workspace_audience: bool,
) -> list[ClaimedBatch]:
    """Edge Case 4 option (a), identical for every leg: a 72-hour pending window
    (the operator's raise), one day of retention, a unit recorded 30 hours ago. Its
    window closes in 42 hours; the horizon was 24 hours ago, 6 hours after it."""
    monkeypatch.setenv(_PENDING_HOURS_ENV, "72")
    _workspace_setting(ev, RETENTION_DAYS_KEY, "1", ValueType.INT)
    _workspace_setting(
        ev, RETENTION_EXPIRE_BY_AGE_KEY, "true" if gate else "false", ValueType.BOOL
    )
    recorded_at = now - timedelta(hours=30)
    record = _evidence(ev, _TURN, at=recorded_at, workspace_audience=workspace_audience)
    attempt = _record_batch(ev, [record], at=now)
    assert len(attempt.accepted) == 1

    # The pre-assertion: still pending, window still open, so the drain below reaches
    # ``_policy_state``'s later checks and not ``_window_state``'s ``expired``.
    [unit] = _units(ev)
    assert unit.state == "pending"
    assert unit.source_recorded_at == recorded_at
    assert unit.source_expires_at == recorded_at + timedelta(hours=72)
    assert unit.source_expires_at > now

    batches = _spy_claims(monkeypatch)
    _drain_at(monkeypatch, ev, now)
    return batches


def _claimed_once_with_no_follow_up(
    ev: EvidenceWorkspace, batches: list[ClaimedBatch]
) -> None:
    """W1: the drain claimed the unit, and enqueued no follow-up drain."""
    assert [len(batch.units) for batch in batches] == [1]
    assert batches[0].follow_up_at is None
    [drain] = _jobs(ev)
    assert (drain.state, drain.attempts) == ("succeeded", 1)


def test_ac6_gate_on_workspace_audience_outside_the_horizon_is_born_expired(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime
) -> None:
    batches = _retention_fixture(
        monkeypatch, ev, now, gate=True, workspace_audience=True
    )

    _claimed_once_with_no_follow_up(ev, batches)
    [unit] = _units(ev)
    assert (unit.state, unit.outcome) == ("settled", "noop")
    [receipt] = _rows(ev, memory_tables.source_receipt)
    assert (receipt.state, receipt.record_id) == ("noop", None)
    assert _rows(ev, memory_tables.memory) == []
    assert _ACCEPT_AUDIT not in _audit_names(ev)


def test_ac6_gate_off_the_same_workspace_unit_is_one_live_memory(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime
) -> None:
    batches = _retention_fixture(
        monkeypatch, ev, now, gate=False, workspace_audience=True
    )

    _claimed_once_with_no_follow_up(ev, batches)
    [unit] = _units(ev)
    assert (unit.state, unit.outcome) == ("settled", "active")
    [receipt] = _rows(ev, memory_tables.source_receipt)
    assert receipt.state == "active"
    [memory] = _rows(ev, memory_tables.memory)
    assert (memory.audience_kind, memory.audience_id) == ("workspace", None)
    [ref] = _recall_refs(ev, None)  # live: a read returns it
    assert str(memory.id) in ref


def test_ac6_gate_on_a_member_unit_of_the_same_age_is_one_live_memory(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime
) -> None:
    _retention_fixture(monkeypatch, ev, now, gate=True, workspace_audience=False)

    [unit] = _units(ev)
    assert (unit.state, unit.outcome) == ("settled", "active")
    [receipt] = _rows(ev, memory_tables.source_receipt)
    assert receipt.state == "active"
    [memory] = _rows(ev, memory_tables.memory)
    assert (memory.audience_kind, memory.audience_id) == ("member", ev.owner_account_id)
    [ref] = _recall_refs(ev, None)
    assert str(memory.id) in ref


# --- AC-7: extraction faults ------------------------------------------------------


class _Flaky:
    """Raises on its first ``fails`` calls, or, given ``only_tag``, on every batch
    whose text carries that tag; otherwise answers as the built-in fake does."""

    def __init__(self, *, fails: int = 0, only_tag: str | None = None) -> None:
        providers()  # the built-ins first, so the swap is undone onto them
        self.fake = PROVIDERS[FAKE_PROVIDER]
        self.fails = fails
        self.only_tag = only_tag
        self.calls = 0

    @property
    def name(self) -> str:
        return FAKE_PROVIDER

    def extract(self, batch: DigestBatch) -> Any:
        self.calls += 1
        if self.only_tag is not None:
            fault = any(self.only_tag in item.text for item in batch.items)
        else:
            fault = self.calls <= self.fails
        if fault:
            raise RuntimeError("a limits-test provider fault")
        return self.fake.extract(batch)


def test_ac7_a_fault_then_success_after_retry_after_writes_one_memory(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime
) -> None:
    flaky = _Flaky(fails=1)
    monkeypatch.setitem(PROVIDERS, FAKE_PROVIDER, flaky)
    _record_batch(ev, [_evidence(ev, _TURN, at=now)], at=now)
    first = _enqueue_drain(ev, at=now)
    _at(monkeypatch, ev, now)

    # The failed drain wrote no receipt at all, and succeeded as a job.
    assert _counts(ev) == _NO_RECEIPTS
    [unit] = _units(ev)
    assert (unit.state, unit.extraction_attempts) == ("pending", 1)
    assert unit.retry_after == now + timedelta(seconds=5)
    [drain] = [job for job in _jobs(ev) if job.id == first]
    assert (drain.state, drain.attempts) == ("succeeded", 1)

    _at(monkeypatch, ev, unit.retry_after)

    assert flaky.calls == 2
    assert _counts(ev) == _ONE_ACCEPTANCE
    [unit] = _units(ev)
    assert (unit.state, unit.outcome) == ("settled", "active")


def test_ac7_five_faults_settle_extraction_failed_and_no_drain_job_ever_retries(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime
) -> None:
    failing = _install_failing(monkeypatch)
    _record_batch(ev, [_evidence(ev, _TURN, at=now)], at=now)
    _enqueue_drain(ev, at=now)

    at = now
    for attempt, step in enumerate((5, 20, 80, 320, None), start=1):
        _at(monkeypatch, ev, at)
        [unit] = _units(ev)
        assert unit.extraction_attempts == attempt
        if step is None:
            break
        assert (unit.state, unit.retry_after) == (
            "pending",
            at + timedelta(seconds=step),
        )
        at = unit.retry_after

    assert failing.calls == 5
    [unit] = _units(ev)
    assert (unit.state, unit.outcome) == ("gap", "extraction_failed")
    assert unit.body is None
    assert unit.settled_at == at
    assert _counts(ev) == _NO_RECEIPTS
    drains = _jobs(ev)
    assert [(job.state, job.attempts) for job in drains] == [("succeeded", 1)] * 5


def test_ac7_a_healthy_later_partition_settles_before_the_failing_one_is_due_again(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime
) -> None:
    """R11's head-of-line proof: the failing partition is recorded first and so
    anchors the first claim; the healthy one, recorded after it, is settled by the
    follow-up drain before the failing one's ``retry_after`` comes due."""
    flaky = _Flaky(only_tag=_FAILING_TAG)
    monkeypatch.setitem(PROVIDERS, FAKE_PROVIDER, flaky)
    failing_body = f"{FAKE_EXTRACTION_MARKER} The {_FAILING_TAG} lanterns."
    _record_batch(
        ev,
        [_evidence(ev, failing_body, at=now - timedelta(seconds=10))],
        purpose=ContextPurpose.RESPOND,
        at=now,
    )
    healthy = ContextPurpose.INTERNAL_ANALYSIS
    _record_batch(
        ev,
        [_evidence(ev, _TURN, at=now - timedelta(seconds=5), purpose=healthy)],
        purpose=healthy,
        at=now,
    )
    batches = _spy_claims(monkeypatch)
    _drain_at(monkeypatch, ev, now)

    by_purpose = {unit.purpose: unit for unit in _units(ev)}
    stuck = by_purpose[ContextPurpose.RESPOND.value]
    settled = by_purpose[healthy.value]
    assert [len(batch.units) for batch in batches[:2]] == [0, 1]
    assert (stuck.state, stuck.extraction_attempts) == ("pending", 1)
    assert (settled.state, settled.outcome) == ("settled", "active")
    assert settled.settled_at == now < stuck.retry_after
    assert _counts(ev) == _ONE_ACCEPTANCE
    queued = [job for job in _jobs(ev) if job.state == "queued"]
    assert [job.next_run_at for job in queued] == [stuck.retry_after]


# --- card item 5: the success audit rolls back with the acceptance ------------------


class _RaisingSink:
    def record(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError(_SINK_FAULT)


_SINK_IDS: Final = (CORE_MODULE_ID, HARNESS_MODULE_ID, MODULE_ID)


@pytest.fixture
def sinks(ev: EvidenceWorkspace) -> Iterator[None]:
    """Snapshot the sink table and put it back afterwards, whatever the test did."""
    installed = {module_id: sink_for(module_id) for module_id in _SINK_IDS}
    assert installed[MODULE_ID] is not None  # Recallatron's manifest installed one
    yield
    _replace_sinks(installed)


def _replace_sinks(table: dict[str, Any]) -> None:
    reset_sinks()
    for module_id, sink in table.items():
        if sink is not None:
            install_sink(module_id, sink)


def _without_recallatron_sink() -> dict[str, Any]:
    return {
        module_id: sink_for(module_id)
        for module_id in _SINK_IDS
        if module_id != MODULE_ID
    }


def _spy_settlements(monkeypatch: pytest.MonkeyPatch) -> list[type[BaseException]]:
    raised: list[type[BaseException]] = []

    def spy(*args: Any, **kwargs: Any) -> None:
        try:
            settle_unit(*args, **kwargs)
        except BaseException as exc:
            raised.append(type(exc))
            raise

    monkeypatch.setattr(automatic, "settle_unit", spy)
    return raised


def _assert_rolled_back(ev: EvidenceWorkspace) -> None:
    """Nothing of the acceptance survived, and the unit waits with its body."""
    assert _counts(ev) == _NO_RECEIPTS
    assert _rows(ev, memory_tables.memory_entity) == []
    [unit] = _units(ev)
    assert (unit.state, unit.outcome, unit.settled_at) == ("pending", None, None)
    assert unit.body is not None and FAKE_EXTRACTION_MARKER in unit.body


def test_card5a_a_raising_sink_fails_the_attempt_and_a_restored_sink_accepts_once(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime, sinks: None
) -> None:
    installed = {module_id: sink_for(module_id) for module_id in _SINK_IDS}
    _replace_sinks({**_without_recallatron_sink(), MODULE_ID: _RaisingSink()})
    _record_batch(ev, [_evidence(ev, _TURN, at=now)], at=now)
    _drain_at(monkeypatch, ev, now)

    [drain] = _jobs(ev)
    assert (drain.state, drain.attempts, drain.last_error) == ("queued", 1, _SINK_FAULT)
    _assert_rolled_back(ev)

    _replace_sinks(installed)  # CORE_AUDIT_SINK back under Recallatron's id
    _at(monkeypatch, ev, drain.next_run_at)

    [drain] = _jobs(ev)
    assert (drain.state, drain.attempts) == ("succeeded", 2)
    assert _counts(ev) == _ONE_ACCEPTANCE
    [unit] = _units(ev)
    assert (unit.state, unit.outcome) == ("settled", "active")


def test_card5b_no_sink_is_evidence_audit_unwritable_and_rolls_everything_back(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime, sinks: None
) -> None:
    """Since #217 the missing sink is found before the model call, so the claim
    raises ``EvidenceAuditUnwritable`` and ``settle_unit`` is never reached."""
    _replace_sinks(_without_recallatron_sink())
    assert sink_for(MODULE_ID) is None
    raised = _spy_settlements(monkeypatch)
    _record_batch(ev, [_evidence(ev, _TURN, at=now)], at=now)
    _drain_at(monkeypatch, ev, now)

    assert raised == []
    [drain] = _jobs(ev)
    assert (drain.state, drain.attempts) == ("queued", 1)
    assert drain.last_error == str(
        EvidenceAuditUnwritable("no audit sink for the accepting module")
    )
    _assert_rolled_back(ev)


def test_card5c_a_noop_settles_with_no_audit_row_while_the_raising_sink_is_installed(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime, sinks: None
) -> None:
    _replace_sinks({**_without_recallatron_sink(), MODULE_ID: _RaisingSink()})
    providers()  # the built-ins first, so the swap is undone onto them
    monkeypatch.setitem(
        PROVIDERS, FAKE_PROVIDER, _Answering({"kind": "note", "title": "", "body": ""})
    )
    _record_batch(ev, [_evidence(ev, _TURN, at=now)], at=now)
    _drain_at(monkeypatch, ev, now)

    [drain] = _jobs(ev)
    assert (drain.state, drain.attempts, drain.last_error) == ("succeeded", 1, None)
    [unit] = _units(ev)
    assert (unit.state, unit.outcome) == ("settled", "noop")
    [receipt] = _rows(ev, memory_tables.source_receipt)
    assert receipt.state == "noop"
    assert _rows(ev, memory_tables.memory) == []
    assert _audit_names(ev).count(_ACCEPT_AUDIT) == 0


# --- #215: one poison unit costs only itself -----------------------------------------

_POISON_TAG: Final = "poison-unit"
_POISON_ROW: Final = "Example Hall Rentals, 555-0142"
"""What the forced refusal's message quotes, as Postgres quotes a failing row."""


def _poisoned_acceptance(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Acceptance at the drain's call site, refused by the database for the unit
    whose body carries :data:`_POISON_TAG`.

    The real seam runs first, so the refusal lands after it wrote the memory, as a
    CHECK failure on a later insert would. The refusal is a real one, SQLSTATE 23514
    from the server, which aborts the transaction until the savepoint rolls back.
    Answers the kinds of the units it refused, in order.
    """
    refused: list[str] = []

    def accept(ctx: Any, uow: Any, unit: Any, **kwargs: Any) -> Any:
        outcome = accept_source_unit(ctx, uow, unit, **kwargs)
        if _POISON_TAG in unit.evidence.body:
            refused.append(unit.evidence.kind)
            uow.connection.execute(
                text(
                    "DO $$ BEGIN RAISE EXCEPTION USING ERRCODE = 'check_violation', "
                    f"MESSAGE = 'Failing row contains ({_POISON_ROW})'; END $$"
                )
            )
        return outcome

    monkeypatch.setattr(automatic, "accept_source_unit", accept)
    return refused


def test_215_a_poison_unit_backs_off_and_its_partition_neighbour_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
    ev: EvidenceWorkspace,
    now: datetime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two units, one partition, one refused by the database. The other is accepted
    in the same drain, the poison unit counts one attempt and backs off, and the
    drain job succeeds; the next drain is due at the poison unit's ``retry_after``.
    Nothing the refusal's message quoted reaches a log line or the job row."""
    refused = _poisoned_acceptance(monkeypatch)
    poison_body = f"{FAKE_EXTRACTION_MARKER} The {_POISON_TAG} lanterns."
    _record_batch(
        ev,
        [
            _evidence(ev, poison_body, at=now - timedelta(seconds=10)),
            _evidence(ev, _TURN, at=now - timedelta(seconds=5)),
        ],
        at=now,
    )
    first = _enqueue_drain(ev, at=now)
    with caplog.at_level(logging.DEBUG):
        _at(monkeypatch, ev, now)

    assert refused == ["note"]
    assert _counts(ev) == _ONE_ACCEPTANCE
    # A settled row's body is cleared, so only the poison unit still has one.
    [poison] = [u for u in _units(ev) if u.body is not None and _POISON_TAG in u.body]
    [healthy] = [u for u in _units(ev) if u.id != poison.id]
    assert (healthy.state, healthy.outcome) == ("settled", "active")
    assert (poison.state, poison.outcome) == ("pending", None)
    assert poison.extraction_attempts == 1
    assert poison.retry_after == now + timedelta(seconds=5)
    [drain] = [job for job in _jobs(ev) if job.id == first]
    assert (drain.state, drain.attempts, drain.last_error) == ("succeeded", 1, None)
    queued = [job for job in _jobs(ev) if job.state == "queued"]
    assert [job.next_run_at for job in queued] == [poison.retry_after]

    [deferred] = [
        r for r in caplog.records if r.getMessage() == ACCEPTANCE_DEFERRED_LOG
    ]
    assert (vars(deferred)["error_type"], vars(deferred)["sqlstate"]) == (
        "IntegrityError",
        "23514",
    )
    assert _POISON_ROW not in caplog.text
    assert _POISON_TAG not in caplog.text
    assert all(job.last_error is None for job in _jobs(ev))


def test_215_a_poison_unit_settles_acceptance_failed_on_its_fifth_attempt(
    monkeypatch: pytest.MonkeyPatch,
    ev: EvidenceWorkspace,
    now: datetime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The deferral's own follow-ups carry the unit to its fifth attempt, on the
    extraction schedule (5, 20, 80, 320 s), with no drain job ever retried.

    #221: only the fifth, settling attempt logs ``evidence_acceptance_failed``, with
    the evidence id and the attempt count and nothing the refusal quoted."""
    refused = _poisoned_acceptance(monkeypatch)
    body = f"{FAKE_EXTRACTION_MARKER} The {_POISON_TAG} lanterns."
    _record_batch(ev, [_evidence(ev, body, at=now)], at=now)
    _enqueue_drain(ev, at=now)

    def settled_lines() -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.getMessage() == ACCEPTANCE_FAILED_LOG]

    at = now
    for attempt, step in enumerate((5, 20, 80, 320, None), start=1):
        with caplog.at_level(logging.WARNING):
            _at(monkeypatch, ev, at)
        [unit] = _units(ev)
        assert unit.extraction_attempts == attempt
        if step is None:
            break
        assert settled_lines() == [], attempt
        assert (unit.state, unit.retry_after) == (
            "pending",
            at + timedelta(seconds=step),
        )
        # The one queued drain is the next attempt's, due exactly then.
        queued = [job for job in _jobs(ev) if job.state == "queued"]
        assert [job.next_run_at for job in queued] == [unit.retry_after]
        at = unit.retry_after

    assert len(refused) == 5
    [unit] = _units(ev)
    assert (unit.state, unit.outcome) == ("gap", "acceptance_failed")
    [settled] = settled_lines()
    assert settled.levelno == logging.WARNING
    assert (vars(settled)["evidence_id"], vars(settled)["attempts"]) == (
        str(unit.id),
        5,
    )
    assert settled.exc_info is None
    assert _POISON_TAG not in repr(vars(settled))
    assert _POISON_ROW not in caplog.text
    assert unit.body is None
    assert unit.settled_at == at
    assert _counts(ev) == _NO_RECEIPTS
    assert _rows(ev, memory_tables.memory_entity) == []
    drains = _jobs(ev)
    assert {(job.state, job.attempts, job.last_error) for job in drains} == {
        ("succeeded", 1, None)
    }


# --- #221: only the accept step is contained -----------------------------------------


def _faulting_audit_insert(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The success audit write refused by the database, as a NOT NULL column added
    without a default or a new CHECK on the audit row would refuse it: a real
    SQLSTATE 23514 from the server, raised where the row is inserted. Answers one
    entry per refused write."""
    dispatch_module = importlib.import_module("rheo_core.operations.dispatch")
    refused: list[int] = []

    def write(ctx: Any, uow: Any, **kwargs: Any) -> bool:
        refused.append(1)
        uow.connection.execute(
            text(
                "DO $$ BEGIN RAISE EXCEPTION USING ERRCODE = 'check_violation', "
                f"MESSAGE = 'Failing row contains ({_POISON_ROW})'; END $$"
            )
        )
        return True

    monkeypatch.setattr(dispatch_module, "record_evidence_acceptance_audit", write)
    return refused


def test_221_a_fault_in_the_audit_insert_rolls_the_drain_back_and_gaps_nothing(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime
) -> None:
    """Before #221 the per-unit savepoint also covered ``settle_unit``, so this
    refusal deferred the unit and the drain succeeded; five drains later it settled
    ``gap/acceptance_failed``. Now the drain job fails its attempt, content-free, and
    the unit waits uncounted with its body."""
    refused = _faulting_audit_insert(monkeypatch)
    _record_batch(ev, [_evidence(ev, _TURN, at=now)], at=now)
    _drain_at(monkeypatch, ev, now)

    assert refused == [1]
    [drain] = _jobs(ev)
    assert (drain.state, drain.attempts) == ("queued", 1)
    assert drain.last_error == "IntegrityError (SQLSTATE 23514)"
    _assert_rolled_back(ev)
    [unit] = _units(ev)
    assert (unit.extraction_attempts, unit.retry_after) == (0, None)


@pytest.mark.parametrize("good", [0, 1], ids=["two_of_two", "two_of_three"])
def test_221_two_poison_units_in_one_batch_each_back_off_and_the_drain_succeeds(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, now: datetime, good: int
) -> None:
    """The #215 regression guard. Two refused units in one partition, alone or
    beside a good one: every deferral commits, so each poison unit counts one
    attempt and backs off, the good unit is accepted, and the job succeeds. Rolling
    the batch back instead would leave ``extraction_attempts`` at 0, and since it
    leads the claim order the next drain would anchor on the poison partition
    again, ahead of a healthy one recorded later."""
    refused = _poisoned_acceptance(monkeypatch)
    poison = [
        _evidence(
            ev,
            f"{FAKE_EXTRACTION_MARKER} The {_POISON_TAG} lanterns {n}.",
            at=now - timedelta(seconds=20 - n),
        )
        for n in range(2)
    ]
    healthy = [_evidence(ev, _TURN, at=now - timedelta(seconds=5))][:good]
    _record_batch(ev, [*poison, *healthy], at=now)
    first = _enqueue_drain(ev, at=now)
    batches = _spy_claims(monkeypatch)
    _at(monkeypatch, ev, now)

    assert len(refused) == 2
    assert [len(batch.units) for batch in batches[:1]] == [2 + good]
    [drain] = [job for job in _jobs(ev) if job.id == first]
    assert (drain.state, drain.attempts, drain.last_error) == ("succeeded", 1, None)
    stuck = [u for u in _units(ev) if u.body is not None and _POISON_TAG in u.body]
    assert len(stuck) == 2
    for unit in stuck:
        assert (unit.state, unit.extraction_attempts) == ("pending", 1)
        assert unit.retry_after == now + timedelta(seconds=5)
    settled = [u for u in _units(ev) if u.id not in {s.id for s in stuck}]
    assert [(u.state, u.outcome) for u in settled] == [("settled", "active")] * good
    assert len(_rows(ev, memory_tables.memory)) == good

    # A healthy partition recorded after the poison one is taken first next time.
    later = ContextPurpose.INTERNAL_ANALYSIS
    _record_batch(
        ev, [_evidence(ev, _TURN, at=now, purpose=later)], purpose=later, at=now
    )
    _drain_at(monkeypatch, ev, now + timedelta(seconds=1))
    [fresh] = [u for u in _units(ev) if u.purpose == later.value]
    assert (fresh.state, fresh.outcome) == ("settled", "active")
    assert len(refused) == 2  # the poison units were not claimed again yet


# --- #217: workspace-wide acceptance faults never reach the provider -----------------


def _no_sink(ev: EvidenceWorkspace) -> str:
    _replace_sinks(_without_recallatron_sink())
    return "no audit sink for the accepting module"


def _no_retention(ev: EvidenceWorkspace) -> str:
    """The gate on and the window out of range: AC 8's ``retention_unavailable``."""
    _workspace_setting(ev, RETENTION_EXPIRE_BY_AGE_KEY, "true", ValueType.BOOL)
    _workspace_setting(ev, RETENTION_DAYS_KEY, "0", ValueType.INT)
    return "retention_unavailable"


@pytest.mark.parametrize("fault", [_no_sink, _no_retention], ids=["sink", "retention"])
def test_217_two_drains_failing_workspace_wide_never_call_the_provider(
    monkeypatch: pytest.MonkeyPatch,
    ev: EvidenceWorkspace,
    now: datetime,
    sinks: None,
    fault: Any,
) -> None:
    """Both faults roll the drain back, extraction included. Checked after the
    model call, each retry would send the same evidence again; checked before it,
    neither drain calls the provider, and the unit waits uncounted with its body."""
    counting = _Flaky()  # delegates to the built-in fake, counting every call
    monkeypatch.setitem(PROVIDERS, FAKE_PROVIDER, counting)
    expected = fault(ev)
    _record_batch(ev, [_evidence(ev, _TURN, at=now)], at=now)
    _drain_at(monkeypatch, ev, now)
    [drain] = _jobs(ev)
    assert (drain.state, drain.attempts) == ("queued", 1)
    _at(monkeypatch, ev, drain.next_run_at)

    assert counting.calls == 0
    [drain] = _jobs(ev)
    assert (drain.state, drain.attempts) == ("queued", 2)
    assert drain.last_error is not None and expected in drain.last_error
    _assert_rolled_back(ev)
    [unit] = _units(ev)
    assert unit.extraction_attempts == 0

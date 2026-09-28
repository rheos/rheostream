"""``claim_units`` and ``settle_unit``: the eligible-evidence service (spec
§ Architecture, System Components item 8; FR 2, 5, 6, 9, 10; R9, R11, R14).

Seams, each proven by its own test rather than inferred from a happy path:

- workspace resolution from the connection: ``EvidenceWorkspaceUnresolved`` on a
  routing mismatch, an unavailable workspace or the control database, with no evidence
  row touched and no provider call;
- the partition invariant: a partition read that misses its anchor raises
  ``EvidenceClaimInconsistent``;
- extraction-failure containment (R11): a failing partition backs off and settles
  ``gap/extraction_failed`` on its fifth attempt while another partition drains;
- locks: ``SKIP LOCKED`` never waits, and an empty claim never schedules a follow-up
  from rows it could not lock (no drain loop);
- the audit coupling: an ``active`` settlement with no sink raises before the row
  settles, so the caller's rollback takes everything with it.

Rows are recorded through ``record_evidence`` with the shared recording fixture, and
every claim runs on a ``HandlerUnitOfWork`` over the provisioned workspace's own
database. The sections below are independent. Every text is synthetic.
"""

import dataclasses
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.evidence import (
    EvidenceWorkspace,
    enable_recording,
    handler_uow,
    probe_registry,
)
from harness.registry import add_member
from rheo_contracts import ContextPurpose, RecordRef, Role, WorkspaceContext
from rheo_contracts.source_units import AuthorityRefused, TrustedSourceUnit
from rheo_core.audit import AUDIT_SUCCEEDED, list_audit_records
from rheo_core.audit.records import AuditRow
from rheo_core.boundary import context_for_harness
from rheo_core.evidence import (
    ClaimedBatch,
    ClaimedUnit,
    EvidenceAuditUnwritable,
    EvidenceClaimInconsistent,
    claim_units,
    settle_unit,
)
from rheo_core.evidence import service as evidence_service
from rheo_core.evidence.authority import SPEAKER_MISMATCH, RuntimeEvidenceAuthority
from rheo_core.evidence.extract import NOOP_EVIDENCE, DigestBatch
from rheo_core.evidence.providers import (
    FAKE_PROVIDER,
    PROVIDERS,
    FakeExtractionProvider,
    providers,
)
from rheo_core.evidence.record import NewEvidence, record_evidence
from rheo_core.evidence.service import (
    EXTRACTION_FAILED_LOG,
    EXTRACTION_MAX_ATTEMPTS,
    MAX_UNITS_PER_JOB_KEY,
    EvidenceWorkspaceUnresolved,
)
from rheo_core.operations import HARNESS_MODULE_ID, register_core_operations
from rheo_core.operations.dispatch import EVIDENCE_ACCEPT_AUDIT
from rheo_core.refs import uuid7
from rheo_core.storage import control_tables
from rheo_core.storage.backend import (
    WORKSPACE_UNAVAILABLE,
    StorageRefusal,
    UnitOfWork,
)
from rheo_core.storage.evidence_tables import (
    OUTCOME_AUTHORITY_UNVERIFIED,
    OUTCOME_EXPIRED_PENDING,
    OUTCOME_EXTRACTION_FAILED,
    STATE_GAP,
    STATE_PENDING,
    STATE_SETTLED,
    evidence_unit,
)
from rheo_core.storage.provisioning import database_name_for
from sqlalchemy import Connection, delete, func, select, text, update

pytestmark = pytest.mark.postgres

FAILING_MARKER: Final = "partition-a-fault"
"""Text the fault-injecting providers fail on."""
UNSINKED_MODULE_ID: Final = "test.no_sink"
LOG_NAME: Final = "rheo_core.evidence"


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture
def recording(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[EvidenceWorkspace]:
    ev = EvidenceWorkspace(cluster, workspace, owner_account_id)
    enable_recording(monkeypatch, ev)
    yield ev


@pytest.fixture
def fake() -> FakeExtractionProvider:
    """The registered ``fake``, its call log emptied: it lives for the process."""
    provider = providers()[FAKE_PROVIDER]
    assert isinstance(provider, FakeExtractionProvider), provider
    provider.reset_calls()
    return provider


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


class _FailingFor:
    """A provider that fails any batch holding :data:`FAILING_MARKER`, and answers
    like the ``fake`` otherwise. Its raise quotes the text, so a log line that kept
    the message would carry evidence."""

    def __init__(self, *, malformed: bool) -> None:
        self.malformed = malformed
        self.calls: list[DigestBatch] = []
        self._fake = FakeExtractionProvider()

    @property
    def name(self) -> str:
        return FAKE_PROVIDER

    def extract(self, batch: DigestBatch) -> Mapping[str, object]:
        self.calls.append(batch)
        for item in batch.items:
            if FAILING_MARKER in item.text:
                if self.malformed:
                    return {"answers": [item.text]}
                raise RuntimeError(f"provider fault while reading {item.text}")
        return self._fake.extract(batch)


def _install(monkeypatch: pytest.MonkeyPatch, provider: _FailingFor) -> _FailingFor:
    providers()  # the built-ins first, so the swap is undone onto them
    monkeypatch.setitem(PROVIDERS, FAKE_PROVIDER, provider)
    return provider


# --- helpers --------------------------------------------------------------------------


def _member_context(
    cluster: ClusterSession, ev: EvidenceWorkspace, purpose: ContextPurpose
) -> WorkspaceContext:
    """A second member of the workspace, in its own bound context."""
    member = add_member(
        cluster.backend, ev.workspace_id, Role.MEMBER, display_name="second-speaker"
    )
    ctx = context_for_harness(
        ev.workspace_id, member, Role.MEMBER, bound_purpose=purpose
    )
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _revoke(cluster: ClusterSession, ev: EvidenceWorkspace, account_id: UUID) -> None:
    with cluster.backend.control_engine.begin() as connection:
        deleted = connection.execute(
            delete(control_tables.membership).where(
                (control_tables.membership.c.account_id == account_id)
                & (control_tables.membership.c.workspace_id == ev.workspace_id)
            )
        ).rowcount
    assert deleted == 1


def _record(
    ev: EvidenceWorkspace,
    ctx: WorkspaceContext,
    texts: Sequence[str],
    *,
    at: datetime,
    workspace_audience: bool = False,
) -> list[NewEvidence]:
    """Record ``texts`` as ``ctx``'s own turns, one second apart from ``at``."""
    account_id = ctx.principal.account_id
    purpose = ctx.principal.bound_purpose
    assert account_id is not None and purpose is not None
    records: list[NewEvidence] = []
    for offset, body in enumerate(texts):
        authority_id = uuid7()
        records.append(
            NewEvidence(
                producer_kind="rheo_runtime",
                authority_id=authority_id,
                native_key=f"turn:{authority_id}",
                speaker_account_id=account_id,
                audience_kind="workspace" if workspace_audience else "member",
                audience_id=None if workspace_audience else account_id,
                purpose=purpose,
                recorded_at=at + timedelta(seconds=offset),
                raw_text=body,
            )
        )
    with ev.handler(probe_registry()) as uow:
        attempt = record_evidence(ctx, uow, records, now=at)
    assert attempt.accepted == tuple(record.native_key for record in records), attempt
    return records


def _rows(ev: EvidenceWorkspace) -> dict[str, Any]:
    with ev.unit_of_work() as uow:
        return {
            row.native_key: row
            for row in uow.connection.execute(select(evidence_unit)).all()
        }


def _row(ev: EvidenceWorkspace, record: NewEvidence) -> Any:
    return _rows(ev)[record.native_key]


def _set(ev: EvidenceWorkspace, records: Sequence[NewEvidence], **values: Any) -> None:
    with ev.unit_of_work() as uow:
        uow.connection.execute(
            update(evidence_unit)
            .where(evidence_unit.c.native_key.in_([r.native_key for r in records]))
            .values(**values)
        )
        uow.commit()


def _claim(ev: EvidenceWorkspace, *, now: datetime) -> ClaimedBatch:
    """One committed claim. The lock timeout turns a claim that would wait on a lock
    into a failure instead of a hang."""
    with ev.unit_of_work() as uow:
        uow.connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        batch = claim_units(handler_uow(uow, probe_registry()), now=now)
        uow.commit()
    return batch


def _outcome_for(claimed: ClaimedUnit) -> tuple[str, str | None]:
    """What an accepting module would answer: a memory for a real candidate, else
    ``noop``."""
    if claimed.unit.evidence == NOOP_EVIDENCE:
        return "noop", None
    return "active", f"harness.note:{uuid7()}"


def _drain(ev: EvidenceWorkspace, *, now: datetime) -> ClaimedBatch:
    """Claim and settle every claimed unit in one committed transaction, the drain
    job's shape."""
    with ev.unit_of_work() as uow:
        view = handler_uow(uow, probe_registry())
        batch = claim_units(view, now=now)
        for claimed in batch.units:
            outcome, memory_ref = _outcome_for(claimed)
            settle_unit(
                view,
                claimed,
                outcome=outcome,
                memory_ref=memory_ref,
                audit_module_id=HARNESS_MODULE_ID,
                now=now,
            )
        uow.commit()
    return batch


def _seen(calls: Sequence[DigestBatch]) -> list[str]:
    return [item.text for batch in calls for item in batch.items]


def _audit_rows(ev: EvidenceWorkspace) -> tuple[AuditRow, ...]:
    with ev.unit_of_work() as uow:
        return list_audit_records(uow.connection, limit=500)


def _added_audit(ev: EvidenceWorkspace, before: tuple[AuditRow, ...]) -> list[AuditRow]:
    seen = {row.id for row in before}
    return [row for row in _audit_rows(ev) if row.id not in seen]


def _other_connection(ev: EvidenceWorkspace) -> Connection:
    return ev.cluster.backend.pools.engine_for(ev.database_name).connect()


def _texts(prefix: str, count: int) -> list[str]:
    return [f"{prefix} turn {n} about the community garden rota." for n in range(count)]


# --- workspace resolution (step 0, R14) ----------------------------------------------


def test_the_claim_resolves_its_workspace_from_the_connection(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    """Proven by its effects: the units' context names the fixture's workspace, and a
    workspace settings row read under the resolved id caps the claim at two."""
    recording.set_workspace(MAX_UNITS_PER_JOB_KEY, 2)
    _record(
        recording,
        recording.context(),
        _texts("capped", 3),
        at=now - timedelta(minutes=5),
    )

    batch = _claim(recording, now=now)

    assert len(batch.units) == 2
    assert {u.context.workspace_id for u in batch.units} == {recording.workspace_id}
    assert batch.follow_up_at == now


def _assert_untouched(
    ev: EvidenceWorkspace,
    expired: NewEvidence,
    claimable: NewEvidence,
    fake: FakeExtractionProvider,
) -> None:
    expired_row = _row(ev, expired)
    assert expired_row.state == STATE_PENDING
    claimable_row = _row(ev, claimable)
    assert claimable_row.state == STATE_PENDING
    assert claimable_row.retry_after is None
    assert claimable_row.extraction_attempts == 0
    assert all(row.settled_at is None for row in _rows(ev).values())
    assert fake.calls == []


@pytest.mark.parametrize("fault", ["another-database", "unavailable"])
def test_a_routing_row_that_does_not_match_raises_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
    recording: EvidenceWorkspace,
    fake: FakeExtractionProvider,
    now: datetime,
    fault: str,
) -> None:
    ctx = recording.context()
    (expired,) = _record(
        recording, ctx, _texts("expired", 1), at=now - timedelta(hours=25)
    )
    (claimable,) = _record(
        recording, ctx, _texts("fresh", 1), at=now - timedelta(minutes=1)
    )
    real = recording.cluster.registry_row(recording.workspace_id)

    def answer(workspace_id: UUID) -> Any:
        if fault == "unavailable":
            raise StorageRefusal(
                WORKSPACE_UNAVAILABLE, "synthetic", workspace_state=None
            )
        return dataclasses.replace(real, database_name=database_name_for(uuid7()))

    monkeypatch.setattr(evidence_service, "active_workspace", answer)

    with pytest.raises(EvidenceWorkspaceUnresolved):
        _claim(recording, now=now)

    _assert_untouched(recording, expired, claimable, fake)


def test_a_unit_of_work_on_the_control_database_raises(
    cluster: ClusterSession,
    recording: EvidenceWorkspace,
    fake: FakeExtractionProvider,
    now: datetime,
) -> None:
    ctx = recording.context()
    (expired,) = _record(
        recording, ctx, _texts("expired", 1), at=now - timedelta(hours=25)
    )
    (claimable,) = _record(
        recording, ctx, _texts("fresh", 1), at=now - timedelta(minutes=1)
    )
    control = cluster.backend.control_engine
    name = control.url.database
    assert name is not None and not name.startswith("ws_")

    with UnitOfWork(control, name) as uow, pytest.raises(EvidenceWorkspaceUnresolved):
        claim_units(handler_uow(uow, probe_registry()), now=now)

    _assert_untouched(recording, expired, claimable, fake)


# --- claim basics ---------------------------------------------------------------------


def test_one_past_the_limit_claims_sixty_four_and_asks_to_follow_up_now(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    _record(
        recording, recording.context(), _texts("many", 65), at=now - timedelta(hours=1)
    )

    batch = _claim(recording, now=now)

    assert len(batch.units) == 64
    assert batch.follow_up_at == now
    assert len(fake.calls) == 1 and len(fake.calls[0].items) == 64


def test_exactly_the_limit_claims_everything_and_needs_no_follow_up(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    _record(
        recording, recording.context(), _texts("exact", 64), at=now - timedelta(hours=1)
    )

    batch = _claim(recording, now=now)

    assert len(batch.units) == 64
    assert batch.follow_up_at is None


def test_expired_pending_is_aged_before_selection(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    ctx = recording.context()
    (expired,) = _record(
        recording, ctx, _texts("stale", 1), at=now - timedelta(hours=25)
    )
    (fresh,) = _record(
        recording, ctx, _texts("fresh", 1), at=now - timedelta(minutes=1)
    )

    batch = _claim(recording, now=now)

    expired_id = _row(recording, expired).id
    assert [u.evidence_id for u in batch.units] == [_row(recording, fresh).id]
    assert expired_id not in {u.evidence_id for u in batch.units}
    aged = _row(recording, expired)
    assert (aged.state, aged.outcome) == (STATE_GAP, OUTCOME_EXPIRED_PENDING)
    assert aged.body is None and aged.settled_at == now
    assert not any("stale" in seen for seen in _seen(fake.calls))


def test_a_partition_with_no_verified_unit_settles_it_and_calls_no_provider(
    cluster: ClusterSession,
    recording: EvidenceWorkspace,
    fake: FakeExtractionProvider,
    now: datetime,
) -> None:
    """Step 3a: a speaker with no membership has no acceptance context. Recorded as a
    member, then the membership row is deleted."""
    member_ctx = _member_context(cluster, recording, ContextPurpose.INTERNAL_ANALYSIS)
    (record,) = _record(
        recording, member_ctx, _texts("departed", 1), at=now - timedelta(minutes=2)
    )
    assert member_ctx.principal.account_id is not None
    _revoke(cluster, recording, member_ctx.principal.account_id)

    batch = _claim(recording, now=now)

    assert batch.units == ()
    assert batch.follow_up_at is None
    row = _row(recording, record)
    assert (row.state, row.outcome) == (STATE_SETTLED, OUTCOME_AUTHORITY_UNVERIFIED)
    assert row.settled_at == now and row.body is None
    assert fake.calls == []


def test_an_authority_refusal_settles_the_row_through_the_placeholder_unit(
    monkeypatch: pytest.MonkeyPatch,
    recording: EvidenceWorkspace,
    fake: FakeExtractionProvider,
    now: datetime,
) -> None:
    """Step 3b. A real speaker mismatch is unreachable through ``claim_units`` (core
    builds the principal from the row), so a stub refuses the marked row."""
    marked, kept = _record(
        recording,
        recording.context(ContextPurpose.INTERNAL_ANALYSIS),
        ["The marked turn about the bicycle shed.", "[fake-extract] The kept turn."],
        at=now - timedelta(minutes=3),
    )
    original = RuntimeEvidenceAuthority.verify
    received: dict[str, TrustedSourceUnit] = {}

    def stub(
        self: RuntimeEvidenceAuthority,
        unit: TrustedSourceUnit,
        *,
        workspace_id: UUID,
        now: datetime,
    ) -> Any:
        received[unit.external_source_key] = unit
        if unit.external_source_key == marked.native_key:
            return AuthorityRefused(reason=SPEAKER_MISMATCH)
        return original(self, unit, workspace_id=workspace_id, now=now)

    monkeypatch.setattr(RuntimeEvidenceAuthority, "verify", stub)

    batch = _claim(recording, now=now)

    assert received[marked.native_key].evidence is NOOP_EVIDENCE
    assert [u.unit.external_source_key for u in batch.units] == [kept.native_key]
    row = _row(recording, marked)
    assert (row.state, row.outcome) == (STATE_SETTLED, OUTCOME_AUTHORITY_UNVERIFIED)
    assert row.settled_at == now and row.body is None
    seen = _seen(fake.calls)
    assert len(fake.calls) == 1 and len(seen) == 1
    assert "bicycle shed" not in seen[0]


# --- partitioning (R9) ----------------------------------------------------------------


def test_each_claim_holds_exactly_one_partition(
    cluster: ClusterSession,
    recording: EvidenceWorkspace,
    fake: FakeExtractionProvider,
    now: datetime,
) -> None:
    owner_internal = recording.context(ContextPurpose.INTERNAL_ANALYSIS)
    owner_referral = recording.context(ContextPurpose.SHARE_WITH_REFERRAL)
    member_internal = _member_context(
        cluster, recording, ContextPurpose.INTERNAL_ANALYSIS
    )
    _record(
        recording, owner_internal, _texts("p-one", 2), at=now - timedelta(minutes=30)
    )
    _record(
        recording, owner_referral, _texts("p-two", 2), at=now - timedelta(minutes=20)
    )
    _record(
        recording, member_internal, _texts("p-three", 1), at=now - timedelta(minutes=10)
    )
    expected = [
        ("p-one", 2, owner_internal, now),
        ("p-two", 2, owner_referral, now),
        ("p-three", 1, member_internal, None),
    ]
    rows_by_id = {row.id: row for row in _rows(recording).values()}

    for claim_number, (prefix, size, ctx, follow_up) in enumerate(expected):
        batch = _drain(recording, now=now)
        seen = [item.text for item in fake.calls[claim_number].items]
        assert len(seen) == size and all(prefix in body for body in seen), seen
        assert batch.follow_up_at == follow_up
        for claimed in batch.units:
            row = rows_by_id[claimed.evidence_id]
            assert claimed.unit.bound_purpose == ContextPurpose(row.purpose)
            assert claimed.unit.bound_purpose == ctx.principal.bound_purpose
            assert claimed.context.principal.bound_purpose == ContextPurpose(
                row.purpose
            )
            assert claimed.context.principal.account_id == row.speaker_account_id
            assert claimed.unit.principal_account_id == row.speaker_account_id
            assert claimed.unit.evidence.purposes == ()
    assert len(fake.calls) == len(expected)


# --- workspace ceiling (W1) -----------------------------------------------------------


def test_workspace_audience_units_share_one_partition(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    """A null ``audience_id`` matches only through ``IS NOT DISTINCT FROM``."""
    _record(
        recording,
        recording.context(ContextPurpose.INTERNAL_ANALYSIS),
        _texts("ceiling", 2),
        at=now - timedelta(minutes=4),
        workspace_audience=True,
    )

    batch = _claim(recording, now=now)

    assert len(batch.units) == 2
    assert {(u.unit.audience_kind, u.unit.audience_id) for u in batch.units} == {
        ("workspace", None)
    }
    assert batch.follow_up_at is None


def test_a_partition_read_that_misses_its_anchor_raises(
    monkeypatch: pytest.MonkeyPatch,
    recording: EvidenceWorkspace,
    fake: FakeExtractionProvider,
    now: datetime,
) -> None:
    _record(
        recording,
        recording.context(ContextPurpose.INTERNAL_ANALYSIS),
        _texts("ceiling", 2),
        at=now - timedelta(minutes=4),
        workspace_audience=True,
    )
    original: Callable[..., Sequence[Any]] = evidence_service._partition_rows

    def missing_anchor(
        uow: UnitOfWork, anchor: Any, *, now: datetime, limit: int
    ) -> Sequence[Any]:
        rows = original(uow, anchor, now=now, limit=limit)
        return [row for row in rows if row.id != anchor.id]

    monkeypatch.setattr(evidence_service, "_partition_rows", missing_anchor)

    with pytest.raises(EvidenceClaimInconsistent):
        _claim(recording, now=now)
    assert {row.state for row in _rows(recording).values()} == {STATE_PENDING}
    assert fake.calls == []


# --- locks ----------------------------------------------------------------------------


def _lock(connection: Connection, ids: Sequence[UUID]) -> None:
    locked = connection.execute(
        select(evidence_unit.c.id).where(evidence_unit.c.id.in_(ids)).with_for_update()
    ).all()
    assert len(locked) == len(ids)


def test_rows_another_drain_holds_are_skipped_without_waiting(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    _record(
        recording, recording.context(), _texts("held", 3), at=now - timedelta(minutes=5)
    )
    ids = [row.id for row in _rows(recording).values()]

    with _other_connection(recording) as other:
        _lock(other, ids)
        batch = _claim(recording, now=now)
        other.rollback()

    assert batch.units == ()
    assert batch.follow_up_at is None
    assert fake.calls == []


def test_aging_skips_an_expired_row_another_drain_holds(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    held, *others = _record(
        recording,
        recording.context(),
        _texts("expired", 3),
        at=now - timedelta(hours=26),
    )
    held_id = _row(recording, held).id

    with _other_connection(recording) as other:
        _lock(other, [held_id])
        _claim(recording, now=now)
        other.rollback()

    assert _row(recording, held).state == STATE_PENDING
    for record in others:
        row = _row(recording, record)
        assert (row.state, row.outcome) == (STATE_GAP, OUTCOME_EXPIRED_PENDING)


def test_no_drain_loop_only_a_waiting_row_schedules_a_follow_up(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    """Every pending row is claimable (``retry_after`` in the past) but locked by
    another drain: an empty claim asks for no follow-up, neither ``now`` nor that past
    instant. Then one unlocked waiting row is the only thing that schedules one."""
    ctx = recording.context()
    locked = _record(recording, ctx, _texts("locked", 2), at=now - timedelta(minutes=9))
    _set(
        recording, locked, retry_after=now - timedelta(seconds=1), extraction_attempts=1
    )
    # A waiting row the other drain holds too: it backed that row off itself and
    # schedules its own follow-up, so this claim derives nothing from it.
    held_waiting = _record(
        recording, ctx, _texts("held", 1), at=now - timedelta(minutes=8)
    )
    _set(recording, held_waiting, retry_after=now + timedelta(seconds=30))
    locked_ids = [_row(recording, record).id for record in [*locked, *held_waiting]]

    with _other_connection(recording) as other:
        _lock(other, locked_ids)

        empty = _claim(recording, now=now)
        assert empty.units == ()
        assert empty.follow_up_at is None

        waiting = _record(
            recording, ctx, _texts("waiting", 1), at=now - timedelta(minutes=1)
        )
        _set(recording, waiting, retry_after=now + timedelta(seconds=20))
        scheduled = _claim(recording, now=now)
        other.rollback()

    assert scheduled.units == ()
    assert scheduled.follow_up_at == now + timedelta(seconds=20)
    assert fake.calls == []


# --- settled_at everywhere ------------------------------------------------------------


def test_every_non_pending_row_carries_settled_at_after_a_full_drain(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    recording: EvidenceWorkspace,
    now: datetime,
) -> None:
    _install(monkeypatch, _FailingFor(malformed=False))
    owner_internal = recording.context(ContextPurpose.INTERNAL_ANALYSIS)
    _record(
        recording,
        owner_internal,
        ["[fake-extract] The shed key is under the blue pot.", "A plain noop turn."],
        at=now - timedelta(minutes=40),
    )
    member_ctx = _member_context(cluster, recording, ContextPurpose.INTERNAL_ANALYSIS)
    _record(
        recording, member_ctx, _texts("departed", 1), at=now - timedelta(minutes=30)
    )
    assert member_ctx.principal.account_id is not None
    _revoke(cluster, recording, member_ctx.principal.account_id)
    _record(
        recording,
        recording.context(ContextPurpose.SHARE_WITH_REFERRAL),
        [f"A {FAILING_MARKER} turn."],
        at=now - timedelta(minutes=20),
    )
    _record(recording, owner_internal, _texts("stale", 1), at=now - timedelta(hours=25))

    at = now
    for _ in range(20):
        batch = _drain(recording, now=at)
        if batch.follow_up_at is None:
            break
        at = max(at, batch.follow_up_at)

    rows = list(_rows(recording).values())
    assert {row.outcome for row in rows} == {
        "active",
        "noop",
        OUTCOME_AUTHORITY_UNVERIFIED,
        OUTCOME_EXTRACTION_FAILED,
        OUTCOME_EXPIRED_PENDING,
    }
    with recording.unit_of_work() as uow:
        unstamped = uow.connection.execute(
            select(func.count())
            .select_from(evidence_unit)
            .where(
                evidence_unit.c.state != STATE_PENDING,
                evidence_unit.c.settled_at.is_(None),
            )
        ).scalar_one()
    assert unstamped == 0


# --- extraction failure and rotation (R11, W6) ----------------------------------------


@pytest.mark.parametrize(
    ("malformed", "error_type"),
    [(False, "RuntimeError"), (True, "ExtractionOutputInvalid")],
    ids=["provider-raises", "malformed-response"],
)
def test_a_failing_partition_backs_off_and_never_blocks_another(
    monkeypatch: pytest.MonkeyPatch,
    recording: EvidenceWorkspace,
    caplog: pytest.LogCaptureFixture,
    now: datetime,
    malformed: bool,
    error_type: str,
) -> None:
    provider = _install(monkeypatch, _FailingFor(malformed=malformed))
    failing = _record(
        recording,
        recording.context(ContextPurpose.INTERNAL_ANALYSIS),
        [f"First {FAILING_MARKER} turn.", f"Second {FAILING_MARKER} turn."],
        at=now - timedelta(minutes=10),
    )
    (healthy,) = _record(
        recording,
        recording.context(ContextPurpose.SHARE_WITH_REFERRAL),
        ["[fake-extract] The healthy partition's turn."],
        at=now - timedelta(minutes=5),
    )

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        first = _drain(recording, now=now)
        assert first.units == ()
        assert first.follow_up_at == now  # the healthy partition is still claimable
        for record in failing:
            row = _row(recording, record)
            assert row.state == STATE_PENDING and row.settled_at is None
            assert row.extraction_attempts == 1
            assert row.retry_after == now + timedelta(seconds=5)

        second = _drain(recording, now=now)
        assert [u.unit.external_source_key for u in second.units] == [
            healthy.native_key
        ]
        assert _row(recording, healthy).state == STATE_SETTLED
        assert second.follow_up_at == now + timedelta(seconds=5)

        at = now
        for attempt, wait in enumerate((5, 20, 80, 320), start=2):
            at += timedelta(seconds=wait)
            batch = _drain(recording, now=at)
            assert batch.units == ()
            for record in failing:
                row = _row(recording, record)
                assert row.extraction_attempts == attempt
                if attempt < EXTRACTION_MAX_ATTEMPTS:
                    assert row.state == STATE_PENDING
            if attempt < EXTRACTION_MAX_ATTEMPTS:
                assert batch.follow_up_at is not None and batch.follow_up_at > at
            else:
                assert batch.follow_up_at is None

    for record in failing:
        row = _row(recording, record)
        assert (row.state, row.outcome) == (STATE_GAP, OUTCOME_EXTRACTION_FAILED)
        assert row.body is None and row.settled_at == at
    lines = [r for r in caplog.records if r.getMessage() == EXTRACTION_FAILED_LOG]
    assert len(lines) == EXTRACTION_MAX_ATTEMPTS
    for line in lines:
        assert getattr(line, "error_type", None) == error_type
        assert getattr(line, "unit_count", None) == 2
        assert FAILING_MARKER not in repr(vars(line))
        assert line.exc_info is None
    # Every provider call held one partition only.
    for batch_seen in provider.calls:
        texts = [item.text for item in batch_seen.items]
        assert all(FAILING_MARKER in t for t in texts) or not any(
            FAILING_MARKER in t for t in texts
        )


def test_a_past_retry_after_on_a_claimed_row_schedules_no_follow_up(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    """A row whose back-off has run out is claimable, not waiting: claiming it whole
    leaves nothing to follow up, not its past ``retry_after``."""
    records = _record(
        recording,
        recording.context(),
        _texts("returned", 2),
        at=now - timedelta(minutes=6),
    )
    _set(
        recording,
        records,
        extraction_attempts=1,
        retry_after=now - timedelta(seconds=1),
    )

    batch = _claim(recording, now=now)

    assert len(batch.units) == 2
    assert batch.follow_up_at is None


def test_a_failed_unit_sorts_after_a_never_failed_one_recorded_later(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    """``extraction_attempts`` leads the anchor order: the older, once-failed (and
    claimable again) row is not the anchor."""
    older = _record(
        recording,
        recording.context(ContextPurpose.INTERNAL_ANALYSIS),
        _texts("once-failed", 1),
        at=now - timedelta(minutes=10),
    )
    _set(
        recording, older, extraction_attempts=1, retry_after=now - timedelta(seconds=1)
    )
    (later,) = _record(
        recording,
        recording.context(ContextPurpose.SHARE_WITH_REFERRAL),
        _texts("never-failed", 1),
        at=now - timedelta(minutes=1),
    )

    batch = _claim(recording, now=now)

    assert [u.unit.external_source_key for u in batch.units] == [later.native_key]
    seen = _seen(fake.calls)
    assert len(seen) == 1 and "never-failed" in seen[0]
    assert batch.follow_up_at == now


# --- settle and audit -----------------------------------------------------------------


AUDIT_MARKER: Final = "quokka-marker-0142"


def test_an_active_settlement_writes_one_content_free_audit_row(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    (record,) = _record(
        recording,
        recording.context(ContextPurpose.INTERNAL_ANALYSIS),
        [f"[fake-extract] The {AUDIT_MARKER} rota moved to Tuesdays."],
        at=now - timedelta(minutes=2),
    )
    before = _audit_rows(recording)
    memory_ref = f"harness.note:{uuid7()}"

    with recording.unit_of_work() as uow:
        view = handler_uow(uow, probe_registry())
        (claimed,) = claim_units(view, now=now).units
        assert AUDIT_MARKER in claimed.unit.evidence.title  # anti-vacuity
        settle_unit(
            view,
            claimed,
            outcome="active",
            memory_ref=memory_ref,
            audit_module_id=HARNESS_MODULE_ID,
            now=now,
        )
        uow.commit()

    (row,) = _added_audit(recording, before)
    assert row.operation_name == EVIDENCE_ACCEPT_AUDIT
    assert row.safety_class == "mutate"
    assert row.outcome == AUDIT_SUCCEEDED
    assert row.operation_id is None
    assert (row.actor_kind, row.actor_id, row.entry) == ("system", None, "job")
    assert row.subject_ref == RecordRef.parse(memory_ref).format()
    assert row.request_digest == claimed.unit.payload_digest()
    for field in dataclasses.fields(row):
        value = getattr(row, field.name)
        rendered = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        assert AUDIT_MARKER not in rendered, field.name
    settled = _row(recording, record)
    assert (settled.state, settled.outcome) == (STATE_SETTLED, "active")
    assert settled.body is None and settled.settled_at == now


def test_outcomes_other_than_active_write_no_audit_row(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    records = _record(
        recording,
        recording.context(ContextPurpose.INTERNAL_ANALYSIS),
        _texts("quiet", 3),
        at=now - timedelta(minutes=2),
    )
    outcomes = ("noop", "denied", OUTCOME_AUTHORITY_UNVERIFIED)
    before = _audit_rows(recording)

    with recording.unit_of_work() as uow:
        view = handler_uow(uow, probe_registry())
        batch = claim_units(view, now=now)
        assert len(batch.units) == len(outcomes)
        for claimed, outcome in zip(batch.units, outcomes, strict=True):
            settle_unit(
                view,
                claimed,
                outcome=outcome,
                memory_ref=None,
                audit_module_id=HARNESS_MODULE_ID,
                now=now,
            )
        uow.commit()

    assert _added_audit(recording, before) == []
    assert sorted(_row(recording, r).outcome for r in records) == sorted(outcomes)


def test_no_sink_raises_before_the_row_settles_and_the_rollback_keeps_it_pending(
    recording: EvidenceWorkspace,
    fake: FakeExtractionProvider,
    caplog: pytest.LogCaptureFixture,
    now: datetime,
) -> None:
    (record,) = _record(
        recording,
        recording.context(ContextPurpose.INTERNAL_ANALYSIS),
        ["[fake-extract] The compost bins are full."],
        at=now - timedelta(minutes=2),
    )
    before = _audit_rows(recording)

    with (
        caplog.at_level(logging.ERROR, logger="rheo_core.operations"),
        recording.unit_of_work() as uow,
    ):
        view = handler_uow(uow, probe_registry())
        (claimed,) = claim_units(view, now=now).units
        with pytest.raises(EvidenceAuditUnwritable):
            settle_unit(
                view,
                claimed,
                outcome="active",
                memory_ref=f"harness.note:{uuid7()}",
                audit_module_id=UNSINKED_MODULE_ID,
                now=now,
            )
        # Raised before the settlement: in this very transaction the row is still
        # pending, so the caller's rollback has nothing of it to undo but the claim.
        state = uow.connection.execute(
            select(evidence_unit.c.state).where(
                evidence_unit.c.id == claimed.evidence_id
            )
        ).scalar_one()
        assert state == STATE_PENDING
        uow.rollback()

    assert any(r.getMessage() == "audit_row_unwritable" for r in caplog.records)
    assert _added_audit(recording, before) == []
    assert _row(recording, record).state == STATE_PENDING
    assert [u.evidence_id for u in _claim(recording, now=now).units] == [
        claimed.evidence_id
    ]


def test_an_active_outcome_without_a_memory_ref_raises_and_settles_nothing(
    recording: EvidenceWorkspace, fake: FakeExtractionProvider, now: datetime
) -> None:
    _record(
        recording,
        recording.context(ContextPurpose.INTERNAL_ANALYSIS),
        ["[fake-extract] The gate code changed."],
        at=now - timedelta(minutes=2),
    )
    before = _audit_rows(recording)

    with recording.unit_of_work() as uow:
        view = handler_uow(uow, probe_registry())
        (claimed,) = claim_units(view, now=now).units
        with pytest.raises(ValueError, match="memory"):
            settle_unit(
                view,
                claimed,
                outcome="active",
                memory_ref=None,
                audit_module_id=HARNESS_MODULE_ID,
                now=now,
            )
        state = uow.connection.execute(
            select(evidence_unit.c.state).where(
                evidence_unit.c.id == claimed.evidence_id
            )
        ).scalar_one()
        assert state == STATE_PENDING

    assert _added_audit(recording, before) == []

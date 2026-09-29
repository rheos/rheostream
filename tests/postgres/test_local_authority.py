"""``LocalEvidenceAuthority.verify`` against real ``core.evidence_unit`` and
``core.evidence_enrollment`` rows (spec § Architecture → System Components, item 1;
FR 4; AC 2, AC 3).

Seams:

- the grant: a pending ``claude_code_local`` row under an active enrollment of the
  same account and purpose verifies, and the grant is the row's;
- the six refusal words, each by its own fixture: ``producer_mismatch``,
  ``source_unavailable`` (a missing, a deleted, a settled and a gap row),
  ``speaker_mismatch``, ``enrollment_inactive`` (no enrollment, a revoked one),
  ``enrollment_mismatch`` (account, purpose, audience) and ``membership_revoked``;
- AC 2's "absent or already expired by the time it is claimed": an expired pending row
  is aged ``gap/expired_pending`` by ``claim_units`` before any authority runs, so no
  memory or receipt exists, and ``verify`` then refuses it;
- both reads carry ``FOR SHARE`` in the caller's transaction;
- every refusal is a bare word from the closed vocabulary, nothing appended (AC 3).

Evidence rows are written through ``record_evidence`` as the speaker's own context,
with ``producer_kind = "claude_code_local"`` and ``authority_id`` the enrollment's id.
Enrollment rows are inserted directly; nothing in this file enrolls through an
operation. Fingerprints, keys and texts are synthetic, and every row this file writes
is removed in teardown (shared preamble, "Test-row hygiene").
"""

import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from conftest import ClusterSession
from harness.evidence import EvidenceWorkspace, enable_recording, probe_registry
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.registry import add_member
from rheo_contracts import ContextPurpose, Role, WorkspaceContext
from rheo_contracts.source_units import (
    AuthorityGrant,
    AuthorityRefused,
    ProducerKind,
    SanitizedEvidence,
    SourceAudienceKind,
    TrustedSourceUnit,
)
from rheo_core.boundary import context_for_harness
from rheo_core.evidence import claim_units
from rheo_core.evidence.local_authority import (
    ENROLLMENT_INACTIVE,
    ENROLLMENT_MISMATCH,
    LOCAL_PRODUCER_KIND,
    MEMBERSHIP_REVOKED,
    PRODUCER_MISMATCH,
    REFUSAL_REASONS,
    SOURCE_UNAVAILABLE,
    SPEAKER_MISMATCH,
    LocalEvidenceAuthority,
)
from rheo_core.evidence.record import NewEvidence, record_evidence
from rheo_core.operations import register_core_operations
from rheo_core.refs import uuid7
from rheo_core.storage import control_tables
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.evidence_enrollment_tables import (
    STATE_ACTIVE,
    STATE_REVOKED,
    evidence_enrollment,
)
from rheo_core.storage.evidence_tables import (
    OUTCOME_EXPIRED_PENDING,
    STATE_GAP,
    STATE_SETTLED,
    evidence_unit,
)
from rheo_recallatron.configuration import MODULE_ID
from rheo_recallatron.storage import tables as memory_tables
from sqlalchemy import delete, event, insert, inspect, select, update

pytestmark = pytest.mark.postgres

TURN = "Remember that the spare projector cable is in the blue drawer."
_BARE_WORD = re.compile(r"^[a-z_]+$")


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture(autouse=True)
def clean_evidence_rows(cluster: ClusterSession, workspace: UUID) -> Iterator[None]:
    """Remove this test's evidence and enrollment rows before the next test runs, so
    no revoked enrollment or settled row outlives it (#238's rule)."""
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
def ev(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[EvidenceWorkspace]:
    """Recallatron installed and enabled, and recording on with the ``fake`` provider:
    the acceptance harness's own fixture."""
    with loaded_probe_modules(monkeypatch, MODULE_ID):
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, MODULE_ID)
        evidence = EvidenceWorkspace(cluster, workspace, owner_account_id)
        enable_recording(monkeypatch, evidence)
        yield evidence


# --- helpers ----------------------------------------------------------------------


def _fingerprint() -> str:
    return secrets.token_hex(32)


def _enroll(
    ev: EvidenceWorkspace,
    *,
    account_id: UUID,
    purpose: ContextPurpose,
    state: str = STATE_ACTIVE,
) -> UUID:
    """Insert one enrollment row directly; its id, the evidence ``authority_id``."""
    enrollment_id = uuid7()
    now = datetime.now(UTC)
    with ev.unit_of_work() as uow:
        uow.connection.execute(
            insert(evidence_enrollment).values(
                id=enrollment_id,
                account_id=account_id,
                token_id=uuid4(),
                machine_fingerprint=_fingerprint(),
                project_fingerprint=_fingerprint(),
                purpose=purpose.value,
                state=state,
                created_at=now,
                revoked_at=None if state == STATE_ACTIVE else now,
            )
        )
        uow.commit()
    return enrollment_id


def _record(
    ev: EvidenceWorkspace,
    ctx: WorkspaceContext,
    *,
    authority_id: UUID,
    audience_kind: SourceAudienceKind = "member",
    audience_id: UUID | None = None,
    recorded_at: datetime | None = None,
) -> NewEvidence:
    """Record one local turn as ``ctx``'s own account and purpose; the record written.

    The audience defaults to the speaker's own ``member`` ceiling; pass
    ``audience_kind="workspace"`` for a null ``audience_id``.
    """
    account_id = ctx.principal.account_id
    purpose = ctx.principal.bound_purpose
    assert account_id is not None and purpose is not None
    record = NewEvidence(
        producer_kind=LOCAL_PRODUCER_KIND,
        authority_id=authority_id,
        native_key=f"cc1:{_fingerprint()}",
        speaker_account_id=account_id,
        audience_kind=audience_kind,
        audience_id=(
            None
            if audience_kind == "workspace"
            else account_id
            if audience_id is None
            else audience_id
        ),
        purpose=purpose,
        recorded_at=datetime.now(UTC) if recorded_at is None else recorded_at,
        raw_text=TURN,
    )
    with ev.handler(probe_registry()) as uow:
        attempt = record_evidence(ctx, uow, [record], now=datetime.now(UTC))
    assert attempt.accepted == (record.native_key,), attempt
    return record


def _enrolled_record(
    ev: EvidenceWorkspace,
    purpose: ContextPurpose = ContextPurpose.INTERNAL_ANALYSIS,
) -> NewEvidence:
    """The owner's active enrollment and one pending turn recorded under it."""
    enrollment_id = _enroll(ev, account_id=ev.owner_account_id, purpose=purpose)
    return _record(ev, ev.context(purpose), authority_id=enrollment_id)


def _unit(
    record: NewEvidence,
    *,
    producer_kind: ProducerKind = LOCAL_PRODUCER_KIND,
    principal_account_id: UUID | None = None,
) -> TrustedSourceUnit:
    """The unit a drain would build for ``record``, with one field overridden."""
    return TrustedSourceUnit(
        producer_kind=producer_kind,
        authority_id=record.authority_id,
        principal_account_id=(
            record.speaker_account_id
            if principal_account_id is None
            else principal_account_id
        ),
        audience_kind=record.audience_kind,
        audience_id=record.audience_id,
        bound_purpose=record.purpose,
        source_recorded_at=record.recorded_at,
        source_expires_at=record.recorded_at + timedelta(hours=24),
        external_source_key=record.native_key,
        evidence=SanitizedEvidence(
            kind="note", title="Projector cable", body="The cable is in the drawer."
        ),
    )


def _verify(
    ev: EvidenceWorkspace, unit: TrustedSourceUnit
) -> AuthorityGrant | AuthorityRefused:
    """``verify`` inside one unit of work, the authority bound to it."""
    with ev.unit_of_work() as uow:
        return LocalEvidenceAuthority(uow).verify(
            unit, workspace_id=ev.workspace_id, now=datetime.now(UTC)
        )


def _row(ev: EvidenceWorkspace, record: NewEvidence) -> Any:
    with ev.unit_of_work() as uow:
        return uow.connection.execute(
            evidence_unit.select().where(
                evidence_unit.c.native_key == record.native_key
            )
        ).one()


def _rows(ev: EvidenceWorkspace, table: Any) -> list[Any]:
    with ev.unit_of_work() as uow:
        return list(uow.connection.execute(select(table)))


def _settle(
    ev: EvidenceWorkspace, record: NewEvidence, state: str, outcome: str
) -> None:
    """Move a recorded row out of ``pending`` the way the drain does."""
    with ev.unit_of_work() as uow:
        moved = uow.connection.execute(
            update(evidence_unit)
            .where(evidence_unit.c.native_key == record.native_key)
            .values(
                state=state, outcome=outcome, body=None, settled_at=datetime.now(UTC)
            )
        ).rowcount
        uow.commit()
    assert moved == 1


def _refused(result: AuthorityGrant | AuthorityRefused, reason: str) -> None:
    """The refusal is exactly ``reason``: a bare word, nothing appended (AC 3)."""
    assert isinstance(result, AuthorityRefused), result
    assert result.reason == reason
    assert result.reason in REFUSAL_REASONS
    assert _BARE_WORD.match(result.reason), result.reason


# --- the grant --------------------------------------------------------------------


@pytest.mark.parametrize(
    "purpose",
    [ContextPurpose.INTERNAL_ANALYSIS, ContextPurpose.RESPOND],
    ids=lambda purpose: purpose.value,
)
def test_a_pending_row_under_an_active_enrollment_verifies_with_the_rows_values(
    recording: EvidenceWorkspace, purpose: ContextPurpose
) -> None:
    record = _enrolled_record(recording, purpose)
    row = _row(recording, record)

    grant = _verify(recording, _unit(record))

    assert grant == AuthorityGrant(
        workspace_id=recording.workspace_id,
        producer_kind=LOCAL_PRODUCER_KIND,
        authority_id=row.authority_id,
        principal_account_id=row.speaker_account_id,
        audience_kind=row.audience_kind,
        audience_id=row.audience_id,
        bound_purpose=ContextPurpose(row.purpose),
        source_revision=1,
        required_links=(),
    )
    assert grant.bound_purpose is purpose
    assert grant.principal_account_id == recording.owner_account_id
    assert grant.audience_kind == "member"
    assert grant.audience_id == recording.owner_account_id


def test_verify_reads_the_row_and_the_enrollment_for_share(
    recording: EvidenceWorkspace,
) -> None:
    """Both reads lock in the caller's transaction, so neither row can move under
    the check."""
    record = _enrolled_record(recording)
    executed: list[str] = []

    def capture(
        conn: Any, cursor: Any, statement: str, *args: Any, **kwargs: Any
    ) -> None:
        executed.append(statement)

    with recording.unit_of_work() as uow:
        event.listen(uow.connection, "before_cursor_execute", capture)
        try:
            grant = LocalEvidenceAuthority(uow).verify(
                _unit(record),
                workspace_id=recording.workspace_id,
                now=datetime.now(UTC),
            )
        finally:
            event.remove(uow.connection, "before_cursor_execute", capture)
    assert isinstance(grant, AuthorityGrant), grant
    for table in ("core.evidence_unit", "core.evidence_enrollment"):
        reads = [sql for sql in executed if table in sql]
        assert len(reads) == 1, executed
        assert reads[0].rstrip().endswith("FOR SHARE"), reads[0]


# --- producer_mismatch ------------------------------------------------------------


def test_a_unit_of_another_producer_kind_refuses_producer_mismatch(
    recording: EvidenceWorkspace,
) -> None:
    record = _enrolled_record(recording)
    _refused(
        _verify(recording, _unit(record, producer_kind="rheo_runtime")),
        PRODUCER_MISMATCH,
    )


# --- source_unavailable -----------------------------------------------------------


@pytest.mark.parametrize("how", ["never_written", "deleted"])
def test_evidence_absent_at_claim_refuses_source_unavailable(
    recording: EvidenceWorkspace, how: str
) -> None:
    """The row was never written, or the retention sweep deleted it, before
    ``verify`` runs; the enrollment itself stands."""
    record = _enrolled_record(recording)
    if how == "never_written":
        unit = _unit(
            NewEvidence(
                producer_kind=LOCAL_PRODUCER_KIND,
                authority_id=record.authority_id,
                native_key=f"cc1:{_fingerprint()}",
                speaker_account_id=record.speaker_account_id,
                audience_kind=record.audience_kind,
                audience_id=record.audience_id,
                purpose=record.purpose,
                recorded_at=record.recorded_at,
                raw_text=TURN,
            )
        )
    else:
        # Anti-vacuity: the same unit verifies while its row stands.
        unit = _unit(record)
        assert isinstance(_verify(recording, unit), AuthorityGrant)
        with recording.unit_of_work() as uow:
            deleted = uow.connection.execute(
                delete(evidence_unit).where(
                    evidence_unit.c.native_key == record.native_key
                )
            ).rowcount
            uow.commit()
        assert deleted == 1
    _refused(_verify(recording, unit), SOURCE_UNAVAILABLE)


def test_a_settled_row_refuses_source_unavailable(recording: EvidenceWorkspace) -> None:
    record = _enrolled_record(recording)
    _settle(recording, record, STATE_SETTLED, "active")
    _refused(_verify(recording, _unit(record)), SOURCE_UNAVAILABLE)


def test_a_gap_row_refuses_source_unavailable(recording: EvidenceWorkspace) -> None:
    record = _enrolled_record(recording)
    _settle(recording, record, STATE_GAP, "extraction_failed")
    _refused(_verify(recording, _unit(record)), SOURCE_UNAVAILABLE)


def test_evidence_expired_before_claim_settles_expired_and_makes_no_memory(
    ev: EvidenceWorkspace,
) -> None:
    """A pending local row past its ``source_expires_at`` is aged by ``claim_units``
    before any authority is looked up: it settles ``gap/expired_pending``, nothing is
    claimed, no memory or receipt exists, and ``verify`` then refuses it."""
    record = _enrolled_record(ev)
    row = _row(ev, record)
    claim_at = row.source_expires_at + timedelta(seconds=1)

    with ev.unit_of_work() as base:
        uow = HandlerUnitOfWork(base, operation_id=None, consumers=probe_registry())
        batch = claim_units(uow, now=claim_at)
        base.commit()

    assert batch.units == ()
    aged = _row(ev, record)
    assert (aged.state, aged.outcome) == (STATE_GAP, OUTCOME_EXPIRED_PENDING)
    assert aged.body is None
    assert _rows(ev, memory_tables.memory) == []
    assert _rows(ev, memory_tables.source_receipt) == []
    _refused(_verify(ev, _unit(record)), SOURCE_UNAVAILABLE)


# --- speaker_mismatch -------------------------------------------------------------


def test_a_unit_naming_another_principal_refuses_speaker_mismatch(
    recording: EvidenceWorkspace,
) -> None:
    record = _enrolled_record(recording)
    _refused(
        _verify(recording, _unit(record, principal_account_id=uuid7())),
        SPEAKER_MISMATCH,
    )


# --- enrollment_inactive ----------------------------------------------------------


def test_a_row_with_no_enrollment_refuses_enrollment_inactive(
    recording: EvidenceWorkspace,
) -> None:
    ctx = recording.context(ContextPurpose.INTERNAL_ANALYSIS)
    record = _record(recording, ctx, authority_id=uuid7())
    _refused(_verify(recording, _unit(record)), ENROLLMENT_INACTIVE)


def test_a_revoked_enrollment_refuses_enrollment_inactive(
    recording: EvidenceWorkspace,
) -> None:
    """AC 2's revoked-enrollment case: the row is still pending, the enrollment it
    was ingested under is not."""
    record = _enrolled_record(recording)
    # Anti-vacuity: the same unit verifies while the enrollment is active.
    assert isinstance(_verify(recording, _unit(record)), AuthorityGrant)
    with recording.unit_of_work() as uow:
        moved = uow.connection.execute(
            update(evidence_enrollment)
            .where(evidence_enrollment.c.id == record.authority_id)
            .values(state=STATE_REVOKED, revoked_at=datetime.now(UTC))
        ).rowcount
        uow.commit()
    assert moved == 1
    _refused(_verify(recording, _unit(record)), ENROLLMENT_INACTIVE)


# --- enrollment_mismatch ----------------------------------------------------------


def test_an_enrollment_of_another_account_refuses_enrollment_mismatch(
    recording: EvidenceWorkspace,
) -> None:
    enrollment_id = _enroll(
        recording, account_id=uuid7(), purpose=ContextPurpose.INTERNAL_ANALYSIS
    )
    record = _record(
        recording,
        recording.context(ContextPurpose.INTERNAL_ANALYSIS),
        authority_id=enrollment_id,
    )
    _refused(_verify(recording, _unit(record)), ENROLLMENT_MISMATCH)


def test_an_enrollment_of_another_purpose_refuses_enrollment_mismatch(
    recording: EvidenceWorkspace,
) -> None:
    enrollment_id = _enroll(
        recording,
        account_id=recording.owner_account_id,
        purpose=ContextPurpose.RESPOND,
    )
    record = _record(
        recording,
        recording.context(ContextPurpose.INTERNAL_ANALYSIS),
        authority_id=enrollment_id,
    )
    _refused(_verify(recording, _unit(record)), ENROLLMENT_MISMATCH)


@pytest.mark.parametrize("audience", ["workspace", "another_member"])
def test_a_row_outside_the_enrolled_members_audience_refuses_enrollment_mismatch(
    recording: EvidenceWorkspace, audience: str
) -> None:
    """An enrollment admits only its own account's ``member`` ceiling: a row under
    the ``workspace`` ceiling, or under another member's, is refused."""
    enrollment_id = _enroll(
        recording,
        account_id=recording.owner_account_id,
        purpose=ContextPurpose.INTERNAL_ANALYSIS,
    )
    ctx = recording.context(ContextPurpose.INTERNAL_ANALYSIS)
    if audience == "workspace":
        record = _record(
            recording, ctx, authority_id=enrollment_id, audience_kind="workspace"
        )
    else:
        record = _record(
            recording, ctx, authority_id=enrollment_id, audience_id=uuid7()
        )
    _refused(_verify(recording, _unit(record)), ENROLLMENT_MISMATCH)


# --- membership_revoked -----------------------------------------------------------


def test_a_speaker_whose_membership_was_revoked_refuses_membership_revoked(
    cluster: ClusterSession, recording: EvidenceWorkspace
) -> None:
    member = add_member(
        cluster.backend, recording.workspace_id, Role.MEMBER, display_name="speaker"
    )
    ctx = context_for_harness(
        recording.workspace_id,
        member,
        Role.MEMBER,
        bound_purpose=ContextPurpose.INTERNAL_ANALYSIS,
    )
    assert isinstance(ctx, WorkspaceContext), ctx
    enrollment_id = _enroll(
        recording, account_id=member, purpose=ContextPurpose.INTERNAL_ANALYSIS
    )
    record = _record(recording, ctx, authority_id=enrollment_id)
    # Anti-vacuity: the same unit verifies while the membership stands.
    assert isinstance(_verify(recording, _unit(record)), AuthorityGrant)

    with cluster.backend.control_engine.begin() as connection:
        deleted = connection.execute(
            delete(control_tables.membership).where(
                (control_tables.membership.c.account_id == member)
                & (control_tables.membership.c.workspace_id == recording.workspace_id)
            )
        ).rowcount
    assert deleted == 1

    _refused(_verify(recording, _unit(record)), MEMBERSHIP_REVOKED)


# --- the vocabulary and the public surface ----------------------------------------


def test_the_refusal_vocabulary_is_exactly_the_six_words() -> None:
    assert (
        frozenset(
            {
                "producer_mismatch",
                "source_unavailable",
                "speaker_mismatch",
                "enrollment_inactive",
                "enrollment_mismatch",
                "membership_revoked",
            }
        )
        == REFUSAL_REASONS
    )


def test_the_authority_exposes_nothing_public_but_verify(
    recording: EvidenceWorkspace,
) -> None:
    with recording.unit_of_work() as uow:
        authority = LocalEvidenceAuthority(uow)
        assert [n for n in dir(authority) if not n.startswith("_")] == ["verify"]

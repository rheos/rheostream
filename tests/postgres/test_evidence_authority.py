"""``RuntimeEvidenceAuthority.verify`` against real ``core.evidence_unit`` rows (spec
§ System Components, item 4; FR 3, the authority half).

Seams:

- the four refusal words, each by its own case: ``producer_mismatch``,
  ``source_unavailable`` (a settled row, a gap row, a missing row),
  ``speaker_mismatch`` and ``membership_revoked``;
- the pending-only rule: only a ``pending`` row verifies;
- the grant is the row's: principal, audience ceiling and ``bound_purpose`` come from
  core's row, never from the unit, for ``internal_analysis`` and
  ``share_with_referral`` alike;
- the authority exposes nothing public but ``verify``.

Rows are written through the shared recording fixture, as the speaker's own context
(``record_evidence`` refuses a record that is not the context's account and purpose).
A speaker mismatch is staged on the unit, never on the row. Every text is synthetic.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.evidence import EvidenceWorkspace, enable_recording, probe_registry
from harness.registry import add_member
from rheo_contracts import ContextPurpose, Role, WorkspaceContext
from rheo_contracts.source_units import (
    AuthorityGrant,
    AuthorityRefused,
    ProducerKind,
    SanitizedEvidence,
    TrustedSourceUnit,
)
from rheo_core.boundary import context_for_harness
from rheo_core.evidence.authority import (
    MEMBERSHIP_REVOKED,
    PRODUCER_MISMATCH,
    REFUSAL_REASONS,
    SOURCE_UNAVAILABLE,
    SPEAKER_MISMATCH,
    RuntimeEvidenceAuthority,
)
from rheo_core.evidence.record import NewEvidence, record_evidence
from rheo_core.operations import register_core_operations
from rheo_core.refs import uuid7
from rheo_core.storage import control_tables
from rheo_core.storage.evidence_tables import (
    STATE_GAP,
    STATE_SETTLED,
    evidence_unit,
)
from sqlalchemy import delete, update

pytestmark = pytest.mark.postgres

TURN = "Please move the Thursday planning session to the small meeting room."


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


def _record(
    ev: EvidenceWorkspace,
    ctx: WorkspaceContext,
) -> NewEvidence:
    """Record one turn as ``ctx``'s own account and purpose; the record written."""
    account_id = ctx.principal.account_id
    purpose = ctx.principal.bound_purpose
    assert account_id is not None and purpose is not None
    authority_id = uuid7()
    record = NewEvidence(
        producer_kind="rheo_runtime",
        authority_id=authority_id,
        native_key=f"turn:{authority_id}",
        speaker_account_id=account_id,
        audience_kind="member",
        audience_id=account_id,
        purpose=purpose,
        recorded_at=datetime.now(UTC),
        raw_text=TURN,
    )
    with ev.handler(probe_registry()) as uow:
        attempt = record_evidence(ctx, uow, [record], now=datetime.now(UTC))
    assert attempt.accepted == (record.native_key,), attempt
    return record


def _unit(
    record: NewEvidence,
    *,
    producer_kind: ProducerKind = "rheo_runtime",
    principal_account_id: UUID | None = None,
    bound_purpose: ContextPurpose | None = None,
) -> TrustedSourceUnit:
    """The unit a drain would build for ``record``, with any one field overridden."""
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
        bound_purpose=record.purpose if bound_purpose is None else bound_purpose,
        source_recorded_at=record.recorded_at,
        source_expires_at=record.recorded_at + timedelta(hours=24),
        external_source_key=record.native_key,
        evidence=SanitizedEvidence(
            kind="note", title="Planning room", body="The planning session moved."
        ),
    )


def _verify(
    ev: EvidenceWorkspace, unit: TrustedSourceUnit
) -> AuthorityGrant | AuthorityRefused:
    """``verify`` inside one unit of work, the authority bound to it."""
    with ev.unit_of_work() as uow:
        return RuntimeEvidenceAuthority(uow).verify(
            unit, workspace_id=ev.workspace_id, now=datetime.now(UTC)
        )


def _row(ev: EvidenceWorkspace, record: NewEvidence) -> Any:
    with ev.unit_of_work() as uow:
        return uow.connection.execute(
            evidence_unit.select().where(
                evidence_unit.c.native_key == record.native_key
            )
        ).one()


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
    assert isinstance(result, AuthorityRefused), result
    assert result.reason == reason
    assert result.reason in REFUSAL_REASONS


# --- a pending row verifies, and the grant is the row's ------------------------------


@pytest.mark.parametrize(
    "purpose",
    [ContextPurpose.INTERNAL_ANALYSIS, ContextPurpose.SHARE_WITH_REFERRAL],
    ids=lambda purpose: purpose.value,
)
def test_a_pending_row_verifies_with_the_rows_own_values(
    recording: EvidenceWorkspace, purpose: ContextPurpose
) -> None:
    record = _record(recording, recording.context(purpose))
    row = _row(recording, record)

    grant = _verify(recording, _unit(record))

    assert isinstance(grant, AuthorityGrant), grant
    assert grant == AuthorityGrant(
        workspace_id=recording.workspace_id,
        producer_kind="rheo_runtime",
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


def test_the_grant_purpose_is_the_rows_not_the_units(
    recording: EvidenceWorkspace,
) -> None:
    """A unit claiming another purpose still gets the row's: the seam's own
    ``unit.bound_purpose != grant.bound_purpose`` check is what then refuses it."""
    record = _record(recording, recording.context(ContextPurpose.INTERNAL_ANALYSIS))
    grant = _verify(
        recording, _unit(record, bound_purpose=ContextPurpose.SHARE_WITH_REFERRAL)
    )
    assert isinstance(grant, AuthorityGrant), grant
    assert grant.bound_purpose is ContextPurpose.INTERNAL_ANALYSIS


# --- source_unavailable: pending only -------------------------------------------------


def test_a_settled_row_refuses_source_unavailable(recording: EvidenceWorkspace) -> None:
    record = _record(recording, recording.context(ContextPurpose.INTERNAL_ANALYSIS))
    _settle(recording, record, STATE_SETTLED, "active")
    _refused(_verify(recording, _unit(record)), SOURCE_UNAVAILABLE)


def test_a_gap_row_refuses_source_unavailable(recording: EvidenceWorkspace) -> None:
    record = _record(recording, recording.context(ContextPurpose.INTERNAL_ANALYSIS))
    _settle(recording, record, STATE_GAP, "extraction_failed")
    _refused(_verify(recording, _unit(record)), SOURCE_UNAVAILABLE)


def test_a_missing_row_refuses_source_unavailable(recording: EvidenceWorkspace) -> None:
    record = _record(recording, recording.context(ContextPurpose.INTERNAL_ANALYSIS))
    other_run = uuid7()
    never_recorded = NewEvidence(
        producer_kind="rheo_runtime",
        authority_id=other_run,
        native_key=f"turn:{other_run}",
        speaker_account_id=record.speaker_account_id,
        audience_kind=record.audience_kind,
        audience_id=record.audience_id,
        purpose=record.purpose,
        recorded_at=record.recorded_at,
        raw_text=TURN,
    )
    _refused(_verify(recording, _unit(never_recorded)), SOURCE_UNAVAILABLE)


# --- speaker_mismatch -----------------------------------------------------------------


def test_a_unit_naming_another_principal_refuses_speaker_mismatch(
    recording: EvidenceWorkspace,
) -> None:
    record = _record(recording, recording.context(ContextPurpose.INTERNAL_ANALYSIS))
    _refused(
        _verify(recording, _unit(record, principal_account_id=uuid7())),
        SPEAKER_MISMATCH,
    )


# --- membership_revoked ---------------------------------------------------------------


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
    record = _record(recording, ctx)
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


# --- producer_mismatch ----------------------------------------------------------------


def test_a_unit_of_another_producer_kind_refuses_producer_mismatch(
    recording: EvidenceWorkspace,
) -> None:
    record = _record(recording, recording.context(ContextPurpose.INTERNAL_ANALYSIS))
    _refused(
        _verify(recording, _unit(record, producer_kind="migration")),
        PRODUCER_MISMATCH,
    )


# --- the public surface ---------------------------------------------------------------


def test_the_authority_exposes_nothing_public_but_verify(
    recording: EvidenceWorkspace,
) -> None:
    with recording.unit_of_work() as uow:
        authority = RuntimeEvidenceAuthority(uow)
        assert [n for n in dir(authority) if not n.startswith("_")] == ["verify"]

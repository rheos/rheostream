"""``claim_units`` checks each claimed row by its own producer kind's authority (spec
§ Requirements FR 5, Edge Case 8; § Architecture → System Components item 2; AC 9).

Seams under test:

- **AC 9: an authority per row, not per partition.** One partition holds a
  ``rheo_runtime`` row and a ``claude_code_local`` row with the same speaker, audience
  and purpose. Run once with each kind anchoring it (the earlier ``recorded_at`` sorts
  first), both rows reach the ``fake`` provider in one batch, and each
  :class:`~rheo_core.evidence.ClaimedUnit` carries its own kind's authority, which
  grants that unit in the claim's own transaction.
- **A revoked enrollment costs only its own rows.** With the local row's enrollment
  revoked, that row settles ``settled/authority_unverified`` while the runtime row in
  the same partition still reaches the provider and is claimed, again with each kind
  anchoring in turn.
- **The batch is scoped.** The provider's batch carries the workspace, the anchor's
  speaker and its purpose.

Claims run directly on a handler unit of work, so no Recallatron acceptance is
involved. Enrollment rows are inserted directly. Every text, key and fingerprint is
synthetic, and every evidence and enrollment row is removed in teardown (#238's rule).
"""

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from conftest import ClusterSession
from harness.evidence import EvidenceWorkspace, enable_recording, probe_registry
from rheo_contracts import ContextPurpose, WorkspaceContext
from rheo_contracts.source_units import AuthorityGrant, AuthorityRefused
from rheo_core.evidence import ClaimedBatch, claim_units
from rheo_core.evidence.authority import RuntimeEvidenceAuthority
from rheo_core.evidence.extract import DigestBatch, ExtractionScope
from rheo_core.evidence.local_authority import (
    LOCAL_PRODUCER_KIND,
    LocalEvidenceAuthority,
)
from rheo_core.evidence.providers import (
    FAKE_EXTRACTION_MARKER,
    FAKE_PROVIDER,
    FakeExtractionProvider,
    providers,
)
from rheo_core.evidence.record import NewEvidence, record_evidence
from rheo_core.operations import register_core_operations
from rheo_core.refs import uuid7
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.evidence_enrollment_tables import (
    STATE_ACTIVE,
    STATE_REVOKED,
    evidence_enrollment,
)
from rheo_core.storage.evidence_tables import (
    OUTCOME_AUTHORITY_UNVERIFIED,
    STATE_PENDING,
    STATE_SETTLED,
    evidence_unit,
)
from sqlalchemy import Row, delete, insert, inspect, select, update

from postgres.test_automatic_memory_acceptance import _new_evidence

pytestmark = pytest.mark.postgres

_PURPOSE: Final = ContextPurpose.RESPOND
_LOCAL_TURN: Final = f"{FAKE_EXTRACTION_MARKER} The spare lantern is on shelf four."
_RUNTIME_TURN: Final = f"{FAKE_EXTRACTION_MARKER} The ladder hangs by the back door."
_ANCHORS: Final = ("runtime_anchors", "local_anchors")


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture(autouse=True)
def clean_evidence_rows(cluster: ClusterSession, workspace: UUID) -> Iterator[None]:
    """Remove this test's evidence and enrollment rows, so no settled row or revoked
    enrollment outlives it for ``rheo doctor`` to read (#238's rule)."""
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
    """Recording on, with the ``fake`` provider resolving."""
    evidence = EvidenceWorkspace(cluster, workspace, owner_account_id)
    enable_recording(monkeypatch, evidence)
    yield evidence


@pytest.fixture
def fake() -> FakeExtractionProvider:
    provider = providers()[FAKE_PROVIDER]
    assert isinstance(provider, FakeExtractionProvider)
    provider.reset_calls()
    return provider


# --- helpers, shared with test_automatic_memory_local_poison.py ------------------


def _fingerprint() -> str:
    return secrets.token_hex(32)


def enroll(ev: EvidenceWorkspace, *, purpose: ContextPurpose = _PURPOSE) -> UUID:
    """One active enrollment of the owner, inserted directly; its id is the local
    evidence rows' ``authority_id``."""
    enrollment_id = uuid7()
    with ev.unit_of_work() as uow:
        uow.connection.execute(
            insert(evidence_enrollment).values(
                id=enrollment_id,
                account_id=ev.owner_account_id,
                token_id=uuid4(),
                machine_fingerprint=_fingerprint(),
                project_fingerprint=_fingerprint(),
                purpose=purpose.value,
                state=STATE_ACTIVE,
                created_at=datetime.now(UTC),
                revoked_at=None,
            )
        )
        uow.commit()
    return enrollment_id


def revoke(ev: EvidenceWorkspace, enrollment_id: UUID) -> None:
    with ev.unit_of_work() as uow:
        uow.connection.execute(
            update(evidence_enrollment)
            .where(evidence_enrollment.c.id == enrollment_id)
            .values(state=STATE_REVOKED, revoked_at=datetime.now(UTC))
        )
        uow.commit()


def local_evidence(
    ctx: WorkspaceContext, body: str, *, enrollment_id: UUID, at: datetime
) -> NewEvidence:
    """A local turn of ``ctx``'s own account, on its ``member`` ceiling, under the
    enrollment: the shape the ingest path writes."""
    account_id = ctx.principal.account_id
    purpose = ctx.principal.bound_purpose
    assert account_id is not None and purpose is not None
    return NewEvidence(
        producer_kind=LOCAL_PRODUCER_KIND,
        authority_id=enrollment_id,
        native_key=f"cc1:{_fingerprint()}",
        speaker_account_id=account_id,
        audience_kind="member",
        audience_id=account_id,
        purpose=purpose,
        recorded_at=at,
        raw_text=body,
    )


def record(ev: EvidenceWorkspace, records: list[NewEvidence], *, at: datetime) -> None:
    """One attempt as the owner bound to the shared purpose, published to the probe
    only; every record must be accepted."""
    with ev.handler(probe_registry()) as uow:
        attempt = record_evidence(ev.context(_PURPOSE), uow, records, now=at)
    assert attempt.accepted == tuple(r.native_key for r in records), attempt


# --- this file's helpers ------------------------------------------------------------


def _mixed_partition(
    ev: EvidenceWorkspace, anchor: str, *, now: datetime
) -> tuple[NewEvidence, NewEvidence, UUID]:
    """A runtime row and a local row in one partition, ``anchor``'s recorded first so
    it sorts first; answers ``(runtime, local, enrollment id)``."""
    earlier, later = now - timedelta(seconds=10), now - timedelta(seconds=5)
    if anchor == "runtime_anchors":
        runtime_at, local_at = earlier, later
    else:
        runtime_at, local_at = later, earlier
    enrollment_id = enroll(ev)
    ctx = ev.context(_PURPOSE)
    runtime = _new_evidence(ctx, _RUNTIME_TURN, at=runtime_at)
    local = local_evidence(ctx, _LOCAL_TURN, enrollment_id=enrollment_id, at=local_at)
    record(ev, [runtime, local], at=now)
    return runtime, local, enrollment_id


def _claim_and_grant(
    ev: EvidenceWorkspace, *, now: datetime
) -> tuple[ClaimedBatch, dict[str, AuthorityGrant | AuthorityRefused]]:
    """One claim, and each claimed unit's own authority asked to verify it inside the
    claim's transaction, keyed by native key; then commit."""
    grants: dict[str, AuthorityGrant | AuthorityRefused] = {}
    with ev.unit_of_work() as base:
        uow = HandlerUnitOfWork(base, operation_id=None, consumers=probe_registry())
        batch = claim_units(uow, now=now)
        for claimed in batch.units:
            grants[claimed.unit.external_source_key] = claimed.authority.verify(
                claimed.unit, workspace_id=ev.workspace_id, now=now
            )
        base.commit()
    return batch, grants


def _row(ev: EvidenceWorkspace, native_key: str) -> Row[Any]:
    with ev.unit_of_work() as uow:
        return uow.connection.execute(
            select(evidence_unit).where(evidence_unit.c.native_key == native_key)
        ).one()


def _anchor_first(
    anchor: str, runtime: NewEvidence, local: NewEvidence
) -> list[NewEvidence]:
    return [runtime, local] if anchor == "runtime_anchors" else [local, runtime]


# --- AC 9 ---------------------------------------------------------------------------


@pytest.mark.parametrize("anchor", _ANCHORS)
def test_ac9_each_row_of_a_mixed_partition_is_authorized_by_its_own_kind(
    ev: EvidenceWorkspace, fake: FakeExtractionProvider, anchor: str
) -> None:
    """Both rows reach the provider in one batch, in claim order, and each claimed
    unit's authority is its own kind's and grants it, whichever kind anchors."""
    now = datetime.now(UTC)
    runtime, local, _ = _mixed_partition(ev, anchor, now=now)

    batch, grants = _claim_and_grant(ev, now=now)

    [call] = fake.calls
    ordered = _anchor_first(anchor, runtime, local)
    assert [item.text for item in call.items] == [r.raw_text for r in ordered]
    by_key = {claimed.unit.external_source_key: claimed for claimed in batch.units}
    assert list(by_key) == [r.native_key for r in ordered]

    runtime_unit, local_unit = by_key[runtime.native_key], by_key[local.native_key]
    assert runtime_unit.unit.producer_kind == "rheo_runtime"
    assert type(runtime_unit.authority) is RuntimeEvidenceAuthority
    assert local_unit.unit.producer_kind == LOCAL_PRODUCER_KIND
    assert type(local_unit.authority) is LocalEvidenceAuthority
    for record_, kind in ((runtime, "rheo_runtime"), (local, LOCAL_PRODUCER_KIND)):
        grant = grants[record_.native_key]
        assert isinstance(grant, AuthorityGrant), grant
        assert (grant.producer_kind, grant.authority_id) == (
            kind,
            record_.authority_id,
        )
    # Nothing was settled at claim time: both rows are the drain's to accept.
    for record_ in ordered:
        row = _row(ev, record_.native_key)
        assert (row.state, row.outcome) == (STATE_PENDING, None)


@pytest.mark.parametrize("anchor", _ANCHORS)
def test_ac9_a_revoked_enrollment_settles_only_its_own_row(
    ev: EvidenceWorkspace, fake: FakeExtractionProvider, anchor: str
) -> None:
    """The local row settles ``authority_unverified``; the runtime row beside it still
    reaches the provider and is claimed with the runtime authority."""
    now = datetime.now(UTC)
    runtime, local, enrollment_id = _mixed_partition(ev, anchor, now=now)
    revoke(ev, enrollment_id)

    batch, grants = _claim_and_grant(ev, now=now)

    [call] = fake.calls
    assert [item.text for item in call.items] == [runtime.raw_text]
    [claimed] = batch.units
    assert claimed.unit.external_source_key == runtime.native_key
    assert type(claimed.authority) is RuntimeEvidenceAuthority
    assert isinstance(grants[runtime.native_key], AuthorityGrant)

    settled = _row(ev, local.native_key)
    assert (settled.state, settled.outcome, settled.body) == (
        STATE_SETTLED,
        OUTCOME_AUTHORITY_UNVERIFIED,
        None,
    )
    assert _row(ev, runtime.native_key).state == STATE_PENDING


def test_the_batch_is_scoped_to_the_workspace_and_the_anchor(
    ev: EvidenceWorkspace, fake: FakeExtractionProvider
) -> None:
    """``DigestBatch.scope`` names the workspace, the anchor's speaker and purpose."""
    now = datetime.now(UTC)
    _mixed_partition(ev, "local_anchors", now=now)

    _claim_and_grant(ev, now=now)

    [call] = fake.calls
    assert isinstance(call, DigestBatch)
    assert call.scope == ExtractionScope(
        workspace_id=ev.workspace_id,
        account_id=ev.owner_account_id,
        purpose=_PURPOSE,
    )

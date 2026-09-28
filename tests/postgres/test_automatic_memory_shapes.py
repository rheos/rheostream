"""Automatic memory, acceptance criteria 8 and 9, and the runtime half of 11(b): the
``claude_code_local`` shape at the seam, module absence, and what a real claim hands
Recallatron (spec § Acceptance criteria 8, 9 and 11; § Purpose binding, "The
``claude_code_local`` fixture"; FR 8, FR 26).

Seams under test:

- **AC-8: the seam admits 1a4b's producer kind without this run building it.** The
  authority below is test code only; no production ``claude_code_local`` authority
  exists. It is called through Recallatron's own ``accept_source_unit`` directly, with
  no producer plumbing and no hook, in three legs: an unknown machine, a revoked
  enrollment and an unverifiable principal each answer ``authority_unverified`` with
  no receipt at all; an account-backed, member-audience, both-instants unit is
  ``active`` bound ``internal_analysis`` and again bound ``share_with_referral``, its
  memory carrying exactly that one purpose each time; and a unit bound
  ``share_with_referral`` under an enrollment bound ``internal_analysis`` is
  ``denied`` with no memory.
- **AC-9: no Recallatron, no effect.** In a workspace that never installed the module,
  with ``automatic_memory.enabled`` on at both levels and the ``fake`` provider
  resolving, a real runtime run succeeds and leaves no evidence row, no job but its
  own, no recorded event, no delivery, no memory schema and no evidence log line.
  Condition 3 is what makes this hold, and the positive control proves it: the same
  run with a subscriber that *is* enabled records one row. Two legs register nothing
  off Recallatron's manifest; the third, FR 26's case, loads Recallatron's real
  subscription into the registry and still sees every zero, because the workspace
  never enabled the module.
- **AC-11(b), runtime half.** On a batch a real ``claim_units`` returned, no value of
  the batch or its units is a raw handle, and the authority's only public attribute is
  ``verify``. Its private unit of work is the one sanctioned exception, pinned by
  identity. The static half is ``tests/test_evidence_boundary.py``.

AC-10 is not repeated here. Its scans are
``test_evidence_is_a_separate_vocabulary_from_the_transcript_kinds`` in
``tests/postgres/test_runtime_evidence_hook.py`` and
``test_the_runtime_transcript_is_unchanged`` in
``tests/postgres/test_evidence_tables.py``. Every text and identifier is synthetic.
"""

import dataclasses
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.evidence import EvidenceWorkspace, enable_recording, probe_registry
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.registry import register_harness
from harness.runtime_matrix import SECRET_REF, SECRET_VALUE, workspace_engine
from rheo_contracts import ContextPurpose, Role, WorkspaceContext
from rheo_contracts.source_units import (
    AuthorityGrant,
    AuthorityRefused,
    SanitizedEvidence,
    TrustedSourceUnit,
)
from rheo_core.boundary import context_for_harness
from rheo_core.events import ConsumerRegistry
from rheo_core.evidence import EVIDENCE_RECORDED, claim_units
from rheo_core.evidence.authority import RuntimeEvidenceAuthority
from rheo_core.operations import register_core_operations
from rheo_core.refs import uuid7
from rheo_core.runtime import RUNTIME_RUN
from rheo_core.storage import work_tables
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_recallatron.configuration import MODULE_ID
from rheo_recallatron.source_units import AcceptOutcome, accept_source_unit
from rheo_recallatron.storage import tables as memory_tables
from sqlalchemy import Connection, Engine, Row, Select, Table, select, text

from postgres.test_automatic_memory_acceptance import (
    _TURN,
    _consumers,
    _record,
    _rows,
)
from postgres.test_runtime_evidence_hook import (
    _assert_reached_the_hook_and_finished,
    _probe_deliveries,
    _run,
    _units,
)

pytestmark = pytest.mark.postgres

_LOCAL_BRIDGE: Final = "claude_code_local"
_FACT: Final = "The spare projector cable is in the second drawer."

_UNKNOWN_MACHINE: Final = "unknown_machine"
_ENROLLMENT_REVOKED: Final = "enrollment_revoked"
_PRINCIPAL_UNVERIFIABLE: Final = "principal_unverifiable"

_RAW_HANDLES: Final[tuple[type, ...]] = (
    Connection,
    Engine,
    Table,
    Select,
    Row,
    UnitOfWork,
)


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model credential a runtime run binds; the double adapter never uses it."""
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", "api_key")
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_ref", SECRET_REF)
    monkeypatch.setenv("RHEO_ANTHROPIC_API_KEY", SECRET_VALUE)


@pytest.fixture
def ev(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[EvidenceWorkspace]:
    """Recallatron installed and enabled: the seam's own workspace, for AC-8."""
    with loaded_probe_modules(monkeypatch, MODULE_ID):
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, MODULE_ID)
        yield EvidenceWorkspace(cluster, workspace, owner_account_id)


@pytest.fixture
def absent(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> EvidenceWorkspace:
    """A workspace that never installed Recallatron, recording conditions 1 and 2 on."""
    evidence = EvidenceWorkspace(cluster, workspace, owner_account_id)
    enable_recording(monkeypatch, evidence)
    return evidence


# --- AC-8: the claude_code_local shape at the seam ----------------------------------


@dataclass(frozen=True)
class _Enrollment:
    """One enrolled machine, as a later local bridge might hold it. Synthetic."""

    account_id: UUID
    bound_purpose: ContextPurpose
    revoked: bool = False


@dataclass(frozen=True)
class SyntheticLocalBridgeAuthority:
    """A ``claude_code_local`` authority over a fixed table of enrolled machines.

    Test code only, and deliberately thin: an enrollment is keyed by the unit's
    ``authority_id`` (the machine), binds one account and one purpose, and can be
    revoked. It answers what it verified and nothing the unit claims beyond its
    identity, which is all the seam asks of any authority.
    """

    enrollments: Mapping[UUID, _Enrollment]

    def verify(
        self, unit: TrustedSourceUnit, *, workspace_id: UUID, now: datetime
    ) -> AuthorityGrant | AuthorityRefused:
        enrollment = self.enrollments.get(unit.authority_id)
        if unit.producer_kind != _LOCAL_BRIDGE or enrollment is None:
            return AuthorityRefused(reason=_UNKNOWN_MACHINE)
        if enrollment.revoked:
            return AuthorityRefused(reason=_ENROLLMENT_REVOKED)
        if unit.principal_account_id != enrollment.account_id:
            return AuthorityRefused(reason=_PRINCIPAL_UNVERIFIABLE)
        return AuthorityGrant(
            workspace_id=workspace_id,
            producer_kind=_LOCAL_BRIDGE,
            authority_id=unit.authority_id,
            principal_account_id=enrollment.account_id,
            audience_kind="member",
            audience_id=enrollment.account_id,
            bound_purpose=enrollment.bound_purpose,
            source_revision=1,
        )


def _local_unit(
    account_id: UUID, machine_id: UUID, purpose: ContextPurpose, *, at: datetime
) -> TrustedSourceUnit:
    """An account-backed, member-audience unit with both source instants."""
    return TrustedSourceUnit(
        producer_kind=_LOCAL_BRIDGE,
        authority_id=machine_id,
        principal_account_id=account_id,
        audience_kind="member",
        audience_id=account_id,
        bound_purpose=purpose,
        source_recorded_at=at - timedelta(minutes=5),
        source_expires_at=at + timedelta(days=1),
        external_source_key=f"local-turn:{uuid7()}",
        evidence=SanitizedEvidence(
            kind="fact", title="Where the projector cable is", body=_FACT
        ),
    )


def _accept(
    ev: EvidenceWorkspace,
    unit: TrustedSourceUnit,
    authority: SyntheticLocalBridgeAuthority,
    *,
    at: datetime,
) -> AcceptOutcome:
    """One direct seam call under the owner's context bound to the unit's purpose."""
    assert unit.bound_purpose is not None
    with ev.unit_of_work() as uow:
        outcome = accept_source_unit(
            ev.context(unit.bound_purpose),
            uow,
            unit,
            authority=authority,
            consumers=ConsumerRegistry(),
            now=at,
        )
        uow.commit()
    return outcome


def _memory_purposes(ev: EvidenceWorkspace, memory_id: UUID) -> set[str]:
    return {
        row.purpose
        for row in _rows(ev, memory_tables.memory_purpose)
        if row.memory_id == memory_id
    }


def _refusing_case(
    ev: EvidenceWorkspace, reason: str, at: datetime
) -> tuple[TrustedSourceUnit, SyntheticLocalBridgeAuthority]:
    machine_id = uuid7()
    owner = ev.owner_account_id
    internal = ContextPurpose.INTERNAL_ANALYSIS
    unit = _local_unit(owner, machine_id, internal, at=at)
    if reason == _UNKNOWN_MACHINE:
        return unit, SyntheticLocalBridgeAuthority({})
    if reason == _ENROLLMENT_REVOKED:
        enrollment = _Enrollment(owner, internal, revoked=True)
        return unit, SyntheticLocalBridgeAuthority({machine_id: enrollment})
    # An unverifiable principal: the machine is enrolled to another account.
    other = _Enrollment(uuid7(), internal)
    return unit, SyntheticLocalBridgeAuthority({machine_id: other})


@pytest.mark.parametrize(
    "reason", [_UNKNOWN_MACHINE, _ENROLLMENT_REVOKED, _PRINCIPAL_UNVERIFIABLE]
)
def test_ac8_an_unverifiable_local_unit_is_refused_with_no_receipt(
    ev: EvidenceWorkspace, reason: str
) -> None:
    at = datetime.now(UTC)
    unit, authority = _refusing_case(ev, reason, at)
    refused = authority.verify(unit, workspace_id=ev.workspace_id, now=at)
    assert refused == AuthorityRefused(reason=reason)

    outcome = _accept(ev, unit, authority, at=at)

    assert outcome == AcceptOutcome("authority_unverified")
    assert _rows(ev, memory_tables.source_receipt) == []
    assert _rows(ev, memory_tables.memory) == []


def test_ac8_a_verified_local_unit_is_active_bound_to_each_purpose_in_turn(
    ev: EvidenceWorkspace,
) -> None:
    """Once bound ``internal_analysis`` and once ``share_with_referral``: each memory
    carries exactly its own bound purpose, and each receipt names the local bridge."""
    at = datetime.now(UTC)
    owner = ev.owner_account_id
    refs: dict[ContextPurpose, str] = {}
    for purpose in (
        ContextPurpose.INTERNAL_ANALYSIS,
        ContextPurpose.SHARE_WITH_REFERRAL,
    ):
        machine_id = uuid7()
        authority = SyntheticLocalBridgeAuthority(
            {machine_id: _Enrollment(owner, purpose)}
        )
        outcome = _accept(
            ev, _local_unit(owner, machine_id, purpose, at=at), authority, at=at
        )
        assert outcome.state == "active" and outcome.memory_ref is not None, outcome
        refs[purpose] = outcome.memory_ref

    memories = {row.id: row for row in _rows(ev, memory_tables.memory)}
    receipts = _rows(ev, memory_tables.source_receipt)
    assert len(memories) == len(receipts) == 2
    for receipt in receipts:
        memory = memories[receipt.record_id]
        assert receipt.state == "active"
        assert receipt.producer_kind == _LOCAL_BRIDGE
        assert receipt.principal_account_id == owner
        assert memory.origin == "derived"
        assert (memory.audience_kind, memory.audience_id) == ("member", owner)
        assert _memory_purposes(ev, memory.id) == {receipt.bound_purpose}
        assert refs[ContextPurpose(receipt.bound_purpose)].endswith(str(memory.id))
    assert {receipt.bound_purpose for receipt in receipts} == {
        ContextPurpose.INTERNAL_ANALYSIS.value,
        ContextPurpose.SHARE_WITH_REFERRAL.value,
    }


def test_ac8_a_unit_bound_to_another_purpose_than_its_enrollment_is_denied(
    ev: EvidenceWorkspace,
) -> None:
    """The seam's own bound-purpose-versus-grant check: a verified caller failing
    policy, so a ``denied`` receipt in its own partition and no memory."""
    at = datetime.now(UTC)
    owner = ev.owner_account_id
    machine_id = uuid7()
    authority = SyntheticLocalBridgeAuthority(
        {machine_id: _Enrollment(owner, ContextPurpose.INTERNAL_ANALYSIS)}
    )
    unit = _local_unit(owner, machine_id, ContextPurpose.SHARE_WITH_REFERRAL, at=at)

    outcome = _accept(ev, unit, authority, at=at)

    assert outcome == AcceptOutcome("denied")
    assert _rows(ev, memory_tables.memory) == []
    [receipt] = _rows(ev, memory_tables.source_receipt)
    assert (receipt.state, receipt.record_id) == ("denied", None)
    assert receipt.producer_kind == _LOCAL_BRIDGE


# --- AC-9: a workspace without Recallatron ------------------------------------------


def _engine(ev: EvidenceWorkspace) -> Engine:
    return workspace_engine(ev.cluster, ev.workspace_id)


def _all(ev: EvidenceWorkspace, table: Table) -> list[Row[Any]]:
    with _engine(ev).connect() as connection:
        return list(connection.execute(select(table)).all())


def _memory_schema_exists(ev: EvidenceWorkspace) -> bool:
    with _engine(ev).connect() as connection:
        return (
            connection.execute(
                text(
                    "SELECT 1 FROM information_schema.schemata "
                    "WHERE schema_name = :name"
                ),
                {"name": memory_tables.MEMORY_SCHEMA},
            ).first()
            is not None
        )


def _evidence_log(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name.startswith("rheo_core.evidence")
    ]


def _assert_no_pipeline_trace(
    absent: EvidenceWorkspace,
    consumers: ConsumerRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One real run with ``consumers`` wired in, and every AC-9 zero after it."""
    caplog.set_level(logging.DEBUG, logger="rheo_core.evidence")
    assert MODULE_ID not in absent.context().enabled_modules
    assert not _memory_schema_exists(absent)

    operation_id = _run(absent, consumers=consumers)

    _assert_reached_the_hook_and_finished(absent, operation_id)
    [job] = _all(absent, work_tables.job)
    assert (job.kind, job.state, job.attempts, job.last_error) == (
        RUNTIME_RUN,
        "succeeded",
        1,
        None,
    )
    assert _units(absent) == []
    assert [
        event
        for event in _all(absent, work_tables.outbox_event)
        if event.type == EVIDENCE_RECORDED
    ] == []
    assert _all(absent, work_tables.event_delivery) == []
    # No receipt and no memory: the module's schema was never created.
    assert not _memory_schema_exists(absent)
    assert _evidence_log(caplog) == []


def test_ac9_without_recallatron_a_run_leaves_no_trace_of_the_pipeline(
    absent: EvidenceWorkspace, caplog: pytest.LogCaptureFixture
) -> None:
    """No subscriber wired in at all."""
    _assert_no_pipeline_trace(absent, ConsumerRegistry(), caplog)


def test_ac9_recallatrons_loaded_subscription_is_inert_where_it_is_not_enabled(
    absent: EvidenceWorkspace, caplog: pytest.LogCaptureFixture
) -> None:
    """FR 26's real case, and the one exemption from AC-9's no-manifest rule: the
    process has Recallatron's own subscription loaded, off its real manifest, but
    this workspace never enabled the module. Condition 3 counts only a subscriber
    whose module the workspace enabled, so the same zeros hold."""
    consumers = _consumers()
    assert [s.module_id for s in consumers.for_type(EVIDENCE_RECORDED)] == [MODULE_ID]
    _assert_no_pipeline_trace(absent, consumers, caplog)


def test_ac9_positive_control_an_enabled_subscriber_records_the_same_run(
    absent: EvidenceWorkspace,
) -> None:
    """Everything but condition 3 is the same as above: an enabled subscriber on the
    harness module, still no Recallatron. One row, so the zero above is condition 3's
    doing and not a run that never reached the hook."""
    operation_id = _run(absent, consumers=probe_registry())

    _assert_reached_the_hook_and_finished(absent, operation_id)
    [unit] = _units(absent)
    assert unit.native_key == f"turn:{operation_id}"
    assert unit.state == "pending"
    [delivery] = _probe_deliveries(absent)
    assert delivery.state == "delivered"
    assert not _memory_schema_exists(absent)


# --- AC-11(b), runtime half: what a real claim hands over ---------------------------


def _raw(value: object) -> bool:
    return isinstance(value, _RAW_HANDLES)


def test_ac11b_a_claimed_batch_carries_no_raw_handle_but_the_named_one(
    absent: EvidenceWorkspace,
) -> None:
    """The sanctioned exception: ``RuntimeEvidenceAuthority`` holds the caller's own
    unit of work privately, because ``SourceAuthority.verify`` takes none and its
    ``FOR SHARE`` read must run in the caller's transaction (spec component 4). It is
    the unit of work the drain passed into ``claim_units``, so it grants nothing new,
    and Recallatron sees it only as the ``SourceAuthority`` protocol. Pinned by
    identity, so it cannot quietly widen to another handle."""
    at = datetime.now(UTC)
    _record(absent, ContextPurpose.RESPOND, _TURN, at=at)

    with absent.unit_of_work() as base:
        uow = HandlerUnitOfWork(base, operation_id=None, consumers=ConsumerRegistry())
        batch = claim_units(uow, now=at)
        [claimed] = batch.units

        for field in dataclasses.fields(batch):
            assert not _raw(getattr(batch, field.name)), field.name
        for field in dataclasses.fields(claimed):
            assert not _raw(getattr(claimed, field.name)), field.name

        authority = claimed.authority
        public = [name for name in dir(authority) if not name.startswith("_")]
        assert public == ["verify"]
        assert not any(_raw(getattr(authority, name)) for name in public)

        assert isinstance(authority, RuntimeEvidenceAuthority)
        assert authority.__slots__ == ("_uow",)
        assert authority._uow is uow
        # Nothing is committed: the claim is read and rolled back.

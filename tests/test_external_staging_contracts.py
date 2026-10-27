"""Declared external registries and effective guard composition."""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    AuditSpec,
    ExternalSpec,
    Idempotency,
    KeyedSpec,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
)
from rheo_core.approvals.guards import DestinationGuard, effective_guards_for
from rheo_core.approvals.records import ApprovalRow
from rheo_core.modules import (
    DestinationMode,
    DestinationRegistration,
    DestinationRegistry,
    GuardRegistration,
    GuardRegistry,
)
from rheo_core.refs import uuid7


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: str
    ref: RecordRef
    destination_ref: Literal["synthetic"] = "synthetic"
    title: str


class _Output(BaseModel):
    item_ref: str


class _Guard:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def check(self, *args: object, **kwargs: object) -> str | None:
        return None


def _declaration() -> OperationDeclaration:
    return OperationDeclaration(
        name="probe.send.create",
        safety_class=SafetyClass.EXTERNAL,
        roles=frozenset({Role.OWNER}),
        input_model=_Input,
        output=_Output,
        idempotency=Idempotency.KEYED,
        keyed=KeyedSpec(
            key_field="request_key", request_schema_version=2, result_schema_version=3
        ),
        guards=("probe.first", "probe.second"),
        external=ExternalSpec(
            destination_field="destination_ref",
            destination_ref="synthetic",
            result_ref_field="item_ref",
        ),
        audit=AuditSpec(subject_field="ref"),
    )


def _approval(destination_ref: str | None = "synthetic") -> ApprovalRow:
    now = datetime.now(UTC)
    return ApprovalRow(
        id=uuid7(),
        actor_kind="account",
        actor_id=uuid7(),
        operation_name="probe.send.create",
        operation_id=uuid7(),
        destination_ref=destination_ref,
        subject_ref=None,
        subject_revision=1,
        payload_digest=b"x",
        purpose=None,
        window_start=now - timedelta(seconds=1),
        window_end=now + timedelta(seconds=1),
        state="approved",
        approved_by_kind="account",
        approved_by_id=uuid7(),
        approved_at=now,
        approved_entry="web",
        executed_at=None,
        invalidated_reason=None,
    )


def _destination(*, credentials: bool = True) -> DestinationRegistration:
    return DestinationRegistration(
        destination_ref="synthetic",
        operation_name="probe.send.create",
        mode=DestinationMode.IN_PROCESS,
        is_installed=lambda: True,
        is_enabled=lambda ctx: True,
        is_ready=lambda ctx, uow: True,
        credentials_available=lambda ctx, uow: credentials,
        execute=lambda ctx, uow, model: _Output(item_ref="synthetic:item"),
    )


def test_destination_guard_refuses_unregistered_and_unavailable_credentials() -> None:
    registry = DestinationRegistry()
    guard = DestinationGuard(
        "probe.send.create", "destination_ref", "synthetic", registry
    )
    model = _Input(
        request_key="one",
        ref=RecordRef(module="probe", record_type="note", id=uuid7()),
        title="Synthetic",
    )
    now = datetime.now(UTC)
    ctx = cast(Any, object())
    uow = cast(Any, object())

    assert "not registered" in cast(
        str,
        guard.check(ctx, uow, approval=_approval(), model_input=model, now=now),
    )

    registry.register(_destination(credentials=False), origin="probe")
    assert "credentials are unavailable" in cast(
        str,
        guard.check(ctx, uow, approval=_approval(), model_input=model, now=now),
    )


def test_destination_guard_refuses_inexact_approval_or_input_destination() -> None:
    registry = DestinationRegistry()
    registry.register(_destination(), origin="probe")
    guard = DestinationGuard(
        "probe.send.create", "destination_ref", "synthetic", registry
    )
    model = _Input(
        request_key="one",
        ref=RecordRef(module="probe", record_type="note", id=uuid7()),
        title="Synthetic",
    )
    now = datetime.now(UTC)
    ctx = cast(Any, object())
    uow = cast(Any, object())

    # The control: an available destination and an exactly bound approval permit.
    assert (
        guard.check(ctx, uow, approval=_approval(), model_input=model, now=now) is None
    )
    for bound in (None, "other"):
        assert "not bound" in cast(
            str,
            guard.check(
                ctx, uow, approval=_approval(bound), model_input=model, now=now
            ),
        )
    wrong = _Input.model_construct(**{**model.model_dump(), "destination_ref": "other"})
    assert "not the declared exact destination" in cast(
        str,
        guard.check(ctx, uow, approval=_approval(), model_input=wrong, now=now),
    )


def test_staging_guard_composition_is_core_record_destination_then_declared() -> None:
    guards = GuardRegistry()
    guards.register(
        GuardRegistration(name="probe.first", guard=_Guard("probe.first")),
        origin="probe",
    )
    guards.register(
        GuardRegistration(name="probe.second", guard=_Guard("probe.second")),
        origin="probe",
    )

    effective = effective_guards_for(_declaration(), guards=guards)

    assert [guard.name for guard in effective] == [
        "ActorPermissionGuard",
        "WindowGuard",
        "RecordStateGuard",
        "DestinationGuard",
        "probe.first",
        "probe.second",
    ]

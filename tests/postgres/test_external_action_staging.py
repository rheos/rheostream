"""Atomic approval-bound EXTERNAL staging through the transaction service."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field
from rheo_contracts import (
    AuditSpec,
    ExternalSpec,
    Idempotency,
    KeyedSpec,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.approvals.tables import approval
from rheo_core.audit import CORE_AUDIT_SINK, install_sink
from rheo_core.boundary import context_for_harness
from rheo_core.modules import (
    DestinationMode,
    DestinationRegistration,
    DestinationRegistry,
)
from rheo_core.operations import OperationRefused, OperationRegistry
from rheo_core.operations.external_actions import external_action
from rheo_core.operations.idempotency_tables import idempotency_result
from rheo_core.operations.records import APPROVAL_REQUIRED
from rheo_core.operations.transaction import (
    DESTINATION_GUARD_REFUSED,
    stage_external_in_transaction,
)
from rheo_core.settings import CORE_ORIGIN
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import job, operation
from sqlalchemy import func, select

pytestmark = pytest.mark.postgres

OPERATION = "core.external.stage"


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: str
    destination_ref: Literal["synthetic"] = "synthetic"
    title: str = Field(min_length=1)


class _Output(BaseModel):
    item_ref: str


def _declaration() -> OperationDeclaration:
    return OperationDeclaration(
        name=OPERATION,
        safety_class=SafetyClass.EXTERNAL,
        roles=frozenset({Role.OWNER}),
        input_model=_Input,
        output=_Output,
        idempotency=Idempotency.KEYED,
        keyed=KeyedSpec(
            key_field="request_key", request_schema_version=1, result_schema_version=1
        ),
        external=ExternalSpec(
            destination_field="destination_ref",
            destination_ref="synthetic",
            result_ref_field="item_ref",
        ),
        audit=AuditSpec(subject_field=None),
    )


def _context(workspace: UUID, owner_account_id: UUID) -> WorkspaceContext:
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext)
    return ctx


def _registries(calls: list[str]) -> tuple[OperationRegistry, DestinationRegistry]:
    operations = OperationRegistry()
    declaration = _declaration()

    def forbidden_handler(
        ctx: WorkspaceContext, uow: UnitOfWork, model: _Input
    ) -> _Output:
        calls.append("operation")
        return _Output(item_ref="synthetic:item")

    def forbidden_destination(
        ctx: WorkspaceContext, uow: UnitOfWork, model: BaseModel
    ) -> BaseModel:
        calls.append("destination")
        return _Output(item_ref="synthetic:item")

    operations.register(declaration, forbidden_handler, origin=CORE_ORIGIN)
    destinations = DestinationRegistry()
    destinations.register(
        DestinationRegistration(
            destination_ref="synthetic",
            operation_name=OPERATION,
            mode=DestinationMode.IN_PROCESS,
            is_installed=lambda: True,
            is_enabled=lambda ctx: True,
            is_ready=lambda ctx, uow: True,
            credentials_available=None,
            execute=forbidden_destination,
        ),
        origin="core",
    )
    return operations, destinations


def _counts(ctx: WorkspaceContext, key: str) -> tuple[int, int, int, int, int]:
    with open_unit_of_work(ctx) as uow:
        conn = uow.connection
        return (
            int(
                conn.execute(
                    select(func.count())
                    .select_from(idempotency_result)
                    .where(
                        idempotency_result.c.operation_name == OPERATION,
                        idempotency_result.c.idempotency_key == key,
                    )
                ).scalar_one()
            ),
            int(
                conn.execute(
                    select(func.count())
                    .select_from(external_action)
                    .where(
                        external_action.c.operation_name == OPERATION,
                        external_action.c.idempotency_key == key,
                    )
                ).scalar_one()
            ),
            int(
                conn.execute(
                    select(func.count())
                    .select_from(approval)
                    .where(approval.c.operation_name == OPERATION)
                ).scalar_one()
            ),
            int(
                conn.execute(
                    select(func.count())
                    .select_from(operation)
                    .where(operation.c.name == OPERATION)
                ).scalar_one()
            ),
            int(conn.execute(select(func.count()).select_from(job)).scalar_one()),
        )


def test_external_action_staging_is_atomic_bound_and_never_invokes_destination(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls: list[str] = []
    operations, destinations = _registries(calls)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    payload = {
        "request_key": "staging-one",
        "destination_ref": "synthetic",
        "title": "Synthetic work",
    }
    now = datetime.now(UTC)

    with open_unit_of_work(ctx) as uow:
        staged = stage_external_in_transaction(
            ctx,
            uow,
            OPERATION,
            payload,
            now=now,
            registry=operations,
            destinations=destinations,
        )
        uow.commit()
    with open_unit_of_work(ctx) as uow:
        replayed = stage_external_in_transaction(
            ctx,
            uow,
            OPERATION,
            payload,
            now=now,
            registry=operations,
            destinations=destinations,
        )
        uow.commit()

    assert staged.created is True
    assert replayed.created is False
    assert replayed.action_id == staged.action_id
    assert replayed.approval_id == staged.approval_id
    assert replayed.operation_id == staged.operation_id
    assert calls == []
    assert _counts(ctx, "staging-one") == (1, 1, 1, 1, 0)
    with open_unit_of_work(ctx) as uow:
        action = (
            uow.connection.execute(
                select(external_action).where(external_action.c.id == staged.action_id)
            )
            .mappings()
            .one()
        )
        held = uow.connection.execute(
            select(operation.c.state).where(operation.c.id == staged.operation_id)
        ).scalar_one()
        bound = uow.connection.execute(
            select(approval.c.destination_ref, approval.c.payload_digest).where(
                approval.c.id == staged.approval_id
            )
        ).one()
    assert held == APPROVAL_REQUIRED
    assert action["approval_payload_ref"] == staged.approval_id
    assert action["approval_payload_digest"] == bound.payload_digest
    assert bound.destination_ref == "synthetic"


def test_external_action_staging_destination_guard_refuses_before_reservation(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls: list[str] = []
    operations, _ = _registries(calls)
    ctx = _context(workspace, owner_account_id)
    with pytest.raises(OperationRefused) as excinfo:
        with open_unit_of_work(ctx) as uow:
            stage_external_in_transaction(
                ctx,
                uow,
                OPERATION,
                {
                    "request_key": "unavailable",
                    "destination_ref": "synthetic",
                    "title": "No reservation",
                },
                now=datetime.now(UTC),
                registry=operations,
                destinations=DestinationRegistry(),
            )
    assert excinfo.value.state == DESTINATION_GUARD_REFUSED
    assert "DestinationGuard" in excinfo.value.detail
    assert _counts(ctx, "unavailable") == (0, 0, 0, 0, 0)


def test_external_action_staging_rolls_back_every_row_on_late_failure(
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rheo_core.operations.external_actions as actions

    calls: list[str] = []
    operations, destinations = _registries(calls)
    ctx = _context(workspace, owner_account_id)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic late failure")

    monkeypatch.setattr(actions, "create_pending", fail)
    with pytest.raises(RuntimeError, match="synthetic late failure"):
        with open_unit_of_work(ctx) as uow:
            stage_external_in_transaction(
                ctx,
                uow,
                OPERATION,
                {
                    "request_key": "rollback",
                    "destination_ref": "synthetic",
                    "title": "Roll everything back",
                },
                now=datetime.now(UTC),
                registry=operations,
                destinations=destinations,
            )
    assert _counts(ctx, "rollback") == (0, 0, 0, 0, 0)

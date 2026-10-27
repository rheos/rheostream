"""Atomic approval-bound EXTERNAL staging through the transaction service."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
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
from rheo_core.operations import OperationRefused, OperationRegistry, dispatch
from rheo_core.operations.external_actions import external_action
from rheo_core.operations.idempotency import (
    IDEMPOTENCY_KEY_REUSED,
    finish_error,
    finish_success,
    purge_expired,
)
from rheo_core.operations.idempotency_tables import idempotency_result
from rheo_core.operations.records import APPROVAL_REQUIRED, SUCCEEDED
from rheo_core.operations.transaction import (
    DESTINATION_GUARD_REFUSED,
    StagedExternalAction,
    stage_external_in_transaction,
)
from rheo_core.settings import CORE_ORIGIN
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import job, operation
from sqlalchemy import func, select, update

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


def _registries(
    calls: list[str], *, available: dict[str, bool] | None = None
) -> tuple[OperationRegistry, DestinationRegistry]:
    """The synthetic operation and its destination.

    ``available["ready"]`` is read on every availability check, so a test can make
    the destination unavailable between two calls.
    """
    operations = OperationRegistry()
    declaration = _declaration()
    flags = {"ready": True} if available is None else available

    def forbidden_handler(
        ctx: WorkspaceContext, uow: UnitOfWork, model: BaseModel
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
            is_ready=lambda ctx, uow: flags["ready"],
            credentials_available=None,
            execute=forbidden_destination,
        ),
        origin="core",
    )
    return operations, destinations


def _counts(
    ctx: WorkspaceContext, key: str, *, name: str = OPERATION
) -> tuple[int, int, int, int, int]:
    """(envelopes, actions) for ``key``; (approvals, operations) for ``name``; jobs."""
    with open_unit_of_work(ctx) as uow:
        conn = uow.connection
        return (
            int(
                conn.execute(
                    select(func.count())
                    .select_from(idempotency_result)
                    .where(
                        idempotency_result.c.operation_name == name,
                        idempotency_result.c.idempotency_key == key,
                    )
                ).scalar_one()
            ),
            int(
                conn.execute(
                    select(func.count())
                    .select_from(external_action)
                    .where(
                        external_action.c.operation_name == name,
                        external_action.c.idempotency_key == key,
                    )
                ).scalar_one()
            ),
            int(
                conn.execute(
                    select(func.count())
                    .select_from(approval)
                    .where(approval.c.operation_name == name)
                ).scalar_one()
            ),
            int(
                conn.execute(
                    select(func.count())
                    .select_from(operation)
                    .where(operation.c.name == name)
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


# --- shared steps for the replay and post-retention cases ----------------------------


def _payload(key: str, *, title: str = "Synthetic work") -> dict[str, object]:
    return {"request_key": key, "destination_ref": "synthetic", "title": title}


def _stage(
    ctx: WorkspaceContext,
    payload: Mapping[str, object],
    operations: OperationRegistry,
    destinations: DestinationRegistry,
    *,
    name: str = OPERATION,
) -> StagedExternalAction:
    with open_unit_of_work(ctx) as uow:
        staged = stage_external_in_transaction(
            ctx,
            uow,
            name,
            payload,
            now=datetime.now(UTC),
            registry=operations,
            destinations=destinations,
        )
        uow.commit()
    return staged


def _refused_restage(
    ctx: WorkspaceContext,
    payload: Mapping[str, object],
    operations: OperationRegistry,
    destinations: DestinationRegistry,
    *,
    name: str = OPERATION,
    caller_commits: bool = False,
) -> OperationRefused:
    """Stage once more and return the refusal.

    ``caller_commits`` models a domain handler that records a refusal and still
    commits its own transaction: staging must have left nothing of its own behind.
    """
    if caller_commits:
        with open_unit_of_work(ctx) as uow:
            with pytest.raises(OperationRefused) as caught:
                stage_external_in_transaction(
                    ctx,
                    uow,
                    name,
                    payload,
                    now=datetime.now(UTC),
                    registry=operations,
                    destinations=destinations,
                )
            uow.commit()
        return caught.value
    with pytest.raises(OperationRefused) as excinfo:
        _stage(ctx, payload, operations, destinations, name=name)
    return excinfo.value


def _settle(
    ctx: WorkspaceContext, staged: StagedExternalAction, key: str, *, state: str
) -> None:
    """Move the staged action and its envelope to one terminal outcome."""
    now = datetime.now(UTC)
    with open_unit_of_work(ctx) as uow:
        conn = uow.connection
        if state == "succeeded":
            finish_success(
                conn,
                declaration=_declaration(),
                key=key,
                result=_Output(item_ref="synthetic:item"),
                now=now,
            )
            values: dict[str, object] = {"result_ref": "synthetic:item"}
        else:
            finish_error(
                conn,
                declaration=_declaration(),
                key=key,
                state=state,
                error_code="synthetic_cancelled",
                error_detail="synthetic cancellation",
                now=now,
            )
            values = {
                "failure_class": "cancelled_no_effect",
                "error_code": "synthetic_cancelled",
                "error_detail": "synthetic cancellation",
            }
        conn.execute(
            update(external_action)
            .where(external_action.c.id == staged.action_id)
            .values(state=state, terminal_at=now, updated_at=now, **values)
        )
        uow.commit()


def _sweep_expired(ctx: WorkspaceContext) -> int:
    """Run the retention purge at an instant past every envelope's retention."""
    with open_unit_of_work(ctx) as uow:
        removed = purge_expired(
            uow.connection, now=datetime.now(UTC) + timedelta(days=400)
        )
        uow.commit()
    return removed


# --- the post-retention boundary ----------------------------------------------------


def test_restage_after_retention_sweep_refuses_and_writes_nothing(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls: list[str] = []
    operations, destinations = _registries(calls)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    payload = _payload("retired")

    staged = _stage(ctx, payload, operations, destinations)
    _settle(ctx, staged, "retired", state="cancelled")
    assert _sweep_expired(ctx) == 1
    history = _counts(ctx, "retired")
    assert history == (0, 1, 1, 1, 0)

    # The same request after the sweep: refused, no envelope/action/approval/op/job.
    first = _refused_restage(ctx, payload, operations, destinations)
    assert first.state == IDEMPOTENCY_KEY_REUSED
    assert "new key" in first.detail
    assert _counts(ctx, "retired") == history

    # A third identical attempt refuses the same way (no MultipleResultsFound), even
    # when the caller commits its own transaction after catching the refusal.
    again = _refused_restage(
        ctx, payload, operations, destinations, caller_commits=True
    )
    assert again.state == IDEMPOTENCY_KEY_REUSED
    assert _counts(ctx, "retired") == history
    assert calls == []


# --- a settled envelope replays its retained outcome --------------------------------


def test_replay_returns_retained_success_not_approval_pending(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls: list[str] = []
    operations, destinations = _registries(calls)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)

    staged = _stage(ctx, _payload("settled"), operations, destinations)
    assert staged.retained is None
    _settle(ctx, staged, "settled", state="succeeded")
    replayed = _stage(ctx, _payload("settled"), operations, destinations)

    assert replayed.created is False
    assert (replayed.action_id, replayed.approval_id, replayed.operation_id) == (
        staged.action_id,
        staged.approval_id,
        staged.operation_id,
    )
    assert replayed.retained is not None
    assert replayed.retained.state == "succeeded"
    assert replayed.retained.result == _Output(item_ref="synthetic:item")
    assert calls == []
    assert _counts(ctx, "settled") == (1, 1, 1, 1, 0)


def test_replay_returns_retained_terminal_error(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls: list[str] = []
    operations, destinations = _registries(calls)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)

    staged = _stage(ctx, _payload("cancelled"), operations, destinations)
    _settle(ctx, staged, "cancelled", state="cancelled")
    replayed = _stage(ctx, _payload("cancelled"), operations, destinations)

    assert replayed.created is False
    assert replayed.retained is not None
    assert replayed.retained.state == "cancelled"
    assert replayed.retained.error_code == "synthetic_cancelled"
    assert replayed.retained.result is None
    assert calls == []


def test_replay_of_a_pending_envelope_stays_pending(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls: list[str] = []
    operations, destinations = _registries(calls)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)

    staged = _stage(ctx, _payload("pending"), operations, destinations)
    replayed = _stage(ctx, _payload("pending"), operations, destinations)

    assert replayed.created is False
    assert replayed.retained is not None
    assert replayed.retained.state == "pending"
    assert replayed.approval_id == staged.approval_id


# --- same-identity replay precedes the availability gate -----------------------------


def test_pending_replay_returns_original_ids_while_destination_unavailable(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls: list[str] = []
    available = {"ready": True}
    operations, destinations = _registries(calls, available=available)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)

    staged = _stage(ctx, _payload("outage"), operations, destinations)
    available["ready"] = False
    replayed = _stage(ctx, _payload("outage"), operations, destinations)

    assert replayed.created is False
    assert (replayed.action_id, replayed.approval_id, replayed.operation_id) == (
        staged.action_id,
        staged.approval_id,
        staged.operation_id,
    )
    assert _counts(ctx, "outage") == (1, 1, 1, 1, 0)
    assert calls == []


def test_new_reservation_still_requires_availability_and_leaves_nothing(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls: list[str] = []
    operations, destinations = _registries(calls, available={"ready": False})
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)

    refused = _refused_restage(
        ctx, _payload("first-outage"), operations, destinations, caller_commits=True
    )

    assert refused.state == DESTINATION_GUARD_REFUSED
    assert "not ready" in refused.detail
    assert _counts(ctx, "first-outage") == (0, 0, 0, 0, 0)


# --- each pre-reservation refusal and the changed-identity refusal ------------------

# The contract pins the destination field to ``Literal[<destination>]``, so a
# blank or different destination is refused by strict input validation, before
# reserving.
_PRE_RESERVATION_REFUSALS: dict[str, tuple[str, dict[str, object], str]] = {
    "blank destination": (
        OPERATION,
        {"request_key": "pre", "destination_ref": "  ", "title": "x"},
        "input_invalid",
    ),
    "wrong destination": (
        OPERATION,
        {"request_key": "pre", "destination_ref": "other", "title": "x"},
        "input_invalid",
    ),
    "blank key": (
        OPERATION,
        {"request_key": "   ", "destination_ref": "synthetic", "title": "x"},
        "input_invalid",
    ),
    "oversize key": (
        OPERATION,
        {"request_key": "k" * 257, "destination_ref": "synthetic", "title": "x"},
        "input_invalid",
    ),
    "invalid schema": (
        OPERATION,
        {
            "request_key": "pre",
            "destination_ref": "synthetic",
            "title": "x",
            "unexpected": True,
        },
        "input_invalid",
    ),
    "invalid required field": (
        OPERATION,
        {"request_key": "pre", "destination_ref": "synthetic", "title": ""},
        "input_invalid",
    ),
    "oversize payload": (
        OPERATION,
        {"request_key": "pre", "destination_ref": "synthetic", "title": "x" * 70000},
        "approval_payload_too_large",
    ),
}


@pytest.mark.parametrize("case", sorted(_PRE_RESERVATION_REFUSALS))
def test_each_pre_reservation_refusal_writes_no_row(
    workspace: UUID, owner_account_id: UUID, case: str
) -> None:
    name, payload, state = _PRE_RESERVATION_REFUSALS[case]
    calls: list[str] = []
    operations, destinations = _registries(calls)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    key = str(payload["request_key"])

    refused = _refused_restage(
        ctx, payload, operations, destinations, name=name, caller_commits=True
    )

    assert refused.state == state
    assert _counts(ctx, key, name=name) == (0, 0, 0, 0, 0)
    assert calls == []


def test_changed_identity_refuses_key_reused_without_a_second_action(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls: list[str] = []
    operations, destinations = _registries(calls)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)

    _stage(ctx, _payload("changed", title="First"), operations, destinations)
    refused = _refused_restage(
        ctx,
        _payload("changed", title="Second"),
        operations,
        destinations,
        caller_commits=True,
    )

    assert refused.state == IDEMPOTENCY_KEY_REUSED
    assert _counts(ctx, "changed") == (1, 1, 1, 1, 0)
    assert calls == []


# --- the public dispatch() staging path, end to end ----------------------------------


def test_public_dispatch_stages_replays_and_returns_retained_success(
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rheo_core.modules.execution as execution

    calls: list[str] = []
    operations, destinations = _registries(calls)
    monkeypatch.setattr(execution, "DESTINATIONS", destinations)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)

    held = dispatch(ctx, OPERATION, _payload("public"), registry=operations)
    repeat = dispatch(ctx, OPERATION, _payload("public"), registry=operations)

    assert held.state == APPROVAL_REQUIRED
    assert held.approval_id is not None and held.operation_id is not None
    assert (repeat.state, repeat.approval_id, repeat.operation_id) == (
        APPROVAL_REQUIRED,
        held.approval_id,
        held.operation_id,
    )
    assert _counts(ctx, "public") == (1, 1, 1, 1, 0)

    with open_unit_of_work(ctx) as uow:
        action_id = uow.connection.execute(
            select(external_action.c.id).where(
                external_action.c.approval_id == held.approval_id
            )
        ).scalar_one()
    staged = StagedExternalAction(
        action_id, held.approval_id, held.operation_id, "pending", True
    )
    _settle(ctx, staged, "public", state="succeeded")
    settled = dispatch(ctx, OPERATION, _payload("public"), registry=operations)

    assert settled.state == SUCCEEDED
    assert settled.result == _Output(item_ref="synthetic:item")
    assert settled.operation_id == held.operation_id
    assert calls == []
    assert _counts(ctx, "public") == (1, 1, 1, 1, 0)

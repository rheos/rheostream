"""Public KEYED dispatch replay/refusal, compatibility, and retention seams."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    KeyedSpec,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.audit import CORE_AUDIT_SINK, install_sink
from rheo_core.boundary import context_for_harness
from rheo_core.operations import OperationRefused, OperationRegistry, dispatch
from rheo_core.operations.idempotency import (
    IDEMPOTENCY_KEY_REUSED,
    IDEMPOTENCY_SCHEMA_INCOMPATIBLE,
    KEYED_COMPATIBILITY,
    KeyedCompatibilityRegistry,
    purge_expired,
)
from rheo_core.operations.idempotency_tables import external_action, idempotency_result
from rheo_core.refs import uuid7
from rheo_core.settings import CORE_ORIGIN
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from sqlalchemy import func, insert, select

pytestmark = pytest.mark.postgres


class KeyedInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    request_key: str
    title: str
    note: str | None = None


class KeyedOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_ref: str
    title: str


def _context(workspace: UUID, owner_account_id: UUID) -> WorkspaceContext:
    found = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(found, WorkspaceContext)
    return found


def _declaration(
    *, request_version: int = 1, result_version: int = 1
) -> OperationDeclaration:
    return OperationDeclaration(
        name="core.keyed.create",
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER}),
        input_model=KeyedInput,
        output=KeyedOutput,
        idempotency=Idempotency.KEYED,
        keyed=KeyedSpec(
            key_field="request_key",
            request_schema_version=request_version,
            result_schema_version=result_version,
        ),
        audit=AuditSpec(subject_field=None),
    )


def test_keyed_dispatch_replays_defaults_nulls_unicode_and_exact_whitespace(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls: list[str] = []

    def handler(
        ctx: WorkspaceContext, uow: UnitOfWork, model: KeyedInput
    ) -> KeyedOutput:
        calls.append(model.title)
        return KeyedOutput(item_ref="current.item:synthetic", title=model.title)

    registry = OperationRegistry()
    registry.register(_declaration(), handler, origin=CORE_ORIGIN)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    first = dispatch(
        ctx,
        "core.keyed.create",
        {"request_key": "κ ey", "title": "  Café  "},
        registry=registry,
    )
    replayed = dispatch(
        ctx,
        "core.keyed.create",
        {"title": "  Café  ", "note": None, "request_key": "κ ey"},
        registry=registry,
    )

    assert first.ok and replayed.ok
    assert first.result == replayed.result
    assert calls == ["  Café  "]


def test_keyed_dispatch_refuses_changed_protected_request_without_handler_invocation(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls: list[str] = []

    def handler(
        ctx: WorkspaceContext, uow: UnitOfWork, model: KeyedInput
    ) -> KeyedOutput:
        calls.append(model.title)
        return KeyedOutput(item_ref="current.item:one", title=model.title)

    registry = OperationRegistry()
    registry.register(_declaration(), handler, origin=CORE_ORIGIN)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    assert dispatch(
        ctx,
        "core.keyed.create",
        {"request_key": "same", "title": "First"},
        registry=registry,
    ).ok
    changed = dispatch(
        ctx,
        "core.keyed.create",
        {"request_key": "same", "title": "Second"},
        registry=registry,
    )

    assert changed.state == "refused"
    assert changed.error is not None
    assert changed.error.error_code == IDEMPOTENCY_KEY_REUSED
    assert calls == ["First"]


@pytest.mark.parametrize("request_key", ["", "   ", "κ" * 129])
def test_keyed_dispatch_refuses_blank_or_oversize_utf8_key_before_handler(
    workspace: UUID, owner_account_id: UUID, request_key: str
) -> None:
    calls = 0

    def handler(ctx: Any, uow: Any, model: KeyedInput) -> KeyedOutput:
        nonlocal calls
        calls += 1
        return KeyedOutput(item_ref="current.item:never", title=model.title)

    registry = OperationRegistry()
    registry.register(_declaration(), handler, origin=CORE_ORIGIN)
    install_sink("core", CORE_AUDIT_SINK)
    outcome = dispatch(
        _context(workspace, owner_account_id),
        "core.keyed.create",
        {"request_key": request_key, "title": "No effect"},
        registry=registry,
    )

    assert outcome.state == "input_invalid"
    assert calls == 0


def test_keyed_dispatch_replays_terminal_refusal_and_rolls_back_effect(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls: list[str] = []

    def handler(
        ctx: WorkspaceContext, uow: UnitOfWork, model: KeyedInput
    ) -> KeyedOutput:
        calls.append(model.title)
        raise OperationRefused("synthetic_refusal", "synthetic request was refused")

    registry = OperationRegistry()
    registry.register(_declaration(), handler, origin=CORE_ORIGIN)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    payload = {"request_key": "refused", "title": "No effect"}
    first = dispatch(ctx, "core.keyed.create", payload, registry=registry)
    replayed = dispatch(ctx, "core.keyed.create", payload, registry=registry)

    assert first.state == replayed.state == "synthetic_refusal"
    assert first.error == replayed.error
    assert calls == ["No effect"]


class V1Input(BaseModel):
    request_key: str
    title: str


class V2Input(BaseModel):
    request_key: str
    headline: str


class V1Output(BaseModel):
    ref: str


class V2Output(BaseModel):
    item_ref: str


def _versioned_declaration(
    input_model: type[BaseModel],
    output: type[BaseModel],
    version: int,
    *,
    name: str = "core.keyed.versioned",
) -> OperationDeclaration:
    return OperationDeclaration(
        name=name,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER}),
        input_model=input_model,
        output=output,
        idempotency=Idempotency.KEYED,
        keyed=KeyedSpec(
            key_field="request_key",
            request_schema_version=version,
            result_schema_version=version,
        ),
        audit=AuditSpec(subject_field=None),
    )


def test_keyed_retained_schema_adapters_replay_and_missing_path_refuses(
    workspace: UUID, owner_account_id: UUID
) -> None:
    v1_calls = 0
    v2_calls = 0

    def v1_handler(ctx: Any, uow: Any, model: V1Input) -> V1Output:
        nonlocal v1_calls
        v1_calls += 1
        return V1Output(ref="current.item:adapted")

    def v2_handler(ctx: Any, uow: Any, model: V2Input) -> V2Output:
        nonlocal v2_calls
        v2_calls += 1
        return V2Output(item_ref="should-not-run")

    v1_registry = OperationRegistry()
    v1_registry.register(
        _versioned_declaration(V1Input, V1Output, 1), v1_handler, origin=CORE_ORIGIN
    )
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    assert dispatch(
        ctx,
        "core.keyed.versioned",
        {"request_key": "adapt", "title": "Example"},
        registry=v1_registry,
    ).ok

    v2_registry = OperationRegistry()
    v2_registry.register(
        _versioned_declaration(V2Input, V2Output, 2), v2_handler, origin=CORE_ORIGIN
    )
    incompatible = dispatch(
        ctx,
        "core.keyed.versioned",
        {"request_key": "adapt", "headline": "Example"},
        registry=v2_registry,
    )
    assert incompatible.state == IDEMPOTENCY_SCHEMA_INCOMPATIBLE
    assert v2_calls == 0

    KEYED_COMPATIBILITY.register_request(
        "core.keyed.versioned",
        1,
        2,
        lambda value: {
            **value,
            "input": {
                "request_key": value["input"]["request_key"],
                "headline": value["input"]["title"],
            },
        },
    )
    KEYED_COMPATIBILITY.register_result(
        "core.keyed.versioned",
        1,
        2,
        lambda value: {"item_ref": value["ref"]},
    )
    replayed = dispatch(
        ctx,
        "core.keyed.versioned",
        {"request_key": "adapt", "headline": "Example"},
        registry=v2_registry,
    )
    assert replayed.ok
    assert replayed.result == V2Output(item_ref="current.item:adapted")
    assert (v1_calls, v2_calls) == (1, 0)


def test_keyed_failed_adapter_refuses_without_effect_and_graph_is_unique_acyclic(
    workspace: UUID, owner_account_id: UUID
) -> None:
    name = "core.keyed.adapter_failure"
    v2_calls = 0
    v1_registry = OperationRegistry()
    v1_registry.register(
        _versioned_declaration(V1Input, V1Output, 1, name=name),
        lambda ctx, uow, model: V1Output(ref="current.item:never-adapted"),
        origin=CORE_ORIGIN,
    )
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    assert dispatch(
        ctx,
        name,
        {"request_key": "adapter-fails", "title": "Example"},
        registry=v1_registry,
    ).ok

    def fail_adapter(value: Any) -> Any:
        raise ValueError("synthetic adapter failure")

    KEYED_COMPATIBILITY.register_request(name, 1, 2, fail_adapter)
    v2_registry = OperationRegistry()

    def v2_handler(ctx: Any, uow: Any, model: V2Input) -> V2Output:
        nonlocal v2_calls
        v2_calls += 1
        return V2Output(item_ref="current.item:must-not-run")

    v2_registry.register(
        _versioned_declaration(V2Input, V2Output, 2, name=name),
        v2_handler,
        origin=CORE_ORIGIN,
    )
    outcome = dispatch(
        ctx,
        name,
        {"request_key": "adapter-fails", "headline": "Example"},
        registry=v2_registry,
    )
    assert outcome.state == IDEMPOTENCY_SCHEMA_INCOMPATIBLE
    assert v2_calls == 0

    adapters = KeyedCompatibilityRegistry()

    def passthrough(value: Any) -> Any:
        return value

    adapters.register_request("core.keyed.graph", 1, 2, passthrough)
    adapters.register_request("core.keyed.graph", 2, 3, passthrough)
    with pytest.raises(ValueError, match="more than one path"):
        adapters.register_request("core.keyed.graph", 1, 4, passthrough)
    with pytest.raises(ValueError, match="cycle"):
        adapters.register_request("core.keyed.graph", 3, 1, passthrough)


def test_idempotency_retention_is_fixed_at_creation_and_sweeps_expired_rows(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls = 0

    def handler(ctx: Any, uow: Any, model: KeyedInput) -> KeyedOutput:
        nonlocal calls
        calls += 1
        return KeyedOutput(item_ref="current.item:retained", title=model.title)

    registry = OperationRegistry()
    registry.register(_declaration(), handler, origin=CORE_ORIGIN)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    payload = {"request_key": "retention", "title": "Retained"}
    assert dispatch(ctx, "core.keyed.create", payload, registry=registry).ok

    with open_unit_of_work(ctx) as uow:
        original = uow.connection.execute(
            select(idempotency_result.c.retained_until).where(
                idempotency_result.c.operation_name == "core.keyed.create",
                idempotency_result.c.idempotency_key == "retention",
            )
        ).scalar_one()
        assert purge_expired(uow.connection, now=original + timedelta(seconds=1)) == 1
        remaining = uow.connection.execute(
            select(func.count()).select_from(idempotency_result)
        ).scalar_one()
        uow.commit()
    assert original > datetime.now(UTC) + timedelta(days=89)
    assert remaining == 0

    # Post-sweep is a documented boundary, not a resurrection of the old envelope.
    assert dispatch(ctx, "core.keyed.create", payload, registry=registry).ok
    assert calls == 2


def test_idempotency_retention_preserves_nonterminal_external_action(
    workspace: UUID, owner_account_id: UUID
) -> None:
    registry = OperationRegistry()
    registry.register(
        _declaration(),
        lambda ctx, uow, model: KeyedOutput(
            item_ref="current.item:protected", title=model.title
        ),
        origin=CORE_ORIGIN,
    )
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    assert dispatch(
        ctx,
        "core.keyed.create",
        {"request_key": "protected", "title": "Protected"},
        registry=registry,
    ).ok

    now = datetime.now(UTC)
    with open_unit_of_work(ctx) as uow:
        retained_until = uow.connection.execute(
            select(idempotency_result.c.retained_until).where(
                idempotency_result.c.operation_name == "core.keyed.create",
                idempotency_result.c.idempotency_key == "protected",
            )
        ).scalar_one()
        uow.connection.execute(
            insert(external_action).values(
                id=uuid7(),
                operation_id=uuid7(),
                approval_id=uuid7(),
                operation_name="core.keyed.create",
                subject_ref=None,
                source_ref="synthetic.source:one",
                destination_ref="synthetic",
                approval_payload_ref=uuid7(),
                approval_payload_digest=b"x" * 32,
                idempotency_key="protected",
                request_schema_version=1,
                result_schema_version=1,
                provider_request_id=None,
                execution_job_id=None,
                state="pending",
                failure_class=None,
                attempts=0,
                result_ref=None,
                result_detail=None,
                error_code=None,
                error_detail=None,
                created_at=now,
                updated_at=now,
                terminal_at=None,
            )
        )
        assert (
            purge_expired(uow.connection, now=retained_until + timedelta(seconds=1))
            == 0
        )
        remaining = uow.connection.execute(
            select(func.count()).select_from(idempotency_result)
        ).scalar_one()
        uow.commit()
    assert remaining == 1

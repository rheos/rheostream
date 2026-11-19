"""Public KEYED dispatch replay/refusal, compatibility, and retention seams."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock
from time import monotonic, sleep
from typing import Any
from uuid import UUID

import pytest
import rheo_core.operations.idempotency as idempotency_module
from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    KeyedSpec,
    OperationDeclaration,
    Role,
    SafetyClass,
    StaleRecord,
    WorkspaceContext,
)
from rheo_core.audit import CORE_AUDIT_SINK, install_sink
from rheo_core.boundary import context_for_harness
from rheo_core.modules import readiness
from rheo_core.operations import (
    HANDLER_FAILED,
    OUTPUT_INVALID,
    OperationRefused,
    OperationRegistry,
    dispatch,
)
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
from rheo_core.storage.backend import StorageRefusal, UnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import audit_record, consumer_processed
from sqlalchemy import func, insert, select, text

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


class NonFiniteOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_ref: str
    score: float


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


def _audit_outcomes(ctx: WorkspaceContext, operation_name: str) -> list[str]:
    with open_unit_of_work(ctx) as uow:
        return list(
            uow.connection.execute(
                select(audit_record.c.outcome)
                .where(audit_record.c.operation_name == operation_name)
                .order_by(audit_record.c.occurred_at, audit_record.c.id)
            ).scalars()
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


@pytest.mark.parametrize("_race_round", range(5))
def test_keyed_dispatch_concurrent_same_request_runs_one_handler_and_effect(
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
    _race_round: int,
) -> None:
    start = Barrier(2)
    state_lock = Lock()
    reservation_pids: set[int] = set()
    handler_calls = 0
    contention_observed = False
    effect_event_id = uuid7()
    effect_consumer_id = f"synthetic:keyed-race-{_race_round}"
    real_reserve = idempotency_module.reserve

    def synchronized_reserve(conn: Any, **kwargs: Any) -> Any:
        pid = int(conn.execute(text("SELECT pg_backend_pid()")).scalar_one())
        with state_lock:
            reservation_pids.add(pid)
        start.wait(timeout=10)
        return real_reserve(conn, **kwargs)

    def handler(ctx: Any, uow: Any, model: KeyedInput) -> KeyedOutput:
        nonlocal contention_observed, handler_calls
        current_pid = int(
            uow.connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        )
        with state_lock:
            handler_calls += 1
            other_pids = reservation_pids - {current_pid}
        assert len(other_pids) == 1
        other_pid = other_pids.pop()

        # Hold the winning transaction open until PostgreSQL reports the losing
        # reservation waiting on its uncommitted unique key. This proves genuine
        # row/index contention rather than two serial calls released by a barrier.
        deadline = monotonic() + 10
        while monotonic() < deadline:
            wait_event_type = uow.connection.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": other_pid},
            ).scalar_one_or_none()
            if wait_event_type == "Lock":
                contention_observed = True
                break
            sleep(0.01)
        assert contention_observed

        uow.connection.execute(
            insert(consumer_processed).values(
                consumer_id=effect_consumer_id,
                event_id=effect_event_id,
                processed_at=datetime.now(UTC),
            )
        )
        return KeyedOutput(item_ref="current.item:concurrent", title=model.title)

    monkeypatch.setattr(idempotency_module, "reserve", synchronized_reserve)
    registry = OperationRegistry()
    registry.register(_declaration(), handler, origin=CORE_ORIGIN)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    payload = {
        "request_key": f"concurrent-{_race_round}",
        "title": "Concurrent request",
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                dispatch,
                ctx,
                "core.keyed.create",
                payload,
                registry=registry,
            )
            for _ in range(2)
        ]
        outcomes = [future.result(timeout=20) for future in futures]

    with open_unit_of_work(ctx) as uow:
        effect_count = uow.connection.execute(
            select(func.count())
            .select_from(consumer_processed)
            .where(
                consumer_processed.c.consumer_id == effect_consumer_id,
                consumer_processed.c.event_id == effect_event_id,
            )
        ).scalar_one()

    assert len(reservation_pids) == 2
    assert contention_observed
    assert handler_calls == 1
    assert effect_count == 1
    assert all(outcome.ok for outcome in outcomes)
    assert (
        outcomes[0].result
        == outcomes[1].result
        == KeyedOutput(item_ref="current.item:concurrent", title="Concurrent request")
    )
    assert _audit_outcomes(ctx, "core.keyed.create") == ["succeeded", "succeeded"]


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
    assert _audit_outcomes(ctx, "core.keyed.create") == ["succeeded", "refused"]


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
    assert _audit_outcomes(ctx, "core.keyed.create") == ["refused", "refused"]


@pytest.mark.parametrize(
    ("transient", "expected_state"),
    [("stale", "record_stale"), ("storage", "workspace_unavailable")],
)
def test_keyed_dispatch_leaves_key_unbound_after_transient_handler_refusal(
    workspace: UUID,
    owner_account_id: UUID,
    transient: str,
    expected_state: str,
) -> None:
    attempts = 0
    successes = 0

    def handler(ctx: Any, uow: Any, model: KeyedInput) -> KeyedOutput:
        nonlocal attempts, successes
        attempts += 1
        if attempts == 1:
            if transient == "stale":
                raise StaleRecord("synthetic record moved")
            raise StorageRefusal(
                "workspace_unavailable", "synthetic transient storage refusal"
            )
        successes += 1
        return KeyedOutput(item_ref="current.item:retried", title=model.title)

    registry = OperationRegistry()
    registry.register(_declaration(), handler, origin=CORE_ORIGIN)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    payload = {"request_key": f"retry-{transient}", "title": "Retry safely"}

    first = dispatch(ctx, "core.keyed.create", payload, registry=registry)
    retried = dispatch(ctx, "core.keyed.create", payload, registry=registry)
    replayed = dispatch(ctx, "core.keyed.create", payload, registry=registry)

    assert first.state == expected_state
    assert retried.ok and replayed.ok
    assert retried.result == replayed.result
    assert attempts == 2
    assert successes == 1
    assert _audit_outcomes(ctx, "core.keyed.create") == [
        "refused",
        "succeeded",
        "succeeded",
    ]


def test_keyed_dispatch_leaves_key_unbound_after_transient_module_readiness(
    workspace: UUID, owner_account_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    readiness_checks = 0
    handler_calls = 0
    real_require_ready = readiness.require_ready

    def require_ready_once(conn: Any, module_id: str) -> None:
        nonlocal readiness_checks
        readiness_checks += 1
        if readiness_checks == 1:
            raise readiness.ModuleNotReady(
                "module_unavailable", "synthetic module upgrade in progress"
            )
        real_require_ready(conn, module_id)

    def handler(ctx: Any, uow: Any, model: KeyedInput) -> KeyedOutput:
        nonlocal handler_calls
        handler_calls += 1
        return KeyedOutput(item_ref="current.item:ready", title=model.title)

    monkeypatch.setattr(readiness, "require_ready", require_ready_once)
    registry = OperationRegistry()
    registry.register(_declaration(), handler, origin=CORE_ORIGIN)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    payload = {"request_key": "retry-readiness", "title": "Retry readiness"}

    first = dispatch(ctx, "core.keyed.create", payload, registry=registry)
    retried = dispatch(ctx, "core.keyed.create", payload, registry=registry)
    replayed = dispatch(ctx, "core.keyed.create", payload, registry=registry)

    assert first.state == "module_unavailable"
    assert retried.ok and replayed.ok
    assert retried.result == replayed.result
    assert readiness_checks == 2
    assert handler_calls == 1
    assert _audit_outcomes(ctx, "core.keyed.create") == [
        "refused",
        "succeeded",
        "succeeded",
    ]


def test_keyed_output_invalid_is_failed_and_replayed_without_reinvocation(
    workspace: UUID, owner_account_id: UUID
) -> None:
    calls = 0

    def handler(ctx: Any, uow: Any, model: KeyedInput) -> V1Output:
        nonlocal calls
        calls += 1
        return V1Output(ref="synthetic:wrong-output")

    registry = OperationRegistry()
    registry.register(_declaration(), handler, origin=CORE_ORIGIN)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    payload = {"request_key": "wrong-output", "title": "Wrong output"}

    first = dispatch(ctx, "core.keyed.create", payload, registry=registry)
    replayed = dispatch(ctx, "core.keyed.create", payload, registry=registry)

    assert first.state == replayed.state == "failed"
    assert first.error is not None and replayed.error is not None
    assert first.error.error_code == replayed.error.error_code == OUTPUT_INVALID
    assert calls == 1
    assert _audit_outcomes(ctx, "core.keyed.create") == ["failed", "failed"]


def test_keyed_result_canonicalization_failure_rolls_back_handler_effect(
    workspace: UUID, owner_account_id: UUID
) -> None:
    operation_name = "core.keyed.nonfinite"
    consumer_id = "synthetic:keyed-nonfinite"
    event_id = uuid7()
    calls = 0

    declaration = OperationDeclaration(
        name=operation_name,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER}),
        input_model=KeyedInput,
        output=NonFiniteOutput,
        idempotency=Idempotency.KEYED,
        keyed=KeyedSpec(
            key_field="request_key",
            request_schema_version=1,
            result_schema_version=1,
        ),
        audit=AuditSpec(subject_field=None),
    )

    def handler(ctx: Any, uow: Any, model: KeyedInput) -> NonFiniteOutput:
        nonlocal calls
        calls += 1
        uow.connection.execute(
            insert(consumer_processed).values(
                consumer_id=consumer_id,
                event_id=event_id,
                processed_at=datetime.now(UTC),
            )
        )
        return NonFiniteOutput(
            item_ref="current.item:must-roll-back", score=float("nan")
        )

    registry = OperationRegistry()
    registry.register(declaration, handler, origin=CORE_ORIGIN)
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    payload = {"request_key": "nonfinite", "title": "Nonfinite result"}

    first = dispatch(ctx, operation_name, payload, registry=registry)
    replayed = dispatch(ctx, operation_name, payload, registry=registry)

    with open_unit_of_work(ctx) as uow:
        effect_count = uow.connection.execute(
            select(func.count())
            .select_from(consumer_processed)
            .where(
                consumer_processed.c.consumer_id == consumer_id,
                consumer_processed.c.event_id == event_id,
            )
        ).scalar_one()
        retained = uow.connection.execute(
            select(idempotency_result.c.state, idempotency_result.c.error_code).where(
                idempotency_result.c.operation_name == operation_name,
                idempotency_result.c.idempotency_key == "nonfinite",
            )
        ).one()

    assert first.state == replayed.state == "failed"
    assert first.error is not None and replayed.error is not None
    assert first.error.error_code == replayed.error.error_code == HANDLER_FAILED
    assert calls == 1
    assert effect_count == 0
    assert retained == ("failed", HANDLER_FAILED)
    assert _audit_outcomes(ctx, operation_name) == ["failed", "failed"]


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


def _insert_external_action(
    uow: UnitOfWork, *, key: str, state: str, now: datetime
) -> UUID:
    action_id = uuid7()
    terminal = state not in {"pending", "executing"}
    unresolved = state == "unresolved"
    uow.connection.execute(
        insert(external_action).values(
            id=action_id,
            operation_id=uuid7(),
            approval_id=uuid7(),
            operation_name="core.keyed.create",
            subject_ref=None,
            source_ref="synthetic.source:one",
            destination_ref="synthetic",
            approval_payload_ref=uuid7(),
            approval_payload_digest=b"x" * 32,
            idempotency_key=key,
            request_schema_version=1,
            result_schema_version=1,
            provider_request_id=None,
            execution_job_id=None,
            state=state,
            failure_class="outcome_unknown" if unresolved else None,
            attempts=0,
            result_ref="current.item:terminal" if state == "succeeded" else None,
            result_detail=None,
            error_code="outcome_unknown" if unresolved else None,
            error_detail="synthetic unknown outcome" if unresolved else None,
            created_at=now,
            updated_at=now,
            terminal_at=now if terminal else None,
        )
    )
    return action_id


@pytest.mark.parametrize("action_state", ["pending", "executing", "unresolved"])
def test_idempotency_retention_preserves_nonterminal_or_unresolved_external_action(
    workspace: UUID, owner_account_id: UUID, action_state: str
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
        _insert_external_action(uow, key="protected", state=action_state, now=now)
        assert (
            purge_expired(uow.connection, now=retained_until + timedelta(seconds=1))
            == 0
        )
        remaining = uow.connection.execute(
            select(func.count()).select_from(idempotency_result)
        ).scalar_one()
        uow.commit()
    assert remaining == 1


def test_idempotency_retention_sweeps_terminal_envelope_but_preserves_action_history(
    workspace: UUID, owner_account_id: UUID
) -> None:
    registry = OperationRegistry()
    registry.register(
        _declaration(),
        lambda ctx, uow, model: KeyedOutput(
            item_ref="current.item:terminal", title=model.title
        ),
        origin=CORE_ORIGIN,
    )
    install_sink("core", CORE_AUDIT_SINK)
    ctx = _context(workspace, owner_account_id)
    assert dispatch(
        ctx,
        "core.keyed.create",
        {"request_key": "terminal-history", "title": "Terminal"},
        registry=registry,
    ).ok

    now = datetime.now(UTC)
    with open_unit_of_work(ctx) as uow:
        retained_until = uow.connection.execute(
            select(idempotency_result.c.retained_until).where(
                idempotency_result.c.operation_name == "core.keyed.create",
                idempotency_result.c.idempotency_key == "terminal-history",
            )
        ).scalar_one()
        action_id = _insert_external_action(
            uow, key="terminal-history", state="succeeded", now=now
        )

        assert (
            purge_expired(uow.connection, now=retained_until + timedelta(seconds=1))
            == 1
        )
        envelope_count = uow.connection.execute(
            select(func.count()).select_from(idempotency_result)
        ).scalar_one()
        history = uow.connection.execute(
            select(
                external_action.c.operation_name,
                external_action.c.idempotency_key,
                external_action.c.state,
                external_action.c.result_ref,
            ).where(external_action.c.id == action_id)
        ).one()
        uow.commit()

    assert envelope_count == 0
    assert history == (
        "core.keyed.create",
        "terminal-history",
        "succeeded",
        "current.item:terminal",
    )

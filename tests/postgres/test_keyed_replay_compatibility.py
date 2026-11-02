"""Retained KEYED schema compatibility across active workspace databases."""

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import pytest
from pydantic import BaseModel
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    KeyedSpec,
    OperationDeclaration,
    Role,
    SafetyClass,
)
from rheo_core.operations.idempotency import (
    KeyedCompatibilityRegistry,
    KeyedReplayCompatibilityError,
    check_keyed_replay_compatibility,
)
from rheo_core.operations.idempotency_tables import idempotency_result
from rheo_core.operations.registry import OperationRegistry
from rheo_core.settings import CORE_ORIGIN
from rheo_core.storage.postgres import PostgresBackend
from sqlalchemy import insert

pytestmark = pytest.mark.postgres


class _Input(BaseModel):
    request_key: str


class _Output(BaseModel):
    item_ref: str


class _ClusterSession(Protocol):
    backend: PostgresBackend


def _handler(ctx: object, uow: object, model: object) -> _Output:
    raise AssertionError("compatibility checks never dispatch")


def _registry(*, keyed: bool = True) -> OperationRegistry:
    registry = OperationRegistry()
    registry.register(
        OperationDeclaration(
            name="core.replay.create",
            safety_class=SafetyClass.MUTATE,
            roles=frozenset({Role.OWNER}),
            input_model=_Input,
            output=_Output,
            idempotency=Idempotency.KEYED if keyed else Idempotency.NONE,
            keyed=(
                KeyedSpec(
                    key_field="request_key",
                    request_schema_version=2,
                    result_schema_version=2,
                )
                if keyed
                else None
            ),
            audit=AuditSpec(subject_field=None),
        ),
        _handler,
        origin=CORE_ORIGIN,
    )
    return registry


def _insert_retained_v1(
    backend: PostgresBackend, workspace: UUID, *, retained_until: datetime
) -> None:
    database_name = f"ws_{workspace.hex}"
    written_at = min(datetime.now(UTC), retained_until - timedelta(days=1))
    with backend.pools.acquire(database_name) as engine:
        with engine.begin() as connection:
            connection.execute(
                insert(idempotency_result).values(
                    operation_name="core.replay.create",
                    idempotency_key="retained-one",
                    request_identity={
                        "operation_name": "core.replay.create",
                        "input": {"request_key": "retained-one"},
                    },
                    request_digest=b"synthetic-digest",
                    request_schema_version=1,
                    state="succeeded",
                    operation_id=None,
                    result={"item_ref": "current.item:synthetic"},
                    result_schema_version=1,
                    error_code=None,
                    error_detail=None,
                    created_at=written_at,
                    updated_at=written_at,
                    retained_until=retained_until,
                )
            )


def test_keyed_replay_compatibility_rejects_and_then_accepts_retained_versions(
    cluster: _ClusterSession, workspace: UUID
) -> None:
    backend = cluster.backend
    now = datetime.now(UTC)
    _insert_retained_v1(backend, workspace, retained_until=now + timedelta(days=1))
    registry = _registry()
    adapters = KeyedCompatibilityRegistry()

    with pytest.raises(KeyedReplayCompatibilityError, match="request schema 1"):
        check_keyed_replay_compatibility(backend, registry, adapters=adapters, now=now)

    adapters.register_request("core.replay.create", 1, 2, lambda value: dict(value))
    adapters.register_result("core.replay.create", 1, 2, lambda value: dict(value))
    check_keyed_replay_compatibility(backend, registry, adapters=adapters, now=now)


def test_keyed_replay_compatibility_ignores_expired_rows(
    cluster: _ClusterSession, workspace: UUID
) -> None:
    backend = cluster.backend
    now = datetime.now(UTC)
    _insert_retained_v1(backend, workspace, retained_until=now - timedelta(seconds=1))

    check_keyed_replay_compatibility(
        backend,
        _registry(),
        adapters=KeyedCompatibilityRegistry(),
        now=now,
    )


def test_keyed_replay_compatibility_rejects_registered_operation_no_longer_keyed(
    cluster: _ClusterSession, workspace: UUID
) -> None:
    backend = cluster.backend
    now = datetime.now(UTC)
    _insert_retained_v1(backend, workspace, retained_until=now + timedelta(days=1))

    with pytest.raises(KeyedReplayCompatibilityError, match="no longer KEYED"):
        check_keyed_replay_compatibility(
            backend,
            _registry(keyed=False),
            adapters=KeyedCompatibilityRegistry(),
            now=now,
        )


def test_keyed_replay_compatibility_ignores_unselected_operation_rows(
    cluster: _ClusterSession, workspace: UUID
) -> None:
    backend = cluster.backend
    now = datetime.now(UTC)
    _insert_retained_v1(backend, workspace, retained_until=now + timedelta(days=1))

    check_keyed_replay_compatibility(
        backend,
        OperationRegistry(),
        adapters=KeyedCompatibilityRegistry(),
        now=now,
    )

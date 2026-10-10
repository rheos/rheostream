"""Authenticated webhook delivery, independent of HTTP and domain modules."""

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from uuid import UUID

from rheo_contracts import RecordRef
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import context_for_connection
from rheo_core.connectors.contracts import ConnectionState
from rheo_core.connectors.credentials import resolve_key
from rheo_core.connectors.tables import locator
from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
from rheo_core.events import ConsumerRegistry
from rheo_core.modules.manifest import ConnectorBinding
from rheo_core.operations import dispatch
from rheo_core.operations.registry import REGISTRY, OperationRegistry
from rheo_core.secrets import SecretRefusal
from rheo_core.storage.backend import StorageRefusal, UnitOfWork
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.repositories import list_module_states
from rheo_core.storage.routing import active_workspace

MAX_BODY_BYTES = 262144


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    raise ValueError("non-finite JSON value")


def matched_generation(
    workspace_id: UUID,
    connection_id: UUID,
    state: ConnectionState,
    body: bytes,
    timestamp: str,
    signature: str,
) -> int | None:
    now = datetime.now(UTC)
    if not state.active or not re.fullmatch(r"[0-9]{1,12}", timestamp):
        return None
    if abs(now.timestamp() - int(timestamp)) > state.replay_seconds:
        return None
    if not re.fullmatch(r"v1=[0-9a-f]{64}", signature):
        return None
    message = timestamp.encode("ascii") + b"." + body
    candidates = [(state.current_ref, state.generation)]
    if state.previous_until is not None and state.previous_until > now:
        candidates.append((state.previous_ref, state.generation - 1))
    matched = None
    for reference, generation in candidates:
        if reference is None:
            continue
        try:
            key = resolve_key(workspace_id, connection_id, reference)
        except SecretRefusal:
            continue
        expected = "v1=" + hmac.new(key.expose(), message, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, signature):
            matched = generation
    return matched


def receive(
    module_id: str,
    binding: ConnectorBinding,
    connection_id: UUID,
    body: bytes,
    timestamp: str,
    signature: str,
    event_id_header: str | None,
    *,
    consumers: ConsumerRegistry,
    registry: OperationRegistry = REGISTRY,
) -> tuple[int, dict[str, object]]:
    """Acknowledge only after the existing dispatcher has committed acceptance."""
    refused: tuple[int, dict[str, object]] = (401, {"state": "authentication_refused"})
    if len(body) > MAX_BODY_BYTES:
        return 413, {"state": "payload_too_large"}
    try:
        with get_backend().control_engine.connect() as conn:
            located = (
                conn.execute(
                    select(locator).where(
                        locator.c.connection_id == connection_id,
                        locator.c.module_id == module_id,
                        locator.c.transport == binding.transport,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if located is None or binding.read_connection is None:
            return refused
        workspace_id = located["workspace_id"]
        workspace = active_workspace(workspace_id)
        pools = get_backend().pools
        engine = pools.engine_for(workspace.database_name, pin=True)
        with UnitOfWork(engine, workspace.database_name, pool=pools) as uow:
            lock_workspace_lifecycle(uow.connection)
            if not any(
                s.module_id == module_id and s.state == "enabled"
                for s in list_module_states(uow.connection)
            ):
                return refused
            state = binding.read_connection(workspace_id, uow, connection_id)
            if state is None:
                return refused
            generation = matched_generation(
                workspace_id, connection_id, state, body, timestamp, signature
            )
            if generation is None:
                if binding.record_refusal is not None:
                    binding.record_refusal(workspace_id, uow, connection_id)
                uow.commit()
                return refused
            if len(body) > state.max_bytes:
                return 413, {"state": "payload_too_large"}
        try:
            text = body.decode("utf-8")
            payload = json.loads(
                text, object_pairs_hook=_json_object, parse_constant=_invalid_constant
            )
        except (ValueError, UnicodeError, RecursionError):
            return 422, {"state": "input_invalid"}
        if not isinstance(payload, dict):
            return 422, {"state": "input_invalid"}
        # Identity must be covered by the signature. The optional header is only
        # an assertion of the signed body identity, never an unsigned override.
        event_id = payload.get("event_id", hashlib.sha256(body).hexdigest())
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 512:
            return 422, {"state": "input_invalid"}
        if event_id_header is not None and event_id_header != event_id:
            return 422, {"state": "input_invalid"}
        ctx = context_for_connection(
            workspace_id, connection_id, module_id, binding.transport
        )
        if isinstance(ctx, Refusal):
            return refused
        outcome = dispatch(
            ctx,
            binding.service_operation,
            {
                "connection_ref": RecordRef(
                    module=module_id, record_type="intake_connection", id=connection_id
                ).format(),
                "source_event_id": event_id,
                "body": text,
                "signing_key_generation": generation,
            },
            registry=registry,
            consumers=consumers,
        )
        if not outcome.ok or outcome.result is None:
            if outcome.state in {
                "connection_revoked",
                "role_not_permitted",
                "module_unavailable",
                "not_found",
            }:
                return refused
            return 422, {"state": outcome.state}
        result = outcome.result.model_dump(mode="json")
        return (409 if result.get("outcome") == "conflict" else 202), result
    except StorageRefusal:
        return refused
    except (SQLAlchemyError, OSError):
        return 503, {"state": "intake_unavailable"}

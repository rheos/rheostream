"""Canonical KEYED request identity, retained outcomes, adapters, and retention.

The public dispatcher is the only first-reservation caller. Compatibility adapters
are pure value transforms: they receive detached JSON-compatible mappings and return
new JSON-compatible mappings; no context, storage, clock, or handler is available.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ValidationError
from rheo_contracts import KeyedSpec, OperationDeclaration
from sqlalchemy import Connection, RowMapping, delete, literal, null, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rheo_core.operations.idempotency_tables import (
    external_action,
    idempotency_result,
)

IDEMPOTENCY_KEY_REUSED: Final = "idempotency_key_reused"
IDEMPOTENCY_SCHEMA_INCOMPATIBLE: Final = "idempotency_schema_incompatible"
IDEMPOTENCY_RETENTION_DAYS_KEY: Final = "work.idempotency_retention_days"
MAX_KEY_BYTES: Final = 256

JsonObject = dict[str, Any]
Adapter = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class IdempotencyContractError(ValueError):
    """A request cannot enter the retained keyed contract."""


class IdempotencySchemaIncompatible(Exception):
    """A retained value has no valid path to the declaration's current version."""


@dataclass(frozen=True, slots=True)
class CanonicalRequest:
    identity: JsonObject
    encoded: bytes
    digest: bytes
    key: str


@dataclass(frozen=True, slots=True)
class IdempotencyRow:
    operation_name: str
    idempotency_key: str
    request_identity: JsonObject
    request_digest: bytes
    request_schema_version: int
    state: str
    operation_id: UUID | None
    result: JsonObject | None
    result_schema_version: int | None
    error_code: str | None
    error_detail: str | None
    created_at: datetime
    updated_at: datetime
    retained_until: datetime


@dataclass(frozen=True, slots=True)
class Reservation:
    created: bool
    row: IdempotencyRow


@dataclass(frozen=True, slots=True)
class RetainedReplay:
    state: str
    result: BaseModel | None
    error_code: str | None
    error_detail: str | None
    operation_id: UUID | None


def _json_object(value: object, *, label: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise IdempotencySchemaIncompatible(f"{label} is not a JSON object")
    return dict(value)


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return text.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise IdempotencyContractError(
            "validated keyed input is not finite canonical UTF-8 JSON"
        ) from exc


def validate_idempotency_key(value: object) -> str:
    if not isinstance(value, str):
        raise IdempotencyContractError("idempotency key must be a string")
    if not value.strip():
        raise IdempotencyContractError("idempotency key must be nonblank")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise IdempotencyContractError("idempotency key must be valid UTF-8") from exc
    if len(encoded) > MAX_KEY_BYTES:
        raise IdempotencyContractError(
            f"idempotency key must be at most {MAX_KEY_BYTES} UTF-8 bytes"
        )
    return value


def canonical_request(
    declaration: OperationDeclaration, model_input: BaseModel
) -> CanonicalRequest:
    spec = declaration.keyed
    if not isinstance(spec, KeyedSpec):
        raise IdempotencyContractError("operation has no keyed declaration")
    key = validate_idempotency_key(getattr(model_input, spec.key_field, None))
    try:
        projected = model_input.model_dump(
            mode="json",
            by_alias=False,
            exclude_unset=False,
            exclude_defaults=False,
            exclude_none=False,
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IdempotencyContractError(
            "validated keyed input cannot be represented as canonical JSON"
        ) from exc
    identity: JsonObject = {
        "operation_name": declaration.name,
        "input": projected,
    }
    encoded = _canonical_bytes(identity)
    return CanonicalRequest(identity, encoded, sha256(encoded).digest(), key)


class KeyedCompatibilityRegistry:
    """Unique acyclic request and result adapter paths, keyed by operation."""

    def __init__(self) -> None:
        self._request: dict[tuple[str, int], tuple[int, Adapter]] = {}
        self._result: dict[tuple[str, int], tuple[int, Adapter]] = {}

    def _register(
        self,
        table: dict[tuple[str, int], tuple[int, Adapter]],
        operation_name: str,
        from_version: int,
        to_version: int,
        adapter: Adapter,
    ) -> None:
        if not operation_name or from_version <= 0 or to_version <= 0:
            raise ValueError("adapter operation and versions must be positive/nonblank")
        if from_version == to_version:
            raise ValueError("an adapter must advance to a different version")
        if not callable(adapter):
            raise TypeError("adapter must be callable")
        key = (operation_name, from_version)
        existing = table.get(key)
        if existing is not None:
            if existing == (to_version, adapter):
                return
            raise ValueError(
                f"{operation_name} version {from_version} has more than one path"
            )
        cursor = to_version
        visited = {from_version}
        while True:
            if cursor in visited:
                raise ValueError(f"{operation_name} adapter graph contains a cycle")
            visited.add(cursor)
            edge = table.get((operation_name, cursor))
            if edge is None:
                break
            cursor = edge[0]
        table[key] = (to_version, adapter)

    def register_request(
        self,
        operation_name: str,
        from_version: int,
        to_version: int,
        adapter: Adapter,
    ) -> None:
        self._register(self._request, operation_name, from_version, to_version, adapter)

    def register_result(
        self,
        operation_name: str,
        from_version: int,
        to_version: int,
        adapter: Adapter,
    ) -> None:
        self._register(self._result, operation_name, from_version, to_version, adapter)

    @staticmethod
    def _adapt(
        table: Mapping[tuple[str, int], tuple[int, Adapter]],
        operation_name: str,
        value: Mapping[str, Any],
        from_version: int,
        to_version: int,
    ) -> JsonObject:
        current = from_version
        adapted = dict(value)
        visited: set[int] = set()
        while current != to_version:
            if current in visited:
                raise IdempotencySchemaIncompatible("adapter graph contains a cycle")
            visited.add(current)
            edge = table.get((operation_name, current))
            if edge is None:
                raise IdempotencySchemaIncompatible(
                    f"no adapter from {operation_name} schema {current} to {to_version}"
                )
            next_version, adapter = edge
            try:
                adapted = _json_object(
                    adapter(dict(adapted)), label="adapted keyed value"
                )
                _canonical_bytes(adapted)
            except IdempotencySchemaIncompatible:
                raise
            except Exception as exc:
                raise IdempotencySchemaIncompatible(
                    f"adapter from {current} to {next_version} failed"
                ) from exc
            current = next_version
        return adapted

    def adapt_request(
        self,
        operation_name: str,
        value: Mapping[str, Any],
        from_version: int,
        to_version: int,
    ) -> JsonObject:
        return self._adapt(
            self._request, operation_name, value, from_version, to_version
        )

    def adapt_result(
        self,
        operation_name: str,
        value: Mapping[str, Any],
        from_version: int,
        to_version: int,
    ) -> JsonObject:
        return self._adapt(
            self._result, operation_name, value, from_version, to_version
        )


KEYED_COMPATIBILITY: Final = KeyedCompatibilityRegistry()


def _row(row: RowMapping) -> IdempotencyRow:
    raw_result = row["result"]
    return IdempotencyRow(
        operation_name=str(row["operation_name"]),
        idempotency_key=str(row["idempotency_key"]),
        request_identity=_json_object(
            row["request_identity"], label="stored request identity"
        ),
        request_digest=bytes(row["request_digest"]),
        request_schema_version=int(row["request_schema_version"]),
        state=str(row["state"]),
        operation_id=row["operation_id"],
        result=(
            None
            if raw_result is None
            else _json_object(raw_result, label="stored result")
        ),
        result_schema_version=(
            None
            if row["result_schema_version"] is None
            else int(row["result_schema_version"])
        ),
        error_code=(None if row["error_code"] is None else str(row["error_code"])),
        error_detail=(
            None if row["error_detail"] is None else str(row["error_detail"])
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        retained_until=row["retained_until"],
    )


def reserve(
    conn: Connection,
    *,
    declaration: OperationDeclaration,
    request: CanonicalRequest,
    operation_id: UUID | None,
    now: datetime,
    retained_until: datetime,
) -> Reservation:
    spec = declaration.keyed
    if spec is None:
        raise IdempotencyContractError("operation has no keyed declaration")
    inserted = (
        conn.execute(
            pg_insert(idempotency_result)
            .values(
                operation_name=declaration.name,
                idempotency_key=request.key,
                request_identity=request.identity,
                request_digest=request.digest,
                request_schema_version=spec.request_schema_version,
                state="pending",
                operation_id=operation_id,
                result=null(),
                result_schema_version=None,
                error_code=None,
                error_detail=None,
                created_at=now,
                updated_at=now,
                retained_until=retained_until,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    idempotency_result.c.operation_name,
                    idempotency_result.c.idempotency_key,
                ]
            )
            .returning(*idempotency_result.c)
        )
        .mappings()
        .one_or_none()
    )
    if inserted is not None:
        return Reservation(True, _row(inserted))
    existing = (
        conn.execute(
            select(idempotency_result).where(
                idempotency_result.c.operation_name == declaration.name,
                idempotency_result.c.idempotency_key == request.key,
            )
        )
        .mappings()
        .one()
    )
    return Reservation(False, _row(existing))


def replay(
    row: IdempotencyRow,
    *,
    declaration: OperationDeclaration,
    request: CanonicalRequest,
    adapters: KeyedCompatibilityRegistry = KEYED_COMPATIBILITY,
) -> RetainedReplay:
    spec = declaration.keyed
    if spec is None:
        raise IdempotencySchemaIncompatible("operation has no keyed declaration")
    try:
        original_encoded = _canonical_bytes(row.request_identity)
        if sha256(original_encoded).digest() != row.request_digest:
            raise IdempotencySchemaIncompatible(
                "stored keyed request identity does not match its digest"
            )
        stored_identity = adapters.adapt_request(
            declaration.name,
            row.request_identity,
            row.request_schema_version,
            spec.request_schema_version,
        )
        stored_encoded = _canonical_bytes(stored_identity)
    except (IdempotencyContractError, IdempotencySchemaIncompatible) as exc:
        raise IdempotencySchemaIncompatible(str(exc)) from exc
    if stored_encoded != request.encoded:
        return RetainedReplay(
            "refused",
            None,
            IDEMPOTENCY_KEY_REUSED,
            "the idempotency key is already bound to a different request",
            row.operation_id,
        )
    if row.state == "succeeded":
        if row.result is None or row.result_schema_version is None:
            raise IdempotencySchemaIncompatible("stored success has no result/version")
        adapted_result = adapters.adapt_result(
            declaration.name,
            row.result,
            row.result_schema_version,
            spec.result_schema_version,
        )
        try:
            model = declaration.output.model_validate(adapted_result)
        except ValidationError as exc:
            raise IdempotencySchemaIncompatible(
                "adapted stored result does not validate against current output"
            ) from exc
        return RetainedReplay("succeeded", model, None, None, row.operation_id)
    if row.state == "pending":
        return RetainedReplay("pending", None, None, None, row.operation_id)
    if row.error_code is None:
        raise IdempotencySchemaIncompatible("stored terminal error has no code")
    # A retained refusal replays the public refusal state, which is its stable code;
    # ``refused`` is the durable category used by the row/check constraint.
    replay_state = row.error_code if row.state == "refused" else row.state
    return RetainedReplay(
        replay_state,
        None,
        row.error_code,
        row.error_detail or row.error_code,
        row.operation_id,
    )


def finish_success(
    conn: Connection,
    *,
    declaration: OperationDeclaration,
    key: str,
    result: BaseModel,
    now: datetime,
) -> None:
    spec = declaration.keyed
    if spec is None:
        raise IdempotencyContractError("operation has no keyed declaration")
    body = result.model_dump(mode="json", by_alias=False)
    _canonical_bytes(body)
    changed = conn.execute(
        update(idempotency_result)
        .where(
            idempotency_result.c.operation_name == declaration.name,
            idempotency_result.c.idempotency_key == key,
            idempotency_result.c.state == "pending",
        )
        .values(
            state="succeeded",
            result=body,
            result_schema_version=spec.result_schema_version,
            error_code=None,
            error_detail=None,
            updated_at=now,
        )
    ).rowcount
    if changed != 1:
        raise RuntimeError("pending keyed reservation was not advanced exactly once")


def finish_error(
    conn: Connection,
    *,
    declaration: OperationDeclaration,
    key: str,
    state: str,
    error_code: str,
    error_detail: str,
    now: datetime,
) -> None:
    if state not in {"failed", "refused", "unresolved", "cancelled"}:
        raise ValueError(f"{state!r} is not a terminal keyed error state")
    changed = conn.execute(
        update(idempotency_result)
        .where(
            idempotency_result.c.operation_name == declaration.name,
            idempotency_result.c.idempotency_key == key,
            idempotency_result.c.state == "pending",
        )
        .values(
            state=state,
            result=null(),
            result_schema_version=None,
            error_code=error_code,
            error_detail=error_detail,
            updated_at=now,
        )
    ).rowcount
    if changed != 1:
        raise RuntimeError("pending keyed reservation was not advanced exactly once")


def purge_expired(conn: Connection, *, now: datetime) -> int:
    """Delete expired envelopes unless an open/unknown external action references it.

    After deletion, generic core replay is intentionally no longer promised. A
    domain may still recognize its own durable source identity, but a new effect must
    use a new intentional key/source identity.
    """

    protected = (
        select(literal(1))
        .where(
            external_action.c.operation_name == idempotency_result.c.operation_name,
            external_action.c.idempotency_key == idempotency_result.c.idempotency_key,
            external_action.c.state.in_(("pending", "executing", "unresolved")),
        )
        .exists()
    )
    return int(
        conn.execute(
            delete(idempotency_result).where(
                idempotency_result.c.retained_until < now,
                ~protected,
            )
        ).rowcount
    )

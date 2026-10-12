"""Typed Current references keep audit subjects and approval bindings intact."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator, PlainSerializer
from rheo_contracts import RecordRef

MODULE_ID = "current"
DELETABLE = frozenset({"item", "project"})


def _parse(kinds: frozenset[str]) -> Callable[[object], RecordRef]:
    def validate(value: object) -> RecordRef:
        reference = RecordRef.parse(value) if isinstance(value, str) else value
        if (
            not isinstance(reference, RecordRef)
            or reference.module != MODULE_ID
            or reference.record_type not in kinds
        ):
            raise ValueError("expected a " + " or ".join(sorted(kinds)) + " reference")
        return reference

    return validate


def _wire(value: RecordRef) -> str:
    return value.format()


ItemRef = Annotated[
    RecordRef,
    BeforeValidator(_parse(frozenset({"item"})), json_schema_input_type=str),
    PlainSerializer(_wire, return_type=str),
]
ProjectRef = Annotated[
    RecordRef,
    BeforeValidator(_parse(frozenset({"project"})), json_schema_input_type=str),
    PlainSerializer(_wire, return_type=str),
]


def ref(kind: str, identifier: UUID) -> str:
    return RecordRef(module=MODULE_ID, record_type=kind, id=identifier).format()

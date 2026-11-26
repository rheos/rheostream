"""Typed Current references keep audit subjects and approval bindings intact."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator, PlainSerializer, ValidationInfo
from rheo_contracts import RecordRef
from rheo_core.refs.short import ref_value

MODULE_ID = "current"
DELETABLE = frozenset({"item", "project"})


def _parse(kind: str) -> Callable[[object, ValidationInfo], RecordRef]:
    # A full reference parses; a short ID (#587), typed or bare since the field knows
    # its type, resolves through dispatch's validation context.
    def validate(value: object, info: ValidationInfo) -> RecordRef:
        reference = ref_value(value, info, MODULE_ID, kind)
        if (
            not isinstance(reference, RecordRef)
            or reference.module != MODULE_ID
            or reference.record_type != kind
        ):
            raise ValueError(f"expected a {kind} reference")
        return reference

    return validate


def _wire(value: RecordRef) -> str:
    return value.format()


ItemRef = Annotated[
    RecordRef,
    BeforeValidator(_parse("item"), json_schema_input_type=str),
    PlainSerializer(_wire, return_type=str),
]
ProjectRef = Annotated[
    RecordRef,
    BeforeValidator(_parse("project"), json_schema_input_type=str),
    PlainSerializer(_wire, return_type=str),
]


def ref(kind: str, identifier: UUID) -> str:
    return RecordRef(module=MODULE_ID, record_type=kind, id=identifier).format()

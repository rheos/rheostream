"""Typed canonical references keep audit subjects and approval bindings intact."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator, PlainSerializer
from rheo_contracts import RecordRef


def parse(value: object) -> RecordRef:
    ref = RecordRef.parse(value) if isinstance(value, str) else value
    if not isinstance(ref, RecordRef) or ref.module != "relationships":
        raise ValueError("expected a relationships reference")
    return ref


def typed(kind: str) -> Callable[[object], RecordRef]:
    def validate(value: object) -> RecordRef:
        ref = parse(value)
        if ref.record_type != kind:
            raise ValueError("unexpected reference type")
        return ref

    return validate


# Operations keep a typed ref for audit/approval; tool dumps keep the canonical
# string the next input validator accepts. JSON Schema advertises that same wire form.
def wire(value: RecordRef) -> str:
    return value.format()


PartyRef = Annotated[
    RecordRef,
    BeforeValidator(typed("party"), json_schema_input_type=str),
    PlainSerializer(wire, return_type=str),
]
MergeRef = Annotated[
    RecordRef,
    BeforeValidator(typed("merge_record"), json_schema_input_type=str),
    PlainSerializer(wire, return_type=str),
]
CandidateRef = Annotated[
    RecordRef,
    BeforeValidator(typed("review_candidate"), json_schema_input_type=str),
    PlainSerializer(wire, return_type=str),
]


def ref(kind: str, identifier: UUID) -> str:
    return RecordRef(module="relationships", record_type=kind, id=identifier).format()

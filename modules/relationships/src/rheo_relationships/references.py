"""Typed canonical references keep audit subjects and approval bindings intact."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator
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


PartyRef = Annotated[RecordRef, BeforeValidator(typed("party"))]
MergeRef = Annotated[RecordRef, BeforeValidator(typed("merge_record"))]
CandidateRef = Annotated[RecordRef, BeforeValidator(typed("review_candidate"))]


def ref(kind: str, identifier: UUID) -> str:
    return RecordRef(module="relationships", record_type=kind, id=identifier).format()

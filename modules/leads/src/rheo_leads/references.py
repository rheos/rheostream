"""Canonical Leads references, with content-free input refusals."""

from uuid import UUID

from rheo_contracts import RecordRef, RecordRefMalformed
from rheo_core.operations.refusals import OperationRefused


def ref(kind: str, identifier: UUID) -> str:
    return RecordRef(module="leads", record_type=kind, id=identifier).format()


def identifier(value: str, kind: str) -> UUID:
    try:
        parsed = RecordRef.parse(value)
    except RecordRefMalformed:
        raise OperationRefused("input_invalid", "invalid reference") from None
    if parsed.module != "leads" or parsed.record_type != kind:
        raise OperationRefused("input_invalid", "unexpected reference type")
    return parsed.id

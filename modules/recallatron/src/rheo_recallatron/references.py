"""Parsing the canonical references this module accepts, in one place.

Three callers need the same four lines — the read operations, the write machinery and
the entity service — and a second copy is how ``input_invalid`` and ``not_found`` end
up meaning different things on different paths. A reference is a value, never a join
(``rheo_contracts.refs``), so nothing here touches the database.

**A malformed reference is ``input_invalid``, and a well-formed one naming something
else is not.** ``recallatron.memory:<uuid>`` handed to an entity argument is a
perfectly canonical reference for the wrong kind of record, so it is still
``input_invalid`` — the caller's argument is wrong. A reference that is canonical,
of the right kind, and simply does not resolve for this caller is ``not_found``, and
that answer is the eligibility function's rather than this file's.
"""

from typing import Final
from uuid import UUID

from rheo_contracts import RecordRef, RecordRefMalformed
from rheo_core.operations.refusals import OperationRefused

from rheo_recallatron.configuration import (
    ENTITY_RECORD_TYPE,
    MEMORY_RECORD_TYPE,
    MODULE_ID,
)
from rheo_recallatron.refusals import INPUT_INVALID

ENTITY_REF_PREFIX: Final = f"{MODULE_ID}.{ENTITY_RECORD_TYPE}:"


def canonical_ref(reference: str) -> RecordRef:
    """Any canonical reference, or ``input_invalid``."""
    try:
        return RecordRef.parse(reference)
    except (RecordRefMalformed, TypeError):
        raise OperationRefused(INPUT_INVALID, "a reference must be canonical") from None


def is_memory_ref(parsed: RecordRef) -> bool:
    return parsed.module == MODULE_ID and parsed.record_type == MEMORY_RECORD_TYPE


def memory_target(reference: str) -> RecordRef:
    """A canonical reference that names one of this module's memories."""
    parsed = canonical_ref(reference)
    if not is_memory_ref(parsed):
        raise OperationRefused(INPUT_INVALID, "the target must be a memory reference")
    return parsed


def entity_reference(entity_id: UUID) -> str:
    """The canonical reference for one of this module's entities."""
    return f"{ENTITY_REF_PREFIX}{entity_id}"


def entity_target(reference: str) -> UUID:
    """A canonical reference that names one of this module's entities."""
    parsed = canonical_ref(reference)
    if parsed.module != MODULE_ID or parsed.record_type != ENTITY_RECORD_TYPE:
        raise OperationRefused(INPUT_INVALID, "the target must be an entity reference")
    return parsed.id

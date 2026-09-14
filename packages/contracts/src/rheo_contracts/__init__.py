"""Shared rheoStream contracts: pure models, no I/O.

Holds the manifest model, ``WorkspaceContext`` type, safety classes, the event and
observation envelopes, the runtime contract types, the shared repository protocols, and
the record-reference type. The observation envelope and the runtime contract types are
still scaffolds; 0b1 populated the context, safety, reference and declaration types, and
0c0 the event envelope and the repository protocols.

This distribution may import only the standard library, ``pydantic`` and itself. The
scan in ``tests/test_imports.py`` enforces that, and it is the reason the operation
handler is not a field on :class:`~rheo_contracts.manifest.OperationDeclaration`.
"""

from rheo_contracts.context import (
    ALL_OPERATIONS,
    Actor,
    ActorKind,
    AllOperations,
    Audience,
    AudienceKind,
    Entry,
    Role,
    WorkspaceContext,
)
from rheo_contracts.event_envelope import EventEnvelope
from rheo_contracts.manifest import (
    RESERVED_INPUT_FIELDS,
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    ToolDeclaration,
)
from rheo_contracts.refs import (
    RESERVED_MODULE_SEGMENT,
    RecordRef,
    RecordRefMalformed,
    is_reserved_module,
)
from rheo_contracts.repositories import Repository, StaleRecord
from rheo_contracts.safety import SafetyClass

CONTRACT_VERSION = 1

__all__ = [
    "ALL_OPERATIONS",
    "CONTRACT_VERSION",
    "RESERVED_INPUT_FIELDS",
    "RESERVED_MODULE_SEGMENT",
    "Actor",
    "ActorKind",
    "AllOperations",
    "AuditSpec",
    "Audience",
    "AudienceKind",
    "Entry",
    "EventEnvelope",
    "Idempotency",
    "OperationDeclaration",
    "RecordRef",
    "RecordRefMalformed",
    "Repository",
    "Role",
    "SafetyClass",
    "StaleRecord",
    "ToolDeclaration",
    "WorkspaceContext",
    "is_reserved_module",
]

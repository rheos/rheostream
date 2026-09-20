"""The deletion coordinator: the R5 cascade, its registry, and its content-free ledger.

**Re-exports the registry and the ledger's shapes, never ``operations.py``.** That
module imports ``rheo_core.operations``, whose own ``__init__`` imports
``operations/core_ops.py`` as its first statement, and ``core_ops`` reaches back here
for the declaration it registers — so a re-export of the operation module would make
that a real import cycle. ``operations/core_ops.py`` and ``approvals/gate.py`` both
reach ``rheo_core.deletion.operations`` by module path, inside the function that needs
it, exactly as they already reach ``rheo_core.tokens.issue`` and
``rheo_core.modules.operations``.
"""

from rheo_core.deletion.lifecycle import (
    WORKSPACE_LIFECYCLE_LOCK_KEY,
    lock_workspace_lifecycle,
)
from rheo_core.deletion.records import (
    DeletionRecordRow,
    deletion_ref,
    get_deletion_record,
    insert_deletion_record,
)
from rheo_core.deletion.registry import (
    NOTHING_REMOVED,
    OWNED_DELETIONS,
    RECORD_NOT_DELETABLE,
    DeleteAuthorization,
    DeleteAuthorizer,
    DeletionHandler,
    OwnedDeleter,
    OwnedDeletion,
    OwnedDeletionRegistry,
    RegisteredParticipant,
    RemovedMemories,
    authorize_owned_delete,
)
from rheo_core.deletion.tables import (
    DELETION_CAUSES,
    RETENTION_EXPIRY,
    USER_ERASURE,
    deletion_record,
)

__all__ = [
    "DELETION_CAUSES",
    "NOTHING_REMOVED",
    "OWNED_DELETIONS",
    "RECORD_NOT_DELETABLE",
    "RETENTION_EXPIRY",
    "USER_ERASURE",
    "WORKSPACE_LIFECYCLE_LOCK_KEY",
    "DeleteAuthorization",
    "DeleteAuthorizer",
    "DeletionHandler",
    "DeletionRecordRow",
    "OwnedDeleter",
    "OwnedDeletion",
    "OwnedDeletionRegistry",
    "RegisteredParticipant",
    "RemovedMemories",
    "authorize_owned_delete",
    "deletion_record",
    "deletion_ref",
    "get_deletion_record",
    "insert_deletion_record",
    "lock_workspace_lifecycle",
]

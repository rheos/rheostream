"""Approvals for destructive, external and financial operations: the record, the
binding tuple, the execution guards, the dispatcher's upper-class path,
``core.approval.approve`` / ``.refuse``, and the standing grants that may never cover
any of those three classes.

- ``tables.py`` — ``core.approval`` and ``core.approval_payload``, on their own
  ``MetaData`` so ``0003_approvals`` creates exactly those two.
- ``records.py`` — the repository: the mint, the four transitions, the two reads.
- ``binding.py`` — the payload digest and the comparison execution makes against it.
- ``guards.py`` — the guard protocol, the three guards the core attaches, and the
  attachment rule that decides which of them an operation gets.
- ``gate.py`` — hold a gated call, and execute an approved one.
- ``operations.py`` — the two operations a person approves or refuses through.
- ``grant_tables.py`` — ``core.standing_grant`` and ``core.standing_grant_operation``,
  on their own ``MetaData`` so ``0004_standing_grants`` creates exactly those two.
- ``grants.py`` — the grant repository, and what "covered" means.
- ``grant_operations.py`` — ``core.standing_grant.create`` and ``.revoke``, including
  the grant-time refusal of the three upper classes.

**The import direction, stated once here because every module in this package
depends on it.** ``rheo_core.operations`` may reach this package only from inside a
function — ``dispatch()``'s upper-class branch, and ``register_core_operations()`` —
and this package reaches ``rheo_core.operations`` freely, by submodule. The
asymmetry is not a preference: ``rheo_core.operations.__init__`` imports
``core_ops`` as its first statement, so a module-level import in that direction would
run this package while the other is only half-built. ``gate.py``'s own docstring
carries the full reasoning, and ``rheo_core.tokens.sets`` carries the same rule for
the same shape one package over.
"""

from rheo_core.approvals.binding import (
    INVALID_APPROVAL,
    binding_detail,
    executable_payload,
    payload_bytes,
    payload_digest,
)
from rheo_core.approvals.gate import (
    GUARD_REFUSED,
    PAYLOAD_TOO_LARGE,
    HeldCall,
    execute_approved,
    hold_for_approval,
    window_seconds,
)
from rheo_core.approvals.grant_operations import (
    CREATE_DECLARATION as GRANT_CREATE_DECLARATION,
)
from rheo_core.approvals.grant_operations import (
    GRANT_OPERATIONS,
    STANDING_GRANT_CLASS_REFUSED,
    STANDING_GRANT_CREATE,
    STANDING_GRANT_REVOKE,
    STANDING_GRANT_STATE,
    StandingGrantCreate,
    StandingGrantRecord,
    StandingGrantRef,
)
from rheo_core.approvals.grant_operations import (
    REVOKE_DECLARATION as GRANT_REVOKE_DECLARATION,
)
from rheo_core.approvals.grant_tables import (
    GRANT_TABLES,
    grant_metadata,
    standing_grant,
    standing_grant_operation,
)
from rheo_core.approvals.grants import GrantRow
from rheo_core.approvals.guards import (
    CORE_GUARDS,
    GUARDED_CLASSES,
    ActorPermissionGuard,
    Guard,
    GuardRefusal,
    RecordStateGuard,
    WindowGuard,
    core_guards_for,
    run_guards,
)
from rheo_core.approvals.operations import (
    APPROVAL_APPROVE,
    APPROVAL_OPERATIONS,
    APPROVAL_REFUSE,
    APPROVAL_STATE,
    APPROVE_DECLARATION,
    REFUSE_DECLARATION,
    ApprovalRecord,
    ApprovalRef,
    approve_handler,
    refuse_handler,
)
from rheo_core.approvals.records import ApprovalRow
from rheo_core.approvals.tables import (
    APPROVAL_STATES,
    approval,
    approval_metadata,
    approval_payload,
)

__all__ = [
    "APPROVAL_APPROVE",
    "APPROVAL_OPERATIONS",
    "APPROVAL_REFUSE",
    "APPROVAL_STATE",
    "APPROVAL_STATES",
    "APPROVE_DECLARATION",
    "CORE_GUARDS",
    "GRANT_CREATE_DECLARATION",
    "GRANT_OPERATIONS",
    "GRANT_REVOKE_DECLARATION",
    "GRANT_TABLES",
    "GUARDED_CLASSES",
    "GUARD_REFUSED",
    "INVALID_APPROVAL",
    "PAYLOAD_TOO_LARGE",
    "REFUSE_DECLARATION",
    "STANDING_GRANT_CLASS_REFUSED",
    "STANDING_GRANT_CREATE",
    "STANDING_GRANT_REVOKE",
    "STANDING_GRANT_STATE",
    "ActorPermissionGuard",
    "ApprovalRecord",
    "ApprovalRef",
    "ApprovalRow",
    "GrantRow",
    "Guard",
    "GuardRefusal",
    "HeldCall",
    "RecordStateGuard",
    "StandingGrantCreate",
    "StandingGrantRecord",
    "StandingGrantRef",
    "WindowGuard",
    "approval",
    "approval_metadata",
    "approval_payload",
    "approve_handler",
    "binding_detail",
    "core_guards_for",
    "executable_payload",
    "execute_approved",
    "grant_metadata",
    "hold_for_approval",
    "payload_bytes",
    "payload_digest",
    "refuse_handler",
    "run_guards",
    "standing_grant",
    "standing_grant_operation",
    "window_seconds",
]

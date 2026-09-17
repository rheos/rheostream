"""Approvals for destructive, external and financial operations: the record, the
binding tuple, the guard seam, the dispatcher's upper-class path, and
``core.approval.approve`` / ``.refuse``.

- ``tables.py`` — ``core.approval`` and ``core.approval_payload``, on their own
  ``MetaData`` so ``0003_approvals`` creates exactly those two.
- ``records.py`` — the repository: the mint, the three transitions, the two reads.
- ``binding.py`` — the payload digest and the comparison execution makes against it.
- ``guards.py`` — the execution-guard seam, deliberately empty in this chunk.
- ``gate.py`` — hold a gated call, and execute an approved one.
- ``operations.py`` — the two operations a person approves or refuses through.

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
from rheo_core.approvals.guards import CORE_GUARDS, Guard, GuardRefusal, run_guards
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
    "GUARD_REFUSED",
    "INVALID_APPROVAL",
    "PAYLOAD_TOO_LARGE",
    "REFUSE_DECLARATION",
    "ApprovalRecord",
    "ApprovalRef",
    "ApprovalRow",
    "Guard",
    "GuardRefusal",
    "HeldCall",
    "approval",
    "approval_metadata",
    "approval_payload",
    "approve_handler",
    "binding_detail",
    "executable_payload",
    "execute_approved",
    "hold_for_approval",
    "payload_bytes",
    "payload_digest",
    "refuse_handler",
    "run_guards",
    "window_seconds",
]

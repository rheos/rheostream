"""The audit seam: the ``AuditSink`` protocol, the table of installed sinks keyed by
owning module id, the core's own sink, the ``core.audit_record`` repository, and
``core.audit.list``'s models and handler.

**The dispatcher reaches a sink, and a module id with no registration still answers
``None``.** Issue #45's split left the protocol and the registry here as substrate and
the call site to run 0c2; 0c2 built it in ``rheo_core.operations.dispatch``, which
resolves ``sink_for(module_id)`` for every non-``READ`` operation and refuses
``audit_sink_missing`` when the answer is ``None`` — a refusal rather than a default
object standing in for a registration, which is the one answer #45 removed. See
``sink.py`` for why the installed sinks are keyed by module id rather than held in one
process-global slot.

**Nothing in this package imports ``rheo_core.operations``**, and that is structural
rather than tidy: ``operations/__init__.py`` imports ``core_ops`` as its first
statement and ``dispatch.py`` imports this package, so an import back would close a
real cycle. It is why ``check_audit_paths`` — which has to read a registry — lives
under ``rheo_core/operations/`` and not here.
"""

from rheo_core.audit.core_sink import CORE_AUDIT_SINK, CoreAuditSink
from rheo_core.audit.operations import (
    AUDIT_LIST,
    TELEMETRY_ROLES,
    AuditList,
    AuditListInput,
    AuditRecord,
    ToolTelemetryRecord,
    audit_list_handler,
)
from rheo_core.audit.records import (
    AUDIT_FAILED,
    AUDIT_REFUSED,
    AUDIT_SUCCEEDED,
    AuditOutcome,
    AuditRow,
    insert_audit_record,
    list_audit_records,
)
from rheo_core.audit.sink import (
    AuditSink,
    AuditSinkRefused,
    install_sink,
    reset_sinks,
    sink_for,
)
from rheo_core.audit.tool_telemetry import (
    TELEMETRY_LOCK_KEY,
    TOOL_MAX_ROWS_KEY,
    TOOL_RETENTION_DAYS_KEY,
    ToolTelemetryRow,
    list_tool_telemetry,
    purge_expired_tool_telemetry,
    record_tool_call,
)

__all__ = [
    "AUDIT_FAILED",
    "AUDIT_LIST",
    "AUDIT_REFUSED",
    "AUDIT_SUCCEEDED",
    "CORE_AUDIT_SINK",
    "TELEMETRY_LOCK_KEY",
    "TELEMETRY_ROLES",
    "TOOL_MAX_ROWS_KEY",
    "TOOL_RETENTION_DAYS_KEY",
    "AuditList",
    "AuditListInput",
    "AuditOutcome",
    "AuditRecord",
    "AuditRow",
    "AuditSink",
    "AuditSinkRefused",
    "CoreAuditSink",
    "ToolTelemetryRecord",
    "ToolTelemetryRow",
    "audit_list_handler",
    "insert_audit_record",
    "install_sink",
    "list_audit_records",
    "list_tool_telemetry",
    "purge_expired_tool_telemetry",
    "record_tool_call",
    "reset_sinks",
    "sink_for",
]

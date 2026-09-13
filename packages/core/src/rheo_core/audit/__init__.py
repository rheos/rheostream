"""The audit seam the dispatcher calls: a protocol, a no-op default, and a table of
installed sinks keyed by owning module id.

No audit *table* lives here. ``core.audit_record`` is run 0c's, and this package
writes nothing by itself — ``NullAuditSink`` is the production default. See
``sink.py`` for why the installed sinks are keyed by module id rather than held in one
process-global slot.
"""

from rheo_core.audit.sink import (
    NULL_SINK,
    AuditSink,
    AuditSinkRefused,
    NullAuditSink,
    install_sink,
    reset_sinks,
    sink_for,
)

__all__ = [
    "NULL_SINK",
    "AuditSink",
    "AuditSinkRefused",
    "NullAuditSink",
    "install_sink",
    "reset_sinks",
    "sink_for",
]

"""The audit seam's contract: the ``AuditSink`` protocol, and the table of installed
sinks keyed by owning module id.

Nothing here writes anything, and nothing here is called: no audit *table* lives in
this package (``core.audit_record`` is run 0c's) and no caller reaches a sink, which
is issue #45's split — the protocol and the registry are substrate, the call site is
0c2's. A module id with no registration is reported as ``sink_for`` returning
``None``, not as a default object standing in for one. See ``sink.py`` for why the
installed sinks are keyed by module id rather than held in one process-global slot,
and for what that ``None`` deliberately leaves undecided.
"""

from rheo_core.audit.sink import (
    AuditSink,
    AuditSinkRefused,
    install_sink,
    reset_sinks,
    sink_for,
)

__all__ = [
    "AuditSink",
    "AuditSinkRefused",
    "install_sink",
    "reset_sinks",
    "sink_for",
]

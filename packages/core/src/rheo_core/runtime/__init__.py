"""Core runtime orchestration: dispatch, job handler, and adapter registry.

``RuntimeRequest`` is constructed only in this package. Run-scoped tokens are
issued by ``rheo_core.tokens.issue.issue_runtime_token``, not here.
``WorkspaceContext`` is constructed only in ``rheo_core.boundary.factories``.
"""

from rheo_core.runtime.operations import (
    RUNTIME_RUN,
    RuntimeJobPayload,
    RuntimeRunInput,
    RuntimeRunScheduled,
    build_context,
    check_runtime_gate,
    make_run_runtime_job,
    runtime_run_handler,
)
from rheo_core.runtime.registry import AdapterRegistry

__all__ = [
    "RUNTIME_RUN",
    "AdapterRegistry",
    "RuntimeJobPayload",
    "RuntimeRunInput",
    "RuntimeRunScheduled",
    "build_context",
    "check_runtime_gate",
    "make_run_runtime_job",
    "runtime_run_handler",
]

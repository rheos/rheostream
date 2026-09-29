"""Core runtime orchestration: dispatch, job handler, and adapter registry.

``RuntimeRequest`` is constructed only in this package. Run-scoped tokens are
issued by ``rheo_core.tokens.issue.issue_runtime_token``, not here.
``WorkspaceContext`` is constructed only in ``rheo_core.boundary.factories``.
"""

# First, and on purpose (#142's order): ``rheo_core.operations`` imports
# ``rheo_core.runtime.operations`` through ``core_ops``, and only the order that starts
# at ``rheo_core.operations`` completes. Without this line a cold
# ``import rheo_core.runtime.<anything>`` dies on the cycle
# (``tests/test_evidence_imports.py`` imports ``rheo_core.runtime.extraction`` cold).
import rheo_core.operations  # noqa: F401
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

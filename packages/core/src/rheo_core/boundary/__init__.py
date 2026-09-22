"""The context boundary: the only constructor of ``WorkspaceContext``.

``factories.py`` holds the factory functions, each returning ``WorkspaceContext |
Refusal``; ``context.py`` holds :class:`Refusal` and the refusal-state constants.
This run (0b1, C4) ships ``context_for_operator`` and the ``profile = test``-gated
``context_for_harness``. The session and token factories (``context_from_session``,
``context_from_token``) are 0b2's and are added to ``factories.py`` there.

Boundary adapters that call the factories: the ``rheo`` CLI in this run; the ``api``
surface, the internal listener and the MCP facade in 0b2; the worker's job loader
and the intake receiver in later phases.
"""

from rheo_core.boundary.context import (
    CONTEXT_REQUIRED,
    MEMBERSHIP_MISSING,
    PROFILE_REQUIRED,
    WORKSPACE_MISSING_DETAIL,
    WORKSPACE_UNAVAILABLE,
    Refusal,
)
from rheo_core.boundary.factories import (
    MEMORY_EXPIRY_OPERATIONS,
    context_for_harness,
    context_for_memory_expiry,
    context_for_operator,
)

__all__ = [
    "CONTEXT_REQUIRED",
    "MEMBERSHIP_MISSING",
    "MEMORY_EXPIRY_OPERATIONS",
    "PROFILE_REQUIRED",
    "WORKSPACE_MISSING_DETAIL",
    "WORKSPACE_UNAVAILABLE",
    "Refusal",
    "context_for_harness",
    "context_for_memory_expiry",
    "context_for_operator",
]

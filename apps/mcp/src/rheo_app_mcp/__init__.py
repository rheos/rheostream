"""MCP facade: the session resolver, the ``call_tool`` seam, and the transport.

Three modules. ``session.py`` names the surface a bearer is presented against,
``tools.py`` is the ``list_tools``/``call_tool`` seam over the live tool
registry, and ``transport.py`` is the streamable-HTTP server the ``mcp`` SDK
speaks, built around that seam rather than beside it.

Imports ``rheo_core.boundary``, ``rheo_core.operations`` and ``rheo_core.tokens``
(the AST scan in ``tests/test_mcp_boundary.py`` pins this allowlist, landed in
0b2 rather than "later") plus ``rheo_contracts``, and nothing from
``rheo_core.storage``, ``rheo_core.migrations``, or a DB driver. The transport
did not widen that list: it reaches storage only through the same ``dispatch``
call the seam already made.

Nothing here builds a server at import time. ``apps/core``'s ``main.py`` imports
this package to record the composition-root edge and does not run it; a caller
that wants the endpoint calls ``transport.build_mcp_app()``.
"""

from rheo_contracts import CONTRACT_VERSION

__all__ = ["CONTRACT_VERSION"]

"""Tokens (``cli``/``mcp``) and their operation-set snapshots (C8, run 0b2).

``format.py`` -- the ``rheo_<kind>_<43 chars>`` string shape. ``policy.py`` --
``NON_TOKEN_ISSUABLE``, the eight names no token may ever carry. ``sets.py`` --
the three named package sets, evaluated against the registry at issuance, and
the MCP facade's tool table. ``issue.py`` -- the handlers behind
``core.token.issue``/``core.token.revoke`` (registered in
``operations/core_ops.py``). ``presentation.py`` -- the presented-token
refusal chain behind ``boundary.factories.context_from_token``.

**Only ``format`` and ``policy`` -- true leaves with no ``rheo_core`` imports of
their own -- are re-exported here.** ``sets``, ``issue`` and ``presentation``
are not, and every caller reaches them through their own submodule path
instead (``from rheo_core.tokens.sets import ...``, and so on). This is load
bearing, not tidiness: ``operations/registry.py`` imports
``rheo_core.boundary.context``, and ``rheo_core.boundary``'s own ``__init__.py``
imports ``factories.py``, which imports ``rheo_core.tokens.presentation`` --
so importing *this* package's ``__init__`` is already reachable from deep
inside ``operations.registry``'s own import. If this file eagerly re-exported
``sets`` (which needs ``rheo_core.operations.registry`` for
``OperationRegistry``/``REGISTRY``), that one edge would close a real cycle:
``operations.registry`` -> ``boundary`` -> ``factories`` -> ``tokens``
(``__init__``) -> ``sets`` -> ``operations.registry`` (still mid-init).
Keeping this file to leaves only means the first time ``tokens/__init__.py``
runs -- however it is reached -- it can never need something that is not
finished loading yet.
"""

from rheo_core.tokens.format import ParsedToken, mint, parse
from rheo_core.tokens.policy import NON_TOKEN_ISSUABLE

__all__ = [
    "NON_TOKEN_ISSUABLE",
    "ParsedToken",
    "mint",
    "parse",
]

"""Alembic environment for one fixture module's per-workspace chain.

Identical in every fixture chain: the module id comes from this file's own directory
name, so the version table it configures cannot name a different module from the one
whose chain this is. See ``harness.module_migrations`` for why that derivation is
right here and a literal is right in a shipped module.
"""

from harness.module_migrations import configure_module_chain, module_id_of

configure_module_chain(module_id_of(__file__))

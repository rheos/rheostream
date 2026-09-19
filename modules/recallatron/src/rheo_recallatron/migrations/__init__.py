"""Recallatron's Alembic script directory, packaged so the installed wheel works.

A real package rather than a bare directory, and that is load-bearing:
``manifest.storage.migrations_path`` names this module, and its reader resolves the
directory with ``importlib.resources.files(...)`` — which hands back a plain
``Path`` for a regular package and a ``MultiplexedPath`` for a namespace one, whose
``str()`` is a repr rather than a filesystem path. ``rheo_core.migrations`` carries
an ``__init__.py`` for exactly this reason; ``script_location()`` there converts
with ``Path(str(...))``.

**No Alembic by hand.** ``env.py`` refuses unless ``rheo_core.migrations.
orchestrator`` has passed a live connection; the rule and its reasons are stated
once, in ``rheo_core.migrations``.
"""

"""The migration orchestrator: the only entry point for Alembic.

**No Alembic by hand: this paragraph is the rule's one home.** There is no
``alembic.ini`` and no console script in the image; each chain's ``env.py`` never
reads ``sqlalchemy.url`` and refuses with a ``RuntimeError`` unless
``orchestrator.py`` has passed the live connection through
``config.attributes["connection"]``, so Alembic invoked by hand has no URL, no
connection, and nothing to upgrade. The other files in this package cross-reference
this paragraph rather than restate it.

Two chains live here as Alembic script directories: ``control/`` (the control plane,
one ratified revision creating all ten tables, never edited afterwards) and ``core/``
(the per-workspace ``core`` schema, one revision creating exactly six tables; later
chain steps append revisions). A module chain is the third kind, and it does not live
here: it ships inside the module's own distribution, and
``orchestrator.script_location`` resolves it through that manifest's
``storage.migrations_path``. ``module_chain.py`` is the door a module install comes
through — the one that raises its chain's failure to the caller rather than marking
the whole workspace unavailable, which is what ``migrate_workspace`` does and why it
refuses a module chain name.
"""

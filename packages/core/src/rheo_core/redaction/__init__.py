"""The redaction contract (``runtime-and-mcp.md`` § The redaction contract), enforced.

What leaves the deployment for a model provider, decided field by field. Five pieces,
each imported by module path rather than re-exported here, because
``rheo_core.modules.manifest`` imports :mod:`rheo_core.redaction.tiers` and this
package's ``__init__`` runs first on that import: a re-export of anything that reaches
the settings resolver or the storage layer from here would put that on the manifest's
import path for no reader.

- :mod:`~rheo_core.redaction.tiers`: the three tiers, the :class:`Tiered` field marker
  a pydantic model (an event's ``data``, an operation's output) declares a field's tier
  with, and the walks that read those markers.
- :mod:`~rheo_core.redaction.policy`: which tiers one purpose may send, computed from
  the ``redaction.*`` settings.
- :mod:`~rheo_core.redaction.masking`: the free-text backstop for contact values and
  secret references inside a string.
- :mod:`~rheo_core.redaction.render`: the default record renderer and the tool-output
  renderer, plus the two callable shapes a record type declares.
- :mod:`~rheo_core.redaction.registry`: the record type -> rendering table the loader
  fills and the context builder reads.
"""

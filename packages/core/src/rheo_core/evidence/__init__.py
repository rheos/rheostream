"""Automatic-memory evidence: the core side of the Rheo-owned producer (run 1a4a).

Submodules, each imported by its own path for now:

- :mod:`~rheo_core.evidence.sanitize`: the pure sanitizer every evidence text passes
  through before it is stored or shown to a model (FR 4).
- :mod:`~rheo_core.evidence.attribution`: which actor kinds are a person speaking.
- :mod:`~rheo_core.evidence.providers`: the extraction provider seam and its registry.

``__all__`` is the locked public surface Recallatron may reach. It is empty until the
recorded event and the eligible-evidence service land; nothing here is re-exported
before then.
"""

__all__: list[str] = []

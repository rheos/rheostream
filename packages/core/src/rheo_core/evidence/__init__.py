"""Automatic-memory evidence: the core side of the Rheo-owned producer (run 1a4a).

Submodules, each imported by its own path:

- :mod:`~rheo_core.evidence.sanitize`: the pure sanitizer every evidence text passes
  through before it is stored or shown to a model (FR 4).
- :mod:`~rheo_core.evidence.attribution`: which actor kinds are a person speaking.
- :mod:`~rheo_core.evidence.providers`: the extraction provider seam and its registry.
- :mod:`~rheo_core.evidence.record`: the ingest path, its recording gate, and the
  recorded event.

``__all__`` is the locked public surface Recallatron may reach. It grows only as the
eligible-evidence service lands; today it is the recorded event's type alone.
"""

from rheo_core.evidence.record import EVIDENCE_RECORDED

__all__: list[str] = ["EVIDENCE_RECORDED"]

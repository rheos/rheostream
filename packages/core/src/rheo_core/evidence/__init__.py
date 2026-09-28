"""Automatic-memory evidence: the core side of the Rheo-owned producer (run 1a4a).

Submodules, each imported by its own path:

- :mod:`~rheo_core.evidence.sanitize`: the pure sanitizer every evidence text passes
  through before it is stored or shown to a model (FR 4).
- :mod:`~rheo_core.evidence.attribution`: which actor kinds are a person speaking.
- :mod:`~rheo_core.evidence.providers`: the extraction provider seam and its registry.
- :mod:`~rheo_core.evidence.record`: the ingest path, its recording gate, and the
  recorded event.
- :mod:`~rheo_core.evidence.service`: the eligible-evidence service, claim and settle.

``__all__`` is the locked public surface Recallatron may reach, exactly these seven
names. Nothing else joins it: not the authority, not the workspace resolution, not a
table or a query helper.
"""

from rheo_core.evidence.record import EVIDENCE_RECORDED
from rheo_core.evidence.service import (
    ClaimedBatch,
    ClaimedUnit,
    EvidenceAuditUnwritable,
    EvidenceClaimInconsistent,
    claim_units,
    settle_unit,
)

__all__: list[str] = [
    "EVIDENCE_RECORDED",
    "ClaimedBatch",
    "ClaimedUnit",
    "EvidenceAuditUnwritable",
    "EvidenceClaimInconsistent",
    "claim_units",
    "settle_unit",
]

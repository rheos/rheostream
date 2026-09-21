"""The refusal states Recallatron's read surface answers with.

Three of them are new vocabulary this module owns, one is re-declared from the core's
runtime package on purpose, and two are imported from the core rather than restated.

**Why ``PURPOSE_MISMATCH`` is spelled here instead of imported.** The core declares the
same string beside its runtime-job refusals, and AC 3's import-graph scan asserts that
Recallatron's own source tree imports nothing from the core's runtime package — the
mechanical proof that 1a1 performs no automatic transcript extraction. Importing a
four-word constant would spend that proof, so the string is declared once more here and
the two are held in step by the single acceptance criterion that names it. The core's
own boundary package makes the same call for the same reason, one cycle further out.

**Why ``NOT_FOUND`` and ``INPUT_INVALID`` are imported.** Neither sits behind a
boundary this module must not cross, both are the states the dispatcher and the record
resolver already answer with, and a second copy of either would be a second vocabulary
a caller has to learn.
"""

from typing import Final

from rheo_core.operations.refusals import INPUT_INVALID as INPUT_INVALID
from rheo_core.refs.resolver import NOT_FOUND as NOT_FOUND

RETENTION_UNAVAILABLE: Final = "retention_unavailable"
"""The workspace's retention policy is missing, unparseable or out of range (AC 8).

Every read path refuses with it **before** any container scan or content resolution,
and the write paths and the sweep import this same symbol rather than re-declaring the
string. Fail-closed: a reader never substitutes the package default for a row that
should be there and is not.
"""

CONTAINER_MEMBERSHIP_REQUIRED: Final = "container_membership_required"
"""The authorized target is not linked to the container the caller named.

Answered from step 3 of the read precedence, before a single neighbour is selected, so
a caller cannot learn anything about a container's population through a target that
does not belong to it.
"""

WINDOW_SCAN_LIMIT: Final = "window_scan_limit"
"""More than 500 row-locally eligible candidates would have to be checked.

The refusal is fixed and content-free: no target content, no candidate count, no
eligible or hidden count, no identity, position, total, bounds, ``has_more`` or partial
items. It is reachable only by a caller that has already proved target eligibility and
exact container membership.
"""

REFERENCE_SCAN_LIMIT: Final = "reference_scan_limit"
"""The request's shared distinct-reference or depth budget was exhausted.

No partial content and no window metadata accompany it, at any step.
"""

PURPOSE_MISMATCH: Final = "purpose_mismatch"
"""A well-formed purpose that is not the one this context is bound to.

Distinct from ``input_invalid``, which is what an unparseable purpose gets: the first
says "not yours", the second says "not a purpose", and collapsing them would make a
malformed value indistinguishable from a refused one.
"""

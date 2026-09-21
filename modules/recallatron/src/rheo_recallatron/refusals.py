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

AUDIENCE_UNAVAILABLE: Final = "audience_unavailable"
"""The requested audience is wider than this caller's write ceiling (§ A4).

Bound delegated work with an account has that account's member ceiling, and an
accountless service may create workspace memory only. Asking for more is refused
rather than quietly narrowed, because a caller that asked for a workspace audience and
silently got a private one would believe it had shared something it had not.
"""

AUDIENCE_EMPTY: Final = "audience_empty"
"""A derive whose sources have no common audience — two different member audiences
meet nowhere. Refused **before** any write, so no row and no entity is created."""

PURPOSES_EMPTY: Final = "purposes_empty"
"""No purpose survives: an explicitly empty set, an omitted set on an unbound caller,
or a derive whose sources share none. Every memory carries at least one purpose in the
same transaction as its row, so there is no "decide later" state to write."""

USE_DERIVE: Final = "use_derive"
"""``remember`` was handed a memory reference in a provenance or about relation.

Refused before a row or an entity is created. A memory built from other memories is a
derivation, and derivation is the operation that computes the audience meet and the
purpose intersection — accepting the ref here would store a memory whose restrictions
were never narrowed by the sources it came from.
"""

AUTHORITY_UNVERIFIED: Final = "authority_unverified"
"""A trusted source unit whose authority could not be verified (§ A5).

Content-free, and it writes **no receipt**: a unit whose authority is unverifiable has
not proved which partition it belongs to, so recording an outcome against the key it
claimed would let an unauthorized caller terminalize somebody else's identity.
"""

SOURCE_UNAVAILABLE: Final = "source_unavailable"
"""The one answer every non-replayable receipt state gives (§ A5).

A changed digest, a noncurrent or missing representation, and an erased, expired,
noop, denied or orphaned receipt all answer with this single state. They are one word
on purpose: telling them apart would report whether a source was once accepted and
then erased, which is exactly the fact an erasure removes.
"""

RECORD_STALE: Final = "record_stale"
"""The supplied ``expected_revision`` is not the revision the row holds (§ A7).

Compared **before** the current-state test and after the history-capable
authorization, which is what makes it the answer the loser of a concurrent
supersession gets: the winner has already incremented the predecessor's revision, so
the loser's compare-and-set misses and it is told its copy is stale rather than told
the record is gone.

Distinct from ``not_found``, and the distinction is the whole value: ``not_found``
means "nothing you may act on is here", ``record_stale`` means "something is, and it
has moved". A caller that got ``not_found`` for a revision mismatch would retry
nothing; one that gets this rereads and retries.
"""

PURPOSE_MISMATCH: Final = "purpose_mismatch"
"""A well-formed purpose that is not the one this context is bound to.

Distinct from ``input_invalid``, which is what an unparseable purpose gets: the first
says "not yours", the second says "not a purpose", and collapsing them would make a
malformed value indistinguishable from a refused one.
"""

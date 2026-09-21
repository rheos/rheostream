"""The module's identity, its two settings keys, and Architecture § A13's fixed limits.

**A leaf module, and that is the whole reason it exists.** ``manifest.py`` declares the
operations, so it imports ``operations.py``, which imports ``eligibility.py`` — and
eligibility has to read the retention keys to answer anything at all. Leaving them on
the manifest would close that loop. Everything here imports nothing from this package,
so every other module in it can reach these values whichever one is imported first.

§ A13 asks for the module's fixed limits "centralized as named constants". This is that
one place: the read-side bounds, the traversal budget and — since the write half
exists — the write-side bounds on title, body, entity name, refs, mentions and the
trusted-ingest identity.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from rheo_core.settings import KeySpec, Scope, ValueType

MODULE_ID: Final = "recallatron"

MEMORY_RECORD_TYPE: Final = "memory"
"""The one record type this run's resolver answers for; a memory reference reads
``<module id>.<this>:<uuid>``."""

ENTITY_RECORD_TYPE: Final = "memory_entity"
"""The second addressable record type, and the one with **no** resolver.

An entity is reachable only through an eligible mention or an independently readable
backing ref, so generic resolution has nothing to answer for it: a resolver would be a
second route to an entity, reachable by anyone holding a well-formed id.
"""

RETENTION_EXPIRE_BY_AGE_KEY: Final = f"{MODULE_ID}.retention.expire_by_age"
"""Whether this workspace deletes memories by age **at all** (§ A9).

**Off by default, and off means there is no horizon.** Recallatron never auto-forgets a
memory by age: a memory is corrected or superseded, not expired for having got old. A
workspace that wants an age bound says so by setting this row to true, and until it
does no read, write, acceptance, export or sweep applies an age predicate of any kind.

``explicit_per_workspace``, like the window below, so every workspace's retention
posture is a stored readable value rather than an inference from an absent row: the
module-enable step writes **both** rows, and turning expiry on is then one settings
write against a schedule that already exists.

**Absent or unparseable means ``false``, and that asymmetry with the window is
deliberate.** For this key, falling back to the package default *is* the safe
direction, because the default retains; for the window, falling back to a default
would silently widen a policy at the moment it went missing. So a pre-correction
workspace holding no row at all resolves ``false`` with no backfill, while a missing
``days`` row still refuses — but only in the one state that reads it.
"""

RETENTION_EXPIRE_BY_AGE_DEFAULT: Final = False

RETENTION_EXPIRE_BY_AGE_SPEC: Final = KeySpec(
    key=RETENTION_EXPIRE_BY_AGE_KEY,
    type=ValueType.BOOL,
    scope=Scope.WORKSPACE,
    floor=None,
    explicit_per_workspace=True,
    default=RETENTION_EXPIRE_BY_AGE_DEFAULT,
)

RETENTION_DAYS_KEY: Final = f"{MODULE_ID}.retention.days"
"""How long a memory stays readable in a workspace, in days — **while the gate is on**.

Read only when :data:`RETENTION_EXPIRE_BY_AGE_KEY` resolves true. With the gate off
this key is not a mandatory setting: nothing reads it, so nothing can refuse for it.

``explicit_per_workspace`` because a workspace's retention is its own stated policy
from the moment the module is enabled, not an inherited deployment value it happens to
share: the module-enable step writes the row from the default below, and the core's own
settings-write operation moves it from there. (Named in prose rather than spelled out,
because the schema-ownership scan reads a ``<core-schema>.<name>`` literal in this tree
as a foreign table reference and is right to — this module must not name one, even in a
docstring.)

**Bounded rather than floored, and the two are different tools.** A floor combines a
workspace's override with the deployment's value under a comparator — which is right
for an approval window, where a workspace may only shorten its own. Retention is not
narrowable-only-downward: a workspace may legitimately choose a longer window than its
neighbour. What must not be settable at *any* layer is zero days (every read empty the
instant it commits) or a millennium, and that is what ``minimum``/``maximum`` express
and a floor cannot. "No bound at all" is the *gate's* answer and never a magic number
on this key.

**Missing or corrupt is not "use the default".** Every read, write and sweep refuses
``retention_unavailable`` when the stored row is absent, unparseable or outside the
range below (AC 8) — while the gate is on. The default is what *enable* writes; it is
never what a reader substitutes for a row somebody deleted, because substituting it
would silently widen a workspace's retention window at exactly the moment its policy
went missing.
"""

RETENTION_DAYS_DEFAULT: Final = 365
RETENTION_DAYS_MINIMUM: Final = 1
RETENTION_DAYS_MAXIMUM: Final = 3650

RETENTION_DAYS_SPEC: Final = KeySpec(
    key=RETENTION_DAYS_KEY,
    type=ValueType.INT,
    scope=Scope.WORKSPACE,
    floor=None,
    explicit_per_workspace=True,
    default=RETENTION_DAYS_DEFAULT,
    minimum=RETENTION_DAYS_MINIMUM,
    maximum=RETENTION_DAYS_MAXIMUM,
)
"""The declaration the manifest publishes and every reader validates a row against.

Readers validate against **this spec**, not against whatever the process-wide settings
registry happens to hold: the registry's contents depend on which modules a process
loaded, and a retention bound must not.
"""


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """What the two rows together say: a window, or no window at all (§ A9).

    **A value rather than an ``int | None``, because the ``None`` would have meant two
    opposite things.** The reader this replaces answered ``int | None``, where ``None``
    was ``retention_unavailable`` — fail closed, refuse everything. "No age bound"
    needed a third answer, and reusing ``None`` for it would have made every existing
    branch mean the opposite of what it says. So the *unavailable* answer keeps the
    ``None``, and this value carries the two usable policies; a branch that forgets one
    of them is a ``make typecheck`` failure rather than a sweep.

    ``days is None`` is the default posture: no horizon exists, so no read filters by
    age, no sweep selects a root, and no acceptance rechecks a source clock.
    """

    days: int | None

    @classmethod
    def unbounded(cls) -> "RetentionPolicy":
        """The gate is off. Nothing in this workspace expires by age."""
        return cls(days=None)

    @classmethod
    def of_days(cls, days: int) -> "RetentionPolicy":
        """The gate is on, against a validated in-range window."""
        return cls(days=days)

    @property
    def expires_by_age(self) -> bool:
        return self.days is not None

    def horizon(self, now: datetime) -> datetime | None:
        """The oldest ``recorded_at`` still readable, or ``None`` for no horizon.

        Equality is retained (§ A9), so every comparison against a horizon is ``<``
        for expired and ``>=`` for kept.
        """
        return None if self.days is None else now - timedelta(days=self.days)


# --- Architecture § A13: recall and read input bounds ---------------------------------

RECALL_QUERY_MIN_LENGTH: Final = 1
RECALL_QUERY_MAX_LENGTH: Final = 1000
RECALL_K_MIN: Final = 1
RECALL_K_MAX: Final = 50
RECALL_K_DEFAULT: Final = 10

READ_CONTEXT_MIN: Final = 0
READ_CONTEXT_MAX: Final = 10
READ_CONTEXT_DEFAULT: Final = 2

# --- Architecture § A13: the bounded scan and the shared traversal budget ------------

CANDIDATE_SCAN_LIMIT: Final = 500
"""How many ordered candidates a read may resolve, and how far recall may scan.

The read window's cap is § A6's verbatim: at most 500 candidates are fully evaluated.
Recall reuses the same number as the bound on how deep into the scored candidate list
it will look for ``k`` eligible rows, rather than declaring a second limit that would
have to be justified separately and kept in step with this one.
"""

CANDIDATE_SENTINEL_LIMIT: Final = CANDIDATE_SCAN_LIMIT + 1
"""``LIMIT 501``. The 501st identifier is an existence test and never a content read:
its presence is the whole of the ``window_scan_limit`` signal, and no part of it is
resolved, counted or described."""

MAX_DISTINCT_REFERENCES: Final = 4096
MAX_REFERENCE_DEPTH: Final = 64
"""One request-wide budget, shared by the target, every candidate, and every link
either of them reaches. Exceeding it refuses ``reference_scan_limit`` with no partial
content and no window metadata."""

# --- Architecture § A13: the write-side bounds ---------------------------------------

TITLE_MIN_LENGTH: Final = 1
TITLE_MAX_LENGTH: Final = 200
"""Trimmed characters. A title of spaces is not a short title, it is a missing one."""

BODY_MAX_BYTES: Final = 65536
"""UTF-8 **bytes**, not characters, and nonblank.

Bytes because that is what the column and every transport actually cost; a
character bound would admit a body four times the size it was meant to.
"""

ENTITY_NAME_MIN_LENGTH: Final = 1
ENTITY_NAME_MAX_LENGTH: Final = 200
MENTION_ROLE_MAX_LENGTH: Final = 100
"""The typed half of the bound ``memory_mention_role_length`` already holds in DDL."""

MAX_REFS_PER_WRITE: Final = 64
MAX_MENTIONS_PER_WRITE: Final = 64
DERIVE_SOURCES_MIN: Final = 1
DERIVE_SOURCES_MAX: Final = 64
"""§ A13's per-write ceilings. They bound what a caller *supplies*; they deliberately
do not bound what derive **inherits**, which is checked against the shared reference
budget instead — a copied graph too large to read back refuses
``reference_scan_limit`` rather than being silently truncated."""

MINIMUM_REVISION: Final = 1
"""§ A7's floor on a compare-and-set, and the database's own floor on the column.

``memory_revision_positive`` refuses a row below it, so a caller supplying a smaller
``expected_revision`` is naming a revision no row can ever hold — which is an input
error rather than a stale copy, and is refused as one.
"""

ENTITY_LIST_LIMIT_MIN: Final = 1
ENTITY_LIST_LIMIT_MAX: Final = 50
ENTITY_LIST_LIMIT_DEFAULT: Final = 10

AUTOMATIC_BOUND_PURPOSE: Final = "internal_analysis"
"""§ A5: automatic acceptance binds exactly one purpose, for both producer kinds.

Named here rather than inlined at the seam because the product consequence is the
reason it is a constant: a memory recorded under it is invisible to any bound read
requiring a different purpose, and visible to an unbound browse and to a read bound to
it. Changing this value changes that visibility for every automatic memory, which is a
spec amendment and not a local edit.
"""

AUTOMATIC_PRODUCER_KINDS: Final = ("rheo_runtime", "claude_code_local")
"""The two producer kinds whose acceptance binds :data:`AUTOMATIC_BOUND_PURPOSE`.

``migration`` is the third receipt producer kind and is deliberately absent: it may be
unbound and preserves each imported memory's own ratified purposes, under its
separately verified import contract.
"""

"""The module's identity, its one settings key, and Architecture § A13's fixed limits.

**A leaf module, and that is the whole reason it exists.** ``manifest.py`` declares the
operations, so it imports ``operations.py``, which imports ``eligibility.py`` — and
eligibility has to read the retention key to answer anything at all. Leaving the key on
the manifest would close that loop. Everything here imports nothing from this package,
so every other module in it can reach these values whichever one is imported first.

§ A13 asks for the module's fixed limits "centralized as named constants". This is that
one place: the read-side bounds and the traversal budget are below, and the write-side
bounds (title, body, entity name, source key) belong beside them when the run that
needs them arrives.
"""

from typing import Final

from rheo_core.settings import KeySpec, Scope, ValueType

MODULE_ID: Final = "recallatron"

MEMORY_RECORD_TYPE: Final = "memory"
"""The one record type this run's resolver answers for; a memory reference reads
``<module id>.<this>:<uuid>``."""

RETENTION_DAYS_KEY: Final = f"{MODULE_ID}.retention.days"
"""How long a memory stays readable in a workspace, in days.

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
and a floor cannot.

**Missing or corrupt is not "use the default".** Every read, write and sweep refuses
``retention_unavailable`` when the stored row is absent, unparseable or outside the
range below (AC 8). The default is what *enable* writes; it is never what a reader
substitutes for a row somebody deleted, because substituting it would silently widen a
workspace's retention window at exactly the moment its policy went missing.
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

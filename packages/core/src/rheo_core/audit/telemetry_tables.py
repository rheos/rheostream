"""DDL for ``core.tool_telemetry``, the bounded tool-call metadata sink (core 0008).

**Nothing here can hold what a caller said or what a tool answered**, which is § A11's
whole point rather than a property to test for afterwards. The columns are a tool
name, its declared safety class, a lifecycle mode from a closed set, three integers,
an outcome from a closed set, and an array of *argument names*. There is no query
column, no value column, no body, title, prompt, credential or exception-text column,
and no json column a caller could put one in.

``query_length`` is the one column that measures a caller's input, and it measures it
rather than storing it: an integer cannot be read back as the query. It is nullable
because only a ``read`` tool that declares a query field has one to measure.

``argument_names`` holds the *declared* names a write call supplied — never the
values, and never a name the tool's own input model does not declare, so a crafted
argument name cannot become the content this table is not allowed to hold. Its
cardinality is bounded in the database as well as by the writer, for the reason every
other bound here is: a bound only the writer keeps is a bound one caller away from
being gone.

**Its own ``MetaData``, like ``deletion/tables.py``'s.** Revision 0008 creates exactly
this table, so frozen revisions 0001-0007 still create exactly their own sets.
"""

from typing import Final

from rheo_contracts import SafetyClass
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from rheo_core.storage.core_tables import CORE_SCHEMA

TELEMETRY_MODE_NONE: Final = "none"
TELEMETRY_MODE_CURRENT: Final = "current"
TELEMETRY_MODE_HISTORY: Final = "history"
TELEMETRY_MODES: Final = (
    TELEMETRY_MODE_NONE,
    TELEMETRY_MODE_CURRENT,
    TELEMETRY_MODE_HISTORY,
)
"""§ A11's "allowlisted mode", as the closed set the check constraint holds it to.

``mode`` is the *lifecycle* read mode — the only thing this Architecture calls a
"mode" anywhere else (§ A6: "the requested lifecycle mode (current: no invalidation
and no successor; history: ...)"), and distinct from ``safety_class``, which this row
already carries in its own column. A tool that declares no lifecycle selector has
:data:`TELEMETRY_MODE_NONE`, which is a value rather than a NULL so that "this tool
has no mode" and "nobody wrote a mode" cannot be the same row.

Allowlisted rather than merely bounded: the writer's own ``Literal`` is checked
against this tuple at import time, and the database refuses anything outside it, so a
caller-supplied string can never reach this column however the writer is called.
"""

TELEMETRY_SUCCESS: Final = "success"
TELEMETRY_REFUSED: Final = "refused"
TELEMETRY_ERROR: Final = "error"
TELEMETRY_OUTCOMES: Final = (TELEMETRY_SUCCESS, TELEMETRY_REFUSED, TELEMETRY_ERROR)
"""§ A11's "success/refused/error", spelled as the Architecture spells it.

Deliberately **not** ``rheo_core.audit.records.AUDIT_OUTCOMES``
(``succeeded``/``failed``/``refused``), which is the audit record's vocabulary for a
different question. Two of the three words differ, and folding them together would
make a telemetry row claim to be an audit row — the distinction § A11 closes with
"telemetry is a distinct optional diagnostic sink, not a replacement for audit".
"""

MAX_ARGUMENT_NAMES: Final = 32
"""The cardinality bound on ``argument_names``.

Comfortably above the widest tool input model in the tree (``recallatron_remember``'s
is nine fields) and far below anything that would make this array a payload. The
writer truncates to it and the check constraint enforces it.
"""

telemetry_metadata = MetaData(schema=CORE_SCHEMA)
"""This revision's own ``MetaData``; see the module docstring."""


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


SAFETY_CLASSES: Final = tuple(member.value for member in SafetyClass)
"""The declared safety classes, read off the enum rather than retyped.

``audit/records.py`` compares its own ``Literal`` against the DDL's tuple at import
time for the same reason; here the enum is already the single source, so the
constraint is built from it directly and there is no second spelling to drift.
"""


tool_telemetry = Table(
    "tool_telemetry",
    telemetry_metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    # The tool's *registered* name, never the name a caller asked for: the façade
    # emits nothing for a name that is not a tool available to that context, so an
    # unregistered string cannot reach this column.
    Column("tool_name", Text, nullable=False),
    Column("safety_class", Text, nullable=False),
    Column("mode", Text, nullable=False),
    # Returned items, not rows touched. § A11: a ``forget`` count is "one
    # acknowledgment or zero, not erased row count".
    Column("result_count", Integer, nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("outcome", Text, nullable=False),
    # Measured, never stored: see the module docstring.
    Column("query_length", Integer, nullable=True),
    Column("argument_names", ARRAY(Text), nullable=False),
    CheckConstraint(_in("mode", TELEMETRY_MODES), name="tool_telemetry_mode"),
    CheckConstraint(_in("outcome", TELEMETRY_OUTCOMES), name="tool_telemetry_outcome"),
    CheckConstraint(
        _in("safety_class", SAFETY_CLASSES), name="tool_telemetry_safety_class"
    ),
    CheckConstraint(
        "result_count >= 0 AND duration_ms >= 0",
        name="tool_telemetry_measures_are_non_negative",
    ),
    CheckConstraint(
        "query_length IS NULL OR query_length >= 0",
        name="tool_telemetry_query_length_is_non_negative",
    ),
    CheckConstraint(
        f"cardinality(argument_names) <= {MAX_ARGUMENT_NAMES}",
        name="tool_telemetry_argument_names_are_bounded",
    ),
    # § A3: "telemetry order/pruning uses ``(occurred_at,id)``". Both bounds read it —
    # the age purge's range scan and the row-count eviction's oldest-first ordering —
    # and so does the audit collection's newest-first page.
    Index("tool_telemetry_occurred_at_id", "occurred_at", "id"),
)

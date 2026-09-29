"""DDL for revision ``0011_local_evidence``: the enrollment table and the live shape of
``core.evidence_unit`` once 0011 widens its ``producer_kind`` CHECK.

Two ``MetaData`` objects, never merged, the ``runtime_tables.py`` pattern
(``runtime_session_0006`` frozen, ``runtime_session`` live on
``session_policy_metadata``):

- ``enrollment_metadata`` holds only ``core.evidence_enrollment``. 0011 creates it with
  ``create_all``, so that call can create nothing else.
- ``evidence_unit_live_metadata`` holds only :data:`evidence_unit_live`, the shape
  ``core.evidence_unit`` has after 0011. It is never passed to ``create_all`` (the
  table already exists, created by 0010); 0011 swaps the one CHECK in place. Its one
  reader is the test that compares its CHECK text with the database's. Every read and
  write path keeps using ``evidence_tables.evidence_unit``: a CHECK is DDL only.

An enrollment deliberately has no workspace column (the database is the workspace) and
no audience-ceiling column (fixed by construction to ``member``/``account_id``, enforced
in code; a column with one legal value has no reader that could branch on it).
"""

from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from rheo_core.storage.core_tables import CORE_SCHEMA
from rheo_core.storage.evidence_tables import _in, evidence_unit

EVIDENCE_PRODUCER_KINDS_0011: Final = ("rheo_runtime", "claude_code_local")
"""``core.evidence_unit.producer_kind`` as revision ``0011_local_evidence`` widens it.

Frozen at 0011; a later producer gets its own migration. The 0010 tuple
(``evidence_tables.EVIDENCE_PRODUCER_KINDS``) stays as it was, so 0010 still builds
exactly what it built."""
ENROLLMENT_PURPOSES_0011: Final = (
    "respond",
    "follow_up",
    "share_with_referral",
    "internal_analysis",
)
"""The ``ContextPurpose`` values frozen at 0011, in enum order.

A literal rather than a derivation for the reason ``EVIDENCE_PURPOSES`` gives: a new
enum member must not silently change what 0011 creates.
``tests/postgres/test_evidence_enrollment_tables.py`` reds when the two diverge."""
STATE_ACTIVE: Final = "active"
STATE_REVOKED: Final = "revoked"
ENROLLMENT_STATES: Final = (STATE_ACTIVE, STATE_REVOKED)
"""Order is 0011's CHECK text; do not reorder."""
FINGERPRINT_PATTERN: Final = "^[0-9a-f]{64}$"
"""A lowercase hex sha256: the machine key's or the enrolled directory's realpath's."""

enrollment_metadata = MetaData(schema=CORE_SCHEMA)
evidence_unit_live_metadata = MetaData(schema=CORE_SCHEMA)


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


def producer_kind_clause(values: tuple[str, ...]) -> str:
    """The ``evidence_unit_producer_kind`` CHECK body, built by 0010's own helper so
    the clause shape matches what 0010 created."""
    return _in("producer_kind", values)


evidence_enrollment = Table(
    "evidence_enrollment",
    enrollment_metadata,
    # uuid7, minted by the caller; the evidence unit's ``authority_id``.
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("account_id", UUID(as_uuid=True), nullable=False),
    # The control-plane ``access_token.id``; no FK, that is another database.
    Column("token_id", UUID(as_uuid=True), nullable=False),
    Column("machine_fingerprint", Text, nullable=False),
    Column("project_fingerprint", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("created_at", _timestamptz(), nullable=False),
    Column("revoked_at", _timestamptz(), nullable=True),
    UniqueConstraint("token_id", name="evidence_enrollment_token_id"),
    CheckConstraint(
        f"machine_fingerprint ~ '{FINGERPRINT_PATTERN}'",
        name="evidence_enrollment_machine_fingerprint",
    ),
    CheckConstraint(
        f"project_fingerprint ~ '{FINGERPRINT_PATTERN}'",
        name="evidence_enrollment_project_fingerprint",
    ),
    CheckConstraint(
        _in("purpose", ENROLLMENT_PURPOSES_0011), name="evidence_enrollment_purpose"
    ),
    CheckConstraint(_in("state", ENROLLMENT_STATES), name="evidence_enrollment_state"),
    CheckConstraint(
        "(state = 'active') = (revoked_at IS NULL)",
        name="evidence_enrollment_revoked_pairing",
    ),
    # One live enrollment per machine and directory; enroll refuses a second.
    Index(
        "evidence_enrollment_active_pair",
        "machine_fingerprint",
        "project_fingerprint",
        unique=True,
        postgresql_where=text("state = 'active'"),
    ),
)

ENROLLMENT_TABLES: Final[tuple[Table, ...]] = (evidence_enrollment,)
"""The one enrollment table, exactly what revision ``0011_local_evidence`` creates."""


def _live_constraints() -> list[CheckConstraint]:
    """0010's CHECKs carried over by name and text, ``producer_kind`` widened."""
    constraints: list[CheckConstraint] = []
    for constraint in evidence_unit.constraints:
        if not isinstance(constraint, CheckConstraint):
            continue
        body = str(constraint.sqltext)
        if constraint.name == "evidence_unit_producer_kind":
            body = producer_kind_clause(EVIDENCE_PRODUCER_KINDS_0011)
        constraints.append(CheckConstraint(body, name=constraint.name))
    return sorted(constraints, key=lambda constraint: str(constraint.name))


evidence_unit_live = Table(
    "evidence_unit",
    evidence_unit_live_metadata,
    *(column._copy() for column in evidence_unit.columns),
    *_live_constraints(),
)
"""The live shape of ``core.evidence_unit``: 0010's columns and CHECKs, with
``evidence_unit_producer_kind`` widened to :data:`EVIDENCE_PRODUCER_KINDS_0011`.

Named ``evidence_unit`` on its own ``MetaData`` because it is that relation, as
``runtime_session`` is on ``session_policy_metadata``. Never created; code reads and
writes ``evidence_tables.evidence_unit``."""

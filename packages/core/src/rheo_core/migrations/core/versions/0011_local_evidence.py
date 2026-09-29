"""Local evidence: the enrollment table, and ``claude_code_local`` admitted.

Revision ID: 0011_local_evidence
Revises: 0010_evidence_unit

Creates exactly ``core.evidence_enrollment`` from
``rheo_core.storage.evidence_enrollment_tables``'s ``enrollment_metadata``, which holds
that one table, then replaces ``core.evidence_unit``'s ``evidence_unit_producer_kind``
CHECK with one admitting ``EVIDENCE_PRODUCER_KINDS_0011``. The live-shape sibling
``evidence_unit_live_metadata`` is never created here: the table is 0010's.

Downgrade deletes every ``claude_code_local`` evidence row first, since 0010's CHECK
cannot hold them. They are transient pending turns and content-free gaps, the same
reason 0010's own downgrade gives for dropping its whole table, so nothing durable is
lost. It then restores 0010's CHECK from 0010's frozen tuple and drops the enrollment
table. Reversible in code; treat it as one-way in production.
"""

from collections.abc import Sequence

from alembic import op
from rheo_core.storage.evidence_enrollment_tables import (
    EVIDENCE_PRODUCER_KINDS_0011,
    enrollment_metadata,
    producer_kind_clause,
)
from rheo_core.storage.evidence_tables import EVIDENCE_PRODUCER_KINDS
from sqlalchemy import text

revision: str = "0011_local_evidence"
down_revision: str | None = "0010_evidence_unit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRODUCER_KIND_CHECK = "evidence_unit_producer_kind"
_LOCAL_PRODUCER_KIND = "claude_code_local"


def _replace_producer_kind_check(values: tuple[str, ...]) -> None:
    op.drop_constraint(
        _PRODUCER_KIND_CHECK, "evidence_unit", schema="core", type_="check"
    )
    op.create_check_constraint(
        _PRODUCER_KIND_CHECK,
        "evidence_unit",
        producer_kind_clause(values),
        schema="core",
    )


def upgrade() -> None:
    # ``checkfirst=False``: a table already present means this chain is running
    # against a database it does not own, which must fail loudly, never skip.
    enrollment_metadata.create_all(op.get_bind(), checkfirst=False)
    _replace_producer_kind_check(EVIDENCE_PRODUCER_KINDS_0011)


def downgrade() -> None:
    op.get_bind().execute(
        text("DELETE FROM core.evidence_unit WHERE producer_kind = :kind"),
        {"kind": _LOCAL_PRODUCER_KIND},
    )
    _replace_producer_kind_check(EVIDENCE_PRODUCER_KINDS)
    enrollment_metadata.drop_all(op.get_bind(), checkfirst=False)

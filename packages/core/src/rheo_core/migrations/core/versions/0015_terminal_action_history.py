"""Let terminal external-action history outlive its keyed replay envelope.

Revision ID: 0015_terminal_action_history
Revises: 0014_external_actions

Revision 0014's composite foreign key made every external action retain its keyed
envelope forever, including terminal history that the retention sweep is required to
leave behind. Operation name and idempotency key remain on the action as immutable
historical identity; runtime retention explicitly protects pending, executing, and
unresolved actions. Removing this one constraint permits only the intended terminal
envelope deletion and changes no 0014 column or row.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_terminal_action_history"
down_revision: str | None = "0014_external_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "external_action_idempotency_result_fkey"


def upgrade() -> None:
    op.drop_constraint(
        _CONSTRAINT,
        "external_action",
        schema="core",
        type_="foreignkey",
    )


def downgrade() -> None:
    # This intentionally fails if a terminal action has already outlived its envelope:
    # restoring a false reference by deleting history is not a valid downgrade.
    op.create_foreign_key(
        _CONSTRAINT,
        "external_action",
        "idempotency_result",
        ["operation_name", "idempotency_key"],
        ["operation_name", "idempotency_key"],
        source_schema="core",
        referent_schema="core",
    )

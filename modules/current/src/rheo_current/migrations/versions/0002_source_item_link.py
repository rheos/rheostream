"""Frozen DDL for the lasting source-to-item link and its tombstone.

Append-only: it adds one table and changes nothing 0001 created.
"""

from alembic import op

revision = "0002_source_item_link"
down_revision = "0001_current"
branch_labels = None
depends_on = None
STATEMENTS = (
    """CREATE TABLE current.source_item_link (
source_ref TEXT NOT NULL,
item_ref TEXT NOT NULL,
revision INTEGER DEFAULT '1' NOT NULL,
item_state TEXT NOT NULL,
project_ref TEXT,
last_project_ref TEXT,
project_state TEXT NOT NULL,
changed_at TIMESTAMP WITH TIME ZONE NOT NULL,
PRIMARY KEY (source_ref),
CONSTRAINT source_item_link_revision CHECK (revision >= 1),
CONSTRAINT source_item_link_item_state CHECK (item_state in ('available','deleted')),
CONSTRAINT source_item_link_project_state CHECK (project_state in
('never_assigned','linked','cleared','deleted')),
CONSTRAINT source_item_link_live_project CHECK
((project_state = 'linked') = (project_ref IS NOT NULL)),
CONSTRAINT source_item_link_project_history CHECK
(project_state NOT IN ('cleared','deleted') OR last_project_ref IS NOT NULL),
CONSTRAINT source_item_link_never_assigned CHECK
(project_state <> 'never_assigned' OR last_project_ref IS NULL)
)""",
    """CREATE UNIQUE INDEX uq_source_item_link_item
ON current.source_item_link (item_ref)""",
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("forward-only module migrations")

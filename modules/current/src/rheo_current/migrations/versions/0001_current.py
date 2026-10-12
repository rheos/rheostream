"""Frozen initial Current DDL and the default area list."""

from alembic import op

revision = "0001_current"
down_revision = None
branch_labels = None
depends_on = None
STATEMENTS = (
    """CREATE SCHEMA IF NOT EXISTS current""",
    """CREATE TABLE current.area (
key TEXT NOT NULL,
label TEXT NOT NULL,
position INTEGER NOT NULL,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
retired_at TIMESTAMP WITH TIME ZONE,
PRIMARY KEY (key),
CONSTRAINT area_key CHECK (key ~ '^[a-z][a-z0-9_]{0,39}$')
)""",
    """CREATE TABLE current.project (
id UUID NOT NULL,
title TEXT NOT NULL,
description TEXT NOT NULL,
status TEXT NOT NULL,
area TEXT,
due_on DATE,
revision INTEGER DEFAULT '1' NOT NULL,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
PRIMARY KEY (id),
CONSTRAINT project_status CHECK (status in ('active','paused','done')),
FOREIGN KEY(area) REFERENCES current.area (key)
)""",
    """CREATE TABLE current.item (
id UUID NOT NULL,
title TEXT NOT NULL,
body TEXT NOT NULL,
status TEXT NOT NULL,
priority TEXT NOT NULL,
area TEXT,
project_id UUID,
due_on DATE,
snoozed_until TIMESTAMP WITH TIME ZONE,
provenance TEXT NOT NULL,
source_kind TEXT,
source_ref TEXT,
event_count INTEGER DEFAULT '1' NOT NULL,
revision INTEGER DEFAULT '1' NOT NULL,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
closed_at TIMESTAMP WITH TIME ZONE,
PRIMARY KEY (id),
CONSTRAINT item_status CHECK (status in ('open','waiting','snoozed','done')),
CONSTRAINT item_priority CHECK (priority in ('critical','high','normal','low')),
CONSTRAINT item_provenance CHECK (provenance in
('manual','agent','email','alert','handoff')),
CONSTRAINT item_snooze CHECK ((status = 'snoozed') = (snoozed_until IS NOT NULL)),
CONSTRAINT item_closed CHECK ((status = 'done') = (closed_at IS NOT NULL)),
CONSTRAINT item_source_pair CHECK ((source_kind IS NULL) = (source_ref IS NULL)),
CONSTRAINT item_event_count CHECK (event_count >= 1),
FOREIGN KEY(area) REFERENCES current.area (key),
FOREIGN KEY(project_id) REFERENCES current.project (id)
)""",
    """CREATE INDEX ix_item_project ON current.item (project_id)""",
    """CREATE INDEX ix_item_search ON current.item USING gin
(to_tsvector('english'::regconfig, (title || ' '::text) || body))""",
    """CREATE UNIQUE INDEX uq_item_source ON current.item (source_kind, source_ref)
WHERE source_ref IS NOT NULL""",
    """CREATE TABLE current.comment (
id UUID NOT NULL,
item_id UUID NOT NULL,
kind TEXT NOT NULL,
body TEXT NOT NULL,
author_kind TEXT NOT NULL,
author_id UUID,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
PRIMARY KEY (id),
CONSTRAINT comment_kind CHECK (kind in ('comment','system')),
FOREIGN KEY(item_id) REFERENCES current.item (id)
)""",
    """CREATE INDEX ix_comment_item ON current.comment (item_id)""",
    """CREATE INDEX ix_comment_search ON current.comment USING gin
(to_tsvector('english'::regconfig, body))""",
    """CREATE TABLE current.signal (
item_id UUID NOT NULL,
fingerprint TEXT NOT NULL,
seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
CONSTRAINT signal_pkey PRIMARY KEY (item_id, fingerprint),
FOREIGN KEY(item_id) REFERENCES current.item (id)
)""",
    """CREATE TABLE current.dependency (
item_id UUID NOT NULL,
blocker_id UUID NOT NULL,
created_by_kind TEXT NOT NULL,
created_by_id UUID,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
CONSTRAINT dependency_pkey PRIMARY KEY (item_id, blocker_id),
CONSTRAINT dependency_not_self CHECK (item_id <> blocker_id),
FOREIGN KEY(item_id) REFERENCES current.item (id),
FOREIGN KEY(blocker_id) REFERENCES current.item (id)
)""",
    """CREATE INDEX ix_dependency_blocker ON current.dependency (blocker_id)""",
    """INSERT INTO current.area (key, label, position, created_at, updated_at)
VALUES ('work', 'Work', 1, now(), now()),
('commerce', 'Commerce', 2, now(), now()),
('bills', 'Bills', 3, now(), now()),
('peace', 'Peace', 4, now(), now()),
('education', 'Education', 5, now(), now()),
('dev', 'Dev', 6, now(), now()),
('interior', 'Interior', 7, now(), now()),
('foreign', 'Foreign affairs', 8, now(), now())""",
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("forward-only module migrations")

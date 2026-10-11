"""Work items, projects, dependencies and areas, owned by one workspace.

The status invariants are database checks so no write path can leave a half state:
only a snoozed item holds a snooze time, and only a done item holds a close time.
"""

from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    ColumnClause,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    Uuid,
    func,
    literal_column,
    text,
)

metadata = MetaData(schema="current")
ENGLISH: ColumnClause[Any] = literal_column("'english'::regconfig")
SPACE: ColumnClause[Any] = literal_column("' '::text")


def stamp(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


area = Table(
    "area",
    metadata,
    Column("key", Text, primary_key=True),
    Column("label", Text, nullable=False),
    Column("position", Integer, nullable=False),
    stamp("created_at"),
    stamp("updated_at"),
    stamp("retired_at", nullable=True),
    CheckConstraint("key ~ '^[a-z][a-z0-9_]{0,39}$'", name="area_key"),
)
project = Table(
    "project",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("area", Text, ForeignKey("current.area.key")),
    Column("due_on", Date),
    Column("revision", Integer, nullable=False, server_default="1"),
    stamp("created_at"),
    stamp("updated_at"),
    CheckConstraint("status in ('active','paused','done')", name="project_status"),
)
item = Table(
    "item",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("priority", Text, nullable=False),
    Column("area", Text, ForeignKey("current.area.key")),
    Column("project_id", Uuid, ForeignKey("current.project.id")),
    Column("due_on", Date),
    stamp("snoozed_until", nullable=True),
    Column("provenance", Text, nullable=False),
    Column("source_kind", Text),
    Column("source_ref", Text),
    Column("event_count", Integer, nullable=False, server_default="1"),
    Column("revision", Integer, nullable=False, server_default="1"),
    stamp("created_at"),
    stamp("updated_at"),
    stamp("closed_at", nullable=True),
    CheckConstraint(
        "status in ('open','waiting','snoozed','done')", name="item_status"
    ),
    CheckConstraint(
        "priority in ('critical','high','normal','low')", name="item_priority"
    ),
    CheckConstraint(
        "provenance in ('manual','agent','email','alert','handoff')",
        name="item_provenance",
    ),
    CheckConstraint(
        "(status = 'snoozed') = (snoozed_until IS NOT NULL)", name="item_snooze"
    ),
    CheckConstraint("(status = 'done') = (closed_at IS NOT NULL)", name="item_closed"),
    CheckConstraint(
        "(source_kind IS NULL) = (source_ref IS NULL)", name="item_source_pair"
    ),
    CheckConstraint("event_count >= 1", name="item_event_count"),
)
# Dedup spans every stored item, snoozed and done included; deletion removes the row.
Index(
    "uq_item_source",
    item.c.source_kind,
    item.c.source_ref,
    unique=True,
    postgresql_where=text("source_ref IS NOT NULL"),
)
Index("ix_item_project", item.c.project_id)
Index(
    "ix_item_search",
    func.to_tsvector(ENGLISH, item.c.title.op("||")(SPACE).op("||")(item.c.body)),
    postgresql_using="gin",
    # The leading regconfig literal hides the table from SQLAlchemy's inference.
    _table=item,
)
comment = Table(
    "comment",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("item_id", Uuid, ForeignKey("current.item.id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("author_kind", Text, nullable=False),
    Column("author_id", Uuid),
    stamp("created_at"),
    CheckConstraint("kind in ('comment','system')", name="comment_kind"),
)
Index("ix_comment_item", comment.c.item_id)
Index(
    "ix_comment_search",
    func.to_tsvector(ENGLISH, comment.c.body),
    postgresql_using="gin",
    _table=comment,
)
signal = Table(
    "signal",
    metadata,
    Column("item_id", Uuid, ForeignKey("current.item.id"), nullable=False),
    Column("fingerprint", Text, nullable=False),
    stamp("seen_at"),
    PrimaryKeyConstraint("item_id", "fingerprint", name="signal_pkey"),
)
dependency = Table(
    "dependency",
    metadata,
    Column("item_id", Uuid, ForeignKey("current.item.id"), nullable=False),
    Column("blocker_id", Uuid, ForeignKey("current.item.id"), nullable=False),
    Column("created_by_kind", Text, nullable=False),
    Column("created_by_id", Uuid),
    stamp("created_at"),
    PrimaryKeyConstraint("item_id", "blocker_id", name="dependency_pkey"),
    CheckConstraint("item_id <> blocker_id", name="dependency_not_self"),
)
Index("ix_dependency_blocker", dependency.c.blocker_id)

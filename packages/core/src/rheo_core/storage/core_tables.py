"""The six core-owned tables of a workspace database, as SQLAlchemy Core ``Table``
objects in schema ``core``.

Columns are verbatim from ``docs/architecture/storage-and-workspaces.md`` § Composition
and schema versions and § A5. The ``core`` migration chain imports these objects; its
first revision creates exactly these six and is never edited afterwards.

**Deliberately absent** (the hard 0c boundary — a table created here is a table revision
0001 creates, and that revision is frozen). The seven durable-work tables
(``outbox_event``, ``event_delivery``, ``consumer_processed``, ``job``, ``schedule``,
``operation``, ``audit_record``) are declared in ``work_tables.py`` on their own
``MetaData`` and created by revision ``0002_durable_work``, never here. The approval
family (``approval``, ``approval_payload``, ``standing_grant``,
``standing_grant_operation``, ``external_action``) belongs to a later chain step that
owns it, as do the runtime, deletion and export tables. A seventh table in this module
is a boundary violation, not a head start.
"""

from typing import Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    true,
)
from sqlalchemy.dialects.postgresql import UUID

CORE_SCHEMA: Final = "core"
CORE_VERSION_TABLE: Final = "alembic_version_core"

MODULE_STATES: Final = ("installed", "enabled", "disabled", "removed")

core_metadata = MetaData(schema=CORE_SCHEMA)


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


# One row: ``singleton`` is always true, checked, and uniquely indexed, so a second
# insert fails on the index rather than silently producing two compositions.
workspace_composition = Table(
    "workspace_composition",
    core_metadata,
    Column("singleton", Boolean, nullable=False, server_default=true()),
    Column("core_version", Text, nullable=False),
    Column("core_contract_version", Integer, nullable=False),
    Column("updated_at", _timestamptz(), nullable=False),
    CheckConstraint("singleton", name="workspace_composition_singleton"),
    Index("workspace_composition_one_row", "singleton", unique=True),
)

module_state = Table(
    "module_state",
    core_metadata,
    Column("module_id", Text, primary_key=True),
    Column("package_version", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("installed_at", _timestamptz(), nullable=False),
    Column("enabled_at", _timestamptz(), nullable=True),
    Column("disabled_at", _timestamptz(), nullable=True),
    Column("state_detail", Text, nullable=True),
    CheckConstraint(_in("state", MODULE_STATES), name="module_state_state"),
)

# One row per applied migration step; the latest row is the current version. The
# primary key ``(module_id, schema_version)`` is this run's addition to the ratified
# column list: it keeps duplicate history rows out, and forbids recording the same
# step applied twice, which cannot happen before phase 2 because no module exists.
# If a phase-2 re-apply ever needs recording, the key becomes
# ``(module_id, schema_version, applied_at)`` by an appended revision; the ratified
# columns are unchanged either way.
module_schema_version = Table(
    "module_schema_version",
    core_metadata,
    Column("module_id", Text, nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("applied_at", _timestamptz(), nullable=False),
    Column("core_version_at_apply", Text, nullable=False),
    PrimaryKeyConstraint(
        "module_id", "schema_version", name="module_schema_version_pkey"
    ),
)

# ``updated_by`` is null for a row written by provisioning (step 4, from the package
# default) or by the operator, neither of which has an account id.
workspace_setting = Table(
    "workspace_setting",
    core_metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("value_type", Text, nullable=False),
    Column("updated_by", UUID(as_uuid=True), nullable=True),
    Column("updated_at", _timestamptz(), nullable=False),
)

member_setting = Table(
    "member_setting",
    core_metadata,
    Column("account_id", UUID(as_uuid=True), nullable=False),
    Column("key", Text, nullable=False),
    Column("value", Text, nullable=False),
    Column("value_type", Text, nullable=False),
    Column("updated_at", _timestamptz(), nullable=False),
    PrimaryKeyConstraint("account_id", "key", name="member_setting_pkey"),
)

# DDL only in this run: the repository arrives with the first reader or writer.
member_credential = Table(
    "member_credential",
    core_metadata,
    Column("account_id", UUID(as_uuid=True), nullable=False),
    Column("name", Text, nullable=False),
    Column("secret_ref", Text, nullable=False),
    Column("created_at", _timestamptz(), nullable=False),
    PrimaryKeyConstraint("account_id", "name", name="member_credential_pkey"),
)

CORE_TABLES: Final[tuple[Table, ...]] = (
    workspace_composition,
    module_state,
    module_schema_version,
    workspace_setting,
    member_setting,
    member_credential,
)

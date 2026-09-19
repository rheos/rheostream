"""Workspace-database repositories over the ``core`` schema: the composition row, the
module state rows, and the two settings tables.

No ``member_credential`` repository: the table's DDL ships in ``core_tables.py`` and
its repository is deferred to the run that first reads or writes it. ``module_state``
has a reader here (``core.workspace.status`` in C4) and no writer yet: its writers
arrive with module install. ``module_schema_version`` has both — its writer is
``run_module_chain``'s, one row per newly applied module migration step.

Every function takes the caller's ``Connection`` (a ``UnitOfWork.connection``) and
runs inside its transaction; none commits.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Connection, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping

from rheo_core.settings import ValueType
from rheo_core.storage import core_tables as c


def _now() -> datetime:
    return datetime.now(UTC)


# --- composition ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompositionRow:
    core_version: str
    core_contract_version: int
    updated_at: datetime


def write_composition(
    conn: Connection, *, core_version: str, core_contract_version: int
) -> CompositionRow:
    """Insert or update the single composition row."""
    row = CompositionRow(core_version, core_contract_version, _now())
    values = {
        "core_version": row.core_version,
        "core_contract_version": row.core_contract_version,
        "updated_at": row.updated_at,
    }
    statement = (
        pg_insert(c.workspace_composition)
        .values(singleton=True, **values)
        .on_conflict_do_update(
            index_elements=[c.workspace_composition.c.singleton], set_=values
        )
    )
    conn.execute(statement)
    return row


def read_composition(conn: Connection) -> CompositionRow | None:
    found = conn.execute(select(c.workspace_composition)).mappings().first()
    if found is None:
        return None
    return CompositionRow(
        core_version=found["core_version"],
        core_contract_version=found["core_contract_version"],
        updated_at=found["updated_at"],
    )


# --- module state ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModuleStateRow:
    module_id: str
    package_version: str
    state: str
    installed_at: datetime
    enabled_at: datetime | None
    disabled_at: datetime | None
    state_detail: str | None


def _module_state(row: RowMapping) -> ModuleStateRow:
    return ModuleStateRow(
        module_id=row["module_id"],
        package_version=row["package_version"],
        state=row["state"],
        installed_at=row["installed_at"],
        enabled_at=row["enabled_at"],
        disabled_at=row["disabled_at"],
        state_detail=row["state_detail"],
    )


def list_module_states(conn: Connection) -> tuple[ModuleStateRow, ...]:
    """Every module row, by module id. Empty until module install lands (phase 2)."""
    statement = select(c.module_state).order_by(c.module_state.c.module_id)
    return tuple(_module_state(row) for row in conn.execute(statement).mappings())


@dataclass(frozen=True, slots=True)
class ModuleSchemaVersionRow:
    module_id: str
    schema_version: str
    applied_at: datetime
    core_version_at_apply: str


def insert_module_schema_version(
    conn: Connection,
    *,
    module_id: str,
    schema_version: str,
    applied_at: datetime,
    core_version_at_apply: str,
) -> ModuleSchemaVersionRow:
    """Append one applied migration step for ``module_id``.

    Append-only: the table keeps history, so there is no upsert and no update path
    here. A second row for the same ``(module_id, schema_version)`` violates the
    primary key and raises, which is the behaviour wanted — its caller writes one row
    per *newly applied* revision, so a duplicate means the caller miscounted rather
    than that the step ran twice.

    ``applied_at`` and ``core_version_at_apply`` are the caller's, not this function's:
    the caller knows which transaction the step ran in and which ``rheo-core`` applied
    it (``storage.provisioning.core_version()``), and the export-restore path replays
    the values the artifact recorded rather than today's.
    """
    row = ModuleSchemaVersionRow(
        module_id=module_id,
        schema_version=schema_version,
        applied_at=applied_at,
        core_version_at_apply=core_version_at_apply,
    )
    conn.execute(
        insert(c.module_schema_version).values(
            module_id=row.module_id,
            schema_version=row.schema_version,
            applied_at=row.applied_at,
            core_version_at_apply=row.core_version_at_apply,
        )
    )
    return row


def list_module_schema_versions(
    conn: Connection,
) -> tuple[ModuleSchemaVersionRow, ...]:
    """Every applied module step, oldest first (``core.workspace.status``'s reader).

    Ordered by ``applied_at`` then ``module_id``, so two modules' steps interleave by
    when they were applied. :func:`insert_module_schema_version` is its writer.
    """
    statement = select(c.module_schema_version).order_by(
        c.module_schema_version.c.applied_at, c.module_schema_version.c.module_id
    )
    return tuple(
        ModuleSchemaVersionRow(
            module_id=row["module_id"],
            schema_version=row["schema_version"],
            applied_at=row["applied_at"],
            core_version_at_apply=row["core_version_at_apply"],
        )
        for row in conn.execute(statement).mappings()
    )


# --- settings rows --------------------------------------------------------------------


def workspace_settings(conn: Connection) -> dict[str, str]:
    """Every ``core.workspace_setting`` row as ``key -> value`` text."""
    statement = select(c.workspace_setting.c.key, c.workspace_setting.c.value)
    return {str(key): str(value) for key, value in conn.execute(statement)}


def upsert_workspace_setting(
    conn: Connection,
    *,
    key: str,
    value: str,
    value_type: ValueType,
    updated_by: UUID | None,
) -> None:
    """Insert or replace one workspace override row.

    ``value`` is the text encoding from ``encode_text`` (a ``SettingAccepted``'s
    ``encoded``); ``updated_by`` is the acting account, or ``None`` for provisioning
    and the operator.
    """
    values = {
        "value": value,
        "value_type": ValueType(value_type).value,
        "updated_by": updated_by,
        "updated_at": _now(),
    }
    statement = (
        pg_insert(c.workspace_setting)
        .values(key=key, **values)
        .on_conflict_do_update(index_elements=[c.workspace_setting.c.key], set_=values)
    )
    conn.execute(statement)


def insert_workspace_setting_if_absent(
    conn: Connection,
    *,
    key: str,
    value: str,
    value_type: ValueType,
    updated_by: UUID | None,
) -> bool:
    """Insert one workspace row only when no row for ``key`` exists (provisioning
    step 4, which must never overwrite a value a workspace has since set).

    Returns ``True`` when a row was inserted.
    """
    statement = (
        pg_insert(c.workspace_setting)
        .values(
            key=key,
            value=value,
            value_type=ValueType(value_type).value,
            updated_by=updated_by,
            updated_at=_now(),
        )
        .on_conflict_do_nothing(index_elements=[c.workspace_setting.c.key])
        .returning(c.workspace_setting.c.key)
    )
    return conn.execute(statement).first() is not None


def member_settings(conn: Connection, account_id: UUID) -> dict[str, str]:
    """Every ``core.member_setting`` row of ``account_id`` as ``key -> value`` text."""
    statement = select(c.member_setting.c.key, c.member_setting.c.value).where(
        c.member_setting.c.account_id == account_id
    )
    return {str(key): str(value) for key, value in conn.execute(statement)}


def upsert_member_setting(
    conn: Connection,
    *,
    account_id: UUID,
    key: str,
    value: str,
    value_type: ValueType,
) -> None:
    """Insert or replace one member override row for ``account_id``."""
    values = {
        "value": value,
        "value_type": ValueType(value_type).value,
        "updated_at": _now(),
    }
    statement = (
        pg_insert(c.member_setting)
        .values(account_id=account_id, key=key, **values)
        .on_conflict_do_update(
            index_elements=[c.member_setting.c.account_id, c.member_setting.c.key],
            set_=values,
        )
    )
    conn.execute(statement)

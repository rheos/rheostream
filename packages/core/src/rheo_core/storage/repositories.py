"""Workspace-database repositories over the ``core`` schema: the composition row, the
module state rows, the two settings tables, and the schedule writer enable uses.

No ``member_credential`` repository: the table's DDL ships in ``core_tables.py`` and
its repository is deferred to the run that first reads or writes it. ``module_state``
and ``module_schema_version`` each have a reader here (``core.workspace.status`` in C4)
and, from module install, their writers: :func:`insert_module_state`,
:func:`set_module_state` and :func:`insert_module_schema_version`.

**This module is now the only code that inserts into either table (FR 10).** The three
callers are ``core.module.install`` (``modules/operations.py``), a module's migration
chain (``migrations/module_chain.py``) and the export-restore path
(``exports/artifact.py``'s ``_install_modules``); each of them calls a function here
rather than building a statement of its own. No migration inserts into either table —
the core chain only creates them. ``tests/test_module_state_writers.py`` is the static
scan that measures the property, asserted both ways over ``packages/``, ``apps/``,
``modules/``, ``scripts/`` and ``tests/``, with this file as its one declared exception.

Every function takes the caller's ``Connection`` (a ``UnitOfWork.connection``) and
runs inside its transaction; none commits.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Connection, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping

from rheo_core.refs import uuid7
from rheo_core.settings import ValueType
from rheo_core.storage import core_tables as c
from rheo_core.storage import work_tables as w


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
    """Every module row, by module id. Empty in a workspace that installed nothing."""
    statement = select(c.module_state).order_by(c.module_state.c.module_id)
    return tuple(_module_state(row) for row in conn.execute(statement).mappings())


def insert_module_state(
    conn: Connection,
    *,
    module_id: str,
    package_version: str,
    state: str,
    installed_at: datetime,
    enabled_at: datetime | None = None,
    state_detail: str | None = None,
) -> ModuleStateRow:
    """Write the one ``core.module_state`` row a module gets in this workspace.

    **A plain insert, so a second row for one module id raises on the primary key**
    rather than folding into the first. That is deliberate and it is the writer half
    of Decision D: ``core.module.install`` refuses ``module_already_installed`` for a
    module that already has a row *in any state*, and an ``ON CONFLICT DO NOTHING``
    here would turn the race that slipped past that pre-flight check into a silent
    success reporting an install that never happened — with the recorded package
    version possibly a different one from the package this deployment loaded. A caller
    that genuinely wants "insert or leave alone" reads :func:`list_module_states`
    first; ``tests/harness/registry.py``'s ``enable_harness_module`` is the one such
    caller and does exactly that.

    ``installed_at`` is the caller's, not this function's, for the reason
    :func:`insert_module_schema_version` gives: install stamps the instant its own
    transaction ran, and export-restore replays the artifact's.

    ``disabled_at`` is never set here — a row is not born disabled — and
    :func:`set_module_state` is what moves an existing row.
    """
    row = ModuleStateRow(
        module_id=module_id,
        package_version=package_version,
        state=state,
        installed_at=installed_at,
        enabled_at=enabled_at,
        disabled_at=None,
        state_detail=state_detail,
    )
    conn.execute(
        insert(c.module_state).values(
            module_id=row.module_id,
            package_version=row.package_version,
            state=row.state,
            installed_at=row.installed_at,
            enabled_at=row.enabled_at,
            disabled_at=row.disabled_at,
            state_detail=row.state_detail,
        )
    )
    return row


def set_module_state(
    conn: Connection, *, module_id: str, state: str, enabled_at: datetime | None
) -> bool:
    """Move an existing module's row to ``state``; answer whether one was there.

    ``False`` means no row matched, which is a caller's error rather than this
    function's: ``core.module.enable`` refuses a module with no row before it gets
    here, and the return value is what lets it prove that rather than assume it. An
    ``UPDATE`` that matched nothing is otherwise indistinguishable from one that
    worked.

    ``enabled_at`` is a parameter rather than ``_now()`` so the caller stamps the
    instant of its own transaction, and so an export-restore replaying a recorded
    value can use the same writer.
    """
    result = conn.execute(
        update(c.module_state)
        .where(c.module_state.c.module_id == module_id)
        .values(state=state, enabled_at=enabled_at)
    )
    return result.rowcount > 0


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
    it (``storage.provisioning.core_version()``), and the export-restore path — the
    second caller — replays the values the artifact recorded rather than today's.

    Its two callers are ``migrations.module_chain.run_module_chain`` and
    ``exports/artifact.py``'s ``_install_modules``, and it is now the table's only
    writer; see this module's own docstring.
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
    when they were applied. Every row reaches it through
    :func:`insert_module_schema_version`, including the export-restore path's.
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


# --- schedules ------------------------------------------------------------------------


def insert_schedule_if_absent(
    conn: Connection,
    *,
    module_id: str,
    name: str,
    job_kind: str,
    cron: str,
    enabled: bool,
    next_run_at: datetime,
) -> bool:
    """Insert one ``core.schedule`` row unless ``(module_id, name)`` already has one.

    The columns are exactly the ones the only other insert into this table writes —
    ``migrations/core/versions/0006_runtime.py``'s ``core.retention_sweep`` row —
    including the ``id`` this function mints and the ``last_run_at`` that is null
    because a schedule has never run when it is created. ``next_run_at`` is the
    caller's, as every timestamp in this module is: ``0006`` stamps migrate-time plus
    a day, and ``core.module.enable`` stamps its own transaction's.

    **Absence is read rather than enforced by an index, and the shape is worth
    stating rather than hiding.** ``core.schedule`` is keyed on ``id`` alone and
    carries no unique constraint over ``(module_id, name)``, so there is no conflict
    target for the ``ON CONFLICT DO NOTHING`` that
    :func:`insert_workspace_setting_if_absent` uses one screen up. Writing this as a
    single ``INSERT ... WHERE NOT EXISTS`` would read as though it closed the race and
    would not: two transactions whose snapshots both predate either insert find
    nothing either way. What keeps the duplicate unreachable is the caller —
    ``core.module.enable`` refuses a module that is already ``enabled``, so a second
    enable refuses before it reaches here — and closing it properly needs a unique
    index, which is a core migration and a later run's.

    Returns ``True`` when a row was inserted.
    """
    existing = conn.execute(
        select(w.schedule.c.id).where(
            w.schedule.c.module_id == module_id, w.schedule.c.name == name
        )
    ).first()
    if existing is not None:
        return False
    conn.execute(
        insert(w.schedule).values(
            id=uuid7(),
            module_id=module_id,
            name=name,
            job_kind=job_kind,
            cron=cron,
            enabled=enabled,
            last_run_at=None,
            next_run_at=next_run_at,
        )
    )
    return True

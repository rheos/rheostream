"""The finance module through core's real install, schedule ticker and worker.

Seams under test: ``core.module.install``/``enable`` through the harness's loading
recipe, ``run_module_chain`` on a workspace database, ``run_due_schedules`` and one
``visit_workspace`` over the real job queue. Values are synthetic; the layout lives
under the session's temporary data root.

- D1: install records one ``core.module_schema_version`` row per revision, oldest
  first, the last one the chain head.
- D2: after the chain (and after the install check) the transaction runs as the session
  user again: ``env.py`` reset the role.
- D3: everything in schema ``finance`` belongs to ``finance_owner``; nothing outside it
  belongs to either finance role, save the TOAST tables (and their indexes) of
  finance's own tables, which Postgres gives the parent table's owner.
- D4: neither role has a privilege outside ``finance`` beyond USAGE on the system
  schemas and ``public``, each privilege probed on its own relkinds; the positive
  controls hold.
- D5: enable writes the ``tick`` row; a due visit enqueues nothing until the workspace
  itself sets ``finance.jobs.enabled``, and then one ``finance.tick``.
- D6: one worker visit runs the tick over an empty layout: the job succeeds, the tick
  is recorded, and the next workspace session is the session user.
- D7: a database at a known revision other than the code's head fails the job with
  the fixed ``schema_behind`` code alone.
- D8: no ``finance.*`` name reaches core's operation or tool registries.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import pytest
from alembic.script import ScriptDirectory
from conftest import ClusterSession
from harness.modules import (
    LoadedSurfaces,
    _advancing_clock,
    chain_head,
    install_and_enable_module,
    loaded_probe_modules,
)
from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.events import ConsumerRegistry
from rheo_core.migrations.module_chain import run_module_chain
from rheo_core.operations import register_core_operations
from rheo_core.settings import resolver as resolver_module
from rheo_core.settings.schema import REGISTRY, SettingsRegistry, ValueType, encode_text
from rheo_core.storage import core_tables, work_tables
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work.jobs import enqueue_job
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_core.work.schedules import run_due_schedules
from rheo_finance import db
from rheo_finance.jobs import tick as tick_module
from rheo_finance.wiring.manifest import JOBS_ENABLED_KEY, MANIFEST, install_check
from rheo_finance.wiring.tick_job import TICK_JOB_KIND, TickPayload, finance_tick_job
from sqlalchemy import Connection, Engine, select, text

pytestmark = pytest.mark.postgres

ROLES: Final = (db.OWNER_ROLE, db.READER_ROLE)
SYSTEM_SCHEMAS: Final = frozenset(
    {"finance", "pg_catalog", "information_schema", "public"}
)
INTERVAL: Final = timedelta(seconds=900)
# A TOAST table and its index sit in ``pg_toast`` and belong to their parent table's
# owner. One counts as finance's own only when its parent table is in ``finance``:
# the parent's ``reltoastrelid`` names the TOAST table, and ``pg_index`` maps the
# TOAST index to that table. Over ``pg_class c`` joined to ``pg_namespace n``.
_TOAST_OF_FINANCE: Final = (
    "n.nspname = 'pg_toast' AND EXISTS ("
    "SELECT 1 FROM pg_class parent"
    " JOIN pg_namespace pn ON pn.oid = parent.relnamespace"
    " WHERE pn.nspname = 'finance' AND parent.reltoastrelid IN ("
    "c.oid, (SELECT i.indrelid FROM pg_index i WHERE i.indexrelid = c.oid)))"
)


@dataclass
class Finance:
    cluster: ClusterSession
    workspace: UUID
    operator: WorkspaceContext
    surfaces: LoadedSurfaces

    def engine(self) -> Engine:
        row = self.cluster.registry_row(self.workspace)
        return self.cluster.backend.pools.engine_for(row.database_name)


@pytest.fixture
def finance_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finance's keys resolvable by the ticker and the settings writer, for the test
    only: the harness keeps a fixture module's keys out of the process registry."""
    registry = SettingsRegistry()
    for spec in REGISTRY.specs():
        registry.register(spec, origin=REGISTRY.origin_of(spec.key))
    for spec in MANIFEST.configuration_schema:
        if spec.key not in {each.key for each in REGISTRY.specs()}:
            registry.register(spec, origin="finance")
    monkeypatch.setattr(resolver_module, "REGISTRY", registry)


@pytest.fixture
def finance(
    cluster: ClusterSession,
    workspace: UUID,
    monkeypatch: pytest.MonkeyPatch,
    finance_keys: None,
) -> Iterator[Finance]:
    with loaded_probe_modules(monkeypatch, "finance") as surfaces:
        register_core_operations()
        register_core_operations(surfaces.operations)
        operator = context_for_operator(workspace)
        assert isinstance(operator, WorkspaceContext)
        install_and_enable_module(cluster.backend, operator, workspace, "finance")
        yield Finance(cluster, workspace, operator, surfaces)


def _chain() -> list[str]:
    script = ScriptDirectory.from_config(db.alembic_config())
    return [each.revision for each in reversed(list(script.walk_revisions()))]


def _session_user_again(connection: Connection) -> bool:
    return bool(
        connection.execute(text("SELECT current_user = session_user")).scalar_one()
    )


# --- install -------------------------------------------------------------------------


def test_install_records_every_revision_oldest_first(finance: Finance) -> None:
    table = core_tables.module_schema_version
    with finance.engine().connect() as connection:
        rows = connection.execute(
            select(table.c.schema_version)
            .where(table.c.module_id == "finance")
            # Deterministic even if two stamps tie: the zero-padded revision ids sort
            # in chain order, so the tie-break never reorders the chain.
            .order_by(table.c.applied_at, table.c.schema_version)
        ).scalars()
        recorded = list(rows)
    assert recorded == _chain()
    assert recorded == sorted(recorded)
    assert recorded[-1] == chain_head(MANIFEST) == db.head_revision()


def test_the_chain_and_the_install_check_leave_the_session_user(
    cluster: ClusterSession, workspace: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    with loaded_probe_modules(monkeypatch, "finance"):
        operator = context_for_operator(workspace)
        assert isinstance(operator, WorkspaceContext)
        database = cluster.registry_row(workspace).database_name
        with open_unit_of_work(operator) as uow:
            run_module_chain(
                uow.connection,
                MANIFEST,
                expected_database=database,
                core_version="test",
            )
            assert _session_user_again(uow.connection)
            install_check(uow)
            assert _session_user_again(uow.connection)
            # Not committed: the unit of work rolls the chain back.


def test_everything_in_finance_belongs_to_the_owner(finance: Finance) -> None:
    with finance.engine().connect() as connection:
        schema_owner = connection.execute(
            text(
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace"
                " WHERE nspname = 'finance'"
            )
        ).scalar_one()
        assert schema_owner == db.OWNER_ROLE
        not_owner = connection.execute(
            text(
                "SELECT 'class ' || c.relname FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'finance'"
                " AND pg_get_userbyid(c.relowner) <> :owner"
                " UNION ALL SELECT 'proc ' || p.proname FROM pg_proc p"
                " JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'finance'"
                " AND pg_get_userbyid(p.proowner) <> :owner"
                " UNION ALL SELECT 'type ' || t.typname FROM pg_type t"
                " JOIN pg_namespace n ON n.oid = t.typnamespace"
                " WHERE n.nspname = 'finance'"
                " AND pg_get_userbyid(t.typowner) <> :owner"
            ),
            {"owner": db.OWNER_ROLE},
        ).scalars()
        assert list(not_owner) == []
        outside = connection.execute(
            text(
                "SELECT 'class ' || n.nspname || '.' || c.relname FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname <> 'finance'"
                " AND pg_get_userbyid(c.relowner) IN (:owner, :reader)"
                f" AND NOT ({_TOAST_OF_FINANCE})"
                " UNION ALL SELECT 'proc ' || p.proname FROM pg_proc p"
                " JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname <> 'finance'"
                " AND pg_get_userbyid(p.proowner) IN (:owner, :reader)"
                " UNION ALL SELECT 'type ' || t.typname FROM pg_type t"
                " JOIN pg_namespace n ON n.oid = t.typnamespace"
                " WHERE n.nspname <> 'finance'"
                " AND pg_get_userbyid(t.typowner) IN (:owner, :reader)"
                " UNION ALL SELECT 'schema ' || nspname FROM pg_namespace"
                " WHERE nspname <> 'finance'"
                " AND pg_get_userbyid(nspowner) IN (:owner, :reader)"
            ),
            {"owner": db.OWNER_ROLE, "reader": db.READER_ROLE},
        ).scalars()
        assert list(outside) == []
        # The controls: the schema holds objects to check, and the TOAST exception
        # matched relations the owner holds, so it is not vacuous.
        tables = connection.execute(
            text("SELECT count(*) FROM pg_tables WHERE schemaname = 'finance'")
        ).scalar_one()
        assert tables > 0
        toasted = connection.execute(
            text(
                "SELECT count(*) FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                f" WHERE pg_get_userbyid(c.relowner) = :owner AND {_TOAST_OF_FINANCE}"
            ),
            {"owner": db.OWNER_ROLE},
        ).scalar_one()
        assert toasted > 0


@pytest.mark.parametrize("role", ROLES)
def test_neither_role_reaches_outside_finance(finance: Finance, role: str) -> None:
    with finance.engine().connect() as connection:
        # Each privilege is probed only on the relkinds it applies to. SQL does not
        # order the conditions of a WHERE, so the relkind test and the probe sit in
        # one CASE, which does: a probe never sees a relation of the wrong kind.
        relations = connection.execute(
            text(
                "SELECT n.nspname || '.' || c.relname FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname NOT IN"
                " ('finance', 'pg_catalog', 'information_schema', 'pg_toast')"
                " AND CASE WHEN c.relkind IN ('r', 'p', 'v', 'm', 'f')"
                " THEN has_table_privilege(:role, c.oid,"
                " 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')"
                " WHEN c.relkind = 'S'"
                " THEN has_sequence_privilege(:role, c.oid, 'USAGE, SELECT, UPDATE')"
                " ELSE false END"
            ),
            {"role": role},
        ).scalars()
        assert list(relations) == []
        # The control: the sweep reached relations of both kinds outside finance.
        kinds = set(
            connection.execute(
                text(
                    "SELECT DISTINCT c.relkind FROM pg_class c"
                    " JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname NOT IN"
                    " ('finance', 'pg_catalog', 'information_schema', 'pg_toast')"
                    " AND c.relkind IN ('r', 'S')"
                )
            ).scalars()
        )
        assert kinds == {"r", "S"}
        creatable = connection.execute(
            text(
                "SELECT nspname FROM pg_namespace WHERE nspname <> 'finance'"
                " AND has_schema_privilege(:role, oid, 'CREATE')"
            ),
            {"role": role},
        ).scalars()
        assert list(creatable) == []
        usable = set(
            connection.execute(
                text(
                    "SELECT nspname FROM pg_namespace"
                    " WHERE has_schema_privilege(:role, oid, 'USAGE')"
                ),
                {"role": role},
            ).scalars()
        )
        assert "finance" in usable and usable <= SYSTEM_SCHEMAS
        # The positive controls.
        reader_reads = connection.execute(
            text("SELECT has_table_privilege(:reader, 'finance.report_job', 'SELECT')"),
            {"reader": db.READER_ROLE},
        ).scalar_one()
        owner_writes = connection.execute(
            text("SELECT has_table_privilege(:owner, 'finance.decision', 'INSERT')"),
            {"owner": db.OWNER_ROLE},
        ).scalar_one()
        assert reader_reads is True and owner_writes is True


# --- the schedule and its gate --------------------------------------------------------


def _tick_jobs(connection: Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        select(work_tables.job.c.input).where(work_tables.job.c.kind == TICK_JOB_KIND)
    ).scalars()
    return list(rows)


def _open_gate(connection: Connection) -> None:
    spec = next(
        each for each in MANIFEST.configuration_schema if each.key == JOBS_ENABLED_KEY
    )
    upsert_workspace_setting(
        connection,
        key=JOBS_ENABLED_KEY,
        value=encode_text(spec, True),
        value_type=ValueType.BOOL,
        updated_by=None,
    )


def test_the_tick_enqueues_only_once_the_workspace_opens_its_gate(
    finance: Finance,
) -> None:
    schedule = work_tables.schedule
    with finance.engine().begin() as connection:
        row = connection.execute(
            select(schedule).where(
                schedule.c.module_id == "finance", schedule.c.name == "tick"
            )
        ).one()
        assert row.enabled is True and row.job_kind == TICK_JOB_KIND
        due = max(row.next_run_at, datetime.now(UTC))
        run_due_schedules(connection, workspace_id=finance.workspace, now=due)
        assert _tick_jobs(connection) == []
        advanced = connection.execute(
            select(schedule.c.next_run_at).where(schedule.c.id == row.id)
        ).scalar_one()
        assert advanced == due + INTERVAL
        _open_gate(connection)
        run_due_schedules(connection, workspace_id=finance.workspace, now=advanced)
        assert _tick_jobs(connection) == [{"workspace_id": str(finance.workspace)}]


# --- the tick as a worker job ---------------------------------------------------


def _layout(workspace: UUID) -> db.FinanceLayout:
    layout = db.layout_under(workspace_dir_for(workspace, Purpose.UPLOADS))
    for directory in layout.directories:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return layout


def _visit(finance: Finance) -> dict[str, object]:
    with finance.engine().begin() as connection:
        _open_gate(connection)
        job_id = enqueue_job(
            connection,
            kind=TICK_JOB_KIND,
            payload={"workspace_id": str(finance.workspace)},
            now=datetime.now(UTC),
            max_attempts=3,
        )
    kinds = JobKindRegistry()
    kinds.register(TICK_JOB_KIND, TickPayload, finance_tick_job)
    visit_workspace(
        DueWorkspace(workspace_id=finance.workspace, observed_due_at=None),
        kinds=kinds,
        consumers=ConsumerRegistry(),
        backend=finance.cluster.backend,
        owner="finance-tick-test",
        clock=_advancing_clock(),
        jitter=None,
    )
    with finance.engine().connect() as connection:
        job = connection.execute(
            select(work_tables.job.c.state, work_tables.job.c.last_error).where(
                work_tables.job.c.id == job_id
            )
        ).one()
    return {"state": job.state, "last_error": job.last_error}


def test_one_visit_runs_the_tick_and_returns_the_session_user(
    finance: Finance,
) -> None:
    _layout(finance.workspace)
    assert _visit(finance) == {"state": "succeeded", "last_error": None}
    with finance.engine().connect() as connection:
        ticks = connection.execute(
            text("SELECT count(*) FROM finance.job_run WHERE job = 'finance.tick'")
        ).scalar_one()
    assert ticks >= 1
    with open_unit_of_work(finance.operator) as uow:
        assert _session_user_again(uow.connection)


def test_a_known_revision_that_is_not_the_head_fails_with_the_behind_code_alone(
    finance: Finance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick calls any known revision other than the code's head ``schema_behind``
    (``jobs/tick.py``). The database stays at the real head and the tick is told the
    head is the revision before it: rewinding the version table instead would stop at
    core's readiness check (``schema_version_mismatch``) before the tick runs."""
    _layout(finance.workspace)
    chain = _chain()
    with finance.engine().connect() as connection:
        recorded = connection.execute(
            text(f"SELECT version_num FROM {db.SCHEMA}.{db.VERSION_TABLE}")
        ).scalar_one()
    assert recorded == chain[-1]
    told = chain[-2]
    assert told in db.code_revisions() and told != recorded
    monkeypatch.setattr(tick_module, "head_revision", lambda: told)
    outcome = _visit(finance)
    assert outcome["state"] != "succeeded"
    assert outcome["last_error"] == "finance_schema_behind"


# --- package sets ---------------------------------------------------------------


def test_no_finance_name_reaches_core_registries(finance: Finance) -> None:
    named = finance.surfaces.operations.names() | finance.surfaces.tools.names()
    assert not {name for name in named if name.startswith("finance")}
    # The control: core's own operations are registered, so the registry is not empty.
    assert any(name.startswith("core.") for name in finance.surfaces.operations.names())

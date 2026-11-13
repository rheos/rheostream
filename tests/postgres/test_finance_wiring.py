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
- D8 (RS-6d D3): exactly the eight opt-in reads and their tools reach core's
  registries, each registration ``explicit_only`` and owner-only, each output with
  nothing restricted or unrenderable under core's own walks.
- RS-6d D4: an owner's session calls ``finance_status`` and ``finance_owed`` through
  the MCP facade over the loaded registries; the owed ``model_view`` has none of its
  restricted totals, and no audit row is written.
- RS-6d D5: in one open core unit of work the adapter runs a synthetic reader spec as
  ``finance_reader`` and leaves the transaction as the session user.
- RS-6d D6: the operator context is refused ``role_not_permitted``.

The long steps renew the lease (#546, ADR-0027), over a filing step that hashes four
archive files while the worker's clock moves 25 s per file (100 s in all, longer than
the 60 s lease) and a thief calls ``acquire_lease`` after each hash:

- D9: the thief never gets the job; it succeeds on its first attempt.
- D10: right after each file's beat at least 40 s of lease remain, and after each
  25 s step the lease is still ahead of the clock.
- D11: a thief that takes the lease once it lapses makes the next file's beat raise
  ``JobLeaseLost``: no document and no ``job_run`` row commit, and the job stays
  leased to the thief, unfinished.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final
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
from pydantic import BaseModel, ConfigDict
from rheo_app_mcp.tools import call_tool
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.boundary.factories import context_from_session
from rheo_core.events import ConsumerRegistry
from rheo_core.migrations.module_chain import run_module_chain
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.refusals import ROLE_NOT_PERMITTED
from rheo_core.redaction.tiers import restricted_fields, unrenderable_paths
from rheo_core.sessions import create_session, mint_host_secret, switch_workspace
from rheo_core.settings import resolver as resolver_module
from rheo_core.settings.schema import REGISTRY, SettingsRegistry, ValueType, encode_text
from rheo_core.storage import core_tables, work_tables
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work.jobs import acquire_lease, enqueue_job
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import HEARTBEAT_SECONDS, LEASE_SECONDS, visit_workspace
from rheo_core.work.schedules import run_due_schedules
from rheo_finance import db
from rheo_finance.contracts import Role as FinanceRole
from rheo_finance.contracts import SafetyClass as FinanceSafetyClass
from rheo_finance.contracts import Tiered
from rheo_finance.documents import registry as documents_registry
from rheo_finance.jobs import registry as job_registry
from rheo_finance.jobs import tick as tick_module
from rheo_finance.operations import OperationSpec
from rheo_finance.tier import Tier
from rheo_finance.wiring import reads
from rheo_finance.wiring.manifest import JOBS_ENABLED_KEY, MANIFEST, install_check
from rheo_finance.wiring.tick_job import TICK_JOB_KIND, TickPayload, finance_tick_job
from sqlalchemy import Connection, Engine, Row, func, select, text

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


def _enqueue(finance: Finance) -> UUID:
    with finance.engine().begin() as connection:
        _open_gate(connection)
        return enqueue_job(
            connection,
            kind=TICK_JOB_KIND,
            payload={"workspace_id": str(finance.workspace)},
            now=datetime.now(UTC),
            max_attempts=3,
        )


def _drain(finance: Finance, clock: Callable[[], datetime]) -> None:
    kinds = JobKindRegistry()
    kinds.register(TICK_JOB_KIND, TickPayload, finance_tick_job)
    visit_workspace(
        DueWorkspace(workspace_id=finance.workspace, observed_due_at=None),
        kinds=kinds,
        consumers=ConsumerRegistry(),
        backend=finance.cluster.backend,
        owner="finance-tick-test",
        clock=clock,
        jitter=None,
    )


def _visit(finance: Finance) -> dict[str, object]:
    job_id = _enqueue(finance)
    _drain(finance, _advancing_clock())
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


# --- #546: the long steps renew the lease -----------------------------------------

THIEF: Final = "finance-thief-test"
STEP: Final = timedelta(seconds=25)
FILES: Final = 4


@dataclass
class _Probe:
    """One hash in the archive walk: the lease just after that file's beat, the
    clock after the step's advance, the lease then and what the thief got."""

    beaten_until: datetime
    beaten_at: datetime
    at: datetime
    until: datetime
    stolen: object


class _Clock:
    """The worker's clock, moved only by the walk's hash wrapper."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC) + timedelta(seconds=1)
        self.start = self.now

    def __call__(self) -> datetime:
        return self.now


def _lease_until(finance: Finance, job_id: UUID) -> datetime:
    with finance.engine().connect() as connection:
        until = connection.execute(
            select(work_tables.job.c.lease_until).where(work_tables.job.c.id == job_id)
        ).scalar_one()
    assert isinstance(until, datetime)
    return until


def _job_row(finance: Finance, job_id: UUID) -> Row[Any]:
    job = work_tables.job
    with finance.engine().connect() as connection:
        return connection.execute(
            select(
                job.c.state,
                job.c.last_error,
                job.c.lease_owner,
                job.c.attempts,
                job.c.finished_at,
            ).where(job.c.id == job_id)
        ).one()


def _walk_under_a_thief(
    finance: Finance, monkeypatch: pytest.MonkeyPatch, *, steal_on: int | None
) -> tuple[UUID, _Clock, list[_Probe]]:
    """Run the tick with only the filing job over ``FILES`` archive files. Each hash
    moves the clock ``STEP`` on; then a thief tries for the lease, at the clock, or,
    on hash ``steal_on``, just after the lease runs out."""
    layout = _layout(finance.workspace)
    for n in range(FILES):
        target = layout.archive / "alder" / f"2055-0{n + 1}-12.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"Sample statement {n}\n".encode())
    monkeypatch.setattr(
        tick_module,
        "JOBS",
        {"finance.file_downloads": job_registry.JOBS["finance.file_downloads"]},
    )
    clock = _Clock()
    job_id = _enqueue(finance)
    probes: list[_Probe] = []
    hashed = documents_registry._sha256

    def sha256(path: Path) -> str:
        beaten_at = clock.now
        beaten_until = _lease_until(finance, job_id)
        clock.now += STEP
        at = clock.now
        if steal_on == len(probes) + 1:
            at = _lease_until(finance, job_id) + timedelta(seconds=1)
        with finance.engine().begin() as connection:
            stolen = acquire_lease(
                connection, owner=THIEF, now=at, lease_seconds=LEASE_SECONDS
            )
        probes.append(
            _Probe(beaten_until, beaten_at, at, _lease_until(finance, job_id), stolen)
        )
        return hashed(path)

    monkeypatch.setattr(documents_registry, "_sha256", sha256)
    _drain(finance, clock)
    return job_id, clock, probes


def test_a_long_filing_step_keeps_its_lease_from_a_second_claimant(
    finance: Finance, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id, _clock, probes = _walk_under_a_thief(finance, monkeypatch, steal_on=None)
    assert len(probes) == FILES
    assert [probe.stolen for probe in probes] == [None] * FILES
    row = _job_row(finance, job_id)
    assert (row.state, row.last_error, row.lease_owner, row.attempts) == (
        "succeeded",
        None,
        None,
        1,
    )
    with finance.engine().connect() as connection:
        documents = connection.execute(
            text("SELECT count(*) FROM finance.document")
        ).scalar_one()
    assert documents == FILES


def test_each_beat_renews_the_lease_through_a_step_longer_than_it(
    finance: Finance, monkeypatch: pytest.MonkeyPatch
) -> None:
    _job_id, clock, probes = _walk_under_a_thief(finance, monkeypatch, steal_on=None)
    assert len(probes) == FILES
    floor = timedelta(seconds=LEASE_SECONDS - HEARTBEAT_SECONDS)
    for probe in probes:
        # Right after the file's beat at least 40 s remain; after the 25 s step the
        # lease is still ahead of the clock.
        assert probe.beaten_until >= probe.beaten_at + floor
        assert probe.until > probe.at
    assert probes[-1].until > probes[0].beaten_until
    assert clock.now - clock.start == STEP * FILES
    assert clock.now - clock.start > timedelta(seconds=LEASE_SECONDS)


def test_a_lease_lost_mid_step_commits_nothing_and_leaves_the_thief_its_job(
    finance: Finance, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id, _clock, probes = _walk_under_a_thief(finance, monkeypatch, steal_on=2)
    # The second hash let the thief in; the third file's beat raised, so no third
    # hash ran.
    assert len(probes) == 2
    assert probes[0].stolen is None
    assert probes[1].stolen is not None
    row = _job_row(finance, job_id)
    assert (row.state, row.lease_owner, row.finished_at) == ("leased", THIEF, None)
    with finance.engine().connect() as connection:
        documents = connection.execute(
            text("SELECT count(*) FROM finance.document")
        ).scalar_one()
        runs = connection.execute(
            text(
                "SELECT count(*) FROM finance.job_run"
                " WHERE job IN ('finance.file_downloads', 'finance.tick')"
            )
        ).scalar_one()
    assert (documents, runs) == (0, 0)


# --- package sets ---------------------------------------------------------------


FINANCE_READS: Final = frozenset(
    {
        "finance.coverage.get",
        "finance.document.list",
        "finance.openitem.list",
        "finance.owed.list",
        "finance.owed.trace",
        "finance.sink.state",
        "finance.status.get",
        "finance.txn.list",
    }
)
FINANCE_TOOLS: Final = frozenset(
    {
        "finance_coverage",
        "finance_documents",
        "finance_open_items",
        "finance_owed",
        "finance_trace",
        "finance_sinks",
        "finance_status",
        "finance_lines",
    }
)
SESSION_HOST: Final = "example.test"


def test_only_the_eight_opt_in_reads_reach_core_registries(finance: Finance) -> None:
    """D8 (RS-6d D3): exactly the eight reads and their tools, each registration
    ``explicit_only``, each output with nothing restricted or unrenderable."""
    operations = finance.surfaces.operations
    names = {name for name in operations.names() if name.startswith("finance")}
    tools = {
        name for name in finance.surfaces.tools.names() if name.startswith("finance")
    }
    assert names == FINANCE_READS
    assert tools == FINANCE_TOOLS
    for name in names:
        registered = operations.lookup(name)
        assert registered is not None and registered.explicit_only, name
        assert registered.declaration.roles == frozenset({Role.OWNER}), name
        output = registered.declaration.output
        assert restricted_fields(output) == (), name
        assert unrenderable_paths(output) == (), name
    # The control: core's own operations are registered, and are not opt-in.
    core_status = operations.lookup("core.workspace.status")
    assert core_status is not None and not core_status.explicit_only


# --- the reads through core (RS-6d) ---------------------------------------------


def _owner_session(workspace: UUID, account: UUID) -> WorkspaceContext:
    session_row = create_session(account)
    secret = mint_host_secret(session_row.id, SESSION_HOST)
    assert switch_workspace(session_row.id, workspace) is None
    ctx = context_from_session(secret, SESSION_HOST)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _audit_rows(finance: Finance) -> int:
    with finance.engine().connect() as connection:
        return int(
            connection.execute(
                select(func.count()).select_from(work_tables.audit_record)
            ).scalar_one()
        )


def test_an_owner_reads_through_the_tools_and_no_restricted_field_reaches_the_model(
    finance: Finance, owner_account_id: UUID
) -> None:
    """D4: ``finance_status`` and ``finance_owed`` succeed for the owner's session over
    the loaded registries; the owed view has none of its restricted totals, and READ
    writes no audit row."""
    _layout(finance.workspace)
    ctx = _owner_session(finance.workspace, owner_account_id)
    assert "finance" in ctx.enabled_modules
    before = _audit_rows(finance)
    views: dict[str, object] = {}
    for tool in ("finance_status", "finance_owed"):
        outcome = call_tool(
            ctx,
            tool,
            {},
            consumers=None,
            tools=finance.surfaces.tools,
            registry=finance.surfaces.operations,
        )
        assert outcome.ok, outcome
        assert outcome.model_view is not None and not outcome.model_view.withheld
        views[tool] = outcome.model_view.value
    owed = views["finance_owed"]
    assert isinstance(owed, dict)
    assert {"as_of", "rows", "credits", "gaps"} <= set(owed)  # the control
    assert not {"due_in_window", "overdue", "carried", "headline"} & set(owed)
    assert "line" not in owed["credits"]
    assert _audit_rows(finance) == before


class _WhoIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _WhoOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    user: Annotated[str, Tiered(Tier.PUBLIC)]


def _whoami(_ctx: object, uow: Any, _data: _WhoIn) -> _WhoOut:
    return _WhoOut(user=str(uow.scalar(text("SELECT current_user"))))


def test_the_adapter_reads_as_finance_reader_and_hands_back_the_session_user(
    finance: Finance,
) -> None:
    """D5: in one open core unit of work, a synthetic reader spec's handler runs as
    ``finance_reader``, and the same transaction is the session user after it."""
    _layout(finance.workspace)
    spec = OperationSpec(
        name="finance.probe.whoami",
        input_model=_WhoIn,
        output_model=_WhoOut,
        handler=_whoami,
        safety_class=FinanceSafetyClass.READ,
        roles=frozenset({FinanceRole.OWNER}),
        connection="reader",
        description="A synthetic probe of the role a read runs as.",
    )
    handle = reads.read_handler(spec)
    with open_unit_of_work(finance.operator) as uow:
        assert _session_user_again(uow.connection)  # the control
        out = handle(finance.operator, uow, _WhoIn())
        assert out.model_dump() == {"user": db.READER_ROLE}
        assert _session_user_again(uow.connection)


def test_the_operator_cannot_dispatch_a_finance_read(finance: Finance) -> None:
    """D6: the reads are the owner's alone; the operator context (role ``operator``)
    is refused before any handler runs."""
    _layout(finance.workspace)
    outcome = dispatch(
        finance.operator,
        "finance.status.get",
        {},
        registry=finance.surfaces.operations,
    )
    assert outcome.state == ROLE_NOT_PERMITTED, outcome

"""The test-suite environment contract: the profile, the cluster, the session control
database, and the tracked-drop teardown.

**The suite bridges the test cluster into the production settings and secret path.
Nothing here injects a DSN.** The backend reaches the cluster the way production does:
``storage.cluster_dsn_ref`` -> ``SecretStore`` -> ``EnvBackend`` ->
``RHEO_CLUSTER_DSN``.
That settings-to-secret-scope path is what criterion 17's foundation (B9) is about, and
an injected ``PostgresBackend(dsn=...)`` would leave it with zero coverage. The bridge
is four environment variables set below, **before any ``rheo_core`` import**:

- ``RHEO_PROFILE=test``, then asserted against the resolved profile, so
  ``origin = test_harness`` registrations are accepted locally exactly as in CI.
- ``RHEO_CLUSTER_DSN`` from ``RHEO_TEST_CLUSTER_DSN`` (default: the compose
  placeholder), otherwise the storage scope raises ``secret_missing`` in CI.
- ``RHEO__storage__control_database`` = this session's ``rheo_control_test_<12 hex>``,
  otherwise the CLI tests drive ``rheo migrate`` / ``workspace create`` against the
  developer's real ``rheo_control`` and leak a ``ws_*`` database per run.
- ``RHEO_DATA_ROOT`` = a per-session temporary directory, otherwise
  ``validate_data_root`` writes into the developer's real application-data directory.

When the cluster is unreachable the session **fails fast** (``pytest.exit``) naming the
remedy; it never skips, because a skipped ``postgres`` marker passes CI vacuously. The
session records every database it creates and drops exactly those (never a ``ws_%``
pattern): each test's databases when that test finishes, and everything left at
session teardown as a backstop. It refuses to run at all unless the **resolved**
``storage.control_database`` starts with ``rheo_control_test_``.

**Leaks from killed sessions (#191).** A session stopped by a timeout, a 429 or
Ctrl-C never reaches teardown. So each session starts by reaping ``ws_*`` databases
older than ``RHEO_TEST_REAP_AFTER_HOURS`` (default 6; ``0`` or ``off`` turns it off)
that have no connection in ``pg_stat_activity``. The drop is a plain ``DROP
DATABASE``, never ``FORCE``, so a database some other session still has open refuses
and survives. See :func:`reap_stale_workspace_databases`.
"""

import atexit
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.pool import NullPool

DEFAULT_TEST_CLUSTER_DSN = "postgresql://rheo:rheo_dev_only@localhost:5432/postgres"
CONTROL_TEST_PREFIX = "rheo_control_test_"
WORKSPACE_DATABASE_PREFIX = "ws_"
REMEDY = "run `make up`, or set RHEO_TEST_CLUSTER_DSN to a reachable cluster"
REAP_AFTER_ENV = "RHEO_TEST_REAP_AFTER_HOURS"
DEFAULT_REAP_AFTER_HOURS = 6.0
#: Exactly what ``database_name_for`` produces. The reaper only ever considers names
#: of this shape, so a hand-made ``ws_scratch`` is never touched either.
REAPABLE_WORKSPACE_NAME = re.compile(r"ws_[0-9a-f]{32}")

_test_cluster_dsn = (
    os.environ.get("RHEO_TEST_CLUSTER_DSN", "").strip() or DEFAULT_TEST_CLUSTER_DSN
)
_control_db = f"{CONTROL_TEST_PREFIX}{secrets.token_hex(6)}"
_session_tmp_data_root = Path(tempfile.mkdtemp(prefix="rheo-stream-tests-"))
atexit.register(shutil.rmtree, _session_tmp_data_root, True)

# noqa: E402 below — these assignments MUST precede the rheo_core imports:
# settings and the secret store read the environment at import time, so moving
# them under the imports silently un-pins the profile and the cluster.
os.environ["RHEO_PROFILE"] = "test"
os.environ["RHEO_CLUSTER_DSN"] = _test_cluster_dsn
os.environ["RHEO__storage__control_database"] = _control_db
os.environ["RHEO_DATA_ROOT"] = str(_session_tmp_data_root)

# Model artifacts persist across sessions. The data root above is fresh every session,
# so without this ``<data_root>/models/`` would start empty each time and every test of
# a local model provider would download its artifact again. A symlink rather than a
# second location: the provider still asks core for ``<data_root>/models/<component>``
# and gets exactly that path, and the ``atexit`` ``rmtree`` above unlinks a symlink
# without following it, so the cache survives the session. Made before any
# ``rheo_core`` import, and ``models`` is not a directory the data-root layout creates,
# so nothing races it.
_test_model_cache = Path(
    os.environ.get("RHEO_TEST_MODEL_CACHE", "").strip()
    or Path.home() / ".cache" / "rheo-stream" / "test-models"
).expanduser()
_test_model_cache.mkdir(parents=True, exist_ok=True)
(_session_tmp_data_root / "models").symlink_to(
    _test_model_cache, target_is_directory=True
)

from harness.settings_keys import register_harness_keys  # noqa: E402
from rheo_core.audit import CORE_AUDIT_SINK, install_sink  # noqa: E402
from rheo_core.migrations.orchestrator import migrate_control  # noqa: E402
from rheo_core.operations import CORE_MODULE_ID, HARNESS_MODULE_ID  # noqa: E402
from rheo_core.refs import uuid7  # noqa: E402
from rheo_core.settings import current_profile, resolve  # noqa: E402
from rheo_core.storage.control_plane import (  # noqa: E402
    WorkspaceRow,
    get_workspace,
    insert_account,
)
from rheo_core.storage.postgres import (  # noqa: E402
    PostgresBackend,
    cluster_url_from_dsn,
    get_backend,
    reset_backend,
)
from rheo_core.storage.provisioning import (  # noqa: E402
    AfterStep,
    database_name_for,
    provision,
)

# The two halves of the profile pin, and the name guard. All three read back
# through the settings resolver: asserting the local strings would prove only that
# the file can concatenate, not that the override took effect.
_resolved_profile = current_profile()
if _resolved_profile != "test":
    raise RuntimeError(
        f"tests/conftest.py set RHEO_PROFILE=test but the resolved profile is "
        f"{_resolved_profile!r}; the harness registrations would be refused"
    )
_resolved_control = resolve().get_str("storage.control_database")
if not _resolved_control.startswith(CONTROL_TEST_PREFIX):
    raise RuntimeError(
        f"refusing to run: the resolved storage.control_database is "
        f"{_resolved_control!r}, which does not start with {CONTROL_TEST_PREFIX!r}; "
        "the suite must never touch a real control database"
    )


def _droppable(name: str) -> str:
    """Only names this session could have created are ever dropped."""
    if not (
        name.startswith(CONTROL_TEST_PREFIX)
        or name.startswith(WORKSPACE_DATABASE_PREFIX)
    ):
        raise RuntimeError(f"refusing to drop {name!r}: not a test-session database")
    return name


def reap_after_from_env() -> timedelta | None:
    """The reap threshold from ``RHEO_TEST_REAP_AFTER_HOURS``; ``None`` when off."""
    raw = os.environ.get(REAP_AFTER_ENV, "").strip().lower()
    if raw in {"off", "0"}:
        return None
    if not raw:
        return timedelta(hours=DEFAULT_REAP_AFTER_HOURS)
    try:
        hours = float(raw)
    except ValueError:
        hours = -1.0
    if not hours > 0:
        raise RuntimeError(
            f"{REAP_AFTER_ENV}={raw!r}: expected a positive number of hours, "
            "or 0 / off to turn the reaper off"
        )
    return timedelta(hours=hours)


def _minted_at(database_name: str) -> datetime | None:
    """When a ``ws_<uuid7 hex>`` name's id was minted, read from the UUIDv7 time
    field; ``None`` for any other UUID version. Needs no privilege."""
    workspace_id = UUID(hex=database_name.removeprefix(WORKSPACE_DATABASE_PREFIX))
    if workspace_id.version != 7:
        return None
    return datetime.fromtimestamp((workspace_id.int >> 80) / 1000, tz=UTC)


def _created_on_disk(connection: Connection, oid: int) -> datetime | None:
    """The mtime of the database's ``PG_VERSION`` file, written once by ``CREATE
    DATABASE``. Needs superuser or ``pg_read_server_files``; ``None`` without."""
    try:
        return connection.execute(
            text(
                "SELECT (pg_stat_file('base/' || :oid || '/PG_VERSION')).modification"
            ),
            {"oid": oid},
        ).scalar_one()
    except DBAPIError:
        return None


@dataclass
class ReapReport:
    """What one reaper pass did, by database name."""

    older_than: timedelta
    reaped: list[str] = field(default_factory=list)
    fresh: list[str] = field(default_factory=list)
    connected: list[str] = field(default_factory=list)
    unknown_age: list[str] = field(default_factory=list)
    #: Listed, then gone before our DROP ran: another session's reaper got it first.
    gone: list[str] = field(default_factory=list)

    def summary(self) -> str:
        hours = self.older_than.total_seconds() / 3600
        return (
            f"ws_* reaper (older than {hours:g} h, unconnected): "
            f"dropped {len(self.reaped)}; kept {len(self.fresh)} fresh, "
            f"{len(self.connected)} connected, {len(self.unknown_age)} of unknown age; "
            f"{len(self.gone)} already gone"
        )


def reap_stale_workspace_databases(
    maintenance: Engine,
    *,
    older_than: timedelta,
    now: datetime | None = None,
    only: Collection[str] | None = None,
) -> ReapReport:
    """Drop ``ws_*`` databases older than ``older_than`` that nobody is connected to.

    What makes this safe on a cluster several sessions share:

    - **Only** ``ws_<32 hex>`` names, and each passes :func:`_droppable` too. Control
      databases, ``rheo_control`` and anything hand-made are never candidates.
    - **Age** comes from the name's UUIDv7 time field, else from the ``PG_VERSION``
      mtime; a database whose age can't be read is kept. A live session's databases
      are younger than the threshold: since #191 each lives only as long as its test.
    - **Plain ``DROP DATABASE``, never ``FORCE``.** The ``pg_stat_activity`` check is
      only a first filter. Postgres itself refuses to drop a database with any other
      connection (``ObjectInUse``), checked under the drop's own lock, so a session
      that connects between our check and our drop keeps its database.
    - **Two reapers racing** is harmless: the loser's ``DROP`` finds no database
      (``InvalidCatalogName``) and counts it as ``gone``.

    ``now`` and ``only`` exist for the reaper's own tests: shifting ``now`` makes
    everything look old, so a test must also pass ``only`` to confine the pass to
    the databases it made.
    """
    report = ReapReport(older_than)
    now = datetime.now(UTC) if now is None else now
    with maintenance.connect() as connection:
        preparer = connection.dialect.identifier_preparer
        rows = connection.execute(
            text(
                "SELECT oid, datname FROM pg_database "
                "WHERE datname LIKE 'ws\\_%' AND NOT datistemplate ORDER BY datname"
            )
        ).all()
        for oid, name in rows:
            if only is not None and name not in only:
                continue
            if not REAPABLE_WORKSPACE_NAME.fullmatch(name):
                continue
            created = _minted_at(name) or _created_on_disk(connection, oid)
            if created is None:
                report.unknown_age.append(name)
                continue
            if now - created < older_than:
                report.fresh.append(name)
                continue
            in_use = connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datid = :oid)"
                ),
                {"oid": oid},
            ).scalar_one()
            if in_use:
                report.connected.append(name)
                continue
            try:
                connection.execute(
                    text(f"DROP DATABASE {preparer.quote(_droppable(name))}")
                )
            except DBAPIError as exc:
                if isinstance(exc.orig, psycopg.errors.ObjectInUse):
                    report.connected.append(name)
                elif isinstance(exc.orig, psycopg.errors.InvalidCatalogName):
                    report.gone.append(name)
                else:
                    raise
            else:
                report.reaped.append(name)
    return report


def _drop_own(maintenance: Engine, names: Collection[str]) -> None:
    """Drop databases this session created. ``FORCE`` is allowed here and only here:
    each name was recorded by this session before it was created."""
    with maintenance.connect() as connection:
        preparer = connection.dialect.identifier_preparer
        for name in names:
            quoted = preparer.quote(_droppable(name))
            connection.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))


def _oauth_client_ids(connection: Connection) -> set[str]:
    return set(
        connection.execute(text("SELECT client_id FROM control.oauth_client")).scalars()
    )


def _delete_grantless_clients(connection: Connection, client_ids: set[str]) -> None:
    """Delete those of ``client_ids`` that hold no ``oauth_grant`` row (a client with
    one is never deleted, the house rule the RESTRICT key backs)."""
    if not client_ids:
        return
    connection.execute(
        text(
            "DELETE FROM control.oauth_client c WHERE c.client_id = ANY(:ids) "
            "AND NOT EXISTS (SELECT 1 FROM control.oauth_grant g "
            "WHERE g.client_id = c.client_id)"
        ),
        {"ids": sorted(client_ids)},
    )


def _forget_workspaces(control: Engine, workspace_ids: Collection[UUID]) -> None:
    """Delete the session control database's registry rows for ``workspace_ids``.

    Run before a mid-session drop, because a row whose database is gone breaks every
    later reader that walks the registry (``rheo migrate``, doctor, the workers).
    There is no workspace-deletion path in core yet (#133), so this follows the
    catalog: each foreign key to ``control.workspace`` that isn't already ``CASCADE``
    or ``SET NULL`` gets its rows nulled (nullable column) or deleted, then the
    workspace rows go. Reading the catalog rather than naming tables means a new
    reference can't be silently skipped. It only ever runs on this session's
    ``rheo_control_test_*`` database.
    """
    if not workspace_ids:
        return
    database = control.url.database or ""
    if not database.startswith(CONTROL_TEST_PREFIX):
        raise RuntimeError(f"refusing to edit registry rows in {database!r}")
    ids = list(workspace_ids)
    with control.begin() as connection:
        # Connector grants hold their access_token row with RESTRICT (a deliberate
        # backstop against erasing a grant), so the access_token delete below would
        # fail. Clear the OAuth rows keyed on these workspaces' tokens first.
        tokens = "SELECT id FROM control.access_token WHERE workspace_id = ANY(:ids)"
        connection.execute(
            text(
                f"DELETE FROM control.oauth_refresh_token WHERE token_id IN ({tokens})"
            ),
            {"ids": ids},
        )
        orphaned = set(
            connection.execute(
                text(
                    f"DELETE FROM control.oauth_grant WHERE token_id IN ({tokens}) "
                    "RETURNING client_id"
                ),
                {"ids": ids},
            ).scalars()
        )
        connection.execute(
            text(
                "UPDATE control.oauth_authorization SET token_id = NULL "
                f"WHERE token_id IN ({tokens})"
            ),
            {"ids": ids},
        )
        # A client whose every grant went with these workspaces is grant-less now,
        # and a recent grant-less client counts toward identity.oauth.max_clients
        # as pending, so a session's dropped grants would fill doctor's cap. Its
        # redirect URIs and authorization rows cascade.
        _delete_grantless_clients(connection, orphaned)
        preparer = connection.dialect.identifier_preparer
        references = connection.execute(
            text(
                "SELECT c.conrelid::regclass::text AS tbl, a.attname AS col, "
                "a.attnotnull AS required "
                "FROM pg_constraint c JOIN pg_attribute a "
                "ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
                "WHERE c.contype = 'f' "
                "AND c.confrelid = 'control.workspace'::regclass "
                "AND cardinality(c.conkey) = 1 AND c.confdeltype NOT IN ('c', 'n')"
            )
        ).all()
        for table, column, required in references:
            col = preparer.quote(column)
            statement = (
                f"DELETE FROM {table} WHERE {col} = ANY(:ids)"
                if required
                else f"UPDATE {table} SET {col} = NULL WHERE {col} = ANY(:ids)"
            )
            connection.execute(text(statement), {"ids": ids})
        connection.execute(
            text("DELETE FROM control.workspace WHERE id = ANY(:ids)"), {"ids": ids}
        )


@dataclass
class ClusterSession:
    """What one test session holds: the backend, its control database, and the
    exact list of databases it created."""

    backend: PostgresBackend
    control_database: str
    maintenance: Engine
    created_databases: list[str] = field(default_factory=list)
    #: The databases recorded by the running test, dropped when it finishes. ``None``
    #: outside a test body, so a module- or session-scoped fixture's databases are
    #: left to the session teardown and outlive the first test that used them.
    test_databases: list[str] | None = None
    reap_report: ReapReport | None = None

    def record(self, database_name: str) -> str:
        """Track a database this session is about to create, so it gets dropped."""
        _droppable(database_name)
        if database_name not in self.created_databases:
            self.created_databases.append(database_name)
        if self.test_databases is not None and database_name not in self.test_databases:
            self.test_databases.append(database_name)
        return database_name

    def registry_row(self, workspace_id: UUID) -> WorkspaceRow:
        row = self.registry_row_or_none(workspace_id)
        assert row is not None, f"workspace {workspace_id} has no registry row"
        return row

    def registry_row_or_none(self, workspace_id: UUID) -> WorkspaceRow | None:
        with self.backend.control_engine.connect() as connection:
            return get_workspace(connection, workspace_id)


def _announce(config: pytest.Config, line: str) -> None:
    """Write one line to the terminal from inside a fixture, past output capture."""
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    capture = config.pluginmanager.get_plugin("capturemanager")
    if reporter is None or capture is None:
        return
    with capture.global_and_fixture_disabled():
        reporter.write_line(line)


@pytest.fixture(scope="session", autouse=True)
def cluster(request: pytest.FixtureRequest) -> Iterator[ClusterSession]:
    """Reach the cluster or exit; reap stale ``ws_*`` leftovers from killed sessions;
    create and migrate the session control database; drop whatever the session
    created and still holds at teardown."""
    url = cluster_url_from_dsn(_test_cluster_dsn)
    maintenance = create_engine(
        url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )
    try:
        with maintenance.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        cause = type(exc.orig).__name__ if exc.orig is not None else "OperationalError"
        pytest.exit(
            f"the test cluster at {url.render_as_string(hide_password=True)} is "
            f"unreachable ({cause}): {REMEDY}. Postgres tests never skip.",
            returncode=3,
        )

    reap_after = reap_after_from_env()
    report = None
    if reap_after is None:
        _announce(request.config, f"ws_* reaper: off ({REAP_AFTER_ENV})")
    else:
        report = reap_stale_workspace_databases(maintenance, older_than=reap_after)
        _announce(request.config, report.summary())

    created: list[str] = [_droppable(_control_db)]
    backend = get_backend()
    if backend.control_database != _resolved_control:
        raise RuntimeError(
            "the backend's control database does not match the resolved setting"
        )
    migrate_control(backend)
    session = ClusterSession(
        backend, _control_db, maintenance, created, reap_report=report
    )
    yield session

    reset_backend()
    _drop_own(maintenance, list(reversed(session.created_databases)))
    maintenance.dispose()


@pytest.fixture(autouse=True)
def drop_test_databases(cluster: ClusterSession) -> Iterator[None]:
    """Drop the databases a test recorded as soon as that test finishes (#191), after
    deleting their registry rows so later registry walkers never meet a dead row.

    Autouse and function-scoped, so it is set up before any fixture the test asks
    for and torn down after all of them: a fixture that records in its own teardown,
    like ``test_cli.py``'s ``created_workspaces``, still lands in this test's list.
    Higher-scoped fixtures are set up before it, outside the window, and keep their
    databases until the session teardown, which also stays as the backstop for
    anything this drop misses.
    """
    cluster.test_databases = []
    try:
        yield
    finally:
        names, cluster.test_databases = cluster.test_databases, None
        if names:
            _forget_workspaces(
                cluster.backend.control_engine,
                [
                    UUID(hex=name.removeprefix(WORKSPACE_DATABASE_PREFIX))
                    for name in names
                    if REAPABLE_WORKSPACE_NAME.fullmatch(name)
                ],
            )
            _drop_own(cluster.maintenance, list(reversed(names)))


@pytest.fixture(autouse=True)
def forget_oauth_clients(cluster: ClusterSession) -> Iterator[None]:
    """Delete the grant-less ``oauth_client`` rows a test created, when it finishes.

    A registered client with no grant counts toward ``identity.oauth.max_clients``
    as pending for ``abandoned_client_minutes`` (60 by default), so without this a
    session's registrations add up and doctor's ``connector grants`` line goes
    ``FAIL`` at the cap for whichever test runs doctor afterwards (Prompt 10 rework).
    The set of client ids is read before the test and only new grant-less ones are
    deleted; a client holding a grant is never touched here (``_forget_workspaces``
    removes those with their workspace).
    """
    with cluster.backend.control_engine.connect() as connection:
        before = _oauth_client_ids(connection)
    yield
    with cluster.backend.control_engine.begin() as connection:
        _delete_grantless_clients(connection, _oauth_client_ids(connection) - before)


@pytest.fixture(autouse=True)
def audit_sinks() -> None:
    """``CORE_AUDIT_SINK`` installed under ``core`` and ``harness`` before every test.

    **Why a fixture and not the two registration helpers alone.** Dispatching an
    operation above the read class now refuses ``audit_sink_missing`` unless its owning
    module has an installed sink. ``register_core_operations()`` installs one for
    ``core`` and ``register_harness()`` one for ``harness``, which covers every test
    module that calls either — but two dispatch a mutating operation and call neither:
    ``tests/test_handler_uow.py`` hand-registers ``core.settings.set`` on a private
    registry, and ``tests/postgres/test_sessions.py`` imports ``SETTINGS_SET``
    directly. Neither needs an edit; this fixture is what closes them.

    **Function-scoped and here rather than session-scoped or per-module**, because
    ``tests/test_audit_sink.py``'s own autouse fixture calls ``reset_sinks()`` on a
    process-wide table around each of *its* tests. A sink installed once per session
    would be gone for every module that ran after that file, making the blast radius
    depend on collection order. Conftest-level autouse fixtures run before
    module-level ones, so that file still empties the table for its own tests, which
    is what it wants.

    ``install_sink`` is a no-op for the identical object, so running before every test
    costs a dict lookup and cannot conflict with either helper.
    """
    install_sink(CORE_MODULE_ID, CORE_AUDIT_SINK)
    install_sink(HARNESS_MODULE_ID, CORE_AUDIT_SINK)


@pytest.fixture
def harness_keys() -> None:
    """The seven harness settings keys, registered (idempotent) under profile test."""
    register_harness_keys()


@pytest.fixture
def owner_account_id(cluster: ClusterSession) -> UUID:
    """A fresh ``control.account`` row, the owner every provisioned workspace needs."""
    with cluster.backend.control_engine.begin() as connection:
        return insert_account(connection, display_name="owner-one").id


MakeWorkspace = Callable[..., UUID]


@pytest.fixture
def make_workspace(
    cluster: ClusterSession, owner_account_id: UUID, harness_keys: None
) -> MakeWorkspace:
    """Provision a workspace through the real state machine, recording its database
    for teardown **before** the create runs, so a faulted create is dropped too."""

    def _make(
        *,
        workspace_id: UUID | None = None,
        slug: str | None = None,
        after_step: AfterStep | None = None,
        owner: UUID | None = None,
    ) -> UUID:
        new_id = uuid7() if workspace_id is None else workspace_id
        cluster.record(database_name_for(new_id))
        provision(
            new_id,
            owner_account_id=owner_account_id if owner is None else owner,
            slug=slug,
            after_step=after_step,
        )
        return new_id

    return _make


@pytest.fixture
def workspace(make_workspace: MakeWorkspace) -> UUID:
    """One provisioned, active workspace."""
    return make_workspace()


def run_pytest_in_subprocess(
    *args: str, extra_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run a child pytest with ``extra_env`` on top of this process's environment.

    Used to prove this file's own fail-fast contract from inside the suite.
    """
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, **extra_env}
    return subprocess.run(
        ["uv", "run", "--frozen", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )

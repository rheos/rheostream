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
session records every database it creates and drops exactly those at teardown (never a
``ws_%`` pattern), and refuses to run at all unless the **resolved**
``storage.control_database`` starts with ``rheo_control_test_``.
"""

import atexit
import os
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

DEFAULT_TEST_CLUSTER_DSN = "postgresql://rheo:rheo_dev_only@localhost:5432/postgres"
CONTROL_TEST_PREFIX = "rheo_control_test_"
WORKSPACE_DATABASE_PREFIX = "ws_"
REMEDY = "run `make up`, or set RHEO_TEST_CLUSTER_DSN to a reachable cluster"

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

from harness.settings_keys import register_harness_keys  # noqa: E402
from rheo_core.migrations.orchestrator import migrate_control  # noqa: E402
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


@dataclass
class ClusterSession:
    """What one test session holds: the backend, its control database, and the
    exact list of databases it created."""

    backend: PostgresBackend
    control_database: str
    maintenance: Engine
    created_databases: list[str] = field(default_factory=list)

    def record(self, database_name: str) -> str:
        """Track a database this session is about to create, so teardown drops it."""
        _droppable(database_name)
        if database_name not in self.created_databases:
            self.created_databases.append(database_name)
        return database_name

    def registry_row(self, workspace_id: UUID) -> WorkspaceRow:
        row = self.registry_row_or_none(workspace_id)
        assert row is not None, f"workspace {workspace_id} has no registry row"
        return row

    def registry_row_or_none(self, workspace_id: UUID) -> WorkspaceRow | None:
        with self.backend.control_engine.connect() as connection:
            return get_workspace(connection, workspace_id)


@pytest.fixture(scope="session", autouse=True)
def cluster() -> Iterator[ClusterSession]:
    """Reach the cluster or exit; create and migrate the session control database;
    drop exactly what the session created at teardown."""
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

    created: list[str] = [_droppable(_control_db)]
    backend = get_backend()
    if backend.control_database != _resolved_control:
        raise RuntimeError(
            "the backend's control database does not match the resolved setting"
        )
    migrate_control(backend)
    session = ClusterSession(backend, _control_db, maintenance, created)
    yield session

    reset_backend()
    with maintenance.connect() as connection:
        preparer = connection.dialect.identifier_preparer
        for name in reversed(session.created_databases):
            quoted = preparer.quote(_droppable(name))
            connection.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
    maintenance.dispose()


@pytest.fixture
def harness_keys() -> None:
    """The six harness settings keys, registered (idempotent) under profile test."""
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

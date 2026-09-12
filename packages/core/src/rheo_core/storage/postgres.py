"""``PostgresBackend``: the release-one storage backend, and the process-wide instance.

The cluster DSN is the setting ``storage.cluster_dsn_ref`` (package default
``secret://env/RHEO_CLUSTER_DSN``), resolved **once at startup** through the storage
component's secret scope, which is constructed here and nowhere else. The control-plane
engine is the same cluster with ``database = storage.control_database``. ``CREATE
DATABASE`` cannot run inside a transaction, so provisioning and the control-database
bootstrap use an autocommit maintenance connection to the DSN's own database.

Ensuring the control database is racy at ``make demo`` (the container's lifespan and
the host-side ``rheo migrate`` start within seconds of each other), so
``ensure_database`` treats psycopg's ``DuplicateDatabase`` as success, exactly as
provisioning step 2 treats "already exists" as a retry.

``get_backend()`` builds the instance from settings on first use and keeps it;
``reset_backend()`` disposes every engine (shutdown, and the test session's teardown).
"""

import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

import psycopg.errors
from rheo_contracts import WorkspaceContext
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.pool import NullPool

from rheo_core.secrets import SecretRef, SecretStore
from rheo_core.settings import PROFILE_KEY, resolve
from rheo_core.storage.backend import StorageBackend, UnitOfWork
from rheo_core.storage.data_root import resolve_data_root
from rheo_core.storage.pools import EnginePool, check_database_name

if TYPE_CHECKING:
    from rheo_core.migrations.orchestrator import MigrationResult

CLUSTER_DSN_REF_KEY: Final = "storage.cluster_dsn_ref"
CONTROL_DATABASE_KEY: Final = "storage.control_database"
TEMPLATE_DATABASE_KEY: Final = "storage.template_database"
POOL_CACHE_SIZE_KEY: Final = "storage.pool_cache_size"
POOL_MAX_CONNECTIONS_KEY: Final = "storage.pool_max_connections"
POOL_IDLE_CLOSE_KEY: Final = "storage.pool_idle_close_seconds"

STORAGE_SCOPE_COMPONENT: Final = "storage"
STORAGE_SCOPE_PREFIXES: Final = (
    "secret://file/cluster/",
    "secret://env/RHEO_CLUSTER_DSN",
)

DEFAULT_MAINTENANCE_DATABASE: Final = "postgres"
_DRIVER: Final = "postgresql+psycopg"


def cluster_url_from_dsn(dsn: str) -> URL:
    """The cluster URL behind a ``postgresql://`` DSN, pinned to the psycopg driver.

    The value is never echoed in an error: a malformed DSN is most likely a pasted
    secret.
    """
    try:
        url = make_url(dsn)
    except ArgumentError:
        raise ValueError(
            f"{CLUSTER_DSN_REF_KEY} resolves to a value that is not a URL"
        ) from None
    if url.drivername == "postgresql":
        url = url.set(drivername=_DRIVER)
    if url.drivername != _DRIVER:
        raise ValueError(f"{CLUSTER_DSN_REF_KEY} must resolve to a postgresql:// URL")
    if not url.database:
        url = url.set(database=DEFAULT_MAINTENANCE_DATABASE)
    return url


def advisory_lock(conn: Connection, key: int) -> None:
    """Take transaction-scoped advisory lock ``key`` on ``conn``; blocks until held.

    Released when the connection's current transaction ends. Session-level locks are
    deliberately not used: their unlock statement cannot run after a failed statement
    has aborted the transaction.
    """
    if not isinstance(key, int) or isinstance(key, bool):
        raise TypeError("an advisory lock key is an int")
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


@dataclass(frozen=True, slots=True)
class ExtensionAvailability:
    """One extension as the cluster's catalogs describe it. ``default_version`` is
    ``None`` when the cluster does not ship it; ``installed_version`` is its state
    in the maintenance database; ``trusted`` is Postgres 13+'s flag that lets a
    non-superuser with ``CREATE`` on a database create it."""

    name: str
    default_version: str | None
    installed_version: str | None
    trusted: bool | None


@dataclass(frozen=True, slots=True)
class ExtensionReport:
    """The catalog facts behind ``rheo doctor``'s ``CREATE EXTENSION`` privilege
    report: read only, never a ``CREATE EXTENSION``."""

    template_database: str
    role_is_superuser: bool
    role_may_create_in_template: bool
    extensions: tuple[ExtensionAvailability, ...]

    def may_create(self, extension: ExtensionAvailability) -> bool:
        """Whether the connecting role could ``CREATE EXTENSION`` it in the template
        database: a superuser always; otherwise a trusted extension with ``CREATE``."""
        if extension.default_version is None:
            return False
        return self.role_is_superuser or (
            bool(extension.trusted) and self.role_may_create_in_template
        )


class PostgresBackend:
    """The Postgres storage backend; satisfies ``StorageBackend``."""

    supports_vector: bool = True
    supports_lexical: bool = True
    supports_skip_locked: bool = True

    def __init__(
        self,
        cluster_url: URL,
        *,
        control_database: str,
        template_database: str,
        pool_cache_size: int,
        pool_max_connections: int,
        pool_idle_close_seconds: float,
        verify_database: bool = False,
    ) -> None:
        self._cluster_url = cluster_url_from_dsn(cluster_url.render_as_string(False))
        # Resolved once, here, and published to every unit of work: the profile is
        # not re-read per transaction (see ``UnitOfWork.verify_database``).
        self.verify_database = verify_database
        UnitOfWork.verify_database = verify_database
        self.control_database = check_database_name(control_database)
        self.template_database = check_database_name(template_database)
        self.maintenance_database = check_database_name(str(self._cluster_url.database))
        self.pool_max_connections = pool_max_connections
        self.pools = EnginePool(
            self._cluster_url,
            cache_size=pool_cache_size,
            pool_size=pool_max_connections,
            idle_close_seconds=pool_idle_close_seconds,
        )
        self._maintenance_engine = create_engine(
            self._cluster_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
        )
        self._control_engine: Engine | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls) -> "PostgresBackend":
        """Resolve the DSN through the storage scope and read the five storage keys."""
        settings = resolve()
        store = SecretStore(resolve_data_root().path)
        scope = SecretStore.scope_for(STORAGE_SCOPE_COMPONENT, *STORAGE_SCOPE_PREFIXES)
        reference = SecretRef.parse(settings.get_str(CLUSTER_DSN_REF_KEY))
        dsn = store.resolve(reference, scope).expose().decode("utf-8")
        return cls(
            cluster_url_from_dsn(dsn),
            control_database=settings.get_str(CONTROL_DATABASE_KEY),
            template_database=settings.get_str(TEMPLATE_DATABASE_KEY),
            pool_cache_size=settings.get_int(POOL_CACHE_SIZE_KEY),
            pool_max_connections=settings.get_int(POOL_MAX_CONNECTIONS_KEY),
            pool_idle_close_seconds=settings.get_int(POOL_IDLE_CLOSE_KEY),
            verify_database=settings.get_str(PROFILE_KEY) == "test",
        )

    # --- engines ----------------------------------------------------------------------

    @property
    def control_engine(self) -> Engine:
        """The control-plane engine: the cluster, database ``control_database``."""
        with self._lock:
            if self._control_engine is None:
                self._control_engine = create_engine(
                    self._cluster_url.set(database=self.control_database),
                    pool_size=self.pool_max_connections,
                    max_overflow=0,
                    pool_pre_ping=True,
                )
            return self._control_engine

    @contextmanager
    def maintenance_connection(self) -> Iterator[Connection]:
        """An autocommit connection to the maintenance database, for ``CREATE
        DATABASE`` and friends. Never pooled."""
        with self._maintenance_engine.connect() as connection:
            yield connection

    def dispose(self) -> None:
        """Dispose every engine this backend created."""
        self.pools.dispose_all()
        with self._lock:
            engine, self._control_engine = self._control_engine, None
        if engine is not None:
            engine.dispose()
        self._maintenance_engine.dispose()

    # --- databases --------------------------------------------------------------------

    def database_exists(self, name: str) -> bool:
        with self.maintenance_connection() as connection:
            found = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": check_database_name(name)},
            ).first()
        return found is not None

    def ensure_database(self, name: str, *, template: str | None = None) -> bool:
        """``CREATE DATABASE name [TEMPLATE template]``; ``False`` when it already
        existed (psycopg's ``DuplicateDatabase`` is success, not failure)."""
        database = check_database_name(name)
        with self.maintenance_connection() as connection:
            preparer = connection.dialect.identifier_preparer
            statement = f"CREATE DATABASE {preparer.quote(database)}"
            if template is not None:
                statement += (
                    f" TEMPLATE {preparer.quote(check_database_name(template))}"
                )
            try:
                connection.execute(text(statement))
            except DBAPIError as exc:
                if isinstance(exc.orig, psycopg.errors.DuplicateDatabase):
                    return False
                raise
        return True

    def ensure_control_database(self) -> bool:
        """Create ``control_database`` if absent; a concurrent creator winning the race
        is success."""
        return self.ensure_database(self.control_database)

    # --- catalog readers (rheo doctor) ------------------------------------------------

    def server_version(self) -> str:
        """The cluster's ``server_version``, read on the maintenance connection."""
        with self.maintenance_connection() as connection:
            return str(connection.execute(text("SHOW server_version")).scalar_one())

    def extension_report(self, names: Sequence[str]) -> ExtensionReport:
        """Availability, trust and the connecting role's privilege for ``names``."""
        wanted = [str(name) for name in names]
        with self.maintenance_connection() as connection:
            superuser = bool(
                connection.execute(
                    text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                ).scalar_one()
            )
            may_create = bool(
                connection.execute(
                    text("SELECT has_database_privilege(:db, 'CREATE')"),
                    {"db": self.template_database},
                ).scalar_one()
            )
            available = {
                str(row.name): (row.default_version, row.installed_version)
                for row in connection.execute(
                    text(
                        "SELECT name, default_version, installed_version "
                        "FROM pg_available_extensions WHERE name = ANY(:names)"
                    ),
                    {"names": wanted},
                )
            }
            trusted = {
                str(row.name): bool(row.trusted)
                for row in connection.execute(
                    text(
                        "SELECT v.name, v.trusted "
                        "FROM pg_available_extension_versions v "
                        "JOIN pg_available_extensions e ON e.name = v.name "
                        "AND e.default_version = v.version "
                        "WHERE v.name = ANY(:names)"
                    ),
                    {"names": wanted},
                )
            }
        return ExtensionReport(
            template_database=self.template_database,
            role_is_superuser=superuser,
            role_may_create_in_template=may_create,
            extensions=tuple(
                ExtensionAvailability(
                    name=name,
                    default_version=available.get(name, (None, None))[0],
                    installed_version=available.get(name, (None, None))[1],
                    trusted=trusted.get(name),
                )
                for name in wanted
            ),
        )

    # --- the StorageBackend protocol --------------------------------------------------

    def open_unit_of_work(self, ctx: WorkspaceContext) -> UnitOfWork:
        # Imported at call time: routing imports this module for ``get_backend``.
        from rheo_core.storage.routing import open_unit_of_work

        return open_unit_of_work(ctx)

    def provision(
        self,
        workspace_id: UUID,
        *,
        owner_account_id: UUID,
        slug: str | None = None,
        after_step: Callable[[str], None] | None = None,
    ) -> None:
        # Imported at call time: provisioning imports this module for ``get_backend``.
        from rheo_core.storage.provisioning import provision

        provision(
            workspace_id,
            owner_account_id=owner_account_id,
            slug=slug,
            after_step=after_step,
        )

    def migrate(self, workspace_id: UUID, chains: Sequence[str]) -> "MigrationResult":
        # Imported at call time: the orchestrator imports ``advisory_lock`` from here.
        from rheo_core.migrations.orchestrator import migrate_workspace

        return migrate_workspace(self, workspace_id, chains=chains)

    def advisory_lock(self, conn: Connection, key: int) -> None:
        advisory_lock(conn, key)


_BACKEND: PostgresBackend | None = None
_BACKEND_LOCK = threading.Lock()


def get_backend() -> PostgresBackend:
    """The process-wide backend, built from settings on first use."""
    global _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            _BACKEND = PostgresBackend.from_settings()
        return _BACKEND


def reset_backend() -> None:
    """Dispose the process-wide backend, if any; the next ``get_backend`` rebuilds."""
    global _BACKEND
    with _BACKEND_LOCK:
        backend, _BACKEND = _BACKEND, None
    if backend is not None:
        backend.dispose()


# mypy strict checks the class against the protocol here, so a drifted signature
# fails the type gate rather than a runtime call.
_PROTOCOL_CHECK: type[StorageBackend] = PostgresBackend

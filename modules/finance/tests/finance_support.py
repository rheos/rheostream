"""What the finance suite's fixtures and tests share that is not a fixture: the test
database harness, the local stub HTTP server and the no-op snapshot provider.

They live here, not in ``conftest.py``, because a test that imports a name from
``conftest`` gets whichever ``conftest`` module pytest loaded last; in a run that
collects the repository's own ``tests/`` as well, that can be the other one. The
fixtures in ``conftest.py`` build on these.

The cluster is the one the repository's own suite uses: ``RHEO_TEST_CLUSTER_DSN``
(``make test-fast`` starts a throwaway pg16 one on port 5434), else the compose
default. The harness logs in as that cluster's user and runs every connection as
``finance_owner`` or ``finance_reader`` (NOLOGIN roles it creates when absent), set at
connect, so no finance password exists.
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from rheo_finance import db
from rheo_finance.context import LineSnapshot, UnitOfWork

TEST_REMEDY = (
    "the test cluster is unreachable: run `make test-pg-up` (or `make up`), or set"
    " RHEO_TEST_CLUSTER_DSN to a reachable cluster"
)
#: The same default as the repository's ``tests/conftest.py``.
DEFAULT_TEST_CLUSTER_DSN = "postgresql://rheo:rheo_dev_only@localhost:5432/postgres"
#: Exactly the shape the repository's session reaper drops when a killed run leaves a
#: database behind, and the only name this harness ever creates or drops.
WORKSPACE_DATABASE = re.compile(r"ws_[0-9a-f]{32}")
# The two roles the frozen revisions name. NOLOGIN: the harness logs in as the
# cluster's own user and switches role per connection, so no finance password exists.
ROLE_SQL = tuple(
    f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname ="
    f" '{role}') THEN CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
    " NOREPLICATION; END IF; END $$"
    for role in (db.OWNER_ROLE, db.READER_ROLE)
)
# Cluster-wide roles: workers creating them at once take this transaction lock first.
ROLE_LOCK_KEY = 0x46494E52


@dataclass(frozen=True)
class DatabaseUnderTest:
    owner_url: URL
    reader_url: URL
    head: str


class UnsafeTestDatabase(Exception):
    """A database name the test fixtures must never create or drop."""


def cluster_url() -> URL:
    """The test cluster, as the repository's suite reaches it."""
    raw = os.environ.get("RHEO_TEST_CLUSTER_DSN", "").strip()
    return db.to_sqlalchemy_url(raw or DEFAULT_TEST_CLUSTER_DSN)


def as_role(cluster: URL, database: str, role: str) -> URL:
    """``cluster`` on ``database``, every session started as ``role`` (``SET ROLE``
    at connect, through libpq's ``options``), so privileges are checked for that role
    exactly as for one that logged in."""
    return cluster.set(database=database).update_query_dict(
        {"options": f"-c role={role}"}
    )


def _checked(name: str) -> str:
    # Explicit raises, not asserts: ``python -O`` strips asserts, and this guards a
    # DROP DATABASE.
    if not WORKSPACE_DATABASE.fullmatch(name):
        raise UnsafeTestDatabase(f"not a test workspace database name: {name!r}")
    return name


def _maintenance(cluster: URL) -> Engine:
    return create_engine(
        cluster.set(database="postgres"),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 10},
    )


def provision_database(cluster: URL) -> DatabaseUnderTest:
    """A fresh ``ws_<32 hex>`` database with schema ``finance`` set up as the module's
    install will set it up (owned by ``finance_owner``; ``PUBLIC`` loses CONNECT and
    TEMPORARY on the database, everything on schema ``public`` and on ``finance``, and
    EXECUTE on functions the owner creates; the reader gets CONNECT and USAGE), then
    migrated to head under ``finance_owner``. Dropped by ``drop_database``."""
    name = _checked(f"ws_{uuid.uuid4().hex}")
    maintenance = _maintenance(cluster)
    try:
        with maintenance.connect() as connection:
            with connection.begin():
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": ROLE_LOCK_KEY}
                )
                for statement in ROLE_SQL:
                    connection.execute(text(statement))
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        maintenance.dispose()
    owner = as_role(cluster, name, db.OWNER_ROLE)
    reader = as_role(cluster, name, db.READER_ROLE)
    try:
        setup = create_engine(cluster.set(database=name), poolclass=NullPool)
        try:
            with setup.begin() as connection:
                for statement in (
                    f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC',
                    f'GRANT CONNECT, TEMPORARY ON DATABASE "{name}" TO {db.OWNER_ROLE}',
                    f'GRANT CONNECT ON DATABASE "{name}" TO {db.READER_ROLE}',
                    f"CREATE SCHEMA {db.SCHEMA} AUTHORIZATION {db.OWNER_ROLE}",
                    "REVOKE ALL ON SCHEMA public FROM PUBLIC",
                    f"REVOKE ALL ON SCHEMA {db.SCHEMA} FROM PUBLIC",
                    f"GRANT USAGE ON SCHEMA {db.SCHEMA} TO {db.READER_ROLE}",
                    # Global, not IN SCHEMA: a per-schema default can only add to the
                    # global default, which gives PUBLIC EXECUTE on new functions.
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {db.OWNER_ROLE}"
                    " REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
                ):
                    connection.execute(text(statement))
        finally:
            setup.dispose()
        # The library call, not ``db.migrate``: that one takes the writer lock under
        # ``$FINANCE_DATA_DIR``, which a test session must not touch.
        head = db._migrate(owner)
    except BaseException:
        drop_database(cluster, name)
        raise
    return DatabaseUnderTest(owner_url=owner, reader_url=reader, head=head)


def drop_database(cluster: URL, name: str) -> None:
    maintenance = _maintenance(cluster)
    try:
        with maintenance.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{_checked(name)}" WITH (FORCE)')
            )
    finally:
        maintenance.dispose()


#: The scratch databases this process made, so a test that clears rows in one can
#: check it is not the session's own database (both are ``ws_*``).
SCRATCH_DATABASES: set[str] = set()


def is_scratch_database(name: str | None) -> bool:
    return name is not None and name in SCRATCH_DATABASES


def no_snapshot(_uow: UnitOfWork, _ref: str) -> LineSnapshot | None:
    """The fixture snapshot provider: no line resolves."""
    return None


@dataclass(frozen=True)
class StubRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class StubReply:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b"ok"
    delay: float = 0.0


class StubServer:
    """A local HTTP server on 127.0.0.1 and an ephemeral port that records every
    request. ``reply(path, ...)`` sets the answer for a path (query ignored);
    ``redirect(path, location)`` answers 302."""

    def __init__(self) -> None:
        self.requests: list[StubRequest] = []
        self._replies: dict[str, StubReply] = {}
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with stub._lock:
                    stub.requests.append(
                        StubRequest(self.command, self.path, dict(self.headers), body)
                    )
                    reply = stub._replies.get(urlsplit(self.path).path, StubReply())
                if reply.delay:
                    time.sleep(reply.delay)
                self.send_response(reply.status)
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _answer

            def log_message(self, format: str, *args: object) -> None:
                pass  # keep test output quiet

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.03},
            daemon=True,
        )
        self._thread.start()

    @property
    def host(self) -> str:
        """The ``SinkSpec.hosts`` entry for this server."""
        return f"127.0.0.1:{self.port}"

    def url(self, path: str) -> str:
        return f"http://{self.host}{path}"

    def reply(self, path: str, **fields: Any) -> None:
        with self._lock:
            self._replies[path] = StubReply(**fields)

    def redirect(self, path: str, location: str) -> None:
        self.reply(path, status=302, headers={"Location": location}, body=b"")

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

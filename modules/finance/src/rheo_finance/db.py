"""The ledger database: credentials, the writer lock, reachability and the chain.

The standalone runtime that once lived here (``finance db up``, ``db init``,
``db migrate``, the ``docker exec`` calls and the native PostgreSQL 17 dump and
restore seam) was not imported into rheoStream: the module runs on rheoStream's
Postgres cluster, whose backups the deployment owns. ``migrate`` stays as the library
call that applies the frozen chain under the owner role.

This module keeps sqlalchemy, psycopg and alembic out of its top-level imports, because
``cli.main`` imports the database layer lazily and must start without it.

Credentials live in ``$FINANCE_DATA_DIR/db.env`` (mode 0600). Nothing here prints a
password or a DSN that carries one.
"""

from __future__ import annotations

import contextvars
import fcntl
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alembic.config import Config
    from sqlalchemy.engine import URL


OWNER_ROLE = "finance_owner"
READER_ROLE = "finance_reader"
SCHEMA = "finance"
LIVE_DATABASE = "finance"
TEST_DATABASE = "finance_test"
DATABASES = (LIVE_DATABASE, TEST_DATABASE)
VERSION_TABLE = "alembic_version_finance"
# Names go into SQL as quoted identifiers, so only plain lower-case names are accepted.
DATABASE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")

ENV_FILE_NAME = "db.env"
OWNER_DSN_KEY = "FINANCE_OWNER_DSN"
READER_DSN_KEY = "FINANCE_READER_DSN"

DEFAULT_DATA_DIR = "~/Finances"

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

REMEDY = "set FINANCE_OWNER_DSN and FINANCE_READER_DSN in db.env"


class DbError(Exception):
    """A database command failed. The message is safe to print: it holds no secret."""


# The lock is shared by installed CLI, foreground worker and release wrapper.
WRITER_WAIT_SECONDS = 30.0


@dataclass
class _WriterOwner:
    identity: tuple[Path, int, int]
    held: bool = True


_LOCK_OWNER: contextvars.ContextVar[_WriterOwner | None] = contextvars.ContextVar(
    "finance_writer_lock", default=None
)


class WriterBusy(DbError):
    """Another process owns the ledger writer lock."""


@contextmanager
def writer_lock(
    data: Path | None = None, *, wait_seconds: float = WRITER_WAIT_SECONDS
) -> Iterator[None]:
    """Bounded, process-local reentrant flock on the shared private data mount.

    Never unlink the lock file: waiters must continue to address the same inode.
    The installed CLI wrapper must hold this context across all release writes.
    """
    if not 0 <= wait_seconds <= 300:
        raise DbError("writer wait must be between 0 and 300 seconds")
    path = (data or data_dir()).resolve() / "locks" / "ledger.lock"
    identity = (path, os.getpid(), threading.get_ident())
    owner = _LOCK_OWNER.get()
    if owner is not None and owner.held and owner.identity == identity:
        yield
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WriterBusy(
                        "writer_busy: retry after the current writer"
                    ) from None
                time.sleep(min(0.05, remaining))
        owner = _WriterOwner(identity)
        token = _LOCK_OWNER.set(owner)
        try:
            yield
        finally:
            # Copies of this context keep the same object after our token resets.
            owner.held = False
            _LOCK_OWNER.reset(token)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --- paths and credentials ---------------------------------------------------------


def data_dir() -> Path:
    """``$FINANCE_DATA_DIR``, default ``~/Finances``, with ``~`` expanded."""
    raw = os.environ.get("FINANCE_DATA_DIR", "").strip() or DEFAULT_DATA_DIR
    return Path(raw).expanduser().absolute()


def env_file_path(data: Path | None = None) -> Path:
    return (data or data_dir()) / ENV_FILE_NAME


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; ``#`` comments and blank lines are skipped."""
    target = path or env_file_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DbError(f"no {ENV_FILE_NAME} at {target}; {REMEDY}") from None
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _env_value(values: dict[str, str], key: str, path: Path) -> str:
    value = values.get(key, "")
    if not value:
        raise DbError(f"{path} has no value for {key}")
    return value


def to_sqlalchemy_url(dsn: str, database: str | None = None) -> URL:
    """A libpq URI as a SQLAlchemy URL on psycopg 3, optionally for another database."""
    from sqlalchemy.engine import make_url

    url = make_url(dsn).set(drivername="postgresql+psycopg")
    if database is not None:
        url = url.set(database=database)
    return url


def owner_url(database: str | None = None) -> URL:
    path = env_file_path()
    return to_sqlalchemy_url(
        _env_value(read_env_file(path), OWNER_DSN_KEY, path), database
    )


def reader_url(database: str | None = None) -> URL:
    path = env_file_path()
    return to_sqlalchemy_url(
        _env_value(read_env_file(path), READER_DSN_KEY, path), database
    )


# --- an unreachable server -----------------------------------------------------------

UNREACHABLE = "database unreachable"
# SQLSTATEs that mean the server dropped or refused the session: class 08 (connection
# exception), and 57P01 to 57P03 (admin or crash shutdown, cannot connect now).
_UNREACHABLE_STATES = frozenset({"57P01", "57P02", "57P03"})
# What libpq relays from a server that answers but takes no connection yet.
_NOT_ACCEPTING = (
    "the database system is starting up",
    "the database system is shutting down",
    "the database system is in recovery mode",
    "the database system is not yet accepting connections",
    "the database system is not accepting connections",
)


def unreachable(exc: BaseException) -> bool:
    """Whether ``exc`` means Postgres cannot be reached: no server answered (refused,
    timed out, no such host, connection lost) or the one that answered takes no
    connection now.

    A failed connect reaches psycopg with no SQLSTATE, so libpq's text decides it: a
    server's ``FATAL`` reply (a wrong password, a missing database or role) means the
    server is up and the problem is elsewhere, so it is not "unreachable" and keeps its
    own failure line; any other connect failure is.
    """
    import psycopg
    from sqlalchemy.exc import DBAPIError

    orig = exc.orig if isinstance(exc, DBAPIError) else exc
    if not isinstance(orig, psycopg.OperationalError):
        return False
    state = orig.sqlstate
    if state is not None:
        return state.startswith("08") or state in _UNREACHABLE_STATES
    message = str(orig)
    if "FATAL:" not in message:
        return True
    return any(phrase in message for phrase in _NOT_ACCEPTING)


# --- the migration chain ------------------------------------------------------------


def alembic_config() -> Config:
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise DbError("the migration chain has no head")
    return head


def code_revisions() -> frozenset[str]:
    """Every revision id in the code's migration chain."""
    from alembic.script import ScriptDirectory

    chain = ScriptDirectory.from_config(alembic_config()).walk_revisions()
    return frozenset(each.revision for each in chain)


def migrate(url: URL | None = None) -> str:
    """Upgrade to head as owner, excluding the worker and other writers."""
    with writer_lock():
        return _migrate(url)


def _migrate(url: URL | None = None) -> str:
    """Upgrade to head under the owner role (``url``, default the live owner DSN)."""
    from alembic import command
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.pool import NullPool

    config = alembic_config()
    engine = create_engine(url or owner_url(), poolclass=NullPool)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    except OperationalError as exc:
        raise DbError(f"could not migrate: {exc.orig}; {REMEDY}") from None
    finally:
        engine.dispose()
    return head_revision()

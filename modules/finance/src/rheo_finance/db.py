"""The ledger database: roles, the writer lock, the data layout, reachability and the
chain.

The standalone runtime that once lived here (``finance db up``, ``db init``,
``db migrate``, the ``docker exec`` calls and the native PostgreSQL 17 dump and
restore seam) was not imported into rheoStream: the module runs on rheoStream's
Postgres cluster, whose backups the deployment owns. Core runs the frozen chain on its
own connection through ``migrations/env.py``, which sets the two roles up with
``prepare_roles`` and runs every revision as ``finance_owner`` (``acting_as``).

This module keeps sqlalchemy, psycopg and alembic out of its top-level imports, because
``cli.main`` imports the database layer lazily and must start without it.

The one-off read of an old standalone ledger takes its owner DSN from a private env
file (``read_env_file``, mode 0600). Nothing here prints a password or a DSN that
carries one.
"""

from __future__ import annotations

import contextvars
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from alembic.config import Config
    from sqlalchemy import Connection
    from sqlalchemy.engine import URL


OWNER_ROLE = "finance_owner"
READER_ROLE = "finance_reader"
ROLES: Final[tuple[str, str]] = (OWNER_ROLE, READER_ROLE)
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


class LayoutRefused(DbError):
    """The workspace layout, or a private file in it, cannot be used. The message is a
    fixed code."""


# --- the writer lock -----------------------------------------------------------------

# The CLI and the worker are separate processes on one workspace database.
WRITER_WAIT_SECONDS = 30.0
# ``b"RHEOFINW"``: core's key style, one constant per database, so one per workspace.
WRITER_LOCK_KEY: Final = 0x5248454F46494E57
_LOCK_POLL_SECONDS = 0.05


class WriterBusy(DbError):
    """Another connection holds the ledger writer lock."""


@contextmanager
def writer_lock(
    connection: Connection, *, wait_seconds: float = WRITER_WAIT_SECONDS
) -> Iterator[None]:
    """The ledger writer lock on ``connection``'s transaction, waited for at most
    ``wait_seconds`` (0 to 300), else ``WriterBusy``.

    A transaction-level advisory lock, released at commit or rollback: a session-level
    one would outlive a pooled connection's transaction. It is polled with
    ``pg_try_advisory_xact_lock`` rather than bounded by ``lock_timeout``, because a
    lock timeout aborts the caller's transaction. Postgres stacks the lock, so a nested
    call on the same connection enters at once; one on another connection waits, then
    raises ``WriterBusy``.
    """
    if not 0 <= wait_seconds <= 300:
        raise DbError("writer wait must be between 0 and 300 seconds")
    from sqlalchemy import text

    statement = text("SELECT pg_try_advisory_xact_lock(:key)")
    deadline = time.monotonic() + wait_seconds
    while not connection.execute(statement, {"key": WRITER_LOCK_KEY}).scalar_one():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WriterBusy("writer_busy: retry after the current writer")
        time.sleep(min(_LOCK_POLL_SECONDS, remaining))
    yield


# --- the two roles -------------------------------------------------------------------

RolePlan = Literal["reuse", "create", "refuse"]


def role_plan(*, roles_exist: bool, can_set_both: bool, is_superuser: bool) -> RolePlan:
    """What ``prepare_roles`` does with what it read.

    ``reuse`` when both roles exist and the session can switch to both (a superuser, or
    a member of both with the SET option); ``create`` when they do not both exist and
    the session is a superuser; otherwise ``refuse``. A deployment whose application
    role is not a superuser creates both roles itself and grants them to that role, per
    the module README, and then gets ``reuse``.
    """
    if roles_exist and (is_superuser or can_set_both):
        return "reuse"
    if not roles_exist and is_superuser:
        return "create"
    return "refuse"


def _create_role_sql(role: str) -> str:
    # Roles are cluster-wide and advisory locks per database, so two installs in two
    # workspaces can race here: the exception block settles it.
    return (
        f"DO $$ BEGIN CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
        " NOREPLICATION; EXCEPTION WHEN duplicate_object OR unique_violation THEN"
        " NULL; END $$"
    )


def prepare_roles(connection: Connection) -> RolePlan:
    """Make sure both roles exist, own schema ``finance`` as ``finance_owner`` when the
    schema exists, and take ``PUBLIC``'s EXECUTE off functions the owner creates.

    Runs in the caller's transaction (``CREATE ROLE`` is transactional, so a failed
    install takes the roles back). The facts are the session user's, because ``SET
    ROLE`` is checked against it. Nothing database-wide is changed: that is core's.
    """
    from sqlalchemy import text

    found: set[str] = set(
        connection.execute(
            text("SELECT rolname FROM pg_catalog.pg_roles WHERE rolname IN (:o, :r)"),
            {"o": OWNER_ROLE, "r": READER_ROLE},
        ).scalars()
    )
    roles_exist = found == set(ROLES)
    is_superuser = bool(
        connection.execute(
            text(
                "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = session_user"
            )
        ).scalar_one()
    )
    can_set_both = (
        roles_exist
        and not is_superuser
        and all(
            connection.execute(
                text("SELECT pg_has_role(session_user, :role, 'SET')"), {"role": role}
            ).scalar_one()
            for role in ROLES
        )
    )
    plan = role_plan(
        roles_exist=roles_exist, can_set_both=can_set_both, is_superuser=is_superuser
    )
    if plan == "refuse":
        raise DbError("finance_roles_unavailable: see the module README")
    if plan == "create":
        for role in ROLES:
            connection.execute(text(_create_role_sql(role)))
    owner = connection.execute(
        text(
            "SELECT pg_get_userbyid(nspowner) FROM pg_catalog.pg_namespace"
            " WHERE nspname = :schema"
        ),
        {"schema": SCHEMA},
    ).scalar_one_or_none()
    if owner is not None and owner != OWNER_ROLE:
        connection.execute(text(f"ALTER SCHEMA {SCHEMA} OWNER TO {OWNER_ROLE}"))
    # Global, not IN SCHEMA: a per-schema default can only add to the global default,
    # which gives PUBLIC EXECUTE on new functions.
    connection.execute(
        text(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE}"
            " REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
        )
    )
    return plan


@contextmanager
def acting_as(connection: Connection, role: str) -> Iterator[None]:
    """Run the block as ``role`` (one of the two finance roles) inside ``connection``'s
    transaction: ``SET LOCAL ROLE``, then ``RESET ROLE`` when the block returns.

    No reset after an exception: the caller's rollback (or its rollback to a savepoint
    taken before this) undoes the role, and a statement in an aborted transaction
    would fail. The reset matters because core's install goes on in the same
    transaction after the chain.
    """
    if role not in ROLES:
        raise DbError("not a finance role")
    from sqlalchemy import text

    connection.execute(text(f"SET LOCAL ROLE {role}"))
    yield
    connection.execute(text("RESET ROLE"))


# --- paths and private files -------------------------------------------------------


@dataclass(frozen=True)
class FinanceLayout:
    """The module's directories under the workspace's uploads directory."""

    root: Path
    data: Path
    archive: Path
    inbox: Path
    keys: Path

    @property
    def directories(self) -> tuple[Path, ...]:
        return (self.root, self.data, self.archive, self.inbox, self.keys)


def layout_under(uploads: Path) -> FinanceLayout:
    """``<uploads>/finance/{data,archive,inbox,keys}``. Pure: touches nothing."""
    root = uploads / "finance"
    return FinanceLayout(
        root=root,
        data=root / "data",
        archive=root / "archive",
        inbox=root / "inbox",
        keys=root / "keys",
    )


def check_layout(layout: FinanceLayout) -> FinanceLayout:
    """``layout`` when its five directories exist; nothing is created.

    An auto-created empty archive on an unmounted volume would let derive empty the
    ledger, so a missing directory is a refusal; ``finance init-data`` creates them.
    """
    for directory in layout.directories:
        if directory.is_symlink() or not directory.is_dir():
            raise LayoutRefused("finance_layout_missing")
    return layout


_LAYOUT: contextvars.ContextVar[FinanceLayout | None] = contextvars.ContextVar(
    "finance_layout", default=None
)

LayoutToken = contextvars.Token[FinanceLayout | None]


def use_layout(layout: FinanceLayout) -> LayoutToken:
    """Bind ``layout`` for this context; ``release_layout`` undoes it."""
    return _LAYOUT.set(layout)


def release_layout(token: LayoutToken) -> None:
    _LAYOUT.reset(token)


def bound_layout() -> FinanceLayout | None:
    return _LAYOUT.get()


def data_dir() -> Path:
    """The bound layout's ``data``, else ``$FINANCE_DATA_DIR``, else ``~/Finances``,
    with ``~`` expanded."""
    layout = bound_layout()
    if layout is not None:
        return layout.data
    raw = os.environ.get("FINANCE_DATA_DIR", "").strip() or DEFAULT_DATA_DIR
    return Path(raw).expanduser().absolute()


def read_private_file(path: Path, *, within: Path | None = None) -> str:
    """The text of ``path`` when it is a regular file, not a symlink, readable by its
    owner alone and, when ``within`` is given, inside it; else
    ``LayoutRefused("finance_private_file_refused")``. Secrets live in files like this,
    never in deployment variables."""
    refused = LayoutRefused("finance_private_file_refused")
    if within is not None:
        try:
            path.resolve().relative_to(within.resolve())
        except ValueError:
            raise refused from None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise refused from None
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise refused
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        body = handle.read()
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise refused from None


def env_file_path(data: Path | None = None) -> Path:
    return (data or data_dir()) / ENV_FILE_NAME


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; ``#`` comments and blank lines are skipped."""
    target = path or env_file_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DbError(f"no {ENV_FILE_NAME} at {target}; {REMEDY}") from None
    return parse_env_text(text)


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def to_sqlalchemy_url(dsn: str, database: str | None = None) -> URL:
    """A libpq URI as a SQLAlchemy URL on psycopg 3, optionally for another database."""
    from sqlalchemy.engine import make_url

    url = make_url(dsn).set(drivername="postgresql+psycopg")
    if database is not None:
        url = url.set(database=database)
    return url


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


def _migrate(url: URL) -> str:
    """Upgrade to head on a connection to ``url``, as ``env.py`` does under core: the
    test harness's path to a migrated scratch database."""
    from alembic import command
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.pool import NullPool

    config = alembic_config()
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    except OperationalError as exc:
        raise DbError(f"could not migrate: {exc.orig}") from None
    finally:
        engine.dispose()
    return head_revision()

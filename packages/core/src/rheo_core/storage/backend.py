"""The storage backend protocol, the unit of work, and the storage refusals.

``UnitOfWork`` is one SQLAlchemy ``Connection`` in one transaction against one
workspace database, with ``commit()`` and ``rollback()`` and nothing else. It has no
outbox slot and no audit writer, typed or ``None``-typed: that is the hard 0c boundary
in concrete form. 0c0 adds whatever it needs, and adding attributes here later never
changes the ``(ctx, uow, input)`` handler signature, so there is nothing to pre-shape.

The unit of work takes an **engine and the expected database name**, not a context.
``routing.open_unit_of_work(ctx)`` composes ``route(ctx)`` with it; the split is what
lets the storage tests drive the unit of work from an engine, because no test may
hand-build a ``WorkspaceContext``. Under ``profile = test`` ``__enter__`` asserts
``SELECT current_database()`` equals the expected name once per unit of work (the
profile is resolved once, by the backend: see ``UnitOfWork.verify_database``); that
assertion plus the registry read in ``route()`` are the backstop for the run's worst
silent failure, a write landing in the wrong workspace database.

``HandlerUnitOfWork`` is the sealed view ``dispatch()`` hands a handler: the same
connection, with ``commit``, ``rollback`` and ``__enter__`` refused. It lives here
rather than in ``operations/`` because it needs the four private slots above, and it
is a subclass rather than a flag on ``UnitOfWork`` because that class's public
surface is a pinned 0c-boundary invariant. See its own docstring.
"""

from collections.abc import Callable, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar, Final, Protocol, Self
from uuid import UUID

from rheo_contracts import WorkspaceContext
from sqlalchemy import Connection, Engine, text
from sqlalchemy.engine import Transaction

if TYPE_CHECKING:
    # ``events/consumers.py`` imports ``HandlerUnitOfWork`` from this module at
    # runtime, to type ``ConsumerHandler``. A runtime import back the other way would
    # close that cycle, so the registry's type is resolved here and the annotations
    # that use it are strings — the same shape ``EnginePool`` already has.
    from rheo_core.events.consumers import ConsumerRegistry
    from rheo_core.migrations.orchestrator import MigrationResult
    from rheo_core.storage.pools import EnginePool
    from rheo_core.work.scheduled_authority import VerifiedScheduledExecution

WORKSPACE_UNAVAILABLE: Final = "workspace_unavailable"
WORKSPACE_MISSING: Final = "workspace_missing"
WORKSPACE_EXISTS: Final = "workspace_exists"
WORKSPACE_STATE: Final = "workspace_state"
ACCOUNT_MISSING: Final = "account_missing"
IDENTITY_EXISTS: Final = "identity_exists"
SLUG_TAKEN: Final = "slug_taken"
DATABASE_MISMATCH: Final = "database_mismatch"
SCHEMA_AHEAD: Final = "schema_ahead"
UNIT_OF_WORK_CLOSED: Final = "unit_of_work_closed"
HANDLER_MAY_NOT_COMMIT: Final = "handler_may_not_commit"
"""The state :class:`HandlerUnitOfWork` refuses ``commit``/``rollback``/``__enter__``
with. Re-exported from ``rheo_core.operations.refusals`` because the dispatcher is
where a caller meets it, as an outcome state."""


class StorageRefusal(Exception):
    """A storage refusal. ``state`` names it; ``detail`` never carries a secret.

    ``workspace_state`` is set for ``workspace_unavailable`` and names the registry
    state that caused the refusal (``provisioning``, ``unavailable``, ...).
    """

    def __init__(
        self, state: str, detail: str, *, workspace_state: str | None = None
    ) -> None:
        super().__init__(f"{state}: {detail}")
        self.state = state
        self.detail = detail
        self.workspace_state = workspace_state


class UnitOfWork:
    """One connection, one transaction, one workspace database."""

    __slots__ = (
        "_connection",
        "_engine",
        "_expected_database",
        "_pool",
        "_transaction",
    )

    verify_database: ClassVar[bool] = False
    """Whether ``__enter__`` probes ``current_database()`` against the expected name.

    ``PostgresBackend`` sets this once at construction from the resolved profile: on
    under ``test``, off otherwise. A class-level flag rather than a per-transaction
    ``current_profile()`` call, because that helper re-reads ``deployment.toml`` and
    the environment on every call, which would be a file read per database
    transaction in production.
    """

    def __init__(
        self,
        engine: Engine,
        expected_database: str,
        *,
        pool: "EnginePool | None" = None,
    ) -> None:
        if not isinstance(expected_database, str) or not expected_database:
            raise TypeError("UnitOfWork needs the expected database name")
        self._engine = engine
        self._expected_database = expected_database
        self._pool = pool
        self._connection: Connection | None = None
        self._transaction: Transaction | None = None

    def __enter__(self) -> Self:
        if self._connection is not None:
            raise StorageRefusal(UNIT_OF_WORK_CLOSED, "a unit of work is entered once")
        connection: Connection | None = None
        transaction: Transaction | None = None
        try:
            connection = self._engine.connect()
            transaction = connection.begin()
            if UnitOfWork.verify_database:
                actual = str(
                    connection.execute(text("SELECT current_database()")).scalar_one()
                )
                if actual != self._expected_database:
                    raise StorageRefusal(
                        DATABASE_MISMATCH,
                        f"unit of work expected database {self._expected_database!r} "
                        f"but the connection is to {actual!r}",
                    )
            self._connection = connection
            self._transaction = transaction
            return self
        except BaseException:
            if transaction is not None and transaction.is_active:
                transaction.rollback()
            if connection is not None:
                connection.close()
            if self._pool is not None:
                self._pool.unpin(self._engine)
                self._pool = None
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        connection = self._connection
        transaction = self._transaction
        self._transaction = None
        try:
            if connection is None:
                return
            try:
                if transaction is not None and transaction.is_active:
                    transaction.rollback()
            finally:
                connection.close()
        finally:
            if self._pool is not None:
                self._pool.unpin(self._engine)
                self._pool = None

    @property
    def connection(self) -> Connection:
        """The one connection, inside the one transaction."""
        if self._connection is None or self._transaction is None:
            raise StorageRefusal(
                UNIT_OF_WORK_CLOSED,
                "the unit of work is not open: enter it, and use it before commit "
                "or rollback",
            )
        return self._connection

    def commit(self) -> None:
        transaction = self._transaction
        if transaction is None:
            raise StorageRefusal(UNIT_OF_WORK_CLOSED, "nothing to commit")
        self._transaction = None
        transaction.commit()

    def rollback(self) -> None:
        transaction = self._transaction
        if transaction is None:
            raise StorageRefusal(UNIT_OF_WORK_CLOSED, "nothing to roll back")
        self._transaction = None
        transaction.rollback()


class HandlerUnitOfWork(UnitOfWork):
    """The view ``dispatch()`` hands a handler: the same connection, sealed.

    A handler runs *inside* the dispatcher's one transaction, so ending that
    transaction is the dispatcher's job. This subclass refuses the three entry
    points a handler would reach for — ``commit``, ``rollback`` and ``__enter__``
    — with ``StorageRefusal(HANDLER_MAY_NOT_COMMIT, ...)``, which ``dispatch()``'s
    existing ``except StorageRefusal`` already folds into a refusal outcome whose
    state is that name.

    **A subclass beside ``UnitOfWork``, not a change to it.**
    ``tests/postgres/test_isolation.py`` pins ``UnitOfWork``'s public surface to
    exactly four names and its ``__slots__`` to exactly four slots, so a ``sealed``
    flag or a fifth slot would edit a ratified 0c-boundary guard. A subclass adds
    nothing to ``dir(UnitOfWork)``, leaves every existing ``uow: UnitOfWork``
    annotation compiling (``core_ops.py``, ``tokens/issue.py``, ``resolve_in`` and
    the test harness need no edit), and reaches the four private slots only because
    it lives in the module that declares them.
    ``__slots__ = ("_operation_id", "_consumers", "_scheduled_execution")``: it adds
    exactly three of its own, read through the :attr:`operation_id`,
    :attr:`consumers` and :attr:`scheduled_execution` properties, and none is a change
    to ``UnitOfWork``'s pinned surface — the guard reads ``dir(UnitOfWork)`` and
    ``UnitOfWork.__slots__``, neither of which a subclass's own slot appears in.
    ``tests/test_handler_uow.py`` pins the tuple exactly, so every addition to it is a
    deliberate, reviewed change.

    **What ``_operation_id`` is for.** ``dispatch()`` mints a ``core.operation``
    record before it opens the work transaction for a ``long_running`` declaration,
    and the handler has to learn that id to stamp it on the job it enqueues. Carrying
    it on the view the handler already receives is what keeps the ``(ctx, uow,
    input)`` handler signature unchanged — and therefore every registered handler,
    and every test that builds one, unedited. It is ``None`` for every other dispatch
    and for every view the worker loop constructs.

    **What ``_consumers`` is for.** A handler that publishes an event needs the
    process's one :class:`~rheo_core.events.consumers.ConsumerRegistry` —
    ``events.publish.publish`` takes it as a required keyword — and neither that
    package nor this one holds a process-wide instance, on purpose. The composition
    root builds it (``apps/worker``'s ``main.py``, ``apps/core``'s ``startup.py``)
    and hands it to ``dispatch()``, which carries it here on the same view, for the
    same reason the minted id is carried here: the handler signature does not move.
    It is ``None`` for a dispatch no composition root wired one into, and a handler
    that needs one refuses ``consumers_missing`` rather than building a throwaway
    registry whose fan-out would depend on which call built it.

    **What ``_scheduled_execution`` is for, and why it is the one slot with no public
    producer.** A scheduled retention expiry is release one's only deletion without a
    per-action approval, so the thing that stands in for the approval is proof that
    the call really is a leased, scheduled, single-matching-schedule worker visit —
    :class:`~rheo_core.work.scheduled_authority.VerifiedScheduledExecution`, minted
    only by ``work/loop.py``'s own leased-job path against the live row it just
    leased. ``dispatch()`` takes no such parameter and never will: a keyword on the
    public dispatcher would be precisely the channel a caller could hand itself an
    approval bypass through. It is ``None`` for every view the dispatcher builds, and
    ``approvals/gate.py`` carries through whatever the view it was given holds rather
    than producing one, exactly as it does for ``consumers``.

    **The seal is over those three names and claims no more.** ``connection`` is
    inherited and still returns a live SQLAlchemy ``Connection``, so
    ``view.connection.commit()`` still ends the transaction. Closing that would mean
    narrowing the *handler protocol* — handing handlers something that is not a
    ``UnitOfWork`` at all, which changes every registered handler's signature and the
    ``Handler`` alias — and that is a different change from this one. Run 0v's
    findings note carries the remaining gap as F1; ``overview.md``'s "a handler cannot
    commit on its own" is true of the two named methods after this and still false of
    the connection.
    """

    __slots__ = ("_operation_id", "_consumers", "_scheduled_execution")

    def __init__(
        self,
        uow: UnitOfWork,
        *,
        operation_id: UUID | None = None,
        consumers: "ConsumerRegistry | None" = None,
        scheduled_execution: "VerifiedScheduledExecution | None" = None,
    ) -> None:
        """Share an already-entered unit of work's connection and transaction.

        Not ``(engine, expected_database)``: the view never opens anything, it
        borrows what the dispatcher already opened. The first four assignments are
        the parent's own slots, reachable here because this class sits in that
        module; the last three are this subclass's own — the minted
        ``core.operation`` id or ``None``, the process's consumer registry or
        ``None``, and the worker's verified scheduled-execution capability or
        ``None``.

        All three are keyword-only with a ``None`` default, so every construction
        site that has none — the delivery drain, and every test that builds a view
        directly — needs no edit.
        """
        if not isinstance(uow, UnitOfWork):
            raise TypeError("HandlerUnitOfWork wraps a UnitOfWork")
        self._engine = uow._engine
        self._expected_database = uow._expected_database
        self._connection = uow._connection
        self._transaction = uow._transaction
        self._pool = None
        self._operation_id = operation_id
        self._consumers = consumers
        self._scheduled_execution = scheduled_execution

    @property
    def operation_id(self) -> UUID | None:
        """The ``core.operation`` record this handler's work belongs to, or ``None``.

        Read-only: a handler learns the id, it does not choose one. Non-``None``
        only for a declaration marked ``long_running``, whose handler is expected to
        pass it to ``work.jobs.enqueue_job`` so the worker can terminalise the
        record when the job finishes.
        """
        return self._operation_id

    @property
    def consumers(self) -> "ConsumerRegistry | None":
        """The process's one consumer registry, or ``None``.

        Read-only, exactly like :attr:`operation_id`: a handler reads the registry
        the composition root built, it does not choose or construct one. ``None``
        when no composition root wired one into this dispatch — a deployment gap
        that a publishing handler refuses ``consumers_missing`` for, never a licence
        to build a per-call registry.
        """
        return self._consumers

    @property
    def scheduled_execution(self) -> "VerifiedScheduledExecution | None":
        """The worker's verified scheduled-execution capability, or ``None``.

        Read-only, like the two above, and ``None`` everywhere but inside a handler
        the worker's leased-job path is running. A handler that wants to act on it
        hands it straight back to
        ``rheo_core.work.scheduled_authority.dispatch_verified_expiry_in``, which
        refuses anything that is not the very object this property returned.
        """
        return self._scheduled_execution

    def __enter__(self) -> Self:
        raise StorageRefusal(
            HANDLER_MAY_NOT_COMMIT,
            "a handler runs inside the dispatcher's unit of work and may not enter "
            "another",
        )

    def commit(self) -> None:
        raise StorageRefusal(
            HANDLER_MAY_NOT_COMMIT,
            "a handler may not commit: the dispatcher commits once, after the "
            "handler returns",
        )

    def rollback(self) -> None:
        raise StorageRefusal(
            HANDLER_MAY_NOT_COMMIT,
            "a handler may not roll back: raise OperationRefused and the dispatcher "
            "rolls back",
        )


class StorageBackend(Protocol):
    """What every storage backend provides. Postgres is release one's only one."""

    supports_vector: bool
    supports_lexical: bool
    supports_skip_locked: bool

    def open_unit_of_work(self, ctx: WorkspaceContext) -> UnitOfWork: ...

    def provision(
        self,
        workspace_id: UUID,
        *,
        owner_account_id: UUID,
        slug: str | None = None,
        after_step: Callable[[str], None] | None = None,
    ) -> None: ...

    def migrate(
        self, workspace_id: UUID, chains: Sequence[str]
    ) -> "MigrationResult": ...

    def advisory_lock(self, conn: Connection, key: int) -> None: ...

"""Harness registrations under ``profile = test`` with origin ``test_harness``: the
``harness.note`` record type (a resolver over the table ``tests/harness/records.py``
creates through the storage backend, never a shipped migration), the operations
``harness.note.get(ref)``, ``harness.note.write(body)``,
``harness.note.explode(body, message)`` (a handler that raises after writing, for the
dispatcher's rollback and failure envelope) and ``harness.note.schedule(body)`` (the
tree's one ``long_running`` declaration, which enqueues a job carrying the minted
operation id), and the scaffolding B2 needs to drive the reserved-field refusal.

Also two pieces of control-plane scaffolding the C4 tests share: ``add_member``
(an account plus its ``control.membership`` row through C3's repositories, because
``rheo member add`` is 0b2's) and ``enable_harness_module`` (a ``core.module_state``
row for ``harness`` in state ``enabled``, so a context built over that workspace
carries ``harness`` in ``enabled_modules`` — module install is phase 2, and this row
is the only way a 0b1 test can exercise the ``module_disabled`` branch both ways).

Registration is explicit (:func:`register_harness`), idempotent, and never an import
side effect. Nothing under ``rheo_core`` knows any of this exists.

**``harness.note.get`` also serves as the MCP facade's ``harness_get_note`` tool**
(C8, run 0b2), declared as a ``ToolDeclaration`` in
``rheo_core.tokens.sets.REGISTERED_TOOLS`` rather than here — that module
cannot import test code (a shipped package must not depend on
``tests/harness``, which is never installed), so it duplicates this
operation's name (``NOTE_GET``, ``"harness.note.get"``) and ``NoteRefInput``'s
shape as its own literal and its own tiny local model, rather than importing
either from this file. The test-profile gate for that second life is not a
runtime check in ``sets.py``: ``agent_default`` intersects its declared tools'
operations with the registry's live names, and this operation is only ever
actually registered by :func:`register_harness` below, so it is absent from
that intersection under any profile that never calls it.
"""

import warnings
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.audit import CORE_AUDIT_SINK, install_sink
from rheo_core.operations import (
    HARNESS_MODULE_ID,
    REGISTRY,
    OperationRefused,
    OperationRegistry,
)
from rheo_core.refs.resolver import (
    LIVE,
    NOT_FOUND,
    RESOLVERS,
    RecordHead,
    ResolverRegistry,
    Unavailable,
    resolve_in,
)
from rheo_core.settings import TEST_HARNESS_ORIGIN, resolve
from rheo_core.storage import core_tables
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.control_plane import insert_account, insert_membership_if_absent
from rheo_core.storage.postgres import PostgresBackend
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import enqueue_job
from sqlalchemy import Connection, inspect
from sqlalchemy.dialects.postgresql import insert as pg_insert

from harness.records import HARNESS_SCHEMA, ensure_note_table, get_note, write_note

NOTE_RECORD_TYPE: Final = "note"
NOTE_GET: Final = "harness.note.get"
NOTE_WRITE: Final = "harness.note.write"
NOTE_EXPLODE: Final = "harness.note.explode"
NOTE_SCHEDULE: Final = "harness.note.schedule"
NOTE_SCHEDULE_KIND: Final = "harness.note.schedule.job"
"""The one ``long_running`` harness operation and the job kind it enqueues.

The operation is ``MUTATE`` and declares ``long_running = True``, so dispatching it
mints a ``core.operation`` record; its handler reads the minted id off the sealed view
and stamps it on the job, which is what makes the worker terminalise that record when
the job finishes. It is the only ``long_running`` declaration anywhere in the tree —
no shipped ``core.*`` operation carries the flag — so it is what every criterion-13
test drives the real path with."""

_ALL_THREE_ROLES: Final = frozenset({Role.OWNER, Role.MEMBER, Role.OPERATOR})
# See ``rheo_core.operations.core_ops`` for why ``ignore`` (the default) is stated.
_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")


def note_ref(note_id: UUID) -> str:
    return RecordRef(
        module=HARNESS_MODULE_ID, record_type=NOTE_RECORD_TYPE, id=note_id
    ).format()


# --- the record type ------------------------------------------------------------------


def resolve_note(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> RecordHead | Unavailable:
    """The ``harness.note`` resolver: a lookup in the caller's own database.

    A workspace that never had the table, or has no such row, is ``not_found`` —
    which is exactly what a reference minted in another workspace gets.
    """
    connection = uow.connection
    if not inspect(connection).has_table(NOTE_RECORD_TYPE, schema=HARNESS_SCHEMA):
        return Unavailable(ref.format(), NOT_FOUND)
    row = get_note(connection, ref.id)
    if row is None:
        return Unavailable(ref.format(), NOT_FOUND)
    return RecordHead(
        ref=ref, display=row.body[:40], readable=True, state=LIVE, revision=1
    )


# --- the operations -------------------------------------------------------------------


class NoteRefInput(BaseModel):
    model_config = _IGNORE_EXTRA

    ref: str


class NoteHead(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str
    display: str
    readable: bool
    state: str
    revision: int | None


class NoteWriteInput(BaseModel):
    model_config = _IGNORE_EXTRA

    body: str


class NoteWritten(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str
    body: str


def _get_note(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: NoteRefInput
) -> NoteHead:
    head = resolve_in(model_input.ref, ctx, uow)
    if isinstance(head, Unavailable):
        raise OperationRefused(NOT_FOUND, f"{head.reference} is not available here")
    return NoteHead(
        ref=head.ref.format(),
        display=head.display,
        readable=head.readable,
        state=head.state,
        revision=head.revision,
    )


def _write_note(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: NoteWriteInput
) -> NoteWritten:
    ensure_note_table(uow.connection)
    row = write_note(uow.connection, body=model_input.body)
    return NoteWritten(ref=note_ref(row.id), body=row.body)


class NoteExplodeInput(BaseModel):
    """``body`` is written first, then the handler raises ``RuntimeError(message)``:
    the dispatcher must roll the write back and must not echo ``message``."""

    model_config = _IGNORE_EXTRA

    body: str
    message: str


def _explode(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: NoteExplodeInput
) -> NoteWritten:
    ensure_note_table(uow.connection)
    write_note(uow.connection, body=model_input.body)
    raise RuntimeError(model_input.message)


class NoteScheduleInput(BaseModel):
    """``body`` is what the enqueued job will write when the worker runs it.

    ``max_attempts`` is a **test knob on a test-owned operation**, not a shape any
    shipped operation has: omitted, the job takes the production budget from
    ``work.max_attempts`` exactly as ``work.jobs.enqueue`` does. A test that needs to
    watch a long-running job reach ``failed`` would otherwise have to drive eight
    visits and eight backoffs to get there, and would be testing the retry budget —
    which ``tests/postgres/test_worker_loop.py`` already pins — rather than the
    terminalisation mapping it is actually about.
    """

    model_config = _IGNORE_EXTRA

    body: str
    max_attempts: int | None = None


class NoteScheduled(BaseModel):
    """What a ``long_running`` dispatch answers with: the job it queued, and the
    operation record the caller can watch.

    ``operation_id`` is published on the output model as well as on the envelope so a
    test driving ``dispatch()`` directly — with no HTTP envelope to read — still sees
    the id the handler was actually handed, rather than inferring it from the outcome
    and assuming the two agree."""

    model_config = ConfigDict(frozen=True)

    job_id: UUID
    operation_id: UUID | None


class NoteSchedulePayload(BaseModel):
    """The enqueued job's own stored input."""

    body: str


def _schedule_note(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: NoteScheduleInput
) -> NoteScheduled:
    """Queue the work and return, which is the whole shape of a long-running operation.

    ``uow`` is annotated ``UnitOfWork`` because that is what the ``Handler`` alias
    declares; ``dispatch()`` always hands a ``HandlerUnitOfWork``, and the narrowing
    below is what lets this read the minted id without widening the handler signature
    every registered operation shares.

    ``ensure_note_table`` runs **here**, in the dispatcher's transaction, rather than
    in the job: the job's own transaction is where its note is written, and a DDL
    statement in there would be a second thing to roll back on a retry.

    **This handler does not mark the job due, and does not have to: the dispatcher
    does it after the commit.** A handler cannot mark it — this one runs inside the
    dispatcher's transaction against a *workspace* connection, and ``mark_work_due``
    writes the *control* database — so ``dispatch()`` calls
    ``_mark_workspace_due(ctx)`` on the ``long_running`` success path, once the work
    transaction has committed. Anything copying this pattern inherits that, which is
    the point of the fix living at the seam rather than here: the queued job is
    discovered on the very next worker pass. Until run 0c3 there was no such hook and
    the job waited for ``record_visit``'s floor — ``work.due_reconcile_seconds``,
    900 s by default — so a handler written from this pattern carried a start delay of
    up to fifteen minutes. ``operations/dispatch.py``'s module docstring carries the
    mechanism. The tests here are unaffected either way, because they call
    ``visit_workspace`` directly rather than going through ``run_one_pass``'s due-work
    index.
    """
    ensure_note_table(uow.connection)
    operation_id = uow.operation_id if isinstance(uow, HandlerUnitOfWork) else None
    job_id = enqueue_job(
        uow.connection,
        kind=NOTE_SCHEDULE_KIND,
        payload={"body": model_input.body},
        now=datetime.now(UTC),
        max_attempts=(
            resolve().get_int("work.max_attempts")
            if model_input.max_attempts is None
            else model_input.max_attempts
        ),
        operation_id=operation_id,
    )
    return NoteScheduled(job_id=job_id, operation_id=operation_id)


def run_scheduled_note(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """The job kind :data:`NOTE_SCHEDULE_KIND` runs: write the note, and nothing else.

    Registered into a ``JobKindRegistry`` by the test that needs it, never globally —
    that registry is injected all the way down from ``worker_loop``, so there is no
    process-wide instance for this module to reach.
    """
    assert isinstance(payload, NoteSchedulePayload)
    write_note(uow.connection, body=payload.body)


NOTE_SCHEDULE_DECLARATION: Final = OperationDeclaration(
    name=NOTE_SCHEDULE,
    safety_class=SafetyClass.MUTATE,
    roles=_ALL_THREE_ROLES,
    input_model=NoteScheduleInput,
    output=NoteScheduled,
    idempotency=Idempotency.NONE,
    # ``MUTATE``, so the ``AuditSpec`` is required exactly as it is on the other two
    # harness mutate declarations below.
    audit=AuditSpec(subject_field=None),
    long_running=True,
)

NOTE_EXPLODE_DECLARATION: Final = OperationDeclaration(
    name=NOTE_EXPLODE,
    safety_class=SafetyClass.MUTATE,
    roles=_ALL_THREE_ROLES,
    input_model=NoteExplodeInput,
    output=NoteWritten,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)

NOTE_GET_DECLARATION: Final = OperationDeclaration(
    name=NOTE_GET,
    safety_class=SafetyClass.READ,
    roles=_ALL_THREE_ROLES,
    input_model=NoteRefInput,
    output=NoteHead,
    idempotency=Idempotency.NONE,
    audit=None,
)

NOTE_WRITE_DECLARATION: Final = OperationDeclaration(
    name=NOTE_WRITE,
    safety_class=SafetyClass.MUTATE,
    roles=_ALL_THREE_ROLES,
    input_model=NoteWriteInput,
    output=NoteWritten,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)


def register_harness(
    *, registry: OperationRegistry = REGISTRY, resolvers: ResolverRegistry = RESOLVERS
) -> None:
    """Register the note resolver and every ``harness.note.*`` operation this module
    declares (idempotent).

    Scoped rather than counted, deliberately: the previous wording named a number,
    and the number went stale the first time a run added a declaration here.

    **Every declaration above is registered here, and that matters.** A declaration
    that exists as a module constant but is never passed to a registry is never
    actually registered, and reads as present to anyone grepping for it while being
    invisible to ``REGISTRY.names()`` and to anything derived from it.

    **It installs the harness module's audit sink too**, the same
    ``CORE_AUDIT_SINK`` the core installs for itself: ``harness`` has no manifest, so
    nothing loads one for it, and three of the four operations above are ``MUTATE``,
    which ``dispatch()`` refuses ``audit_sink_missing`` without one. The sink is a
    stateless singleton that writes into whatever workspace database the caller's unit
    of work is routed to, so installing the identical object under two module ids is
    a no-op for ``install_sink`` rather than a conflict. **This is not the whole fix**
    — two test modules dispatch a mutating operation without calling this function at
    all, which is why ``tests/conftest.py`` carries an autouse fixture that installs
    the same sink before every test.
    """
    install_sink(HARNESS_MODULE_ID, CORE_AUDIT_SINK)
    resolvers.register(
        HARNESS_MODULE_ID, NOTE_RECORD_TYPE, resolve_note, origin=TEST_HARNESS_ORIGIN
    )
    registry.register(NOTE_GET_DECLARATION, _get_note, origin=TEST_HARNESS_ORIGIN)
    registry.register(NOTE_WRITE_DECLARATION, _write_note, origin=TEST_HARNESS_ORIGIN)
    registry.register(NOTE_EXPLODE_DECLARATION, _explode, origin=TEST_HARNESS_ORIGIN)
    registry.register(
        NOTE_SCHEDULE_DECLARATION, _schedule_note, origin=TEST_HARNESS_ORIGIN
    )


# --- B2 scaffolding: input models a registration must refuse --------------------------


class Nothing(BaseModel):
    """An output model for probe declarations."""

    model_config = ConfigDict(frozen=True)


def reserved_field_models(name: str) -> tuple[type[BaseModel], ...]:
    """Input models a payload key ``name`` would bind to: one declaring ``name``
    directly, and one through an alias on a safe attribute name. Both are what
    registration must refuse naming ``name``.

    pydantic warns that ``schema`` shadows a ``BaseModel`` attribute (it still builds
    the model); the warning is silenced here because the shadowing is the point.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        plain = create_model(f"Plain_{name}", **{name: (str, ...)})
    aliased = create_model(
        f"Aliased_{name}", **{f"field_{name}": (str, Field(alias=name))}
    )
    return (plain, aliased)


def probe_declaration(name: str, input_model: type[BaseModel]) -> OperationDeclaration:
    return OperationDeclaration(
        name=name,
        safety_class=SafetyClass.MUTATE,
        roles=_ALL_THREE_ROLES,
        input_model=input_model,
        output=Nothing,
        idempotency=Idempotency.NONE,
        audit=AuditSpec(subject_field=None),
    )


def probe_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: BaseModel
) -> Nothing:
    return Nothing()


# --- control-plane and workspace scaffolding ------------------------------------------


def add_member(
    backend: PostgresBackend, workspace_id: UUID, role: Role, *, display_name: str
) -> UUID:
    """A fresh account with a ``control.membership`` row of ``role`` (``rheo member
    add`` is 0b2's; this writes the same row through C3's repositories)."""
    with backend.control_engine.begin() as connection:
        account = insert_account(connection, display_name=display_name)
        insert_membership_if_absent(
            connection, account_id=account.id, workspace_id=workspace_id, role=role
        )
    return account.id


def enable_harness_module(conn: Connection) -> None:
    """A ``core.module_state`` row for ``harness`` in state ``enabled`` (idempotent),
    in the connection's workspace database."""
    now = datetime.now(UTC)
    conn.execute(
        pg_insert(core_tables.module_state)
        .values(
            module_id=HARNESS_MODULE_ID,
            package_version="0",
            state="enabled",
            installed_at=now,
            enabled_at=now,
            disabled_at=None,
            state_detail=None,
        )
        .on_conflict_do_nothing(index_elements=[core_tables.module_state.c.module_id])
    )

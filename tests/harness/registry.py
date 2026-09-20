"""Harness registrations under ``profile = test`` with origin ``test_harness``: the
``harness.note`` record type (a resolver over the table ``tests/harness/records.py``
creates through the storage backend, never a shipped migration), the operations
``harness.note.get(ref)``, ``harness.note.write(body)``,
``harness.note.explode(body, message)`` (a handler that raises after writing, for the
dispatcher's rollback and failure envelope) and ``harness.note.schedule(body)`` (the
tree's one ``long_running`` declaration, which enqueues a job carrying the minted
operation id), the two upper-class fixtures ``harness.fixture.act(ref)`` (destructive,
and since 1a1 the tree's one window onto the context an approved handler actually runs
under) and ``harness.sink.send(destination, message)`` (external), and the scaffolding
B2 needs to drive the reserved-field refusal.

**The two upper-class fixtures are named ``harness.*`` and the ratified document
names them ``test.*``, and that is a forced deviation rather than a choice.**
``docs/architecture/confirmation-and-safety.md`` § The recording sink and the
destructive fixture calls them ``test.sink.send`` and ``test.fixture.act``. An
operation name's first segment **is** its module id
(``operations/registry.py``'s ``check_origin``), and a registration under
``origin = test_harness`` may only claim the module id ``harness``; a ``test.``
prefix would have to be registered under an origin named ``test``, which is not the
test-harness origin and therefore **not gated to ``profile = test``** — the gate
criterion 18's production-registration assertion leans on. Keeping the profile gate
and moving the prefix was the only combination that kept both rules; the document is
amended at that section to record the shipped names.

Also two pieces of control-plane scaffolding the C4 tests share: ``add_member``
(an account plus its ``control.membership`` row through C3's repositories, because
``rheo member add`` is 0b2's) and ``enable_harness_module`` (a ``core.module_state``
row for ``harness`` in state ``enabled``, so a context built over that workspace
carries ``harness`` in ``enabled_modules`` — ``harness`` is not a loaded distribution
and ``core.module.install`` will never write its row, so this is the only way a test
can exercise the ``module_disabled`` branch both ways).

Registration is explicit (:func:`register_harness`), idempotent, and never an import
side effect. Nothing under ``rheo_core`` knows any of this exists.

**``harness.note.get`` also serves as the MCP facade's ``harness_get_note`` tool**
(C8, run 0b2), declared as a ``ToolDeclaration`` in
``rheo_core.tokens.sets.CORE_TOOLS`` rather than here — that module
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
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.control_plane import insert_account, insert_membership_if_absent
from rheo_core.storage.postgres import PostgresBackend
from rheo_core.storage.repositories import insert_module_state, list_module_states
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import enqueue_job
from sqlalchemy import Connection, inspect

from harness.records import (
    HARNESS_SCHEMA,
    ensure_note_table,
    get_note,
    list_notes,
    write_note,
)

NOTE_RECORD_TYPE: Final = "note"
NOTE_GET: Final = "harness.note.get"
NOTE_WRITE: Final = "harness.note.write"
NOTE_EXPLODE: Final = "harness.note.explode"
NOTE_SCHEDULE: Final = "harness.note.schedule"
FIXTURE_ACT: Final = "harness.fixture.act"
SINK_SEND: Final = "harness.sink.send"
"""The two upper-class fixtures: the destructive one and the recording sink.

The only operations of any class above ``MUTATE`` anywhere in the tree, which is why
they are here rather than in the chunk that first needs a guard to attach to one: the
dispatcher's upper-class branch has nothing to be tested against without them.
Consumers import these constants rather than the literals — see the module docstring
for why the names are ``harness.*`` and not the document's ``test.*``."""

ACT_PREFIX: Final = "fixture.act:"
SINK_PREFIX: Final = "sink.send:"
"""How each fixture's recorded row is told apart in ``harness.note``.

A prefix on a body in the table the harness already owns, rather than two tables of
their own: what a test needs of these fixtures is "did it run, how many times, and
with what", and ``harness.note`` answers all three. A second and third harness table
would be DDL nothing else reads."""

_ACTING_SEPARATOR: Final = "@as:"
"""What splits ``harness.fixture.act``'s recorded reference from the identity of the
context its handler actually ran under.

A sequence no record reference (``<module>.<record_type>:<uuid>``) can contain, so
:func:`recorded_acts` still answers exactly the references it always did and
:func:`recorded_act_callers` answers the other half."""
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


# --- the two upper-class fixtures -----------------------------------------------------


class FixtureActInput(BaseModel):
    """``ref`` is the record the destructive operation would act on.

    A ``RecordRef`` rather than its string form, because the declaration names it as
    its ``AuditSpec`` subject field and ``dispatch``'s ``_subject_ref`` reads a field
    that *is* a ``RecordRef`` — a string there records a null subject on every row.
    That is also what puts a subject on the approval this operation is held behind,
    so ``RecordStateGuard`` has a ``subject_revision`` to compare against later.
    """

    model_config = _IGNORE_EXTRA

    ref: RecordRef


class FixtureActed(BaseModel):
    """What the destructive fixture answers with: the row it recorded, and the
    reference it was asked to act on."""

    model_config = ConfigDict(frozen=True)

    recorded_ref: str
    acted_on: str


class SinkSendInput(BaseModel):
    """``destination`` is who it would have sent to, ``message`` is what.

    ``destination`` is a payload field rather than something the declaration names,
    because no declaration surface carries a destination at all
    (``rheo_contracts.manifest``) — which is the same reason
    ``core.approval.destination_ref`` is written null in release one, and why a
    differing destination is caught as a differing payload digest.
    """

    model_config = _IGNORE_EXTRA

    destination: str
    message: str


class SinkSent(BaseModel):
    """What the sink answers with: the row it appended, and where it would have
    gone."""

    model_config = ConfigDict(frozen=True)

    recorded_ref: str
    destination: str


def acting_identity(ctx: WorkspaceContext) -> str:
    """The acting context's identity, flattened to one comparable string.

    Actor kind and id, role, and the bound purpose off the principal: exactly the
    values that differ between the caller who *made* a held call and the one who
    *approved* it. One string rather than four, because a test that compares one value
    cannot pass by checking the easy half and leaving the rest unasserted.
    """
    purpose = ctx.principal.bound_purpose
    return (
        f"{ctx.actor.kind.value}|{ctx.actor.id}|{ctx.role.value}|"
        f"{'-' if purpose is None else purpose.value}"
    )


def _fixture_act(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: FixtureActInput
) -> FixtureActed:
    """Record the request **and who is acting**, and delete nothing
    (``confirmation-and-safety.md`` § The recording sink and the destructive fixture).

    Destructive class, and it destroys nothing on purpose: what a test needs is an
    operation the dispatcher *treats* as destructive, and a fixture that really
    deleted something would make every assertion about "nothing happened" depend on
    also restoring it.

    The acting identity joined the recorded body in 1a1, when ``execute_approved``
    began running a gated handler under the original held caller's rebuilt context
    rather than the approver's. This is the only handler in the tree reached from
    inside that function, so it is the only place the difference is observable — every
    other harness handler ignores ``ctx``. It rides in the same row behind
    :data:`_ACTING_SEPARATOR` rather than in a second one, so the row *count* this
    fixture produces is what it always was: several tests assert on
    ``harness.note``'s whole contents, and a fixture that silently doubled its writes
    would change what they see.
    """
    ensure_note_table(uow.connection)
    reference = model_input.ref.format()
    row = write_note(
        uow.connection,
        body=f"{ACT_PREFIX}{reference}{_ACTING_SEPARATOR}{acting_identity(ctx)}",
    )
    return FixtureActed(recorded_ref=note_ref(row.id), acted_on=reference)


def _sink_send(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: SinkSendInput
) -> SinkSent:
    """Append what it would have sent, and send nothing.

    External class. Release one has no ``external_action`` machinery and no job queue
    for one — no criterion in this run's scope forces either — so the effect of an
    external-class operation here is this handler's own write, run from the approving
    transaction like the destructive fixture's. What that makes testable is the
    approval binding and the gate; what it does not yet test is the enqueue-and-outcome
    path, which arrives with the phase-five execution operation.
    """
    ensure_note_table(uow.connection)
    row = write_note(
        uow.connection,
        body=f"{SINK_PREFIX}{model_input.destination}|{model_input.message}",
    )
    return SinkSent(recorded_ref=note_ref(row.id), destination=model_input.destination)


def _recorded(conn: Connection, prefix: str) -> tuple[str, ...]:
    """Every row one fixture recorded, oldest first, with its prefix stripped.

    Reads the table directly rather than through an operation: this is the harness
    looking at its own scaffolding, which is what a test counts calls with. A
    workspace whose ``harness.note`` table was never created answers ``()`` — the
    same "nothing recorded" a created-but-empty table gives, because the fixture
    creates the table itself and a workspace where it never ran has neither.
    """
    if not inspect(conn).has_table(NOTE_RECORD_TYPE, schema=HARNESS_SCHEMA):
        return ()
    return tuple(
        row.body[len(prefix) :]
        for row in list_notes(conn)
        if row.body.startswith(prefix)
    )


def act_payload(reference: str) -> dict[str, object]:
    """``harness.fixture.act``'s payload for ``reference``, as a caller sends it.

    Built from the string form's own parts rather than from a ``RecordRef`` object,
    so a caller that holds only the formatted reference — which is what every
    operation publishes — can dispatch this fixture without importing the contract
    type. The nested shape is what pydantic validates a ``RecordRef`` field from.
    """
    module, _, rest = reference.partition(".")
    record_type, _, record_id = rest.partition(":")
    return {"ref": {"module": module, "record_type": record_type, "id": record_id}}


def recorded_acts(conn: Connection) -> tuple[str, ...]:
    """The references ``harness.fixture.act`` has recorded, oldest first."""
    return tuple(
        body.split(_ACTING_SEPARATOR, 1)[0] for body in _recorded(conn, ACT_PREFIX)
    )


def recorded_act_callers(conn: Connection) -> tuple[str, ...]:
    """The acting identity each ``harness.fixture.act`` call ran under, oldest first.

    Paired with :func:`recorded_acts` positionally: one row per call, the reference
    before the separator and the caller after it.
    """
    return tuple(
        body.split(_ACTING_SEPARATOR, 1)[1] for body in _recorded(conn, ACT_PREFIX)
    )


def sent_messages(conn: Connection) -> tuple[str, ...]:
    """The ``<destination>|<message>`` rows ``harness.sink.send`` has appended."""
    return _recorded(conn, SINK_PREFIX)


FIXTURE_ACT_DECLARATION: Final = OperationDeclaration(
    name=FIXTURE_ACT,
    safety_class=SafetyClass.DESTRUCTIVE,
    roles=_ALL_THREE_ROLES,
    input_model=FixtureActInput,
    output=FixtureActed,
    idempotency=Idempotency.NONE,
    # Above ``READ``, so required. It names a subject field, unlike every other
    # declaration in this tree: the approval this operation is held behind binds the
    # record it acts on, and the audit row names it too.
    audit=AuditSpec(subject_field="ref"),
)

SINK_SEND_DECLARATION: Final = OperationDeclaration(
    name=SINK_SEND,
    safety_class=SafetyClass.EXTERNAL,
    roles=_ALL_THREE_ROLES,
    input_model=SinkSendInput,
    output=SinkSent,
    idempotency=Idempotency.NONE,
    # ``subject_field=None``: a send acts on no record of a registered type. Its
    # destination is a payload field, which is not what a subject field names.
    audit=AuditSpec(subject_field=None),
)


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
    nothing loads one for it, and every declaration above the read class — which is
    all of them but ``harness.note.get``, scoped rather than counted because the
    count went stale the moment this run added two — is refused
    ``audit_sink_missing`` by ``dispatch()`` without one. The sink is a
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
    registry.register(FIXTURE_ACT_DECLARATION, _fixture_act, origin=TEST_HARNESS_ORIGIN)
    registry.register(SINK_SEND_DECLARATION, _sink_send, origin=TEST_HARNESS_ORIGIN)


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
    in the connection's workspace database.

    **Through ``insert_module_state``, which is the table's only writer (FR 10), and
    with the idempotency moved here rather than into it.** This helper is called more
    than once against one workspace across several files, so "insert or do nothing" is
    a property callers depend on — but the writer itself must stay a strict insert, or
    a repeat ``core.module.install`` racing past its own ``module_already_installed``
    pre-flight check would report success while writing nothing and leaving whatever
    version the earlier row recorded. A read-then-write is the right shape *here*,
    where the caller is a test harness inside its own transaction and there is no
    concurrency for the window to matter to.

    ``harness`` is not a loaded module and never will be: this row is what puts it in
    a context's ``enabled_modules`` so a 0b1-era test can drive the ``module_disabled``
    branch both ways.
    """
    if any(row.module_id == HARNESS_MODULE_ID for row in list_module_states(conn)):
        return
    now = datetime.now(UTC)
    insert_module_state(
        conn,
        module_id=HARNESS_MODULE_ID,
        package_version="0",
        state="enabled",
        installed_at=now,
        enabled_at=now,
    )

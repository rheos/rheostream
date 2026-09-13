"""The spike's record type, its resolver, and its three operation declarations.

**This file names the audit sink nowhere, and that absence is asserted.** AC 11's
static scan (``tests/test_audit_sink.py::test_only_the_dispatcher_calls_the_sink``)
walks ``packages``, ``apps`` and ``modules`` for any call of ``.record(...)`` and
requires exactly one hit, in ``rheo_core/operations/dispatch.py``. A module declares
*that* an operation is audited, by carrying an ``AuditSpec``; it never decides *when*
the row is written. That separation is what FR 4 exists to prove, and a single
convenience call from a handler here would disprove it.

**The handlers do not create the schema, unlike ``tests/harness/registry.py``'s
``_write_note``**, which calls ``ensure_note_table`` on every write. They do not need
to: ``authorize`` refuses ``module_disabled`` before a handler runs unless the
workspace carries an enabled ``core.module_state`` row for ``spike``, and the only
two things that write that row — ``rheo-spike install`` and the test fixture — create
the schema in the same transaction. So a reachable handler always has its tables, and
a handler that created them on demand would hide a broken install behind a lazy
CREATE.

The three declarations, and why there are three rather than the run card's two:

- ``spike.note.add`` — ``mutate``, roles ``{owner, member}``, with a real
  ``AuditSpec``. The operation the audit seam is proved on.
- ``spike.note.list`` — ``read``, roles ``{owner, member, operator}``, ``audit =
  None``. A workspace with no notes answers ``{"notes": []}``: a success, not a
  refusal.
- ``spike.note.commit_early`` — ``mutate``, roles ``{owner}`` only, **test-only
  scaffolding**, deleted with the rest of the module. Its handler calls
  ``uow.commit()``, so AC 10's refusal is driven through the real dispatch path
  rather than by unit-testing the sealed subclass; and its one-role set is the only
  reachable driver for a ``role_not_permitted`` refusal in this run, because the
  other two operations permit every role a session can hold.
"""

from datetime import datetime
from typing import Annotated, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.operations.registry import Handler
from rheo_core.refs.resolver import LIVE, NOT_FOUND, RecordHead, Unavailable
from rheo_core.storage.backend import UnitOfWork

from rheo_spike.records import (
    NOTE_TABLE,
    SPIKE_SCHEMA,
    get_note,
    has_note_table,
    list_notes,
    write_note,
)

MODULE_ID: Final = SPIKE_SCHEMA
NOTE_RECORD_TYPE: Final = NOTE_TABLE

NOTE_ADD: Final = "spike.note.add"
NOTE_LIST: Final = "spike.note.list"
NOTE_COMMIT_EARLY: Final = "spike.note.commit_early"

# ``extra = "ignore"`` is pydantic's default and is stated explicitly for the reason
# ``rheo_core.operations.core_ops`` states it: a payload that also carries
# ``workspace_id``, ``database``, ``dsn``, ``connection_string`` or ``schema`` must
# have those keys dropped and the operation must proceed against the context's own
# workspace (AC 13). ``extra = "forbid"`` would turn that payload into
# ``input_invalid`` and make the "ignored, and the authenticated context is used"
# half unreachable; ``extra = "allow"`` is refused at registration.
_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")

NoteBody = Annotated[str, StringConstraints(min_length=1, max_length=4000)]
"""``constr(min_length=1, max_length=4000)`` in its annotated spelling, which is what
``rheo_contracts.refs.Segment`` already uses in this checkout. Empty and oversize both
refuse ``input_invalid`` naming ``body``; ``dispatch``'s ``_describe_validation_error``
reports the location and the message and never the value."""


def note_ref(note_id: UUID) -> RecordRef:
    return RecordRef(module=MODULE_ID, record_type=NOTE_RECORD_TYPE, id=note_id)


# --- the record type ------------------------------------------------------------------


def resolve_note(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
) -> RecordHead | Unavailable:
    """The ``spike.note`` resolver: a lookup in the caller's own database.

    A workspace that never had the schema, and a workspace that has it but not this
    row, are both ``not_found`` — which is exactly what a reference minted in another
    workspace gets, and is criterion 7's seam at the storage layer.
    """
    connection = uow.connection
    if not has_note_table(connection):
        return Unavailable(ref.format(), NOT_FOUND)
    row = get_note(connection, ref.id)
    if row is None:
        return Unavailable(ref.format(), NOT_FOUND)
    return RecordHead(
        ref=ref,
        display=row.body[:40],
        readable=True,
        state=LIVE,
        revision=row.revision,
    )


# --- the models -----------------------------------------------------------------------


class NoteAddInput(BaseModel):
    model_config = _IGNORE_EXTRA

    body: NoteBody


class NoteAdded(BaseModel):
    """``spike.note.add``'s output: the reference the caller can resolve, and the row
    as written."""

    model_config = ConfigDict(frozen=True)

    ref: str
    body: str
    created_at: datetime
    revision: int


class NoteHead(BaseModel):
    """One item of ``spike.note.list``.

    The same four fields as :class:`NoteAdded` and deliberately a separate model: the
    generated OpenAPI document names each operation's own output type, and collapsing
    the two would make ``spike.note.add``'s response schema read as a list item.
    """

    model_config = ConfigDict(frozen=True)

    ref: str
    body: str
    created_at: datetime
    revision: int


class NoteListInput(BaseModel):
    model_config = _IGNORE_EXTRA

    limit: Annotated[int, Field(ge=1, le=200)] = 50


class NoteList(BaseModel):
    model_config = ConfigDict(frozen=True)

    notes: list[NoteHead]


class NoteCommitEarlyInput(BaseModel):
    """The test-only third operation's input; the same ``body`` rule as the real one."""

    model_config = _IGNORE_EXTRA

    body: NoteBody


# --- the handlers ---------------------------------------------------------------------


def _add_note(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: NoteAddInput
) -> NoteAdded:
    row = write_note(uow.connection, body=model_input.body)
    return NoteAdded(
        ref=note_ref(row.id).format(),
        body=row.body,
        created_at=row.created_at,
        revision=row.revision,
    )


def _list_notes(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: NoteListInput
) -> NoteList:
    rows = list_notes(uow.connection, limit=model_input.limit)
    return NoteList(
        notes=[
            NoteHead(
                ref=note_ref(row.id).format(),
                body=row.body,
                created_at=row.created_at,
                revision=row.revision,
            )
            for row in rows
        ]
    )


def _commit_early(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: NoteCommitEarlyInput
) -> NoteAdded:
    """Write a note, then try to end the dispatcher's transaction.

    ``uow`` is a ``HandlerUnitOfWork``, so ``commit()`` raises
    ``StorageRefusal(handler_may_not_commit)``, which ``dispatch``'s existing
    ``except StorageRefusal`` turns into a refusal outcome of that state — and the
    write above it is rolled back with everything else. The ``return`` below is
    unreachable through the dispatcher and exists only so the declared output type is
    honest.
    """
    row = write_note(uow.connection, body=model_input.body)
    uow.commit()
    return NoteAdded(
        ref=note_ref(row.id).format(),
        body=row.body,
        created_at=row.created_at,
        revision=row.revision,
    )


# --- the declarations -----------------------------------------------------------------


NOTE_ADD_DECLARATION: Final = OperationDeclaration(
    name=NOTE_ADD,
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER, Role.MEMBER}),
    input_model=NoteAddInput,
    output=NoteAdded,
    idempotency=Idempotency.NONE,
    # ``subject_field=None``: the note does not exist until the handler has run, so
    # there is no input field naming it. That ``AuditSpec`` cannot describe a create
    # is run 0v's finding F4.
    audit=AuditSpec(subject_field=None),
)

NOTE_LIST_DECLARATION: Final = OperationDeclaration(
    name=NOTE_LIST,
    safety_class=SafetyClass.READ,
    roles=frozenset({Role.OWNER, Role.MEMBER, Role.OPERATOR}),
    input_model=NoteListInput,
    output=NoteList,
    idempotency=Idempotency.NONE,
    audit=None,
)

NOTE_COMMIT_EARLY_DECLARATION: Final = OperationDeclaration(
    name=NOTE_COMMIT_EARLY,
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER}),
    input_model=NoteCommitEarlyInput,
    output=NoteAdded,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)

OPERATIONS: Final[tuple[tuple[OperationDeclaration, Handler], ...]] = (
    (NOTE_ADD_DECLARATION, _add_note),
    (NOTE_LIST_DECLARATION, _list_notes),
    (NOTE_COMMIT_EARLY_DECLARATION, _commit_early),
)
"""What the manifest hands the loader: each declaration beside its handler, because
``OperationDeclaration`` cannot carry one (``rheo_contracts`` may not import
``UnitOfWork`` — run 0v's finding F3)."""

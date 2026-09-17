"""Criterion 14 / AC 21-26: audit dispatch, end to end against a real workspace.

**This file replaces ``tests/test_absent_behaviour.py::test_dispatch_calls_no_audit
_sink``**, removed in the same commit that landed the behaviour it denied — the fourth
and last criterion probe to leave by that route. That probe searched three roots for a
call; what defends criterion 14 now is the behaviour below, driven through the real
dispatcher against real workspace databases.

**Nothing here queries ``core.audit_record`` directly.** Every read goes through
``core.audit.list``, dispatched the way a caller would (AC 26, criterion 14's third
sentence). Writes to *other* tables — the ``core.operation`` row an
``unresolved`` record needs — go through their own repositories, because the subject
here is the audit record and not those.

**The structural half of this coverage lives in ``tests/test_audit_sink.py``**, which
pins that exactly one module in the shipped tree calls a sink and that the audit module
names the dispatcher nowhere. That file runs without a database and empties the
process-wide sink table around each of its own tests, which is why the behaviour is
here and the shape is there. Read the two as one pair.

Seams: ``dispatch()``'s audit write on every outcome path (success inside the
operation's transaction, failure and refusal after the rollback, a pre-transaction
refusal on a fresh connection) and the three-layer missing-registration refusal
(declaration — which is asserted in ``tests/postgres/test_context_routing.py``, beside
the registry that enforces it — startup wiring, and dispatch).
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from importlib import import_module
from typing import Any
from uuid import UUID, uuid4

import pytest
from conftest import ClusterSession
from harness.records import ensure_note_table, list_notes
from harness.registry import (
    FIXTURE_ACT,
    NOTE_EXPLODE,
    NOTE_SCHEDULE,
    NOTE_WRITE,
    SINK_SEND,
    NoteWriteInput,
    NoteWritten,
    Nothing,
    act_payload,
    add_member,
    enable_harness_module,
    probe_declaration,
    probe_handler,
    register_harness,
)
from harness.settings_keys import HARNESS_MEMBER
from pydantic import BaseModel
from rheo_contracts import AuditSpec, RecordRef, Role, SafetyClass, WorkspaceContext
from rheo_core.approvals import (
    APPROVAL_APPROVE,
    APPROVAL_REFUSE,
    STANDING_GRANT_CREATE,
    STANDING_GRANT_REVOKE,
)
from rheo_core.audit import (
    AUDIT_FAILED,
    AUDIT_LIST,
    AUDIT_REFUSED,
    AUDIT_SUCCEEDED,
    CORE_AUDIT_SINK,
    install_sink,
    reset_sinks,
)
from rheo_core.boundary import context_for_harness
from rheo_core.boundary.factories import context_from_token
from rheo_core.operations import (
    AUDIT_SINK_MISSING,
    CORE_MODULE_ID,
    HARNESS_MODULE_ID,
    INPUT_INVALID,
    MODULE_DISABLED,
    OPERATION_GET,
    OPERATION_NOT_PERMITTED,
    OPERATION_UNKNOWN,
    ROLE_NOT_PERMITTED,
    SETTINGS_SET,
    SETTINGS_SET_MEMBER,
    WORKSPACE_EXPORT,
    WORKSPACE_RESTORE,
    OperationRegistry,
    RegistrationRefused,
    dispatch,
    register_core_operations,
)
from rheo_core.operations.audit_paths import check_audit_paths
from rheo_core.operations.core_ops import (
    TOKEN_ISSUE,
    TOKEN_REVOKE,
    WORKSPACE_STATUS,
)
from rheo_core.operations.operation_ops import OPERATION_RESOLVE
from rheo_core.operations.records import AUDIENCE_NONE, mark_unresolved, mint
from rheo_core.settings import TEST_HARNESS_ORIGIN
from rheo_core.storage.backend import (
    HANDLER_MAY_NOT_COMMIT,
    StorageRefusal,
    UnitOfWork,
)
from rheo_core.storage.routing import open_unit_of_work
from sqlalchemy import Engine

pytestmark = pytest.mark.postgres

TOKEN_DAYS_CLI = "identity.token_max_days.cli"
"""A real, workspace-scope, writable key, the same one ``test_handler_uow.py`` uses."""

NOTE = "the body an audited dispatch writes"

THE_SIXTEEN = frozenset(
    {
        SETTINGS_SET,
        SETTINGS_SET_MEMBER,
        TOKEN_ISSUE,
        TOKEN_REVOKE,
        OPERATION_RESOLVE,
        APPROVAL_APPROVE,
        APPROVAL_REFUSE,
        STANDING_GRANT_CREATE,
        STANDING_GRANT_REVOKE,
        WORKSPACE_EXPORT,
        WORKSPACE_RESTORE,
        NOTE_WRITE,
        NOTE_EXPLODE,
        NOTE_SCHEDULE,
        FIXTURE_ACT,
        SINK_SEND,
    }
)
"""Every registered operation above the read class at the end of run 0c3's C7: nine
core and five harness (test profile only).

It was eight at the end of run 0c2 and twelve at the end of C6. C7 adds
``core.standing_grant.create`` and ``core.standing_grant.revoke``, both ``MUTATE``.
C6 had added ``core.approval.approve`` and ``core.approval.refuse`` (both ``MUTATE``)
and the two upper-class harness fixtures — ``harness.fixture.act`` (``DESTRUCTIVE``)
and ``harness.sink.send`` (``EXTERNAL``), the first operations of any class above
``MUTATE`` in the tree.

**A literal, and the registry-derived set is compared against it**, not the other way
round. Derived alone, the assertion would equal whatever the registry happened to hold
and could not fail — an operation that lost its ``MUTATE`` class would match its own
mistake. The count is asserted as well as the membership, so a fifteenth operation
added later fails here loudly rather than being silently left out of the coverage
below."""


# --- fixtures and helpers -------------------------------------------------------------


@pytest.fixture(autouse=True)
def registrations(harness_keys: None) -> None:
    """The shipped core operations and the suite's harness module, on the process-wide
    registries. Both are idempotent, and both install an audit sink."""
    register_core_operations()
    register_harness()


@pytest.fixture
def database(cluster: ClusterSession, workspace: UUID) -> str:
    return cluster.registry_row(workspace).database_name


@pytest.fixture
def engine(cluster: ClusterSession, database: str) -> Engine:
    return cluster.backend.pools.engine_for(database)


@pytest.fixture
def owner(
    workspace: UUID, owner_account_id: UUID, engine: Engine, database: str
) -> WorkspaceContext:
    """An owner context over a workspace whose ``harness`` module is enabled.

    The module row is what puts ``harness`` in ``enabled_modules``; without it every
    ``harness.*`` dispatch refuses ``module_disabled`` before its handler, and each
    case below would pass its assertion for the wrong reason. The note table is created
    here too, so a test asserting an effect *did not* land is not asserting against a
    table that was never there.
    """
    with UnitOfWork(engine, database) as uow:
        enable_harness_module(uow.connection)
        ensure_note_table(uow.connection)
        uow.commit()
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _records(ctx: WorkspaceContext) -> tuple[Any, ...]:
    """Every audit record of the context's workspace, through the supported read."""
    outcome = dispatch(ctx, AUDIT_LIST, {"limit": 500})
    assert outcome.ok, outcome
    assert outcome.result is not None
    return tuple(outcome.result.records)  # type: ignore[attr-defined]


def _ids(ctx: WorkspaceContext) -> frozenset[UUID]:
    """The audit record ids, for a before/after delta."""
    return frozenset(record.audit_id for record in _records(ctx))


def _added(ctx: WorkspaceContext, before: frozenset[UUID]) -> tuple[Any, ...]:
    """The records written since ``before`` was taken, newest first.

    A delta rather than a count, because ``core.audit.list`` is a workspace-wide read
    and a test that dispatched anything to set itself up has rows of its own in it.
    """
    return tuple(record for record in _records(ctx) if record.audit_id not in before)


def _one(ctx: WorkspaceContext, before: frozenset[UUID]) -> Any:
    """The single record written since ``before``; fails naming the others if not."""
    added = _added(ctx, before)
    assert len(added) == 1, [(r.operation_name, r.outcome) for r in added]
    return added[0]


def _digest(payload: dict[str, object]) -> str:
    """The expected ``request_digest``, computed independently of the dispatcher.

    Spelled out here rather than imported from ``dispatch`` so that the assertion is
    over the *canonical form the column documents* — the SHA-256 of the sorted,
    whitespace-free JSON — and not over whatever the implementation happens to do.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _unresolved_operation(engine: Engine, database: str) -> UUID:
    """A ``core.operation`` row in state ``unresolved``, for ``core.operation.resolve``.

    Written through the repository rather than dispatched: reaching ``unresolved`` from
    a dispatch means a worker and an external effect whose outcome is unknown, which is
    ``tests/postgres/test_operation_records.py``'s subject. Here the record is scenery.
    """
    now = datetime.now(UTC)
    with UnitOfWork(engine, database) as uow:
        operation_id = mint(
            uow.connection,
            name=NOTE_SCHEDULE,
            safety_class=SafetyClass.MUTATE.value,
            actor_kind="system",
            actor_id=None,
            entry="job",
            audience_kind=AUDIENCE_NONE,
            audience_id=None,
            now=now,
        )
        assert mark_unresolved(uow.connection, operation_id=operation_id, now=now)
        uow.commit()
    return operation_id


@pytest.fixture
def private_registry() -> OperationRegistry:
    """A registry of this test's own, holding the core and harness registrations.

    Never the process-wide ``REGISTRY``: it is a singleton with no reset helper, so a
    count asserted against it would depend on whatever else the session registered
    first — which is the whole reason AC 26 specifies a derived set over a registry the
    test controls.
    """
    registry = OperationRegistry()
    register_core_operations(registry)
    register_harness(registry=registry)
    return registry


@pytest.fixture
def restore_sinks() -> Iterator[None]:
    """Put the process-wide sink table back after a test empties it.

    ``tests/conftest.py`` installs both sinks before every test, so the *next* test is
    safe either way; this is for the rest of the test that emptied the table, which
    still has an audit list to read.
    """
    yield
    install_sink(CORE_MODULE_ID, CORE_AUDIT_SINK)
    install_sink(HARNESS_MODULE_ID, CORE_AUDIT_SINK)


def _commit_from_the_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: BaseModel
) -> Nothing:
    """A handler that tries to end the dispatcher's transaction.

    ``HandlerUnitOfWork`` refuses ``commit`` with ``handler_may_not_commit``, so this
    raises a ``StorageRefusal`` from inside ``with uow:`` and never returns — which is
    a different ``except`` block from the ``OperationRefused`` one, and the point.
    """
    uow.commit()
    return Nothing()


# --- AC 26: every registered mutating operation is covered ----------------------------


def test_the_mutating_set_derived_from_the_registry_is_the_declared_sixteen(
    private_registry: OperationRegistry,
) -> None:
    """AC 26's first half: the set under test comes from the registry, and is fourteen.

    Derived by safety class rather than by name, so an operation added later is in the
    set whether or not anybody remembered this file — and then fails the comparison
    against :data:`THE_SIXTEEN`, which is the loud failure AC 26 asks for rather
    than a silent gap in the coverage below.

    ``is not SafetyClass.READ`` is what makes this cover the two upper-class fixtures
    as well as the mutate operations: "above read" was a distinction with no example
    until run 0c3 registered one.
    """
    mutating = {
        name
        for name in private_registry.names()
        if (operation := private_registry.lookup(name)) is not None
        and operation.declaration.safety_class is not SafetyClass.READ
    }
    assert mutating == set(THE_SIXTEEN), sorted(mutating)
    assert len(mutating) == 16, sorted(mutating)


def test_one_dispatch_of_every_mutating_kind_leaves_a_matching_audit_record(
    cluster: ClusterSession,
    workspace: UUID,
    owner: WorkspaceContext,
    owner_account_id: UUID,
    engine: Engine,
    database: str,
) -> None:
    """AC 26's second half: a dispatch of **each** registered mutating kind, and a
    record for every one, read through ``core.audit.list``.

    Several do not succeed and that is deliberate rather than a gap:
    ``harness.note.explode`` raises after writing, and the two upper-class fixtures are
    *held* rather than run. A ``failed`` record and a ``refused`` record are both the
    record criterion 14 requires — a call that was refused before its handler is
    exactly the case an audit trail must not be silent about. What is asserted per
    operation is that a row exists and names it; which outcome each path writes is
    asserted case by case below.

    The two approval operations are dispatched against the approvals the two fixtures'
    own holds produced, which is the only way to reach them: an approval id has to come
    from a gated call.

    The two standing-grant operations chain the same way — ``revoke`` needs an id that
    only ``create`` can mint — and ``create`` names a **read** operation, because a
    grant naming any of the three upper classes is refused at grant time and would
    write no row and no audit record at all.
    """
    before = _ids(owner)
    unresolved = _unresolved_operation(engine, database)
    member = add_member(cluster.backend, workspace, Role.MEMBER, display_name="m-audit")

    issued = dispatch(owner, TOKEN_ISSUE, {"kind": "cli", "set_name": "read_only"})
    assert issued.ok, issued
    token_id = issued.result.token_id  # type: ignore[union-attr]
    written = dispatch(owner, NOTE_WRITE, {"body": NOTE})
    assert written.ok, written
    subject = str(written.result.ref)  # type: ignore[union-attr]
    held_act = dispatch(owner, FIXTURE_ACT, act_payload(subject))
    assert held_act.approval_id is not None, held_act
    held_send = dispatch(
        owner, SINK_SEND, {"destination": "party:one", "message": NOTE}
    )
    assert held_send.approval_id is not None, held_send
    granted = dispatch(
        owner,
        STANDING_GRANT_CREATE,
        {
            "account_id": str(owner_account_id),
            "operation_names": [WORKSPACE_STATUS],
            "expires_at": datetime(2999, 1, 1, tzinfo=UTC).isoformat(),
        },
    )
    assert granted.ok, granted
    grant_id = granted.result.grant_id  # type: ignore[union-attr]

    dispatched: dict[str, object] = {
        STANDING_GRANT_CREATE: granted,
        STANDING_GRANT_REVOKE: dispatch(
            owner, STANDING_GRANT_REVOKE, {"grant_id": str(grant_id)}
        ),
        FIXTURE_ACT: held_act,
        SINK_SEND: held_send,
        APPROVAL_APPROVE: dispatch(
            owner, APPROVAL_APPROVE, {"approval_id": str(held_act.approval_id)}
        ),
        APPROVAL_REFUSE: dispatch(
            owner, APPROVAL_REFUSE, {"approval_id": str(held_send.approval_id)}
        ),
        SETTINGS_SET: dispatch(
            owner, SETTINGS_SET, {"key": TOKEN_DAYS_CLI, "value": 30}
        ),
        SETTINGS_SET_MEMBER: dispatch(
            owner, SETTINGS_SET_MEMBER, {"key": HARNESS_MEMBER, "value": "mine"}
        ),
        TOKEN_REVOKE: dispatch(owner, TOKEN_REVOKE, {"token_id": str(token_id)}),
        OPERATION_RESOLVE: dispatch(
            owner,
            OPERATION_RESOLVE,
            {
                "operation_id": str(unresolved),
                "outcome": "cancelled",
                "note": "audited by this test",
            },
        ),
        NOTE_WRITE: dispatch(owner, NOTE_WRITE, {"body": NOTE}),
        NOTE_EXPLODE: dispatch(owner, NOTE_EXPLODE, {"body": NOTE, "message": "boom"}),
        NOTE_SCHEDULE: dispatch(owner, NOTE_SCHEDULE, {"body": NOTE}),
        WORKSPACE_EXPORT: dispatch(owner, WORKSPACE_EXPORT, {}),
        WORKSPACE_RESTORE: dispatch(
            owner,
            WORKSPACE_RESTORE,
            {"artifact_path": "/artifact-does-not-exist.tar.zst"},
        ),
    }
    assert member is not None

    audited = {record.operation_name for record in _added(owner, before)}
    assert audited == set(THE_SIXTEEN), sorted(audited)
    # Every dispatch that was supposed to run did: a coverage assertion that passed
    # because most operations refused for an unrelated reason would prove nothing.
    for name, outcome in dispatched.items():
        assert outcome.state not in {  # type: ignore[union-attr]
            OPERATION_UNKNOWN,
            ROLE_NOT_PERMITTED,
            MODULE_DISABLED,
            AUDIT_SINK_MISSING,
        }, (name, outcome)


def test_an_operation_above_mutate_is_audited_too(
    owner: WorkspaceContext, private_registry: OperationRegistry
) -> None:
    """The rule is "above read", not "is mutate" — and an upper-class operation is
    audited on the path it actually takes.

    **This test changed shape in run 0c3 and the reason is the behaviour, not the
    test.** It was written when every operation in the tree was ``mutate``, to catch a
    dispatcher written as ``safety_class is SafetyClass.MUTATE`` that would skip the
    audit row of a ``destructive`` operation "the moment one is declared". One is now
    declared, and such an operation no longer *runs* on an ordinary dispatch: C6's gate
    holds it for an approval. So the outcome asserted here is ``refused`` — the row for
    a call the dispatcher declined to run — rather than ``succeeded``. The property is
    unchanged and still the point: a class above ``mutate`` leaves a row naming itself
    and its class. What ``succeeded`` looks like for one of these is
    ``tests/postgres/test_approvals.py::test_the_held_call_and_its_execution_are_both
    _audited``, on the approved-execution path where success can actually happen.
    """
    name = "harness.probe.destroy"
    private_registry.register(
        probe_declaration(name, NoteWriteInput).model_copy(
            update={"safety_class": SafetyClass.DESTRUCTIVE}
        ),
        probe_handler,
        origin=TEST_HARNESS_ORIGIN,
    )
    before = _ids(owner)

    outcome = dispatch(owner, name, {"body": NOTE}, registry=private_registry)

    assert outcome.state == "approval_required", outcome
    record = _one(owner, before)
    assert record.operation_name == name
    assert record.safety_class == SafetyClass.DESTRUCTIVE.value
    assert record.outcome == AUDIT_REFUSED


# --- AC 23: the success row, and what it carries --------------------------------------


def test_the_success_row_names_the_actor_the_entry_and_the_request(
    owner: WorkspaceContext,
) -> None:
    """AC 23's column list, on one row, against values the test chose.

    ``subject_ref`` is null because no release-one declaration names a subject field —
    the ordinary case for this column, not a missing value. ``operation_id`` is null
    because ``core.settings.set`` is not ``long_running``.
    """
    payload: dict[str, object] = {"key": TOKEN_DAYS_CLI, "value": 30}
    before = _ids(owner)
    at_least = datetime.now(UTC)

    assert dispatch(owner, SETTINGS_SET, payload).ok

    record = _one(owner, before)
    assert record.operation_name == SETTINGS_SET
    assert record.safety_class == SafetyClass.MUTATE.value
    assert record.actor_kind == owner.actor.kind.value
    assert record.actor_id == owner.actor.id
    assert record.entry == owner.entry.value
    assert record.outcome == AUDIT_SUCCEEDED
    assert record.subject_ref is None
    assert record.operation_id is None
    assert record.request_digest == _digest(payload)
    assert record.occurred_at >= at_least


def test_a_long_running_dispatch_writes_one_row_carrying_its_minted_id(
    owner: WorkspaceContext,
) -> None:
    """The ``operation_id`` foreign key is used, and the dispatch is audited
    ``succeeded``.

    ``succeeded`` is about the *dispatch*, which queued the work and committed; whether
    the work succeeds is the ``core.operation`` record's question and the worker's to
    answer. The two are different rows with different lifetimes, and conflating them is
    the reading this case exists to pin.
    """
    before = _ids(owner)

    outcome = dispatch(owner, NOTE_SCHEDULE, {"body": NOTE})

    assert outcome.state == "pending", outcome
    assert outcome.operation_id is not None
    record = _one(owner, before)
    assert record.operation_name == NOTE_SCHEDULE
    assert record.operation_id == outcome.operation_id
    assert record.outcome == AUDIT_SUCCEEDED


def test_an_operation_whose_own_transaction_cannot_commit_leaves_only_a_failed_row(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 23's sharpest shape: the effect and the audit row are inseparable.

    **What is asserted is one ``failed`` row, not none**, and the name says so. AC 23's
    literal sentence — "finds neither the effect nor the audit row" — is about the
    ``succeeded`` row written inside the transaction that did not commit; that row does
    go down with the rollback. § D5 then requires the ``failed`` row this dispatch owes
    its caller, written afterwards in a transaction of its own. An earlier name here
    carried AC 23's wording rather than this body's assertion.

    The success row is written **inside** the operation's transaction, so a commit that
    fails takes both. The failure is injected because the tree has no natural
    commit-time failure to drive it with — there is no deferred constraint, and every
    other way of making a handler fail raises *before* the commit, where the row was
    never written in the first place and the property would not be under test.

    The mutation this kills is committing the success row in a transaction of its own:
    under it the caller is told the dispatch failed, the note is absent, and a
    ``succeeded`` audit record for it survives anyway — a record of a mutation that
    never happened, which is the one defect that makes the whole log untrustworthy.
    """

    class _CommitRefusingUnitOfWork(UnitOfWork):
        __slots__ = ()

        def commit(self) -> None:
            raise RuntimeError("the operation's transaction could not commit")

    real = open_unit_of_work
    calls = {"n": 0}

    def refuse_the_first_commit(ctx: WorkspaceContext) -> UnitOfWork:
        calls["n"] += 1
        if calls["n"] == 1:
            return _CommitRefusingUnitOfWork(engine, database)
        return real(ctx)

    # Both snapshots are taken **before** the patch is installed: reading the audit
    # list is itself a dispatch, so it would spend the one poisoned unit of work on a
    # read and the operation under test would commit normally.
    before = _ids(owner)
    notes_before = _notes(engine, database)
    # ``import_module`` rather than a string target or ``import ... as``, for the
    # reason ``test_operation_records.py`` gives: the package re-exports ``dispatch``
    # itself, so both of those forms patch an attribute on a function object and the
    # dispatcher never sees the replacement.
    dispatch_module = import_module("rheo_core.operations.dispatch")
    monkeypatch.setattr(dispatch_module, "open_unit_of_work", refuse_the_first_commit)

    outcome = dispatch(owner, NOTE_WRITE, {"body": NOTE})

    monkeypatch.undo()
    assert outcome.state == "failed", outcome
    assert _notes(engine, database) == notes_before, "the effect must not have landed"
    record = _one(owner, before)
    assert record.operation_name == NOTE_WRITE
    assert record.outcome == AUDIT_FAILED


def _notes(engine: Engine, database: str) -> tuple[str, ...]:
    with UnitOfWork(engine, database) as uow:
        return tuple(row.body for row in list_notes(uow.connection))


# --- AC 24: the non-success rows ------------------------------------------------------


def test_a_handler_that_raises_leaves_one_failed_row_after_the_rollback(
    owner: WorkspaceContext, engine: Engine, database: str
) -> None:
    """``harness.note.explode`` writes, then raises: no note, exactly one ``failed``
    row.

    The row survives a transaction that was rolled back, which is the whole reason it
    is written after the rollback in one of its own rather than beside the effect.
    """
    before = _ids(owner)
    notes_before = _notes(engine, database)

    outcome = dispatch(owner, NOTE_EXPLODE, {"body": NOTE, "message": "boom"})

    assert outcome.state == "failed", outcome
    assert _notes(engine, database) == notes_before
    record = _one(owner, before)
    assert record.operation_name == NOTE_EXPLODE
    assert record.outcome == AUDIT_FAILED


def test_a_refusal_raised_inside_the_transaction_leaves_one_refused_row(
    owner: WorkspaceContext,
) -> None:
    """A handler's ``OperationRefused`` — ``core.settings.set`` on an undeclared key —
    is refused, rolled back, and audited ``refused`` rather than ``failed``.

    The distinction is not cosmetic: ``refused`` says the system declined, ``failed``
    says it broke, and an auditor reading a log that spelled both the same way could
    not tell a permission boundary doing its job from a service falling over.
    """
    before = _ids(owner)

    outcome = dispatch(owner, SETTINGS_SET, {"key": "no.such.key", "value": 1})

    assert not outcome.ok, outcome
    record = _one(owner, before)
    assert record.operation_name == SETTINGS_SET
    assert record.outcome == AUDIT_REFUSED


@pytest.mark.parametrize("state", [ROLE_NOT_PERMITTED, MODULE_DISABLED, INPUT_INVALID])
def test_a_refusal_before_the_transaction_still_writes_exactly_one_row(
    cluster: ClusterSession,
    workspace: UUID,
    owner: WorkspaceContext,
    make_workspace: Any,
    state: str,
) -> None:
    """The fresh-connection path: a refusal reached before any unit of work was opened
    is still audited, exactly once.

    These four are the reachable refusals that have a resolved declaration and a
    routable workspace but **no transaction to roll back from** — so the row is written
    on a connection opened for it alone. Nothing about them is unauditable; it would
    simply be easy to write a dispatcher that only audited after its ``uow`` opened,
    and that dispatcher would silently lose every permission refusal in the system.
    ``operation_not_permitted`` is the fourth and has its own case below, because
    reaching it needs a token-derived context rather than a role or a module row.
    """
    before = _ids(owner)

    if state == ROLE_NOT_PERMITTED:
        # ``core.settings.set`` is owner-only, so a member context refuses.
        account = add_member(
            cluster.backend, workspace, Role.MEMBER, display_name="m-refused"
        )
        ctx = context_for_harness(workspace, account, Role.MEMBER)
        assert isinstance(ctx, WorkspaceContext), ctx
        outcome = dispatch(ctx, SETTINGS_SET, {"key": TOKEN_DAYS_CLI, "value": 30})
    elif state == MODULE_DISABLED:
        # A second workspace, whose ``harness`` module row was never written.
        other = make_workspace()
        ctx = context_for_harness(other, owner.actor.id, Role.OWNER)
        assert isinstance(ctx, WorkspaceContext), ctx
        outcome = dispatch(ctx, NOTE_WRITE, {"body": NOTE})
    else:
        # ``body`` is required, so an empty payload is refused by the input model.
        ctx = owner
        outcome = dispatch(ctx, NOTE_WRITE, {})

    assert outcome.state == state, outcome
    if state == MODULE_DISABLED:
        # The row lands in *that* workspace's database, not this one, which is AC 23's
        # "the row's workspace is the database it is in" stated as a test.
        assert _added(owner, before) == ()
        elsewhere = _one(ctx, frozenset())
    else:
        elsewhere = _one(owner, before)
    assert elsewhere.outcome == AUDIT_REFUSED
    assert elsewhere.subject_ref is None


def test_an_operation_outside_the_contexts_set_is_refused_and_audited(
    owner: WorkspaceContext,
) -> None:
    """``operation_not_permitted``, the fourth reachable pre-transaction refusal.

    A ``read_only`` token carries an operation set that excludes every mutate, so
    presenting it and dispatching one refuses before ``authorize`` reaches the role
    check — and is audited, on a fresh connection, exactly like the other three.
    """
    issued = dispatch(owner, TOKEN_ISSUE, {"kind": "cli", "set_name": "read_only"})
    assert issued.ok, issued
    bearer = context_from_token(issued.result.value, "api")  # type: ignore[union-attr]
    assert isinstance(bearer, WorkspaceContext), bearer
    before = _ids(owner)

    outcome = dispatch(bearer, SETTINGS_SET, {"key": TOKEN_DAYS_CLI, "value": 30})

    assert outcome.state == OPERATION_NOT_PERMITTED, outcome
    record = _one(owner, before)
    assert record.operation_name == SETTINGS_SET
    assert record.outcome == AUDIT_REFUSED


def test_the_three_structurally_excluded_refusals_write_nothing(
    owner: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary of the reachable set, pinned from the outside.

    The three branches § D5 names structurally outside the set, each driven in the
    shape in which it genuinely has nothing to write a row with: ``context_required``
    (no ``WorkspaceContext``, so no workspace to route to), ``operation_unknown`` (no
    declaration, so no safety class — and that column is NOT NULL), and a
    ``StorageRefusal`` out of ``open_unit_of_work`` — **on a dispatch that minted
    nothing**, which is the case driven below: ``harness.note.write`` is not
    ``long_running``, so no operation record exists to be ended, no second connection
    is attempted, and there is genuinely nothing to write the row with.

    That qualifier is load-bearing rather than pedantic. The exclusion is about having
    nothing to write with, not about which branch the control flow is in, so a
    ``long_running`` dispatch that reaches the same refusal *does* get a row once its
    operation record is successfully ended on a fresh connection — the case
    :func:`test_an_unobtainable_transaction_still_audits_a_dispatch_that_minted_a_record`
    drives, and the reason this one uses the non-long-running operation.

    Asserted here so that a later narrowing of the *reachable* side cannot hide inside
    the exclusions: with only the positive cases above, a dispatcher that audited
    nothing before its transaction opened would still look correct from one direction.
    """
    before = _ids(owner)

    assert dispatch(None, SETTINGS_SET, {"key": TOKEN_DAYS_CLI, "value": 30}).state == (
        "context_required"
    )
    assert dispatch(owner, "core.nothing.here", {}).state == OPERATION_UNKNOWN

    real = open_unit_of_work
    calls = {"n": 0}

    def refuse_the_first(ctx: WorkspaceContext) -> UnitOfWork:
        calls["n"] += 1
        if calls["n"] == 1:
            raise StorageRefusal("workspace_unavailable", "the pool went away")
        return real(ctx)

    dispatch_module = import_module("rheo_core.operations.dispatch")
    monkeypatch.setattr(dispatch_module, "open_unit_of_work", refuse_the_first)
    refused = dispatch(owner, NOTE_WRITE, {"body": NOTE})
    monkeypatch.undo()

    assert refused.state == "workspace_unavailable", refused
    assert _added(owner, before) == (), [
        (r.operation_name, r.outcome) for r in _added(owner, before)
    ]


def test_an_unobtainable_transaction_still_audits_a_dispatch_that_minted_a_record(
    owner: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the third exclusion, where it is not an exclusion at all.

    A ``long_running`` dispatch mints its ``core.operation`` row and **commits it** on
    its own connection before the work transaction is opened. So when that work
    transaction is then refused, the dispatcher opens a *fresh* connection to end the
    orphaned record ``failed`` — and when that connection is obtained, a second one
    demonstrably does not "meet the identical failure": one was just obtained. Without
    the row, the tree holds a durable, committed operation record naming an actor, a
    workspace and a time, with no audit record naming it, on the criterion whose whole
    content is that every mutating operation writes one.

    Only the **second** ``open_unit_of_work`` call is refused, which is the work
    transaction: the mint takes the first, and ending the record and writing the row
    take the two after it. Refusing every call would test the exclusion above instead.
    """
    before = _ids(owner)

    real = open_unit_of_work
    calls = {"n": 0}

    def refuse_the_second(ctx: WorkspaceContext) -> UnitOfWork:
        calls["n"] += 1
        if calls["n"] == 2:
            raise StorageRefusal("workspace_unavailable", "the pool went away")
        return real(ctx)

    dispatch_module = import_module("rheo_core.operations.dispatch")
    monkeypatch.setattr(dispatch_module, "open_unit_of_work", refuse_the_second)
    refused = dispatch(owner, NOTE_SCHEDULE, {"body": NOTE})
    monkeypatch.undo()

    assert refused.state == "workspace_unavailable", refused
    assert refused.operation_id is not None, refused
    # The committed record the audit row has to name, read the supported way.
    got = dispatch(owner, OPERATION_GET, {"operation_id": str(refused.operation_id)})
    assert got.ok, got
    assert got.result.state == "failed", got.result  # type: ignore[union-attr]

    record = _one(owner, before)
    assert record.operation_name == NOTE_SCHEDULE
    assert record.outcome == AUDIT_REFUSED
    assert record.operation_id == refused.operation_id


def test_a_handler_returning_the_wrong_type_leaves_exactly_one_failed_row(
    owner: WorkspaceContext, private_registry: OperationRegistry
) -> None:
    """``output_invalid``: the row is written from inside ``_output_invalid``.

    One of two row-writing exits that no assertion reached — removing the audit write
    from this branch left every other case in this file green, so the branch shipped
    unpinned. The declaration promises ``NoteWritten`` and the probe handler returns
    ``Nothing``, which is the dispatcher's own output-type check and not an exception
    out of the handler: ``failed`` is the outcome because the caller asked for
    something the system was willing to do and the operation did not deliver it.
    """
    name = "harness.probe.mistyped"
    private_registry.register(
        probe_declaration(name, NoteWriteInput).model_copy(
            update={"output": NoteWritten}
        ),
        probe_handler,
        origin=TEST_HARNESS_ORIGIN,
    )
    before = _ids(owner)

    outcome = dispatch(owner, name, {"body": NOTE}, registry=private_registry)

    assert outcome.state == "failed", outcome
    assert outcome.error is not None and outcome.error.error_code == "output_invalid"
    record = _one(owner, before)
    assert record.operation_name == name
    assert record.outcome == AUDIT_FAILED


def test_a_handler_that_commits_its_own_transaction_leaves_exactly_one_refused_row(
    owner: WorkspaceContext, private_registry: OperationRegistry
) -> None:
    """The handler-side ``StorageRefusal``: the second unpinned row-writing exit.

    ``HandlerUnitOfWork`` refuses ``commit`` — ending the one transaction is the
    dispatcher's job — so a handler that calls it raises a ``StorageRefusal`` out of
    ``with uow:`` rather than an ``OperationRefused``. That is a different ``except``
    block from the one the refusal case above drives, and its audit write was likewise
    removable without turning anything red.
    """
    name = "harness.probe.commits"
    private_registry.register(
        probe_declaration(name, NoteWriteInput), _commit_from_the_handler,
        origin=TEST_HARNESS_ORIGIN,
    )  # fmt: skip
    before = _ids(owner)

    outcome = dispatch(owner, name, {"body": NOTE}, registry=private_registry)

    assert outcome.state == HANDLER_MAY_NOT_COMMIT, outcome
    record = _one(owner, before)
    assert record.operation_name == name
    assert record.outcome == AUDIT_REFUSED


# --- the subject reference, and the digest that reaches every row ---------------------


class _SubjectInput(BaseModel):
    """An input model that carries a subject reference, which no shipped one does."""

    ref: RecordRef


def test_a_declaration_naming_a_subject_field_records_the_formatted_reference(
    owner: WorkspaceContext, private_registry: OperationRegistry
) -> None:
    """``_subject_ref``'s positive branch, which the tree cannot otherwise reach.

    Every shipped ``AuditSpec`` is ``AuditSpec(subject_field=None)``, so the branch
    that reads the field ships as production code with no dispatch exercising it —
    replacing the whole function with ``return None`` left this file green. A
    declaration of this test's own names a field, and the row carries the canonical
    string form the column documents.
    """
    name = "harness.probe.subject"
    ref = RecordRef(module="harness", record_type="note", id=uuid4())
    private_registry.register(
        probe_declaration(name, _SubjectInput).model_copy(
            update={"audit": AuditSpec(subject_field="ref")}
        ),
        probe_handler,
        origin=TEST_HARNESS_ORIGIN,
    )
    before = _ids(owner)

    outcome = dispatch(
        owner, name, {"ref": ref.model_dump(mode="json")}, registry=private_registry
    )

    assert outcome.ok, outcome
    record = _one(owner, before)
    assert record.operation_name == name
    assert record.subject_ref == ref.format()
    assert record.subject_ref == f"harness.note:{ref.id}"


def test_registration_refuses_a_subject_field_the_input_model_does_not_declare(
    private_registry: OperationRegistry,
) -> None:
    """The registration-time half: an unreadable ``subject_field`` is refused.

    ``_subject_ref`` reads the named field with a ``None`` default, so without this
    rule a misspelled ``subject_field`` records a null on every row of the operation —
    indistinguishable from the ordinary null of an operation that acts on no record.
    That is the ``NULL_SINK`` silent skip one level down, inside the chunk whose
    subject is removing it, so it is refused where the declaration is made rather than
    discovered by reading rows that all look ordinary.
    """
    with pytest.raises(RegistrationRefused) as excinfo:
        private_registry.register(
            probe_declaration("harness.probe.missubject", NoteWriteInput).model_copy(
                update={"audit": AuditSpec(subject_field="not_a_field")}
            ),
            probe_handler,
            origin=TEST_HARNESS_ORIGIN,
        )

    message = str(excinfo.value)
    assert "not_a_field" in message, message
    assert NoteWriteInput.__name__ in message, message


def test_a_payload_python_cannot_canonicalise_is_answered_not_raised(
    owner: WorkspaceContext,
) -> None:
    """``dispatch()`` answers its caller with an outcome and never raises — including
    on a payload whose digest has no ordinary canonical form.

    ``_request_digest`` runs before ``authorize`` and outside every ``try``, so an
    exception there is not a refusal anybody can read. A nested mapping with keys of
    mixed types is exactly that: ``sort_keys=True`` compares the keys to sort them and
    ``int`` does not order against ``str``, which ``default=str`` does not reach
    because that hook is for values. Unreachable through a JSON body, whose keys are
    always strings; reachable from every in-process caller, this suite included.

    The digest is asserted to be **stable** rather than to equal a particular value:
    what the column needs from the degraded rendering is determinism, and a test
    pinning the exact fallback bytes would pin the fallback's shape instead.
    """
    payload: dict[str, object] = {"body": NOTE, "extra": {1: "x", "b": 2}}
    before = _ids(owner)

    first = dispatch(owner, NOTE_WRITE, dict(payload))
    second = dispatch(owner, NOTE_WRITE, dict(payload))

    assert first.ok, first
    assert second.ok, second
    rows = _added(owner, before)
    assert len(rows) == 2, [(r.operation_name, r.outcome) for r in rows]
    assert {row.operation_name for row in rows} == {NOTE_WRITE}
    digests = {row.request_digest for row in rows}
    assert len(digests) == 1, digests
    # And it is a real digest, not the sentinel path's, which would still be stable:
    # the key-coerced rendering succeeds, so this differs from a payload that lacks
    # the offending member entirely.
    assert digests != {_digest({"body": NOTE})}


# --- AC 25 and AC 22: the missing registration, at two of its layers ------------------


def test_a_missing_sink_refuses_the_call_and_writes_neither_effect_nor_row(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    restore_sinks: None,
) -> None:
    """AC 25: no sink, no call — the refusal happens **before the handler runs**.

    Both halves are asserted and the first is the one that matters. A dispatcher that
    merely skipped the row when ``sink_for`` answered ``None`` would let the operation
    run and commit, which is exactly the ``NULL_SINK`` silent no-op issue #45 removed,
    and a test checking only "no audit row" would pass under it unchanged.
    """
    notes_before = _notes(engine, database)
    before = _ids(owner)
    reset_sinks()

    outcome = dispatch(owner, NOTE_WRITE, {"body": "never written"})

    assert outcome.state == AUDIT_SINK_MISSING, outcome
    assert outcome.error is not None
    assert HARNESS_MODULE_ID in outcome.error.error_text
    # The read below needs no sink of its own: ``core.audit.list`` is a read operation,
    # and audit is required only above the read class.
    assert _notes(engine, database) == notes_before, "the handler must not have run"
    assert _added(owner, before) == ()


def test_check_audit_paths_names_every_offender(
    private_registry: OperationRegistry, restore_sinks: None
) -> None:
    """AC 22: startup refuses when a registered non-``READ`` operation has no sink,
    naming every one of them and its module.

    Every offender in one message rather than the first: a deployment that wired three
    modules wrongly should learn all three from one boot. Read operations must not
    appear — ``core.audit.list`` and the rest register with ``audit = None`` legally —
    which is asserted by comparing against the exact declared set.
    """
    reset_sinks()

    with pytest.raises(RuntimeError) as excinfo:
        check_audit_paths(private_registry)

    message = str(excinfo.value)
    for name in THE_SIXTEEN:
        assert name in message, message
    assert AUDIT_LIST not in message, message
    assert CORE_MODULE_ID in message and HARNESS_MODULE_ID in message, message

    # And it passes once the sinks are back, so the refusal is about the wiring and not
    # about the registry being non-empty.
    install_sink(CORE_MODULE_ID, CORE_AUDIT_SINK)
    install_sink(HARNESS_MODULE_ID, CORE_AUDIT_SINK)
    check_audit_paths(private_registry)


def test_check_audit_paths_ignores_a_module_whose_operations_are_all_reads(
    restore_sinks: None,
) -> None:
    """A module that registers only reads needs no sink, and startup must not demand
    one.

    The mutation this kills is a check written over "every registered operation"
    instead of "every registered operation above the read class", which would refuse
    startup for a perfectly legal read-only module and make the wiring layer
    unusable.
    """
    registry = OperationRegistry()
    register_core_operations(registry)
    reset_sinks()
    install_sink(CORE_MODULE_ID, CORE_AUDIT_SINK)

    check_audit_paths(registry)


def test_a_read_operation_needs_no_sink_at_dispatch_either(
    owner: WorkspaceContext, restore_sinks: None
) -> None:
    """The dispatch layer's other direction: an empty sink table does not stop a read.

    ``core.audit.list`` is itself the read used everywhere else in this file, so this
    is also what makes the two assertions above readable at all.
    """
    reset_sinks()

    outcome = dispatch(owner, AUDIT_LIST, {"limit": 1})

    assert outcome.ok, outcome

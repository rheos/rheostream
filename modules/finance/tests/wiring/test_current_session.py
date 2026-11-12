"""The in-process reminder session over core, with no database (RS-6c, ADR-0025).

- L2: each exception raised under ``call_in_transaction`` leaves as its fixed code
  with nothing chained, and a planted refusal detail reaches neither the error nor the
  log line; a storage error and any other exception pass unchanged; an operation
  outside the four is refused before any call; ``create`` reaches
  ``current.item.create``.
- L3: over a recording connection, a call in an owner block runs at the session role
  inside a savepoint and returns the block to ``finance_owner``; a refused call sends
  nothing after the rollback; a call outside a block sends no ``SET``; blocks do not
  nest.
- L7: a session refuses with its code before any statement when core refuses the
  service context, Current is not enabled or there is no consumer registry; otherwise
  the writer lock is its first statement, and it commits only through ``commit``.

Values are synthetic.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel
from rheo_contracts import StaleRecord
from rheo_core.boundary.context import Refusal
from rheo_core.modules.readiness import ModuleNotReady
from rheo_core.operations.refusals import OperationRefused
from rheo_core.storage.backend import StorageRefusal
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from rheo_finance import db
from rheo_finance.sinks import current
from rheo_finance.wiring import current as wiring

DETAIL = "synthetic planted detail Sample Card"
LOGGER = "rheo_finance.wiring.current"
SAVEPOINT = "SAVEPOINT"
RELEASE = "RELEASE SAVEPOINT"
ROLLBACK = "ROLLBACK TO SAVEPOINT"
SET_OWNER = f"SET LOCAL ROLE {db.OWNER_ROLE}"
RESET = "RESET ROLE"
LOCK = "SELECT pg_try_advisory_xact_lock(:key)"


class Answer:
    def scalar_one(self) -> bool:
        return True


class RecordingConnection:
    """Records each statement's text and each savepoint's end."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: object, parameters: object = None) -> Answer:
        self.statements.append(str(statement))
        return Answer()

    @contextmanager
    def begin_nested(self) -> Iterator[None]:
        self.statements.append(SAVEPOINT)
        try:
            yield
        except BaseException:
            self.statements.append(ROLLBACK)
            raise
        self.statements.append(RELEASE)


class CoreSession:
    def __init__(self) -> None:
        self.connection = RecordingConnection()
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


class Item(BaseModel):
    ref: str


def _session() -> tuple[wiring._Session, CoreSession]:
    core = CoreSession()
    session = wiring._Session(cast(Any, core), cast(Any, object()), cast(Any, object()))
    return session, core


def _patch_call(
    monkeypatch: pytest.MonkeyPatch, outcome: BaseModel | BaseException
) -> list[str]:
    names: list[str] = []

    def call(_ctx: object, _uow: object, name: str, _payload: object) -> BaseModel:
        names.append(name)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(wiring, "call_in_transaction", call)
    return names


# --- L2: fixed codes ------------------------------------------------------------------


MAPPED = [
    (ModuleNotReady("module_unavailable", DETAIL), "current_unavailable"),
    (OperationRefused("record_stale", DETAIL), "record_stale"),
    (OperationRefused("not_found", DETAIL), "refused"),
    (OperationRefused("area_retired", DETAIL), "refused"),
    (OperationRefused("consumers_missing", DETAIL), "refused"),
    (OperationRefused("operation_not_permitted", DETAIL), "refused"),
    (StaleRecord(DETAIL), "record_stale"),
    (StorageRefusal("workspace_unavailable", DETAIL), "current_unavailable"),
]


@pytest.mark.parametrize(("exc", "code"), MAPPED)
def test_a_refusal_leaves_as_its_fixed_code_alone(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exc: BaseException,
    code: str,
) -> None:
    _patch_call(monkeypatch, exc)
    session, _core = _session()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with pytest.raises(current.CurrentError) as caught:
            session.call("update", {"ref": "sample-1"})
    error = caught.value
    assert error.code == code and str(error) == code
    assert error.__cause__ is None and error.__context__ is None
    assert error.__suppress_context__ is True
    assert DETAIL not in repr(error)
    lines = [record.getMessage() for record in caplog.records]
    assert lines == [f"finance_current_call_refused operation=update code={code}"]
    assert not any(DETAIL in line for line in lines)


@pytest.mark.parametrize(
    "exc",
    [
        SQLAlchemyError(DETAIL),
        OperationalError(DETAIL, {}, Exception(DETAIL)),
        ValueError(DETAIL),
    ],
    ids=["storage", "operational", "other"],
)
def test_storage_errors_and_anything_else_pass_unchanged(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    _patch_call(monkeypatch, exc)
    session, _core = _session()
    with pytest.raises(type(exc)) as caught:
        session.call("get", {"ref": "sample-1"})
    assert caught.value is exc


def test_an_operation_outside_the_four_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = _patch_call(monkeypatch, Item(ref="sample-1"))
    session, core = _session()
    with pytest.raises(current.CurrentError) as caught:
        session.call("delete", {"ref": "sample-1"})
    assert caught.value.code == "protocol_error"
    assert names == [] and core.connection.statements == []


@pytest.mark.parametrize("operation", current.OPERATIONS)
def test_each_operation_reaches_its_current_name(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    names = _patch_call(monkeypatch, Item(ref="sample-1"))
    session, _core = _session()
    assert session.call(operation, {"ref": "sample-1"}) == {"ref": "sample-1"}
    assert names == [f"current.item.{operation}"]
    assert set(wiring.CURRENT_OPERATIONS) == {
        f"current.item.{each}" for each in current.OPERATIONS
    }


# --- L3: the role around each call ----------------------------------------------------


def test_a_call_in_a_block_runs_at_the_session_role_and_returns_to_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_call(monkeypatch, Item(ref="sample-1"))
    session, core = _session()
    with session.owner():
        session.call("get", {"ref": "sample-1"})
    assert core.connection.statements == [
        SET_OWNER,
        SAVEPOINT,
        RESET,
        RELEASE,
        SET_OWNER,
        RESET,
    ]


def test_a_refused_call_sends_nothing_after_the_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_call(monkeypatch, OperationRefused("not_found", DETAIL))
    session, core = _session()
    with session.owner():
        with pytest.raises(current.CurrentError):
            session.call("get", {"ref": "sample-1"})
        sent = list(core.connection.statements)
    assert sent == [SET_OWNER, SAVEPOINT, RESET, ROLLBACK]


def test_a_call_outside_a_block_sends_no_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_call(monkeypatch, Item(ref="sample-1"))
    session, core = _session()
    session.call("get", {"ref": "sample-1"})
    assert core.connection.statements == [SAVEPOINT, RESET, RELEASE]


def test_owner_blocks_do_not_nest() -> None:
    session, core = _session()
    with session.owner():
        with pytest.raises(RuntimeError):
            with session.owner():
                pass
    # The outer block still ended at the session role, and a new one opens.
    with session.owner():
        pass
    assert core.connection.statements == [SET_OWNER, RESET, SET_OWNER, RESET]


# --- L7: opening a session ----------------------------------------------------------


WORKSPACE = uuid.UUID(int=7)


def _patch_factories(monkeypatch: pytest.MonkeyPatch, service: object) -> CoreSession:
    core = CoreSession()

    @contextmanager
    def open_unit_of_work(_ctx: object) -> Iterator[CoreSession]:
        yield core

    monkeypatch.setattr(wiring, "open_unit_of_work", open_unit_of_work)
    monkeypatch.setattr(wiring, "context_for_event_consumer", lambda *_a, **_k: service)
    monkeypatch.setattr(
        wiring, "HandlerUnitOfWork", lambda uow, *, consumers: (uow, consumers)
    )
    return core


def _binding(consumers: object | None) -> wiring.CoreCurrent:
    core_ctx = SimpleNamespace(workspace_id=WORKSPACE)
    return wiring.CoreCurrent(cast(Any, core_ctx), consumers=cast(Any, consumers))


ENABLED = SimpleNamespace(enabled_modules=frozenset({"current", "finance"}))


@pytest.mark.parametrize(
    ("service", "consumers", "code"),
    [
        (Refusal("workspace_unavailable", DETAIL), object(), "current_unavailable"),
        (
            SimpleNamespace(enabled_modules=frozenset({"finance"})),
            object(),
            "not_configured",
        ),
        (ENABLED, None, "current_unavailable"),
    ],
    ids=["refusal", "current-not-enabled", "no-registry"],
)
def test_a_session_that_cannot_open_refuses_before_any_statement(
    monkeypatch: pytest.MonkeyPatch,
    service: object,
    consumers: object | None,
    code: str,
) -> None:
    core = _patch_factories(monkeypatch, service)
    with pytest.raises(current.CurrentError) as caught:
        with _binding(consumers).session():
            raise AssertionError("the session must not open")
    assert caught.value.code == code
    assert DETAIL not in repr(caught.value)
    assert core.connection.statements == [] and core.commits == 0


@pytest.mark.parametrize("commit", [False, True])
def test_the_writer_lock_comes_first_and_only_commit_commits(
    monkeypatch: pytest.MonkeyPatch, commit: bool
) -> None:
    core = _patch_factories(monkeypatch, ENABLED)
    with _binding(object()).session() as session:
        with session.owner():
            pass
        if commit:
            session.commit()
    assert core.connection.statements[0] == LOCK
    assert core.connection.statements[1:] == [SET_OWNER, RESET]
    assert core.commits == int(commit)

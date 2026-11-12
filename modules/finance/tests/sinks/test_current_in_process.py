"""The Current sink's in-process seam, with no database (RS-6c, ADR-0025).

- L1: bound, the spec is ``IN_PROCESS_SPEC`` whatever the settings file says; its
  digest differs from the configured and the unconfigured HTTPS spec; released, the
  HTTPS spec returns.
- L4: the scope rule. An item whose source pair is not this sink's, on the read, on
  the stale re-read or as a create result, gives ``not_owned`` with no update or
  status change sent; an owned item is updated at its revision.
- L5: over a transport the gate is asked before every call: closed, or approved at
  an HTTPS digest, gives ``sink_off`` and no call.
- L6: the commit rule. The session commits only when the key loop finished
  (``synced`` or ``partial``, ADR-0027); a session that cannot open gives ``pending``
  for ``not_configured`` and ``failed`` otherwise; a role other than owner or service
  is refused before the session opens.
- L7: a code the sink raises itself (``not_owned`` after a call returns) is logged
  once by the sync, code alone, with no key or item ref in the line; a code the
  session already logged is not logged again.

#546 and #548 (ADR-0027):

- the sync beats once per key, before the key's savepoint and its first block;
- a key refused for itself is set aside: its savepoint rolls back, its row keeps
  the code, the other keys sync and the session commits once (``partial``);
- a global code stops the sync with nothing committed and the later keys unattempted;
- ``KEY_LOCAL`` and ``GLOBAL`` split ``CURRENT_CODES``.

Values are synthetic.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import ClassVar, cast

import pytest
from sqlalchemy.exc import SQLAlchemyError

from rheo_finance import settings
from rheo_finance.clock import SystemClock
from rheo_finance.context import FinanceContext, FinancePaths, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.operations import OperationError, RoleRefused
from rheo_finance.reminders.desired import DesiredOut
from rheo_finance.reminders.policy import PayloadRefused
from rheo_finance.sinks import current
from rheo_finance.sinks.current_store import CreateSnapshot, SinkTicket
from rheo_finance.sinks.spec import SinkSpec, spec_digest

KEY = "finance:alpha:p1"
OTHER_KEY = "finance:beta:p1"
NO_UOW = cast(UnitOfWork, None)
IN_PROCESS_DIGEST = spec_digest(current.IN_PROCESS_SPEC)


def _item(
    *,
    revision: int = 1,
    kind: str | None = current.SOURCE_KIND,
    key: str | None = KEY,
) -> dict[str, object]:
    return {
        "ref": "sample-1",
        "revision": revision,
        "status": "open",
        "title": "Sample Utility due",
        "due_on": "2063-01-15",
        "priority": "normal",
        "source_kind": kind,
        "source_ref": key,
    }


class FakeTransport:
    """Answers each operation from a queue; records what was sent."""

    def __init__(self, *answers: dict[str, object] | current.CurrentError) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, operation: str, inputs: dict[str, object]) -> dict[str, object]:
        self.calls.append((operation, inputs))
        answer = self.answers.pop(0)
        if isinstance(answer, current.CurrentError):
            raise answer
        return answer

    def operations(self) -> list[str]:
        return [operation for operation, _ in self.calls]


def _open_gate(monkeypatch: pytest.MonkeyPatch, digest: str | None) -> None:
    monkeypatch.setattr(current, "enabled_digest", lambda _uow, _sink: digest)


def _row(key: str = KEY) -> SinkTicket:
    return SinkTicket(
        source_ref=key,
        state="post_pending",
        create=CreateSnapshot(
            title="Sample Utility due",
            body="Type: bill-due",
            due_on=dt.date(2063, 1, 15),
            priority="normal",
        ),
    )


# --- L1: the spec -----------------------------------------------------------------


class InertBinding:
    def session(self) -> AbstractContextManager[current.ReminderSession]:
        raise AssertionError("the spec provider opens no session")


def _configure_https(tmp_path: Path, *, configured: bool) -> None:
    if configured:
        credential = tmp_path / "synthetic.env"
        credential.write_text(
            'export CURRENT_API_URL="https://current.example.invalid"\n'
            'export CURRENT_ALERT_TOKEN="synthetic-token"\n'
        )
        text = f'[finance.reminders]\ncurrent_credential_file = "{credential}"\n'
    else:
        text = ""
    (tmp_path / settings.SETTINGS_FILE_NAME).write_text(text)


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "bare"])
def test_bound_spec_is_the_in_process_spec_and_never_an_https_one(
    tmp_path: Path, configured: bool
) -> None:
    _configure_https(tmp_path, configured=configured)
    https = current.spec(NO_UOW)
    assert ("token_var", "CURRENT_ALERT_TOKEN") in https.data_inputs
    assert bool(https.hosts) is configured
    token = current.use_in_process(InertBinding())
    try:
        bound = current.spec(NO_UOW)
    finally:
        current.release_in_process(token)
    assert bound == current.IN_PROCESS_SPEC
    assert bound.hosts == () and bound.key_file is None
    # The same four request templates (the spec sorts them).
    assert bound.requests == https.requests
    assert set(bound.requests) == set(current.REQUESTS)
    assert spec_digest(bound) != spec_digest(https)
    assert current.in_process() is None
    assert current.spec(NO_UOW) == https


# --- L4: scope ----------------------------------------------------------------------


@pytest.fixture
def gate_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _open_gate(monkeypatch, IN_PROCESS_DIGEST)


def _sink(transport: FakeTransport) -> current.CurrentSink:
    return current.CurrentSink(NO_UOW, transport=transport)


@pytest.mark.parametrize(
    ("kind", "key"),
    [("email", KEY), (current.SOURCE_KIND, OTHER_KEY), (None, None)],
    ids=["foreign-kind", "other-key", "no-pair"],
)
def test_a_foreign_item_read_first_is_never_changed(
    gate_open: None, kind: str | None, key: str | None
) -> None:
    transport = FakeTransport({"item": _item(kind=kind, key=key)})
    with pytest.raises(current.CurrentError) as caught:
        _sink(transport).mutate("update", "sample-1", {"title": "x"}, source_ref=KEY)
    assert caught.value.code == "not_owned"
    assert transport.operations() == ["get"]


def test_a_foreign_fresh_item_is_never_changed(gate_open: None) -> None:
    transport = FakeTransport()
    fresh = current.RemoteItem.model_validate_json(json.dumps(_item(kind="email")))
    with pytest.raises(current.CurrentError) as caught:
        _sink(transport).mutate(
            "set_status", "sample-1", {"status": "done"}, fresh=fresh, source_ref=KEY
        )
    assert caught.value.code == "not_owned"
    assert transport.calls == []


def test_a_foreign_item_on_the_stale_reread_is_never_changed(gate_open: None) -> None:
    transport = FakeTransport(
        {"item": _item()},
        current.CurrentError("record_stale"),
        {"item": _item(revision=2, key=OTHER_KEY)},
    )
    with pytest.raises(current.CurrentError) as caught:
        _sink(transport).mutate("update", "sample-1", {"title": "x"}, source_ref=KEY)
    assert caught.value.code == "not_owned"
    assert transport.operations() == ["get", "update", "get"]


@pytest.mark.parametrize("ref_only", [False, True], ids=["item", "ref-only"])
def test_a_create_answered_with_another_pair_is_not_owned(
    gate_open: None, ref_only: bool
) -> None:
    foreign = _item(key=OTHER_KEY)
    answers: list[dict[str, object] | current.CurrentError] = (
        [{"action": "grouped", "ref": "sample-1"}, {"item": foreign}]
        if ref_only
        else [{"action": "grouped", "item": foreign}]
    )
    transport = FakeTransport(*answers)
    with pytest.raises(current.CurrentError) as caught:
        _sink(transport).create(_row())
    assert caught.value.code == "not_owned"
    sent = transport.calls[0][1]
    assert sent["source_kind"] == current.SOURCE_KIND
    assert sent["source_ref"] == sent["fingerprint"] == KEY


def test_an_owned_item_is_updated_at_its_revision(gate_open: None) -> None:
    transport = FakeTransport(
        {"item": _item(revision=3)},
        {"item": _item(revision=4) | {"title": "Sample Card due"}},
    )
    changed = _sink(transport).mutate(
        "update", "sample-1", {"title": "Sample Card due"}, source_ref=KEY
    )
    assert changed.revision == 4 and changed.title == "Sample Card due"
    assert transport.calls[1] == (
        "update",
        {"ref": "sample-1", "revision": 3, "title": "Sample Card due"},
    )


def test_an_owned_create_answer_is_returned(gate_open: None) -> None:
    transport = FakeTransport({"action": "created", "item": _item()})
    created = _sink(transport).create(_row())
    assert (created.source_kind, created.source_ref) == (current.SOURCE_KIND, KEY)


def test_a_reply_without_the_source_pair_is_a_protocol_error(gate_open: None) -> None:
    bare = {k: v for k, v in _item().items() if k not in {"source_kind", "source_ref"}}
    transport = FakeTransport({"item": bare})
    with pytest.raises(current.CurrentError) as caught:
        _sink(transport).get("sample-1")
    assert caught.value.code == "protocol_error"


# --- L5: the gate over a transport --------------------------------------------------


def _https_digest() -> str:
    return spec_digest(
        SinkSpec(
            name="current",
            hosts=("current.example.invalid",),
            requests=current.REQUESTS,
            key_file="/synthetic/keys/current.env",
            payload_policy_version=1,
            data_inputs=(("token_var", "CURRENT_ALERT_TOKEN"),),
        )
    )


@pytest.mark.parametrize("approved", ["closed", "https"])
@pytest.mark.parametrize("operation", ["create", "get", "update", "set_status"])
def test_the_transport_is_never_called_unless_the_in_process_spec_is_approved(
    monkeypatch: pytest.MonkeyPatch, approved: str, operation: str
) -> None:
    _open_gate(monkeypatch, None if approved == "closed" else _https_digest())
    transport = FakeTransport({"item": _item()})
    with pytest.raises(current.CurrentError) as caught:
        _sink(transport)._call(operation, {"ref": "sample-1"})
    assert caught.value.code == "sink_off"
    assert transport.calls == []


def test_an_operation_outside_the_four_is_refused_before_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def gate(_uow: UnitOfWork, _sink: str) -> str:
        raise AssertionError("the gate is not asked for a refused operation")

    monkeypatch.setattr(current, "enabled_digest", gate)
    transport = FakeTransport()
    with pytest.raises(current.CurrentError) as caught:
        _sink(transport)._call("delete", {"ref": "sample-1"})
    assert caught.value.code == "protocol_error"
    assert transport.calls == []


# --- L6: the commit rule ------------------------------------------------------------


class FakeSession:
    """Records each key savepoint as ``key``, then ``released`` or ``rolled back``."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.commits = 0
        self.events = events if events is not None else []

    @contextmanager
    def owner(self) -> Iterator[UnitOfWork]:
        yield NO_UOW

    @contextmanager
    def key(self) -> Iterator[None]:
        self.events.append("key")
        try:
            yield
        except BaseException:
            self.events.append("rolled back")
            raise
        self.events.append("released")

    def commit(self) -> None:
        self.commits += 1

    def call(self, operation: str, inputs: dict[str, object]) -> dict[str, object]:
        raise AssertionError("the patched sync makes no call")


class FakeBinding:
    def __init__(self, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.session_ = FakeSession()
        self.opened = 0
        self.closed = 0

    @contextmanager
    def session(self) -> Iterator[current.ReminderSession]:
        if self.refuse is not None:
            raise current.CurrentError(self.refuse)
        self.opened += 1
        try:
            yield self.session_
        finally:
            self.closed += 1


def _context(role: Role = Role.SERVICE) -> FinanceContext:
    return FinanceContext(
        settings=settings.parse_settings(""),
        clock=SystemClock(),
        paths=FinancePaths(Path(".")),
        role=role,
        snapshot_provider=lambda _u, _r: None,
    )


@contextmanager
def _bound(binding: FakeBinding) -> Iterator[None]:
    token = current.use_in_process(binding)
    try:
        yield
    finally:
        current.release_in_process(token)


def _patch_sync(
    monkeypatch: pytest.MonkeyPatch, outcome: current.SyncResult | BaseException
) -> list[current._Steps]:
    seen: list[current._Steps] = []

    def fake(_ctx: FinanceContext, steps: current._Steps) -> current.SyncResult:
        seen.append(steps)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(current, "_sync", fake)
    return seen


@pytest.mark.parametrize(
    ("outcome", "commits"),
    [
        (current.SyncResult("synced"), 1),
        (current.SyncResult("partial", "refused"), 1),
        (current.SyncResult("pending", "sink_off"), 0),
        (current.SyncResult("failed", "current_unavailable"), 0),
    ],
    ids=["synced", "partial", "pending", "failed"],
)
def test_the_session_commits_only_when_the_key_loop_finished(
    monkeypatch: pytest.MonkeyPatch, outcome: current.SyncResult, commits: int
) -> None:
    seen = _patch_sync(monkeypatch, outcome)
    binding = FakeBinding()
    with _bound(binding):
        assert current.sync_reminders(_context()) == outcome
    assert binding.session_.commits == commits
    assert (binding.opened, binding.closed) == (1, 1)
    # The steps run on the session: its owner blocks, a sink over it.
    sink = seen[0].sink(NO_UOW)
    assert sink.transport is binding.session_
    assert seen[0].begin == binding.session_.owner
    assert seen[0].key == binding.session_.key


def test_a_payload_refusal_leaves_the_session_uncommitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sync(monkeypatch, PayloadRefused("synthetic refusal"))
    binding = FakeBinding()
    with _bound(binding), pytest.raises(PayloadRefused):
        current.sync_reminders(_context())
    assert binding.session_.commits == 0 and binding.closed == 1


def test_a_storage_error_leaves_the_session_uncommitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sync(monkeypatch, SQLAlchemyError("synthetic storage text"))
    binding = FakeBinding()
    with _bound(binding), pytest.raises(OperationError) as caught:
        current.sync_reminders(_context())
    assert str(caught.value) == "current storage failed"
    assert binding.session_.commits == 0 and binding.closed == 1


@pytest.mark.parametrize(
    "expected",
    [
        current.SyncResult("pending", "not_configured"),
        current.SyncResult("failed", "current_unavailable"),
    ],
    ids=["not-configured", "unavailable"],
)
def test_a_session_that_cannot_open_calls_nothing(
    monkeypatch: pytest.MonkeyPatch, expected: current.SyncResult
) -> None:
    seen = _patch_sync(monkeypatch, current.SyncResult("synced"))
    assert expected.reason is not None
    with _bound(FakeBinding(refuse=expected.reason)):
        assert current.sync_reminders(_context()) == expected
    assert seen == []


def test_a_member_is_refused_before_the_session_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_sync(monkeypatch, current.SyncResult("synced"))
    binding = FakeBinding()
    with _bound(binding), pytest.raises(RoleRefused):
        current.sync_reminders(_context(Role.MEMBER))
    assert binding.opened == 0 and seen == []


def test_nothing_bound_keeps_the_https_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_sync(monkeypatch, current.SyncResult("synced"))
    assert current.in_process() is None
    assert current.sync_reminders(_context()) == current.SyncResult("synced")
    assert seen[0].sink(NO_UOW).transport is None
    # No key savepoint over HTTPS: a refusal there stops the sync.
    assert seen[0].key is None


# --- L7: the sync logs a code nobody logged, once -----------------------------------

PLANTED_KEY = "finance:plantedkeytoken:p1"
PLANTED_REF = "planted-item-ref-7"
SYNC_LOGGER = "rheo_finance.sinks.current"


class _MemoryStore:
    """The sink rows in a dict, in place of ``PostgresCurrentTicketRepository``."""

    rows: ClassVar[dict[str, SinkTicket]] = {}

    def __init__(self, _uow: UnitOfWork) -> None:
        pass

    def all(self) -> tuple[SinkTicket, ...]:
        return tuple(self.rows.values())

    def get(self, source_ref: str) -> SinkTicket | None:
        return self.rows.get(source_ref)

    def insert_if_absent(self, ticket: SinkTicket) -> bool:
        return self.rows.setdefault(ticket.source_ref, ticket) is ticket

    def save(self, ticket: SinkTicket) -> None:
        self.rows[ticket.source_ref] = ticket


class _NoFacts:
    def __init__(self, _uow: UnitOfWork) -> None:
        pass

    def registered_payees(self) -> frozenset[str]:
        return frozenset()


@contextmanager
def _block() -> Iterator[UnitOfWork]:
    yield NO_UOW


@pytest.mark.parametrize(
    ("answer", "code", "lines"),
    [
        # The read returns an item with another pair: the scope check raises
        # ``not_owned`` after the call, where no session logs it.
        (
            {"item": _item(key=OTHER_KEY) | {"ref": PLANTED_REF, "title": "x"}},
            "not_owned",
            ["finance_current_sync_stopped code=not_owned"],
        ),
        # A code nobody logged (the HTTPS path's) is logged by the sync.
        (
            current.CurrentError("refused"),
            "refused",
            ["finance_current_sync_stopped code=refused"],
        ),
        # The session already logged this refusal: no second line.
        (current.CurrentError("refused", logged=True), "refused", []),
    ],
    ids=["not-owned", "unlogged", "session-logged"],
)
def test_the_sync_logs_a_code_once_and_never_the_key_or_item(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    answer: dict[str, object] | current.CurrentError,
    code: str,
    lines: list[str],
) -> None:
    _open_gate(monkeypatch, IN_PROCESS_DIGEST)
    rows = {
        PLANTED_KEY: _row(PLANTED_KEY).model_copy(
            update={"external_id": PLANTED_REF, "state": "open"}
        )
    }
    monkeypatch.setattr(_MemoryStore, "rows", rows)
    monkeypatch.setattr(current, "PostgresCurrentTicketRepository", _MemoryStore)
    monkeypatch.setattr(current, "PostgresReminderRepository", _NoFacts)
    monkeypatch.setattr(
        current, "desired_tickets", lambda _ctx, _facts: DesiredOut(tickets=[])
    )
    transport = FakeTransport(answer)
    steps = current._Steps(
        begin=_block,
        sink=lambda uow: current.CurrentSink(uow, transport=transport),
        unready=lambda _uow: None,
    )
    with caplog.at_level(logging.DEBUG):
        result = current._sync(_context(), steps)
    assert result == current.SyncResult("failed", code)
    assert transport.operations() == ["get"]
    # The row annotation still records the code (it rolls back only in core mode).
    assert rows[PLANTED_KEY].last_error_code == code
    assert [r.getMessage() for r in caplog.records if r.name == SYNC_LOGGER] == lines
    for record in caplog.records:
        assert record.exc_info is None
        for planted in (PLANTED_KEY, PLANTED_REF):
            assert planted not in record.getMessage()
            assert planted not in repr(record.args)
    assert PLANTED_KEY not in caplog.text and PLANTED_REF not in caplog.text


# --- #546 L6: the sync beats once per key -------------------------------------------

THREE_KEYS = ("finance:alpha:p1", "finance:beta:p1", "finance:gamma:p1")


def _settled(key: str) -> SinkTicket:
    """A row acknowledged as done, so its key's pass reads it and returns."""
    row = _row(key)
    return row.model_copy(
        update={
            "state": "done",
            "last_title": row.create.title,
            "last_due_on": row.create.due_on,
            "last_priority": row.create.priority,
            "last_status": "done",
        }
    )


@pytest.mark.parametrize("in_process", [False, True], ids=["https", "in-process"])
def test_the_sync_beats_before_each_key(
    monkeypatch: pytest.MonkeyPatch, in_process: bool
) -> None:
    events: list[str] = []
    rows = {key: _settled(key) for key in THREE_KEYS}
    monkeypatch.setattr(_MemoryStore, "rows", rows)
    monkeypatch.setattr(current, "PostgresCurrentTicketRepository", _MemoryStore)
    monkeypatch.setattr(current, "PostgresReminderRepository", _NoFacts)
    monkeypatch.setattr(
        current, "desired_tickets", lambda _ctx, _facts: DesiredOut(tickets=[])
    )

    @contextmanager
    def begin() -> Iterator[UnitOfWork]:
        events.append("begin")
        yield NO_UOW

    session = FakeSession(events)
    steps = current._Steps(
        begin=begin,
        sink=lambda uow: current.CurrentSink(
            uow, transport=session if in_process else None
        ),
        unready=lambda _uow: None,
        key=session.key if in_process else None,
    )
    ctx = dataclasses.replace(_context(), progress=lambda: events.append("beat"))

    assert current._sync(ctx, steps) == current.SyncResult("synced")
    # The preamble's block, then a beat before each key's savepoint (in process)
    # and its first block.
    per_key = ["beat", "key", "begin", "released"] if in_process else ["beat", "begin"]
    assert events == ["begin"] + per_key * 3


# --- #548 L7, L8, L11: a refused key is set aside -----------------------------------


class _Session(FakeSession):
    """A reminder session whose calls go to a fake transport."""

    def __init__(self, transport: FakeTransport) -> None:
        super().__init__()
        self.transport = transport

    def call(self, operation: str, inputs: dict[str, object]) -> dict[str, object]:
        return self.transport.call(operation, inputs)


class _Binding:
    def __init__(self, session: _Session) -> None:
        self.session_ = session

    @contextmanager
    def session(self) -> Iterator[current.ReminderSession]:
        yield self.session_


def _open_rows(monkeypatch: pytest.MonkeyPatch) -> dict[str, SinkTicket]:
    """Alpha, beta and gamma synced open; nothing desired now, so each is closed."""
    rows = {
        key: _row(key).model_copy(
            update={
                "external_id": f"ref-{key.split(':')[1]}",
                "state": "open",
                "last_title": "Sample Utility due",
                "last_due_on": dt.date(2063, 1, 15),
                "last_priority": "normal",
                "last_status": "open",
            }
        )
        for key in THREE_KEYS
    }
    monkeypatch.setattr(_MemoryStore, "rows", rows)
    monkeypatch.setattr(current, "PostgresCurrentTicketRepository", _MemoryStore)
    monkeypatch.setattr(current, "PostgresReminderRepository", _NoFacts)
    monkeypatch.setattr(
        current, "desired_tickets", lambda _ctx, _facts: DesiredOut(tickets=[])
    )
    monkeypatch.setattr(current, "is_enabled", lambda _uow, _sink: True)
    _open_gate(monkeypatch, IN_PROCESS_DIGEST)
    return rows


def _closing(key: str) -> list[dict[str, object]]:
    """The answers that close ``key``'s item: the read, the status change's own
    fresh read, then the status change."""
    ref = f"ref-{key.split(':')[1]}"
    return [
        {"item": _item(key=key) | {"ref": ref}},
        {"item": _item(key=key) | {"ref": ref}},
        {"item": _item(revision=2, key=key) | {"ref": ref, "status": "done"}},
    ]


CLOSE = ["get", "get", "set_status"]


ACKNOWLEDGING = (
    "state",
    "external_id",
    "last_title",
    "last_due_on",
    "last_priority",
    "last_status",
    "last_success_at",
    "settled_at",
    "create",
)


def test_a_key_refused_for_itself_is_set_aside_and_the_others_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _open_rows(monkeypatch)
    alpha, beta, gamma = THREE_KEYS
    before = rows[beta]
    transport = FakeTransport(
        *_closing(alpha),
        current.CurrentError("refused", logged=True),
        *_closing(gamma),
    )
    session = _Session(transport)
    with _bound(cast(FakeBinding, _Binding(session))):
        result = current.sync_reminders(_context())

    assert result == current.SyncResult("partial", "refused")
    assert session.commits == 1
    assert session.events == [
        "key",
        "released",
        "key",
        "rolled back",
        "key",
        "released",
    ]
    for key in (alpha, gamma):
        assert rows[key].last_status == "done" and rows[key].state == "done"
        assert rows[key].last_error_code is None
    after = rows[beta]
    assert after.last_error_code == "refused"
    assert after.last_attempt_at is not None and before.last_attempt_at is None
    assert {name: getattr(after, name) for name in ACKNOWLEDGING} == {
        name: getattr(before, name) for name in ACKNOWLEDGING
    }
    assert transport.operations() == [*CLOSE, "get", *CLOSE]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("sink_off", current.SyncResult("pending", "sink_off")),
        ("not_configured", current.SyncResult("pending", "not_configured")),
        ("current_unavailable", current.SyncResult("failed", "current_unavailable")),
    ],
)
def test_a_global_code_stops_the_sync_and_commits_nothing(
    monkeypatch: pytest.MonkeyPatch, code: str, expected: current.SyncResult
) -> None:
    _open_rows(monkeypatch)
    alpha, _beta, _gamma = THREE_KEYS
    transport = FakeTransport(*_closing(alpha), current.CurrentError(code))
    session = _Session(transport)
    with _bound(cast(FakeBinding, _Binding(session))):
        assert current.sync_reminders(_context()) == expected
    assert session.commits == 0
    # Gamma is never attempted.
    assert transport.operations() == [*CLOSE, "get"]
    assert session.events == ["key", "released", "key", "rolled back"]


def test_the_key_local_and_global_codes_split_every_code() -> None:
    assert current.KEY_LOCAL | current.GLOBAL == current.CURRENT_CODES
    assert current.KEY_LOCAL & current.GLOBAL == set()
    assert current.KEY_LOCAL == {
        "refused",
        "record_stale",
        "not_owned",
        "not_found",
        "incomplete_convergence",
        "protocol_error",
    }
    assert len(current.CURRENT_CODES) == 13

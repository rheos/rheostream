"""The Current sink's in-process seam, with no database (RS-6c, ADR-0025).

- L1: bound, the spec is ``IN_PROCESS_SPEC`` whatever the settings file says; its
  digest differs from the configured and the unconfigured HTTPS spec; released, the
  HTTPS spec returns.
- L4: the scope rule. An item whose source pair is not this sink's, on the read, on
  the stale re-read or as a create result, gives ``not_owned`` with no update or
  status change sent; an owned item is updated at its revision.
- L5: over a transport the gate is asked before every call: closed, or approved at
  an HTTPS digest, gives ``sink_off`` and no call.
- L6: the commit rule. The session commits only when the sync is ``synced``; a
  session that cannot open gives ``pending`` for ``not_configured`` and ``failed``
  otherwise; a role other than owner or service is refused before the session opens.

Values are synthetic.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.exc import SQLAlchemyError

from rheo_finance import settings
from rheo_finance.clock import SystemClock
from rheo_finance.context import FinanceContext, FinancePaths, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.operations import OperationError, RoleRefused
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
    def __init__(self) -> None:
        self.commits = 0

    @contextmanager
    def owner(self) -> Iterator[UnitOfWork]:
        yield NO_UOW

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
        (current.SyncResult("pending", "sink_off"), 0),
        (current.SyncResult("failed", "refused"), 0),
    ],
    ids=["synced", "pending", "failed"],
)
def test_the_session_commits_only_a_synced_result(
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

"""Current's gated operation client and durable desired-state reconciliation.

Two transports reach Current's four item operations. With nothing bound, each is an
HTTPS POST through ``http.Client``, and the sync commits a short owner transaction
around each step. Under core, ``rheo_finance.wiring.current`` binds an in-process
client (``use_in_process``): each operation runs through core's
``call_in_transaction`` in one transaction that also holds this sink's rows, with a
savepoint per reminder key (ADR-0025, ADR-0027). A key refused for itself
(``KEY_LOCAL``) is set aside with its code and the others sync; a global code stops
and rolls back the sync; the sync commits when its key loop finishes. Over HTTPS a
refusal still stops the keys after it. The gate is asked before every call on either
path, and an update or status change reaches only an item that carries this sink's
own source pair.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import re
import shlex
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from rheo_finance.clock import SystemClock
from rheo_finance.context import FinanceContext, FinancePaths, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.http import Client, RequestFailed
from rheo_finance.operations import OperationError, RoleRefused, owner_transaction
from rheo_finance.reminders.desired import DesiredTicket, desired_tickets
from rheo_finance.reminders.policy import (
    PayloadPolicy,
    PayloadRefused,
    build_payload,
    safe_token,
)
from rheo_finance.reminders.store import PostgresReminderRepository
from rheo_finance.settings import Settings, SettingsError, load_settings
from rheo_finance.sinks.current_store import (
    CreateSnapshot,
    PostgresCurrentTicketRepository,
    SinkTicket,
    Status,
)
from rheo_finance.sinks.gate import (
    AllowlistRefused,
    HostRefused,
    SinkDisabled,
    enabled_digest,
    is_enabled,
)
from rheo_finance.sinks.preview import PREVIEW_BUILDERS, PreviewItem, SinkPreview
from rheo_finance.sinks.spec import SINK_SPECS, RequestTemplate, SinkSpec, spec_digest

SINK = "current"
SOURCE_KIND: Final = "float.obligation"
OPERATIONS = ("create", "get", "update", "set_status")
REQUESTS = tuple(
    RequestTemplate(method="POST", path=f"/api/v1/operations/current.item.{op}")
    for op in OPERATIONS
)
ALLOWLIST = tuple((r.method, r.path) for r in REQUESTS)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LOG = logging.getLogger(__name__)

# The spec an approval pins when the sink runs in process. The four request templates
# are the operations the transport may call; no host and no key file. Every HTTPS spec
# carries a ``token_var`` data input, so no HTTPS approval can open this one, nor the
# reverse.
IN_PROCESS_SPEC: Final = SinkSpec(
    name=SINK,
    hosts=(),
    requests=REQUESTS,
    key_file=None,
    payload_policy_version=1,
    data_inputs=(("transport", "in_process"),),
)


class CurrentError(Exception):
    """Fixed local codes only; remote text and HTTP exception text never escape.

    ``current_unavailable`` (Current or its session cannot be reached in process),
    ``not_owned`` (an item that does not carry this sink's source pair) and, in process
    only, ``not_found`` (Current has no item with that id) join the HTTPS path's
    codes. ``logged`` marks a code whose log line is already written (the
    in-process session logs each refused call), so the sync logs it only otherwise.
    """

    def __init__(self, code: str, *, logged: bool = False) -> None:
        self.code = code
        self.logged = logged
        super().__init__(code)


# Every code a ``CurrentError`` carries.
CURRENT_CODES: Final = frozenset(
    {
        "configuration_error",
        "not_configured",
        "unauthorized",
        "refused",
        "record_stale",
        "protocol_error",
        "sink_off",
        "request_refused",
        "network_error",
        "incomplete_convergence",
        "current_unavailable",
        "not_owned",
        "not_found",
    }
)
# A refusal that belongs to one reminder key: in process that key is set aside and the
# sync goes on with the others (ADR-0027).
KEY_LOCAL: Final = frozenset(
    {
        "refused",
        "record_stale",
        "not_owned",
        "not_found",
        "incomplete_convergence",
        "protocol_error",
    }
)
# A condition of the whole sync (a closed gate, Current unconfigured or unavailable,
# the HTTPS transport's own failures): it stops the sync and rolls it all back.
GLOBAL: Final = CURRENT_CODES - KEY_LOCAL
LINE_STOP: Final = "finance_current_sync_stopped code=%s"
LINE_SET_ASIDE: Final = "finance_current_key_set_aside code=%s"


class CurrentTransport(Protocol):
    """One of the four operations, by its short name; raises only ``CurrentError``."""

    def call(self, operation: str, inputs: dict[str, object]) -> dict[str, object]: ...


class ReminderSession(CurrentTransport, Protocol):
    """One transaction for a whole sync. ``owner`` is a block as ``finance_owner`` on
    it (not a transaction); ``key`` is one reminder key's savepoint, taken outside any
    owner block, which rolls back alone when the key is refused; leaving the session
    without ``commit`` rolls it all back."""

    def owner(self) -> AbstractContextManager[UnitOfWork]: ...

    def key(self) -> AbstractContextManager[None]: ...

    def commit(self) -> None: ...


class InProcessCurrent(Protocol):
    """What the wiring binds under core: a factory of reminder sessions."""

    def session(self) -> AbstractContextManager[ReminderSession]: ...


_IN_PROCESS: contextvars.ContextVar[InProcessCurrent | None] = contextvars.ContextVar(
    "finance_current_in_process", default=None
)

InProcessToken = contextvars.Token[InProcessCurrent | None]


def use_in_process(binding: InProcessCurrent) -> InProcessToken:
    """Bind ``binding`` for this context; ``release_in_process`` undoes it."""
    return _IN_PROCESS.set(binding)


def release_in_process(token: InProcessToken) -> None:
    _IN_PROCESS.reset(token)


def in_process() -> InProcessCurrent | None:
    return _IN_PROCESS.get()


@dataclass(frozen=True, repr=False)
class Configuration:
    base_url: str
    host: str


def _env(settings: Settings) -> dict[str, str]:
    filename = settings.get("finance.reminders.current_credential_file")
    if not isinstance(filename, str):
        return {}
    failure = False
    variables: dict[str, str] = {}
    try:
        for line in (
            Path(filename).expanduser().read_text(encoding="utf-8").splitlines()
        ):
            words = shlex.split(line, comments=True)
            if not words:
                continue
            if len(words) != 2 or words[0] != "export":
                raise ValueError
            name, equals, value = words[1].partition("=")
            if not equals or not _ENV_NAME.fullmatch(name) or name in variables:
                raise ValueError
            variables[name] = value
    except (OSError, UnicodeError, ValueError):
        failure = True
    if failure:
        raise CurrentError("configuration_error")
    return variables


def configuration(settings: Settings) -> Configuration | None:
    base = settings.get("finance.reminders.current_base_url")
    if not isinstance(base, str):
        base = _env(settings).get("CURRENT_API_URL")
    if not base:
        return None
    failure = False
    host = ""
    try:
        parts = urlsplit(base)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError
        host = parts.hostname.lower()
        if parts.port is not None:
            host += f":{parts.port}"
    except ValueError:
        failure = True
    if failure:
        raise CurrentError("configuration_error")
    return Configuration(base.rstrip("/"), host)


def spec(_uow: UnitOfWork) -> SinkSpec:
    if in_process() is not None:
        # Under core the HTTP keys are ignored: the tick, the CLI approval and derive
        # all compute this one spec.
        return IN_PROCESS_SPEC
    settings = load_settings()
    try:
        config = configuration(settings)
    except CurrentError:
        # Unavailable outbound configuration must not block local derivation.
        # No host is allowed until configuration can be resolved again.
        config = None
    credential = settings.get("finance.reminders.current_credential_file")
    failure = False
    try:
        declared = SinkSpec(
            name=SINK,
            hosts=(config.host,) if config else (),
            requests=REQUESTS,
            key_file=credential if isinstance(credential, str) else None,
            payload_policy_version=1,
            data_inputs=(
                ("token_var", settings.get_str("finance.reminders.current_token_var")),
            ),
        )
    except ValidationError:
        failure = True
    if failure:
        raise CurrentError("configuration_error")
    return declared


def https_client(uow: UnitOfWork, declared: SinkSpec) -> Client:
    return Client(SINK, declared, ALLOWLIST, uow=uow)


make_client: Callable[[UnitOfWork, SinkSpec], Client] = https_client


class RemoteItem(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    ref: str = Field(min_length=1)
    revision: int = Field(ge=1)
    status: Literal["open", "waiting", "snoozed", "done"]
    title: str
    due_on: dt.date | None
    priority: Literal["critical", "high", "normal", "low"]
    # Required, so a reply without the pair is a protocol error: the scope check
    # reads them.
    source_kind: str | None
    source_ref: str | None


def _owned(item: RemoteItem, key: str) -> RemoteItem:
    """``item`` when it carries this sink's source pair for ``key``, else
    ``not_owned``: a service may edit any Current item, so the scope is ours."""
    if item.source_kind != SOURCE_KIND or item.source_ref != key:
        raise CurrentError("not_owned")
    return item


class RemoteError(BaseModel):
    model_config = ConfigDict(extra="ignore")
    error_code: str


class Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    state: str
    operation_id: str | None
    result: dict[str, object] | None = None
    error: RemoteError | None = None


class CurrentSink:
    """The four operations over ``transport``, or over HTTPS when it is None."""

    def __init__(
        self, uow: UnitOfWork, transport: CurrentTransport | None = None
    ) -> None:
        self.uow = uow
        self.transport = transport

    def _call(self, operation: str, inputs: dict[str, object]) -> dict[str, object]:
        if operation not in OPERATIONS:
            raise CurrentError("protocol_error")
        if self.transport is None:
            return self._https_call(operation, inputs)
        # The gate before every in-process call, as http.Client asks it before every
        # socket: only an approval pinned to the in-process spec opens it.
        if enabled_digest(self.uow, SINK) != spec_digest(IN_PROCESS_SPEC):
            raise CurrentError("sink_off")
        return self.transport.call(operation, inputs)

    def _https_call(
        self, operation: str, inputs: dict[str, object]
    ) -> dict[str, object]:
        settings = load_settings()
        config = configuration(settings)
        if config is None:
            raise CurrentError("not_configured")
        name = settings.get_str("finance.reminders.current_token_var")
        if not _ENV_NAME.fullmatch(name):
            raise CurrentError("configuration_error")
        token = _env(settings).get(name)
        if not token:
            raise CurrentError("not_configured")
        error: str | None = None
        result: dict[str, object] | None = None
        try:
            client = make_client(self.uow, spec(self.uow))
            response = client.request(
                "POST",
                f"{config.base_url}/api/v1/operations/current.item.{operation}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                body=json.dumps(inputs).encode("utf-8"),
            )
            if response.status == 401:
                error = "unauthorized"
            else:
                try:
                    envelope = Envelope.model_validate_json(response.body)
                except ValidationError:
                    if response.status == 200:
                        raise
                    error = "refused"
                else:
                    if (
                        response.status == 200
                        and envelope.state == "succeeded"
                        and envelope.result is not None
                    ):
                        result = envelope.result
                    elif envelope.error is not None:
                        error = (
                            "record_stale"
                            if response.status == 409
                            and envelope.error.error_code == "record_stale"
                            else "refused"
                        )
                    else:
                        error = "protocol_error"
        except SinkDisabled:
            error = "sink_off"
        except (AllowlistRefused, HostRefused):
            error = "request_refused"
        except RequestFailed:
            error = "network_error"
        except (ValidationError, ValueError):
            error = "protocol_error"
        if error:
            raise CurrentError(error)
        if result is None:
            raise CurrentError("protocol_error")
        return result

    def _item(self, operation: str, inputs: dict[str, object]) -> RemoteItem:
        result = self._call(operation, inputs)
        raw = result.get("item", result)
        failure = False
        try:
            # JSON validation admits ISO dates while preserving strict numeric types.
            item = RemoteItem.model_validate_json(json.dumps(raw))
        except (ValidationError, ValueError, TypeError):
            failure = True
        if failure:
            raise CurrentError("protocol_error")
        return item

    def get(self, ref: str) -> RemoteItem:
        return self._item("get", {"ref": ref})

    def create(self, row: SinkTicket) -> RemoteItem:
        result = self._call(
            "create",
            {
                **row.create.model_dump(mode="json"),
                "area": "bills",
                "provenance": "agent",
                "source_kind": SOURCE_KIND,
                "source_ref": row.source_ref,
                "fingerprint": row.source_ref,
            },
        )
        raw = result.get("item")
        if raw is None:
            ref = result.get("ref")
            if not isinstance(ref, str) or not ref:
                raise CurrentError("protocol_error")
            return _owned(self.get(ref), row.source_ref)
        failure = False
        try:
            item = RemoteItem.model_validate_json(json.dumps(raw))
        except (ValidationError, ValueError, TypeError):
            failure = True
        if failure:
            raise CurrentError("protocol_error")
        return _owned(item, row.source_ref)

    def mutate(
        self,
        operation: str,
        ref: str,
        fields: dict[str, object],
        fresh: RemoteItem | None = None,
        *,
        source_ref: str,
    ) -> RemoteItem:
        """``operation`` on ``ref`` at the revision just read, retried once after a
        stale answer. Each item read is checked against ``source_ref`` first, and the
        revision compare-and-set makes the checked item the one changed."""
        item = fresh if fresh is not None else self.get(ref)
        for attempt in range(2):
            _owned(item, source_ref)
            try:
                return self._item(
                    operation, {"ref": ref, "revision": item.revision, **fields}
                )
            except CurrentError as exc:
                if exc.code != "record_stale" or attempt:
                    raise
            item = self.get(ref)
        raise CurrentError("record_stale")


@dataclass(frozen=True)
class Target:
    create: CreateSnapshot
    status: Status


def target(ticket: DesiredTicket, registered: frozenset[str]) -> Target:
    """Revalidate safe facts through policy62; never forward its incoming text."""
    fields = ticket.source_ref.split(":")
    if (
        len(fields) != 3
        or fields[0] != "finance"
        or not safe_token(fields[1])
        or fields[2] != ticket.period
    ):
        raise PayloadRefused("invalid reminder source key")
    policy: PayloadPolicy | None = None
    try:
        policy = PayloadPolicy(
            payee_label=ticket.payee_label,
            amount=ticket.amount,
            due_date=ticket.due_date,
            period=ticket.period,
        )
    except ValidationError:
        pass  # Raise outside the exception context, without rejected facts.
    if policy is None:
        raise PayloadRefused("invalid reminder payload policy")
    validated = build_payload(policy, registered_payees=registered)
    amount = f"{validated.amount:.2f} CAD"
    body = (
        f"Type: bill-due\nWho/what: {validated.payee_label}, {validated.period}\n"
        f"Amount/detail: {amount}\nDate: due {validated.due_date.isoformat()}\n"
        f"Period-key: {ticket.source_ref}\n"
        "Note: tracked by Float; paid or settled items are closed automatically"
    )
    snapshot: CreateSnapshot | None = None
    try:
        snapshot = CreateSnapshot(
            title=validated.title,
            body=body,
            due_on=validated.due_date,
            priority=ticket.severity,
        )
    except ValidationError:
        pass  # Length/shape errors must not repeat restricted payload facts.
    if snapshot is None:
        raise PayloadRefused("invalid Current reminder payload")
    return Target(snapshot, ticket.status)


def _absent(row: SinkTicket) -> Target:
    return Target(
        row.create.model_copy(
            update={
                "title": row.last_title or row.create.title,
                "due_on": row.last_due_on or row.create.due_on,
                "priority": row.last_priority or row.create.priority,
            }
        ),
        "done",
    )


def _same(row: SinkTicket, wanted: Target) -> bool:
    return (row.last_title, row.last_due_on, row.last_priority, row.last_status) == (
        wanted.create.title,
        wanted.create.due_on,
        wanted.create.priority,
        wanted.status,
    )


@dataclass(frozen=True)
class SyncResult:
    """``partial``: the key loop finished with one or more keys set aside, the first
    one's code as ``reason``; the other keys synced."""

    state: Literal["synced", "partial", "pending", "failed"]
    reason: str | None = None


@dataclass(frozen=True)
class _Steps:
    """How each step of a sync opens its owner block, builds its sink and asks
    whether the sink is ready: per-step committed transactions over HTTPS, role
    blocks on one reminder session in process. ``key`` is the session's per-key
    savepoint in process; over HTTPS there is none, and a refusal stops the sync."""

    begin: Callable[[], AbstractContextManager[UnitOfWork]]
    sink: Callable[[UnitOfWork], CurrentSink]
    unready: Callable[[UnitOfWork], SyncResult | None]
    key: Callable[[], AbstractContextManager[None]] | None = None


def _https_unready(uow: UnitOfWork) -> SyncResult | None:
    try:
        settings = load_settings()
        if configuration(settings) is None:
            return SyncResult("pending", "not_configured")
        credential = settings.get("finance.reminders.current_credential_file")
        if not isinstance(credential, str):
            return SyncResult("pending", "not_configured")
        if not is_enabled(uow, SINK):
            return SyncResult("pending", "sink_off")
    except (CurrentError, SettingsError):
        return SyncResult("pending", "configuration_error")
    return None


def _https_steps(ctx: FinanceContext) -> _Steps:
    return _Steps(
        begin=lambda: owner_transaction(ctx), sink=CurrentSink, unready=_https_unready
    )


def _in_process_unready(uow: UnitOfWork) -> SyncResult | None:
    return None if is_enabled(uow, SINK) else SyncResult("pending", "sink_off")


def _session_steps(session: ReminderSession) -> _Steps:
    return _Steps(
        begin=session.owner,
        sink=lambda uow: CurrentSink(uow, transport=session),
        unready=_in_process_unready,
        key=session.key,
    )


def sync_reminders(ctx: FinanceContext) -> SyncResult:
    """Job root, outside dispatch.

    Over HTTPS (nothing bound) short transactions commit around each request, and a
    refusal stops the keys after it. In process the whole sync is one reminder
    session with a savepoint per key, committed when the key loop finishes: a key
    refused for itself (``KEY_LOCAL``) is rolled back alone, its code kept on its row,
    and the others sync (``partial``); a global code, a payload refusal or a storage
    error leaves neither Current nor this sink's rows changed by the sync.
    """
    failed = False
    try:
        binding = in_process()
        if binding is None:
            return _sync(ctx, _https_steps(ctx))
        return _sync_in_process(ctx, binding)
    except SQLAlchemyError:
        failed = True
    if failed:
        raise OperationError("current storage failed")
    raise OperationError("current sync failed")


def _sync_in_process(ctx: FinanceContext, binding: InProcessCurrent) -> SyncResult:
    if ctx.role not in {Role.OWNER, Role.SERVICE}:
        raise RoleRefused("the reminder sync refuses this role")
    with ExitStack() as stack:
        session: ReminderSession | None = None
        opened: str | None = None
        try:
            session = stack.enter_context(binding.session())
        except CurrentError as exc:
            opened = exc.code
        if session is None:
            # The session could not open: nothing was called.
            code = opened or "current_unavailable"
            return SyncResult("pending" if code == "not_configured" else "failed", code)
        result = _sync(ctx, _session_steps(session))
        if result.state in {"synced", "partial"}:
            # The key loop finished: the synced keys and the set-aside keys'
            # annotations commit together.
            session.commit()
        # Anything else rolls back as the session closes.
        return result
    raise OperationError("current sync failed")


def _sync(ctx: FinanceContext, steps: _Steps) -> SyncResult:
    with steps.begin() as uow:
        facts = PostgresReminderRepository(uow)
        desired = desired_tickets(ctx, facts)
        registered = facts.registered_payees()
        wanted = {t.source_ref: target(t, registered) for t in desired.tickets}
        if len(wanted) != len(desired.tickets):
            raise PayloadRefused("duplicate reminder source keys")
        existing = {r.source_ref: r for r in PostgresCurrentTicketRepository(uow).all()}
        unready = steps.unready(uow)
        if unready is not None:
            return unready
    set_aside: list[str] = []
    for ref in sorted(wanted.keys() | existing.keys()):
        # Renews a tick's lease before each key; outside the except below, so a lost
        # lease or a cancel leaves the sync and its session rolls back.
        ctx.progress()
        intent = wanted.get(ref) or _absent(existing[ref])
        failure: str | None = None
        local = False
        try:
            # In process the key's Current writes and its rows share one savepoint,
            # rolled back alone when the key is refused.
            with steps.key() if steps.key is not None else nullcontext():
                _reconcile(ctx, ref, intent, steps)
        except CurrentError as exc:
            failure = exc.code
            local = steps.key is not None and failure in KEY_LOCAL
            if not exc.logged:
                # The code alone, never the key, the item or text.
                _LOG.info(LINE_SET_ASIDE if local else LINE_STOP, failure)
        if failure is None:
            continue
        _annotate(ctx, steps, ref, intent, failure)
        if local:
            set_aside.append(failure)
            continue
        # A global condition (or any refusal over HTTPS) stops the sync; in process
        # the session rolls back, annotation and earlier keys alike.
        return SyncResult(
            "pending" if failure in {"sink_off", "not_configured"} else "failed",
            failure,
        )
    if set_aside:
        return SyncResult("partial", set_aside[0])
    return SyncResult("synced")


def _annotate(
    ctx: FinanceContext, steps: _Steps, ref: str, intent: Target, failure: str
) -> None:
    """Record ``failure`` on the key's row. A new open key whose claim rolled back
    with its key savepoint gets the claim, so the next sync completes it and health
    and status see it."""
    now = ctx.clock.now_utc()
    with steps.begin() as uow:
        store = PostgresCurrentTicketRepository(uow)
        row = store.get(ref)
        if row is not None:
            store.save(
                row.model_copy(
                    update={"last_error_code": failure, "last_attempt_at": now}
                )
            )
        elif intent.status == "open":
            _claim(store, ref, intent, now, error=failure)


def _claim(
    store: PostgresCurrentTicketRepository,
    ref: str,
    intent: Target,
    now: dt.datetime,
    *,
    error: str | None = None,
) -> SinkTicket:
    """The row of a key seen for the first time: ``post_pending`` for an open
    period, acknowledged for a settled one, which is never created."""
    candidate = SinkTicket(
        source_ref=ref,
        state="post_pending" if intent.status == "open" else "done",
        create=intent.create,
        last_attempt_at=now,
        last_error_code=error,
    )
    if intent.status == "done":
        candidate = _acknowledged(candidate, intent, now)
    store.insert_if_absent(candidate)
    row = store.get(ref)
    assert row is not None
    return row


def _acknowledged(row: SinkTicket, intent: Target, now: dt.datetime) -> SinkTicket:
    return row.model_copy(
        update={
            "state": intent.status,
            "last_title": intent.create.title,
            "last_due_on": intent.create.due_on,
            "last_priority": intent.create.priority,
            "last_status": intent.status,
            "last_success_at": now,
            "last_attempt_at": now,
            "last_error_code": None,
            "settled_at": now if intent.status == "done" else None,
        }
    )


def _reconcile(ctx: FinanceContext, ref: str, intent: Target, steps: _Steps) -> None:
    now = ctx.clock.now_utc()
    # Claim transaction A always exits before any create. It is not a worker lock:
    # Current's source pair and fingerprint also fold overlapping create attempts.
    with steps.begin() as uow:
        store = PostgresCurrentTicketRepository(uow)
        row = store.get(ref)
        if row is None:
            row = _claim(store, ref, intent, now)
        if row.external_id is None and row.state == "done" and intent.status == "open":
            # This period was first seen settled and has never had a remote create.
            row = row.model_copy(
                update={"state": "post_pending", "create": intent.create}
            )
            store.save(row)
        if _same(row, intent) and row.state != "post_pending":
            return
    fresh: RemoteItem | None = None
    if row.state == "post_pending":
        with steps.begin() as uow:
            fresh = steps.sink(uow).create(row)
            row = row.model_copy(update={"external_id": fresh.ref, "state": "open"})
            PostgresCurrentTicketRepository(uow).save(row)
    ref_ = row.external_id
    if ref_ is None:
        with steps.begin() as uow:
            PostgresCurrentTicketRepository(uow).save(_acknowledged(row, intent, now))
        return
    # Each mutation gets a fresh read; only a just-returned create can supply it.
    fields: dict[str, object] = {}
    with steps.begin() as uow:
        sink = steps.sink(uow)
        read: RemoteItem | None = fresh
        if read is None:
            try:
                read = sink.get(ref_)
            except CurrentError as exc:
                # Only this read of the stored id treats the item as gone; a write
                # that finds it gone sets the key aside until the next read.
                if exc.code != "not_found":
                    raise
        if read is not None:
            if read.title != intent.create.title:
                fields["title"] = intent.create.title
            if read.due_on != intent.create.due_on:
                fields["due_on"] = intent.create.due_on.isoformat()
            if read.priority != intent.create.priority:
                fields["priority"] = intent.create.priority
            if fields:
                read = sink.mutate("update", ref_, fields, fresh=read, source_ref=ref)
    if read is None:
        _refile(ctx, ref, row, intent, steps, now)
        return
    remote = read
    if remote.status != intent.status:
        with steps.begin() as uow:
            remote = steps.sink(uow).mutate(
                "set_status",
                ref_,
                {"status": intent.status},
                fresh=fresh if not fields else None,
                source_ref=ref,
            )
    if (remote.title, remote.due_on, remote.priority, remote.status) != (
        intent.create.title,
        intent.create.due_on,
        intent.create.priority,
        intent.status,
    ):
        raise CurrentError("incomplete_convergence")
    with steps.begin() as uow:
        store = PostgresCurrentTicketRepository(uow)
        persisted = store.get(ref)
        assert persisted is not None
        store.save(_acknowledged(persisted, intent, now))


def _refile(
    ctx: FinanceContext,
    ref: str,
    row: SinkTicket,
    intent: Target,
    steps: _Steps,
    now: dt.datetime,
) -> None:
    """Current no longer has the stored item (the owner deleted it). A settled
    period is forgotten, never created again (ADR-0014); an open one is claimed
    afresh and created once more, which Current folds into any holder of the pair
    (ADR-0027). Runs inside the key's savepoint, so a refused create rolls it back."""
    with steps.begin() as uow:
        store = PostgresCurrentTicketRepository(uow)
        if intent.status == "done":
            store.save(
                _acknowledged(row.model_copy(update={"external_id": None}), intent, now)
            )
            return
        store.save(
            row.model_copy(
                update={
                    "external_id": None,
                    "state": "post_pending",
                    "create": intent.create,
                }
            )
        )
    # The pass again: the claim is post_pending, so it creates, then converges from
    # the create's answer and never reads the stored id again.
    _reconcile(ctx, ref, intent, steps)


def preview_builder(uow: UnitOfWork) -> list[PreviewItem]:
    """Exact pending changes now, including clock-driven overdue priority.

    Current alone may change preview membership/code across the local due-date
    boundary. Typed fields exclude rendered clock text; see ADR-0015.
    """
    settings = load_settings()
    ctx = FinanceContext(
        settings=settings,
        clock=SystemClock(),
        paths=FinancePaths(Path(".")),
        role=Role.OWNER,
        snapshot_provider=lambda _u, _r: None,
    )
    facts = PostgresReminderRepository(uow)
    wanted = {
        t.source_ref: target(t, facts.registered_payees())
        for t in desired_tickets(ctx, facts).tickets
    }
    rows = {r.source_ref: r for r in PostgresCurrentTicketRepository(uow).all()}
    items = []
    for ref in sorted(wanted.keys() | rows.keys()):
        row = rows.get(ref)
        intent = wanted.get(ref) or _absent(rows[ref])
        if row is not None and _same(row, intent):
            continue
        items.append(
            PreviewItem(
                kind="reminder",
                key=ref,
                due_on=intent.create.due_on,
                fields=(
                    ("title", intent.create.title),
                    ("status", intent.status),
                    ("floor", str(settings.get_date("finance.reminders.min_due_date"))),
                ),
            )
        )
    return items


SINK_SPECS[SINK] = spec
PREVIEW_BUILDERS[SINK] = SinkPreview(items=preview_builder)

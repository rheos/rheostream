"""The owner's ``leads.jobfeed.connect`` and ``leads.jobfeed.reconnect``.

Both run through the real dispatcher (and once through the real internal operation
route the web app calls) against a fake authorization server with a counting
registration endpoint. Secret files land in a real file store under a temporary
data root, so every "no file" assertion reads the file system.
"""

import base64
import hashlib
import json
import logging
import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from harness.jobfeed import (
    CLIENT_NAME,
    ISSUER,
    RESOURCE_URL,
    ROUTING_HOST,
    Collector,
    FakeAuthorizationServer,
    FakeClock,
    FakeResolver,
    Hold,
    answer,
    configure_connector,
    install_collector,
    issued,
    jobsearch_pipeline,
    malformed,
    oversized,
    owner_web,
    redirected,
    registered_client,
    serve,
)
from harness.registry import add_member
from rheo_app_core import internal_routes
from rheo_app_core.main import internal_app
from rheo_contracts import ActorKind, Entry, RecordRef, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.boundary.factories import context_for_connection, context_from_token
from rheo_core.connectors import oauth
from rheo_core.deletion import WORKSPACE_LIFECYCLE_LOCK_KEY
from rheo_core.operations import dispatch
from rheo_core.operations import registry as operation_registry
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.refs import uuid7
from rheo_core.secrets import SecretRef
from rheo_core.sessions import create_session, mint_host_secret, switch_workspace
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import audit_record, schedule
from rheo_core.storage.work_tables import operation as operation_record
from rheo_leads.jobfeed import authorization
from rheo_leads.jobfeed.state import read_connection
from rheo_leads.references import ref
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import tables as t
from rheo_leads.storage.tables import metadata as leads_metadata
from sqlalchemy import Engine, event, func, select, text, update

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres

CONNECT = "leads.jobfeed.connect"
RECONNECT = "leads.jobfeed.reconnect"
SECRET = "client-secret-marker-1e7c4b22-9d0a-4f61-8b35-5a2e90c7d413"
TOKEN = "registration-token-marker-6a0f2d84-3b1c-4e97-a512-c8d7e4f09b26"
INTERNAL_SECRET = "jobfeed-connect-internal-secret"


# --- fixtures and helpers -----------------------------------------------------------


@pytest.fixture
def root(intake: Intake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    configure_connector(monkeypatch, tmp_path)
    return tmp_path


@pytest.fixture
def server() -> FakeAuthorizationServer:
    return FakeAuthorizationServer()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def served(
    root: Path, server: FakeAuthorizationServer, clock: FakeClock
) -> Iterator[FakeAuthorizationServer]:
    with serve(server, clock=clock):
        yield server


def run(
    i: Intake, ctx: WorkspaceContext, operation: str, /, **payload: Any
) -> OperationOutcome:
    return dispatch(
        ctx, operation, payload, registry=i.surfaces.operations, consumers=i.consumers
    )


def connect(i: Intake, ctx: WorkspaceContext, pipeline_id: UUID) -> OperationOutcome:
    return run(i, ctx, CONNECT, pipeline_ref=ref("pipeline", pipeline_id))


def refused(outcome: OperationOutcome, code: str) -> None:
    assert not outcome.ok, outcome
    assert outcome.error is not None and outcome.error.error_code == code, outcome


def pending(ctx: WorkspaceContext) -> list[dict[str, Any]]:
    with open_unit_of_work(ctx) as uow:
        return [
            dict(row)
            for row in uow.connection.execute(
                select(j.jobfeed_authorization)
            ).mappings()
        ]


def secret_files(root: Path) -> set[Path]:
    return {p for p in (root / "secrets").rglob("*") if p.is_file()}


def file_of(root: Path, reference: str) -> Path:
    return root / "secrets" / SecretRef.parse(reference).id


def with_secrets(**extra: Any) -> Callable[[httpx.Request], httpx.Response]:
    return issued(client_secret=SECRET, registration_access_token=TOKEN, **extra)


def row_files(root: Path, row: dict[str, Any]) -> set[Path]:
    return {
        file_of(root, row[name])
        for name in ("client_secret_ref", "registration_token_ref")
        if row[name] is not None
    }


def audits(ctx: WorkspaceContext, name: str) -> list[dict[str, Any]]:
    with open_unit_of_work(ctx) as uow:
        return [
            dict(row)
            for row in uow.connection.execute(
                select(audit_record).where(audit_record.c.operation_name == name)
            ).mappings()
        ]


def dump(ctx: WorkspaceContext) -> str:
    """Every Leads row, every audit row and every operation record, as text."""
    with open_unit_of_work(ctx) as uow:
        rows = [
            list(uow.connection.execute(select(table)).mappings())
            for table in (*leads_metadata.sorted_tables, audit_record, operation_record)
        ]
    return repr(rows)


def counts(ctx: WorkspaceContext) -> dict[str, int]:
    with open_unit_of_work(ctx) as uow:
        return {
            table.name: uow.connection.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
            for table in (
                j.jobfeed_connection,
                j.jobfeed_authorization,
                t.intake_connection,
                t.connection_health,
            )
        }


@pytest.fixture
def owner(intake: Intake) -> WorkspaceContext:
    return owner_web(intake)


@pytest.fixture
def pipeline_id(intake: Intake, owner: WorkspaceContext) -> UUID:
    return jobsearch_pipeline(intake, owner)


def pinned_collector(
    i: Intake, server: FakeAuthorizationServer, *, root: Path, secrets: bool = False
) -> Collector:
    """A collector whose stored client was registered against ``server`` for the
    current redirect URI, so a reconnect with unchanged discovery reuses it.

    With ``secrets`` the stored client is a real registration whose secret files
    the store wrote (and so could discard); the requests it took are forgotten.
    """
    connection_id = uuid7()
    redirect = oauth.redirect_uri("leads")
    client = registered_client(client_id="stored-client", redirect_uri=redirect)
    if secrets:
        responder = server.registration
        server.registration = with_secrets(client_id="stored-client")
        endpoint = server.registration_endpoint
        assert endpoint is not None
        client = oauth.register(
            endpoint,
            redirect,
            CLIENT_NAME,
            workspace_id=i.workspace,
            connection_id=connection_id,
            resolver=FakeResolver(),
            clock=FakeClock().monotonic,
            transport=httpx.MockTransport(server.handle),
        )
        assert client.client_secret_ref and client.registration_token_ref
        server.registration = responder
        server.requests.clear()
        server.registrations.clear()
    return install_collector(
        i, client=client, endpoints=server.discovered(), connection_id=connection_id
    )


def token_context(i: Intake, owner: WorkspaceContext, kind: str, entry: str) -> Any:
    """A real owner token naming both operations, so the refusal under test is the
    handler's browser-session check rather than the token's operation set."""
    # Token issue and token resolution read the process-wide operation registry;
    # the intake fixture loads Leads into an isolated one.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(operation_registry, "REGISTRY", i.surfaces.operations)
        issued_token = run(
            i, owner, "core.token.issue", kind=kind, operations=[CONNECT, RECONNECT]
        )
        assert issued_token.ok, issued_token
        ctx = context_from_token(str(issued_token.result.value), entry)  # type: ignore[union-attr]
    assert isinstance(ctx, WorkspaceContext), ctx
    assert ctx.actor.kind is ActorKind.TOKEN and ctx.role is Role.OWNER
    assert {CONNECT, RECONNECT} <= set(ctx.operation_set)  # type: ignore[arg-type]
    return ctx


def owner_on_api(owner: WorkspaceContext) -> WorkspaceContext:
    """The owner's own account with one half of the browser-session pair changed:
    the API entry instead of the web entry. The other half, a token actor on the web
    entry, has no boundary factory (tokens present only on the API and MCP)."""
    ctx = context_for_harness(owner.workspace_id, owner.actor.id, Role.OWNER, Entry.API)
    assert isinstance(ctx, WorkspaceContext), ctx
    assert ctx.actor == owner.actor and ctx.entry is not owner.entry
    return ctx


def member_context(i: Intake) -> WorkspaceContext:
    member = add_member(
        i.cluster.backend, i.workspace, Role.MEMBER, display_name="Sample member"
    )
    ctx = context_for_harness(i.workspace, member, Role.MEMBER, Entry.WEB)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def capability(i: Intake, owner: WorkspaceContext, value: bool) -> None:
    outcome = run(
        i, owner, "core.settings.set", key="leads.capabilities.jobsearch", value=value
    )
    assert outcome.ok, outcome


def b64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# --- AC 1: who may connect and reconnect ---------------------------------------------


def test_connect_is_refused_for_every_caller_but_the_owners_browser_session(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    """The browser-session check's web-entry token half is defence in depth for a
    context no boundary factory can build, so it has no case here."""
    other = run(
        intake,
        owner,
        "leads.pipeline.create",
        name="Inbound",
        preset="inbound_services",
    )
    assert other.ok, other
    inbound = RecordRef.parse(other.result.ref).id  # type: ignore[union-attr]
    poll = context_for_connection(
        intake.workspace, intake.connection_id, "leads", "manual"
    )
    assert isinstance(poll, WorkspaceContext), poll
    cases: list[tuple[WorkspaceContext, UUID, str]] = [
        (member_context(intake), pipeline_id, "role_not_permitted"),
        (token_context(intake, owner, "mcp", "mcp"), pipeline_id, "role_not_permitted"),
        (token_context(intake, owner, "cli", "api"), pipeline_id, "role_not_permitted"),
        # A connection actor's operation set holds only its acceptance operation.
        (poll, pipeline_id, "operation_not_permitted"),
        (owner_on_api(owner), pipeline_id, "role_not_permitted"),
        (owner, inbound, "preset_not_allowed"),
        (owner, uuid7(), "not_found"),
    ]
    for ctx, target, code in cases:
        refused(connect(intake, ctx, target), code)
    capability(intake, owner, False)
    refused(connect(intake, owner, pipeline_id), "capability_disabled")
    assert served.requests == []
    assert pending(owner) == []
    assert len(audits(owner, CONNECT)) == len(cases) + 1

    capability(intake, owner, True)
    outcome = connect(intake, owner, pipeline_id)
    assert outcome.ok, outcome
    assert len(served.registrations) == 1


def test_reconnect_is_refused_for_every_caller_but_the_owners_browser_session(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    """The browser-session check's web-entry token half is defence in depth for a
    context no boundary factory can build, so it has no case here."""
    collector = pinned_collector(intake, served, root=root)
    owner = collector.owner
    cases = [
        member_context(intake),  # (a) a member role
        token_context(intake, owner, "mcp", "mcp"),  # (b) an agent, (e) owner on MCP
        token_context(intake, owner, "cli", "api"),  # (e) an owner token on the API
        owner_on_api(owner),
    ]
    for ctx in cases:
        refused(run(intake, ctx, RECONNECT), "role_not_permitted")
    # (b) a connection actor: its operation set holds only its acceptance operation.
    refused(run(intake, collector.poll, RECONNECT), "operation_not_permitted")
    capability(intake, owner, False)
    refused(run(intake, owner, RECONNECT), "capability_disabled")
    assert served.requests == []
    assert pending(owner) == []

    capability(intake, owner, True)
    assert run(intake, owner, RECONNECT).ok


def test_reconnect_without_a_connection_is_not_found(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    refused(run(intake, owner, RECONNECT), "not_found")
    assert served.requests == []


# --- AC 2 and I4: the URL and the pending row -----------------------------------------


def test_the_url_carries_pkce_state_resource_redirect_and_the_issued_client(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    clock: FakeClock,
) -> None:
    outcome = connect(intake, owner, pipeline_id)
    assert outcome.ok, outcome
    url = outcome.result.authorization_url  # type: ignore[union-attr]
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{ISSUER}/authorize"
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    (row,) = pending(owner)

    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == b64url_sha256(row["code_verifier"])
    assert row["code_verifier"] not in url
    state = query["state"]
    assert len(base64.urlsafe_b64decode(state + "==")) * 8 >= 128
    assert row["state_hash"] == hashlib.sha256(state.encode("ascii")).digest()
    assert state not in dump(owner)
    assert query["resource"] == RESOURCE_URL
    assert query["redirect_uri"] == oauth.redirect_uri("leads")
    assert query["client_id"] == "issued-client-0001"
    assert query["scope"] == "jobs:read"

    assert row["account_id"] == owner.actor.id
    assert row["pipeline_id"] == pipeline_id
    assert row["mode"] == "connect"
    assert row["singleton"] is True
    assert row["client_registered"] is True
    assert row["client_id"] == "issued-client-0001"
    assert row["client_auth_method"] == "none"
    assert row["redirect_uri"] == oauth.redirect_uri("leads")
    assert row["issuer"] == ISSUER
    assert row["registration_endpoint"] == f"{ISSUER}/register"
    assert row["revocation_endpoint"] == f"{ISSUER}/revoke"
    assert row["created_at"] == clock.now()
    assert row["expires_at"] == row["created_at"] + timedelta(minutes=10)
    assert outcome.result.expires_at == row["expires_at"]  # type: ignore[union-attr]
    with open_unit_of_work(owner) as uow:
        assert read_connection(uow.connection) is None


def test_a_second_connect_replaces_the_first_and_discards_its_fresh_files(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    served.registration = with_secrets()
    first = connect(intake, owner, pipeline_id)
    assert first.ok, first
    (first_row,) = pending(owner)
    first_files = row_files(root, first_row)
    assert len(first_files) == 2 and all(p.exists() for p in first_files)

    second = connect(intake, owner, pipeline_id)
    assert second.ok, second
    (second_row,) = pending(owner)
    assert second_row["state_hash"] != first_row["state_hash"]
    assert second_row["connection_id"] != first_row["connection_id"]
    assert not any(p.exists() for p in first_files)
    assert secret_files(root) == row_files(root, second_row)
    first_state = parse_qs(urlsplit(first.result.authorization_url).query)["state"][0]  # type: ignore[union-attr]
    assert hashlib.sha256(first_state.encode()).digest() not in {
        r["state_hash"] for r in pending(owner)
    }


def test_a_reused_client_replaced_during_discovery_refuses_and_keeps_files(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    """A reconnect decides to reuse the stored client before discovery; if another
    completion stores a different client meanwhile, the start refuses instead of
    pinning the retired client's files, and nothing it did not create is touched."""
    collector = pinned_collector(intake, served, root=root, secrets=True)
    stored = secret_files(root)
    hold = Hold()
    served.holds["discovery"] = hold
    result: list[OperationOutcome] = []
    worker = threading.Thread(
        target=lambda: result.append(run(intake, collector.owner, RECONNECT))
    )
    worker.start()
    try:
        assert hold.entered.wait(timeout=30)
        with open_unit_of_work(collector.owner) as uow:
            uow.connection.execute(
                update(j.jobfeed_connection).values(
                    client_id="completed-client",
                    client_secret_ref=None,
                    registration_token_ref=None,
                )
            )
            uow.commit()
    finally:
        hold.release.set()
        worker.join(timeout=60)
    refused(result[0], "record_stale")
    assert served.registrations == []
    assert pending(collector.owner) == []
    assert secret_files(root) == stored


def test_replacing_a_reused_clients_row_discards_nothing(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    collector = pinned_collector(intake, served, root=root, secrets=True)
    stored = secret_files(root)
    assert len(stored) == 2
    for _ in range(2):
        outcome = run(intake, collector.owner, RECONNECT)
        assert outcome.ok, outcome
    (row,) = pending(collector.owner)
    assert row["client_registered"] is False
    assert row["client_id"] == "stored-client"
    assert served.registrations == []
    assert secret_files(root) == stored


# --- retirement after commit ----------------------------------------------------------


class InjectedFailure(Exception):
    """Raised by the test's statement listener; carries no text."""


def _is_pending_insert(statement: str) -> bool:
    return (
        statement.lstrip().upper().startswith("INSERT INTO LEADS.JOBFEED_AUTHORIZATION")
    )


def test_a_replacement_that_fails_before_commit_keeps_the_previous_row_and_files(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    served.registration = with_secrets()
    assert connect(intake, owner, pipeline_id).ok
    (previous,) = pending(owner)
    previous_files = row_files(root, previous)
    deleted_first: list[bool] = []

    def explode(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if (
            statement.lstrip()
            .upper()
            .startswith("DELETE FROM LEADS.JOBFEED_AUTHORIZATION")
        ):
            deleted_first.append(True)
        if _is_pending_insert(statement):
            raise InjectedFailure

    event.listen(Engine, "before_cursor_execute", explode)
    try:
        outcome = connect(intake, owner, pipeline_id)
    finally:
        event.remove(Engine, "before_cursor_execute", explode)

    assert deleted_first == [True]
    assert not outcome.ok and outcome.error is not None
    assert outcome.error.error_code == "handler_failed"
    assert outcome.error.error_text == "InjectedFailure"
    assert len(served.registrations) == 2
    assert pending(owner) == [previous]
    assert all(p.exists() for p in previous_files)
    assert secret_files(root) == previous_files
    for marker in (SECRET, TOKEN, previous["code_verifier"]):
        assert marker not in repr(outcome)


def test_a_failure_after_the_replacement_commits_keeps_the_new_rows_files(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure while the committed replacement unit closes must not discard the
    files the committed row now references; the replaced row's files still go."""
    served.registration = with_secrets()
    assert connect(intake, owner, pipeline_id).ok
    (previous,) = pending(owner)
    previous_files = row_files(root, previous)
    inserted: set[int] = set()
    real_open = authorization.open_unit_of_work

    def watch(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if _is_pending_insert(statement):
            inserted.add(id(conn))

    class FailingClose:
        def __init__(self, ctx: WorkspaceContext) -> None:
            self.inner = real_open(ctx)

        def __enter__(self) -> Any:
            entered = self.inner.__enter__()
            self.conn = entered.connection
            return entered

        def __exit__(self, *exc: Any) -> None:
            self.inner.__exit__(*exc)
            if exc[0] is None and id(self.conn) in inserted:
                raise InjectedFailure

    monkeypatch.setattr(authorization, "open_unit_of_work", FailingClose)
    event.listen(Engine, "before_cursor_execute", watch)
    try:
        outcome = connect(intake, owner, pipeline_id)
    finally:
        event.remove(Engine, "before_cursor_execute", watch)

    assert inserted
    assert not outcome.ok and outcome.error is not None
    assert outcome.error.error_code == "handler_failed"
    (current,) = pending(owner)
    assert current["state_hash"] != previous["state_hash"]
    current_files = row_files(root, current)
    assert len(current_files) == 2 and all(p.exists() for p in current_files)
    assert not any(p.exists() for p in previous_files)
    assert secret_files(root) == current_files


def test_the_replaced_rows_files_are_discarded_only_after_the_replacement_commits(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    served.registration = with_secrets()
    assert connect(intake, owner, pipeline_id).ok
    (previous,) = pending(owner)
    previous_files = row_files(root, previous)
    inserting: set[int] = set()
    at_commit: list[bool] = []

    def watch(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if _is_pending_insert(statement):
            inserting.add(id(conn))

    def on_commit(conn: Any) -> None:
        if id(conn) in inserting:
            inserting.discard(id(conn))
            at_commit.append(all(p.exists() for p in previous_files))

    event.listen(Engine, "before_cursor_execute", watch)
    event.listen(Engine, "commit", on_commit)
    try:
        outcome = connect(intake, owner, pipeline_id)
    finally:
        event.remove(Engine, "before_cursor_execute", watch)
        event.remove(Engine, "commit", on_commit)

    assert outcome.ok, outcome
    assert at_commit == [True]
    assert not any(p.exists() for p in previous_files)


# --- connection_exists and endpoint_misconfigured -------------------------------------


def test_connect_refuses_when_a_connection_exists(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    collector = install_collector(intake)
    refused(
        connect(intake, collector.owner, collector.pipeline_id), "connection_exists"
    )
    assert served.requests == []


def test_a_connection_completed_during_registration_refuses_and_discards(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    served.registration = with_secrets()
    hold = Hold()
    served.holds["registration"] = hold
    result: list[OperationOutcome] = []
    worker = threading.Thread(
        target=lambda: result.append(connect(intake, owner, pipeline_id))
    )
    worker.start()
    try:
        assert hold.entered.wait(timeout=30)
        install_collector(intake)
    finally:
        hold.release.set()
        worker.join(timeout=60)
    refused(result[0], "connection_exists")
    assert len(served.registrations) == 1
    assert pending(owner) == []
    assert secret_files(root) == set()


UNCONFIGURED = pytest.mark.parametrize(
    "settings",
    [
        {"enabled": False},
        {"identity_path": "/signin"},
        {"resource_url": ""},
        {},  # positive control: configured, so the same call does reach the server
    ],
    ids=["switch-off", "identity-path-not-served", "resource-url-unset", "control"],
)


@UNCONFIGURED
def test_an_unconfigured_connect_sends_nothing_at_all(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, Any],
) -> None:
    configure_connector(monkeypatch, root, **settings)
    outcome = connect(intake, owner, pipeline_id)
    if not settings:
        assert outcome.ok, outcome
        assert len(served.registrations) == 1
        return
    refused(outcome, "endpoint_misconfigured")
    assert served.requests == []
    assert pending(owner) == []
    assert secret_files(root) == set()


@UNCONFIGURED
def test_an_unconfigured_reconnect_sends_nothing_at_all(
    intake: Intake,
    served: FakeAuthorizationServer,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, Any],
) -> None:
    collector = pinned_collector(intake, served, root=root)
    configure_connector(monkeypatch, root, **settings)
    outcome = run(intake, collector.owner, RECONNECT)
    if not settings:
        assert outcome.ok, outcome
        assert served.requests
        return
    refused(outcome, "endpoint_misconfigured")
    assert served.requests == []
    assert pending(collector.owner) == []


@pytest.mark.parametrize(
    ("broken", "registrations"),
    [("no-registration-endpoint", 0), ("registration-redirect", 1), ("control", 1)],
)
def test_discovery_or_a_redirected_registration_is_endpoint_misconfigured(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
    broken: str,
    registrations: int,
) -> None:
    if broken == "no-registration-endpoint":
        served.registration_path = None
    elif broken == "registration-redirect":
        served.registration = redirected()
    outcome = connect(intake, owner, pipeline_id)
    assert len(served.registrations) == registrations
    if broken == "control":
        assert outcome.ok, outcome
        return
    refused(outcome, "endpoint_misconfigured")
    assert len(served.registrations) == registrations
    assert pending(owner) == []
    assert secret_files(root) == set()


def test_a_reconnect_discovery_failure_writes_no_health(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    collector = pinned_collector(intake, served, root=root)
    served.registration_path = None
    refused(run(intake, collector.owner, RECONNECT), "endpoint_misconfigured")
    with open_unit_of_work(collector.owner) as uow:
        health = uow.connection.execute(
            select(t.connection_health.c.last_error).where(
                t.connection_health.c.connection_id == collector.connection_id
            )
        ).scalar_one()
    assert health is None


# --- AC 7 (a): the registration body through the real operation route --


def test_the_registration_body_is_independent_of_the_owner_request(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    monkeypatch.setenv("RHEO_INTERNAL_SECRET", INTERNAL_SECRET)
    monkeypatch.setenv(
        "RHEO__internal__secret_ref", "secret://env/RHEO_INTERNAL_SECRET"
    )
    dispatched: list[str] = []

    def through_module_registry(
        ctx: Any, name: str, payload: Any, **kwargs: Any
    ) -> Any:
        dispatched.append(name)
        assert ctx.entry is Entry.WEB and ctx.actor.kind is ActorKind.ACCOUNT
        kwargs["registry"] = intake.surfaces.operations
        kwargs["consumers"] = intake.consumers
        return dispatch(ctx, name, payload, **kwargs)

    monkeypatch.setattr(internal_routes, "dispatch", through_module_registry)
    assert owner.actor.id is not None
    session = create_session(owner.actor.id)
    secret = mint_host_secret(session.id, ROUTING_HOST).hex()
    assert switch_workspace(session.id, intake.workspace) is None
    base = {
        "X-Rheo-Internal": INTERNAL_SECRET,
        "X-Rheo-Host": ROUTING_HOST,
        "X-Rheo-Session": secret,
    }
    varied = {
        **base,
        "Referer": "https://elsewhere.example.org/page?x=1",
        "X-Forwarded-Host": "forged.example.org",
        "X-Forwarded-Proto": "http",
        "User-Agent": "varied-agent/9.9",
        "Accept-Language": "fr-CA",
    }
    body = {"pipeline_ref": ref("pipeline", pipeline_id)}

    async def post(
        headers: dict[str, str], params: dict[str, str] | None = None
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as client:
            return await client.post(
                f"/internal/v1/operations/{CONNECT}",
                headers=headers,
                params=params,
                json=body,
            )

    first = asyncio.run(post(base))
    second = asyncio.run(post(varied))
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert dispatched == [CONNECT, CONNECT]
    assert len(served.registrations) == 2
    one, two = served.registrations
    assert one.content == two.content
    assert one.headers["content-type"] == "application/json"
    assert "authorization" not in one.headers and "authorization" not in two.headers
    assert json.loads(one.content) == {
        "redirect_uris": [oauth.redirect_uri("leads")],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "client_name": CLIENT_NAME,
    }
    others = [r for r in served.requests if r not in served.registrations]
    assert others
    for request in others:
        assert CLIENT_NAME not in str(request.url)
        assert CLIENT_NAME.encode() not in request.content
        assert CLIENT_NAME not in repr(dict(request.headers))

    # A query string never reaches the dispatcher, so it cannot shape the body.
    third = asyncio.run(post(varied, {"redirect_uri": "https://forged.example.org/cb"}))
    assert third.status_code == 422
    assert dispatched == [CONNECT, CONNECT]
    assert len(served.registrations) == 2


# --- AC 7 (e): issued secrets exist only as files -------------------------------------


def test_issued_secrets_exist_only_as_referenced_files(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    served.registration = with_secrets(token_endpoint_auth_method="client_secret_post")
    caplog.set_level(logging.DEBUG)
    outcome = connect(intake, owner, pipeline_id)
    assert outcome.ok, outcome
    (row,) = pending(owner)
    assert row["client_auth_method"] == "client_secret_post"
    assert file_of(root, row["client_secret_ref"]).read_text() == SECRET
    assert file_of(root, row["registration_token_ref"]).read_text() == TOKEN
    surfaces = "\n".join(
        [
            dump(owner),
            repr(outcome),
            "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records),
        ]
    )
    for marker in (SECRET, TOKEN):
        assert marker not in surfaces


# --- AC 78: registration count and reuse ----------------------------------------------


def test_a_reconnect_with_unchanged_discovery_reuses_the_stored_client(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    collector = pinned_collector(intake, served, root=root)
    outcome = run(intake, collector.owner, RECONNECT)
    assert outcome.ok, outcome
    assert served.registrations == []
    query = parse_qs(urlsplit(outcome.result.authorization_url).query)  # type: ignore[union-attr]
    assert query["client_id"] == ["stored-client"]
    (row,) = pending(collector.owner)
    assert row["mode"] == "reconnect"
    assert row["connection_id"] == collector.connection_id
    assert row["pipeline_id"] == collector.pipeline_id
    assert row["client_registered"] is False


@pytest.mark.parametrize(
    "change",
    ["issuer", "registration_endpoint", "redirect_uri", "null_client"],
)
def test_a_reconnect_registers_once_when_the_stored_client_does_not_match(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    collector = pinned_collector(intake, served, root=root)
    if change == "issuer":
        served.issuer = f"{ISSUER}/tenant-b"
    elif change == "registration_endpoint":
        served.registration_path = "/register-v2"
    elif change == "redirect_uri":
        configure_connector(monkeypatch, root, routing_host="example.net")
    else:
        with open_unit_of_work(collector.owner) as uow:
            uow.connection.execute(
                update(j.jobfeed_connection).values(
                    client_id=None, client_auth_method=None, client_redirect_uri=None
                )
            )
            uow.commit()
    outcome = run(intake, collector.owner, RECONNECT)
    assert outcome.ok, outcome
    assert len(served.registrations) == 1
    (row,) = pending(collector.owner)
    assert row["client_registered"] is True
    assert row["client_id"] == "issued-client-0001"
    assert json.loads(served.registrations[0].content)["redirect_uris"] == [
        oauth.redirect_uri("leads")
    ]


FAILURES = {
    "registration_failed": (
        answer(400, {"error": "invalid_client_metadata"}),
        "registration_failed",
    ),
    "registration_unsupported": (
        issued(token_endpoint_auth_method="private_key_jwt"),
        "registration_unsupported",
    ),
    "redirect": (redirected(), "endpoint_misconfigured"),
    "oversized": (oversized(), "registration_failed"),
    "malformed": (malformed(), "registration_failed"),
}


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_a_first_connect_whose_registration_fails_stores_nothing(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
    failure: str,
) -> None:
    responder, code = FAILURES[failure]
    served.registration = responder
    before = counts(owner)
    outcome = connect(intake, owner, pipeline_id)
    refused(outcome, code)
    assert len(served.registrations) == 1
    assert counts(owner) == before
    assert pending(owner) == []
    assert secret_files(root) == set()
    (audit,) = audits(owner, CONNECT)
    assert audit["outcome"] == "refused"


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_a_reconnect_whose_registration_fails_writes_only_the_health_code(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    failure: str,
) -> None:
    collector = pinned_collector(intake, served, root=root)
    owner = collector.owner
    served.registration_path = "/register-v2"
    responder, code = FAILURES[failure]
    served.registration = responder
    with open_unit_of_work(owner) as uow:
        before = read_connection(uow.connection)
        schedules = list(uow.connection.execute(select(schedule)).mappings())
    outcome = run(intake, owner, RECONNECT)
    refused(outcome, code)
    assert len(served.registrations) == 1
    with open_unit_of_work(owner) as uow:
        assert read_connection(uow.connection) == before
        assert list(uow.connection.execute(select(schedule)).mappings()) == schedules
        health = (
            uow.connection.execute(
                select(t.connection_health).where(
                    t.connection_health.c.connection_id == collector.connection_id
                )
            )
            .mappings()
            .one()
        )
    assert health["last_error"] == "registration_failed"
    assert health["unresolved_failures"] == 1
    assert pending(owner) == []
    assert secret_files(root) == set()
    (audit,) = audits(owner, RECONNECT)
    assert audit["outcome"] == "refused"


# --- AC 17: one audit row each, no secret in it ---------------------------------------


def test_connect_and_reconnect_each_write_one_audit_row_without_secrets(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    served.registration = with_secrets()
    outcome = connect(intake, owner, pipeline_id)
    assert outcome.ok, outcome
    (row,) = pending(owner)
    state = parse_qs(urlsplit(outcome.result.authorization_url).query)["state"][0]  # type: ignore[union-attr]
    (connected,) = audits(owner, CONNECT)
    assert connected["outcome"] == "succeeded"
    assert connected["subject_ref"] is None
    assert connected["actor_kind"] == "account" and connected["entry"] == "web"

    collector = pinned_collector(intake, served, root=root)
    reconnected = run(intake, collector.owner, RECONNECT)
    assert reconnected.ok, reconnected
    (audit,) = audits(owner, RECONNECT)
    assert audit["outcome"] == "succeeded"
    text_of_rows = repr(audits(owner, CONNECT) + audits(owner, RECONNECT))
    for marker in (row["code_verifier"], state, SECRET, TOKEN):
        assert marker not in text_of_rows


# --- transaction shape: nothing held during discovery or registration --


def _shape_during(
    i: Intake,
    owner: WorkspaceContext,
    server: FakeAuthorizationServer,
    kind: str,
    call: Callable[[], OperationOutcome],
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]], bool, OperationOutcome]:
    hold = Hold()
    server.holds[kind] = hold
    result: list[OperationOutcome] = []
    worker = threading.Thread(target=lambda: result.append(call()))
    worker.start()
    try:
        assert hold.entered.wait(timeout=30), f"{kind} never arrived"
        with open_unit_of_work(owner) as uow:
            busy = [
                dict(row)
                for row in uow.connection.execute(
                    text(
                        "SELECT pid, state, backend_xid::text AS xid "
                        "FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() AND state <> 'idle'"
                    )
                ).mappings()
            ]
            pids = [row["pid"] for row in busy]
            locks = [
                (row.nspname, row.relname, row.mode)
                for row in uow.connection.execute(
                    text(
                        "SELECT n.nspname, c.relname, l.mode FROM pg_locks l "
                        "JOIN pg_class c ON c.oid = l.relation "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE l.pid = ANY(:pids)"
                    ),
                    {"pids": pids},
                )
            ]
            lifecycle_free = uow.connection.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": WORKSPACE_LIFECYCLE_LOCK_KEY},
            ).scalar_one()
            uow.rollback()
    finally:
        hold.release.set()
        worker.join(timeout=60)
    return busy, locks, lifecycle_free, result[0]


@pytest.mark.parametrize("kind", ["discovery", "registration"])
def test_no_leads_lock_or_write_transaction_is_held_during_a_network_call(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    kind: str,
) -> None:
    busy, locks, lifecycle_free, outcome = _shape_during(
        intake, owner, served, kind, lambda: connect(intake, owner, pipeline_id)
    )
    assert outcome.ok, outcome
    # Exactly one open transaction: the dispatcher's. No short transaction of the
    # handler's is open, and the dispatcher's has written nothing (no xid).
    assert len(busy) == 1, busy
    assert busy[0]["state"] == "idle in transaction"
    assert busy[0]["xid"] is None
    # Before calling the handler, core's readiness check (``require_ready``) reads
    # the module's own Alembic version table on the dispatcher's transaction, which
    # holds an access-share lock on that table and its key index. Nothing else in
    # ``leads`` is locked: the handler has added nothing there.
    leads_locks = {
        (relation, mode) for schema, relation, mode in locks if schema == "leads"
    }
    assert leads_locks <= {
        ("alembic_version_leads", "AccessShareLock"),
        ("alembic_version_leads_pkc", "AccessShareLock"),
    }, leads_locks
    assert lifecycle_free is True

"""``leads.jobfeed.revoke``, ``leads.jobfeed.select_account``, the owner health
read's ``collector`` object and the catalog's ``jobfeed`` row.

Seams under test: both operations through the real dispatcher against a fake
authorization server with counting registration, token and revocation endpoints;
the ``leads.connection.health`` projection and the ``leads.ui.catalog`` row through
the real operations. The connected collector is built by the real OAuth client
(registration and code exchange), so its client-secret, registration-token and
refresh-token files are real files under a temporary data root and every "file
gone" or "file kept" assertion reads the file system.
"""

import base64
import logging
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, quote_plus
from uuid import UUID

import httpx
import pytest
from harness.jobfeed import (
    CLIENT_SECRET,
    REGISTRATION_TOKEN,
    STORED_CLIENT_ID,
    ConnectedCollector,
    FakeAuthorizationServer,
    FakeClock,
    FakeResolver,
    Tokens,
    answer,
    connected_collector,
    deliver,
    drain,
    token_error,
)
from rheo_contracts import ActorKind, Role, WorkspaceContext
from rheo_core.boundary.factories import context_from_token
from rheo_core.connectors import oauth
from rheo_core.connectors.oauth import ClientAuth
from rheo_core.exports.artifact import ExportSnapshot
from rheo_core.modules import loaded_manifests
from rheo_core.operations import registry as operation_registry
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.tool_facade import visible_tools
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.contracts import ConnectionHealth
from rheo_leads.export import export_records
from rheo_leads.jobfeed import authorization
from rheo_leads.references import ref
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import Engine, event, insert, select, update

from postgres.test_jobfeed_authorization import (
    COMPLETE,
    application_log,
    client_files,
    client_of,
    collect_schedule,
    complete,
    health,
    restart,
    result,
    set_connection,
    stored,
)
from postgres.test_jobfeed_connect import (
    CONNECT,
    RECONNECT,
    _shape_during,
    audits,
    capability,
    file_of,
    half_owner,
    member_context,
    refused,
    run,
    secret_files,
)
from postgres.test_jobfeed_connect import clock as clock
from postgres.test_jobfeed_connect import owner as owner
from postgres.test_jobfeed_connect import pipeline_id as pipeline_id
from postgres.test_jobfeed_connect import root as root
from postgres.test_jobfeed_connect import served as served
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres

REVOKE = "leads.jobfeed.revoke"
SELECT = "leads.jobfeed.select_account"
HEALTH = "leads.connection.health"
FIVE = frozenset({CONNECT, RECONNECT, COMPLETE, REVOKE, SELECT})
NOW = FakeClock().now()
ORG_ONE = "org-example-1"
ORG_TWO = "org-example-2"
CHOICES = ((ORG_ONE, "Example organization one"), (ORG_TWO, None))
REPORT = {
    "fetched": 11,
    "imported": 7,
    "skipped_dup": 2,
    "refreshed": 1,
    "filtered": 3,
    "skipped_untracked": 4,
    "errors": 5,
    "detail_reads": 6,
    "pages": 8,
}


class InjectedFailure(Exception):
    """Raised from a test listener to fail a statement on purpose."""


@pytest.fixture
def server() -> FakeAuthorizationServer:
    return FakeAuthorizationServer(token=Tokens())


# --- helpers


def connected(
    i: Intake, server: FakeAuthorizationServer, **options: Any
) -> ConnectedCollector:
    return connected_collector(i, server, **options)


def revoke(i: Intake, ctx: WorkspaceContext) -> OperationOutcome:
    return run(i, ctx, REVOKE)


def upstream(outcome: OperationOutcome) -> str:
    """The revoke result's upstream outcome; the local state is always revoked."""
    assert outcome.ok, outcome
    assert outcome.result.state == "revoked"  # type: ignore[union-attr]
    return str(outcome.result.upstream)  # type: ignore[union-attr]


def select_account(
    i: Intake, ctx: WorkspaceContext, organization_id: str
) -> OperationOutcome:
    return run(i, ctx, SELECT, organization_id=organization_id)


def health_read(i: Intake, ctx: WorkspaceContext, connection_id: UUID) -> Any:
    return run(i, ctx, HEALTH, connection_ref=ref("intake_connection", connection_id))


def collector_health(i: Intake, c: ConnectedCollector) -> dict[str, Any]:
    outcome = health_read(i, c.owner, c.connection_id)
    assert outcome.ok and isinstance(outcome.result, ConnectionHealth), outcome
    assert outcome.result.collector is not None
    return outcome.result.collector.model_dump()


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode("ascii"))


def owner_token(
    i: Intake, owner: WorkspaceContext, kind: str, entry: str, **issue: Any
) -> WorkspaceContext:
    """A real owner-role token on ``entry``, issued with ``issue`` (an explicit
    ``operations`` list or a ``set_name``)."""
    # Token issue and resolution read the process-wide operation registry; the
    # intake fixture loads Leads into an isolated one.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(operation_registry, "REGISTRY", i.surfaces.operations)
        issued = run(i, owner, "core.token.issue", kind=kind, **issue)
        assert issued.ok, issued
        ctx = context_from_token(str(issued.result.value), entry)  # type: ignore[union-attr]
    assert isinstance(ctx, WorkspaceContext), ctx
    assert ctx.actor.kind is ActorKind.TOKEN and ctx.role is Role.OWNER
    return ctx


def connection_files(root: Path, c: ConnectedCollector) -> set[Path]:
    return client_files(root, stored(c.owner)) | {file_of(root, c.token_ref)}


def secret_markers(root: Path, c: ConnectedCollector) -> list[str]:
    """Every value a result, log or audit row must never carry."""
    current = stored(c.owner)
    return [
        c.refresh_token,
        c.token_ref,
        CLIENT_SECRET,
        REGISTRATION_TOKEN,
        str(root),
        *(
            value
            for value in (current.client_secret_ref, current.registration_token_ref)
            if value is not None
        ),
    ]


def records(ctx: WorkspaceContext) -> dict[str, list[dict[str, Any]]]:
    with open_unit_of_work(ctx) as uow:
        return {
            table.name: sorted(
                (dict(r) for r in uow.connection.execute(select(table)).mappings()),
                key=repr,
            )
            for table in (
                p.routing_rule,
                p.pipeline,
                p.opportunity,
                p.opportunity_observation,
                t.observation,
            )
        }


def exported(ctx: WorkspaceContext) -> list[dict[str, Any]]:
    with open_unit_of_work(ctx) as uow:
        return list(
            export_records(
                cast(ExportSnapshot, SimpleNamespace(connection=uow.connection))
            )
        )


def collect_one_posting(i: Intake, c: ConnectedCollector) -> None:
    accepted = deliver(
        c.poll,
        i,
        {
            "title": "Example posting",
            "posting_id": "posting-0001",
            "observed_at": "2026-01-05T09:00:00+00:00",
        },
        "posting-0001:1",
    )
    assert accepted.ok, accepted
    drain(i)


def _is_end_update(statement: str) -> bool:
    return (
        statement.lstrip()
        .upper()
        .startswith("UPDATE LEADS.JOBFEED_CONNECTION SET AUTHORIZATION_STATE")
    )


def _is_client_clear(statement: str) -> bool:
    return (
        statement.lstrip()
        .upper()
        .startswith("UPDATE LEADS.JOBFEED_CONNECTION SET CLIENT_ID")
    )


# --- AC 1: who may revoke and select ---------------------------------------------


def test_revoke_and_select_are_refused_for_every_caller_but_the_owners_browser_session(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(intake, served, choices=CHOICES)
    before = stored(c.owner)
    operations = [REVOKE, SELECT]
    cases = [
        member_context(intake),  # (a) a member role
        # (b) an agent, (e) an owner-role token on MCP; (e) one on the API
        owner_token(intake, c.owner, "mcp", "mcp", operations=operations),
        owner_token(intake, c.owner, "cli", "api", operations=operations),
        *half_owner(c.owner),
    ]
    for ctx in cases:
        refused(revoke(intake, ctx), "role_not_permitted")
        refused(select_account(intake, ctx, ORG_ONE), "role_not_permitted")
    # (b) a connection actor: its operation set holds only its acceptance operation.
    refused(revoke(intake, c.poll), "operation_not_permitted")
    refused(select_account(intake, c.poll, ORG_ONE), "operation_not_permitted")
    # (c) the capability off: the owner cannot revoke in-app.
    capability(intake, c.owner, False)
    refused(revoke(intake, c.owner), "capability_disabled")
    refused(select_account(intake, c.owner, ORG_ONE), "capability_disabled")
    assert served.requests == []
    assert stored(c.owner) == before
    assert file_of(root, c.token_ref).exists()

    capability(intake, c.owner, True)
    assert select_account(intake, c.owner, ORG_ONE).ok
    assert upstream(revoke(intake, c.owner)) == "revoked"


def test_revoke_and_select_without_a_connection_are_not_found(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    refused(revoke(intake, owner), "not_found")
    refused(select_account(intake, owner, ORG_ONE), "not_found")
    assert served.requests == []


# --- AC 15, I22, AC 62, AC 63: revoke ends locally, then revokes the captured token


def test_revoke_clears_and_captures_before_revoking_exactly_that_reference(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(intake, served)
    token_file = file_of(root, c.token_ref)
    assert token_file.exists()
    assert collect_schedule(c.owner)["enabled"] is True  # type: ignore[index]
    at_request: list[tuple[Any, ...]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        current = stored(c.owner)
        at_request.append(
            (
                current.token_ref,
                current.credential_generation,
                current.authorization_state,
                token_file.exists(),
            )
        )
        return httpx.Response(200)

    served.revoke = respond
    ended: set[int] = set()
    at_commit: list[bool] = []

    def watch(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if _is_end_update(statement):
            ended.add(id(conn))

    def on_commit(conn: Any) -> None:
        if id(conn) in ended:
            ended.discard(id(conn))
            at_commit.append(token_file.exists())

    event.listen(Engine, "before_cursor_execute", watch)
    event.listen(Engine, "commit", on_commit)
    try:
        outcome = revoke(intake, c.owner)
    finally:
        event.remove(Engine, "before_cursor_execute", watch)
        event.remove(Engine, "commit", on_commit)

    assert upstream(outcome) == "revoked"
    # The local end committed with the file still present; the one request came
    # after it, with the reference already cleared and the generation bumped.
    assert at_commit == [True]
    assert at_request == [(None, 2, "revoked", True)]
    (request,) = served.revocation_requests
    assert form(request)["token"] == [c.refresh_token]
    assert form(request)["token_type_hint"] == ["refresh_token"]
    # Exactly that file went, after the request; the stored client was kept.
    assert not token_file.exists()
    after = stored(c.owner)
    assert after.token_ref is None and after.credential_generation == 2
    assert after.authorization_state == "revoked"
    assert after.client_id == STORED_CLIENT_ID
    assert client_files(root, after) and all(
        f.exists() for f in client_files(root, after)
    )
    schedule = collect_schedule(c.owner)
    assert schedule is not None and schedule["enabled"] is False
    recorded = health(c.owner, c.connection_id)
    assert recorded is not None and recorded["last_error"] == "authorization_required"
    assert collector_health(intake, c)["authorization_state"] == "revoked"
    assert served.registrations == [] and served.token_requests == []


@pytest.mark.parametrize(
    "responder",
    [answer(503), answer(400, {"error": "invalid_request"}), answer(302)],
    ids=["unavailable", "rejected-token", "redirected"],
)
def test_an_upstream_failure_still_ends_locally(
    intake: Intake, root: Path, served: FakeAuthorizationServer, responder: Any
) -> None:
    c = connected(intake, served)
    files = client_files(root, stored(c.owner))
    served.revoke = responder
    assert upstream(revoke(intake, c.owner)) == "revocation_failed"
    assert len(served.revocation_requests) == 1
    after = stored(c.owner)
    assert after.token_ref is None and after.authorization_state == "revoked"
    assert not file_of(root, c.token_ref).exists()
    # Not a client rejection: the stored client and its files stay.
    assert client_files(root, after) == files and all(f.exists() for f in files)
    assert collect_schedule(c.owner)["enabled"] is False  # type: ignore[index]


def test_no_pinned_revocation_endpoint_is_unavailable_with_no_request(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    served.revocation = False
    c = connected(intake, served)
    assert stored(c.owner).revocation_endpoint is None
    assert upstream(revoke(intake, c.owner)) == "revocation_unavailable"
    assert served.requests == []
    assert stored(c.owner).token_ref is None
    assert not file_of(root, c.token_ref).exists()


def test_no_stored_client_is_unavailable_with_no_request(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    """With no client to authenticate as, no revocation request can be made."""
    c = connected(intake, served, registration_token=False)
    set_connection(
        c.owner,
        client_id=None,
        client_auth_method=None,
        client_redirect_uri=None,
    )
    assert upstream(revoke(intake, c.owner)) == "revocation_unavailable"
    assert served.requests == []
    after = stored(c.owner)
    assert after.token_ref is None and after.authorization_state == "revoked"
    assert secret_files(root) == set()


def test_collected_records_are_unchanged_and_exportable_after_revoke(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(intake, served)
    collect_one_posting(intake, c)
    kept = records(c.owner)
    assert kept["opportunity_observation"] and kept["observation"], kept
    assert upstream(revoke(intake, c.owner)) == "revoked"
    assert records(c.owner) == kept
    exported_ids = {row.get("id") for row in exported(c.owner)}
    for table in ("observation", "opportunity"):
        assert {row["id"] for row in kept[table]} <= exported_ids


def test_no_leads_lock_is_held_during_a_slow_revocation(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(intake, served)
    busy, locks, lifecycle_free, outcome = _shape_during(
        intake, c.owner, served, "revocation", lambda: revoke(intake, c.owner)
    )
    assert upstream(outcome) == "revoked"
    # Only the dispatcher's transaction is open, and it has written nothing.
    assert len(busy) == 1, busy
    assert busy[0]["state"] == "idle in transaction"
    assert busy[0]["xid"] is None
    # The readiness check's read of the module's version table is all it holds.
    leads_locks = {
        (relation, mode) for schema, relation, mode in locks if schema == "leads"
    }
    assert leads_locks <= {
        ("alembic_version_leads", "AccessShareLock"),
        ("alembic_version_leads_pkc", "AccessShareLock"),
    }, leads_locks
    assert lifecycle_free is True


def test_a_local_end_that_fails_while_closing_still_revokes_and_discards(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Once the local end has committed, a failure while its unit closes is not
    raised: it is logged as a fixed code only, and the captured reference is still
    revoked and its file discarded."""
    c = connected(intake, served)
    markers = secret_markers(root, c)
    ended: set[int] = set()
    real_open = authorization.open_unit_of_work

    def watch(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if _is_end_update(statement):
            ended.add(id(conn))

    class FailingClose:
        def __init__(self, ctx: WorkspaceContext) -> None:
            self.inner = real_open(ctx)

        def __enter__(self) -> Any:
            entered = self.inner.__enter__()
            self.conn = entered.connection
            return entered

        def __exit__(self, *exc: Any) -> None:
            self.inner.__exit__(*exc)
            if exc[0] is None and id(self.conn) in ended:
                raise InjectedFailure("close-failure-marker")

    monkeypatch.setattr(authorization, "open_unit_of_work", FailingClose)
    event.listen(Engine, "before_cursor_execute", watch)
    try:
        with caplog.at_level(logging.INFO):
            outcome = revoke(intake, c.owner)
    finally:
        event.remove(Engine, "before_cursor_execute", watch)
    assert ended
    assert upstream(outcome) == "revoked"
    (request,) = served.revocation_requests
    assert form(request)["token"] == [c.refresh_token]
    assert stored(c.owner).token_ref is None
    assert not file_of(root, c.token_ref).exists()
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("rheo")]
    assert ours == [authorization.REVOKE_LOCAL_END_CLOSE_FAILED]
    logged = application_log(caplog)
    for marker in (*markers, "close-failure-marker", "InjectedFailure"):
        assert marker not in logged


# --- FR 5, FR 9: the request authenticates as the stored client -------------------


@pytest.mark.parametrize(
    "method", ["none", "client_secret_basic", "client_secret_post"]
)
def test_the_revocation_request_authenticates_per_the_stored_client(
    intake: Intake, root: Path, served: FakeAuthorizationServer, method: str
) -> None:
    c = connected(intake, served, auth_method=method)
    assert stored(c.owner).client_auth_method == method
    assert upstream(revoke(intake, c.owner)) == "revoked"
    (request,) = served.revocation_requests
    sent = form(request)
    assert sent["token"] == [c.refresh_token]
    authorization_header = request.headers.get("authorization")
    if method == "none":
        assert sent["client_id"] == [STORED_CLIENT_ID]
        assert authorization_header is None
        assert "client_secret" not in sent
    elif method == "client_secret_basic":
        pair = f"{quote_plus(STORED_CLIENT_ID)}:{quote_plus(CLIENT_SECRET)}"
        expected = base64.b64encode(pair.encode()).decode()
        assert authorization_header == f"Basic {expected}"
        assert "client_id" not in sent and "client_secret" not in sent
    else:
        assert sent["client_id"] == [STORED_CLIENT_ID]
        assert sent["client_secret"] == [CLIENT_SECRET]
        assert authorization_header is None


def test_an_expired_client_secret_is_never_sent_and_the_client_is_cleared(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(
        intake,
        served,
        auth_method="client_secret_basic",
        client_secret_expires_at=NOW - timedelta(minutes=1),
    )
    files = client_files(root, stored(c.owner))
    assert len(files) == 2
    assert upstream(revoke(intake, c.owner)) == "revocation_failed"
    assert served.requests == []
    after = stored(c.owner)
    assert client_of(after) == (None,) * 6
    assert after.token_ref is None and after.authorization_state == "revoked"
    assert not any(f.exists() for f in files)
    assert secret_files(root) == set()


# --- AC 79, I31, AC 78: invalid_client clears the stored client --------------------


def test_invalid_client_clears_the_stored_client_and_the_next_reconnect_registers(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(intake, served, auth_method="client_secret_post")
    files = client_files(root, stored(c.owner))
    assert len(files) == 2
    served.revoke = token_error("invalid_client", status=401)
    clearing: set[int] = set()
    at_commit: list[bool] = []

    def watch(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if _is_client_clear(statement):
            clearing.add(id(conn))

    def on_commit(conn: Any) -> None:
        if id(conn) in clearing:
            clearing.discard(id(conn))
            at_commit.append(all(f.exists() for f in files))

    event.listen(Engine, "before_cursor_execute", watch)
    event.listen(Engine, "commit", on_commit)
    try:
        outcome = revoke(intake, c.owner)
    finally:
        event.remove(Engine, "before_cursor_execute", watch)
        event.remove(Engine, "commit", on_commit)
    assert upstream(outcome) == "revocation_failed"
    # The client files still existed when the clearing unit committed.
    assert at_commit == [True]
    assert not any(f.exists() for f in files)
    after = stored(c.owner)
    assert client_of(after) == (None,) * 6
    assert after.token_ref is None and after.authorization_state == "revoked"
    assert secret_files(root) == set()
    assert served.registrations == []

    # The next reconnect registers exactly once and yields a working connection.
    params = restart(intake, c.owner, served)
    assert len(served.registrations) == 1
    assert result(complete(intake, c.owner, **params)) == ("connected", None)
    assert len(served.registrations) == 1
    reconnected = stored(c.owner)
    assert reconnected.client_id not in (None, STORED_CLIENT_ID)
    assert reconnected.authorization_state == "connected"


def test_invalid_client_never_strips_a_reconnect_that_completed_meanwhile(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    """A reconnect that completes while revoke waits on the revocation endpoint,
    reusing the very same stored client, keeps that client when the endpoint then
    answers ``invalid_client``: the clearing requires the connection still to be
    revoked at the generation revoke wrote."""
    c = connected(intake, served, auth_method="client_secret_basic")
    files = client_files(root, stored(c.owner))
    assert len(files) == 2
    completed: list[tuple[str, str | None]] = []

    def reconnect_meanwhile() -> None:
        params = restart(intake, c.owner, served)
        completed.append(result(complete(intake, c.owner, **params)))

    def respond(request: httpx.Request) -> httpx.Response:
        # Another request, on its own thread, finishes a reconnect first.
        worker = threading.Thread(target=reconnect_meanwhile)
        worker.start()
        worker.join(timeout=60)
        return httpx.Response(401, json={"error": "invalid_client"})

    served.revoke = respond
    assert upstream(revoke(intake, c.owner)) == "revocation_failed"
    assert completed == [("connected", None)]
    assert served.registrations == []  # the reconnect reused the stored client
    after = stored(c.owner)
    assert after.authorization_state == "connected"
    assert after.client_id == STORED_CLIENT_ID
    assert after.credential_generation == 3
    assert after.token_ref is not None and file_of(root, after.token_ref).exists()
    assert client_files(root, after) == files and all(f.exists() for f in files)


def test_a_normal_revoke_keeps_the_client_and_the_next_reconnect_reuses_it(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    """AC 16: reconnect after a real revoke restores ``connected`` on the same
    connection, rules and pipeline, reusing the stored client."""
    c = connected(intake, served, auth_method="client_secret_basic")
    collect_one_posting(intake, c)
    kept = records(c.owner)
    before = stored(c.owner)
    assert upstream(revoke(intake, c.owner)) == "revoked"
    assert client_of(stored(c.owner)) == client_of(before)

    params = restart(intake, c.owner, served)
    assert result(complete(intake, c.owner, **params)) == ("connected", None)
    assert served.registrations == []
    after = stored(c.owner)
    assert client_of(after) == client_of(before)
    assert after.connection_id == before.connection_id
    assert after.pipeline_id == before.pipeline_id
    assert after.authorization_state == "connected"
    assert after.token_ref is not None and after.token_ref != before.token_ref
    assert after.credential_generation == before.credential_generation + 2
    assert records(c.owner) == kept
    recorded = health(c.owner, c.connection_id)
    assert recorded is not None and recorded["last_error"] is None
    assert collect_schedule(c.owner)["enabled"] is True  # type: ignore[index]


def test_select_and_revoke_never_register(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    """AC 78 for these operations, across a selection, a normal revoke and an
    ``invalid_client`` revoke."""
    c = connected(intake, served, choices=CHOICES)
    assert select_account(intake, c.owner, ORG_ONE).ok
    assert upstream(revoke(intake, c.owner)) == "revoked"
    # A second revoke on the revoked connection has no token to send.
    assert upstream(revoke(intake, c.owner)) == "revocation_unavailable"
    assert len(served.revocation_requests) == 1
    # Reconnect reusing the stored client, then an invalid_client revoke.
    params = restart(intake, c.owner, served)
    assert result(complete(intake, c.owner, **params)) == ("connected", None)
    served.revoke = token_error("invalid_client")
    assert upstream(revoke(intake, c.owner)) == "revocation_failed"
    assert len(served.revocation_requests) == 2
    assert stored(c.owner).client_id is None
    assert served.registrations == []


# --- r2-b2: secret-file failures never fail a revoke -------------------------------


def test_a_refresh_token_file_removed_by_hand_never_fails_a_revoke(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The host operator's path while the capability was off: the file is gone."""
    c = connected(intake, served)
    files = client_files(root, stored(c.owner))
    file_of(root, c.token_ref).unlink()
    markers = secret_markers(root, c)
    with caplog.at_level(logging.INFO):
        outcome = revoke(intake, c.owner)
    assert upstream(outcome) == "revocation_failed"
    assert served.requests == []
    after = stored(c.owner)
    assert after.token_ref is None and after.authorization_state == "revoked"
    # Not a client rejection: the client and its files stay.
    assert after.client_id == STORED_CLIENT_ID and all(f.exists() for f in files)
    (audit,) = audits(c.owner, REVOKE)
    assert audit["outcome"] == "succeeded"
    leaked = repr(outcome) + application_log(caplog) + repr(audits(c.owner, REVOKE))
    for marker in markers:
        assert marker not in leaked


def test_a_client_secret_file_removed_by_hand_clears_the_client(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    c = connected(intake, served, auth_method="client_secret_basic")
    before = stored(c.owner)
    assert before.client_secret_ref is not None
    assert before.registration_token_ref is not None
    file_of(root, before.client_secret_ref).unlink()
    markers = secret_markers(root, c)
    with caplog.at_level(logging.INFO):
        outcome = revoke(intake, c.owner)
    assert upstream(outcome) == "revocation_failed"
    assert served.requests == []
    after = stored(c.owner)
    assert client_of(after) == (None,) * 6
    assert after.token_ref is None and after.authorization_state == "revoked"
    assert secret_files(root) == set()
    (audit,) = audits(c.owner, REVOKE)
    assert audit["outcome"] == "succeeded"
    leaked = repr(outcome) + application_log(caplog) + repr(audits(c.owner, REVOKE))
    for marker in markers:
        assert marker not in leaked


def test_a_failing_client_clearing_unit_never_fails_the_revoke(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    c = connected(intake, served, auth_method="client_secret_basic")
    before = stored(c.owner)
    files = client_files(root, before)
    assert len(files) == 2
    served.revoke = token_error("invalid_client", status=401)
    markers = secret_markers(root, c)

    def explode(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if _is_client_clear(statement):
            raise InjectedFailure("client-clear-failure-marker")

    event.listen(Engine, "before_cursor_execute", explode)
    try:
        with caplog.at_level(logging.INFO):
            outcome = revoke(intake, c.owner)
    finally:
        event.remove(Engine, "before_cursor_execute", explode)
    assert upstream(outcome) == "revocation_failed"
    (request,) = served.revocation_requests
    assert form(request)["token"] == [c.refresh_token]
    after = stored(c.owner)
    # The local end stands; the client columns and their files are intact.
    assert after.token_ref is None and after.authorization_state == "revoked"
    assert client_of(after) == client_of(before)
    assert all(f.exists() for f in files)
    assert not file_of(root, c.token_ref).exists()
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("rheo")]
    assert ours == [authorization.CLIENT_CLEAR_FAILED]
    (audit,) = audits(c.owner, REVOKE)
    assert audit["outcome"] == "succeeded"
    leaked = repr(outcome) + application_log(caplog) + repr(audits(c.owner, REVOKE))
    for marker in (*markers, "client-clear-failure-marker", "InjectedFailure"):
        assert marker not in leaked


# --- AC 69 (revoke half): a rotation that committed before revoke's lock ----------


def test_revoke_sends_the_token_a_rotation_committed_first(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(intake, served, registration_token=False)
    first = stored(c.owner)
    discovered = served.discovered()
    rotating = Tokens(rotating=True)
    served.token = rotating
    _access, rotated = oauth.refresh(
        discovered.token_endpoint,
        refresh_ref=c.token_ref,
        resource=discovered.resource_url,
        client=ClientAuth(
            client_id=STORED_CLIENT_ID,
            auth_method="none",
            client_secret_ref=None,
            secret_expires_at=None,
        ),
        workspace_id=intake.workspace,
        connection_id=c.connection_id,
        resolver=FakeResolver(),
        clock=FakeClock().monotonic,
        transport=httpx.MockTransport(served.handle),
        now=NOW,
    )
    assert rotated is not None and rotated != c.token_ref
    minted = rotating.issued[-1]
    assert minted.startswith("refresh-marker-")
    # The rotation's compare-and-set commits before revoke takes the lock, then its
    # retired file goes.
    with open_unit_of_work(c.owner) as uow:
        switched = uow.connection.execute(
            update(j.jobfeed_connection)
            .where(
                j.jobfeed_connection.c.token_ref == first.token_ref,
                j.jobfeed_connection.c.credential_generation
                == first.credential_generation,
            )
            .values(
                token_ref=rotated,
                credential_generation=first.credential_generation + 1,
            )
        )
        assert switched.rowcount == 1
        uow.commit()
    oauth.discard(c.token_ref)

    assert upstream(revoke(intake, c.owner)) == "revoked"
    sent = [form(r)["token"] for r in served.revocation_requests]
    assert sent == [[minted]]
    after = stored(c.owner)
    assert after.token_ref is None
    assert after.credential_generation == first.credential_generation + 2
    assert secret_files(root) == set()


# --- AC 19, AC 73, I28: account selection -----------------------------------------


def test_select_accepts_only_a_stored_choice_and_calls_nothing(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(intake, served, choices=CHOICES)
    assert [
        (choice["organization_id"], choice["label"])
        for choice in collector_health(intake, c)["account_choices"]
    ] == list(CHOICES)
    before = stored(c.owner)

    refused(select_account(intake, c.owner, "org-example-9"), "not_found")
    assert stored(c.owner) == before
    selected = select_account(intake, c.owner, ORG_TWO)
    assert selected.ok, selected
    assert selected.result.state == "connected"  # type: ignore[union-attr]
    after = stored(c.owner)
    assert after.organization_id == ORG_TWO
    assert collector_health(intake, c)["organization_id"] == ORG_TWO
    # No vendor call and no refresh; the token is untouched.
    assert served.requests == []
    assert after.token_ref == before.token_ref
    assert after.credential_generation == before.credential_generation
    # A choice stored for another connection is not one of this connection's.
    with open_unit_of_work(c.owner) as uow:
        uow.connection.execute(
            insert(j.jobfeed_account_choice).values(
                connection_id=intake.connection_id,
                organization_id="org-example-9",
                label=None,
                ordinal=0,
                seen_at=NOW,
            )
        )
        uow.commit()
    refused(select_account(intake, c.owner, "org-example-9"), "not_found")
    assert stored(c.owner).organization_id == ORG_TWO
    assert served.requests == []


def test_select_clears_only_account_selection_required(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(intake, served, choices=CHOICES)

    def set_error(code: str) -> None:
        with open_unit_of_work(c.owner) as uow:
            uow.connection.execute(
                update(t.connection_health)
                .where(t.connection_health.c.connection_id == c.connection_id)
                .values(last_error=code, last_error_at=NOW)
            )
            uow.commit()

    set_error("upstream_unavailable")
    assert select_account(intake, c.owner, ORG_ONE).ok
    assert health(c.owner, c.connection_id)["last_error"] == "upstream_unavailable"  # type: ignore[index]
    set_error("account_selection_required")
    assert select_account(intake, c.owner, ORG_ONE).ok
    recorded = health(c.owner, c.connection_id)
    assert recorded is not None
    assert recorded["last_error"] is None and recorded["last_error_at"] is None


def test_select_five_minutes_after_a_claim_is_due_when_the_claim_window_ends(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    claimed = NOW - timedelta(minutes=5)
    c = connected(intake, served, choices=CHOICES, run_claimed_at=claimed)
    assert select_account(intake, c.owner, ORG_ONE).ok
    floor = claimed + timedelta(minutes=60)
    schedule = collect_schedule(c.owner)
    assert schedule is not None
    assert schedule["enabled"] is True and schedule["next_run_at"] == floor
    assert collector_health(intake, c)["next_run_not_before"] == floor


def test_select_with_no_prior_claim_is_due_now(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(intake, served, choices=CHOICES)
    assert select_account(intake, c.owner, ORG_ONE).ok
    schedule = collect_schedule(c.owner)
    assert schedule is not None
    assert schedule["enabled"] is True and schedule["next_run_at"] == NOW
    assert collector_health(intake, c)["next_run_not_before"] is None


def test_select_on_a_revoked_connection_keeps_the_schedule_off(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(intake, served, choices=CHOICES)
    assert upstream(revoke(intake, c.owner)) == "revoked"
    selected = select_account(intake, c.owner, ORG_ONE)
    assert selected.ok and selected.result.state == "revoked"  # type: ignore[union-attr]
    assert collect_schedule(c.owner)["enabled"] is False  # type: ignore[index]


# --- AC 51: the projection ---------------------------------------------------------


def test_the_collector_object_carries_exactly_the_stored_state(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    completed = NOW - timedelta(hours=2)
    claimed = NOW - timedelta(minutes=30)
    c = connected(
        intake,
        served,
        auth_method="client_secret_post",
        report=REPORT,
        choices=CHOICES,
        organization_id=ORG_ONE,
        last_completed_at=completed,
        run_claimed_at=claimed,
    )
    with open_unit_of_work(c.owner) as uow:
        uow.connection.execute(
            update(t.connection_health)
            .where(t.connection_health.c.connection_id == c.connection_id)
            .values(last_error="collection_partial", last_error_at=NOW)
        )
        uow.commit()
    outcome = health_read(intake, c.owner, c.connection_id)
    assert outcome.ok and isinstance(outcome.result, ConnectionHealth), outcome
    assert outcome.result.last_error == "collection_partial"
    assert outcome.result.last_error_at == NOW
    assert collector_health(intake, c) == {
        "authorization_state": "connected",
        "last_completed_at": completed,
        "next_run_not_before": claimed + timedelta(minutes=60),
        **REPORT,
        "organization_id": ORG_ONE,
        "account_choices": [
            {"organization_id": organization_id, "label": label}
            for organization_id, label in CHOICES
        ],
    }
    current = stored(c.owner)
    rendered = repr(outcome.result.model_dump())
    for value in (
        *secret_markers(root, c),
        current.client_id,
        current.client_redirect_uri,
        current.resource_url,
        current.issuer,
        current.authorization_endpoint,
        current.token_endpoint,
        current.registration_endpoint,
        current.revocation_endpoint,
    ):
        assert value is not None and value not in rendered

    # The later bound wins; with neither bound there is no earliest next run.
    later = NOW + timedelta(hours=3)
    set_connection(c.owner, not_before=later)
    assert collector_health(intake, c)["next_run_not_before"] == later
    set_connection(c.owner, not_before=None, run_claimed_at=None)
    assert collector_health(intake, c)["next_run_not_before"] is None

    # Only the poll connection carries the object.
    manual = health_read(intake, c.owner, intake.connection_id)
    assert manual.ok and manual.result.collector is None  # type: ignore[union-attr]


def test_no_member_or_agent_can_read_the_collector_health(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(intake, served, choices=CHOICES, organization_id=ORG_ONE)
    refused(
        health_read(intake, member_context(intake), c.connection_id),
        "role_not_permitted",
    )
    # An agent's token carries the operations the loaded modules' tools name (the
    # ``agent_default`` set, built here from the isolated registries the fixture
    # loaded into); the health read has no tool, so it is never among them.
    tool_operations = sorted(
        {
            tool.operation
            for manifest in loaded_manifests().values()
            for tool in manifest.tools
        }
    )
    assert HEALTH not in tool_operations
    agent = owner_token(intake, c.owner, "mcp", "mcp", operations=tool_operations)
    refused(health_read(intake, agent, c.connection_id), "operation_not_permitted")
    assert collector_health(intake, c)["organization_id"] == ORG_ONE


def test_an_owner_token_reads_the_collector_without_organization_ids(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    """Owner tokens are commonly held by agents: the organization ids go only to
    the owner's own session; the rest of the collector object stays."""
    c = connected(
        intake,
        served,
        report=REPORT,
        choices=CHOICES,
        organization_id=ORG_ONE,
    )
    session = collector_health(intake, c)
    assert session["organization_id"] == ORG_ONE
    assert [choice["organization_id"] for choice in session["account_choices"]] == [
        ORG_ONE,
        ORG_TWO,
    ]
    token = owner_token(intake, c.owner, "cli", "api", operations=[HEALTH])
    outcome = health_read(intake, token, c.connection_id)
    assert outcome.ok and isinstance(outcome.result, ConnectionHealth), outcome
    assert outcome.result.collector is not None
    read = outcome.result.collector.model_dump()
    assert read == {**session, "organization_id": None, "account_choices": []}
    rendered = repr(outcome.result.model_dump())
    for organization_id, label in CHOICES:
        assert organization_id not in rendered
        assert label is None or label not in rendered


# --- AC 17: one audit row each, with no secret and no organization id -------------


def test_revoke_and_select_each_write_one_audit_row_without_secrets(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = connected(intake, served, auth_method="client_secret_post", choices=CHOICES)
    markers = secret_markers(root, c)
    assert select_account(intake, c.owner, ORG_TWO).ok
    assert upstream(revoke(intake, c.owner)) == "revoked"
    (selected,) = audits(c.owner, SELECT)
    (revoked,) = audits(c.owner, REVOKE)
    for row in (selected, revoked):
        assert row["outcome"] == "succeeded"
        assert row["subject_ref"] is None
        assert row["actor_kind"] == "account" and row["entry"] == "web"
    rendered = repr([selected, revoked])
    for marker in (*markers, ORG_TWO, ORG_ONE):
        assert marker not in rendered


# --- AC 27, FR 24: no tool in either state; the catalog row only while on ----------


def test_no_tool_exists_for_the_five_operations_in_either_capability_state(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    for manifest in loaded_manifests().values():
        assert not {tool.operation for tool in manifest.tools} & FIVE, (
            manifest.module_id
        )
    c = connected(intake, served, choices=CHOICES)
    # An owner MCP token whose operation set names all five, the health read and an
    # operation that does have a tool: only the absence of a tool keeps them out.
    mcp = owner_token(
        intake,
        c.owner,
        "mcp",
        "mcp",
        operations=[*sorted(FIVE), HEALTH, "leads.intake.capture"],
    )

    def listed() -> list[str]:
        return [
            tool.name
            for tool in visible_tools(
                mcp, tools=intake.surfaces.tools, registry=intake.surfaces.operations
            )
        ]

    def catalog_rows(kind: str) -> list[dict[str, Any]]:
        outcome = run(intake, c.owner, "leads.ui.catalog")
        assert outcome.ok, outcome
        return [row for row in outcome.result.items if row["kind"] == kind]  # type: ignore[union-attr]

    on = listed()
    assert "leads_capture" in on  # positive control: the listing does list tools
    assert catalog_rows("jobfeed") == [{"kind": "jobfeed"}]

    capability(intake, c.owner, False)
    assert listed() == on
    assert catalog_rows("jobfeed") == []
    refused(revoke(intake, c.owner), "capability_disabled")
    refused(select_account(intake, c.owner, ORG_ONE), "capability_disabled")

    capability(intake, c.owner, True)
    assert listed() == on
    assert catalog_rows("jobfeed") == [{"kind": "jobfeed"}]
    declared = {
        tool.operation
        for tool in visible_tools(
            mcp, tools=intake.surfaces.tools, registry=intake.surfaces.operations
        )
    }
    assert not declared & (FIVE | {HEALTH})


def test_turning_the_capability_off_and_on_keeps_the_connection_and_reference(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    """AC 28."""
    c = connected(intake, served, choices=CHOICES, organization_id=ORG_ONE)
    before = stored(c.owner)
    files = connection_files(root, c)
    capability(intake, c.owner, False)
    assert stored(c.owner) == before
    capability(intake, c.owner, True)
    assert stored(c.owner) == before
    assert all(f.exists() for f in files)
    assert served.requests == []

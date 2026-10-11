"""``leads.jobfeed.complete_authorization``, the job feed's OAuth completion.

Seams under test: the operation through the real identity-host callback route
(``rheo_app_core.connector_oauth_routes``) with a real session cookie, and directly
through the dispatcher where noted, against a fake authorization server that mints
codes and answers the token endpoint. The route finds the completion through the
really loaded Leads manifest (``loaded_manifests`` is not patched); only its
``dispatch`` is pointed at the operation registry the loader filled for that module,
because the Leads fixture loads into an isolated registry rather than the process one.

Secret files land in a real file store under a temporary data root, so every "no
file" assertion reads the file system. The schedule rows, the connection, its health
and the audit rows are read from the workspace database.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.jobfeed import (
    FakeAuthorizationServer,
    FakeClock,
    FakeMcpServer,
    Tokens,
    deliver,
    drain,
    poll_context,
    serve,
    token_error,
)
from harness.registry import add_member
from rheo_app_core import connector_oauth_routes
from rheo_app_core.connector_oauth_routes import CONNECTED_PAGE, REFUSED_PAGE
from rheo_app_core.main import public_app
from rheo_contracts import Entry, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.connectors import oauth
from rheo_core.modules import loaded_manifests
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.secrets import SecretStore
from rheo_core.sessions import create_session, mint_host_secret, switch_workspace
from rheo_core.sessions.cookies import SESSION_COOKIE
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import job, schedule
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_leads.jobfeed.run import COLLECT_JOB_KIND, SCHEDULE_NAME
from rheo_leads.jobfeed.state import JobfeedConnection, read_connection
from rheo_leads.manifest import MANIFEST
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import Engine, event, select, update

from postgres.test_jobfeed_connect import (
    RECONNECT,
    SECRET,
    TOKEN,
    _shape_during,
    audits,
    connect,
    counts,
    dump,
    file_of,
    pending,
    row_files,
    run,
    secret_files,
    with_secrets,
)
from postgres.test_jobfeed_connect import clock as clock
from postgres.test_jobfeed_connect import owner as owner
from postgres.test_jobfeed_connect import pipeline_id as pipeline_id
from postgres.test_jobfeed_connect import root as root
from postgres.test_jobfeed_connect import served as served
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres

COMPLETE = "leads.jobfeed.complete_authorization"
JOBSEARCH = "leads.capabilities.jobsearch"
NOW = FakeClock().now()


class InjectedFailure(Exception):
    """Raised from a test listener to fail a statement on purpose."""


@pytest.fixture
def server() -> FakeAuthorizationServer:
    return FakeAuthorizationServer(token=Tokens())


# --- helpers


def authorize(
    server: FakeAuthorizationServer, outcome: OperationOutcome
) -> dict[str, str]:
    """The owner consents: the callback parameters the provider would send."""
    assert outcome.ok, outcome
    state, code = server.authorize(outcome.result.authorization_url)  # type: ignore[union-attr]
    return {"state": state, "code": code, "iss": server.issuer}


def start(
    i: Intake, ctx: WorkspaceContext, server: FakeAuthorizationServer, pipeline: UUID
) -> dict[str, str]:
    return authorize(server, connect(i, ctx, pipeline))


def restart(
    i: Intake, ctx: WorkspaceContext, server: FakeAuthorizationServer
) -> dict[str, str]:
    return authorize(server, run(i, ctx, RECONNECT))


def complete(i: Intake, ctx: WorkspaceContext, **params: str) -> OperationOutcome:
    return run(i, ctx, COMPLETE, **params)


def result(outcome: OperationOutcome) -> tuple[str, str | None]:
    assert outcome.ok, outcome
    return outcome.result.state, outcome.result.reason  # type: ignore[union-attr]


def connection(ctx: WorkspaceContext) -> JobfeedConnection | None:
    with open_unit_of_work(ctx) as uow:
        return read_connection(uow.connection)


def stored(ctx: WorkspaceContext) -> JobfeedConnection:
    current = connection(ctx)
    assert current is not None
    return current


def health(ctx: WorkspaceContext, connection_id: UUID) -> dict[str, Any] | None:
    with open_unit_of_work(ctx) as uow:
        row = (
            uow.connection.execute(
                select(t.connection_health).where(
                    t.connection_health.c.connection_id == connection_id
                )
            )
            .mappings()
            .one_or_none()
        )
    return None if row is None else dict(row)


def collect_schedule(ctx: WorkspaceContext) -> dict[str, Any] | None:
    with open_unit_of_work(ctx) as uow:
        rows = [
            dict(row)
            for row in uow.connection.execute(
                select(schedule).where(
                    schedule.c.module_id == "leads", schedule.c.name == SCHEDULE_NAME
                )
            ).mappings()
        ]
    assert len(rows) <= 1, rows
    return rows[0] if rows else None


def set_connection(ctx: WorkspaceContext, **values: Any) -> None:
    with open_unit_of_work(ctx) as uow:
        uow.connection.execute(update(j.jobfeed_connection).values(**values))
        uow.commit()


def client_files(root: Path, current: JobfeedConnection) -> set[Path]:
    return {
        file_of(root, ref)
        for ref in (current.client_secret_ref, current.registration_token_ref)
        if ref is not None
    }


def client_of(current: JobfeedConnection) -> tuple[Any, ...]:
    return (
        current.client_id,
        current.client_auth_method,
        current.client_redirect_uri,
        current.client_secret_expires_at,
        current.client_secret_ref,
        current.registration_token_ref,
    )


def endpoints_of(current: JobfeedConnection) -> tuple[Any, ...]:
    return (
        current.resource_url,
        current.issuer,
        current.authorization_endpoint,
        current.token_endpoint,
        current.registration_endpoint,
        current.revocation_endpoint,
    )


def connected(
    i: Intake,
    ctx: WorkspaceContext,
    server: FakeAuthorizationServer,
    pipeline: UUID,
    **registration: Any,
) -> JobfeedConnection:
    """A first connect completed through the dispatcher, its registration issuing
    both secrets so the stored client owns two files."""
    server.registration = with_secrets(**registration)
    assert result(complete(i, ctx, **start(i, ctx, server, pipeline))) == (
        "connected",
        None,
    )
    return stored(ctx)


def jobsearch_off(i: Intake, ctx: WorkspaceContext) -> OperationOutcome:
    return run(i, ctx, "core.settings.set", key=JOBSEARCH, value=False)


def other_owner(i: Intake) -> WorkspaceContext:
    """Another account, owner role, on the web entry."""
    account = add_member(
        i.cluster.backend, i.workspace, Role.OWNER, display_name="owner-two"
    )
    ctx = context_for_harness(i.workspace, account, Role.OWNER, Entry.WEB)
    assert isinstance(ctx, WorkspaceContext)
    return ctx


# --- the route


@pytest.fixture
def routed(intake: Intake, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Point the route's dispatch at the registry the loader filled for Leads, and
    record every payload the route handed it."""
    seen: list[dict[str, Any]] = []
    real = connector_oauth_routes.dispatch

    def to_loaded_module(ctx: Any, name: str, payload: Any, **kwargs: Any) -> Any:
        seen.append({"name": name, "payload": dict(payload)})
        kwargs["registry"] = intake.surfaces.operations
        kwargs["consumers"] = intake.consumers
        return real(ctx, name, payload, **kwargs)

    monkeypatch.setattr(connector_oauth_routes, "dispatch", to_loaded_module)
    return seen


def browser_session(account_id: UUID, workspace_id: UUID) -> str:
    host = urlsplit(oauth.redirect_uri("leads")).netloc
    session = create_session(account_id)
    secret = mint_host_secret(session.id, host)
    assert switch_workspace(session.id, workspace_id) is None
    return secret.hex()


def callback(session: str | None, params: dict[str, str]) -> httpx.Response:
    redirect = urlsplit(oauth.redirect_uri("leads"))
    headers = {"Host": redirect.netloc}
    if session is not None:
        headers["Cookie"] = f"{SESSION_COOKIE}={session}"

    async def get() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public_app),
            base_url=f"https://{redirect.netloc}",
        ) as client:
            return await client.get(redirect.path, params=params, headers=headers)

    return asyncio.run(get())


def test_the_route_completes_through_the_really_loaded_leads_manifest(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    routed: list[dict[str, Any]],
) -> None:
    """AC 64: only state, code, error and iss reach the operation."""
    # The route resolves the completion from the loader's own table, unpatched.
    assert connector_oauth_routes.loaded_manifests is loaded_manifests
    assert loaded_manifests()["leads"].authorization_completion == COMPLETE
    params = start(intake, owner, served, pipeline_id)
    assert owner.actor.id is not None
    session = browser_session(owner.actor.id, intake.workspace)
    response = callback(
        session,
        {
            **params,
            "error_description": "description-marker-5b1d",
            "unrelated": "unrelated-marker-0a9c",
        },
    )
    assert response.status_code == 200, response.text
    assert response.text == CONNECTED_PAGE
    assert routed == [{"name": COMPLETE, "payload": params}]
    assert stored(owner).authorization_state == "connected"
    assert served.registrations and len(served.registrations) == 1
    (exchange,) = served.token_requests
    assert served.verifier_matches(exchange)


def test_no_valid_session_consumes_nothing_and_another_session_completes(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    routed: list[dict[str, Any]],
) -> None:
    """AC 3: no session, or a session on another workspace, consumes nothing; the
    initiating owner then completes from a browser session of their own."""
    params = start(intake, owner, served, pipeline_id)
    (row,) = pending(owner)
    assert owner.actor.id is not None
    for session in (None, "00" * 32, "not-hex"):
        response = callback(session, params)
        assert response.text == REFUSED_PAGE
    assert routed == []
    assert pending(owner) == [row]
    assert served.token_requests == []

    first = browser_session(owner.actor.id, intake.workspace)
    second = browser_session(owner.actor.id, intake.workspace)
    assert first != second
    assert callback(second, params).text == CONNECTED_PAGE
    assert pending(owner) == []
    # The same URL again, from the first session: the state is spent.
    assert callback(first, params).text == REFUSED_PAGE
    assert len(served.token_requests) == 1


def test_a_session_on_another_workspace_finds_no_row_and_stores_nothing(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    routed: list[dict[str, Any]],
    make_workspace: MakeWorkspace,
) -> None:
    assert owner.actor.id is not None
    elsewhere = make_workspace(owner=owner.actor.id)
    params = start(intake, owner, served, pipeline_id)
    (row,) = pending(owner)
    response = callback(browser_session(owner.actor.id, elsewhere), params)
    assert response.text == REFUSED_PAGE
    assert pending(owner) == [row]
    assert connection(owner) is None
    assert served.token_requests == []
    # The initiating owner can still complete it.
    session = browser_session(owner.actor.id, intake.workspace)
    assert callback(session, params).text == CONNECTED_PAGE


# --- refusals before the exchange


def test_an_unknown_state_refuses_and_touches_nothing(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    params = start(intake, owner, served, pipeline_id)
    (row,) = pending(owner)
    outcome = complete(intake, owner, **{**params, "state": "unknown-state-marker"})
    assert result(outcome) == ("refused", "state_unknown")
    assert pending(owner) == [row]
    assert served.token_requests == []
    assert counts(owner)["jobfeed_connection"] == 0


def test_a_replayed_state_is_unknown(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    params = start(intake, owner, served, pipeline_id)
    assert result(complete(intake, owner, **params)) == ("connected", None)
    before = stored(owner)
    assert result(complete(intake, owner, **params)) == ("refused", "state_unknown")
    assert len(served.token_requests) == 1
    assert stored(owner) == before


def _refused_and_consumed(
    intake: Intake,
    owner: WorkspaceContext,
    served: FakeAuthorizationServer,
    root: Path,
    ctx: WorkspaceContext,
    params: dict[str, str],
    reason: str,
) -> None:
    (row,) = pending(owner)
    files = row_files(root, row)
    assert len(files) == 2 and all(f.exists() for f in files)
    registrations = len(served.registrations)
    assert result(complete(intake, ctx, **params)) == ("refused", reason)
    assert pending(owner) == []
    assert served.token_requests == []
    assert len(served.registrations) == registrations
    c = counts(owner)
    assert c["jobfeed_connection"] == 0 and c["jobfeed_authorization"] == 0
    # The freshly registered client's files are discarded with the refusal.
    assert secret_files(root) == set()


def test_an_expired_state_is_refused_consumed_and_its_fresh_files_discarded(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    root: Path,
) -> None:
    served.registration = with_secrets()
    params = start(intake, owner, served, pipeline_id)
    clock.advance(10 * 60)
    _refused_and_consumed(intake, owner, served, root, owner, params, "state_expired")


def test_another_owner_account_is_refused_and_the_fresh_files_discarded(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    served.registration = with_secrets()
    params = start(intake, owner, served, pipeline_id)
    _refused_and_consumed(
        intake, owner, served, root, other_owner(intake), params, "account_mismatch"
    )


def test_a_capability_turned_off_before_the_callback_refuses_without_mapping_rows(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    """AC 3 and AC 21 point (b)."""
    served.registration = with_secrets()
    params = start(intake, owner, served, pipeline_id)
    mappings = intake.count(t.field_mapping)
    rules = intake.count(p.routing_rule)
    assert run(
        intake,
        owner,
        "core.settings.set",
        key=JOBSEARCH,
        value=False,
    ).ok
    _refused_and_consumed(
        intake, owner, served, root, owner, params, "capability_disabled"
    )
    assert intake.count(t.field_mapping) == mappings
    assert intake.count(p.routing_rule) == rules
    assert intake.count(t.intake_connection) == 1


def test_a_capability_turned_off_during_the_exchange_installs_nothing(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    """The install's own capability check refuses after a successful exchange; the
    new token file and the fresh client's files are discarded."""
    served.registration = with_secrets()
    params = start(intake, owner, served, pipeline_id)
    answer = served.token

    def turning_off(request: httpx.Request) -> httpx.Response:
        assert jobsearch_off(intake, owner).ok
        return answer(request)

    served.token = turning_off
    mappings = intake.count(t.field_mapping)
    assert result(complete(intake, owner, **params)) == (
        "refused",
        "capability_disabled",
    )
    assert len(served.token_requests) == 1
    assert intake.count(t.field_mapping) == mappings
    assert counts(owner)["jobfeed_connection"] == 0
    assert secret_files(root) == set()


def test_a_provider_error_on_a_first_connect_stores_nothing_and_no_text(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    routed: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 5: only the completion's audit row records it."""
    marker = "access_denied-provider-marker-41c7"
    params = start(intake, owner, served, pipeline_id)
    assert owner.actor.id is not None
    session = browser_session(owner.actor.id, intake.workspace)
    with caplog.at_level(logging.DEBUG):
        response = callback(
            session,
            {
                "state": params["state"],
                "error": marker,
                "error_description": f"{marker}-description",
            },
        )
    assert response.text == REFUSED_PAGE
    assert routed[0]["payload"] == {"state": params["state"], "error": marker}
    c = counts(owner)
    assert c["jobfeed_connection"] == 0 and c["jobfeed_authorization"] == 0
    assert c["intake_connection"] == 1
    assert health_rows_for_jobfeed(owner) == []
    (audit,) = audits(owner, COMPLETE)
    assert audit["outcome"] == "succeeded"
    assert served.token_requests == []
    assert marker not in dump(owner)
    assert marker not in application_log(caplog)
    assert marker not in response.text


def application_log(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured line except the test's own HTTP client's request line, which
    carries the callback URL the test itself built."""
    return "\n".join(
        record.getMessage() for record in caplog.records if record.name != "httpx"
    )


def health_rows_for_jobfeed(ctx: WorkspaceContext) -> list[dict[str, Any]]:
    """Health rows of any connection other than the seeded manual one."""
    with open_unit_of_work(ctx) as uow:
        manual = uow.connection.execute(
            select(t.intake_connection.c.id).where(
                t.intake_connection.c.transport == "manual"
            )
        ).scalar_one()
        return [
            dict(row)
            for row in uow.connection.execute(
                select(t.connection_health).where(
                    t.connection_health.c.connection_id != manual
                )
            ).mappings()
        ]


def test_a_mismatched_issuer_is_refused_with_nothing_stored(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    params = start(intake, owner, served, pipeline_id)
    outcome = complete(intake, owner, **{**params, "iss": "https://www.example.com"})
    assert result(outcome) == ("refused", "issuer_mismatch")
    assert served.token_requests == []
    assert counts(owner)["jobfeed_connection"] == 0
    assert pending(owner) == []


def test_a_provider_error_on_a_reconnect_writes_only_the_health_code(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 5 (reconnect half), AC 75: a connected connection keeps collecting."""
    before = connected(intake, owner, served, pipeline_id)
    schedule_before = collect_schedule(owner)
    assert schedule_before is not None and schedule_before["enabled"] is True
    files = secret_files(root)
    marker = "access_denied-reconnect-marker-9e02"
    params = restart(intake, owner, served)
    with caplog.at_level(logging.DEBUG):
        outcome = complete(intake, owner, state=params["state"], error=marker)
    assert result(outcome) == ("refused", "provider_error")
    assert stored(owner) == before
    assert collect_schedule(owner) == schedule_before
    assert secret_files(root) == files
    recorded = health(owner, before.connection_id)
    assert recorded is not None and recorded["last_error"] == "authorization_required"
    # One completion audit row each, and no other row (no terminal transition).
    assert [a["outcome"] for a in audits(owner, COMPLETE)] == ["succeeded"] * 2
    assert marker not in dump(owner) and marker not in caplog.text


# --- the exchange and the writes


def test_no_refresh_token_stores_nothing(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    """AC 6, first half."""
    served.registration = with_secrets()
    served.token = Tokens(refresh=False)
    params = start(intake, owner, served, pipeline_id)
    assert result(complete(intake, owner, **params)) == ("refused", "no_refresh_token")
    assert len(served.token_requests) == 1
    assert counts(owner)["jobfeed_connection"] == 0
    assert secret_files(root) == set()


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (token_error("invalid_grant"), "exchange_failed"),
        (token_error("unauthorized_client"), "exchange_failed"),
        (token_error("server_error", status=500), "exchange_failed"),
        (token_error("slow_down", status=429), "exchange_failed"),
    ],
    ids=["invalid_grant", "unauthorized_client", "server_error", "rate_limited"],
)
def test_other_exchange_failures_are_exchange_failed_with_nothing_stored(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
    answer: Callable[[httpx.Request], httpx.Response],
    reason: str,
) -> None:
    served.registration = with_secrets()
    served.token = answer
    params = start(intake, owner, served, pipeline_id)
    assert result(complete(intake, owner, **params)) == ("refused", reason)
    assert counts(owner)["jobfeed_connection"] == 0
    assert secret_files(root) == set()


def test_a_first_connect_installs_with_one_reference_and_the_registered_client(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    """AC 6 second half, AC 7 (b) on a first connect, AC 63 and AC 73."""
    # Module enable (the fixture's real ``core.module.enable``) wrote no row.
    assert collect_schedule(owner) is None
    served.registration = with_secrets()
    params = start(intake, owner, served, pipeline_id)
    (row,) = pending(owner)
    assert result(complete(intake, owner, **params)) == ("connected", None)
    current = stored(owner)
    assert current.connection_id == row["connection_id"]
    assert current.authorization_state == "connected"
    assert current.credential_generation == 1
    assert current.token_ref is not None and file_of(root, current.token_ref).exists()
    assert client_of(current) == (
        row["client_id"],
        row["client_auth_method"],
        row["redirect_uri"],
        row["client_secret_expires_at"],
        row["client_secret_ref"],
        row["registration_token_ref"],
    )
    assert endpoints_of(current) == tuple(
        row[name]
        for name in (
            "resource_url",
            "issuer",
            "authorization_endpoint",
            "token_endpoint",
            "registration_endpoint",
            "revocation_endpoint",
        )
    )
    # Exactly the token file and the two client files exist.
    assert secret_files(root) == {
        file_of(root, current.token_ref),
        *client_files(root, current),
    }
    assert intake.count(p.routing_rule) >= 2
    due = collect_schedule(owner)
    assert due is not None
    assert due["enabled"] is True and due["next_run_at"] == NOW
    assert due["job_kind"] == COLLECT_JOB_KIND


def test_a_reconnect_inside_a_claim_is_due_an_hour_after_the_claim(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    """AC 73: ``max(now, run_claimed_at + 60 minutes)``."""
    connected(intake, owner, served, pipeline_id)
    claimed = NOW - timedelta(minutes=5)
    set_connection(owner, run_claimed_at=claimed)
    assert result(complete(intake, owner, **restart(intake, owner, served))) == (
        "connected",
        None,
    )
    due = collect_schedule(owner)
    assert due is not None and due["enabled"] is True
    assert due["next_run_at"] == claimed + timedelta(minutes=60)


def test_a_workspace_without_a_schedule_row_gets_one_on_completion(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    """AC 26: Leads installed before the schedule existed has no row; a completed
    reconnect writes it, enabled and due."""
    connected(intake, owner, served, pipeline_id)
    with open_unit_of_work(owner) as uow:
        uow.connection.execute(
            schedule.delete().where(
                schedule.c.module_id == "leads", schedule.c.name == SCHEDULE_NAME
            )
        )
        uow.commit()
    assert collect_schedule(owner) is None
    assert result(complete(intake, owner, **restart(intake, owner, served))) == (
        "connected",
        None,
    )
    due = collect_schedule(owner)
    assert due is not None and due["enabled"] is True and due["next_run_at"] == NOW


def test_a_reconnect_that_reuses_the_client_leaves_its_columns_and_files(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    """AC 7 (b), reuse half; the old token file goes after the write."""
    before = connected(intake, owner, served, pipeline_id)
    registrations = len(served.registrations)
    assert before.token_ref is not None
    old_token = file_of(root, before.token_ref)
    params = restart(intake, owner, served)
    assert len(served.registrations) == registrations
    assert result(complete(intake, owner, **params)) == ("connected", None)
    after = stored(owner)
    assert client_of(after) == client_of(before)
    assert all(f.exists() for f in client_files(root, after))
    assert after.token_ref not in (None, before.token_ref)
    assert after.credential_generation == before.credential_generation + 1
    assert not old_token.exists()
    assert secret_files(root) == {
        file_of(root, after.token_ref),  # type: ignore[arg-type]
        *client_files(root, after),
    }


def test_a_reconnect_that_registered_stores_the_new_client_and_retires_the_old(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    """AC 7 (b), re-registration half, and the re-pin of every endpoint."""
    before = connected(intake, owner, served, pipeline_id)
    old_files = client_files(root, before) | {file_of(root, before.token_ref)}  # type: ignore[arg-type]
    served.registration_path = "/register-v2"
    served.registration = with_secrets(client_id="second-client")
    params = restart(intake, owner, served)
    (row,) = pending(owner)
    assert row["client_registered"] is True
    assert result(complete(intake, owner, **params)) == ("connected", None)
    after = stored(owner)
    assert after.client_id == "second-client"
    assert client_of(after) == (
        row["client_id"],
        row["client_auth_method"],
        row["redirect_uri"],
        row["client_secret_expires_at"],
        row["client_secret_ref"],
        row["registration_token_ref"],
    )
    assert after.registration_endpoint == served.registration_endpoint
    assert not any(f.exists() for f in old_files)
    assert secret_files(root) == {
        file_of(root, after.token_ref),  # type: ignore[arg-type]
        *client_files(root, after),
    }


def test_a_reconnect_re_pins_the_changed_resource_and_its_endpoints(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = connected(intake, owner, served, pipeline_id)
    moved = "https://mcp.example.com/v2/mcp"
    monkeypatch.setenv("RHEO__leads__jobfeed__resource_url", moved)
    served.resource_url = moved
    served.issuer = "https://auth.example.com/v2"
    served.registration_path = "/v2/register"
    served.revocation = False
    params = restart(intake, owner, served)
    assert result(complete(intake, owner, **params)) == ("connected", None)
    after = stored(owner)
    discovered = served.discovered()
    assert endpoints_of(after) == (
        discovered.resource_url,
        discovered.issuer,
        discovered.authorization_endpoint,
        discovered.token_endpoint,
        discovered.registration_endpoint,
        discovered.revocation_endpoint,
    )
    assert endpoints_of(after) != endpoints_of(before)
    assert after.revocation_endpoint is None
    assert after.connection_id == before.connection_id


@pytest.mark.parametrize("state", ["revoked", "authorization_required"])
def test_a_reconnect_from_a_lost_state_keeps_rules_pipeline_and_records(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    state: str,
) -> None:
    """AC 16 and AC 20: no rule added, an owner-edited rule kept, the pipeline,
    opportunities and observation links unchanged, collector codes cleared."""
    before = connected(intake, owner, served, pipeline_id)
    poll = poll_context(intake, before.connection_id)
    accepted = deliver(
        poll,
        intake,
        {
            "title": "Example posting",
            "posting_id": "posting-0001",
            "observed_at": "2026-01-05T09:00:00+00:00",
        },
        "posting-0001:1",
    )
    assert accepted.ok, accepted
    drain(intake)
    with open_unit_of_work(owner) as uow:
        uow.connection.execute(
            update(p.routing_rule)
            .where(
                p.routing_rule.c.connection_id == before.connection_id,
                p.routing_rule.c.ordinal == 1,
            )
            .values(enabled=False)
        )
        uow.commit()
    set_connection(
        owner,
        authorization_state=state,
        consecutive_failures=3,
        not_before=NOW + timedelta(hours=1),
        organization_id="org-fixture-1",
    )
    with open_unit_of_work(owner) as uow:
        uow.connection.execute(
            update(t.connection_health)
            .where(t.connection_health.c.connection_id == before.connection_id)
            .values(last_error="upstream_unavailable", last_error_at=NOW)
        )
        uow.commit()

    def snapshot() -> dict[str, list[dict[str, Any]]]:
        with open_unit_of_work(owner) as uow:
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

    kept = snapshot()
    assert kept["opportunity_observation"], kept
    params = restart(intake, owner, served)
    assert result(complete(intake, owner, **params)) == ("connected", None)
    after = stored(owner)
    assert snapshot() == kept
    assert after.connection_id == before.connection_id
    assert after.pipeline_id == before.pipeline_id
    assert after.authorization_state == "connected"
    assert after.token_ref != before.token_ref
    assert after.credential_generation == before.credential_generation + 1
    assert after.consecutive_failures == 0 and after.not_before is None
    assert after.organization_id == "org-fixture-1"
    recorded = health(owner, before.connection_id)
    assert recorded is not None and recorded["last_error"] is None


def test_an_intake_health_code_survives_a_reconnect(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    before = connected(intake, owner, served, pipeline_id)
    with open_unit_of_work(owner) as uow:
        uow.connection.execute(
            update(t.connection_health)
            .where(t.connection_health.c.connection_id == before.connection_id)
            .values(last_error="delivery_conflict", last_error_at=NOW)
        )
        uow.commit()
    assert result(complete(intake, owner, **restart(intake, owner, served)))[0] == (
        "connected"
    )
    recorded = health(owner, before.connection_id)
    assert recorded is not None and recorded["last_error"] == "delivery_conflict"


def test_one_audit_row_per_completion_and_no_secret_anywhere(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 17 for completion: token, code, verifier and client secret values appear
    in no audit row, Leads row, operation record or log line."""
    served.registration = with_secrets()
    with caplog.at_level(logging.DEBUG):
        params = start(intake, owner, served, pipeline_id)
        (row,) = pending(owner)
        assert result(complete(intake, owner, **params)) == ("connected", None)
        second = restart(intake, owner, served)
        (row_two,) = pending(owner)
        assert result(complete(intake, owner, **second)) == ("connected", None)
    completions = audits(owner, COMPLETE)
    assert [a["outcome"] for a in completions] == ["succeeded", "succeeded"]
    assert all(a["subject_ref"] is None for a in completions)
    assert isinstance(served.token, Tokens)
    markers = [
        *served.token.issued,
        params["state"],
        params["code"],
        second["state"],
        second["code"],
        row["code_verifier"],
        row_two["code_verifier"],
        SECRET,
        TOKEN,
    ]
    text = dump(owner)
    for marker in markers:
        assert marker not in text, marker
        assert marker not in caplog.text, marker


# --- forced failures: nothing left behind, nothing referenced removed


def _is_connection_insert(statement: str) -> bool:
    return statement.lstrip().upper().startswith("INSERT INTO LEADS.JOBFEED_CONNECTION")


def test_a_failed_install_after_the_exchange_leaves_no_file_and_a_spent_state(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    """AC 74 and I29."""
    served.registration = with_secrets()
    params = start(intake, owner, served, pipeline_id)

    def explode(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if _is_connection_insert(statement):
            raise InjectedFailure

    event.listen(Engine, "before_cursor_execute", explode)
    try:
        outcome = complete(intake, owner, **params)
    finally:
        event.remove(Engine, "before_cursor_execute", explode)
    assert not outcome.ok and outcome.error is not None
    assert outcome.error.error_code == "handler_failed"
    assert len(served.token_requests) == 1
    assert pending(owner) == []
    assert counts(owner)["jobfeed_connection"] == 0
    assert secret_files(root) == set()
    assert result(complete(intake, owner, **params)) == ("refused", "state_unknown")
    assert len(served.token_requests) == 1


def test_a_failed_discard_does_not_change_the_outcome(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    served.registration = with_secrets()
    params = start(intake, owner, served, pipeline_id)

    def explode(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if _is_connection_insert(statement):
            raise InjectedFailure

    def failing_discard(self: Any, ref: Any, scope: Any) -> None:
        raise OSError("discard-failure-marker")

    monkeypatch.setattr(SecretStore, "discard", failing_discard)
    event.listen(Engine, "before_cursor_execute", explode)
    try:
        with caplog.at_level(logging.WARNING):
            outcome = complete(intake, owner, **params)
    finally:
        event.remove(Engine, "before_cursor_execute", explode)
    assert not outcome.ok and outcome.error is not None
    assert outcome.error.error_code == "handler_failed"
    assert outcome.error.error_text == "InjectedFailure"
    assert oauth.SECRET_DISCARD_FAILED in caplog.text
    assert "discard-failure-marker" not in caplog.text


def _reregistering_reconnect(
    intake: Intake,
    owner: WorkspaceContext,
    served: FakeAuthorizationServer,
    root: Path,
) -> tuple[JobfeedConnection, set[Path], dict[str, str], set[Path]]:
    """A connected connection with a token and client files, and a pending reconnect
    whose start registered a fresh client: the connection, its files, the callback
    parameters and the fresh client's files."""
    before = stored(owner)
    assert before.token_ref is not None
    referenced = client_files(root, before) | {file_of(root, before.token_ref)}
    served.registration_path = "/register-v2"
    served.registration = with_secrets(client_id="second-client")
    params = restart(intake, owner, served)
    (row,) = pending(owner)
    assert row["client_registered"] is True
    fresh = row_files(root, row)
    assert len(fresh) == 2 and all(f.exists() for f in fresh)
    return before, referenced, params, fresh


def test_a_reconnect_unit_failing_after_the_token_switch_retires_nothing(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    """AC 62: the unit rolls back, so the old token and the previous client stay
    referenced and on disk; the new token file and the fresh client's files go."""
    connected(intake, owner, served, pipeline_id)
    before, referenced, params, fresh = _reregistering_reconnect(
        intake, owner, served, root
    )
    switched: set[int] = set()

    def explode(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        text = statement.lstrip()
        if text.upper().startswith("UPDATE LEADS.JOBFEED_CONNECTION") and (
            "token_ref" in text
        ):
            switched.add(id(conn))
        elif id(conn) in switched and "core.schedule" in text:
            raise InjectedFailure

    event.listen(Engine, "before_cursor_execute", explode)
    try:
        outcome = complete(intake, owner, **params)
    finally:
        event.remove(Engine, "before_cursor_execute", explode)
    assert switched
    assert not outcome.ok and outcome.error is not None
    assert outcome.error.error_text == "InjectedFailure"
    assert stored(owner) == before
    assert all(f.exists() for f in referenced)
    assert not any(f.exists() for f in fresh)
    assert secret_files(root) == referenced


def test_a_reconnect_retires_the_old_files_only_after_its_unit_commits(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    connected(intake, owner, served, pipeline_id)
    before, referenced, params, fresh = _reregistering_reconnect(
        intake, owner, served, root
    )
    switching: set[int] = set()
    at_commit: list[bool] = []

    def watch(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        text = statement.lstrip()
        if text.upper().startswith("UPDATE LEADS.JOBFEED_CONNECTION") and (
            "token_ref" in text
        ):
            switching.add(id(conn))

    def on_commit(conn: Any) -> None:
        if id(conn) in switching:
            switching.discard(id(conn))
            at_commit.append(all(f.exists() for f in referenced))

    event.listen(Engine, "before_cursor_execute", watch)
    event.listen(Engine, "commit", on_commit)
    try:
        outcome = complete(intake, owner, **params)
    finally:
        event.remove(Engine, "before_cursor_execute", watch)
        event.remove(Engine, "commit", on_commit)
    assert result(outcome) == ("connected", None)
    assert at_commit == [True]
    assert not any(f.exists() for f in referenced)
    after = stored(owner)
    assert secret_files(root) == fresh | {file_of(root, after.token_ref)}  # type: ignore[arg-type]


def test_a_failed_dispatcher_commit_leaves_the_connection_consistent(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
    cluster: ClusterSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler's unit committed; the dispatcher's own commit then fails. The
    connection references the new token, whose file exists."""
    connected(intake, owner, served, pipeline_id)
    before = stored(owner)
    params = restart(intake, owner, served)
    database = cluster.registry_row(intake.workspace).database_name
    engine = cluster.backend.pools.engine_for(database)

    class _CommitRefusingUnitOfWork(UnitOfWork):
        __slots__ = ()

        def commit(self) -> None:
            raise RuntimeError("the operation's transaction could not commit")

    dispatch_module = import_module("rheo_core.operations.dispatch")
    real = dispatch_module.open_unit_of_work
    calls = {"n": 0}

    def refuse_the_first_commit(ctx: WorkspaceContext) -> UnitOfWork:
        calls["n"] += 1
        if calls["n"] == 1:
            return _CommitRefusingUnitOfWork(engine, database)
        return real(ctx)

    monkeypatch.setattr(dispatch_module, "open_unit_of_work", refuse_the_first_commit)
    outcome = complete(intake, owner, **params)
    monkeypatch.undo()
    assert not outcome.ok
    after = stored(owner)
    assert after.authorization_state == "connected"
    assert after.token_ref is not None and after.token_ref != before.token_ref
    assert after.credential_generation == before.credential_generation + 1
    assert file_of(root, after.token_ref).exists()
    assert all(f.exists() for f in client_files(root, after))


# --- client rejection at the exchange


def test_a_reused_client_rejected_is_cleared_and_the_connection_goes_terminal(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    """I31, AC 79 exchange half: no registration until the owner's next start,
    which registers exactly once."""
    before = connected(intake, owner, served, pipeline_id)
    files = client_files(root, before)
    assert len(files) == 2
    registrations = len(served.registrations)
    served.token = token_error("invalid_client")
    params = restart(intake, owner, served)
    (row,) = pending(owner)
    assert row["client_registered"] is False
    assert result(complete(intake, owner, **params)) == ("refused", "client_rejected")
    assert len(served.registrations) == registrations
    after = stored(owner)
    assert client_of(after) == (None,) * 6
    assert after.authorization_state == "authorization_required"
    assert not any(f.exists() for f in files)
    assert after.token_ref == before.token_ref
    due = collect_schedule(owner)
    assert due is not None and due["enabled"] is False
    recorded = health(owner, before.connection_id)
    assert recorded is not None and recorded["last_error"] == "authorization_required"
    assert len(audits(owner, COMPLETE)) == 2

    served.token = Tokens()
    params = restart(intake, owner, served)
    assert len(served.registrations) == registrations + 1
    assert result(complete(intake, owner, **params)) == ("connected", None)
    assert len(served.registrations) == registrations + 1
    assert stored(owner).client_id is not None


def test_a_rejected_client_no_longer_stored_clears_nothing(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    """The clearing is a compare-and-set on ``client_id``: a connection that stored
    another client since the start keeps it, and nothing is discarded."""
    before = connected(intake, owner, served, pipeline_id)
    files = client_files(root, before)
    served.token = token_error("invalid_client")
    params = restart(intake, owner, served)
    set_connection(owner, client_id="replaced-client")
    changed = stored(owner)
    assert result(complete(intake, owner, **params)) == ("refused", "client_rejected")
    assert stored(owner) == changed
    assert changed.authorization_state == "connected"
    assert all(f.exists() for f in files)
    due = collect_schedule(owner)
    assert due is not None and due["enabled"] is True


def test_a_fresh_client_rejected_is_discarded_and_the_connection_kept(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    connected(intake, owner, served, pipeline_id)
    before, referenced, params, fresh = _reregistering_reconnect(
        intake, owner, served, root
    )
    schedule_before = collect_schedule(owner)
    registrations = len(served.registrations)
    served.token = token_error("invalid_client", status=401)
    assert result(complete(intake, owner, **params)) == ("refused", "client_rejected")
    assert len(served.registrations) == registrations
    assert stored(owner) == before
    assert collect_schedule(owner) == schedule_before
    assert not any(f.exists() for f in fresh)
    assert secret_files(root) == referenced


def test_an_expired_reused_client_secret_is_rejected_with_nothing_sent(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    before = connected(
        intake,
        owner,
        served,
        pipeline_id,
        token_endpoint_auth_method="client_secret_basic",
    )
    assert before.client_auth_method == "client_secret_basic"
    set_connection(owner, client_secret_expires_at=NOW - timedelta(minutes=1))
    params = restart(intake, owner, served)
    requests = len(served.token_requests)
    registrations = len(served.registrations)
    assert result(complete(intake, owner, **params)) == ("refused", "client_rejected")
    assert len(served.token_requests) == requests
    assert len(served.registrations) == registrations
    after = stored(owner)
    assert after.client_id is None and after.authorization_state == (
        "authorization_required"
    )
    assert not any(f.exists() for f in client_files(root, before))


def test_a_failing_client_clearing_unit_keeps_the_client_and_its_files(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    root: Path,
) -> None:
    before = connected(intake, owner, served, pipeline_id)
    files = client_files(root, before)
    served.token = token_error("invalid_client")
    params = restart(intake, owner, served)

    def explode(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "core.schedule" in statement:
            raise InjectedFailure

    event.listen(Engine, "before_cursor_execute", explode)
    try:
        outcome = complete(intake, owner, **params)
    finally:
        event.remove(Engine, "before_cursor_execute", explode)
    assert not outcome.ok and outcome.error is not None
    assert outcome.error.error_text == "InjectedFailure"
    assert stored(owner) == before
    assert all(f.exists() for f in files)


# --- the schedule and the skeleton job


def test_the_ticker_runs_the_collect_job_which_calls_nothing(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    server: FakeAuthorizationServer,
    clock: FakeClock,
    root: Path,
) -> None:
    mcp = FakeMcpServer()
    with serve(server, clock=clock, mcp=mcp):
        connected(intake, owner, server, pipeline_id)
        requests = len(server.requests)
        kinds = JobKindRegistry()
        for kind in MANIFEST.jobs:
            kinds.register(kind.name, kind.input_model, kind.handler)
        at = datetime.now(UTC) + timedelta(seconds=2)
        visit_workspace(
            DueWorkspace(workspace_id=intake.workspace, observed_due_at=None),
            kinds=kinds,
            consumers=intake.consumers,
            backend=intake.cluster.backend,
            owner="jobfeed-authorization-test",
            clock=lambda: at,
            jitter=None,
        )
    with open_unit_of_work(owner) as uow:
        jobs = [
            dict(row)
            for row in uow.connection.execute(
                select(job.c.kind, job.c.state, job.c.attempts).where(
                    job.c.kind == COLLECT_JOB_KIND
                )
            ).mappings()
        ]
    assert jobs == [{"kind": COLLECT_JOB_KIND, "state": "succeeded", "attempts": 1}]
    assert mcp.requests == [] and sum(mcp.counts.values()) == 0
    assert len(server.requests) == requests


# --- transaction shape


def test_no_leads_lock_or_write_transaction_is_held_during_the_exchange(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    params = start(intake, owner, served, pipeline_id)
    busy, locks, lifecycle_free, outcome = _shape_during(
        intake, owner, served, "token", lambda: complete(intake, owner, **params)
    )
    assert result(outcome) == ("connected", None)
    assert len(busy) == 1, busy
    assert busy[0]["state"] == "idle in transaction"
    assert busy[0]["xid"] is None
    leads_locks = {
        (relation, mode) for schema, relation, mode in locks if schema == "leads"
    }
    assert leads_locks <= {
        ("alembic_version_leads", "AccessShareLock"),
        ("alembic_version_leads_pkc", "AccessShareLock"),
    }, leads_locks
    assert lifecycle_free is True

"""The job feed's loopback redirect mode and ``leads.jobfeed.complete_pasted``.

Seams under test: connect, reconnect and the pasted-address completion through the
real dispatcher, against a fake authorization server whose registration endpoint
accepts only loopback redirect URIs (the provider behaviour this mode exists for);
the collector job through a real scheduler tick and worker visit; and the catalog
marker the Job feed card reads. Secret files land in a real file store under a
temporary data root.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import httpx
import pytest
from harness.jobfeed import (
    FakeAuthorizationServer,
    FakeClock,
    FakeMcpServer,
    FakeResolver,
    Tokens,
    account,
    collect_jobs,
    configure_connector,
    feed_row,
    issued,
    jobsearch_pipeline,
    owner_web,
    serve,
    visit,
)
from rheo_contracts import Entry, Idempotency, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.connectors import oauth
from rheo_core.operations.dispatch import OperationOutcome
from rheo_leads.jobfeed import authorization
from rheo_leads.manifest import MANIFEST
from rheo_leads.references import ref

from postgres.test_jobfeed_authorization import (
    COMPLETE,
    connection,
    other_owner,
    result,
    stored,
)
from postgres.test_jobfeed_connect import (
    CONNECT,
    RECONNECT,
    audits,
    capability,
    counts,
    dump,
    member_context,
    pending,
    refused,
    run,
    secret_files,
)
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres

PASTED = "leads.jobfeed.complete_pasted"
LOOPBACK = "http://127.0.0.1:33419/callback"
JOB_HOST = "www.example.com"
ORG = "org-loopback-0001"


def loopback_only() -> Callable[[httpx.Request], httpx.Response]:
    """A registration endpoint that, like the provider, accepts only a loopback
    redirect URI and refuses anything else ``invalid_redirect_uri``."""
    accept = issued()

    def respond(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)["redirect_uris"]
        if not all(urlsplit(uri).hostname == "127.0.0.1" for uri in sent):
            return httpx.Response(400, json={"error": "invalid_redirect_uri"})
        return accept(request)

    return respond


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def server() -> FakeAuthorizationServer:
    return FakeAuthorizationServer(registration=loopback_only(), token=Tokens())


@pytest.fixture
def mcp() -> FakeMcpServer:
    return FakeMcpServer(
        accounts=[account(ORG)], feeds={"best_match": [[feed_row(140)]]}
    )


@pytest.fixture
def served(
    intake: Intake,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: FakeAuthorizationServer,
    mcp: FakeMcpServer,
    clock: FakeClock,
) -> Any:
    configure_connector(monkeypatch, tmp_path, job_hosts=JOB_HOST, redirect="loopback")
    with serve(server, clock=clock, resolver=FakeResolver(), mcp=mcp):
        yield server


@pytest.fixture
def owner(intake: Intake, served: FakeAuthorizationServer) -> WorkspaceContext:
    return owner_web(intake)


@pytest.fixture
def pipeline_id(intake: Intake, owner: WorkspaceContext) -> UUID:
    return jobsearch_pipeline(intake, owner)


def consent(server: FakeAuthorizationServer, outcome: OperationOutcome) -> str:
    """The owner consents; the address the browser then lands on (and cannot load)."""
    assert outcome.ok, outcome
    url = outcome.result.authorization_url  # type: ignore[union-attr]
    assert parse_qs(urlsplit(url).query)["redirect_uri"] == [LOOPBACK]
    state, code = server.authorize(url)
    query = urlencode({"code": code, "state": state, "iss": server.issuer})
    return f"{LOOPBACK}?{query}"


def connect(i: Intake, ctx: WorkspaceContext, pipeline: UUID) -> OperationOutcome:
    return run(i, ctx, CONNECT, pipeline_ref=ref("pipeline", pipeline))


def paste(i: Intake, ctx: WorkspaceContext, address: str) -> OperationOutcome:
    return run(i, ctx, PASTED, address=address)


def jobfeed_marker(i: Intake, ctx: WorkspaceContext) -> dict[str, Any]:
    outcome = run(i, ctx, "leads.ui.catalog")
    assert outcome.ok, outcome
    (row,) = [r for r in outcome.result.items if r["kind"] == "jobfeed"]  # type: ignore[union-attr]
    return dict(row)


def code_redirects(server: FakeAuthorizationServer) -> list[str]:
    """The ``redirect_uri`` of every code exchange the token endpoint received."""
    found = []
    for request in server.token_requests:
        form = parse_qs(request.content.decode("ascii"))
        if form["grant_type"] == ["authorization_code"]:
            found += form["redirect_uri"]
    return found


# --- connect, paste, collect ----------------------------------------------------------


def test_a_pasted_address_connects_and_the_collector_then_runs(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    mcp: FakeMcpServer,
    clock: FakeClock,
) -> None:
    address = consent(served, connect(intake, owner, pipeline_id))
    (registration,) = served.registrations
    assert json.loads(registration.content)["redirect_uris"] == [LOOPBACK]
    (row,) = pending(owner)
    assert row["redirect_uri"] == LOOPBACK
    assert jobfeed_marker(intake, owner) == {
        "kind": "jobfeed",
        "loopback": True,
        "pending": True,
    }

    assert result(paste(intake, owner, address)) == ("connected", None)
    assert code_redirects(served) == [LOOPBACK]
    current = stored(owner)
    assert current.client_redirect_uri == LOOPBACK
    assert current.authorization_state == "connected"
    assert pending(owner) == []
    assert jobfeed_marker(intake, owner)["pending"] is False
    (audit,) = audits(owner, PASTED)
    assert audit["outcome"] == "succeeded"

    visit(intake, clock)
    assert [job.state for job in collect_jobs(owner)] == ["succeeded"]
    assert stored(owner).organization_id == ORG
    assert mcp.requests


def test_a_replayed_address_is_state_unknown(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    address = consent(served, connect(intake, owner, pipeline_id))
    assert result(paste(intake, owner, address)) == ("connected", None)
    assert result(paste(intake, owner, address)) == ("refused", "state_unknown")
    assert len(served.token_requests) == 1


def test_an_expired_state_is_refused_and_consumed(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    tmp_path: Path,
) -> None:
    address = consent(served, connect(intake, owner, pipeline_id))
    clock.advance(10 * 60)
    assert jobfeed_marker(intake, owner)["pending"] is False
    assert result(paste(intake, owner, address)) == ("refused", "state_expired")
    assert pending(owner) == [] and connection(owner) is None
    assert served.token_requests == []
    assert secret_files(tmp_path) == set()


def test_a_provider_error_is_refused_with_nothing_stored(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    marker = "access-denied-description-marker-5e21"
    outcome = connect(intake, owner, pipeline_id)
    state, _code = served.authorize(outcome.result.authorization_url)  # type: ignore[union-attr]
    query = urlencode(
        {"error": "access_denied", "error_description": marker, "state": state}
    )
    assert result(paste(intake, owner, f"{LOOPBACK}?{query}")) == (
        "refused",
        "provider_error",
    )
    assert pending(owner) == [] and connection(owner) is None
    assert served.token_requests == []
    assert marker not in dump(owner)


def test_another_owner_is_refused_account_mismatch(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    address = consent(served, connect(intake, owner, pipeline_id))
    other = other_owner(intake)
    assert jobfeed_marker(intake, other)["pending"] is False
    assert result(paste(intake, other, address)) == ("refused", "account_mismatch")
    assert connection(owner) is None and served.token_requests == []


def test_a_capability_turned_off_is_refused(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    address = consent(served, connect(intake, owner, pipeline_id))
    capability(intake, owner, False)
    assert result(paste(intake, owner, address)) == ("refused", "capability_disabled")
    assert connection(owner) is None and served.token_requests == []


# --- the address itself ---------------------------------------------------------------


def _bad_addresses(address: str) -> dict[str, str]:
    parts = urlsplit(address)
    query = parts.query
    state = parse_qs(query)["state"][0]
    code = parse_qs(query)["code"][0]
    return {
        "https": address.replace("http:", "https:", 1),
        "localhost": address.replace("127.0.0.1", "localhost", 1),
        "127.0.0.2": address.replace("127.0.0.1", "127.0.0.2", 1),
        "port": address.replace(":33419", ":33420", 1),
        "no port": address.replace(":33419", "", 1),
        "path": address.replace("/callback", "/callback/x", 1),
        "userinfo": address.replace("//", "//owner@", 1),
        "fragment": f"{address}#done",
        "duplicate state": f"{address}&state={state}",
        "duplicate code": f"{address}&code={code}",
        "oversized code": address.replace(code, "c" * 2049, 1),
        "oversized address": f"{address}&pad={'p' * 4096}",
        "missing state": f"{LOOPBACK}?code={code}",
        "missing code": f"{LOOPBACK}?state={state}",
        "not an address": "it did not work",
    }


def test_every_wrong_address_is_address_invalid_and_consumes_nothing(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    address = consent(served, connect(intake, owner, pipeline_id))
    before = pending(owner)
    for name, wrong in _bad_addresses(address).items():
        outcome = paste(intake, owner, wrong)
        refused(outcome, "address_invalid")
        assert pending(owner) == before, name
        assert wrong not in repr(outcome), name
    assert served.token_requests == []
    assert result(paste(intake, owner, address)) == ("connected", None)


def test_identity_mode_refuses_the_paste_with_nothing_consumed(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = consent(served, connect(intake, owner, pipeline_id))
    before = pending(owner)
    monkeypatch.setenv("RHEO__connectors__oauth_client__redirect", "identity")
    assert jobfeed_marker(intake, owner)["loopback"] is False
    assert jobfeed_marker(intake, owner)["pending"] is False
    refused(paste(intake, owner, address), "endpoint_misconfigured")
    assert pending(owner) == before
    # The control: the same address, back in loopback mode.
    monkeypatch.setenv("RHEO__connectors__oauth_client__redirect", "loopback")
    assert result(paste(intake, owner, address)) == ("connected", None)


def test_only_the_owner_browser_session_can_paste(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
) -> None:
    address = consent(served, connect(intake, owner, pipeline_id))
    before = pending(owner)
    member = member_context(intake)
    assert jobfeed_marker(intake, member)["pending"] is False
    refused(paste(intake, member, address), "role_not_permitted")
    api = context_for_harness(owner.workspace_id, owner.actor.id, Role.OWNER, Entry.API)
    assert isinstance(api, WorkspaceContext)
    refused(paste(intake, api, address), "role_not_permitted")
    assert pending(owner) == before


def test_the_operation_stores_no_input_and_has_no_tool(intake: Intake) -> None:
    """The dispatcher stores a receipt only for keyed idempotency and an operation
    record only for a long-running call; the audit row keeps a digest. So the
    address is held in memory only as long as the call."""
    (declaration,) = [d for d, _ in authorization.OPERATIONS if d.name == PASTED]
    assert declaration.idempotency is Idempotency.NONE
    assert not declaration.long_running
    assert declaration.audit.subject_field is None
    assert declaration.roles == frozenset({Role.OWNER})
    assert PASTED not in {tool.operation for tool in MANIFEST.tools}


# --- reuse versus registering again ---------------------------------------------------


def test_reconnect_reuses_the_loopback_client_and_registers_when_the_mode_changes(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = consent(served, connect(intake, owner, pipeline_id))
    assert result(paste(intake, owner, address)) == ("connected", None)
    first = stored(owner).client_id
    assert len(served.registrations) == 1

    # Same redirect: the stored client is reused, no registration.
    again = consent(served, run(intake, owner, RECONNECT))
    assert len(served.registrations) == 1
    assert result(paste(intake, owner, again)) == ("connected", None)
    assert stored(owner).client_id == first

    # Identity mode: the stored loopback client does not match, so the reconnect
    # registers, and this provider refuses the identity-host URI.
    monkeypatch.setenv("RHEO__connectors__oauth_client__redirect", "identity")
    refused(run(intake, owner, RECONNECT), "registration_failed")
    assert len(served.registrations) == 2
    assert stored(owner).client_id == first

    # A moved loopback port is another redirect URI: a new client.
    monkeypatch.setenv("RHEO__connectors__oauth_client__redirect", "loopback")
    monkeypatch.setenv("RHEO__connectors__oauth_client__loopback_port", "40123")
    moved = run(intake, owner, RECONNECT)
    assert moved.ok, moved
    assert len(served.registrations) == 3
    state, code = served.authorize(moved.result.authorization_url)  # type: ignore[union-attr]
    new_uri = "http://127.0.0.1:40123/callback"
    assert parse_qs(urlsplit(moved.result.authorization_url).query)[  # type: ignore[union-attr]
        "redirect_uri"
    ] == [new_uri]
    stale = f"{LOOPBACK}?{urlencode({'state': state, 'code': code})}"
    refused(paste(intake, owner, stale), "address_invalid")
    fresh = f"{new_uri}?{urlencode({'state': state, 'code': code})}"
    assert result(paste(intake, owner, fresh)) == ("connected", None)
    current = stored(owner)
    assert current.client_id != first
    assert current.client_redirect_uri == new_uri
    assert code_redirects(served)[-1] == new_uri
    assert counts(owner)["jobfeed_authorization"] == 0


def test_identity_mode_still_registers_the_identity_callback(
    intake: Intake,
    owner: WorkspaceContext,
    pipeline_id: UUID,
    served: FakeAuthorizationServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RHEO__connectors__oauth_client__redirect", "identity")
    served.registration = issued()
    outcome = connect(intake, owner, pipeline_id)
    assert outcome.ok, outcome
    (registration,) = served.registrations
    (uri,) = json.loads(registration.content)["redirect_uris"]
    assert uri == oauth.redirect_uri("leads")
    assert urlsplit(uri).scheme == "https"
    assert urlsplit(uri).path == "/auth/connector/leads/callback"
    assert jobfeed_marker(intake, owner) == {
        "kind": "jobfeed",
        "loopback": False,
        "pending": False,
    }
    params = parse_qs(urlsplit(outcome.result.authorization_url).query)  # type: ignore[union-attr]
    assert params["redirect_uri"] == [uri]
    state, code = served.authorize(outcome.result.authorization_url)  # type: ignore[union-attr]
    assert result(run(intake, owner, COMPLETE, state=state, code=code)) == (
        "connected",
        None,
    )

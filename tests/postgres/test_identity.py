"""C7a: the identity-provider boundary, sessions core, and the cookie builder.

Seams: ``rheo_core.boundary.factories.context_from_session`` (the exact refusal order)
and ``rheo_core.sessions.cookies.build_cookie`` (the no-``Domain`` guarantee) — the
contract 08 builds ``/auth/*`` against (``00-index.md`` § Coupling seams, 07 -> 08).

B9's sign-in clause (rows 25-27 of the acceptance-criteria coverage matrix): the
GitHub provider's client secret is resolved only through its own scope, reaches the
token-exchange POST body and nowhere else — not another request, not a log line, not
a control-plane row other than ``identity_provider.client_secret_ref``.
"""

import inspect
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.identity import DEFAULT_FIXED_IDENTITY, FixedIdentityProvider
from harness.registry import add_member
from rheo_contracts import (
    ALL_OPERATIONS,
    ActorKind,
    AudienceKind,
    Entry,
    Role,
    WorkspaceContext,
)
from rheo_core.boundary.context import (
    MEMBERSHIP_MISSING,
    SESSION_EXPIRED,
    SESSION_MISSING,
    SESSION_REVOKED,
    SIGNUP_CLOSED,
    WORKSPACE_UNAVAILABLE,
    WORKSPACE_UNSELECTED,
    Refusal,
)
from rheo_core.boundary.factories import context_from_session
from rheo_core.identity import resolve_or_create, sync_providers
from rheo_core.identity.boundary import ProviderIdentity
from rheo_core.identity.providers.github import (
    GITHUB_SCOPE_COMPONENT,
    GITHUB_SCOPE_PREFIXES,
    GitHubProvider,
)
from rheo_core.secrets import SECRET_SCOPE_DENIED, SecretRef, SecretRefusal, SecretStore
from rheo_core.sessions import create_session, mint_host_secret
from rheo_core.sessions.cookies import (
    CONTINUE_COOKIE,
    OAUTH_REQUEST_COOKIE,
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE,
    build_cookie,
)
from rheo_core.settings.resolver import ResolvedSettings
from rheo_core.storage.control_plane import (
    get_identity_provider,
    insert_account,
    revoke_session,
    set_active_workspace,
    set_workspace_state,
    touch_session,
)
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.postgres import STORAGE_SCOPE_COMPONENT, STORAGE_SCOPE_PREFIXES
from sqlalchemy import text

pytestmark = pytest.mark.postgres

HOST = "shell.example.test"
OTHER_HOST = "other-host.example.test"


# --- test helpers -----------------------------------------------------------------


def _new_session(cluster: ClusterSession, account_id: UUID) -> tuple[UUID, bytes]:
    row = create_session(account_id)
    secret = mint_host_secret(row.id, HOST)
    return row.id, secret


def _point_at_workspace(
    cluster: ClusterSession, session_id: UUID, workspace_id: UUID
) -> None:
    """Bypass ``switch_workspace``'s membership check: test setup only, so an
    ordering test can point a session at a workspace its account is not a member of.
    """
    with cluster.backend.control_engine.begin() as connection:
        set_active_workspace(connection, session_id, workspace_id)


def _expire(cluster: ClusterSession, session_id: UUID) -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    with cluster.backend.control_engine.begin() as connection:
        touch_session(connection, session_id, last_seen_at=past, expires_at=past)


def _revoke(cluster: ClusterSession, session_id: UUID) -> None:
    with cluster.backend.control_engine.begin() as connection:
        revoke_session(connection, session_id)


def _mark_unavailable(cluster: ClusterSession, workspace_id: UUID) -> None:
    with cluster.backend.control_engine.begin() as connection:
        set_workspace_state(
            connection,
            workspace_id,
            state=WorkspaceState.UNAVAILABLE,
            state_detail="marked unavailable by the test",
        )


# --- sign-in creates a session row (row 6) -----------------------------------------


def test_sign_in_through_fixed_identity_provider_creates_a_session_row(
    cluster: ClusterSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shared test-session control database is not empty by the time this test
    # runs (other test modules create accounts too), so signup is forced open
    # explicitly rather than relying on the first-account bootstrap — this test is
    # about session creation, not about the signup-closed rule (that is its own test
    # below).
    monkeypatch.setenv("RHEO__identity__allow_signup", "true")
    provider = FixedIdentityProvider()
    identity = provider.complete({}, "unused-state")
    assert identity == DEFAULT_FIXED_IDENTITY
    account = resolve_or_create(identity)
    assert not isinstance(account, Refusal)
    session_row = create_session(account.id)
    with cluster.backend.control_engine.connect() as connection:
        found = connection.execute(
            text("SELECT account_id FROM control.session WHERE id = :id"),
            {"id": session_row.id},
        ).first()
    assert found is not None and found.account_id == account.id
    # Signing in again with the same identity resolves to the same account.
    again = resolve_or_create(provider.complete({}, "unused-state"))
    assert not isinstance(again, Refusal)
    assert again.id == account.id


def test_signup_closed_refuses_a_second_unknown_identity(
    cluster: ClusterSession, owner_account_id: UUID
) -> None:
    # owner_account_id already made the control plane non-empty, so signup is closed
    # by default (identity.allow_signup defaults false) for anyone else.
    stranger = ProviderIdentity(
        provider_id="test_harness",
        subject="a-stranger",
        email=None,
        email_verified=False,
        display_name="A Stranger",
    )
    refusal = resolve_or_create(stranger)
    assert isinstance(refusal, Refusal)
    assert refusal.state == SIGNUP_CLOSED


# --- the GitHub provider (row 7, and B9 rows 25-27) --------------------------------


def test_github_provider_completes_with_the_documented_shape_and_a_scoped_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cluster: ClusterSession,
) -> None:
    secret_value = "shh-not-a-real-secret"
    reference = "secret://env/RHEO_GITHUB_CLIENT_SECRET"
    monkeypatch.setenv("RHEO_GITHUB_CLIENT_SECRET", secret_value)
    secret_store = SecretStore(tmp_path)

    captured_requests: list[httpx.Request] = []
    captured_responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.url.path == "/login/oauth/access_token":
            response = httpx.Response(200, json={"access_token": "gho_fake_token"})
        elif request.url.path == "/user":
            response = httpx.Response(
                200, json={"id": 42, "login": "octocat", "name": "The Octocat"}
            )
        elif request.url.path == "/user/emails":
            response = httpx.Response(
                200,
                json=[
                    {"email": "old@example.test", "primary": False, "verified": True},
                    {
                        "email": "octo@example.test",
                        "primary": True,
                        "verified": True,
                    },
                ],
            )
        else:  # pragma: no cover - the test names every route the provider calls
            raise AssertionError(f"unexpected request to {request.url}")
        captured_responses.append(response)
        return response

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GitHubProvider(
        client_id="client-123",
        client_secret_ref=reference,
        secret_store=secret_store,
        client=client,
    )

    with caplog.at_level(logging.DEBUG):
        identity = provider.complete({"code": "the-code"}, "xyz")

    assert identity == ProviderIdentity(
        provider_id="github",
        subject="42",
        email="octo@example.test",
        email_verified=True,
        display_name="The Octocat",
    )

    # The secret reaches the token-exchange POST body only.
    token_requests = [
        r for r in captured_requests if r.url.path == "/login/oauth/access_token"
    ]
    assert len(token_requests) == 1
    token_request = token_requests[0]
    assert secret_value in token_request.content.decode("utf-8")
    other_requests = [r for r in captured_requests if r is not token_request]
    assert other_requests, "the test exercises more than the token exchange"
    for request in (*other_requests, token_request):
        if request is not token_request:
            assert secret_value not in request.content.decode("utf-8")
        assert secret_value not in str(request.url)
        for header_value in request.headers.values():
            assert secret_value not in header_value
            assert reference not in header_value

    for response in captured_responses:
        assert secret_value not in response.text
        assert reference not in response.text

    # No log line carries the secret value or the secret:// reference (B9 sign-in
    # clause, spec.md § Test strategy).
    for record in caplog.records:
        message = record.getMessage()
        assert secret_value not in message
        assert reference not in message

    # No control-plane row other than identity_provider.client_secret_ref ever
    # carries the resolved value or the reference string (B9 sign-in clause,
    # spec.md § Acceptance criteria).
    sync_providers(
        cluster.backend,
        _settings_with_github(client_id="client-123", client_secret_ref=reference),
    )
    _assert_control_plane_carries_neither(
        cluster,
        forbidden_value=secret_value,
        reference=reference,
        allowed_reference_column=("identity_provider", "client_secret_ref"),
    )


def test_github_client_secret_is_reachable_only_through_its_own_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHEO_GITHUB_CLIENT_SECRET", "shh-not-a-real-secret")
    store = SecretStore(tmp_path)
    reference = SecretRef.parse("secret://env/RHEO_GITHUB_CLIENT_SECRET")
    own_scope = SecretStore.scope_for(GITHUB_SCOPE_COMPONENT, *GITHUB_SCOPE_PREFIXES)
    assert store.resolve(reference, own_scope).expose() == b"shh-not-a-real-secret"
    wrong_scope = SecretStore.scope_for(
        STORAGE_SCOPE_COMPONENT, *STORAGE_SCOPE_PREFIXES
    )
    with pytest.raises(SecretRefusal) as excinfo:
        store.resolve(reference, wrong_scope)
    assert excinfo.value.state == SECRET_SCOPE_DENIED


def test_provider_sync_skips_an_unconfigured_provider(cluster: ClusterSession) -> None:
    """fit-check.md D5: disabled, or no client id, leaves the row exactly as it was.

    Deliberately compares before/after rather than asserting absence: another test in
    this module legitimately writes a real ``github`` row, and this test must hold
    regardless of run order.
    """

    def _current() -> object:
        with cluster.backend.control_engine.connect() as connection:
            return get_identity_provider(connection, "github")

    before = _current()
    sync_providers(
        cluster.backend,
        _settings_with_github(
            enabled=False, client_id="some-id", client_secret_ref="x"
        ),
    )
    assert _current() == before
    sync_providers(
        cluster.backend,
        _settings_with_github(enabled=True, client_id="", client_secret_ref="x"),
    )
    assert _current() == before


def _settings_with_github(
    *,
    enabled: bool = True,
    client_id: str = "client-123",
    client_secret_ref: str = "secret://env/RHEO_GITHUB_CLIENT_SECRET",
) -> ResolvedSettings:
    return ResolvedSettings(
        {
            "identity.providers.github.enabled": enabled,
            "identity.providers.github.client_id": client_id,
            "identity.providers.github.client_secret_ref": client_secret_ref,
        }
    )


def _assert_control_plane_carries_neither(
    cluster: ClusterSession,
    *,
    forbidden_value: str,
    reference: str,
    allowed_reference_column: tuple[str, str],
) -> None:
    with cluster.backend.control_engine.connect() as connection:
        columns = connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'control' AND data_type IN "
                "('text', 'character varying')"
            )
        ).all()
        for table_name, column_name in columns:
            values = (
                connection.execute(
                    text(f'SELECT "{column_name}" FROM control."{table_name}"')
                )
                .scalars()
                .all()
            )
            for value in values:
                if value is None:
                    continue
                assert forbidden_value not in value, (
                    table_name,
                    column_name,
                    "secret value leaked into a control-plane row",
                )
                if (table_name, column_name) == allowed_reference_column:
                    continue
                assert reference not in value, (
                    table_name,
                    column_name,
                    "secret reference leaked into a control-plane row",
                )


# --- context_from_session: the exact refusal order ---------------------------------


def test_context_from_session_refuses_session_missing_for_an_unknown_secret() -> None:
    refusal = context_from_session(os.urandom(32), HOST)
    assert isinstance(refusal, Refusal)
    assert refusal.state == SESSION_MISSING


def test_context_from_session_is_scoped_to_the_host_the_secret_was_minted_for(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    session_id, secret = _new_session(cluster, owner_account_id)
    _point_at_workspace(cluster, session_id, workspace)
    assert isinstance(context_from_session(secret, HOST), WorkspaceContext)
    refusal = context_from_session(secret, OTHER_HOST)
    assert isinstance(refusal, Refusal) and refusal.state == SESSION_MISSING


def test_context_from_session_refuses_session_expired_even_when_also_revoked(
    cluster: ClusterSession, owner_account_id: UUID
) -> None:
    session_id, secret = _new_session(cluster, owner_account_id)
    _expire(cluster, session_id)
    _revoke(cluster, session_id)
    refusal = context_from_session(secret, HOST)
    assert isinstance(refusal, Refusal)
    assert refusal.state == SESSION_EXPIRED


def test_context_from_session_refuses_session_revoked_before_workspace_unselected(
    cluster: ClusterSession, owner_account_id: UUID
) -> None:
    session_id, secret = _new_session(cluster, owner_account_id)
    # No active workspace was ever set: workspace_unselected would fire if revoked
    # were not checked first.
    _revoke(cluster, session_id)
    refusal = context_from_session(secret, HOST)
    assert isinstance(refusal, Refusal)
    assert refusal.state == SESSION_REVOKED


def test_context_from_session_refuses_workspace_unselected(
    cluster: ClusterSession, owner_account_id: UUID
) -> None:
    _, secret = _new_session(cluster, owner_account_id)
    refusal = context_from_session(secret, HOST)
    assert isinstance(refusal, Refusal)
    assert refusal.state == WORKSPACE_UNSELECTED


def test_context_from_session_membership_missing_before_workspace_tail(
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
) -> None:
    with cluster.backend.control_engine.begin() as connection:
        other_owner = insert_account(connection, display_name="other-owner").id
    other_workspace = make_workspace(owner=other_owner)
    _mark_unavailable(cluster, other_workspace)
    session_id, secret = _new_session(cluster, owner_account_id)
    _point_at_workspace(cluster, session_id, other_workspace)
    refusal = context_from_session(secret, HOST)
    assert isinstance(refusal, Refusal)
    # If the shared tail's workspace-active check ran before the membership read,
    # this would be workspace_unavailable instead.
    assert refusal.state == MEMBERSHIP_MISSING


def test_context_from_session_refuses_an_unavailable_workspace(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """B16's 0b2 factory clause for the session factory (mirrors
    ``tests/test_boundary.py``'s own
    ``test_both_factories_refuse_an_unavailable_workspace``, which this run may not
    edit)."""
    session_id, secret = _new_session(cluster, owner_account_id)
    _point_at_workspace(cluster, session_id, workspace)
    assert isinstance(context_from_session(secret, HOST), WorkspaceContext)
    _mark_unavailable(cluster, workspace)
    refusal = context_from_session(secret, HOST)
    assert isinstance(refusal, Refusal)
    assert refusal.state == WORKSPACE_UNAVAILABLE
    assert refusal.detail == "unavailable"


def test_context_from_session_succeeds_with_the_ratified_values(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    session_id, secret = _new_session(cluster, owner_account_id)
    _point_at_workspace(cluster, session_id, workspace)
    ctx = context_from_session(secret, HOST)
    assert isinstance(ctx, WorkspaceContext)
    assert ctx.workspace_id == workspace
    assert ctx.actor.kind is ActorKind.ACCOUNT and ctx.actor.id == owner_account_id
    assert ctx.role is Role.OWNER
    assert ctx.entry is Entry.WEB
    assert ctx.audience is not None
    assert (
        ctx.audience.kind is AudienceKind.SESSION
        and ctx.audience.id == owner_account_id
    )
    assert ctx.operation_set is ALL_OPERATIONS
    assert ctx.enabled_modules == frozenset()
    assert ctx.request_id.version == 7

    api_ctx = context_from_session(secret, HOST, entry="api")
    assert isinstance(api_ctx, WorkspaceContext)
    assert api_ctx.entry is Entry.API
    assert api_ctx.request_id != ctx.request_id


def test_context_from_session_reflects_the_membership_role(
    cluster: ClusterSession, workspace: UUID
) -> None:
    member_id = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="member-one"
    )
    session_id, secret = _new_session(cluster, member_id)
    _point_at_workspace(cluster, session_id, workspace)
    ctx = context_from_session(secret, HOST)
    assert isinstance(ctx, WorkspaceContext)
    assert ctx.role is Role.MEMBER


# --- the cookie builder (row 38's unit half) ---------------------------------------


@pytest.mark.parametrize(
    "name",
    [SESSION_COOKIE, OAUTH_STATE_COOKIE, CONTINUE_COOKIE, OAUTH_REQUEST_COOKIE],
)
def test_cookie_builder_sets_the_ratified_attributes(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHEO_PROFILE", "test")
    built_https = build_cookie(name, "opaque-value", scheme="https")
    assert "HttpOnly" in built_https
    assert "SameSite=Lax" in built_https
    assert "Path=/" in built_https
    assert "Secure" in built_https
    assert "Domain" not in built_https

    built_http_dev = build_cookie(name, "opaque-value", scheme="http")
    assert "Secure" not in built_http_dev
    assert "Domain" not in built_http_dev
    assert "HttpOnly" in built_http_dev


def test_cookie_builder_keeps_secure_in_production_even_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RHEO_PROFILE", "production")
    built = build_cookie(SESSION_COOKIE, "value", scheme="http")
    assert "Secure" in built


def test_cookie_builder_refuses_an_unknown_cookie_name() -> None:
    with pytest.raises(ValueError):
        build_cookie("not_a_real_cookie", "value", scheme="https")


def test_cookie_builder_signature_carries_no_domain_parameter() -> None:
    signature = inspect.signature(build_cookie)
    assert "domain" not in signature.parameters

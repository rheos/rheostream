"""The OAuth flow service's first half (issue #287): registration (AC-5, AC-6),
the authorization request, workspace binding (AC-10) and the pending-request
refusal matrix (AC-26, service half).

Seams under test: ``rheo_core.oauth.service``'s public functions
``register_client``, ``begin_authorization``, ``pending``, ``consent_choices``,
``approve`` and ``deny``. Every refusal is followed by a re-read of the database
from a fresh connection, because the property under test is what a refusal leaves
committed (or does not write at all).

The control plane is shared across tests: client ids, sources and accounts are
fresh per test, and counts are asserted as deltas or scoped to those ids.
"""

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.registry import register_harness
from rheo_core.oauth.service import (
    Approval,
    ConsentChoices,
    Denial,
    OAuthError,
    approve,
    begin_authorization,
    consent_choices,
    deny,
    pending,
    register_client,
)
from rheo_core.oauth.surface import OAuthLifetimes, OAuthSurface
from rheo_core.operations import register_core_operations
from rheo_core.refs import uuid7
from rheo_core.storage import control_tables as t
from rheo_core.storage import oauth_store
from rheo_core.storage import oauth_tables as o
from rheo_core.storage.control_plane import (
    SessionRow,
    insert_account,
    revoke_access_token,
    set_workspace_state,
)
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.oauth_store import OAuthAuthorizationRow
from rheo_core.tokens.issue import issue_connector_token
from rheo_core.tokens.sets import register_core_tools
from sqlalchemy import func, select, update

pytestmark = pytest.mark.postgres

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "https://mcp.example.test/"
ISSUER = "https://mcp.example.test"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def _surface(**lifetimes: int) -> OAuthSurface:
    values = {
        "access_token_minutes": 60,
        "grant_days": 30,
        "refresh_grace_seconds": 10,
        "max_clients": 100_000,
        "registrations_per_source_per_hour": 30,
        "abandoned_client_minutes": 60,
    }
    values.update(lifetimes)
    return OAuthSurface(
        resource=RESOURCE,
        issuer=ISSUER,
        authorization_endpoint="https://auth.example.test/auth/oauth/authorize",
        token_endpoint="https://auth.example.test/auth/oauth/token",
        registration_endpoint="https://auth.example.test/auth/oauth/register",
        protected_resource_metadata_url=(
            "https://mcp.example.test/.well-known/oauth-protected-resource"
        ),
        authorization_server_metadata_url=(
            "https://mcp.example.test/.well-known/oauth-authorization-server"
        ),
        redirect_uris=(CALLBACK,),
        lifetimes=OAuthLifetimes(**values),
        accepts_empty_path=True,
    )


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_core_tools()
    register_harness()


@pytest.fixture
def source() -> str:
    """A per-test client address, so the per-source limit is this test's own."""
    return f"source-{uuid7().hex}"


def _now() -> datetime:
    return datetime.now(UTC)


def _sha(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


# --- fresh-connection reads ----------------------------------------------------------


def _client_ids_from(cluster: ClusterSession, source: str) -> set[str]:
    with cluster.backend.control_engine.connect() as connection:
        return set(
            connection.execute(
                select(o.oauth_client.c.client_id).where(
                    o.oauth_client.c.registered_from == source
                )
            ).scalars()
        )


def _client_exists(cluster: ClusterSession, client_id: str) -> bool:
    with cluster.backend.control_engine.connect() as connection:
        return oauth_store.get_client(connection, client_id) is not None


def _refused_registrations_at(cluster: ClusterSession, at: datetime) -> int:
    with cluster.backend.control_engine.connect() as connection:
        return connection.execute(
            select(func.count())
            .select_from(o.oauth_event)
            .where(
                o.oauth_event.c.event == "client_registered",
                o.oauth_event.c.outcome == "refused",
                o.oauth_event.c.occurred_at == at,
                o.oauth_event.c.client_id.is_(None),
            )
        ).scalar_one()


def _events(cluster: ClusterSession, client_id: str) -> list[tuple[str, str]]:
    with cluster.backend.control_engine.connect() as connection:
        rows = connection.execute(
            select(o.oauth_event.c.event, o.oauth_event.c.outcome)
            .where(o.oauth_event.c.client_id == client_id)
            .order_by(o.oauth_event.c.id)
        ).all()
    return [(row.event, row.outcome) for row in rows]


def _row(cluster: ClusterSession, nonce: str) -> OAuthAuthorizationRow:
    with cluster.backend.control_engine.connect() as connection:
        row = oauth_store.get_authorization_by_request_hash(connection, _sha(nonce))
    assert row is not None
    return row


def _authorization_count(cluster: ClusterSession, client_id: str) -> int:
    with cluster.backend.control_engine.connect() as connection:
        return connection.execute(
            select(func.count())
            .select_from(o.oauth_authorization)
            .where(o.oauth_authorization.c.client_id == client_id)
        ).scalar_one()


def _issued(cluster: ClusterSession, account_id: UUID, client_id: str) -> int:
    """``access_token`` rows for the account plus ``oauth_grant`` rows for the
    client: anything a code could have led to."""
    with cluster.backend.control_engine.connect() as connection:
        tokens = connection.execute(
            select(func.count())
            .select_from(t.access_token)
            .where(t.access_token.c.account_id == account_id)
        ).scalar_one()
        grants = connection.execute(
            select(func.count())
            .select_from(o.oauth_grant)
            .where(o.oauth_grant.c.client_id == client_id)
        ).scalar_one()
    return tokens + grants


# --- AC-5: registration --------------------------------------------------------------


def _register(
    source: str, metadata: object, *, now: datetime | None = None, **lifetimes: int
) -> dict[str, object] | OAuthError:
    return register_client(
        _surface(**lifetimes),
        metadata=metadata,
        source=source,
        now=_now() if now is None else now,
    )


def _registered(source: str, **lifetimes: int) -> str:
    body = _register(source, {"redirect_uris": [CALLBACK]}, **lifetimes)
    assert isinstance(body, dict), body
    client_id = body["client_id"]
    assert isinstance(client_id, str)
    return client_id


def test_registration_returns_a_public_client(
    cluster: ClusterSession, source: str
) -> None:
    now = _now()
    body = _register(
        source,
        {"redirect_uris": [CALLBACK, CALLBACK], "client_name": "Claude"},
        now=now,
    )
    assert isinstance(body, dict), body
    client_id = body["client_id"]
    assert isinstance(client_id, str) and len(client_id) == 22
    assert body == {
        "client_id": client_id,
        "client_id_issued_at": int(now.timestamp()),
        "client_name": "Claude",
        "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    assert not any("secret" in key for key in body)
    with cluster.backend.control_engine.connect() as connection:
        client = oauth_store.get_client(connection, client_id)
    assert client is not None
    assert client.redirect_uris == (CALLBACK,)
    assert client.registered_from == source
    assert _events(cluster, client_id) == [("client_registered", "succeeded")]


def test_unsupported_metadata_is_overridden_not_refused(
    cluster: ClusterSession, source: str
) -> None:
    body = _register(
        source,
        {
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "client_secret_basic",
            "grant_types": ["client_credentials"],
            "response_types": ["token"],
            "client_name": "Ex\x00am\x1bple‮ " + "n" * 200,
            "logo_uri": "https://example.test/logo.png",
        },
    )
    assert isinstance(body, dict), body
    assert body["token_endpoint_auth_method"] == "none"
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    assert body["response_types"] == ["code"]
    assert "logo_uri" not in body
    name = body["client_name"]
    assert isinstance(name, str)
    assert name.startswith("Example n") and len(name) == 100
    assert all(ch.isprintable() for ch in name)


@pytest.mark.parametrize("name", [None, "", "  ", "\x00\x01", 42])
def test_absent_or_empty_client_name_defaults(source: str, name: object) -> None:
    metadata: dict[str, object] = {"redirect_uris": [CALLBACK]}
    if name is not None:
        metadata["client_name"] = name
    body = _register(source, metadata)
    assert isinstance(body, dict), body
    assert body["client_name"] == "unnamed client"


@pytest.mark.parametrize(
    "redirect_uris",
    [
        ["https://other.example.test/api/mcp/auth_callback"],
        [CALLBACK + "/"],
        [CALLBACK + "/extra"],
        ["http://claude.ai/api/mcp/auth_callback"],
        ["https://claude.ai:8443/api/mcp/auth_callback"],
        ["https://user@claude.ai/api/mcp/auth_callback"],
        [CALLBACK + "#fragment"],
        ["https://CLAUDE.AI/api/mcp/auth_callback"],
        [CALLBACK, "https://other.example.test/cb"],
        [],
        CALLBACK,
        [42],
        None,
    ],
    ids=[
        "other-host",
        "trailing-slash",
        "path-suffix",
        "http",
        "port",
        "userinfo",
        "fragment",
        "uppercase-host",
        "one-bad-of-two",
        "empty",
        "not-a-list",
        "not-a-string",
        "absent",
    ],
)
def test_redirect_uri_refusals_commit_a_refused_event(
    cluster: ClusterSession, source: str, redirect_uris: object
) -> None:
    now = _now()
    metadata = {} if redirect_uris is None else {"redirect_uris": redirect_uris}
    result = _register(source, metadata, now=now)
    assert result == OAuthError("invalid_redirect_uri", 400, redirect=False)
    assert _client_ids_from(cluster, source) == set()
    assert _refused_registrations_at(cluster, now) == 1


@pytest.mark.parametrize("body", [[CALLBACK], "redirect_uris", 7, None])
def test_non_object_body_is_invalid_client_metadata(
    cluster: ClusterSession, source: str, body: object
) -> None:
    now = _now()
    result = _register(source, body, now=now)
    assert result == OAuthError("invalid_client_metadata", 400, redirect=False)
    assert isinstance(result, OAuthError)
    assert result.error_description == "The registration body must be a JSON object."
    assert _client_ids_from(cluster, source) == set()
    assert _refused_registrations_at(cluster, now) == 1


# --- AC-6: limits and cleanup --------------------------------------------------------


def test_per_source_limit_refuses_and_commits_the_refusal(
    cluster: ClusterSession, source: str
) -> None:
    first = _registered(source, registrations_per_source_per_hour=2)
    second = _registered(source, registrations_per_source_per_hour=2)
    now = _now()
    result = _register(
        source,
        {"redirect_uris": [CALLBACK]},
        now=now,
        registrations_per_source_per_hour=2,
    )
    assert result == OAuthError("temporarily_unavailable", 429, redirect=False)
    assert _client_ids_from(cluster, source) == {first, second}
    assert _refused_registrations_at(cluster, now) == 1
    # Another source is not limited by this one.
    assert isinstance(_register(f"{source}-b", {"redirect_uris": [CALLBACK]}), dict)


def test_per_source_limit_counts_before_cleanup_deletes(
    cluster: ClusterSession, source: str
) -> None:
    """With ``abandoned_client_minutes`` at 15, two 30-minute-old registrations are
    abandoned and cleanup deletes them; the hourly limit must still count them."""
    now = _now()
    earlier = now - timedelta(minutes=30)
    limits = {"registrations_per_source_per_hour": 2, "abandoned_client_minutes": 15}
    for _ in range(2):
        body = _register(source, {"redirect_uris": [CALLBACK]}, now=earlier, **limits)
        assert isinstance(body, dict), body
    result = _register(source, {"redirect_uris": [CALLBACK]}, now=now, **limits)
    assert result == OAuthError("temporarily_unavailable", 429, redirect=False)
    # Cleanup did run in the same step: the two abandoned clients are gone.
    assert _client_ids_from(cluster, source) == set()
    assert _refused_registrations_at(cluster, now) == 1


def test_counted_client_cap_refuses_and_commits_the_refusal(
    cluster: ClusterSession, source: str
) -> None:
    now = _now()
    with cluster.backend.control_engine.connect() as connection:
        counted = oauth_store.count_counted_clients(
            connection, now=now, abandoned_before=now - timedelta(minutes=60)
        )
    cap = counted + 1
    kept = _registered(source, max_clients=cap)
    refused_at = _now()
    result = _register(
        source, {"redirect_uris": [CALLBACK]}, now=refused_at, max_clients=cap
    )
    assert result == OAuthError("temporarily_unavailable", 429, redirect=False)
    assert _client_ids_from(cluster, source) == {kept}
    assert _refused_registrations_at(cluster, refused_at) == 1


def _old_client(cluster: ClusterSession, created_at: datetime) -> str:
    client_id = f"client-{uuid7().hex}"
    with cluster.backend.control_engine.begin() as connection:
        oauth_store.insert_client(
            connection,
            client_id=client_id,
            client_name="Example connector",
            registered_from="192.0.2.20",
            created_at=created_at,
            redirect_uris=[CALLBACK],
        )
    return client_id


def _authorization(
    cluster: ClusterSession, client_id: str, *, created_at: datetime
) -> UUID:
    with cluster.backend.control_engine.begin() as connection:
        return oauth_store.insert_authorization(
            connection,
            request_hash=uuid7().bytes * 2,
            client_id=client_id,
            redirect_uri=CALLBACK,
            code_challenge=CHALLENGE,
            state=None,
            created_at=created_at,
            expires_at=created_at + timedelta(minutes=10),
        ).id


def test_registration_cleans_up_abandoned_clients_only(
    cluster: ClusterSession, source: str
) -> None:
    """A three-hour-old client that never got a grant goes, with its redirect URIs
    and its long-expired authorization row; one of the same age with a sign-in
    still inside its 10 minutes stays."""
    now = _now()
    old = now - timedelta(hours=3)
    abandoned = _old_client(cluster, old)
    stale_request = _authorization(cluster, abandoned, created_at=old)
    in_flight = _old_client(cluster, old)
    _authorization(cluster, in_flight, created_at=now - timedelta(minutes=2))
    _registered(source)
    assert not _client_exists(cluster, abandoned)
    with cluster.backend.control_engine.connect() as connection:
        uris = connection.execute(
            select(func.count())
            .select_from(o.oauth_client_redirect_uri)
            .where(o.oauth_client_redirect_uri.c.client_id == abandoned)
        ).scalar_one()
        request = connection.execute(
            select(o.oauth_authorization.c.id).where(
                o.oauth_authorization.c.id == stale_request
            )
        ).first()
    assert uris == 0
    assert request is None
    assert _client_exists(cluster, in_flight)
    assert _authorization_count(cluster, in_flight) == 1


def _grant(
    cluster: ClusterSession,
    client_id: str,
    account_id: UUID,
    workspace_id: UUID,
    *,
    grant_expires_at: datetime,
) -> UUID:
    with cluster.backend.control_engine.begin() as connection:
        token_id, _, _ = issue_connector_token(
            connection,
            account_id=account_id,
            workspace_id=workspace_id,
            expires_at=_now() + timedelta(hours=1),
        )
        oauth_store.insert_grant(
            connection,
            token_id=token_id,
            client_id=client_id,
            created_at=_now(),
            grant_expires_at=grant_expires_at,
        )
    return token_id


def test_granted_clients_survive_cleanup_and_dead_ones_are_not_counted(
    cluster: ClusterSession, source: str, workspace: UUID, owner_account_id: UUID
) -> None:
    """An idle grant (access value lapsed hours ago), a revoked grant and a grant
    past its absolute end all survive a registration-triggered cleanup; only the
    idle one counts toward the cap."""
    now = _now()
    old = now - timedelta(hours=5)
    with cluster.backend.control_engine.connect() as connection:
        before = oauth_store.count_counted_clients(
            connection, now=now, abandoned_before=now - timedelta(minutes=60)
        )
    idle, revoked, past_end = (_old_client(cluster, old) for _ in range(3))
    idle_token = _grant(
        cluster,
        idle,
        owner_account_id,
        workspace,
        grant_expires_at=now + timedelta(days=20),
    )
    revoked_token = _grant(
        cluster,
        revoked,
        owner_account_id,
        workspace,
        grant_expires_at=now + timedelta(days=20),
    )
    _grant(
        cluster,
        past_end,
        owner_account_id,
        workspace,
        grant_expires_at=now - timedelta(minutes=5),
    )
    with cluster.backend.control_engine.begin() as connection:
        connection.execute(
            update(t.access_token)
            .where(t.access_token.c.id == idle_token)
            .values(expires_at=now - timedelta(hours=3))
        )
        revoke_access_token(connection, revoked_token)
    _registered(source, abandoned_client_minutes=15)
    for client_id in (idle, revoked, past_end):
        assert _client_exists(cluster, client_id), client_id
    with cluster.backend.control_engine.connect() as connection:
        after = oauth_store.count_counted_clients(
            connection, now=now, abandoned_before=now - timedelta(minutes=60)
        )
        listed = oauth_store.get_client(connection, revoked)
    # The idle client and the one just registered; not the revoked or past-end one.
    assert after - before == 2
    assert listed is not None and listed.client_name == "Example connector"


def test_an_exception_rolls_the_step_back(
    cluster: ClusterSession, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard that tells the two paths apart: a refusal (returned) commits its
    event, as the refusal tests above show; an exception after a first write rolls
    that write back."""

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("store failure")

    monkeypatch.setattr(oauth_store, "insert_event", boom)
    with pytest.raises(RuntimeError):
        _register(source, {"redirect_uris": [CALLBACK]})
    assert _client_ids_from(cluster, source) == set()


# --- the authorization request -------------------------------------------------------


def _params(registered: str, **overrides: str | None) -> dict[str, str]:
    params: dict[str, str | None] = {
        "response_type": "code",
        "client_id": registered,
        "redirect_uri": CALLBACK,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
        "state": "opaque-state",
    }
    params.update(overrides)
    return {key: value for key, value in params.items() if value is not None}


def _begin(
    client_id: str, *, now: datetime | None = None, **overrides: str | None
) -> str:
    nonce = begin_authorization(
        _surface(), _params(client_id, **overrides), now=_now() if now is None else now
    )
    assert isinstance(nonce, str), nonce
    return nonce


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"client_id": "no-such-client"}, "invalid_client"),
        ({"client_id": None}, "invalid_client"),
        ({"redirect_uri": "https://other.example.test/cb"}, "invalid_redirect_uri"),
        ({"redirect_uri": CALLBACK + "/"}, "invalid_redirect_uri"),
        ({"redirect_uri": None}, "invalid_redirect_uri"),
    ],
)
def test_untrusted_redirect_errors_are_pages(
    cluster: ClusterSession,
    source: str,
    overrides: dict[str, str | None],
    error: str,
) -> None:
    client_id = _registered(source)
    result = begin_authorization(
        _surface(), _params(client_id, **overrides), now=_now()
    )
    assert result == OAuthError(error, 400, redirect=False)
    assert _authorization_count(cluster, client_id) == 0


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"response_type": None}, "unsupported_response_type"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge_method": None}, "invalid_request"),
        ({"code_challenge": None}, "invalid_request"),
        ({"code_challenge": CHALLENGE[:42]}, "invalid_request"),
        ({"code_challenge": "a" * 129}, "invalid_request"),
        ({"code_challenge": CHALLENGE[:42] + "="}, "invalid_request"),
        ({"resource": None}, "invalid_target"),
        ({"resource": "https://mcp.example.test/mcp"}, "invalid_target"),
        ({"resource": "https://other.example.test/"}, "invalid_target"),
    ],
)
def test_redirectable_errors_carry_state_and_iss(
    cluster: ClusterSession,
    source: str,
    overrides: dict[str, str | None],
    error: str,
) -> None:
    client_id = _registered(source)
    result = begin_authorization(
        _surface(), _params(client_id, **overrides), now=_now()
    )
    assert result == OAuthError(
        error,
        302,
        redirect=True,
        redirect_uri=CALLBACK,
        state="opaque-state",
        iss=ISSUER,
    )
    assert _authorization_count(cluster, client_id) == 0


def test_over_long_state_is_refused_and_not_echoed(
    cluster: ClusterSession, source: str
) -> None:
    client_id = _registered(source)
    result = begin_authorization(
        _surface(), _params(client_id, state="s" * 1025), now=_now()
    )
    assert result == OAuthError(
        "invalid_request", 302, redirect=True, redirect_uri=CALLBACK, iss=ISSUER
    )
    assert _authorization_count(cluster, client_id) == 0
    assert isinstance(
        begin_authorization(
            _surface(), _params(client_id, state="s" * 1024), now=_now()
        ),
        str,
    )


def test_begin_holds_the_request_server_side(
    cluster: ClusterSession, source: str
) -> None:
    client_id = _registered(source)
    now = _now()
    nonce = _begin(client_id, now=now, resource=ISSUER)  # empty-path spelling
    assert len(nonce) == 64 and int(nonce, 16) >= 0
    row = _row(cluster, nonce)
    assert row.request_hash == _sha(nonce)
    assert row.client_id == client_id
    assert row.redirect_uri == CALLBACK
    assert row.code_challenge == CHALLENGE
    assert row.state == "opaque-state"
    assert row.expires_at == now + timedelta(minutes=10)
    assert row.decided_at is None and row.code_hash is None
    # No state at all is fine too.
    assert isinstance(_begin(client_id, state=None), str)


def test_pending_boundaries(cluster: ClusterSession, source: str) -> None:
    client_id = _registered(source)
    now = _now()
    nonce = _begin(client_id, now=now)
    expires_at = now + timedelta(minutes=10)
    live = pending(nonce, expires_at - timedelta(seconds=1))
    assert live is not None and live.client_id == client_id
    assert pending(nonce, expires_at) is None
    assert pending(None, now) is None
    assert pending("", now) is None
    assert pending("00" * 32, now) is None


# --- AC-10: workspace binding --------------------------------------------------------


def _session(account_id: UUID, active: UUID | None) -> SessionRow:
    now = _now()
    return SessionRow(
        id=uuid7(),
        account_id=account_id,
        active_workspace_id=active,
        active_workspace_changed_at=None,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=1),
        revoked_at=None,
    )


def test_one_workspace_binds(
    cluster: ClusterSession, source: str, workspace: UUID, owner_account_id: UUID
) -> None:
    choices = consent_choices(owner_account_id, _session(owner_account_id, None))
    assert [choice.workspace_id for choice in choices.workspaces] == [workspace]
    assert choices.default == workspace
    client_id = _registered(source)
    nonce = _begin(client_id)
    row = _row(cluster, nonce)
    now = _now()
    approval = approve(row.id, nonce, owner_account_id, workspace, now=now)
    assert isinstance(approval, Approval), approval
    assert approval.redirect_uri == CALLBACK and approval.state == "opaque-state"
    after = _row(cluster, nonce)
    assert after.decided_at == now
    assert after.account_id == owner_account_id and after.workspace_id == workspace
    assert after.code_hash == _sha(approval.code)
    assert after.code_expires_at == now + timedelta(seconds=60)
    assert _issued(cluster, owner_account_id, client_id) == 0
    assert _events(cluster, client_id) == [
        ("client_registered", "succeeded"),
        ("authorization_granted", "succeeded"),
    ]


def test_two_workspaces_offer_both_and_bind_the_chosen(
    cluster: ClusterSession,
    source: str,
    make_workspace: object,
    owner_account_id: UUID,
) -> None:
    first = make_workspace()  # type: ignore[operator]
    second = make_workspace()  # type: ignore[operator]
    archived = make_workspace()  # type: ignore[operator]
    with cluster.backend.control_engine.begin() as connection:
        set_workspace_state(
            connection, archived, state=WorkspaceState.UNAVAILABLE, state_detail=None
        )
    choices = consent_choices(owner_account_id, _session(owner_account_id, second))
    assert isinstance(choices, ConsentChoices)
    assert {choice.workspace_id for choice in choices.workspaces} == {first, second}
    assert choices.default == second
    elsewhere = consent_choices(owner_account_id, _session(owner_account_id, archived))
    assert elsewhere.default == first
    client_id = _registered(source)
    nonce = _begin(client_id)
    row = _row(cluster, nonce)
    # The archived workspace is not a choice.
    assert approve(row.id, nonce, owner_account_id, archived, now=_now()) == (
        OAuthError("not_a_member", 403, redirect=False)
    )
    approval = approve(row.id, nonce, owner_account_id, second, now=_now())
    assert isinstance(approval, Approval), approval
    assert _row(cluster, nonce).workspace_id == second


def test_no_workspace_is_workspace_unselected_and_decides_the_row(
    cluster: ClusterSession, source: str
) -> None:
    with cluster.backend.control_engine.begin() as connection:
        lonely = insert_account(connection, display_name="no-workspace").id
    choices = consent_choices(lonely, _session(lonely, None))
    assert choices == ConsentChoices(workspaces=(), default=None)
    client_id = _registered(source)
    nonce = _begin(client_id)
    row = _row(cluster, nonce)
    now = _now()
    result = approve(row.id, nonce, lonely, None, now=now)
    assert result == OAuthError("workspace_unselected", 403, redirect=False)
    after = _row(cluster, nonce)
    assert after.decided_at == now
    assert after.code_hash is None and after.code_expires_at is None
    assert after.workspace_id is None
    assert _issued(cluster, lonely, client_id) == 0
    assert _events(cluster, client_id)[-1] == ("authorization_granted", "refused")
    assert pending(nonce, now) is None


def test_non_member_workspace_is_refused_and_writes_nothing(
    cluster: ClusterSession,
    source: str,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    client_id = _registered(source)
    nonce = _begin(client_id)
    before = _row(cluster, nonce)
    stranger_workspace = uuid7()
    result = approve(before.id, nonce, owner_account_id, stranger_workspace, now=_now())
    assert result == OAuthError("not_a_member", 403, redirect=False)
    assert _row(cluster, nonce) == before
    assert _issued(cluster, owner_account_id, client_id) == 0
    assert _events(cluster, client_id) == [("client_registered", "succeeded")]


# --- AC-26, service half: a pending request completes once, in time, here ---------


@pytest.fixture
def flow(
    cluster: ClusterSession, source: str, workspace: UUID, owner_account_id: UUID
) -> Iterator[tuple[str, UUID, UUID]]:
    yield _registered(source), owner_account_id, workspace


def _assert_untouched(
    cluster: ClusterSession,
    nonce: str,
    before: OAuthAuthorizationRow,
    account_id: UUID,
    client_id: str,
) -> None:
    after = _row(cluster, nonce)
    assert after == before
    assert after.code_hash is None and after.code_expires_at is None
    assert _issued(cluster, account_id, client_id) == 0


def test_expired_request_is_refused_at_expiry_and_live_one_second_before(
    cluster: ClusterSession, flow: tuple[str, UUID, UUID]
) -> None:
    client_id, account_id, workspace = flow
    now = _now()
    nonce = _begin(client_id, now=now)
    before = _row(cluster, nonce)
    expires_at = now + timedelta(minutes=10)
    for verb in ("approve", "deny"):
        result = (
            approve(before.id, nonce, account_id, workspace, now=expires_at)
            if verb == "approve"
            else deny(before.id, nonce, account_id, now=expires_at)
        )
        assert result == OAuthError("authorization_expired", 400, redirect=False), verb
        _assert_untouched(cluster, nonce, before, account_id, client_id)
    late = approve(
        before.id, nonce, account_id, workspace, now=expires_at - timedelta(seconds=1)
    )
    assert isinstance(late, Approval), late


@pytest.mark.parametrize("cookie", [None, "", "ab" * 32])
def test_missing_or_unknown_cookie_is_refused(
    cluster: ClusterSession, flow: tuple[str, UUID, UUID], cookie: str | None
) -> None:
    client_id, account_id, workspace = flow
    nonce = _begin(client_id)
    before = _row(cluster, nonce)
    assert approve(before.id, cookie, account_id, workspace, now=_now()) == (
        OAuthError("authorization_expired", 400, redirect=False)
    )
    assert deny(before.id, cookie, account_id, now=_now()) == (
        OAuthError("authorization_expired", 400, redirect=False)
    )
    _assert_untouched(cluster, nonce, before, account_id, client_id)


def test_another_rows_id_is_refused(
    cluster: ClusterSession, flow: tuple[str, UUID, UUID]
) -> None:
    """A browser holding its own live row cannot approve someone else's by id."""
    client_id, account_id, workspace = flow
    theirs = _begin(client_id)
    mine = _begin(client_id)
    their_row = _row(cluster, theirs)
    my_row = _row(cluster, mine)
    assert approve(their_row.id, mine, account_id, workspace, now=_now()) == (
        OAuthError("authorization_expired", 400, redirect=False)
    )
    assert deny(their_row.id, mine, account_id, now=_now()) == (
        OAuthError("authorization_expired", 400, redirect=False)
    )
    _assert_untouched(cluster, theirs, their_row, account_id, client_id)
    _assert_untouched(cluster, mine, my_row, account_id, client_id)
    assert _events(cluster, client_id) == [("client_registered", "succeeded")]


def test_denied_request_cannot_be_replayed(
    cluster: ClusterSession, flow: tuple[str, UUID, UUID]
) -> None:
    client_id, account_id, workspace = flow
    nonce = _begin(client_id)
    row = _row(cluster, nonce)
    denial = deny(row.id, nonce, account_id, now=_now())
    assert denial == Denial(redirect_uri=CALLBACK, state="opaque-state")
    decided = _row(cluster, nonce)
    assert decided.decided_at is not None
    assert pending(nonce, _now()) is None
    assert approve(row.id, nonce, account_id, workspace, now=_now()) == (
        OAuthError("authorization_expired", 400, redirect=False)
    )
    assert deny(row.id, nonce, account_id, now=_now()) == (
        OAuthError("authorization_expired", 400, redirect=False)
    )
    _assert_untouched(cluster, nonce, decided, account_id, client_id)
    assert _events(cluster, client_id) == [
        ("client_registered", "succeeded"),
        ("authorization_denied", "refused"),
    ]


def test_approved_request_cannot_be_replayed(
    cluster: ClusterSession, flow: tuple[str, UUID, UUID]
) -> None:
    client_id, account_id, workspace = flow
    nonce = _begin(client_id)
    row = _row(cluster, nonce)
    first = approve(row.id, nonce, account_id, workspace, now=_now())
    assert isinstance(first, Approval), first
    decided = _row(cluster, nonce)
    assert approve(row.id, nonce, account_id, workspace, now=_now()) == (
        OAuthError("authorization_expired", 400, redirect=False)
    )
    assert deny(row.id, nonce, account_id, now=_now()) == (
        OAuthError("authorization_expired", 400, redirect=False)
    )
    after = _row(cluster, nonce)
    assert after == decided
    assert after.code_hash == _sha(first.code)
    assert _issued(cluster, account_id, client_id) == 0
    assert _events(cluster, client_id) == [
        ("client_registered", "succeeded"),
        ("authorization_granted", "succeeded"),
    ]


def test_an_exception_in_approve_rolls_the_decision_back(
    cluster: ClusterSession,
    flow: tuple[str, UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``record_authorization_decision`` writes first; the event insert then raises.
    The decision must not survive (contrast: ``workspace_unselected``, a returned
    refusal, keeps its decided row)."""
    client_id, account_id, workspace = flow
    nonce = _begin(client_id)
    before = _row(cluster, nonce)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("store failure")

    monkeypatch.setattr(oauth_store, "insert_event", boom)
    with pytest.raises(RuntimeError):
        approve(before.id, nonce, account_id, workspace, now=_now())
    _assert_untouched(cluster, nonce, before, account_id, client_id)
    assert pending(nonce, _now()) == before

"""The OAuth flow service's token half (issue #287): code exchange and refresh
rotation (AC-11 grant half, AC-12, AC-13, AC-14, AC-20 service half, edge cases
3, 4 and 14).

Seams under test: ``rheo_core.oauth.service.exchange_code`` and ``refresh``, and
what each leaves committed. Every refusal is followed by a re-read from a fresh
connection, because the property under test is the state a refusal writes (a
burned code, a revocation, an event row) or does not write at all.

Clients, authorizations and grants are seeded through the service's own
``register_client``, ``begin_authorization`` and ``approve``, never raw SQL. The
spec's "grant id" is the grant's access-token id, ``token_id``.
"""

import base64
import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.registry import add_member, register_harness
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.boundary.context import Refusal
from rheo_core.oauth import service
from rheo_core.oauth.service import (
    Approval,
    OAuthError,
    approve,
    begin_authorization,
    deny,
    exchange_code,
    refresh,
    register_client,
)
from rheo_core.oauth.surface import OAuthLifetimes, OAuthSurface
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.core_ops import TOKEN_REVOKE
from rheo_core.refs import uuid7
from rheo_core.storage import control_tables as t
from rheo_core.storage import oauth_store
from rheo_core.storage import oauth_tables as o
from rheo_core.storage.control_plane import (
    AccessTokenRow,
    get_access_token,
    revoke_access_token,
    set_workspace_state,
)
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.oauth_store import (
    OAuthAuthorizationRow,
    OAuthEventRow,
    OAuthGrantRow,
)
from rheo_core.storage.postgres import get_backend
from rheo_core.tokens import issue as issue_module
from rheo_core.tokens.presentation import ResolvedToken, resolve_token
from rheo_core.tokens.sets import register_core_tools
from sqlalchemy import Connection, delete, func, select

pytestmark = pytest.mark.postgres

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "https://mcp.example.test/"
ISSUER = "https://mcp.example.test"
VERIFIER = "dBjftJeZ4CVP-mJ92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .rstrip(b"=")
    .decode()
)
"""The verifier's S256 challenge, computed independently of the service."""
MCP_MAX_DAYS = 30
"""``identity.token_max_days.mcp``'s default."""


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


def _now() -> datetime:
    return datetime.now(UTC)


def _sha(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


# --- seeding through the service ------------------------------------------------------


def _register(*, now: datetime | None = None, **lifetimes: int) -> str:
    registered = register_client(
        _surface(**lifetimes),
        metadata={"redirect_uris": [CALLBACK], "client_name": "Example connector"},
        source=f"source-{uuid7().hex}",
        now=_now() if now is None else now,
    )
    assert isinstance(registered, dict), registered
    client_id = registered["client_id"]
    assert isinstance(client_id, str)
    return client_id


def _code(
    client_id: str, account_id: UUID, workspace_id: UUID, *, now: datetime
) -> str:
    nonce = begin_authorization(
        _surface(),
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
            "state": "opaque-state",
        },
        now=now,
    )
    assert isinstance(nonce, str), nonce
    with_row = _authorization_by_request(nonce)
    approval = approve(with_row.id, nonce, account_id, workspace_id, now=now)
    assert isinstance(approval, Approval), approval
    return approval.code


def _code_form(client_id: str, code: str, /, **overrides: str | None) -> dict[str, str]:
    form: dict[str, str | None] = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "code_verifier": VERIFIER,
    }
    form.update(overrides)
    return {key: value for key, value in form.items() if value is not None}


def _refresh_form(
    client_id: str, refresh_token: str, /, **overrides: str | None
) -> dict[str, str]:
    form: dict[str, str | None] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    form.update(overrides)
    return {key: value for key, value in form.items() if value is not None}


def _redeem(
    client_id: str, code: str, *, now: datetime, **lifetimes: int
) -> dict[str, object]:
    issued = exchange_code(_surface(**lifetimes), _code_form(client_id, code), now=now)
    assert isinstance(issued, dict), issued
    return issued


def _refreshed(
    client_id: str, refresh_token: str, *, now: datetime, **lifetimes: int
) -> dict[str, object]:
    issued = refresh(
        _surface(**lifetimes), _refresh_form(client_id, refresh_token), now=now
    )
    assert isinstance(issued, dict), issued
    return issued


def _str(issued: dict[str, object], key: str) -> str:
    value = issued[key]
    assert isinstance(value, str)
    return value


@dataclasses.dataclass(frozen=True)
class Connector:
    client_id: str
    account_id: UUID
    workspace_id: UUID
    code: str
    now: datetime


@pytest.fixture
def connector(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> Connector:
    """A registered client and an approved, unredeemed code for the owner."""
    now = _now()
    client_id = _register(now=now)
    code = _code(client_id, owner_account_id, workspace, now=now)
    return Connector(client_id, owner_account_id, workspace, code, now)


# --- fresh-connection reads ----------------------------------------------------------


def _authorization_by_request(nonce: str) -> OAuthAuthorizationRow:
    with get_backend().control_engine.connect() as connection:
        row = oauth_store.get_authorization_by_request_hash(connection, _sha(nonce))
    assert row is not None
    return row


def _authorization(cluster: ClusterSession, code: str) -> OAuthAuthorizationRow:
    with cluster.backend.control_engine.connect() as connection:
        row = oauth_store.get_authorization_by_code_hash(connection, _sha(code))
    assert row is not None
    return row


def _token(cluster: ClusterSession, token_id: UUID) -> AccessTokenRow:
    with cluster.backend.control_engine.connect() as connection:
        row = get_access_token(connection, token_id)
    assert row is not None
    return row


def _grant(cluster: ClusterSession, token_id: UUID) -> OAuthGrantRow:
    with cluster.backend.control_engine.connect() as connection:
        row = oauth_store.get_grant(connection, token_id)
    assert row is not None
    return row


def _refresh_rows(
    cluster: ClusterSession, token_id: UUID
) -> list[tuple[bytes, datetime | None]]:
    with cluster.backend.control_engine.connect() as connection:
        rows = connection.execute(
            select(
                o.oauth_refresh_token.c.token_hash, o.oauth_refresh_token.c.rotated_at
            )
            .where(o.oauth_refresh_token.c.token_id == token_id)
            .order_by(o.oauth_refresh_token.c.created_at)
        ).all()
    return [(bytes(row.token_hash), row.rotated_at) for row in rows]


def _events(cluster: ClusterSession, client_id: str) -> list[OAuthEventRow]:
    with cluster.backend.control_engine.connect() as connection:
        rows = (
            connection.execute(
                select(o.oauth_event)
                .where(o.oauth_event.c.client_id == client_id)
                .order_by(o.oauth_event.c.id)
            )
            .mappings()
            .all()
        )
    return [
        OAuthEventRow(
            id=row["id"],
            occurred_at=row["occurred_at"],
            event=row["event"],
            outcome=row["outcome"],
            client_id=row["client_id"],
            account_id=row["account_id"],
            workspace_id=row["workspace_id"],
            token_id=row["token_id"],
        )
        for row in rows
    ]


def _event_words(cluster: ClusterSession, client_id: str) -> list[tuple[str, str]]:
    return [(row.event, row.outcome) for row in _events(cluster, client_id)]


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


def _resolves(value: str) -> ResolvedToken | Refusal:
    return resolve_token(value, "mcp")


def _state(value: str) -> str | None:
    resolved = _resolves(value)
    return resolved.state if isinstance(resolved, Refusal) else None


def _invalid(error: str) -> OAuthError:
    return OAuthError(error, 401 if error == "invalid_client" else 400, redirect=False)


# --- AC-11 (grant half) and AC-12: what a redemption issues ---------------------------


def test_redemption_issues_an_mcp_token_its_grant_and_a_refresh_token(
    cluster: ClusterSession, connector: Connector
) -> None:
    now = connector.now
    issued = _redeem(connector.client_id, connector.code, now=now)
    assert set(issued) == {"access_token", "token_type", "expires_in", "refresh_token"}
    access = _str(issued, "access_token")
    assert access.startswith("rheo_mcp_")
    assert issued["token_type"] == "Bearer"
    assert issued["expires_in"] == 3600
    authorization = _authorization(cluster, connector.code)
    assert authorization.code_used_at == now
    token_id = authorization.token_id
    assert token_id is not None
    token = _token(cluster, token_id)
    assert token.kind == "mcp" and token.issued_from == "connector"
    assert token.account_id == connector.account_id
    assert token.workspace_id == connector.workspace_id
    assert token.expires_at == now + timedelta(minutes=60)
    grant = _grant(cluster, token_id)
    assert grant.client_id == connector.client_id
    assert grant.grant_expires_at == now + timedelta(days=30)
    assert _refresh_rows(cluster, token_id) == [
        (_sha(_str(issued, "refresh_token")), None)
    ]
    resolved = _resolves(access)
    assert isinstance(resolved, ResolvedToken), resolved
    redeemed = _events(cluster, connector.client_id)[-1]
    assert (redeemed.event, redeemed.outcome) == ("code_redeemed", "succeeded")
    assert redeemed.token_id == token_id


def test_grant_days_above_the_mcp_cap_end_at_the_cap(
    cluster: ClusterSession, connector: Connector
) -> None:
    """AC-12: ``grant_days`` 45 against ``token_max_days.mcp`` 30."""
    now = connector.now
    _redeem(connector.client_id, connector.code, now=now, grant_days=45)
    token_id = _authorization(cluster, connector.code).token_id
    assert token_id is not None
    grant = _grant(cluster, token_id)
    assert grant.grant_expires_at == now + timedelta(days=MCP_MAX_DAYS)
    assert _token(cluster, token_id).expires_at <= grant.grant_expires_at


def test_access_expiry_never_passes_the_grant_end(
    cluster: ClusterSession, connector: Connector
) -> None:
    """An access lifetime longer than the grant is cut to the grant's end."""
    now = connector.now
    issued = _redeem(
        connector.client_id,
        connector.code,
        now=now,
        grant_days=1,
        access_token_minutes=3 * 24 * 60,
    )
    token_id = _authorization(cluster, connector.code).token_id
    assert token_id is not None
    grant = _grant(cluster, token_id)
    assert grant.grant_expires_at == now + timedelta(days=1)
    assert _token(cluster, token_id).expires_at == grant.grant_expires_at
    assert issued["expires_in"] == 24 * 3600


def test_refresh_at_the_grant_cap_is_refused_and_just_before_is_cut_to_it(
    cluster: ClusterSession, connector: Connector
) -> None:
    now = connector.now
    issued = _redeem(connector.client_id, connector.code, now=now)
    token_id = _authorization(cluster, connector.code).token_id
    assert token_id is not None
    end = _grant(cluster, token_id).grant_expires_at
    before = _token(cluster, token_id)
    rows = _refresh_rows(cluster, token_id)
    refused = refresh(
        _surface(),
        _refresh_form(connector.client_id, _str(issued, "refresh_token")),
        now=end,
    )
    assert refused == _invalid("invalid_grant")
    assert _token(cluster, token_id) == before
    assert _refresh_rows(cluster, token_id) == rows
    late = _refreshed(
        connector.client_id,
        _str(issued, "refresh_token"),
        now=end - timedelta(seconds=30),
    )
    assert _token(cluster, token_id).expires_at == end
    assert late["expires_in"] == 30


# --- AC-13: refresh rotation, the grace window and reuse ------------------------------


def test_refresh_rotates_both_values_in_place(
    cluster: ClusterSession, connector: Connector
) -> None:
    now = connector.now
    first = _redeem(connector.client_id, connector.code, now=now)
    token_id = _authorization(cluster, connector.code).token_id
    assert token_id is not None
    old_hash = _token(cluster, token_id).token_hash
    later = now + timedelta(minutes=50)
    second = _refreshed(connector.client_id, _str(first, "refresh_token"), now=later)
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    assert _str(second, "access_token").startswith("rheo_mcp_")
    token = _token(cluster, token_id)
    assert token.token_hash != old_hash
    assert token.expires_at == later + timedelta(minutes=60)
    assert _refresh_rows(cluster, token_id) == [
        (_sha(_str(first, "refresh_token")), later),
        (_sha(_str(second, "refresh_token")), None),
    ]
    # The superseded access value matches no row.
    assert _state(_str(first, "access_token")) == "token_malformed"
    assert isinstance(_resolves(_str(second, "access_token")), ResolvedToken)
    used = _events(cluster, connector.client_id)[-1]
    assert (used.event, used.outcome, used.token_id) == (
        "refresh_used",
        "succeeded",
        token_id,
    )


def test_reuse_inside_the_grace_changes_nothing(
    cluster: ClusterSession, connector: Connector
) -> None:
    now = connector.now
    first = _redeem(connector.client_id, connector.code, now=now)
    token_id = _authorization(cluster, connector.code).token_id
    assert token_id is not None
    rotated_at = now + timedelta(minutes=50)
    second = _refreshed(
        connector.client_id, _str(first, "refresh_token"), now=rotated_at
    )
    token = _token(cluster, token_id)
    rows = _refresh_rows(cluster, token_id)
    events = _event_words(cluster, connector.client_id)
    result = refresh(
        _surface(),
        _refresh_form(connector.client_id, _str(first, "refresh_token")),
        now=rotated_at + timedelta(seconds=9),
    )
    assert result == _invalid("invalid_grant")
    assert _token(cluster, token_id) == token
    assert _refresh_rows(cluster, token_id) == rows
    assert _event_words(cluster, connector.client_id) == events
    # The newest pair still works.
    assert isinstance(_resolves(_str(second, "access_token")), ResolvedToken)
    _refreshed(
        connector.client_id,
        _str(second, "refresh_token"),
        now=rotated_at + timedelta(seconds=20),
    )


def test_reuse_after_the_grace_revokes_the_grant(
    cluster: ClusterSession, connector: Connector
) -> None:
    now = connector.now
    first = _redeem(connector.client_id, connector.code, now=now)
    token_id = _authorization(cluster, connector.code).token_id
    assert token_id is not None
    rotated_at = now + timedelta(minutes=50)
    second = _refreshed(
        connector.client_id, _str(first, "refresh_token"), now=rotated_at
    )
    rows = _refresh_rows(cluster, token_id)
    result = refresh(
        _surface(),
        _refresh_form(connector.client_id, _str(first, "refresh_token")),
        now=rotated_at + timedelta(seconds=10),
    )
    assert result == _invalid("invalid_grant")
    assert _token(cluster, token_id).revoked_at is not None
    assert _refresh_rows(cluster, token_id) == rows
    reuse = _events(cluster, connector.client_id)[-1]
    assert (reuse.event, reuse.outcome) == ("refresh_reuse_revoked", "refused")
    assert reuse.token_id == token_id
    assert reuse.account_id == connector.account_id
    assert reuse.workspace_id == connector.workspace_id
    assert _state(_str(second, "access_token")) == "token_revoked"
    # And the newest refresh token is dead with it.
    assert refresh(
        _surface(),
        _refresh_form(connector.client_id, _str(second, "refresh_token")),
        now=rotated_at + timedelta(seconds=30),
    ) == _invalid("invalid_grant")


def test_unknown_or_missing_refresh_token_is_invalid_grant(
    cluster: ClusterSession, connector: Connector
) -> None:
    for form in (
        _refresh_form(connector.client_id, "not-a-refresh-token"),
        _refresh_form(connector.client_id, "", refresh_token=None),
    ):
        assert refresh(_surface(), form, now=connector.now) == _invalid("invalid_grant")


# --- refresh's validity chain ---------------------------------------------------------


def _redeemed(cluster: ClusterSession, connector: Connector) -> tuple[UUID, str]:
    issued = _redeem(connector.client_id, connector.code, now=connector.now)
    token_id = _authorization(cluster, connector.code).token_id
    assert token_id is not None
    return token_id, _str(issued, "refresh_token")


def _assert_refresh_refused(
    cluster: ClusterSession,
    connector: Connector,
    token_id: UUID,
    form: dict[str, str],
    error: str,
) -> None:
    token = _token(cluster, token_id)
    rows = _refresh_rows(cluster, token_id)
    events = _event_words(cluster, connector.client_id)
    result = refresh(_surface(), form, now=connector.now + timedelta(minutes=5))
    assert result == _invalid(error)
    assert _token(cluster, token_id) == token
    assert _refresh_rows(cluster, token_id) == rows
    assert _event_words(cluster, connector.client_id) == events


def test_refresh_by_another_client_is_invalid_client(
    cluster: ClusterSession, connector: Connector
) -> None:
    token_id, refresh_token = _redeemed(cluster, connector)
    other = _register()
    _assert_refresh_refused(
        cluster,
        connector,
        token_id,
        _refresh_form(other, refresh_token),
        "invalid_client",
    )


def test_refresh_of_a_revoked_grant_is_invalid_grant(
    cluster: ClusterSession, connector: Connector
) -> None:
    token_id, refresh_token = _redeemed(cluster, connector)
    with cluster.backend.control_engine.begin() as connection:
        revoke_access_token(connection, token_id)
    _assert_refresh_refused(
        cluster,
        connector,
        token_id,
        _refresh_form(connector.client_id, refresh_token),
        "invalid_grant",
    )


def test_refresh_after_membership_removed_is_invalid_grant(
    cluster: ClusterSession, workspace: UUID
) -> None:
    member = add_member(cluster.backend, workspace, Role.MEMBER, display_name="m-x")
    now = _now()
    client_id = _register(now=now)
    code = _code(client_id, member, workspace, now=now)
    connector = Connector(client_id, member, workspace, code, now)
    token_id, refresh_token = _redeemed(cluster, connector)
    with cluster.backend.control_engine.begin() as connection:
        connection.execute(
            delete(t.membership).where(
                t.membership.c.account_id == member,
                t.membership.c.workspace_id == workspace,
            )
        )
    _assert_refresh_refused(
        cluster,
        connector,
        token_id,
        _refresh_form(client_id, refresh_token),
        "invalid_grant",
    )


def test_refresh_in_an_archived_workspace_is_invalid_grant(
    cluster: ClusterSession, connector: Connector
) -> None:
    token_id, refresh_token = _redeemed(cluster, connector)
    with cluster.backend.control_engine.begin() as connection:
        set_workspace_state(
            connection,
            connector.workspace_id,
            state=WorkspaceState.UNAVAILABLE,
            state_detail=None,
        )
    _assert_refresh_refused(
        cluster,
        connector,
        token_id,
        _refresh_form(connector.client_id, refresh_token),
        "invalid_grant",
    )


def test_refresh_checks_a_present_resource(
    cluster: ClusterSession, connector: Connector
) -> None:
    token_id, refresh_token = _redeemed(cluster, connector)
    _assert_refresh_refused(
        cluster,
        connector,
        token_id,
        _refresh_form(
            connector.client_id, refresh_token, resource="https://other.example.test/"
        ),
        "invalid_target",
    )
    # The bound value, present, is accepted.
    assert isinstance(
        refresh(
            _surface(),
            _refresh_form(connector.client_id, refresh_token, resource=RESOURCE),
            now=connector.now + timedelta(minutes=5),
        ),
        dict,
    )


def test_a_revoke_between_the_reads_and_the_rotation_writes_nothing(
    cluster: ClusterSession,
    connector: Connector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``rotate_access_token`` refuses ``access_token_revoked`` when a revoke lands
    after the validity read: ``invalid_grant``, no rotated refresh row, no event."""
    token_id, refresh_token = _redeemed(cluster, connector)
    with cluster.backend.control_engine.begin() as connection:
        revoke_access_token(connection, token_id)
    real = service.get_access_token

    def stale_read(conn: Connection, wanted: UUID) -> AccessTokenRow | None:
        row = real(conn, wanted)
        return None if row is None else dataclasses.replace(row, revoked_at=None)

    monkeypatch.setattr(service, "get_access_token", stale_read)
    _assert_refresh_refused(
        cluster,
        connector,
        token_id,
        _refresh_form(connector.client_id, refresh_token),
        "invalid_grant",
    )


# --- AC-14: the code is single-use and burned on every failure ------------------------


def test_replay_revokes_what_the_first_redemption_issued(
    cluster: ClusterSession, connector: Connector
) -> None:
    first = _redeem(connector.client_id, connector.code, now=connector.now)
    token_id = _authorization(cluster, connector.code).token_id
    assert token_id is not None
    replay = exchange_code(
        _surface(),
        _code_form(connector.client_id, connector.code),
        now=connector.now + timedelta(seconds=5),
    )
    assert replay == _invalid("invalid_grant")
    assert _token(cluster, token_id).revoked_at is not None
    assert _event_words(cluster, connector.client_id)[-3:] == [
        ("code_redeemed", "succeeded"),
        ("code_redeemed", "refused"),
        ("grant_revoked", "succeeded"),
    ]
    revoked = _events(cluster, connector.client_id)[-1]
    assert revoked.token_id == token_id
    assert revoked.account_id == connector.account_id
    assert revoked.workspace_id == connector.workspace_id
    assert _state(_str(first, "access_token")) == "token_revoked"
    assert refresh(
        _surface(),
        _refresh_form(connector.client_id, _str(first, "refresh_token")),
        now=connector.now + timedelta(seconds=10),
    ) == _invalid("invalid_grant")
    # A third presentation writes no second grant_revoked.
    exchange_code(
        _surface(),
        _code_form(connector.client_id, connector.code),
        now=connector.now + timedelta(seconds=15),
    )
    words = _event_words(cluster, connector.client_id)
    assert words.count(("grant_revoked", "succeeded")) == 1


@pytest.mark.parametrize(
    ("overrides", "delay", "error"),
    [
        ({"code_verifier": "x" * 43}, 0, "invalid_grant"),
        ({"code_verifier": None}, 0, "invalid_grant"),
        ({"code_verifier": VERIFIER[:42]}, 0, "invalid_grant"),
        ({"client_id": "another-client"}, 0, "invalid_client"),
        ({"redirect_uri": "https://claude.ai/api/mcp/other"}, 0, "invalid_grant"),
        ({"redirect_uri": None}, 0, "invalid_grant"),
        ({}, 61, "invalid_grant"),
        ({}, 60, "invalid_grant"),
        ({"resource": "https://other.example.test/"}, 0, "invalid_target"),
        ({"resource": "https://mcp.example.test/mcp"}, 0, "invalid_target"),
    ],
)
def test_a_failed_redemption_burns_the_code_and_issues_nothing(
    cluster: ClusterSession,
    connector: Connector,
    overrides: dict[str, str | None],
    delay: int,
    error: str,
) -> None:
    at = connector.now + timedelta(seconds=delay)
    result = exchange_code(
        _surface(), _code_form(connector.client_id, connector.code, **overrides), now=at
    )
    assert result == _invalid(error)
    authorization = _authorization(cluster, connector.code)
    assert authorization.code_used_at == at
    assert authorization.token_id is None
    assert _issued(cluster, connector.account_id, connector.client_id) == 0
    # A following correct redemption is a replay of a burned code.
    again = exchange_code(
        _surface(),
        _code_form(connector.client_id, connector.code),
        now=connector.now + timedelta(seconds=1),
    )
    assert again == _invalid("invalid_grant")
    assert _authorization(cluster, connector.code).code_used_at == at
    assert _issued(cluster, connector.account_id, connector.client_id) == 0
    assert _event_words(cluster, connector.client_id)[-1] == (
        "code_redeemed",
        "refused",
    )
    assert ("grant_revoked", "succeeded") not in _event_words(
        cluster, connector.client_id
    )


def test_unknown_or_missing_code_is_invalid_grant(
    cluster: ClusterSession, connector: Connector
) -> None:
    events = _event_words(cluster, connector.client_id)
    for form in (
        _code_form(connector.client_id, "not-a-code"),
        _code_form(connector.client_id, "", code=None),
    ):
        assert exchange_code(_surface(), form, now=connector.now) == _invalid(
            "invalid_grant"
        )
    assert _authorization(cluster, connector.code).code_used_at is None
    assert _event_words(cluster, connector.client_id) == events


@pytest.mark.parametrize("resource", [None, RESOURCE, ISSUER])
def test_absent_or_bound_resource_redeems(
    cluster: ClusterSession, connector: Connector, resource: str | None
) -> None:
    """Decision 8: absent is the value bound at /authorize; the canonical string
    and, in subdomain mode, its empty-path spelling are accepted."""
    issued = exchange_code(
        _surface(),
        _code_form(connector.client_id, connector.code, resource=resource),
        now=connector.now,
    )
    assert isinstance(issued, dict), issued


@pytest.mark.parametrize("state", ["membership_missing", "set_empty"])
def test_an_issuance_refusal_after_the_burn_commits_the_burn(
    cluster: ClusterSession,
    workspace: UUID,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """``issue_connector_token``'s ``OperationRefused`` is caught inside the step's
    transaction and returned, so the burn commits and nothing is issued."""
    member = add_member(cluster.backend, workspace, Role.MEMBER, display_name="m-y")
    now = _now()
    client_id = _register(now=now)
    code = _code(client_id, member, workspace, now=now)
    if state == "membership_missing":
        with cluster.backend.control_engine.begin() as connection:
            connection.execute(
                delete(t.membership).where(
                    t.membership.c.account_id == member,
                    t.membership.c.workspace_id == workspace,
                )
            )
    else:
        monkeypatch.setattr(issue_module, "agent_default", lambda: frozenset())
    result = exchange_code(_surface(), _code_form(client_id, code), now=now)
    assert result == _invalid("invalid_grant")
    authorization = _authorization(cluster, code)
    assert authorization.code_used_at == now
    assert authorization.token_id is None
    assert _issued(cluster, member, client_id) == 0
    assert exchange_code(_surface(), _code_form(client_id, code), now=now) == (
        _invalid("invalid_grant")
    )


# --- edge case 14: expiry boundaries --------------------------------------------------


def test_code_at_59_seconds_redeems(
    cluster: ClusterSession, connector: Connector
) -> None:
    _redeem(
        connector.client_id, connector.code, now=connector.now + timedelta(seconds=59)
    )


@pytest.mark.parametrize(("offset", "state"), [(1, None), (-1, "token_expired")])
def test_access_token_one_second_either_side_of_expiry(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    offset: int,
    state: str | None,
) -> None:
    """Redeemed so the access value expires ``offset`` seconds from the wall clock
    ``resolve_token`` reads."""
    base = _now() - timedelta(minutes=60) + timedelta(seconds=offset)
    client_id = _register(now=base)
    code = _code(client_id, owner_account_id, workspace, now=base)
    issued = _redeem(client_id, code, now=base)
    assert _state(_str(issued, "access_token")) == state


def test_an_idle_connector_survives_cleanup_and_refreshes(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """A grant whose access value expired three hours ago is not abandoned: a
    registration's cleanup keeps its client, and its refresh succeeds."""
    base = _now() - timedelta(hours=4)
    client_id = _register(now=base)
    code = _code(client_id, owner_account_id, workspace, now=base)
    issued = _redeem(client_id, code, now=base)
    assert _state(_str(issued, "access_token")) == "token_expired"
    _register(abandoned_client_minutes=15)
    with cluster.backend.control_engine.connect() as connection:
        assert oauth_store.get_client(connection, client_id) is not None
    renewed = _refreshed(client_id, _str(issued, "refresh_token"), now=_now())
    assert isinstance(_resolves(_str(renewed, "access_token")), ResolvedToken)


# --- AC-20, service half: the seven events -------------------------------------------


def _operator_ctx(workspace_id: UUID) -> WorkspaceContext:
    ctx = context_for_operator(workspace_id)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def test_a_scripted_flow_writes_each_event_once(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    now = _now()
    client_id = _register(now=now)
    code = _code(client_id, owner_account_id, workspace, now=now)
    first = _redeem(client_id, code, now=now)
    token_id = _authorization(cluster, code).token_id
    assert token_id is not None
    _refreshed(client_id, _str(first, "refresh_token"), now=now + timedelta(minutes=1))
    nonce = begin_authorization(
        _surface(),
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        },
        now=now,
    )
    assert isinstance(nonce, str), nonce
    denied = deny(_authorization_by_request(nonce).id, nonce, owner_account_id, now=now)
    assert not isinstance(denied, OAuthError), denied
    revoked = dispatch(
        _operator_ctx(workspace), TOKEN_REVOKE, {"token_id": str(token_id)}
    )
    assert revoked.ok, revoked
    reused = refresh(
        _surface(),
        _refresh_form(client_id, _str(first, "refresh_token")),
        now=now + timedelta(minutes=2),
    )
    assert reused == _invalid("invalid_grant")

    events = {row.event: row for row in _events(cluster, client_id)}
    assert sorted(row.event for row in _events(cluster, client_id)) == sorted(events)
    assert set(events) == {
        "client_registered",
        "authorization_granted",
        "authorization_denied",
        "code_redeemed",
        "refresh_used",
        "refresh_reuse_revoked",
        "grant_revoked",
    }
    outcomes = {name: row.outcome for name, row in events.items()}
    assert outcomes == {
        "client_registered": "succeeded",
        "authorization_granted": "succeeded",
        "authorization_denied": "refused",
        "code_redeemed": "succeeded",
        "refresh_used": "succeeded",
        "refresh_reuse_revoked": "refused",
        "grant_revoked": "succeeded",
    }
    for name in (
        "code_redeemed",
        "refresh_used",
        "refresh_reuse_revoked",
        "grant_revoked",
    ):
        row = events[name]
        assert (row.account_id, row.workspace_id, row.token_id) == (
            owner_account_id,
            workspace,
            token_id,
        ), name
    granted = events["authorization_granted"]
    assert (granted.account_id, granted.workspace_id) == (owner_account_id, workspace)
    assert events["authorization_denied"].account_id == owner_account_id

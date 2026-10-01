"""``core.oauth_event.list``, the read of the ``oauth_event`` trail (issue #287,
AC-25's operation half, FR 20).

Seams under test: ``dispatch()`` of ``core.oauth_event.list``: the role gate, the
newest-first order with its id tie-break, the eight-field record, and the rule that
only the operator sees the rows bound to no workspace.

The events are written by a scripted flow through the service's own functions
(``register_client``, ``begin_authorization``, ``approve``, ``deny``,
``exchange_code``, ``refresh``), never raw SQL, and read only through the operation.
"""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.registry import add_member, register_harness
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.factories import context_from_session
from rheo_core.oauth.operations import OAUTH_EVENT_LIST, OAuthEventList
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
from rheo_core.operations import (
    INPUT_INVALID,
    ROLE_NOT_PERMITTED,
    dispatch,
    register_core_operations,
)
from rheo_core.refs import uuid7
from rheo_core.sessions import create_session, mint_host_secret, switch_workspace
from rheo_core.storage import oauth_store
from rheo_core.storage.control_plane import insert_account
from rheo_core.storage.postgres import get_backend
from rheo_core.tokens.sets import register_core_tools

pytestmark = pytest.mark.postgres

HOST = "example.test"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "https://mcp.example.test/"
VERIFIER = "dBjftJeZ4CVP-mJ92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .rstrip(b"=")
    .decode()
)
CLIENT_NAME = "Example connector"

RECORD_FIELDS = frozenset(
    {
        "event_id",
        "occurred_at",
        "event",
        "outcome",
        "account_id",
        "workspace_id",
        "client_id",
        "token_id",
    }
)
"""The eight published fields, spelled out so a widened record goes red."""


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_core_tools()
    register_harness()


def _surface() -> OAuthSurface:
    return OAuthSurface(
        resource=RESOURCE,
        issuer="https://mcp.example.test",
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
        lifetimes=OAuthLifetimes(
            access_token_minutes=60,
            grant_days=30,
            refresh_grace_seconds=10,
            max_clients=100_000,
            registrations_per_source_per_hour=30,
            abandoned_client_minutes=60,
        ),
        accepts_empty_path=True,
    )


def _begin(client_id: str, *, now: datetime) -> tuple[str, UUID]:
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
    digest = hashlib.sha256(nonce.encode()).digest()
    with get_backend().control_engine.connect() as connection:
        row = oauth_store.get_authorization_by_request_hash(connection, digest)
    assert row is not None
    return nonce, row.id


def _str(issued: object, key: str) -> str:
    assert isinstance(issued, dict), issued
    value = issued[key]
    assert isinstance(value, str)
    return value


def _flow(account_id: UUID, workspace_id: UUID) -> str:
    """Registration, grant, code redemption, refresh, refresh-reuse revocation and a
    deny, one client, at strictly later times except the reuse pair, which shares a
    timestamp and so is ordered by id alone. Returns the client id."""
    t0 = datetime.now(UTC)
    registered = register_client(
        _surface(),
        metadata={"redirect_uris": [CALLBACK], "client_name": CLIENT_NAME},
        source=f"source-{uuid7().hex}",
        now=t0,
    )
    client_id = _str(registered, "client_id")

    granted_at = t0 + timedelta(seconds=1)
    nonce, row_id = _begin(client_id, now=granted_at)
    approval = approve(row_id, nonce, account_id, workspace_id, now=granted_at)
    assert isinstance(approval, Approval), approval

    first = exchange_code(
        _surface(),
        {
            "grant_type": "authorization_code",
            "code": approval.code,
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_verifier": VERIFIER,
        },
        now=t0 + timedelta(seconds=2),
    )
    old_refresh = _str(first, "refresh_token")
    renewed = refresh(
        _surface(),
        {
            "grant_type": "refresh_token",
            "refresh_token": old_refresh,
            "client_id": client_id,
        },
        now=t0 + timedelta(seconds=3),
    )
    assert isinstance(renewed, dict), renewed
    reused = refresh(
        _surface(),
        {
            "grant_type": "refresh_token",
            "refresh_token": old_refresh,
            "client_id": client_id,
        },
        now=t0 + timedelta(seconds=30),
    )
    assert isinstance(reused, OAuthError), reused

    denied_at = t0 + timedelta(seconds=40)
    nonce, row_id = _begin(client_id, now=denied_at)
    denial = deny(row_id, nonce, account_id, now=denied_at)
    assert not isinstance(denial, OAuthError), denial
    return client_id


def _operator(workspace_id: UUID) -> WorkspaceContext:
    ctx = context_for_operator(workspace_id)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _owner_session(workspace_id: UUID, account_id: UUID) -> WorkspaceContext:
    session_row = create_session(account_id)
    secret = mint_host_secret(session_row.id, HOST)
    assert switch_workspace(session_row.id, workspace_id) is None
    ctx = context_from_session(secret, HOST)
    assert isinstance(ctx, WorkspaceContext), ctx
    assert ctx.role is Role.OWNER
    return ctx


def _listing(ctx: WorkspaceContext, payload: dict[str, object]) -> OAuthEventList:
    outcome = dispatch(ctx, OAUTH_EVENT_LIST, payload)
    assert outcome.ok, outcome
    assert isinstance(outcome.result, OAuthEventList)
    return outcome.result


def _assert_newest_first(listing: OAuthEventList) -> None:
    keys = [(event.occurred_at, event.event_id) for event in listing.events]
    assert keys == sorted(keys, reverse=True)


def test_the_operator_reads_the_flow_newest_first_with_the_unbound_rows(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    client_id = _flow(owner_account_id, workspace)

    listing = _listing(_operator(workspace), {})

    _assert_newest_first(listing)
    mine = [event for event in listing.events if event.client_id == client_id]
    assert [(event.event, event.outcome) for event in mine] == [
        ("authorization_denied", "refused"),
        # Same occurred_at as the reuse row; written after it, so a higher uuid7.
        ("grant_revoked", "succeeded"),
        ("refresh_reuse_revoked", "refused"),
        ("refresh_used", "succeeded"),
        ("code_redeemed", "succeeded"),
        ("authorization_granted", "succeeded"),
        ("client_registered", "succeeded"),
    ]
    assert mine[1].occurred_at == mine[2].occurred_at
    assert mine[1].event_id > mine[2].event_id
    # The registration and the deny are bound to no workspace, and the operator
    # alone is shown them.
    unbound = {event.event for event in mine if event.workspace_id is None}
    assert unbound == {"client_registered", "authorization_denied"}
    assert {event.workspace_id for event in listing.events} <= {workspace, None}


def test_each_record_carries_exactly_the_eight_content_free_fields(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    client_id = _flow(owner_account_id, workspace)

    listing = _listing(_operator(workspace), {})

    mine = [event for event in listing.events if event.client_id == client_id]
    assert len(mine) == 7
    for event in mine:
        assert set(event.model_dump()) == RECORD_FIELDS
    dumped = listing.model_dump_json()
    assert CLIENT_NAME not in dumped
    assert CALLBACK not in dumped
    assert VERIFIER not in dumped
    assert CHALLENGE not in dumped


def test_an_owner_session_sees_its_workspace_and_no_unbound_row(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    client_id = _flow(owner_account_id, workspace)

    listing = _listing(_owner_session(workspace, owner_account_id), {})

    _assert_newest_first(listing)
    assert listing.events
    assert all(event.workspace_id == workspace for event in listing.events)
    assert [event.event for event in listing.events] == [
        "grant_revoked",
        "refresh_reuse_revoked",
        "refresh_used",
        "code_redeemed",
        "authorization_granted",
    ]
    assert {event.client_id for event in listing.events} == {client_id}


def test_a_member_is_refused_role_not_permitted(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    _flow(owner_account_id, workspace)
    member_id = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="member-two"
    )
    ctx = context_for_harness(workspace, member_id, Role.MEMBER)
    assert isinstance(ctx, WorkspaceContext)

    outcome = dispatch(ctx, OAUTH_EVENT_LIST, {})

    assert not outcome.ok
    assert outcome.state == ROLE_NOT_PERMITTED
    assert outcome.result is None


def test_another_workspace_s_rows_are_never_shown(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    make_workspace: MakeWorkspace,
) -> None:
    """Two workspaces, two accounts, one flow each: neither the owner nor the
    operator of the first is shown a row bound to the second. Pins the scope
    predicate on purpose: with one seeded workspace, ``workspace_id IS NOT NULL``
    goes red only if some other test happened to leave bound rows behind."""
    with cluster.backend.control_engine.begin() as connection:
        other_owner = insert_account(connection, display_name="owner-two").id
    other_workspace = make_workspace(owner=other_owner)
    mine = _flow(owner_account_id, workspace)
    theirs = _flow(other_owner, other_workspace)

    owner_view = _listing(_owner_session(workspace, owner_account_id), {"limit": 500})
    assert mine in {event.client_id for event in owner_view.events}
    assert theirs not in {event.client_id for event in owner_view.events}
    assert other_owner not in {event.account_id for event in owner_view.events}
    assert other_workspace not in {event.workspace_id for event in owner_view.events}

    # The operator is shown the second flow's two unbound rows (its registration
    # and its deny name no workspace), and none of its five bound ones.
    operator_view = _listing(_operator(workspace), {"limit": 500})
    assert mine in {event.client_id for event in operator_view.events}
    assert other_workspace not in {event.workspace_id for event in operator_view.events}
    theirs_shown = [e for e in operator_view.events if e.client_id == theirs]
    assert sorted(event.event for event in theirs_shown) == [
        "authorization_denied",
        "client_registered",
    ]
    assert all(event.workspace_id is None for event in theirs_shown)


@pytest.mark.parametrize("limit", [0, 501])
def test_a_limit_outside_one_to_five_hundred_is_input_invalid(
    cluster: ClusterSession, workspace: UUID, limit: int
) -> None:
    outcome = dispatch(_operator(workspace), OAUTH_EVENT_LIST, {"limit": limit})

    assert outcome.state == INPUT_INVALID, outcome
    assert outcome.error is not None
    assert "limit" in outcome.error.error_text


def test_limit_two_returns_the_two_newest(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    _flow(owner_account_id, workspace)
    ctx = _operator(workspace)

    everything = _listing(ctx, {"limit": 500})
    two = _listing(ctx, {"limit": 2})

    assert len(two.events) == 2
    assert two.events == everything.events[:2]

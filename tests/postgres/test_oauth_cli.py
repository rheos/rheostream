"""The MCP OAuth connector's operator tooling (issue #287): AC-18, AC-19 and AC-25's
CLI half.

Seams under test: ``rheo token list``, ``rheo token revoke``, ``rheo token events``
and ``rheo doctor`` through ``rheo_app_cli.main.main`` (stdout, stderr, exit code),
plus doctor's two OAuth checks on their own (``_check_oauth_surface``,
``_check_connector_grants``) where the shared control plane's other rows would
otherwise decide the level. Grants are made by the real HTTP flow
(``harness.oauth_client``), except the ones doctor must see in a shape the flow never
produces (revoked or expired history, a live grant with no refresh row), which are
written straight into the control plane.

"grant id" in AC-18/20/25 is the grant's access-token id, ``token_id``.

The control plane is shared by the session, so counts are asserted as deltas from a
baseline read in the same test, and a grant written without a refresh row is revoked
in teardown so later doctor runs do not inherit it.
"""

import hashlib
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.identity import FIXED_PROVIDER_ID, FixedIdentityProvider
from harness.oauth_client import CLAUDE_CALLBACK, OAuthTestClient, oauth_client
from harness.registry import register_harness
from rheo_app_cli.commands.doctor import (
    _check_connector_grants,
    _check_oauth_surface,
)
from rheo_app_cli.main import main
from rheo_app_core import auth_routes
from rheo_app_core.main import app
from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.identity.boundary import ProviderIdentity
from rheo_core.oauth import OAuthSurface, oauth_surface
from rheo_core.oauth.operations import OAUTH_EVENT_LIST
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.refs import uuid7
from rheo_core.routing import RoutingConfig
from rheo_core.settings import resolve
from rheo_core.storage import control_tables
from rheo_core.storage import oauth_tables as o
from rheo_core.storage.control_plane import insert_account, insert_identity
from rheo_core.storage.oauth_store import (
    count_counted_clients,
    insert_client,
    insert_grant,
    insert_refresh_token,
)
from rheo_core.tokens.issue import issue_connector_token
from rheo_core.tokens.sets import register_core_tools
from sqlalchemy import select, update

pytestmark = pytest.mark.postgres

BASE_HOST = "example.test"
LIST_COLUMNS = 12
EVENT_COLUMNS = 7


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_core_tools()
    register_harness()


@pytest.fixture
def address() -> str:
    return f"client-{uuid7().hex}"


def _set_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RHEO__routing__mode", "subdomain")
    monkeypatch.setenv("RHEO__routing__scheme", "https")
    monkeypatch.setenv("RHEO__routing__base_host", BASE_HOST)
    monkeypatch.setenv("RHEO__routing__public_host", "")


def _enable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RHEO__identity__providers__github__enabled", "true")
    monkeypatch.setenv("RHEO__identity__providers__github__client_id", "example-client")


@pytest.fixture
def surface(monkeypatch: pytest.MonkeyPatch) -> OAuthSurface:
    _set_mode(monkeypatch)
    _enable_provider(monkeypatch)
    monkeypatch.setenv("RHEO__identity__oauth__enabled", "true")
    monkeypatch.setenv("RHEO__identity__oauth__max_clients", "1000000")
    resolved = resolve()
    found = oauth_surface(resolved, RoutingConfig.from_settings(resolved))
    assert isinstance(found, OAuthSurface), found
    return found


@pytest.fixture(autouse=True)
def _no_provider_override() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(auth_routes.resolve_identity_provider, None)


@dataclass(frozen=True)
class Member:
    account_id: UUID
    identity: ProviderIdentity
    workspace_id: UUID


@pytest.fixture
def member(cluster: ClusterSession, make_workspace: MakeWorkspace) -> Member:
    subject = f"member-{uuid7().hex[:12]}"
    identity = ProviderIdentity(
        provider_id=FIXED_PROVIDER_ID,
        subject=subject,
        email=f"{subject}@example.test",
        email_verified=True,
        display_name=subject,
    )
    with cluster.backend.control_engine.begin() as connection:
        account_id = insert_account(connection, display_name=subject).id
        insert_identity(
            connection,
            account_id=account_id,
            provider_id=identity.provider_id,
            provider_subject=identity.subject,
            email=identity.email,
            email_verified=identity.email_verified,
        )
    return Member(account_id, identity, make_workspace(owner=account_id))


def _sign_in_as(member: Member) -> None:
    app.dependency_overrides[auth_routes.resolve_identity_provider] = lambda: (
        FixedIdentityProvider(member.identity)
    )


@dataclass(frozen=True)
class Grant:
    client_id: str
    client_name: str
    token_id: UUID
    access: str
    refresh: str


def _token_id_of(cluster: ClusterSession, client_id: str) -> UUID:
    with cluster.backend.control_engine.connect() as connection:
        (token_id,) = connection.execute(
            select(o.oauth_grant.c.token_id).where(
                o.oauth_grant.c.client_id == client_id
            )
        ).scalars()
    assert isinstance(token_id, UUID)
    return token_id


async def _connect(
    client: OAuthTestClient, cluster: ClusterSession, client_name: str
) -> Grant:
    client_id, token = await client.connect(client_name=client_name)
    assert token.status_code == 200, token.text
    body = token.json()
    return Grant(
        client_id=client_id,
        client_name=client_name,
        token_id=_token_id_of(cluster, client_id),
        access=body["access_token"],
        refresh=body["refresh_token"],
    )


Run = tuple[int, str, str]


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> Run:
    capsys.readouterr()
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _listed(out: str) -> dict[str, list[str]]:
    rows = [line.split("\t") for line in out.splitlines()]
    assert all(len(row) == LIST_COLUMNS for row in rows), rows
    return {row[0]: row for row in rows}


def _hashes(cluster: ClusterSession, account_id: UUID) -> list[bytes]:
    with cluster.backend.control_engine.connect() as connection:
        return [
            bytes(value)
            for value in connection.execute(
                select(control_tables.access_token.c.token_hash).where(
                    control_tables.access_token.c.account_id == account_id
                )
            ).scalars()
        ]


def _assert_no_secret(text: str, secrets_seen: list[str], *extra: str) -> None:
    for value in [*secrets_seen, *extra]:
        assert value and value not in text, value


# --- AC-18: rheo token list and revoke ------------------------------------------------


async def test_list_shows_each_connector_grant_with_its_fields_and_no_secret(
    surface: OAuthSurface,
    address: str,
    member: Member,
    cluster: ClusterSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _sign_in_as(member)
    noisy_name = "Example\x07 phone​ connector " + "x" * 80
    async with oauth_client(surface, client_address=address) as client:
        phone = await _connect(client, cluster, noisy_name)
        desktop = await _connect(client, cluster, "Example desktop connector")
        secrets_seen = client.issued.all()

    code, out, err = _run(capsys, "token", "list", "--account", str(member.account_id))
    assert code == 0, err
    listed = _listed(out)
    assert set(listed) == {str(phone.token_id), str(desktop.token_id)}

    for grant in (phone, desktop):
        row = listed[str(grant.token_id)]
        (
            _id,
            kind,
            issued_from,
            set_name,
            account,
            workspace,
            created,
            expires,
            last_used,
            revoked,
            name,
            grant_end,
        ) = row
        assert (kind, issued_from) == ("mcp", "connector")
        assert account == str(member.account_id)
        assert workspace == str(member.workspace_id)
        assert revoked == "-"
        assert set_name, row
        for stamp in (created, expires, grant_end):
            datetime.fromisoformat(stamp)
        assert datetime.fromisoformat(grant_end) > datetime.fromisoformat(expires)
        assert last_used == "-" or datetime.fromisoformat(last_used)
    assert listed[str(desktop.token_id)][10] == "Example desktop connector"
    shown = listed[str(phone.token_id)][10]
    assert shown == ("Example phone connector " + "x" * 80)[:60]
    assert "\x07" not in out and "​" not in out

    # No token value, refresh value, code or hash, in any encoding the row has.
    _assert_no_secret(out + err, secrets_seen)
    for digest in _hashes(cluster, member.account_id):
        assert digest.hex() not in out + err
        assert str(digest) not in out + err


async def test_list_narrows_by_workspace_and_shows_dashes_for_an_operator_token(
    member: Member,
    cluster: ClusterSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, value, err = _run(
        capsys,
        "token",
        "issue",
        "--account",
        str(member.account_id),
        "--workspace",
        str(member.workspace_id),
        "--set",
        "read_only",
        "--kind",
        "cli",
    )
    assert code == 0, err
    code, out, err = _run(
        capsys, "token", "list", "--workspace", str(member.workspace_id)
    )
    assert code == 0, err
    (row,) = _listed(out).values()
    assert row[1:3] == ["cli", "operator"]
    assert row[10:] == ["-", "-"]
    assert value.strip() not in out


async def test_revoking_one_of_two_grants_kills_it_and_leaves_the_other(
    surface: OAuthSurface,
    address: str,
    member: Member,
    cluster: ClusterSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _sign_in_as(member)
    async with oauth_client(surface, client_address=address) as client:
        revoked = await _connect(client, cluster, "Example phone connector")
        kept = await _connect(client, cluster, "Example desktop connector")

    code, out, err = _run(capsys, "token", "revoke", str(revoked.token_id))
    assert code == 0, err
    assert out == ""

    async with oauth_client(surface, client_address=address) as client:
        dead_call = await client.mcp_call(revoked.access)
        dead_refresh = await client.refresh(revoked.refresh, revoked.client_id)
        live_call = await client.mcp_call(kept.access)
        live_refresh = await client.refresh(kept.refresh, kept.client_id)

    assert dead_call.status_code == 401, dead_call.text
    assert (
        dead_call.headers["www-authenticate"]
        == f'Bearer resource_metadata="{surface.protected_resource_metadata_url}"'
    )
    assert dead_refresh.status_code == 400, dead_refresh.text
    assert dead_refresh.json()["error"] == "invalid_grant"
    assert live_call.status_code == 200, live_call.text
    assert live_refresh.status_code == 200, live_refresh.text

    code, out, err = _run(capsys, "token", "list", "--account", str(member.account_id))
    listed = _listed(out)
    assert listed[str(revoked.token_id)][9] != "-"
    assert listed[str(kept.token_id)][9] == "-"


async def test_a_revoked_grant_still_lists_with_its_name_after_cleanup(
    surface: OAuthSurface,
    address: str,
    member: Member,
    cluster: ClusterSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _sign_in_as(member)
    abandoned_id = f"abandoned-{uuid7().hex}"
    with cluster.backend.control_engine.begin() as connection:
        insert_client(
            connection,
            client_id=abandoned_id,
            client_name="Example abandoned connector",
            registered_from=address,
            created_at=datetime.now(UTC) - timedelta(days=1),
            redirect_uris=[CLAUDE_CALLBACK],
        )
    async with oauth_client(surface, client_address=address) as client:
        grant = await _connect(client, cluster, "Example retired connector")
        assert _run(capsys, "token", "revoke", str(grant.token_id))[0] == 0
        # A further registration runs cleanup first.
        again = await client.register({"redirect_uris": [CLAUDE_CALLBACK]})
    assert again.status_code == 201, again.text

    with cluster.backend.control_engine.connect() as connection:
        remaining = set(
            connection.execute(
                select(o.oauth_client.c.client_id).where(
                    o.oauth_client.c.client_id.in_([abandoned_id, grant.client_id])
                )
            ).scalars()
        )
    assert remaining == {grant.client_id}, "cleanup ran and spared the granted client"

    code, out, err = _run(capsys, "token", "list", "--account", str(member.account_id))
    assert code == 0, err
    row = _listed(out)[str(grant.token_id)]
    assert row[9] != "-"
    assert row[10] == "Example retired connector"


# --- AC-25 (CLI half): rheo token events ----------------------------------------------


async def test_events_prints_seven_columns_newest_first_and_no_secret(
    surface: OAuthSurface,
    address: str,
    member: Member,
    cluster: ClusterSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _sign_in_as(member)
    client_name = f"Example events connector {uuid7().hex[:8]}"
    async with oauth_client(surface, client_address=address) as client:
        grant = await _connect(client, cluster, client_name)
        assert (await client.mcp_call(grant.access)).status_code == 200
        refreshed = await client.refresh(grant.refresh, grant.client_id)
        assert refreshed.status_code == 200, refreshed.text
        secrets_seen = client.issued.all()

    code, out, err = _run(
        capsys,
        "token",
        "events",
        "--workspace",
        str(member.workspace_id),
        "--limit",
        "500",
    )
    assert code == 0, err
    rows = [line.split("\t") for line in out.splitlines()]
    assert rows and all(len(row) == EVENT_COLUMNS for row in rows), rows
    stamps = [datetime.fromisoformat(row[0]) for row in rows]
    assert stamps == sorted(stamps, reverse=True)

    ours = [row for row in rows if row[5] == grant.client_id]
    assert [row[1] for row in ours] == [
        "refresh_used",
        "code_redeemed",
        "authorization_granted",
        "client_registered",
    ]
    for row in ours:
        assert row[2] == "succeeded"
    for row in ours[:3]:
        assert row[3:5] == [str(member.account_id), str(member.workspace_id)]
    for row in ours[:2]:
        assert row[6] == str(grant.token_id)
    registered = ours[3]
    assert registered[3] == registered[4] == registered[6] == "-"

    # The listing's rows are the operation's, in its order.
    ctx = context_for_operator(member.workspace_id)
    assert isinstance(ctx, WorkspaceContext)
    outcome = dispatch(ctx, OAUTH_EVENT_LIST, {"limit": 500})
    assert outcome.ok and outcome.result is not None
    assert [row[0] for row in rows] == [
        record.occurred_at.isoformat()
        for record in outcome.result.events  # type: ignore[attr-defined]
    ]

    _assert_no_secret(out + err, secrets_seen, client_name, CLAUDE_CALLBACK)


async def test_a_refused_events_dispatch_exits_one_with_the_state(
    member: Member, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = context_for_operator(member.workspace_id)
    assert isinstance(ctx, WorkspaceContext)
    expected = dispatch(ctx, OAUTH_EVENT_LIST, {"limit": 0}).state

    code, out, err = _run(
        capsys,
        "token",
        "events",
        "--workspace",
        str(member.workspace_id),
        "--limit",
        "0",
    )
    assert code == 1
    assert out == ""
    assert err.startswith(f"{expected}:") or err.strip() == expected, err

    code, out, err = _run(capsys, "token", "events", "--workspace", str(uuid7()))
    assert code == 1
    assert out == "" and err.strip()


# --- AC-19: rheo doctor ---------------------------------------------------------------


def test_the_oauth_surface_line_is_ok_disabled_ok_configured_or_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch)
    off = _check_oauth_surface()
    assert off.line() == "ok   oauth surface: disabled"

    monkeypatch.setenv("RHEO__identity__oauth__enabled", "true")
    monkeypatch.setenv("RHEO__identity__providers__github__enabled", "false")
    incomplete = _check_oauth_surface()
    assert incomplete.level == "FAIL"
    assert "identity_provider_disabled" in incomplete.detail

    # An unsafe pointer target (a plain-http, non-loopback redirect URI).
    monkeypatch.setenv(
        "RHEO__identity__oauth__redirect_uris", "http://claude.example.com/cb"
    )
    _enable_provider(monkeypatch)
    unsafe = _check_oauth_surface()
    assert unsafe.line() == (
        "FAIL oauth surface: enabled but unconfigured: setting_invalid"
    )

    monkeypatch.delenv("RHEO__identity__oauth__redirect_uris")
    on = _check_oauth_surface()
    assert on.level == "ok", on
    assert on.detail == (
        f"configured: issuer https://mcp.{BASE_HOST}; 1 allowed redirect URIs"
    )


def _counted(cluster: ClusterSession, *, minutes: int = 60) -> int:
    now = datetime.now(UTC)
    with cluster.backend.control_engine.connect() as connection:
        return count_counted_clients(
            connection, now=now, abandoned_before=now - timedelta(minutes=minutes)
        )


def _grant_row(
    cluster: ClusterSession,
    member: Member,
    *,
    grant_expires_at: datetime,
    revoked: bool,
    refresh: bool = True,
) -> UUID:
    """A client and a grant written straight into the control plane."""
    now = datetime.now(UTC)
    client_id = f"direct-{uuid7().hex}"
    with cluster.backend.control_engine.begin() as connection:
        insert_client(
            connection,
            client_id=client_id,
            client_name="Example direct connector",
            registered_from="192.0.2.9",
            created_at=now - timedelta(days=2),
            redirect_uris=[CLAUDE_CALLBACK],
        )
        token_id, _, _ = issue_connector_token(
            connection,
            account_id=member.account_id,
            workspace_id=member.workspace_id,
            expires_at=now + timedelta(minutes=5),
        )
        insert_grant(
            connection,
            token_id=token_id,
            client_id=client_id,
            created_at=now - timedelta(days=2),
            grant_expires_at=grant_expires_at,
        )
        if refresh:
            insert_refresh_token(
                connection,
                token_hash=hashlib.sha256(secrets.token_bytes(32)).digest(),
                token_id=token_id,
                created_at=now,
            )
        if revoked:
            _revoke(connection, token_id)
    return token_id


def _revoke(connection: Any, token_id: UUID) -> None:
    connection.execute(
        update(control_tables.access_token)
        .where(control_tables.access_token.c.id == token_id)
        .values(revoked_at=datetime.now(UTC))
    )


def test_connector_grants_fail_at_the_counted_client_cap(
    cluster: ClusterSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    with cluster.backend.control_engine.begin() as connection:
        insert_client(
            connection,
            client_id=f"pending-{uuid7().hex}",
            client_name="Example pending connector",
            registered_from="192.0.2.8",
            created_at=datetime.now(UTC),
            redirect_uris=[CLAUDE_CALLBACK],
        )
    counted = _counted(cluster)
    assert counted >= 1

    monkeypatch.setenv("RHEO__identity__oauth__max_clients", str(counted))
    at_cap = _check_connector_grants(cluster.backend)
    assert at_cap.level == "FAIL", at_cap
    assert at_cap.detail.startswith(f"clients {counted}/{counted};")
    assert "client cap reached" in at_cap.detail

    monkeypatch.setenv("RHEO__identity__oauth__max_clients", str(counted + 1))
    below = _check_connector_grants(cluster.backend)
    assert below.level == "ok", below
    assert below.detail.startswith(f"clients {counted}/{counted + 1};")


def test_revoked_or_expired_grants_keep_their_clients_below_the_cap(
    cluster: ClusterSession, member: Member, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    with cluster.backend.control_engine.begin() as connection:
        insert_client(
            connection,
            client_id=f"pending-{uuid7().hex}",
            client_name="Example pending connector",
            registered_from="192.0.2.8",
            created_at=now,
            redirect_uris=[CLAUDE_CALLBACK],
        )
    baseline = _counted(cluster)
    monkeypatch.setenv("RHEO__identity__oauth__max_clients", str(baseline + 1))
    before = _check_connector_grants(cluster.backend)

    for _ in range(3):
        _grant_row(
            cluster, member, grant_expires_at=now + timedelta(days=20), revoked=True
        )
    for _ in range(3):
        _grant_row(
            cluster, member, grant_expires_at=now - timedelta(days=1), revoked=False
        )

    assert _counted(cluster) == baseline
    after = _check_connector_grants(cluster.backend)
    assert after.level == "ok", after
    assert after.detail.startswith(f"clients {baseline}/{baseline + 1};")

    def figure(detail: str, label: str) -> int:
        return int(detail.split(f"{label} ", 1)[1].split(",")[0].split(";")[0])

    assert figure(after.detail, "revoked") == figure(before.detail, "revoked") + 3
    assert figure(after.detail, "expired") == figure(before.detail, "expired") + 3


@pytest.fixture
def unrefreshable(cluster: ClusterSession, member: Member) -> Iterator[UUID]:
    """A live grant with no refresh row (RESTRICT forbids deleting one, so it is
    inserted without), revoked in teardown so no later doctor run inherits it."""
    token_id = _grant_row(
        cluster,
        member,
        grant_expires_at=datetime.now(UTC) + timedelta(days=20),
        revoked=False,
        refresh=False,
    )
    yield token_id
    with cluster.backend.control_engine.begin() as connection:
        _revoke(connection, token_id)


def test_a_live_grant_with_no_refresh_row_fails(
    cluster: ClusterSession, unrefreshable: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHEO__identity__oauth__max_clients", "1000000")
    check = _check_connector_grants(cluster.backend)
    assert check.level == "FAIL", check
    assert "a live grant cannot refresh" in check.detail
    assert "client cap reached" not in check.detail
    missing = int(check.detail.split("no live refresh row ", 1)[1].split(";")[0])
    assert missing >= 1
    assert str(unrefreshable) not in check.detail


async def test_doctor_shows_both_lines_and_no_client_text(
    surface: OAuthSurface,
    address: str,
    member: Member,
    cluster: ClusterSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _sign_in_as(member)
    client_name = f"Example doctor connector {uuid7().hex[:8]}"
    async with oauth_client(surface, client_address=address) as client:
        grant = await _connect(client, cluster, client_name)
        secrets_seen = client.issued.all()

    _code, out, err = _run(capsys, "doctor")
    report = out.splitlines()
    (surface_line,) = [line for line in report if " oauth surface: " in line]
    assert surface_line == (
        f"ok   oauth surface: configured: issuer {surface.issuer}; "
        "1 allowed redirect URIs"
    )
    (grants_line,) = [line for line in report if " connector grants: " in line]
    assert grants_line.startswith("ok   connector grants: clients ")
    for label in (
        "grants active",
        "expired",
        "revoked",
        "ending within 7 days",
        "live grants with no live refresh row",
    ):
        assert label in grants_line, grants_line

    _assert_no_secret(
        out + err,
        secrets_seen,
        client_name,
        grant.client_id,
        str(grant.token_id),
        CLAUDE_CALLBACK,
    )

"""Connector issuance (issue #287): AC-11's token row and snapshot, the
``grant_revoked`` step in ``core.token.revoke``, ``set_empty``, and the client
lifecycle predicates in ``storage/oauth_store.py``.

Seams under test: ``rheo_core.tokens.issue.issue_connector_token`` (row shape and
snapshot, read back from the database), ``core.token.revoke`` through the real
dispatch path, and ``oauth_store``'s abandoned/counted client predicates against
real rows.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.registry import NOTE_GET, add_member, register_harness
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.core_ops import TOKEN_REVOKE
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import REGISTRY
from rheo_core.refs import uuid7
from rheo_core.storage import control_tables as t
from rheo_core.storage import oauth_store
from rheo_core.storage import oauth_tables as o
from rheo_core.storage.control_plane import (
    get_access_token,
    list_access_token_operations,
    list_access_tokens,
    revoke_access_token,
)
from rheo_core.tokens import issue as issue_module
from rheo_core.tokens.issue import SET_EMPTY, issue_connector_token
from rheo_core.tokens.policy import NON_TOKEN_ISSUABLE
from rheo_core.tokens.sets import (
    DISCOVER_THEN_CALL_OPERATION,
    agent_default,
    register_core_tools,
)
from sqlalchemy import select, update

pytestmark = pytest.mark.postgres

OWNER_SNAPSHOT = frozenset(
    {
        "core.workspace.status",
        "core.operation.get",
        "core.operation.list",
        "core.audit.list",
        NOTE_GET,
    }
)
"""The full ``agent_default`` expansion, spelled out (test_tokens.py's literal)."""

EIGHT = (
    "core.approval.approve",
    "core.approval.refuse",
    "core.evidence_enrollment.create",
    "core.evidence_enrollment.rotate",
    "core.standing_grant.create",
    "core.standing_grant.revoke",
    "core.token.issue",
    "core.token.revoke",
)


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_core_tools()
    register_harness()


def _issue(
    cluster: ClusterSession, account_id: UUID, workspace_id: UUID
) -> tuple[UUID, str, list[str]]:
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    with cluster.backend.control_engine.begin() as connection:
        return issue_connector_token(
            connection,
            account_id=account_id,
            workspace_id=workspace_id,
            expires_at=expires_at,
        )


def _assert_no_non_issuable(operations: frozenset[str]) -> None:
    assert frozenset(EIGHT) == NON_TOKEN_ISSUABLE
    for name in EIGHT:
        assert name not in operations, name
    assert DISCOVER_THEN_CALL_OPERATION not in operations


def test_owner_connector_token_row_and_full_snapshot(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """AC-11: prefix, kind, one account and workspace, an expiry, the connector
    issuer, and the full ``agent_default`` set for an owner."""
    token_id, value, operations = _issue(cluster, owner_account_id, workspace)
    assert value.startswith("rheo_mcp_")
    with cluster.backend.control_engine.connect() as connection:
        row = get_access_token(connection, token_id)
        stored = list_access_token_operations(connection, token_id)
    assert row is not None
    assert row.kind == "mcp"
    assert row.account_id == owner_account_id
    assert row.workspace_id == workspace
    assert row.expires_at is not None
    assert row.issued_from == "connector"
    assert row.issued_from not in {"session", "operator", "runtime"}
    assert row.set_name == "agent_default"
    assert row.purpose is None
    assert stored == frozenset(operations) == OWNER_SNAPSHOT
    _assert_no_non_issuable(stored)


def test_member_snapshot_is_agent_default_for_the_role(
    cluster: ClusterSession, workspace: UUID
) -> None:
    member = add_member(cluster.backend, workspace, Role.MEMBER, display_name="m-1")
    token_id, _, _ = _issue(cluster, member, workspace)
    with cluster.backend.control_engine.connect() as connection:
        stored = list_access_token_operations(connection, token_id)
    expected = frozenset(
        name
        for name in agent_default()
        if Role.MEMBER in REGISTRY.lookup(name).declaration.roles  # type: ignore[union-attr]
    )
    assert stored == expected
    assert stored <= OWNER_SNAPSHOT
    _assert_no_non_issuable(stored)


def test_no_membership_refuses_and_writes_nothing(
    cluster: ClusterSession, workspace: UUID, make_workspace: object
) -> None:
    outsider = add_member(cluster.backend, workspace, Role.MEMBER, display_name="o-1")
    other = make_workspace()  # type: ignore[operator]
    with pytest.raises(OperationRefused) as refused:
        _issue(cluster, outsider, other)
    assert refused.value.state == "membership_missing"


def test_empty_intersection_refuses_set_empty(
    cluster: ClusterSession, workspace: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A role holding none of the set's operations: ``set_empty``, no row."""
    owner_only = sorted(
        name
        for name in REGISTRY.names()
        if Role.MEMBER not in REGISTRY.lookup(name).declaration.roles  # type: ignore[union-attr]
        and name not in NON_TOKEN_ISSUABLE
    )
    assert owner_only, "the registry has no owner-only operation to build the case"
    monkeypatch.setattr(
        issue_module, "agent_default", lambda: frozenset(owner_only[:1])
    )
    member = add_member(cluster.backend, workspace, Role.MEMBER, display_name="m-2")
    with pytest.raises(OperationRefused) as refused:
        _issue(cluster, member, workspace)
    assert refused.value.state == SET_EMPTY
    with cluster.backend.control_engine.connect() as connection:
        assert list_access_tokens(connection, account_id=member) == ()


def _operator_ctx(workspace_id: UUID) -> WorkspaceContext:
    ctx = context_for_operator(workspace_id)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _events(cluster: ClusterSession, token_id: UUID) -> list[tuple[str, str]]:
    with cluster.backend.control_engine.connect() as connection:
        rows = connection.execute(
            select(o.oauth_event.c.event, o.oauth_event.c.outcome).where(
                o.oauth_event.c.token_id == token_id
            )
        ).all()
    return [(row.event, row.outcome) for row in rows]


def _register_client(
    cluster: ClusterSession, client_id: str, created_at: datetime
) -> None:
    with cluster.backend.control_engine.begin() as connection:
        oauth_store.insert_client(
            connection,
            client_id=client_id,
            client_name="Example connector",
            registered_from="192.0.2.10",
            created_at=created_at,
            redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        )


def _grant(
    cluster: ClusterSession,
    client_id: str,
    account_id: UUID,
    workspace_id: UUID,
    *,
    grant_expires_at: datetime,
) -> UUID:
    token_id, _, _ = _issue(cluster, account_id, workspace_id)
    with cluster.backend.control_engine.begin() as connection:
        oauth_store.insert_grant(
            connection,
            token_id=token_id,
            client_id=client_id,
            created_at=datetime.now(UTC),
            grant_expires_at=grant_expires_at,
        )
    return token_id


def test_revoking_a_connector_token_writes_grant_revoked(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    client_id = f"client-revoke-{uuid7().hex}"
    _register_client(cluster, client_id, datetime.now(UTC))
    token_id = _grant(
        cluster,
        client_id,
        owner_account_id,
        workspace,
        grant_expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    outcome = dispatch(
        _operator_ctx(workspace), TOKEN_REVOKE, {"token_id": str(token_id)}
    )
    assert outcome.ok, outcome
    assert _events(cluster, token_id) == [("grant_revoked", "succeeded")]
    # A second revoke of the already-revoked grant writes no second event.
    again = dispatch(
        _operator_ctx(workspace), TOKEN_REVOKE, {"token_id": str(token_id)}
    )
    assert again.ok, again
    assert _events(cluster, token_id) == [("grant_revoked", "succeeded")]
    with cluster.backend.control_engine.connect() as connection:
        event = (
            connection.execute(
                select(o.oauth_event).where(o.oauth_event.c.token_id == token_id)
            )
            .mappings()
            .one()
        )
        row = get_access_token(connection, token_id)
    assert row is not None and row.revoked_at is not None
    assert event["client_id"] == client_id
    assert event["account_id"] == owner_account_id
    assert event["workspace_id"] == workspace


def test_revoking_a_non_connector_token_writes_no_event(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    ctx = _operator_ctx(workspace)
    issued = dispatch(
        ctx,
        "core.token.issue",
        {
            "kind": "mcp",
            "set_name": "agent_default",
            "account_id": str(owner_account_id),
        },
    )
    assert issued.ok, issued
    token_id = issued.result.token_id  # type: ignore[union-attr]
    outcome = dispatch(ctx, TOKEN_REVOKE, {"token_id": str(token_id)})
    assert outcome.ok, outcome
    assert _events(cluster, token_id) == []


# --- oauth_store client lifecycle ----------------------------------------------------


def _counted(cluster: ClusterSession, now: datetime) -> int:
    with cluster.backend.control_engine.connect() as connection:
        return oauth_store.count_counted_clients(
            connection, now=now, abandoned_before=now - timedelta(minutes=60)
        )


def test_client_lifecycle_predicates(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """Abandoned = no grant ever, old, nothing in flight; counted = not abandoned and
    (no grant yet or a live grant). The access token's own expiry plays no part.

    The control plane is shared across tests, so client ids are fresh and the count
    is asserted as a delta."""
    now = datetime.now(UTC)
    old = now - timedelta(hours=3)
    suffix = uuid7().hex
    names = {
        key: f"{key}-{suffix}"
        for key in (
            "abandoned",
            "fresh",
            "in_flight",
            "code_in_flight",
            "idle_live",
            "revoked",
            "past_end",
        )
    }
    baseline = _counted(cluster, now)
    for key, client_id in names.items():
        _register_client(cluster, client_id, now if key == "fresh" else old)
    with cluster.backend.control_engine.begin() as connection:
        oauth_store.insert_authorization(
            connection,
            request_hash=uuid7().bytes * 2,
            client_id=names["in_flight"],
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            state=None,
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        # The request window has closed but the code it issued is still live.
        late = oauth_store.insert_authorization(
            connection,
            request_hash=uuid7().bytes * 2,
            client_id=names["code_in_flight"],
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            state=None,
            created_at=now - timedelta(minutes=20),
            expires_at=now - timedelta(minutes=10),
        )
        oauth_store.record_authorization_decision(
            connection,
            late.id,
            decided_at=now - timedelta(seconds=5),
            account_id=owner_account_id,
            workspace_id=workspace,
            code_hash=uuid7().bytes * 2,
            code_expires_at=now + timedelta(seconds=55),
        )
    live_end = now + timedelta(days=30)
    idle = _grant(
        cluster,
        names["idle_live"],
        owner_account_id,
        workspace,
        grant_expires_at=live_end,
    )
    revoked = _grant(
        cluster,
        names["revoked"],
        owner_account_id,
        workspace,
        grant_expires_at=live_end,
    )
    _grant(
        cluster,
        names["past_end"],
        owner_account_id,
        workspace,
        grant_expires_at=now - timedelta(minutes=1),
    )
    with cluster.backend.control_engine.begin() as connection:
        # The idle grant's hour-long access value lapsed: still a live grant.
        connection.execute(
            update(t.access_token)
            .where(t.access_token.c.id == idle)
            .values(expires_at=now - timedelta(hours=2))
        )
        revoke_access_token(connection, revoked)
    # Counted: fresh (pending, young), in_flight (pending, sign-in live),
    # code_in_flight (request expired, code live), idle_live.
    assert _counted(cluster, now) - baseline == 4
    with cluster.backend.control_engine.begin() as connection:
        oauth_store.delete_abandoned_clients(
            connection, now=now, abandoned_before=now - timedelta(minutes=60)
        )
        survivors = set(
            connection.execute(
                select(o.oauth_client.c.client_id).where(
                    o.oauth_client.c.client_id.in_(list(names.values()))
                )
            ).scalars()
        )
    assert survivors == set(names.values()) - {names["abandoned"]}
    with cluster.backend.control_engine.connect() as connection:
        client = oauth_store.get_client(connection, names["revoked"])
    assert client is not None and client.client_name == "Example connector"


def test_snapshot_strips_non_issuable_and_discover_from_the_expansion(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even an expansion that named them would not carry them: the strip, not the
    set's construction, is what keeps them out."""
    leaked = OWNER_SNAPSHOT | {"core.token.issue", DISCOVER_THEN_CALL_OPERATION}
    monkeypatch.setattr(issue_module, "agent_default", lambda: leaked)
    token_id, _, operations = _issue(cluster, owner_account_id, workspace)
    with cluster.backend.control_engine.connect() as connection:
        stored = list_access_token_operations(connection, token_id)
    assert "core.token.issue" not in stored
    assert DISCOVER_THEN_CALL_OPERATION not in stored
    assert stored == frozenset(operations) == OWNER_SNAPSHOT


def test_expiry_beyond_the_mcp_cap_is_clamped(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """A caller's ``expires_at`` past ``identity.token_max_days.mcp`` (default 30)
    is clamped to the cap."""
    before = datetime.now(UTC)
    with cluster.backend.control_engine.begin() as connection:
        token_id, _, _ = issue_connector_token(
            connection,
            account_id=owner_account_id,
            workspace_id=workspace,
            expires_at=before + timedelta(days=365),
        )
    after = datetime.now(UTC)
    with cluster.backend.control_engine.connect() as connection:
        row = get_access_token(connection, token_id)
    assert row is not None
    assert before + timedelta(days=30) <= row.expires_at <= after + timedelta(days=30)


def test_expired_authorization_cleanup_keeps_redeemed_rows(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """A redeemed row older than the cutoff survives (a late replay must still find
    what its code issued); an unredeemed expired one is deleted."""
    now = datetime.now(UTC)
    client_id = f"client-cleanup-{uuid7().hex}"
    _register_client(cluster, client_id, now - timedelta(days=3))
    token_id, _, _ = _issue(cluster, owner_account_id, workspace)
    with cluster.backend.control_engine.begin() as connection:
        rows = {}
        for key in ("redeemed", "unredeemed"):
            rows[key] = oauth_store.insert_authorization(
                connection,
                request_hash=uuid7().bytes * 2,
                client_id=client_id,
                redirect_uri="https://claude.ai/api/mcp/auth_callback",
                code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                state=None,
                created_at=now - timedelta(days=2, minutes=10),
                expires_at=now - timedelta(days=2),
            ).id
        oauth_store.set_authorization_token(
            connection, rows["redeemed"], token_id=token_id
        )
        oauth_store.delete_expired_authorizations(
            connection, before=now - timedelta(days=1)
        )
        left = set(
            connection.execute(
                select(o.oauth_authorization.c.id).where(
                    o.oauth_authorization.c.client_id == client_id
                )
            ).scalars()
        )
    assert left == {rows["redeemed"]}

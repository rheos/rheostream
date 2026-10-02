"""Revision ``0003_oauth`` (issue #287): the storage half of AC-11.

Seams: the control-plane schema as migrated. A scratch database is taken to
``0002_work_index``, seeded with one token of each existing kind/issuer pair, then
upgraded to head; every seeded token must still read and resolve. On the migrated
schema the widened ``access_token_issued_from`` CHECK takes ``connector`` and still
refuses an unknown issuer, and the RESTRICT keys on ``oauth_grant`` and
``oauth_refresh_token`` refuse the deletes they exist to refuse. The downgrade raises.
"""

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import psycopg.errors
import pytest
from alembic import command
from conftest import ClusterSession
from rheo_contracts import Role
from rheo_core.migrations.orchestrator import (
    CONTROL_CHAIN,
    build_config,
    recorded_revisions,
    run_chain,
)
from rheo_core.refs import uuid7
from rheo_core.sessions.cookies import OAUTH_REQUEST_COOKIE, build_cookie
from rheo_core.storage import control_tables, oauth_tables
from rheo_core.storage.backend import StorageRefusal
from rheo_core.storage.control_plane import (
    ACCESS_TOKEN_MISSING,
    ACCESS_TOKEN_REVOKED,
    get_access_token,
    get_access_token_by_hash,
    insert_access_token,
    insert_account,
    insert_membership_if_absent,
    insert_workspace_if_absent,
    list_access_tokens,
    revoke_access_token,
    rotate_access_token,
    set_workspace_state,
)
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.tokens import presentation
from rheo_core.tokens.format import mint
from rheo_core.tokens.presentation import ResolvedToken, resolve_token
from sqlalchemy import Connection, Engine, delete, insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import Executable

pytestmark = pytest.mark.postgres

_FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)

# Every (kind, issued_from, surface it resolves on) that 0001's CHECKs admit. The
# runtime-pairing CHECK ties ``runtime`` to ``runtime`` both ways, so this is all five.
_EXISTING_PAIRS = (
    ("cli", "session", "api"),
    ("cli", "operator", "api"),
    ("mcp", "session", "mcp"),
    ("mcp", "operator", "mcp"),
    ("runtime", "runtime", "mcp"),
)


@dataclass(frozen=True, slots=True)
class Seeded:
    kind: str
    issued_from: str
    surface: str
    value: str
    token_hash: bytes
    token_id: UUID


@dataclass(frozen=True, slots=True)
class Scratch:
    name: str
    engine: Engine
    account_id: UUID
    workspace_id: UUID
    seeded: tuple[Seeded, ...]


def _stage(connection: Connection, target: str) -> None:
    """Upgrade the scratch database's control chain to ``target`` only."""
    connection.execute(text("CREATE SCHEMA IF NOT EXISTS control"))
    command.upgrade(build_config(CONTROL_CHAIN, connection), target)


@pytest.fixture(scope="module")
def scratch(cluster: ClusterSession) -> Iterator[Scratch]:
    name = cluster.record(f"ws_{uuid7().hex}")
    assert cluster.backend.ensure_database(
        name, template=cluster.backend.template_database
    )
    engine = cluster.backend.pools.engine_for(name)
    with engine.begin() as connection:
        _stage(connection, "0002_work_index")
        assert recorded_revisions(connection, CONTROL_CHAIN) == {"0002_work_index"}
        account_id = insert_account(connection, display_name="owner-one").id
        workspace_id = uuid7()
        insert_workspace_if_absent(
            connection,
            workspace_id=workspace_id,
            slug=f"oauth-{workspace_id.hex[:12]}",
            display_name="OAuth migration",
            database_name=f"ws_{workspace_id.hex}",
            state_detail="seeded for the 0003_oauth migration test",
        )
        set_workspace_state(
            connection, workspace_id, state=WorkspaceState.ACTIVE, state_detail=None
        )
        insert_membership_if_absent(
            connection,
            account_id=account_id,
            workspace_id=workspace_id,
            role=Role.OWNER,
        )
        seeded: list[Seeded] = []
        for kind, issued_from, surface in _EXISTING_PAIRS:
            value, raw = mint(kind)
            token_hash = hashlib.sha256(raw).digest()
            row = insert_access_token(
                connection,
                account_id=account_id,
                workspace_id=workspace_id,
                kind=kind,
                issued_from=issued_from,
                token_hash=token_hash,
                set_name=None,
                purpose="respond" if kind == "runtime" else None,
                expires_at=_FAR_FUTURE,
            )
            seeded.append(Seeded(kind, issued_from, surface, value, token_hash, row.id))
    with engine.begin() as connection:
        run_chain(connection, CONTROL_CHAIN, expected_database=name)
    yield Scratch(name, engine, account_id, workspace_id, tuple(seeded))


def test_upgrade_reaches_0003_with_the_six_tables(scratch: Scratch) -> None:
    with scratch.engine.connect() as connection:
        assert recorded_revisions(connection, CONTROL_CHAIN) == {"0003_oauth"}
        present = set(
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'control' AND table_name LIKE 'oauth_%'"
                )
            ).scalars()
        )
    assert present == {
        "oauth_client",
        "oauth_client_redirect_uri",
        "oauth_authorization",
        "oauth_grant",
        "oauth_refresh_token",
        "oauth_event",
    }
    assert present == {
        table.name for table in oauth_tables.oauth_metadata.sorted_tables
    }


def test_every_existing_token_still_reads_and_resolves(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-11, storage half: the widened CHECK leaves today's tokens untouched."""
    # ``resolve_token`` reads the process backend's control engine; point it at the
    # scratch database that was migrated over these rows.
    monkeypatch.setattr(
        presentation,
        "get_backend",
        lambda: SimpleNamespace(control_engine=scratch.engine),
    )
    assert {(s.kind, s.issued_from) for s in scratch.seeded} == {
        (kind, issued_from) for kind, issued_from, _ in _EXISTING_PAIRS
    }
    for seeded in scratch.seeded:
        with scratch.engine.connect() as connection:
            row = get_access_token_by_hash(connection, seeded.token_hash)
        assert row is not None
        assert (row.id, row.kind, row.issued_from) == (
            seeded.token_id,
            seeded.kind,
            seeded.issued_from,
        )
        resolved = resolve_token(seeded.value, seeded.surface)
        assert isinstance(resolved, ResolvedToken), (seeded.kind, resolved)
        assert resolved.token_id == seeded.token_id
        assert resolved.workspace_id == scratch.workspace_id


def _insert_token(connection: Connection, scratch: Scratch, issued_from: str) -> UUID:
    return insert_access_token(
        connection,
        account_id=scratch.account_id,
        workspace_id=scratch.workspace_id,
        kind="mcp",
        issued_from=issued_from,
        token_hash=hashlib.sha256(uuid7().bytes).digest(),
        set_name=None,
        purpose=None,
        expires_at=_FAR_FUTURE,
    ).id


# Each test below works inside one ``connect()`` block and never commits: closing
# the connection rolls its transaction back, so the module's scratch database keeps
# exactly the seeded rows.


def test_connector_issuer_inserts(scratch: Scratch) -> None:
    with scratch.engine.connect() as connection:
        token_id = _insert_token(connection, scratch, "connector")
        issued = connection.execute(
            text("SELECT issued_from FROM control.access_token WHERE id = :id"),
            {"id": token_id},
        ).scalar_one()
    assert issued == "connector"


def test_unknown_issuer_is_still_refused(scratch: Scratch) -> None:
    with scratch.engine.connect() as connection:
        with pytest.raises(IntegrityError) as excinfo:
            _insert_token(connection, scratch, "not_an_issuer")
    assert isinstance(excinfo.value.orig, psycopg.errors.CheckViolation)
    assert "access_token_issued_from" in str(excinfo.value.orig)


def _seed_grant(
    connection: Connection, scratch: Scratch, *, with_refresh: bool
) -> tuple[str, UUID]:
    """One client with one grant, in the caller's transaction, and one live refresh
    row only when ``with_refresh``: a refresh row's own RESTRICT would otherwise
    refuse a cascaded grant delete and mask the key under test."""
    now = datetime.now(UTC)
    client_id = f"client-{uuid7().hex[:12]}"
    connection.execute(
        insert(oauth_tables.oauth_client).values(
            client_id=client_id,
            client_name="Example connector",
            registered_from="192.0.2.10",
            created_at=now,
        )
    )
    connection.execute(
        insert(oauth_tables.oauth_client_redirect_uri).values(
            client_id=client_id,
            redirect_uri="https://connector.example.com/callback",
        )
    )
    token_id = _insert_token(connection, scratch, "connector")
    connection.execute(
        insert(oauth_tables.oauth_grant).values(
            token_id=token_id,
            client_id=client_id,
            created_at=now,
            grant_expires_at=now + timedelta(days=30),
        )
    )
    if not with_refresh:
        return client_id, token_id
    connection.execute(
        insert(oauth_tables.oauth_refresh_token).values(
            token_hash=hashlib.sha256(uuid7().bytes).digest(),
            token_id=token_id,
            created_at=now,
            rotated_at=None,
        )
    )
    return client_id, token_id


def _assert_foreign_key_refuses(
    scratch: Scratch,
    statement_for: Callable[[str, UUID], Executable],
    *,
    with_refresh: bool = False,
) -> None:
    with scratch.engine.connect() as connection:
        client_id, token_id = _seed_grant(
            connection, scratch, with_refresh=with_refresh
        )
        statement = statement_for(client_id, token_id)
        with pytest.raises(IntegrityError) as excinfo:
            connection.execute(statement)
    assert isinstance(excinfo.value.orig, psycopg.errors.ForeignKeyViolation)


def test_deleting_a_client_with_a_grant_is_refused(scratch: Scratch) -> None:
    _assert_foreign_key_refuses(
        scratch,
        lambda client_id, _token_id: delete(oauth_tables.oauth_client).where(
            oauth_tables.oauth_client.c.client_id == client_id
        ),
    )


def test_deleting_an_access_token_with_a_grant_is_refused(scratch: Scratch) -> None:
    _assert_foreign_key_refuses(
        scratch,
        lambda _client_id, token_id: delete(control_tables.access_token).where(
            control_tables.access_token.c.id == token_id
        ),
    )


def test_deleting_a_grant_with_refresh_rows_is_refused(scratch: Scratch) -> None:
    _assert_foreign_key_refuses(
        scratch,
        lambda _client_id, token_id: delete(oauth_tables.oauth_grant).where(
            oauth_tables.oauth_grant.c.token_id == token_id
        ),
        with_refresh=True,
    )


def test_deleting_a_client_without_a_grant_cascades_its_children(
    scratch: Scratch,
) -> None:
    """The CASCADE side: an abandoned client takes its redirect URIs and pending
    authorization rows with it."""
    now = datetime.now(UTC)
    client_id = f"client-{uuid7().hex[:12]}"
    with scratch.engine.connect() as connection:
        connection.execute(
            insert(oauth_tables.oauth_client).values(
                client_id=client_id,
                client_name="unnamed client",
                registered_from="192.0.2.11",
                created_at=now,
            )
        )
        connection.execute(
            insert(oauth_tables.oauth_client_redirect_uri).values(
                client_id=client_id,
                redirect_uri="https://connector.example.com/callback",
            )
        )
        connection.execute(
            insert(oauth_tables.oauth_authorization).values(
                id=uuid7(),
                request_hash=hashlib.sha256(uuid7().bytes).digest(),
                client_id=client_id,
                redirect_uri="https://connector.example.com/callback",
                code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                created_at=now,
                expires_at=now + timedelta(minutes=10),
            )
        )
        connection.execute(
            delete(oauth_tables.oauth_client).where(
                oauth_tables.oauth_client.c.client_id == client_id
            )
        )
        for table in (
            oauth_tables.oauth_client_redirect_uri,
            oauth_tables.oauth_authorization,
        ):
            left = connection.execute(
                text(f"SELECT count(*) FROM control.{table.name} WHERE client_id = :c"),
                {"c": client_id},
            ).scalar_one()
            assert left == 0, table.name


def test_rotate_access_token_replaces_the_value_in_place(scratch: Scratch) -> None:
    with scratch.engine.connect() as connection:
        token_id = _insert_token(connection, scratch, "connector")
        before = get_access_token(connection, token_id)
        assert before is not None
        new_hash = hashlib.sha256(uuid7().bytes).digest()
        new_expiry = datetime(2099, 6, 1, tzinfo=UTC)
        rotate_access_token(
            connection, token_id, token_hash=new_hash, expires_at=new_expiry
        )
        assert get_access_token_by_hash(connection, before.token_hash) is None
        after = get_access_token_by_hash(connection, new_hash)
        assert after is not None
        assert (after.id, after.expires_at, after.created_at) == (
            token_id,
            new_expiry,
            before.created_at,
        )
        with pytest.raises(StorageRefusal) as excinfo:
            rotate_access_token(
                connection, uuid7(), token_hash=new_hash, expires_at=new_expiry
            )
        assert excinfo.value.state == ACCESS_TOKEN_MISSING


def test_rotate_access_token_refuses_a_revoked_row_and_leaves_it(
    scratch: Scratch,
) -> None:
    with scratch.engine.connect() as connection:
        token_id = _insert_token(connection, scratch, "connector")
        revoke_access_token(connection, token_id)
        before = get_access_token(connection, token_id)
        assert before is not None and before.revoked_at is not None
        with pytest.raises(StorageRefusal) as excinfo:
            rotate_access_token(
                connection,
                token_id,
                token_hash=hashlib.sha256(uuid7().bytes).digest(),
                expires_at=datetime(2099, 6, 1, tzinfo=UTC),
            )
        assert excinfo.value.state == ACCESS_TOKEN_REVOKED
        assert get_access_token(connection, token_id) == before


_DELETE_RULES = {
    # (table, column) -> pg_constraint.confdeltype: 'r' RESTRICT, 'c' CASCADE.
    ("oauth_grant", "token_id"): "r",
    ("oauth_grant", "client_id"): "r",
    ("oauth_refresh_token", "token_id"): "r",
    ("oauth_client_redirect_uri", "client_id"): "c",
    ("oauth_authorization", "client_id"): "c",
}


def test_the_foreign_keys_carry_their_declared_delete_rules(scratch: Scratch) -> None:
    """The rule itself, not only its effect: a NO ACTION key would also refuse the
    deletes above (at statement end), so pin ``confdeltype`` from the catalog."""
    with scratch.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT cl.relname, a.attname, c.confdeltype "
                "FROM pg_constraint c "
                "JOIN pg_class cl ON cl.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = cl.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid "
                "AND a.attnum = c.conkey[1] "
                "WHERE c.contype = 'f' AND n.nspname = 'control' "
                "AND cardinality(c.conkey) = 1"
            )
        ).all()
    found = {(str(r[0]), str(r[1])): str(r[2]) for r in rows}
    assert {key: found.get(key) for key in _DELETE_RULES} == _DELETE_RULES


def test_list_access_tokens_filters_by_workspace_and_account(scratch: Scratch) -> None:
    seeded_ids = [s.token_id for s in scratch.seeded]
    with scratch.engine.connect() as connection:
        every = [row.id for row in list_access_tokens(connection)]
        in_workspace = list_access_tokens(connection, workspace_id=scratch.workspace_id)
        for_account = list_access_tokens(connection, account_id=scratch.account_id)
        both = list_access_tokens(
            connection,
            workspace_id=scratch.workspace_id,
            account_id=scratch.account_id,
        )
        assert list_access_tokens(connection, workspace_id=uuid7()) == ()
        assert list_access_tokens(connection, account_id=uuid7()) == ()
    # Oldest first: the seed order, since uuid7 ids break any created_at tie in order.
    assert every == seeded_ids
    assert [row.id for row in in_workspace] == seeded_ids
    assert for_account == in_workspace == both


def test_downgrade_raises(scratch: Scratch) -> None:
    with scratch.engine.connect() as connection:
        with pytest.raises(NotImplementedError):
            command.downgrade(
                build_config(CONTROL_CHAIN, connection), "0002_work_index"
            )


def test_the_oauth_request_cookie_lives_ten_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RHEO_PROFILE", "test")
    built = build_cookie(OAUTH_REQUEST_COOKIE, "opaque-value", scheme="https")
    assert OAUTH_REQUEST_COOKIE == "rheo_oauth_request"
    assert built.split("; ") == [
        "rheo_oauth_request=opaque-value",
        "HttpOnly",
        "SameSite=Lax",
        "Path=/",
        "Max-Age=600",
        "Secure",
    ]

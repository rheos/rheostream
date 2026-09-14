"""``POST /internal/v1/operations/{name}``: the one piece of run 0v that survives
its branch cut.

The route is the subject. Run 0v drove it with its own throwaway module; 0c0's
branch cut removed that, so the operations here are the shipped ``core.*`` ones and
the suite's own ``harness.*`` registrations — the same pair
``tests/postgres/test_api_surface.py`` drives the sibling bearer ``api`` surface
with. What this module proves, in the order the route itself checks:

1. ``require_internal_secret`` gates the route exactly as it gates the listener's
   other two — a request with a valid session and no shared secret is refused
   before any session is looked up (``test_no_internal_secret_is_401_before_the
   _session_is_even_read``).
2. Every ``context_from_session`` refusal becomes a 401 **before** ``dispatch()``
   is called (``test_a_session_refusal_never_reaches_the_dispatcher``, which spies
   on the dispatcher itself rather than inferring it from a status code). That is
   why ``authorize``'s ``context_required`` is unreachable through HTTP: the route
   is correct, not the test suite incomplete. The refusal itself is pinned
   directly on ``main`` by ``tests/test_boundary.py``.
3. AC 13: the route reads **no** workspace identifier from anywhere in the
   request. A payload naming another workspace in five different reserved keys,
   through both the query string and the JSON body, still writes into the
   session's own workspace.
4. The dispatcher's check-order refusals through the real route, each asserting
   the *first* refusal wins when two conditions are simultaneously false.
5. The mutate and read operations, end to end through the real route.

``operation_not_permitted`` is the one refusal in ``authorize``'s chain this module
does not reach, and cannot: ``context_from_session`` sets ``operation_set =
ALL_OPERATIONS`` unconditionally, so no session input can produce it. It is live on
the token surface only and is already proven there by
``tests/postgres/test_api_surface.py::test_operation_not_permitted_403`` (run 0v's
finding F13).
"""

from typing import Any
from uuid import UUID

import httpx
import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.records import ensure_note_table, list_notes
from harness.registry import (
    NOTE_WRITE,
    add_member,
    enable_harness_module,
    register_harness,
)
from rheo_app_core import internal_routes
from rheo_app_core.main import internal_app
from rheo_contracts import Role
from rheo_core.boundary.context import (
    CONTEXT_REQUIRED,
    SESSION_MISSING,
    SESSION_REVOKED,
    WORKSPACE_UNSELECTED,
)
from rheo_core.operations import register_core_operations
from rheo_core.operations.core_ops import SETTINGS_SET, WORKSPACE_STATUS
from rheo_core.operations.refusals import (
    MODULE_DISABLED,
    OPERATION_UNKNOWN,
    ROLE_NOT_PERMITTED,
    SUCCEEDED,
)
from rheo_core.sessions import (
    create_session,
    mint_host_secret,
    revoke,
    switch_workspace,
)
from rheo_core.storage.backend import UnitOfWork

pytestmark = pytest.mark.postgres

BASE_HOST = "example.test"
INTERNAL_SECRET_VALUE = "internal-operations-test-secret"
SETTING_KEY = "identity.token_max_days.cli"

UNKNOWN_OPERATION = "harness.note.missing"
"""A well-formed operation name the harness does not declare. Deliberately in the
``harness`` module's own namespace: the point of the first check-order case is that
the *unknown* check wins over the *module* check when both would refuse, so the name
has to name a module that really is disabled in the workspace under test."""


# --- fixtures and helpers -------------------------------------------------------------


@pytest.fixture(autouse=True)
def registrations(harness_keys: None) -> None:
    """The shipped core operations and the suite's harness module, on the
    process-wide registries — the pattern ``test_api_surface.py`` and
    ``test_context_routing.py`` already establish. Both are idempotent."""
    register_core_operations()
    register_harness()


@pytest.fixture(autouse=True)
def _internal_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RHEO_INTERNAL_SECRET", INTERNAL_SECRET_VALUE)
    monkeypatch.setenv(
        "RHEO__internal__secret_ref", "secret://env/RHEO_INTERNAL_SECRET"
    )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
    )


def _headers(session_value: str | None) -> dict[str, str]:
    headers = {"X-Rheo-Internal": INTERNAL_SECRET_VALUE, "X-Rheo-Host": BASE_HOST}
    if session_value is not None:
        headers["X-Rheo-Session"] = session_value
    return headers


async def _post(
    name: str,
    session_value: str | None,
    payload: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    async with _client() as client:
        return await client.post(
            f"/internal/v1/operations/{name}",
            headers=_headers(session_value) if headers is None else headers,
            params=params,
            json={} if payload is None else payload,
        )


def _session_for(account_id: UUID, workspace_id: UUID | None) -> str:
    """A hex session secret bound to ``BASE_HOST``, optionally already pointed at a
    workspace. ``workspace_id is None`` is the ``workspace_unselected`` case every
    browser session starts in (CF1)."""
    row = create_session(account_id)
    secret = mint_host_secret(row.id, BASE_HOST)
    if workspace_id is not None:
        assert switch_workspace(row.id, workspace_id) is None
    return secret.hex()


def _member_session(cluster: ClusterSession, workspace_id: UUID, label: str) -> str:
    """A fresh account with a ``member`` membership in ``workspace_id``, and a
    session already switched into it.

    ``member`` rather than the workspace owner because the two role-order cases need
    a role ``core.settings.set`` (roles ``{owner}``) refuses, and the fixture owner
    would be permitted.
    """
    account_id = add_member(
        cluster.backend, workspace_id, Role.MEMBER, display_name=label
    )
    return _session_for(account_id, workspace_id)


def _notes(cluster: ClusterSession, workspace_id: UUID) -> tuple[str, ...]:
    """The note bodies in one workspace's own database, oldest first.

    Only ever called on a workspace ``_prepare`` has created the table in, so an
    empty result means "no note was written" rather than "no table exists" — the
    distinction the not-written assertions below depend on.
    """
    row = cluster.registry_row(workspace_id)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        return tuple(note.body for note in list_notes(uow.connection))


def _prepare(cluster: ClusterSession, workspace_id: UUID, *, enable: bool) -> UUID:
    """Create ``harness.note`` in one workspace's database, and optionally write the
    enabled ``core.module_state`` row for ``harness``.

    Two steps rather than one because they answer different questions, and
    ``tests/postgres/test_context_routing.py`` pairs them the same way: the table is
    what makes a read back well-defined, and the state row is what puts ``harness``
    in a context's ``enabled_modules``. Module install proper is phase 2; this row is
    the only way to drive the enabled direction of the module check.
    """
    row = cluster.registry_row(workspace_id)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        ensure_note_table(uow.connection)
        if enable:
            enable_harness_module(uow.connection)
        uow.commit()
    return workspace_id


@pytest.fixture
def harness_workspace(cluster: ClusterSession, workspace: UUID) -> UUID:
    """A provisioned workspace with the ``harness`` module enabled and its note
    table created."""
    return _prepare(cluster, workspace, enable=True)


@pytest.fixture
def plain_workspace(make_workspace: MakeWorkspace) -> UUID:
    """A second provisioned workspace the harness module was **never enabled in**,
    while the module's operations are registered in this process (the autouse
    ``registrations`` fixture did that).

    That combination is what makes ``module_disabled`` a real refusal rather than an
    absence: the operation exists and is found, and the workspace still refuses it.
    """
    return make_workspace()


@pytest.fixture
def owner_session(harness_workspace: UUID, owner_account_id: UUID) -> str:
    return _session_for(owner_account_id, harness_workspace)


# --- the trust boundary: the shared secret --------------------------------------------


async def test_no_internal_secret_is_401_before_the_session_is_even_read(
    cluster: ClusterSession, harness_workspace: UUID, owner_session: str
) -> None:
    """The route hangs off the listener's ``router``, so ``require_internal_secret``
    is a route dependency and runs before the handler body. A perfectly good session
    plus no shared secret is still 401, and nothing is written."""
    response = await _post(
        NOTE_WRITE,
        None,
        {"body": "should never be written"},
        headers={"X-Rheo-Session": owner_session, "X-Rheo-Host": BASE_HOST},
    )
    assert response.status_code == 401
    assert _notes(cluster, harness_workspace) == ()


async def test_a_wrong_internal_secret_is_401(
    cluster: ClusterSession, harness_workspace: UUID, owner_session: str
) -> None:
    response = await _post(
        NOTE_WRITE,
        None,
        {"body": "should never be written"},
        headers={
            "X-Rheo-Internal": "not-the-secret",
            "X-Rheo-Session": owner_session,
            "X-Rheo-Host": BASE_HOST,
        },
    )
    assert response.status_code == 401
    assert _notes(cluster, harness_workspace) == ()


# --- the trust boundary: every session refusal is a 401, before dispatch --------------


async def test_no_session_header_is_401_session_missing(
    harness_workspace: UUID,
) -> None:
    response = await _post(NOTE_WRITE, None, {"body": "x"})
    assert response.status_code == 401
    assert response.json()["state"] == SESSION_MISSING


async def test_a_malformed_session_header_is_401_session_missing(
    harness_workspace: UUID,
) -> None:
    """A non-hex ``X-Rheo-Session`` resolves the same as no session at all — it
    never reaches ``bytes.fromhex``'s ``ValueError`` as a 500."""
    response = await _post(NOTE_WRITE, "not-hex", {"body": "x"})
    assert response.status_code == 401
    assert response.json()["state"] == SESSION_MISSING


async def test_a_revoked_session_is_401_session_revoked(
    cluster: ClusterSession, harness_workspace: UUID, owner_account_id: UUID
) -> None:
    row = create_session(owner_account_id)
    secret = mint_host_secret(row.id, BASE_HOST)
    assert switch_workspace(row.id, harness_workspace) is None
    revoke(row.id)
    response = await _post(NOTE_WRITE, secret.hex(), {"body": "should not be written"})
    assert response.status_code == 401
    assert response.json()["state"] == SESSION_REVOKED
    assert _notes(cluster, harness_workspace) == ()


async def test_a_session_with_no_workspace_is_401_workspace_unselected(
    harness_workspace: UUID, owner_account_id: UUID
) -> None:
    """Every browser session is created with ``active_workspace_id = NULL`` and only
    ``switch_workspace`` ever sets it (CF1, a ratified scope cut). The route refuses
    that session by name rather than dispatching into a missing workspace."""
    session_value = _session_for(owner_account_id, None)
    response = await _post(NOTE_WRITE, session_value, {"body": "x"})
    assert response.status_code == 401
    assert response.json()["state"] == WORKSPACE_UNSELECTED


async def test_a_session_refusal_never_reaches_the_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
    harness_workspace: UUID,
    owner_account_id: UUID,
    owner_session: str,
) -> None:
    """FR 2's real claim, asserted against the dispatcher rather than inferred from
    a status code: ``dispatch()`` is called only with a ``WorkspaceContext``.

    The spy wraps the real function, so the positive control at the end is what
    keeps the negative assertions from being vacuous — a spy that was never wired
    up would fail that line, not pass silently.
    """
    seen: list[object] = []
    real = internal_routes.dispatch

    def _spy(ctx: object, name: str, payload: Any = None, **kwargs: Any) -> Any:
        seen.append(ctx)
        return real(ctx, name, payload, **kwargs)

    monkeypatch.setattr(internal_routes, "dispatch", _spy)

    for session_value in (None, "not-hex", _session_for(owner_account_id, None)):
        response = await _post(NOTE_WRITE, session_value, {"body": "x"})
        assert response.status_code == 401
        # The boundary factory's own state, never the dispatcher's.
        assert response.json()["state"] != CONTEXT_REQUIRED
    assert seen == [], "a session refusal reached dispatch()"

    ok = await _post(NOTE_WRITE, owner_session, {"body": "the positive control"})
    assert ok.status_code == 200, ok.text
    assert len(seen) == 1


# --- AC 13: server-derived routing ----------------------------------------------------


async def test_a_workspace_naming_payload_is_ignored(
    cluster: ClusterSession,
    harness_workspace: UUID,
    make_workspace: MakeWorkspace,
    owner_session: str,
) -> None:
    """AC 13. A POST whose query string *and* body both name workspace B in five
    reserved keys succeeds and writes into workspace A — the session's own.

    Both channels on purpose: ``api_routes`` and this route each merge the query
    string and the JSON body into one payload, so proving only the body would leave
    the other half unproven. Two mechanisms drop the keys, and either alone would
    be enough: ``OperationRegistry.register`` refuses at *registration* any input
    model that declares a reserved field, so no registered operation can read one;
    and the model's ``extra = "ignore"`` drops whatever arrives anyway.
    """
    # B gets the note table but not the module state row: it is prepared to receive
    # a note, so the empty read below is about rows and not about a missing table.
    other = _prepare(cluster, make_workspace(), enable=False)
    naming_b = {
        "workspace_id": str(other),
        "workspace": str(other),
        "database": f"rheo_ws_{other.hex}",
        "dsn": "postgresql://elsewhere/other",
        "connection_string": "postgresql://elsewhere/other",
        "schema": "harness",
    }
    response = await _post(
        NOTE_WRITE,
        owner_session,
        {"body": "lands in A", **naming_b},
        params={key: value for key, value in naming_b.items()},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == SUCCEEDED

    assert _notes(cluster, harness_workspace) == ("lands in A",)
    assert _notes(cluster, other) == ()


# --- the dispatcher's check order, through the real route -----------------------------


async def test_unknown_operation_refuses_before_the_module_check(
    plain_workspace: UUID, owner_account_id: UUID
) -> None:
    """Two conditions false at once: the operation is not registered, **and** the
    ``harness`` module it names is not enabled in this workspace.
    ``operation_unknown`` wins, because ``authorize`` looks the operation up before
    it can know which module owns it. A check order that derived the module id from
    the name string would answer ``module_disabled`` here."""
    response = await _post(
        UNKNOWN_OPERATION,
        _session_for(owner_account_id, plain_workspace),
        {"body": "x"},
    )
    assert response.json()["state"] == OPERATION_UNKNOWN


async def test_disabled_module_refuses_before_the_role_check(
    cluster: ClusterSession, plain_workspace: UUID
) -> None:
    """Two conditions false at once: the ``harness`` module is not enabled in this
    workspace, **and** the caller's ``member`` role is not one ``core.settings.set``
    (roles ``{owner}``) permits — so the two operations differ, but the request that
    names the harness one is refused for the module, not the role.

    ``module_disabled`` is reachable only through a non-``core`` module:
    ``authorize`` skips the check outright for ``core.*``. After 0c0's branch cut
    the harness is the only such module in the tree, which is why this case is
    driven through it rather than through a shipped operation.
    """
    session_value = _member_session(cluster, plain_workspace, "member-disabled-case")
    response = await _post(NOTE_WRITE, session_value, {"body": "x"})
    assert response.json()["state"] == MODULE_DISABLED


async def test_role_refuses_after_the_module_check(
    cluster: ClusterSession, harness_workspace: UUID
) -> None:
    """The other side of the pair: a ``core.*`` operation reaches the role check
    (the module check does not apply to it at all) and refuses there.

    ``core.settings.set`` is the only reachable driver among the shipped core
    operations — ``core.workspace.status`` permits ``{owner, member, operator}``
    and so does ``core.settings.set_member``, so neither can ever produce this
    refusal from a session.
    """
    session_value = _member_session(cluster, harness_workspace, "member-role-case")
    response = await _post(
        SETTINGS_SET, session_value, {"key": SETTING_KEY, "value": 45}
    )
    assert response.status_code == 403
    assert response.json()["state"] == ROLE_NOT_PERMITTED


# --- the two real operations through the boundary -------------------------------------


async def test_a_mutate_operation_through_the_internal_boundary(
    cluster: ClusterSession, harness_workspace: UUID, owner_session: str
) -> None:
    """The envelope's own ``state`` is the ``succeeded`` a caller acts on, and the
    write really lands in the session's workspace."""
    response = await _post(NOTE_WRITE, owner_session, {"body": "through the boundary"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == SUCCEEDED
    assert body["operation_id"] is None
    assert body["result"]["body"] == "through the boundary"
    assert body["result"]["ref"].startswith("harness.note:")

    assert _notes(cluster, harness_workspace) == ("through the boundary",)


async def test_a_read_operation_through_the_internal_boundary(
    harness_workspace: UUID, owner_session: str
) -> None:
    """A read operation over the same session, reading back what the mutate
    operation wrote a request earlier — through the route both times."""
    written = await _post(NOTE_WRITE, owner_session, {"body": "first"})
    assert written.status_code == 200, written.text
    ref = written.json()["result"]["ref"]

    read = await _post("harness.note.get", owner_session, {"ref": ref})
    assert read.status_code == 200, read.text
    assert read.json()["state"] == SUCCEEDED
    assert read.json()["result"]["ref"] == ref
    assert read.json()["result"]["display"] == "first"


async def test_a_core_read_operation_through_the_internal_boundary(
    harness_workspace: UUID, owner_session: str
) -> None:
    """A shipped ``core.*`` operation over the same route, so the boundary is not
    proved only against the suite's own registrations."""
    response = await _post(WORKSPACE_STATUS, owner_session, {})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == SUCCEEDED
    assert body["operation_id"] is None
    assert "modules" in body["result"]


async def test_input_validation_refuses_through_the_route(
    cluster: ClusterSession, harness_workspace: UUID, owner_session: str
) -> None:
    """The envelope carries a refusal the same way it carries a success, and a
    payload missing a required field never becomes a row: ``input_invalid`` maps to
    422 through the shared ``outcome_status``."""
    response = await _post(NOTE_WRITE, owner_session, {})
    assert response.status_code == 422
    assert response.json()["state"] == "input_invalid"
    assert "body" in response.json()["error"]["error_text"]
    assert _notes(cluster, harness_workspace) == ()


# --- the listener's routing route -----------------------------------------------


async def test_the_routing_config_serialises_every_named_surface() -> None:
    """``/internal/v1/routing``'s response shape, over the listener's own route.

    ``modules`` is the one part of ``RoutingConfig`` no settings key backs: it is
    whatever the manifests this process loaded declared, and after 0c0's branch cut
    no distribution publishes a ``rheo.modules`` entry point, so it is empty. That
    empty value is asserted rather than skipped — it is what a fresh deployment
    serialises, and the six named surfaces must still serialise exactly what they
    serialised before the ``modules`` key existed (which is what keeps
    ``tests/fixtures/routing/*.json`` and ``tests/test_routing.py`` untouched).

    D-6's other half — a loaded manifest's ``WebSurface`` reaching this payload —
    went with the last module distribution. ``tests/test_module_loader.py`` drives
    ``module_surfaces()`` against a fabricated entry point instead.
    """
    async with _client() as client:
        response = await client.get(
            "/internal/v1/routing",
            headers={"X-Rheo-Internal": INTERNAL_SECRET_VALUE},
        )
    assert response.status_code == 200, response.text
    surfaces = response.json()["surfaces"]
    assert surfaces["modules"] == {}
    assert set(surfaces) == {
        "shell",
        "identity",
        "api",
        "mcp",
        "docs",
        "integration",
        "modules",
    }

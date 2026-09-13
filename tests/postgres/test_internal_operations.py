"""``POST /internal/v1/operations/{name}``: the run's one new trust boundary.

**Every route-driven pytest test in run 0v lives here**, because this is the chunk
that builds the route; ``tests/postgres/test_spike_slice.py`` holds only the tests
that call ``dispatch()`` directly. What this module proves, in the order the route
itself checks:

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
4. AC 9's three check-order refusals, each asserting the *first* refusal wins when
   two conditions are simultaneously false.
5. AC 5 and AC 6: the mutate and read operations, end to end through the real
   route.

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
from conftest import ClusterSession, InstallSpike, MakeWorkspace
from harness.registry import add_member
from rheo_app_core import internal_routes
from rheo_app_core.main import internal_app
from rheo_contracts import Role
from rheo_core.boundary.context import (
    CONTEXT_REQUIRED,
    SESSION_MISSING,
    SESSION_REVOKED,
    WORKSPACE_UNSELECTED,
)
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
from rheo_spike.manifest import MANIFEST
from rheo_spike.operations import NOTE_ADD, NOTE_COMMIT_EARLY, NOTE_LIST
from rheo_spike.records import NoteRow, list_notes

pytestmark = pytest.mark.postgres

BASE_HOST = "example.test"
INTERNAL_SECRET_VALUE = "internal-operations-test-secret"
SPIKE_SURFACE = MANIFEST.web
assert SPIKE_SURFACE is not None, "the spike manifest declares a web surface"

UNKNOWN_OPERATION = "spike.note.missing"
"""A well-formed operation name the spike module does not declare. Deliberately in
the ``spike`` module's own namespace: the point of AC 9's first case is that the
*unknown* check wins over the *module* check when both would refuse, so the name has
to name a module that really is disabled in the workspace under test."""


# --- fixtures and helpers -------------------------------------------------------------


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

    ``member`` rather than the workspace owner because AC 9's two role-order cases
    need a role ``spike.note.commit_early`` (roles ``{owner}``) refuses, and the
    fixture owner would be permitted.
    """
    account_id = add_member(
        cluster.backend, workspace_id, Role.MEMBER, display_name=label
    )
    return _session_for(account_id, workspace_id)


def _notes(cluster: ClusterSession, workspace_id: UUID) -> tuple[NoteRow, ...]:
    row = cluster.registry_row(workspace_id)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        return list_notes(uow.connection, limit=200)


@pytest.fixture
def spike_workspace(workspace: UUID, install_spike: InstallSpike) -> UUID:
    """A provisioned workspace with ``modules/spike`` installed and enabled. Asking
    for this fixture is also what registers the module's operations in this process,
    so every test below that names a ``spike.*`` operation requests it."""
    install_spike(workspace)
    return workspace


@pytest.fixture
def plain_workspace(spike_workspace: UUID, make_workspace: MakeWorkspace) -> UUID:
    """A second provisioned workspace the spike was **never installed into**, while
    the module itself is registered in this process (``spike_workspace`` did that).

    That combination is what makes ``module_disabled`` a real refusal rather than an
    absence: the operation exists and is found, and the workspace still refuses it.
    """
    return make_workspace()


@pytest.fixture
def owner_session(spike_workspace: UUID, owner_account_id: UUID) -> str:
    return _session_for(owner_account_id, spike_workspace)


# --- the trust boundary: the shared secret --------------------------------------------


async def test_no_internal_secret_is_401_before_the_session_is_even_read(
    cluster: ClusterSession, spike_workspace: UUID, owner_session: str
) -> None:
    """The route hangs off the listener's ``router``, so ``require_internal_secret``
    is a route dependency and runs before the handler body. A perfectly good session
    plus no shared secret is still 401, and nothing is written."""
    response = await _post(
        NOTE_ADD,
        None,
        {"body": "should never be written"},
        headers={"X-Rheo-Session": owner_session, "X-Rheo-Host": BASE_HOST},
    )
    assert response.status_code == 401
    assert _notes(cluster, spike_workspace) == ()


async def test_a_wrong_internal_secret_is_401(
    cluster: ClusterSession, spike_workspace: UUID, owner_session: str
) -> None:
    response = await _post(
        NOTE_ADD,
        None,
        {"body": "should never be written"},
        headers={
            "X-Rheo-Internal": "not-the-secret",
            "X-Rheo-Session": owner_session,
            "X-Rheo-Host": BASE_HOST,
        },
    )
    assert response.status_code == 401
    assert _notes(cluster, spike_workspace) == ()


# --- the trust boundary: every session refusal is a 401, before dispatch --------------


async def test_no_session_header_is_401_session_missing(spike_workspace: UUID) -> None:
    response = await _post(NOTE_ADD, None, {"body": "x"})
    assert response.status_code == 401
    assert response.json()["state"] == SESSION_MISSING


async def test_a_malformed_session_header_is_401_session_missing(
    spike_workspace: UUID,
) -> None:
    """A non-hex ``X-Rheo-Session`` resolves the same as no session at all — it
    never reaches ``bytes.fromhex``'s ``ValueError`` as a 500."""
    response = await _post(NOTE_ADD, "not-hex", {"body": "x"})
    assert response.status_code == 401
    assert response.json()["state"] == SESSION_MISSING


async def test_a_revoked_session_is_401_session_revoked(
    cluster: ClusterSession, spike_workspace: UUID, owner_account_id: UUID
) -> None:
    row = create_session(owner_account_id)
    secret = mint_host_secret(row.id, BASE_HOST)
    assert switch_workspace(row.id, spike_workspace) is None
    revoke(row.id)
    response = await _post(NOTE_ADD, secret.hex(), {"body": "should not be written"})
    assert response.status_code == 401
    assert response.json()["state"] == SESSION_REVOKED
    assert _notes(cluster, spike_workspace) == ()


async def test_a_session_with_no_workspace_is_401_workspace_unselected(
    spike_workspace: UUID, owner_account_id: UUID
) -> None:
    """Every browser session is created with ``active_workspace_id = NULL`` and only
    ``switch_workspace`` ever sets it (CF1, a ratified scope cut). The route refuses
    that session by name rather than dispatching into a missing workspace."""
    session_value = _session_for(owner_account_id, None)
    response = await _post(NOTE_ADD, session_value, {"body": "x"})
    assert response.status_code == 401
    assert response.json()["state"] == WORKSPACE_UNSELECTED


async def test_a_session_refusal_never_reaches_the_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
    spike_workspace: UUID,
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
        response = await _post(NOTE_ADD, session_value, {"body": "x"})
        assert response.status_code == 401
        # The boundary factory's own state, never the dispatcher's.
        assert response.json()["state"] != CONTEXT_REQUIRED
    assert seen == [], "a session refusal reached dispatch()"

    ok = await _post(NOTE_ADD, owner_session, {"body": "the positive control"})
    assert ok.status_code == 200
    assert len(seen) == 1


# --- AC 13: server-derived routing ----------------------------------------------------


async def test_a_workspace_naming_payload_is_ignored(
    cluster: ClusterSession,
    install_spike: InstallSpike,
    spike_workspace: UUID,
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
    other = make_workspace()
    install_spike(other)
    naming_b = {
        "workspace_id": str(other),
        "workspace": str(other),
        "database": f"rheo_ws_{other.hex}",
        "dsn": "postgresql://elsewhere/other",
        "connection_string": "postgresql://elsewhere/other",
        "schema": "spike",
    }
    response = await _post(
        NOTE_ADD,
        owner_session,
        {"body": "lands in A", **naming_b},
        params={key: value for key, value in naming_b.items()},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == SUCCEEDED

    in_a = _notes(cluster, spike_workspace)
    assert [row.body for row in in_a] == ["lands in A"]
    assert _notes(cluster, other) == ()


# --- AC 9: the dispatcher's check order, through the real route -----------------------


async def test_unknown_operation_refuses_before_the_module_check(
    plain_workspace: UUID, owner_account_id: UUID
) -> None:
    """Two conditions false at once: the operation is not registered, **and** the
    ``spike`` module it names is not enabled in this workspace. ``operation_unknown``
    wins, because ``authorize`` looks the operation up before it can know which
    module owns it. A check order that derived the module id from the name string
    would answer ``module_disabled`` here."""
    response = await _post(
        UNKNOWN_OPERATION,
        _session_for(owner_account_id, plain_workspace),
        {"body": "x"},
    )
    assert response.json()["state"] == OPERATION_UNKNOWN


async def test_disabled_module_refuses_before_the_role_check(
    cluster: ClusterSession, plain_workspace: UUID
) -> None:
    """Two conditions false at once: the ``spike`` module is not enabled in this
    workspace, **and** the caller's ``member`` role is not one
    ``spike.note.commit_early`` (roles ``{owner}``) permits. ``module_disabled``
    wins."""
    session_value = _member_session(cluster, plain_workspace, "member-disabled-case")
    response = await _post(NOTE_COMMIT_EARLY, session_value, {"body": "x"})
    assert response.json()["state"] == MODULE_DISABLED


async def test_role_refuses_after_the_module_check(
    cluster: ClusterSession, spike_workspace: UUID
) -> None:
    """The enabled direction of the same pair: the module check now passes, so the
    role check is reached and refuses. ``spike.note.commit_early`` is the only
    reachable driver — ``spike.note.add`` permits ``{owner, member}`` and
    ``spike.note.list`` permits ``{owner, member, operator}``, so neither can ever
    produce this refusal from a session."""
    session_value = _member_session(cluster, spike_workspace, "member-role-case")
    response = await _post(NOTE_COMMIT_EARLY, session_value, {"body": "x"})
    assert response.status_code == 403
    assert response.json()["state"] == ROLE_NOT_PERMITTED


# --- AC 5 and AC 6: the two real operations through the boundary ----------------------


async def test_add_note_through_the_internal_boundary(
    cluster: ClusterSession, spike_workspace: UUID, owner_session: str
) -> None:
    """AC 5, at the layer pytest can reach without Next.js: the envelope's own
    ``state`` is the same ``succeeded`` the web route handler puts in its 303's
    ``?add=`` parameter."""
    response = await _post(NOTE_ADD, owner_session, {"body": "through the boundary"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == SUCCEEDED
    assert body["operation_id"] is None
    assert body["result"]["body"] == "through the boundary"
    assert body["result"]["ref"].startswith("spike.note:")

    rows = _notes(cluster, spike_workspace)
    assert len(rows) == 1
    assert rows[0].body == "through the boundary"


async def test_list_notes_through_the_internal_boundary(
    spike_workspace: UUID, owner_session: str
) -> None:
    """AC 6: the read operation over the same session, reading back what the mutate
    operation wrote a request earlier."""
    empty = await _post(NOTE_LIST, owner_session, {})
    assert empty.status_code == 200, empty.text
    assert empty.json()["result"] == {"notes": []}

    assert (await _post(NOTE_ADD, owner_session, {"body": "first"})).status_code == 200
    assert (await _post(NOTE_ADD, owner_session, {"body": "second"})).status_code == 200

    listed = await _post(NOTE_LIST, owner_session, {"limit": 10})
    assert listed.status_code == 200, listed.text
    assert listed.json()["state"] == SUCCEEDED
    bodies = [note["body"] for note in listed.json()["result"]["notes"]]
    assert sorted(bodies) == ["first", "second"]


async def test_input_validation_refuses_through_the_route(
    cluster: ClusterSession, spike_workspace: UUID, owner_session: str
) -> None:
    """The envelope carries a refusal the same way it carries a success, and an
    empty body never becomes a row: ``input_invalid`` maps to 422 through the
    shared ``outcome_status``."""
    response = await _post(NOTE_ADD, owner_session, {"body": ""})
    assert response.status_code == 422
    assert response.json()["state"] == "input_invalid"
    assert "body" in response.json()["error"]["error_text"]
    assert _notes(cluster, spike_workspace) == ()


# --- D-6: the loaded module's surface reaches the routing configuration -----------


async def test_the_routing_config_carries_the_loaded_module_surface(
    spike_workspace: UUID,
) -> None:
    """D-6, end to end over the listener's own route.

    ``modules`` is the one part of ``RoutingConfig`` no settings key backs: it is
    whatever the manifests this process loaded declared. ``routing_config()``
    converts each ``WebSurface`` into a ``SurfaceConfig`` at the point of use, so
    ``rheo_core.modules`` never gains a dependency on ``rheo_core.routing``. Asking
    for ``spike_workspace`` is what loads the module.

    The six named surfaces are asserted unchanged in the same breath: the argument
    is additive, and a deployment that loaded no module must still serialise exactly
    what it serialised before the argument existed (which is what keeps
    ``tests/fixtures/routing/*.json`` and ``tests/test_routing.py`` untouched).
    """
    async with _client() as client:
        response = await client.get(
            "/internal/v1/routing",
            headers={"X-Rheo-Internal": INTERNAL_SECRET_VALUE},
        )
    assert response.status_code == 200, response.text
    surfaces = response.json()["surfaces"]
    assert surfaces["modules"]["spike"]["host"] == SPIKE_SURFACE.host
    assert surfaces["modules"]["spike"]["path"] == SPIKE_SURFACE.path
    assert set(surfaces) == {
        "shell",
        "identity",
        "api",
        "mcp",
        "docs",
        "integration",
        "modules",
    }

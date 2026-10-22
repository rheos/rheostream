"""C8: the bearer ``api`` surface's envelope and status mapping
(``POST /api/v1/operations/{name}``).

Drives ``apps/core/src/rheo_app_core/api_routes.py`` in-process over
``httpx.ASGITransport`` (the pattern ``tests/test_healthz.py`` and
``tests/postgres/test_sessions.py`` already establish): no Authorization
header and a malformed one both refuse ``token_malformed`` (401); a valid
token succeeds (200); ``role_not_permitted``/``operation_not_permitted`` (403);
``not_found`` (404); ``input_invalid`` (422); ``operation_id`` is ``null`` for
every operation shipped in release one and carries the minted id for the one
``long_running`` declaration in the tree. Also B2's HTTP channels (plan.md's
own line for this chunk): a reserved field naming another workspace,
presented in the JSON body, is silently dropped and the dispatch lands in the
token's own (authenticated) workspace, and presented in the query string is
refused before dispatch (issue #289: operation input is body-only) -- not a
distinct acceptance-criteria row in ``00-index.md``'s matrix (which lists B2
as fully claimed by 0b1), but
named explicitly by both ``plan.md``'s C8 line ("B2's HTTP JSON-body and
query-string channels (the api surface)") and ``spec.md``'s own B2 bullet
("the HTTP channels are C8"); proved here in favour of the canonical
documents over the matrix's silence, per ``00-index.md``'s own rule that a
canonical document wins a disagreement.

With Current installed, the ``alert_writer`` set over this route: its token creates
and closes by source, is refused every other operation, is audited as the token,
and is refused 401 once revoked or expired. A ``read_only`` token reads Current's
list and get over the same route.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.records import ensure_note_table, get_note, list_notes
from harness.registry import (
    NOTE_SCHEDULE,
    NOTE_WRITE,
    enable_harness_module,
    register_harness,
)
from rheo_app_core import api_routes
from rheo_app_core.main import public_app
from rheo_app_core.startup import CONSUMERS
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.operations import OPERATION_GET, dispatch, register_core_operations
from rheo_core.operations.core_ops import TOKEN_ISSUE, TOKEN_REVOKE
from rheo_core.operations.registry import REGISTRY
from rheo_core.storage import control_tables as t
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import audit_record
from sqlalchemy import inspect, select, update

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()


def _owner_ctx(workspace: UUID, owner_account_id: UUID) -> WorkspaceContext:
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _mint(
    ctx: WorkspaceContext, *, kind: str = "cli", set_name: str = "cli_full"
) -> tuple[str, UUID]:
    """Issue a token; returns its presented value and its id."""
    outcome = dispatch(ctx, TOKEN_ISSUE, {"kind": kind, "set_name": set_name})
    assert outcome.ok, outcome
    assert outcome.result is not None
    return outcome.result.value, outcome.result.token_id  # type: ignore[attr-defined]


async def _post(
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json: object = None,
    params: object = None,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public_app), base_url="http://test"
    ) as client:
        return await client.post(path, headers=headers or {}, json=json, params=params)


# --- token refusals: 401 -----------------------------------------------------


async def test_no_authorization_header_refused_401() -> None:
    resp = await _post("/api/v1/operations/core.workspace.status")
    assert resp.status_code == 401
    body = resp.json()
    assert body["state"] == "token_malformed"
    assert body["operation_id"] is None
    assert body["error"]["error_code"] == "token_malformed"


async def test_wrong_scheme_authorization_refused_401() -> None:
    resp = await _post(
        "/api/v1/operations/core.workspace.status",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp.status_code == 401
    assert resp.json()["state"] == "token_malformed"


async def test_garbage_bearer_refused_401() -> None:
    resp = await _post(
        "/api/v1/operations/core.workspace.status",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401
    assert resp.json()["state"] == "token_malformed"


# --- success: 200, and a null operation_id for every shipped operation ------


async def test_valid_token_succeeds_200_with_null_operation_id(
    workspace: UUID, owner_account_id: UUID
) -> None:
    value, _ = _mint(_owner_ctx(workspace, owner_account_id))
    resp = await _post(
        "/api/v1/operations/core.workspace.status",
        headers={"Authorization": f"Bearer {value}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "succeeded"
    assert body["operation_id"] is None
    assert "modules" in body["result"]


async def test_the_route_dispatches_with_the_process_consumer_registry(
    monkeypatch: pytest.MonkeyPatch, workspace: UUID, owner_account_id: UUID
) -> None:
    """This surface hands ``dispatch()`` the registry ``startup.py`` built, by
    identity — so a handler that publishes reaches the subscriptions module loading
    registered into that same object.

    Spied on the dispatcher rather than inferred, the way
    ``tests/postgres/test_internal_operations.py`` already spies on its own route's:
    nothing in this run publishes, so there is no downstream effect to read the
    wiring off, and the call is the observable. The spy wraps the real function, so
    the 200 below is the positive control that keeps the identity assertion from
    passing over a route that was never reached.
    """
    seen: list[object] = []
    real = api_routes.dispatch

    def _spy(ctx: object, name: str, payload: Any = None, **kwargs: Any) -> Any:
        seen.append(kwargs.get("consumers"))
        return real(ctx, name, payload, **kwargs)

    monkeypatch.setattr(api_routes, "dispatch", _spy)

    value, _ = _mint(_owner_ctx(workspace, owner_account_id))
    resp = await _post(
        "/api/v1/operations/core.workspace.status",
        headers={"Authorization": f"Bearer {value}"},
    )

    assert resp.status_code == 200, resp.text
    assert len(seen) == 1
    assert seen[0] is CONSUMERS


# --- role_not_permitted / operation_not_permitted: 403 -----------------------


async def test_role_not_permitted_403(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """A token's operation_set is a snapshot, frozen at issuance; the role
    the registry checks it against is read fresh, at every dispatch. Mint an
    owner's ``cli_full`` token (its snapshot carries ``core.settings.set``,
    owner-only) and then demote the same account to ``member`` -- the
    snapshot is unchanged, but the role check now refuses it, distinct from
    ``operation_not_permitted`` (which fires when the operation is not even
    in the snapshot, proved separately below).
    """
    value, _ = _mint(_owner_ctx(workspace, owner_account_id))
    with cluster.backend.control_engine.begin() as connection:
        connection.execute(
            update(t.membership)
            .where(
                t.membership.c.account_id == owner_account_id,
                t.membership.c.workspace_id == workspace,
            )
            .values(role=Role.MEMBER.value)
        )
    resp = await _post(
        "/api/v1/operations/core.settings.set",
        headers={"Authorization": f"Bearer {value}"},
        json={"key": "identity.token_max_days.cli", "value": 45},
    )
    assert resp.status_code == 403
    assert resp.json()["state"] == "role_not_permitted"


async def test_operation_not_permitted_403(
    workspace: UUID, owner_account_id: UUID
) -> None:
    value, _ = _mint(_owner_ctx(workspace, owner_account_id), set_name="read_only")
    resp = await _post(
        "/api/v1/operations/core.settings.set",
        headers={"Authorization": f"Bearer {value}"},
        json={"key": "identity.token_max_days.cli", "value": 45},
    )
    assert resp.status_code == 403
    assert resp.json()["state"] == "operation_not_permitted"


# --- not_found: 404 -----------------------------------------------------------


async def test_not_found_404(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    value, _ = _mint(_owner_ctx(workspace, owner_account_id), set_name="cli_full")
    made_up_ref = "harness.note:00000000-0000-7000-8000-000000000000"
    resp = await _post(
        "/api/v1/operations/harness.note.get",
        headers={"Authorization": f"Bearer {value}"},
        json={"ref": made_up_ref},
    )
    assert resp.status_code == 404
    assert resp.json()["state"] == "not_found"


# --- input_invalid: 422 -------------------------------------------------------


async def test_input_invalid_422(workspace: UUID, owner_account_id: UUID) -> None:
    value, _ = _mint(_owner_ctx(workspace, owner_account_id))
    resp = await _post(
        "/api/v1/operations/core.settings.set",
        headers={"Authorization": f"Bearer {value}"},
        json={"key": "identity.token_max_days.cli"},  # missing "value"
    )
    assert resp.status_code == 422
    assert resp.json()["state"] == "input_invalid"


# --- AC 20: the envelope's operation_id, on this surface --------------------


async def test_a_long_running_dispatch_carries_its_minted_id_in_the_envelope(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """AC 20, bearer half: a ``long_running`` dispatch answers with the minted id,
    and that id names a record the supported read finds still ``pending``.

    The envelope's ``operation_id`` becoming a nullable uuid is reachable only
    through a ``long_running`` declaration, and ``harness.note.schedule`` is the
    tree's only one — no shipped ``core.*`` operation carries the flag, which is why
    every other test in this file still asserts ``null`` and stays correct.
    """
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    ctx = _owner_ctx(workspace, owner_account_id)
    value, _ = _mint(ctx)

    resp = await _post(
        f"/api/v1/operations/{NOTE_SCHEDULE}",
        headers={"Authorization": f"Bearer {value}"},
        json={"body": "queued over the bearer surface"},
    )

    body = resp.json()
    assert body["state"] == "pending", body
    minted = body["operation_id"]
    assert minted is not None, body
    # Read back through the supported operation, never the table (AC 19). No worker
    # has visited this workspace, so the record is still where the dispatcher left
    # it.
    read = dispatch(ctx, OPERATION_GET, {"operation_id": minted})
    assert read.ok, read
    assert read.result is not None
    assert read.result.state == "pending"  # type: ignore[attr-defined]
    assert read.result.name == NOTE_SCHEDULE  # type: ignore[attr-defined]

    # 202 Accepted, and the handler's own output carried rather than dropped. A
    # ``pending`` outcome is success-shaped: it has a ``result`` and no ``error``, so
    # the listener takes the result branch through ``api_routes.carries_result``
    # rather than through ``OperationOutcome.ok``, which is ``succeeded``-only and
    # would send this response — a *successful* long-running dispatch — down the
    # error path at 400 with neither key set.
    assert resp.status_code == 202, resp.text
    assert "error" not in body, body
    assert body["result"]["operation_id"] == minted, body
    assert body["result"]["job_id"] is not None, body


# --- B2's HTTP channels: reserved fields are ignored, either way ------------


async def test_reserved_field_in_body_is_ignored_and_in_query_string_is_refused(
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """Criterion 6 over HTTP. The query-string channel is closed (issue #289): a
    request naming another workspace there is refused ``input_invalid`` before
    dispatch and writes nothing. The body channel still drops the reserved keys,
    and the write lands in the token's own workspace."""
    other = make_workspace()
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    value, _ = _mint(_owner_ctx(workspace, owner_account_id), set_name="cli_full")
    refused = await _post(
        f"/api/v1/operations/{NOTE_WRITE}",
        headers={"Authorization": f"Bearer {value}"},
        params={"workspace_id": str(other), "database": "not-consulted"},
        json={"body": "never written"},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json() == {
        "state": "input_invalid",
        "operation_id": None,
        "error": {
            "error_code": "input_invalid",
            "error_text": api_routes.QUERY_REFUSED_TEXT,
        },
    }
    assert str(other) not in refused.text
    resp = await _post(
        f"/api/v1/operations/{NOTE_WRITE}",
        headers={"Authorization": f"Bearer {value}"},
        json={
            "body": "written through the http api surface",
            "workspace_id": str(other),
            "database": "not-consulted",
            "dsn": "ignored-too",
        },
    )
    assert resp.status_code == 200, resp.json()
    ref = resp.json()["result"]["ref"]
    note_id = UUID(ref.rsplit(":", 1)[1])
    with UnitOfWork(engine, row.database_name) as uow:
        note = get_note(uow.connection, note_id)
    assert note is not None and note.body == "written through the http api surface"
    other_row = cluster.registry_row(other)
    other_engine = cluster.backend.pools.engine_for(other_row.database_name)
    with UnitOfWork(other_engine, other_row.database_name) as other_uow:
        # The other workspace never had harness.note.write dispatched against
        # it, so the table itself may not exist there yet -- that absence is
        # exactly the proof the write did not land in it.
        has_table = inspect(other_uow.connection).has_table("note", schema="harness")
        assert not has_table or get_note(other_uow.connection, note_id) is None


@dataclass(frozen=True)
class CurrentOnTheRoute:
    owner: WorkspaceContext
    workspace: UUID


@pytest.fixture
def current_on_the_route(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[CurrentOnTheRoute]:
    """Current installed and enabled in ``workspace``, with its operations in a copy
    of the process-wide registry the route dispatches through.

    The loader recipe puts Current's operations in a local registry, and the route
    reads the global one, so the loaded operations are registered again into a
    ``monkeypatch`` copy of it. The original goes back at teardown, so the exact-set
    assertions in ``tests/postgres/test_tokens.py`` never see Current.
    """
    with loaded_probe_modules(monkeypatch, "current") as surfaces:
        operator = context_for_operator(workspace)
        assert isinstance(operator, WorkspaceContext), operator
        install_and_enable_module(cluster.backend, operator, workspace, "current")
        monkeypatch.setattr(REGISTRY, "_operations", dict(REGISTRY._operations))
        for name in surfaces.operations.names():
            loaded = surfaces.operations.lookup(name)
            assert loaded is not None
            REGISTRY.register(loaded.declaration, loaded.handler, origin=loaded.origin)
        yield CurrentOnTheRoute(_owner_ctx(workspace, owner_account_id), workspace)


def _owner_item(owner: WorkspaceContext) -> str:
    """One open item, created by the owner directly; returns its ref."""
    item = dispatch(
        owner,
        "current.item.create",
        {"title": "Synthetic task"},
        consumers=CONSUMERS,
    )
    assert item.ok, item
    return item.result.item.ref  # type: ignore[union-attr]


ALERT = {
    "title": "Synthetic probe down",
    "body": "Type: infra-alert",
    "priority": "critical",
    "area": "dev",
    "provenance": "alert",
    "source_kind": "health-probe",
    "source_ref": "health-probe:example",
    "fingerprint": "down",
}
SOURCE_PAIR = {"source_kind": "health-probe", "source_ref": "health-probe:example"}


async def test_an_alert_writer_token_creates_and_closes_and_is_audited_as_the_token(
    current_on_the_route: CurrentOnTheRoute,
) -> None:
    value, token_id = _mint(current_on_the_route.owner, set_name="alert_writer")
    headers = {"Authorization": f"Bearer {value}"}

    created = await _post(
        "/api/v1/operations/current.item.create", headers=headers, json=ALERT
    )
    assert created.status_code == 200, created.text
    assert created.json()["state"] == "succeeded"
    assert created.json()["result"]["action"] == "created"
    ref = created.json()["result"]["ref"]

    closed = await _post(
        "/api/v1/operations/current.item.close_by_source",
        headers=headers,
        json=SOURCE_PAIR,
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["state"] == "succeeded"
    assert closed.json()["result"] == {"closed": True, "ref": None}
    got = dispatch(current_on_the_route.owner, "current.item.get", {"ref": ref})
    assert got.ok and got.result.status == "done"  # type: ignore[union-attr]

    # Both calls are audited as the token, on the api entry, not as the owner.
    with open_unit_of_work(current_on_the_route.owner) as uow:
        rows = uow.connection.execute(
            select(
                audit_record.c.operation_name,
                audit_record.c.actor_kind,
                audit_record.c.actor_id,
                audit_record.c.entry,
                audit_record.c.outcome,
            ).where(audit_record.c.operation_name.like("current.item.%"))
        ).all()
    assert sorted(tuple(row) for row in rows) == [
        ("current.item.close_by_source", "token", token_id, "api", "succeeded"),
        ("current.item.create", "token", token_id, "api", "succeeded"),
    ]


async def test_an_alert_writer_token_is_refused_every_other_operation(
    current_on_the_route: CurrentOnTheRoute,
) -> None:
    owner = current_on_the_route.owner
    ref = _owner_item(owner)
    value, _ = _mint(owner, set_name="alert_writer")
    refused = {
        "current.item.list": {},
        "current.item.update": {"ref": ref, "revision": 1, "title": "Changed"},
        "current.item.set_status": {"ref": ref, "revision": 1, "status": "done"},
        "current.item.comment": {"ref": ref, "body": "A comment"},
        "core.record.delete": {"ref": ref},
        "core.workspace.status": {},
    }
    for name, payload in refused.items():
        resp = await _post(
            f"/api/v1/operations/{name}",
            headers={"Authorization": f"Bearer {value}"},
            json=payload,
        )
        assert resp.status_code == 403, (name, resp.text)
        assert resp.json()["state"] == "operation_not_permitted", name
    after = dispatch(owner, "current.item.get", {"ref": ref})
    assert after.ok, after
    assert after.result.status == "open" and after.result.revision == 1  # type: ignore[union-attr]


async def test_a_revoked_and_an_expired_alert_writer_token_are_refused_401(
    cluster: ClusterSession, current_on_the_route: CurrentOnTheRoute
) -> None:
    owner = current_on_the_route.owner
    revoked, revoked_id = _mint(owner, set_name="alert_writer")
    expired, expired_id = _mint(owner, set_name="alert_writer")
    assert dispatch(owner, TOKEN_REVOKE, {"token_id": str(revoked_id)}).ok
    with cluster.backend.control_engine.begin() as connection:
        connection.execute(
            update(t.access_token)
            .where(t.access_token.c.id == expired_id)
            .values(expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    for value, state in ((revoked, "token_revoked"), (expired, "token_expired")):
        resp = await _post(
            "/api/v1/operations/current.item.create",
            headers={"Authorization": f"Bearer {value}"},
            json=ALERT,
        )
        assert resp.status_code == 401, (state, resp.text)
        assert resp.json()["state"] == state
    listed = dispatch(owner, "current.item.list", {"status": "all"})
    assert listed.ok and listed.result.items == []  # type: ignore[union-attr]


SUPPORT = {"source_kind": "support.form"}
ITEM_CONTENT = {
    "item",
    "title",
    "body",
    "comments",
    "provenance",
    "source_kind",
    "source_ref",
    "blockers",
    "blocking",
    "status",
}


def _support_pair(number: int) -> dict[str, str]:
    """A sequential pair, the kind a writer token could enumerate."""
    return {**SUPPORT, "source_ref": f"support-request-{number}"}


def _owner_creates(owner: WorkspaceContext, pair: dict[str, str]) -> Any:
    """An item holding ``pair``, with a body and a comment, created by the owner."""
    made = dispatch(
        owner,
        "current.item.create",
        {"title": "Synthetic support request", "body": "Type: support-ticket", **pair},
        consumers=CONSUMERS,
    )
    assert made.ok, made
    item = made.result.item  # type: ignore[union-attr]
    commented = dispatch(
        owner,
        "current.item.comment",
        {"ref": item.ref, "body": "Synthetic private note"},
    )
    assert commented.ok, commented
    return item


def _owner_sets_status(owner: WorkspaceContext, ref: str, status: str) -> None:
    got = dispatch(owner, "current.item.get", {"ref": ref})
    assert got.ok, got
    payload: dict[str, object] = {
        "ref": ref,
        "revision": got.result.revision,  # type: ignore[union-attr]
        "status": status,
    }
    if status == "snoozed":
        payload["snoozed_until"] = "2099-01-01T09:00:00+00:00"
    moved = dispatch(owner, "current.item.set_status", payload, consumers=CONSUMERS)
    assert moved.ok, moved


async def test_an_alert_writer_create_returns_only_the_action_and_the_ref(
    current_on_the_route: CurrentOnTheRoute,
) -> None:
    """Every create path (created, grouped, reopened, woke) answers a writer token
    with ``{action, ref}``: creating with a known pair never reads the item."""
    owner = current_on_the_route.owner
    existing = {
        "grouped": _owner_creates(owner, _support_pair(1)).ref,
        "reopened": _owner_creates(owner, _support_pair(2)).ref,
        "woke": _owner_creates(owner, _support_pair(3)).ref,
    }
    _owner_sets_status(owner, existing["reopened"], "done")
    _owner_sets_status(owner, existing["woke"], "snoozed")
    value, _ = _mint(owner, set_name="alert_writer")
    headers = {"Authorization": f"Bearer {value}"}

    for number, action in enumerate(("grouped", "reopened", "woke", "created"), 1):
        resp = await _post(
            "/api/v1/operations/current.item.create",
            headers=headers,
            json={"title": "Probe", **_support_pair(number)},
        )
        assert resp.status_code == 200, resp.text
        result = resp.json()["result"]
        assert set(result) == {"action", "ref"}, (action, result)
        assert not set(result) & ITEM_CONTENT
        assert result["action"] == action
        if action in existing:
            assert result["ref"] == existing[action]
        assert "Synthetic private note" not in resp.text
        assert "support-ticket" not in resp.text


async def test_an_alert_writer_close_by_source_never_returns_a_ref(
    current_on_the_route: CurrentOnTheRoute,
) -> None:
    """A done item, an open item and a pair nobody holds all answer ``ref: null``,
    so the call cannot test whether a pair exists."""
    owner = current_on_the_route.owner
    done = _owner_creates(owner, _support_pair(1)).ref
    _owner_sets_status(owner, done, "done")
    still_open = _owner_creates(owner, _support_pair(2)).ref
    value, _ = _mint(owner, set_name="alert_writer")

    answers = []
    for number in (1, 2, 3):
        resp = await _post(
            "/api/v1/operations/current.item.close_by_source",
            headers={"Authorization": f"Bearer {value}"},
            json=_support_pair(number),
        )
        assert resp.status_code == 200, resp.text
        answers.append(resp.json()["result"])
    assert answers == [
        {"closed": False, "ref": None},
        {"closed": True, "ref": None},
        {"closed": False, "ref": None},
    ]
    got = dispatch(owner, "current.item.get", {"ref": still_open})
    assert got.ok and got.result.status == "done"  # type: ignore[union-attr]


async def test_an_owner_session_and_a_reading_token_keep_the_full_results(
    current_on_the_route: CurrentOnTheRoute,
) -> None:
    """Callers that can read items see what they saw before: create returns
    ``{action, item}`` with the full detail, and close by source returns the ref."""
    owner = current_on_the_route.owner
    session = dispatch(
        owner,
        "current.item.create",
        {"title": "Synthetic support request", **_support_pair(1)},
        consumers=CONSUMERS,
    )
    assert session.ok, session
    dumped = session.result.model_dump(mode="json")  # type: ignore[union-attr]
    assert list(dumped) == ["action", "item"]
    assert dumped["item"]["source_ref"] == "support-request-1"
    closed = dispatch(
        owner, "current.item.close_by_source", _support_pair(1), consumers=CONSUMERS
    )
    assert closed.ok, closed
    assert closed.result.model_dump(mode="json") == {  # type: ignore[union-attr]
        "closed": True,
        "ref": dumped["item"]["ref"],
    }

    value, _ = _mint(owner, set_name="cli_full")
    headers = {"Authorization": f"Bearer {value}"}
    created = await _post(
        "/api/v1/operations/current.item.create",
        headers=headers,
        json={"title": "Synthetic support request", **_support_pair(2)},
    )
    assert created.status_code == 200, created.text
    result = created.json()["result"]
    assert list(result) == ["action", "item"]
    assert result["item"]["source_ref"] == "support-request-2"
    assert {"body", "comments", "provenance", "blockers"} <= set(result["item"])
    for closed_now in (True, False):
        resp = await _post(
            "/api/v1/operations/current.item.close_by_source",
            headers=headers,
            json=_support_pair(2),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == {
            "closed": closed_now,
            "ref": result["item"]["ref"],
        }


async def test_a_read_only_cli_token_reads_current_over_the_route(
    current_on_the_route: CurrentOnTheRoute,
) -> None:
    owner = current_on_the_route.owner
    ref = _owner_item(owner)
    value, _ = _mint(owner, set_name="read_only")
    headers = {"Authorization": f"Bearer {value}"}

    listed = await _post(
        "/api/v1/operations/current.item.list", headers=headers, json={}
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["state"] == "succeeded"
    assert [i["ref"] for i in listed.json()["result"]["items"]] == [ref]

    got = await _post(
        "/api/v1/operations/current.item.get", headers=headers, json={"ref": ref}
    )
    assert got.status_code == 200, got.text
    assert got.json()["state"] == "succeeded"
    assert got.json()["result"]["ref"] == ref


async def test_a_payload_field_in_the_query_string_is_refused(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """Issue #289: a real input field sent in the URL is not read. It used to be
    merged into the payload, so ``?body=...`` with an empty JSON body wrote a note.
    Now the request is refused 422 and no note exists afterwards, whatever the
    body says, and an empty query string (``?&``) counts as a query string."""
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    value, _ = _mint(_owner_ctx(workspace, owner_account_id), set_name="cli_full")
    headers = {"Authorization": f"Bearer {value}"}
    for path, body in (
        (f"/api/v1/operations/{NOTE_WRITE}?body=from-the-url", {}),
        (f"/api/v1/operations/{NOTE_WRITE}?body=from-the-url", {"body": "x"}),
        (f"/api/v1/operations/{NOTE_WRITE}?&", {"body": "x"}),
    ):
        resp = await _post(path, headers=headers, json=body)
        assert resp.status_code == 422, (path, resp.text)
        assert resp.json()["error"]["error_code"] == "input_invalid"
        assert "from-the-url" not in resp.text
    with UnitOfWork(engine, row.database_name) as uow:
        ensure_note_table(uow.connection)
        assert list_notes(uow.connection) == ()

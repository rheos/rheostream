"""The internal listener's routes and its shared-secret dependency (C7b, 0v).

Container-network only — compose never publishes port 8100 (11 asserts this with
``make demo``). Every route depends on :func:`require_internal_secret`, which
compares ``X-Rheo-Internal`` in constant time against the secret resolved fresh, at
request time, from ``internal.secret_ref`` through this component's own secret
scope (mirroring the storage/identity scope-construction pattern: a scope built
once here, a value resolved at the moment of use, never cached). This listener
reads only its own three headers — ``X-Rheo-Internal``, ``X-Rheo-Session``,
``X-Rheo-Host`` — and never a cookie: a valid ``rheo_session`` cookie sent
alongside a request with no ``X-Rheo-Session`` header authenticates nothing here.

Run 0v adds the third route, ``POST /internal/v1/operations/{name}`` — the web
tier's only way to reach ``dispatch()`` at all, and the one piece of that run that
outlives it (``docs/architecture/overview.md`` already specifies it; it was simply
never built). Three properties of that route are load-bearing rather than
stylistic, and each has its own test in
``tests/postgres/test_internal_operations.py``:

- it hangs off this module's ``router``, so :func:`require_internal_secret` gates
  it exactly as it gates the other two;
- **every** ``context_from_session`` refusal becomes a 401 *before* ``dispatch()``
  is called, so the dispatcher is never handed a non-context — which is why
  ``context_required`` is unreachable through HTTP and is proved directly in
  ``tests/test_boundary.py`` instead;
- it reads **no** workspace identifier from anywhere in the request. The workspace
  comes from the session row and nowhere else.
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from rheo_core.boundary.context import SESSION_MISSING, Refusal
from rheo_core.boundary.factories import context_from_session
from rheo_core.modules import module_surfaces
from rheo_core.operations import dispatch
from rheo_core.routing import RoutingConfig, SurfaceConfig
from rheo_core.secrets import SecretRef, SecretRefusal, SecretStore
from rheo_core.settings import resolve
from rheo_core.storage.control_plane import list_memberships
from rheo_core.storage.data_root import resolve_data_root
from rheo_core.storage.postgres import get_backend

from rheo_app_core.api_routes import envelope, outcome_status
from rheo_app_core.auth_routes import normalize_host

SESSION_REFUSAL_STATUS = 401

INTERNAL_SCOPE_COMPONENT = "internal"
INTERNAL_SCOPE_PREFIXES = (
    "secret://file/internal/",
    "secret://env/RHEO_INTERNAL_SECRET",
)

_SECRET_KEY = "internal.secret_ref"


def _resolved_internal_secret() -> bytes:
    settings = resolve()
    reference = SecretRef.parse(settings.get_str(_SECRET_KEY))
    store = SecretStore(resolve_data_root().path)
    scope = SecretStore.scope_for(INTERNAL_SCOPE_COMPONENT, *INTERNAL_SCOPE_PREFIXES)
    return store.resolve(reference, scope).expose()


def require_internal_secret(
    x_rheo_internal: str | None = Header(default=None, alias="X-Rheo-Internal"),
) -> None:
    """Refuse before either route below runs, unless ``X-Rheo-Internal`` matches
    the resolved secret in constant time. A missing header, or a secret that is
    not configured at all (the package default is an empty string), both refuse —
    neither ever falls back to trusting a cookie."""
    if not x_rheo_internal:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        expected = _resolved_internal_secret()
    except SecretRefusal as refusal:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from refusal
    if not expected or not hmac.compare_digest(
        x_rheo_internal.encode("utf-8"), expected
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


router = APIRouter(dependencies=[Depends(require_internal_secret)])


@router.get("/internal/v1/routing")
def routing_config() -> dict[str, object]:
    """The serialised :class:`RoutingConfig` (06's shape) — the contract 10 reads,
    proven byte-shape-compatible in ``00-index.md`` § Coupling seams.

    The ``modules`` surfaces are not settings (no key backs them, D-6): they are
    whatever the modules this process loaded declared, which
    ``rheo_core.modules.module_surfaces()`` kept from startup. The
    ``WebSurface -> SurfaceConfig`` conversion happens **here, at the point of
    use**, so ``rheo_core.modules`` never gains a dependency on
    ``rheo_core.routing``. A deployment that loaded no module gets the same empty
    mapping, and therefore the same bytes, it got before this argument existed.
    """
    modules = {
        surface: SurfaceConfig(host=web.host, path=web.path)
        for surface, web in module_surfaces().items()
    }
    return RoutingConfig.from_settings(resolve(), modules=modules).model_dump(
        mode="json"
    )


@router.get("/internal/v1/session")
def session_info(
    x_rheo_session: str | None = Header(default=None, alias="X-Rheo-Session"),
    x_rheo_host: str | None = Header(default=None, alias="X-Rheo-Host"),
) -> dict[str, object]:
    """The account, active workspace, role and memberships behind
    ``X-Rheo-Session``/``X-Rheo-Host``, or the boundary's refusal state — never a
    cookie, and never a workspace id in any URL (B3)."""
    if not x_rheo_session or not x_rheo_host:
        return {"state": "session_missing"}
    try:
        secret = bytes.fromhex(x_rheo_session)
    except ValueError:
        return {"state": "session_missing"}
    ctx = context_from_session(secret, normalize_host(x_rheo_host))
    if isinstance(ctx, Refusal):
        return {"state": ctx.state}
    # context_from_session (factories.py) always builds an account actor, never
    # token/operator/system/connection -- the same narrowing rheo_core.tokens.issue
    # makes at its own actor.id use.
    assert ctx.actor.id is not None
    backend = get_backend()
    with backend.control_engine.connect() as connection:
        memberships = list_memberships(connection, account_id=ctx.actor.id)
    return {
        "state": "ok",
        "actor": {"kind": ctx.actor.kind.value, "id": str(ctx.actor.id)},
        "active_workspace_id": str(ctx.workspace_id),
        "role": ctx.role.value,
        "memberships": [
            {"workspace_id": str(row.workspace_id), "role": row.role.value}
            for row in memberships
        ],
    }


def _session_refusal(state: str, detail: str) -> JSONResponse:
    """Every ``context_from_session`` refusal, as one 401 in the operation envelope.

    One status for all of them on purpose: the states differ
    (``session_missing``, ``session_expired``, ``session_revoked``,
    ``workspace_unselected``, ``membership_missing``, ``workspace_unavailable``),
    and the caller that needs to tell them apart reads ``state`` — but every one of
    them means the same thing to HTTP, that this request carried no usable session.
    """
    return JSONResponse(
        envelope(state, error_code=state, error_text=detail),
        status_code=SESSION_REFUSAL_STATUS,
    )


@router.post("/internal/v1/operations/{name}")
async def run_operation(
    name: str,
    request: Request,
    x_rheo_session: str | None = Header(default=None, alias="X-Rheo-Session"),
    x_rheo_host: str | None = Header(default=None, alias="X-Rheo-Host"),
) -> JSONResponse:
    """Dispatch one registered operation for the session behind ``X-Rheo-Session``.

    The web tier's only path to ``dispatch()``. ``docs/architecture/overview.md``
    (line 132) requires that this listener serve the operations the bearer surface
    serves, but it defines no path, headers or refusal mapping for them, so those
    are this run's own and are pinned here and in ``plan.md`` § C3. Answers with the
    shared operation envelope and the shared status mapping (``api_routes.envelope``
    / ``api_routes.outcome_status``), so the bearer ``api`` surface and this one
    cannot drift.

    Like ``api_routes.run_operation``, this coroutine calls the synchronous
    ``dispatch()`` directly rather than through a worker thread. That is the shipped
    pattern on the bearer surface and is copied deliberately rather than diverged
    from; it is a property of both surfaces, not of this one.

    **The context is built before anything else, and a refusal returns here.**
    ``dispatch()`` is called only with a real ``WorkspaceContext``, which is why
    its own ``context_required`` refusal is unreachable through HTTP — a property
    of this route being correct, not a gap in the tests.

    **No workspace identifier is read from the request.** The path carries the
    operation name; the query string and the JSON body carry the operation's own
    input; the workspace comes from the session row's ``active_workspace_id`` by
    way of ``context_from_session`` and from nowhere else. A payload that also
    names a workspace, database, DSN, connection string or schema has those keys
    dropped by the operation's input model — ``RESERVED_INPUT_FIELDS`` is refused
    at *registration*, so no registered operation can declare one to read — and the
    write still lands in the session's own workspace.

    Query first, body second (the body wins on a collision), exactly as
    ``api_routes.run_operation`` merges them: the same reserved-field drop, over
    the same models, on a second surface.
    """
    if not x_rheo_session or not x_rheo_host:
        return _session_refusal(
            SESSION_MISSING, "no X-Rheo-Session/X-Rheo-Host header pair was presented"
        )
    try:
        secret = bytes.fromhex(x_rheo_session)
    except ValueError:
        return _session_refusal(SESSION_MISSING, "X-Rheo-Session is not hex")
    ctx = context_from_session(secret, normalize_host(x_rheo_host))
    if isinstance(ctx, Refusal):
        return _session_refusal(ctx.state, str(ctx))
    payload: dict[str, object] = dict(request.query_params)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        payload.update(body)
    outcome = dispatch(ctx, name, payload)
    # Never ``status``: this module imports FastAPI's own ``status`` namespace for
    # require_internal_secret's HTTP_401_UNAUTHORIZED.
    status_code = outcome_status(outcome)
    if outcome.ok:
        result = (
            None if outcome.result is None else outcome.result.model_dump(mode="json")
        )
        return JSONResponse(
            envelope(outcome.state, result=result), status_code=status_code
        )
    error = outcome.error
    return JSONResponse(
        envelope(
            outcome.state,
            error_code=None if error is None else error.error_code,
            error_text=None if error is None else error.error_text,
        ),
        status_code=status_code,
    )

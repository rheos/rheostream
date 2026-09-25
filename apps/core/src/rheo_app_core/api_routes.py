"""The bearer ``api`` surface (C8): ``POST /api/v1/operations/{name}``.

Mounted on ``public_app`` (08's alias for 0a's ``app`` -- see ``00-index.md`` §
Coupling seams; 08 lands first, so the alias already exists). Resolves the
bearer via ``context_from_token(value, "api")``, merges the query string and
the JSON body into one payload (query first, body overrides -- B2's two HTTP
channels: ``operations/core_ops.py``'s and ``tests/harness/registry.py``'s own
input models already ignore any key they do not declare, including the
reserved fields ``RESERVED_INPUT_FIELDS`` names, so a workspace-naming key
arriving through either channel is silently dropped exactly as it is at a bare
``dispatch()`` call -- the same criterion 6 (B2) proof, over the HTTP surface),
dispatches, and returns the envelope ``{"state", "operation_id", "result"
| "error": {"error_code", "error_text"}}`` (plus ``approval_id`` on an
``approval_required`` hold) with the status mapping ``spec.md``
gives: 200 succeeded, 401 for every ``context_from_token`` refusal (including
a missing/malformed ``Authorization`` header, before any token is even looked
up), 403 ``operation_not_permitted``/``role_not_permitted``, 404 ``not_found``,
422 ``input_invalid``; anything else maps to 400. Run 0c2 adds **202
``pending``**, which is the answer a ``long_running`` dispatch gets: the work
is accepted and queued, not done, and 202 Accepted is exactly that.

``operation_id`` is a nullable uuid rather than the literal ``null`` it was
before run 0c2. Dispatching an operation whose declaration carries
``long_running = True`` writes a ``core.operation`` row and mints an id, and
that id is what this envelope carries; every other operation -- which is every
operation registered in release one -- still answers ``null``, and
``tests/postgres/test_api_surface.py`` asserts exactly that for the shipped
ones. So "no row is written and no id is minted anywhere on this path" is no
longer the invariant: the invariant is that the id is non-null for a
``long_running`` dispatch and null for every other.
"""

from typing import Final
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from rheo_core.boundary.context import TOKEN_MALFORMED, Refusal
from rheo_core.boundary.factories import context_from_token
from rheo_core.operations import (
    INPUT_INVALID,
    OPERATION_NOT_PERMITTED,
    PENDING,
    ROLE_NOT_PERMITTED,
    SUCCEEDED,
    OperationOutcome,
    dispatch,
)
from rheo_core.refs.resolver import NOT_FOUND

from rheo_app_core.startup import CONSUMERS

router = APIRouter()

_TOKEN_REFUSAL_STATUS: Final = 401
_STATUS_BY_STATE: Final[dict[str, int]] = {
    SUCCEEDED: 200,
    # 202 Accepted, and the reason it is not 200: a ``long_running`` dispatch has
    # queued the work and not done it, so a 200 would tell a caller the effect had
    # landed. This is the only non-2xx-by-default state in the map that is not a
    # refusal, which is why it needs an entry of its own — without one it falls
    # through to ``_DEFAULT_ERROR_STATUS`` and a successful dispatch answers 400.
    PENDING: 202,
    OPERATION_NOT_PERMITTED: 403,
    ROLE_NOT_PERMITTED: 403,
    NOT_FOUND: 404,
    INPUT_INVALID: 422,
}
_DEFAULT_ERROR_STATUS: Final = 400
_RESULT_STATES: Final = frozenset({SUCCEEDED, PENDING})


def _bearer(request: Request) -> str | None:
    """The value of a well-formed ``Authorization: Bearer <value>`` header, or
    ``None`` for anything else (missing, a different scheme, an empty value)."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value


def envelope(
    state: str,
    operation_id: UUID | None,
    *,
    approval_id: UUID | None,
    result: dict[str, object] | None = None,
    error_code: str | None = None,
    error_text: str | None = None,
) -> dict[str, object]:
    """The operation envelope both listeners answer with: ``{"state",
    "operation_id", "result" | "error"}``.

    Public, and shared: run 0v's ``POST /internal/v1/operations/{name}``
    (``internal_routes.py``) answers with this same shape over the same
    ``dispatch()``, so the two surfaces cannot drift apart by being written twice.
    The generated OpenAPI document (``rheo_core.operations.openapi``) describes
    exactly this envelope as the 200 response for every operation. It describes no
    202, and that is accurate for every operation the document contains: 202 is
    reachable only through a ``long_running`` declaration, and the only one in the
    tree is the test harness's, which is never registered in a shipped profile. A run
    that ships a ``long_running`` core operation has to add that response to the
    document in the same change.

    **``operation_id`` is required and has no default, deliberately.** It is the
    outcome's own ``operation_id`` at the four call sites that run after
    ``dispatch()`` has returned, and an explicit ``None`` at the three refusals that
    fire before it is ever called. A default would let this listener and
    ``internal_routes.py`` drift apart silently -- one passing the id, the other
    quietly keeping the default -- which is the exact failure a shared helper exists
    to prevent, and it is what makes AC 20 true on both surfaces at once instead of
    needing two independent proofs.

    **``approval_id`` is keyword-only with no default, for the same reason, and is
    in the body only when it is set** (issue #127). It is the outcome's own
    ``approval_id``, which ``dispatch`` sets on an ``approval_required`` hold and
    nowhere else, so criterion 19's identifier is a field a client reads rather
    than a uuid it parses out of ``error_text`` (which still names it in prose).
    Present-only-when-set rather than always ``null`` like ``operation_id``: an
    added key a reader may ignore, so no existing caller's body changes shape, and
    the key appearing is itself the signal that there is an approval to act on.
    The three refusals that fire before ``dispatch`` pass ``None`` explicitly.
    """
    body: dict[str, object] = {
        "state": state,
        "operation_id": None if operation_id is None else str(operation_id),
    }
    if approval_id is not None:
        body["approval_id"] = str(approval_id)
    if result is not None:
        body["result"] = result
    if error_code is not None:
        body["error"] = {"error_code": error_code, "error_text": error_text}
    return body


def outcome_status(outcome: OperationOutcome) -> int:
    """The HTTP status for a dispatch outcome. Public for the same reason
    :func:`envelope` is: the internal operations route maps outcomes identically,
    because the mapping belongs to the operation contract, not to one listener."""
    return _STATUS_BY_STATE.get(outcome.state, _DEFAULT_ERROR_STATUS)


def carries_result(outcome: OperationOutcome) -> bool:
    """Whether this outcome's body is a ``result`` rather than an ``error``.

    Public and shared for the same reason :func:`envelope` and :func:`outcome_status`
    are, and the third of the three for a sharper reason than symmetry.
    ``OperationOutcome.ok`` is ``state == "succeeded"``, which is the right question
    for a caller asking "did the effect land" and the wrong one for a listener
    choosing which half of the envelope to fill: a ``pending`` outcome carries a
    ``result`` and no ``error``, so a listener branching on ``ok`` sends the error
    branch for a *successful* long-running dispatch — 400, the handler's output
    dropped, and no ``error`` key either, because there is no error. Both listeners
    ask this instead, so neither can answer the other's opposite.

    Two states, and only two: ``succeeded`` and ``pending``. Every other state is a
    refusal or ``failed``, each of which carries an ``error``.
    """
    return outcome.state in _RESULT_STATES


@router.post("/api/v1/operations/{name}")
async def run_operation(name: str, request: Request) -> JSONResponse:
    value = _bearer(request)
    if value is None:
        return JSONResponse(
            # ``None``, not an outcome attribute: this fires before ``dispatch()`` is
            # called, so there is no outcome here to read one from.
            envelope(
                TOKEN_MALFORMED,
                None,
                approval_id=None,
                error_code=TOKEN_MALFORMED,
                error_text="no bearer token was presented",
            ),
            status_code=_TOKEN_REFUSAL_STATUS,
        )
    ctx = context_from_token(value, "api")
    if isinstance(ctx, Refusal):
        # Also before ``dispatch()``: a token that resolved to a refusal never
        # reaches it.
        return JSONResponse(
            envelope(
                ctx.state,
                None,
                approval_id=None,
                error_code=ctx.state,
                error_text=str(ctx),
            ),
            status_code=_TOKEN_REFUSAL_STATUS,
        )
    payload: dict[str, object] = dict(request.query_params)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        payload.update(body)
    # ``consumers`` is this process's one registry, built in ``startup.py`` beside the
    # module load that populates it. Passed on every dispatch rather than resolved
    # inside one, because the registry belongs to the composition root and
    # ``rheo_core`` holds no process-wide instance to fall back on.
    outcome = dispatch(ctx, name, payload, consumers=CONSUMERS)
    status = outcome_status(outcome)
    if carries_result(outcome):
        result = (
            None if outcome.result is None else outcome.result.model_dump(mode="json")
        )
        return JSONResponse(
            envelope(
                outcome.state,
                outcome.operation_id,
                approval_id=outcome.approval_id,
                result=result,
            ),
            status_code=status,
        )
    error = outcome.error
    return JSONResponse(
        envelope(
            outcome.state,
            outcome.operation_id,
            approval_id=outcome.approval_id,
            error_code=None if error is None else error.error_code,
            error_text=None if error is None else error.error_text,
        ),
        status_code=status,
    )

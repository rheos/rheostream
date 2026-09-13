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
dispatches, and returns the envelope ``{"state", "operation_id": null, "result"
| "error": {"error_code", "error_text"}}`` with the status mapping ``spec.md``
gives: 200 succeeded, 401 for every ``context_from_token`` refusal (including
a missing/malformed ``Authorization`` header, before any token is even looked
up), 403 ``operation_not_permitted``/``role_not_permitted``, 404 ``not_found``,
422 ``input_invalid``; anything else maps to 400.

The literal ``operation_id: null`` is a pre-declared, sanctioned occurrence of
the run's cut-symbol scan (``operations/dispatch.py:54-58`` already documents
exactly this: "There is no ``operation_id``... which is why the API envelope
(C8) carries ``operation_id: null`` until 0c"). Do not read it as a boundary
violation -- the real invariant, which ``tests/postgres/test_api_surface.py``
asserts, is that no ``operation`` table row is written and no id is minted
anywhere on this path, not the literal's absence.
"""

from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from rheo_core.boundary.context import TOKEN_MALFORMED, Refusal
from rheo_core.boundary.factories import context_from_token
from rheo_core.operations import (
    INPUT_INVALID,
    OPERATION_NOT_PERMITTED,
    ROLE_NOT_PERMITTED,
    SUCCEEDED,
    OperationOutcome,
    dispatch,
)
from rheo_core.refs.resolver import NOT_FOUND

router = APIRouter()

_TOKEN_REFUSAL_STATUS: Final = 401
_STATUS_BY_STATE: Final[dict[str, int]] = {
    SUCCEEDED: 200,
    OPERATION_NOT_PERMITTED: 403,
    ROLE_NOT_PERMITTED: 403,
    NOT_FOUND: 404,
    INPUT_INVALID: 422,
}
_DEFAULT_ERROR_STATUS: Final = 400


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
    *,
    result: dict[str, object] | None = None,
    error_code: str | None = None,
    error_text: str | None = None,
) -> dict[str, object]:
    """The operation envelope both listeners answer with: ``{"state",
    "operation_id": null, "result" | "error"}``.

    Public, and shared: run 0v's ``POST /internal/v1/operations/{name}``
    (``internal_routes.py``) answers with this same shape over the same
    ``dispatch()``, so the two surfaces cannot drift apart by being written twice.
    The generated OpenAPI document (``rheo_core.operations.openapi``) describes
    exactly this envelope as the 200 response for every operation.
    """
    body: dict[str, object] = {"state": state, "operation_id": None}
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


@router.post("/api/v1/operations/{name}")
async def run_operation(name: str, request: Request) -> JSONResponse:
    value = _bearer(request)
    if value is None:
        return JSONResponse(
            envelope(
                TOKEN_MALFORMED,
                error_code=TOKEN_MALFORMED,
                error_text="no bearer token was presented",
            ),
            status_code=_TOKEN_REFUSAL_STATUS,
        )
    ctx = context_from_token(value, "api")
    if isinstance(ctx, Refusal):
        return JSONResponse(
            envelope(ctx.state, error_code=ctx.state, error_text=str(ctx)),
            status_code=_TOKEN_REFUSAL_STATUS,
        )
    payload: dict[str, object] = dict(request.query_params)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        payload.update(body)
    outcome = dispatch(ctx, name, payload)
    status = outcome_status(outcome)
    if outcome.ok:
        result = (
            None if outcome.result is None else outcome.result.model_dump(mode="json")
        )
        return JSONResponse(envelope(outcome.state, result=result), status_code=status)
    error = outcome.error
    return JSONResponse(
        envelope(
            outcome.state,
            error_code=None if error is None else error.error_code,
            error_text=None if error is None else error.error_text,
        ),
        status_code=status,
    )

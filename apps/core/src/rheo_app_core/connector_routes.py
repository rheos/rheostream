"""Mount declared connector routes on the configured API surface only."""

import asyncio
from uuid import UUID

from fastapi import Request
from rheo_core.connectors.receiver import MAX_BODY_BYTES, receive
from rheo_core.modules import loaded_manifests
from rheo_core.routing import RoutingMode
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from rheo_app_core.auth_routes import normalize_host
from rheo_app_core.routing import routing_config
from rheo_app_core.startup import CONSUMERS


class ConnectorRoutes(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        for module_id, manifest in loaded_manifests().items():
            for binding in manifest.connector_bindings:
                if binding.route is None or binding.read_connection is None:
                    continue
                config = routing_config()
                route = binding.route
                # Declared API routes retain /api on the dedicated host. Path
                # deployments replace that prefix with their configured prefix.
                if config.mode is RoutingMode.PATH:
                    route = config.surfaces.api.path.rstrip("/") + route.removeprefix(
                        "/api"
                    )
                prefix, marker, tail = route.partition("<connection_id>")
                if not marker or tail or not request.url.path.startswith(prefix):
                    continue
                host = normalize_host(request.headers.get("host", ""))
                expected = (
                    config.base_host
                    if config.mode is RoutingMode.PATH
                    else f"{config.surfaces.api.host}.{config.base_host}"
                )
                if host != expected:
                    return JSONResponse({"state": "not_found"}, status_code=404)
                if request.method != "POST":
                    return JSONResponse(
                        {"state": "method_not_allowed"},
                        status_code=405,
                        headers={"Allow": "POST"},
                    )
                try:
                    connection_id = UUID(request.url.path[len(prefix) :])
                except ValueError:
                    return JSONResponse(
                        {"state": "authentication_refused"}, status_code=401
                    )
                if request.query_params:
                    return JSONResponse({"state": "input_invalid"}, status_code=422)
                if (
                    request.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                    != "application/json"
                ):
                    return JSONResponse(
                        {"state": "unsupported_media_type"}, status_code=415
                    )
                if request.headers.get("content-encoding", "identity") != "identity":
                    return JSONResponse(
                        {"state": "unsupported_media_type"}, status_code=415
                    )
                for header in (
                    "x-rheo-timestamp",
                    "x-rheo-signature",
                    "x-rheo-event-id",
                ):
                    if len(request.headers.getlist(header)) > 1:
                        return JSONResponse(
                            {"state": "authentication_refused"}, status_code=401
                        )
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        return JSONResponse(
                            {"state": "payload_too_large"}, status_code=413
                        )
                    body.extend(chunk)
                status, result = await asyncio.to_thread(
                    receive,
                    module_id,
                    binding,
                    connection_id,
                    bytes(body),
                    request.headers.get("x-rheo-timestamp", ""),
                    request.headers.get("x-rheo-signature", ""),
                    request.headers.get("x-rheo-event-id"),
                    consumers=CONSUMERS,
                )
                return JSONResponse(
                    result, status_code=status, headers={"Cache-Control": "no-store"}
                )
        return await call_next(request)

"""The HTTP client the worker posts records and gaps through (spec FR 10).

One operation, ``core.evidence.ingest``, under the bridge's bearer token. Stdlib
``urllib.request`` only: the worker stays stdlib plus ``rheo-core``.

Three outcomes reach the caller, and no raw ``urllib``/socket exception does:

- :class:`IngestResponse`, the server's four tuples of native keys;
- :class:`IngestRefused`, a refusal the operation contract defines (400
  ``enrollment_inactive``/``enrollment_mismatch``, 401 for a token state such as
  revoked or expired, 403 ``operation_not_permitted``/``role_not_permitted``,
  422 ``input_invalid``; see :data:`REFUSAL_CODES`), carrying that code;
- :class:`IngestTransportError`, everything else: timeout, refused connection,
  TLS failure, a redirect, any other status, or a body that is not the envelope.

**The token.** It is sent only in the ``Authorization`` header and never appears
in a message, a ``repr`` or an exception built here. Redirects are refused rather
than followed, because ``urllib`` would carry the header to the new location.
Certificate verification is always on; plain ``http`` is admitted only for a
loopback host, where there is no network to protect the header from. No proxy
is consulted: an ``http_proxy`` in the environment would otherwise receive the
header in the clear on a plain-``http`` request.
"""

from __future__ import annotations

import http.client
import json
import ssl
import string
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

INGEST_PATH: Final = "/api/v1/operations/core.evidence.ingest"
TIMEOUT_SECONDS: Final = 30.0

# The refusals the operation contract defines for this operation, by status: a
# token refusal (401), the handler's two enrollment words (400), a pydantic
# refusal (422) and the two permission states (403). A code outside its
# status's set, or any other status, is a transport failure, so a server
# string reaches an exception message only from this closed set.
REFUSAL_CODES: Final[dict[int, frozenset[str]]] = {
    400: frozenset({"enrollment_inactive", "enrollment_mismatch"}),
    401: frozenset(
        {
            "token_malformed",
            "token_expired",
            "token_revoked",
            "token_wrong_kind",
            "token_scope_invalid",
            "token_purpose_invalid",
        }
    ),
    403: frozenset({"operation_not_permitted", "role_not_permitted"}),
    422: frozenset({"input_invalid"}),
}

# The envelope for up to 1000 records and 1000 gaps is a few hundred KiB; this
# bounds what a misbehaving server or proxy can make the worker hold.
MAX_RESPONSE_BYTES: Final = 8 * 1024 * 1024

_LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
# RFC 6750 token characters, a subset of printable ASCII. Checked up front
# because ``http.client`` would otherwise refuse a bad header value with a
# ``ValueError`` that quotes the value.
_TOKEN_CHARS: Final = frozenset(string.ascii_letters + string.digits + "-._~+/=")

GapReason = Literal["source_truncated", "expired_pending"]


class IngestTransportError(Exception):
    """The request did not produce an ingest answer or a defined refusal."""


class IngestRefused(Exception):
    """The server refused the batch with a state the operation contract defines."""

    def __init__(self, error_code: str, http_status: int) -> None:
        super().__init__(f"ingest refused: {error_code} (HTTP {http_status})")
        self.error_code = error_code
        self.http_status = http_status


@dataclass(frozen=True)
class IngestRecord:
    """One human turn, keyed by its ``cc1:`` HMAC."""

    native_key: str
    recorded_at: datetime
    text: str

    def __post_init__(self) -> None:
        _require_aware(self.recorded_at)


@dataclass(frozen=True)
class IngestGap:
    """One turn the bridge knows it cannot deliver, keyed by its ``cc1g:`` HMAC."""

    native_key: str
    reason: GapReason
    recorded_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.recorded_at)


@dataclass(frozen=True)
class IngestResponse:
    """The server's ``IngestResult``: native keys only, never text or counts."""

    accepted: tuple[str, ...]
    deferred: tuple[str, ...]
    gapped: tuple[str, ...]
    dropped: tuple[str, ...]


class IngestClient(Protocol):
    def post(
        self,
        machine_fingerprint: str,
        project_fingerprint: str,
        records: Sequence[IngestRecord],
        gaps: Sequence[IngestGap],
    ) -> IngestResponse: ...


def _require_aware(value: datetime) -> None:
    # The server refuses a naive time as ``input_invalid``, which would refuse
    # the whole batch; catch it where it is made instead.
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")


def request_body(
    machine_fingerprint: str,
    project_fingerprint: str,
    records: Sequence[IngestRecord],
    gaps: Sequence[IngestGap],
) -> bytes:
    """The JSON body, in ``IngestInput``'s shape.

    ``ensure_ascii`` (the default) escapes every non-ASCII character, so a lone
    surrogate in a turn's text still encodes; the server scrubs it.
    """
    document = {
        "machine_fingerprint": machine_fingerprint,
        "project_fingerprint": project_fingerprint,
        "records": [
            {
                "native_key": r.native_key,
                "recorded_at": r.recorded_at.isoformat(),
                "text": r.text,
            }
            for r in records
        ],
        "gaps": [
            {
                "native_key": g.native_key,
                "reason": g.reason,
                "recorded_at": g.recorded_at.isoformat(),
            }
            for g in gaps
        ],
    }
    return json.dumps(document, ensure_ascii=True).encode("ascii")


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Surface a 3xx as an ``HTTPError`` instead of following it."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _ingest_url(base_url: str) -> str:
    parts = urllib.parse.urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("base_url has no host")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and host in _LOOPBACK_HOSTS
    ):
        raise ValueError("base_url must be https (plain http only for loopback)")
    if parts.query or parts.fragment:
        raise ValueError("base_url must not carry a query or fragment")
    # A second credential beside the token, and one ``__repr__`` would print.
    if parts.username is not None or parts.password is not None:
        raise ValueError("base_url must not carry user information")
    return base_url.rstrip("/") + INGEST_PATH


def _check_token(token: str) -> None:
    if not token or not set(token) <= _TOKEN_CHARS:
        # Never echo the value.
        raise ValueError("the bridge token is empty or holds invalid characters")


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise IngestTransportError("the ingest answer is not four lists of keys")
    return tuple(value)


def _parse_success(body: bytes) -> IngestResponse:
    envelope = _json_object(body)
    if envelope is None or envelope.get("state") != "succeeded":
        raise IngestTransportError("the ingest answer is not a succeeded envelope")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise IngestTransportError("the ingest answer has no result")
    return IngestResponse(
        accepted=_string_tuple(result.get("accepted")),
        deferred=_string_tuple(result.get("deferred")),
        gapped=_string_tuple(result.get("gapped")),
        dropped=_string_tuple(result.get("dropped")),
    )


def _refusal_code(http_status: int, body: bytes) -> str | None:
    """The envelope's code when it is one the contract defines for this status."""
    allowed = REFUSAL_CODES.get(http_status)
    if allowed is None:
        return None
    envelope = _json_object(body)
    if envelope is None:
        return None
    error = envelope.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("error_code")
    return code if isinstance(code, str) and code in allowed else None


def _json_object(body: bytes) -> dict[str, object] | None:
    """The body as a JSON object, or ``None``; every caller maps ``None`` to a
    transport error."""
    try:
        value = json.loads(body)
    except (RecursionError, ValueError):
        # ``ValueError`` covers a decode error and bad JSON; ``RecursionError``
        # is what a deeply nested body raises from the decoder.
        return None
    return value if isinstance(value, dict) else None


def _read_capped(response: http.client.HTTPResponse | urllib.error.HTTPError) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise IngestTransportError("the ingest answer is too large")
    return body


class HttpIngestClient:
    """:class:`IngestClient` over HTTPS with the bridge's bearer token."""

    def __init__(
        self, base_url: str, token: str, *, timeout: float = TIMEOUT_SECONDS
    ) -> None:
        _check_token(token)
        self._url = _ingest_url(base_url)
        self._token = token
        self._timeout = timeout
        # Verification on: the default context requires a valid certificate and
        # a matching host name. Nothing here loosens it.
        # ``ProxyHandler({})`` replaces the default one, which reads proxies
        # from the environment.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _RefuseRedirects(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    def __repr__(self) -> str:
        return f"HttpIngestClient(url={self._url!r})"

    def post(
        self,
        machine_fingerprint: str,
        project_fingerprint: str,
        records: Sequence[IngestRecord],
        gaps: Sequence[IngestGap],
    ) -> IngestResponse:
        request = urllib.request.Request(
            self._url,
            data=request_body(machine_fingerprint, project_fingerprint, records, gaps),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                http_status = response.status
                body = _read_capped(response)
        except urllib.error.HTTPError as exc:
            # A non-2xx answer (including a refused redirect). Read its body to
            # tell a defined refusal from anything else.
            try:
                body = _read_capped(exc)
            except (OSError, http.client.HTTPException):
                body = b""
            finally:
                exc.close()
            code = _refusal_code(exc.code, body)
            if code is None:
                raise IngestTransportError(f"ingest answered HTTP {exc.code}") from None
            raise IngestRefused(code, exc.code) from None
        except urllib.error.URLError as exc:
            reason = type(exc.reason).__name__
            raise IngestTransportError(f"ingest request failed: {reason}") from None
        except (OSError, http.client.HTTPException) as exc:
            # A timeout while reading, a reset connection, a malformed status
            # line: none of these is wrapped in ``URLError`` by ``urllib``.
            raise IngestTransportError(
                f"ingest request failed: {type(exc).__name__}"
            ) from None
        if http_status != 200:
            raise IngestTransportError(f"ingest answered HTTP {http_status}")
        return _parse_success(body)

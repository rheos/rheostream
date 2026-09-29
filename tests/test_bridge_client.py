"""The bridge's ingest client against a local fake server.

Nothing here reaches a real host: every request goes to a ``ThreadingHTTPServer``
on 127.0.0.1 started by the test. The token, keys and fingerprints are invented.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from rheo_bridge import client
from rheo_bridge.client import (
    HttpIngestClient,
    IngestGap,
    IngestRecord,
    IngestRefused,
    IngestResponse,
    IngestTransportError,
)

TOKEN = "rsb_synthetic-token-0001.abcdefXYZ"
MACHINE = "1" * 64
PROJECT = "2" * 64
RECORD_KEY = "cc1:" + "3" * 64
GAP_KEY = "cc1g:" + "4" * 64
WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

RECORDS = [IngestRecord(native_key=RECORD_KEY, recorded_at=WHEN, text="hello")]
GAPS = [IngestGap(native_key=GAP_KEY, reason="source_truncated", recorded_at=WHEN)]


@dataclass
class Answer:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


@dataclass
class Seen:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class FakeServer:
    base_url: str
    seen: list[Seen]
    answer: Callable[[Seen], Answer]


def _envelope(state: str, **extra: object) -> bytes:
    return json.dumps({"state": state, "operation_id": None, **extra}).encode()


def _succeeded(**result: list[str]) -> Answer:
    full = {"accepted": [], "deferred": [], "gapped": [], "dropped": [], **result}
    return Answer(200, _envelope("succeeded", result=full))


def _refused(status: int, code: str) -> Answer:
    error = {"error_code": code, "error_text": "synthetic refusal"}
    return Answer(status, _envelope(code, error=error))


@pytest.fixture
def server() -> Iterator[FakeServer]:
    seen: list[Seen] = []
    fake = FakeServer(base_url="", seen=seen, answer=lambda _: _succeeded())

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            request = Seen(
                self.path, dict(self.headers.items()), self.rfile.read(length)
            )
            seen.append(request)
            answer = fake.answer(request)
            if answer.delay:
                time.sleep(answer.delay)
            self.send_response(answer.status)
            for name, value in answer.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(answer.body)))
            self.end_headers()
            self.wfile.write(answer.body)

        def do_GET(self) -> None:
            # Only a followed redirect would arrive here; record it so a test
            # can prove none did.
            seen.append(Seen(self.path, dict(self.headers.items()), b""))
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    fake.base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield fake
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(base_url: str, **kwargs: float) -> IngestResponse:
    return HttpIngestClient(base_url, TOKEN, **kwargs).post(
        MACHINE, PROJECT, RECORDS, GAPS
    )


def test_a_success_returns_the_four_tuples(server: FakeServer) -> None:
    server.answer = lambda _: _succeeded(accepted=[RECORD_KEY], gapped=[GAP_KEY])
    assert _post(server.base_url) == IngestResponse(
        accepted=(RECORD_KEY,), deferred=(), gapped=(GAP_KEY,), dropped=()
    )


def test_the_request_is_the_ingest_shape_under_the_bearer_token(
    server: FakeServer,
) -> None:
    _post(server.base_url + "/")
    (request,) = server.seen
    assert request.path == "/api/v1/operations/core.evidence.ingest"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.body) == {
        "machine_fingerprint": MACHINE,
        "project_fingerprint": PROJECT,
        "records": [
            {
                "native_key": RECORD_KEY,
                "recorded_at": "2026-01-02T03:04:05+00:00",
                "text": "hello",
            }
        ],
        "gaps": [
            {
                "native_key": GAP_KEY,
                "reason": "source_truncated",
                "recorded_at": "2026-01-02T03:04:05+00:00",
            }
        ],
    }


def test_a_lone_surrogate_in_text_still_encodes() -> None:
    record = IngestRecord(native_key=RECORD_KEY, recorded_at=WHEN, text="a\ud800b")
    body = client.request_body(MACHINE, PROJECT, [record], [])
    assert json.loads(body)["records"][0]["text"] == "a\ud800b"


def test_a_naive_time_is_refused_where_it_is_made() -> None:
    with pytest.raises(ValueError):
        IngestRecord(native_key=RECORD_KEY, recorded_at=datetime(2026, 1, 2), text="x")
    with pytest.raises(ValueError):
        IngestGap(
            native_key=GAP_KEY,
            reason="expired_pending",
            recorded_at=datetime(2026, 1, 2),
        )


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "enrollment_inactive"),
        (400, "enrollment_mismatch"),
        (401, "token_revoked"),
        (401, "token_expired"),
        (422, "input_invalid"),
    ],
)
def test_a_defined_refusal_is_typed_with_its_code(
    server: FakeServer, status: int, code: str
) -> None:
    server.answer = lambda _: _refused(status, code)
    with pytest.raises(IngestRefused) as caught:
        _post(server.base_url)
    assert caught.value.error_code == code
    assert caught.value.http_status == status


@pytest.mark.parametrize(
    "answer",
    [
        Answer(500, _envelope("failed", error={"error_code": "x", "error_text": "y"})),
        Answer(503, b"<html>unavailable</html>"),
        Answer(403, _envelope("operation_not_permitted")),
        # A refusal status whose body is not the envelope (say, a proxy's page).
        Answer(400, b"<html>bad request</html>"),
        Answer(401, b""),
        # A 2xx that is not the succeeded envelope.
        Answer(202, _envelope("pending", result={})),
        Answer(200, b"not json"),
        Answer(200, _envelope("succeeded")),
        Answer(200, _envelope("succeeded", result={"accepted": "nope"})),
        Answer(200, _envelope("failed", result={})),
        # A redirect is refused, never followed with the token.
        Answer(307, b"", headers={"Location": "http://127.0.0.1:9/elsewhere"}),
        Answer(302, b"", headers={"Location": "/elsewhere"}),
    ],
)
def test_anything_else_is_a_transport_error(server: FakeServer, answer: Answer) -> None:
    server.answer = lambda _: answer
    with pytest.raises(IngestTransportError):
        _post(server.base_url)
    assert len(server.seen) == 1


def test_an_oversized_answer_is_a_transport_error(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "MAX_RESPONSE_BYTES", 16)
    server.answer = lambda _: _succeeded(accepted=[RECORD_KEY])
    with pytest.raises(IngestTransportError):
        _post(server.base_url)


def test_a_timeout_is_a_transport_error(server: FakeServer) -> None:
    server.answer = lambda _: Answer(200, b"{}", delay=1.0)
    with pytest.raises(IngestTransportError):
        _post(server.base_url, timeout=0.2)


def test_a_refused_connection_is_a_transport_error(server: FakeServer) -> None:
    # Port 9 on loopback: nothing listens there in the test environment.
    with pytest.raises(IngestTransportError):
        _post("http://127.0.0.1:9")


def test_the_default_timeout_is_thirty_seconds() -> None:
    assert client.TIMEOUT_SECONDS == 30.0


def test_tls_verification_is_on() -> None:
    https = HttpIngestClient("https://api.example.org", TOKEN)
    handlers = [
        h for h in https._opener.handlers if isinstance(h, urllib.request.HTTPSHandler)
    ]
    assert len(handlers) == 1
    # ``_context`` is the stdlib handler's own attribute: the only way to see
    # the context the opener will actually use.
    context: ssl.SSLContext = handlers[0]._context  # type: ignore[attr-defined]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example.org",
        "ftp://api.example.org",
        "https://",
        "https://api.example.org/?q=1",
        "https://someone:secret@api.example.org",
        "https://someone@api.example.org",
        "https://:secret@api.example.org",
        "http://someone:secret@127.0.0.1:8000",
    ],
)
def test_a_base_url_that_would_expose_the_token_is_refused(base_url: str) -> None:
    with pytest.raises(ValueError):
        HttpIngestClient(base_url, TOKEN)


@pytest.mark.parametrize("token", ["", "abc\r\nX-Evil: 1", "has space", "tök"])
def test_a_bad_token_is_refused_without_echoing_it(token: str) -> None:
    with pytest.raises(ValueError) as caught:
        HttpIngestClient("https://api.example.org", token)
    if token:
        assert token not in str(caught.value)


def test_the_token_never_appears_in_a_repr_an_error_or_a_log(
    server: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ingest = HttpIngestClient(server.base_url, TOKEN)
    assert TOKEN not in repr(ingest)
    texts: list[str] = []
    for answer in (
        _refused(401, "token_revoked"),
        Answer(500, TOKEN.encode()),
        Answer(307, b"", headers={"Location": "http://127.0.0.1:9/x"}),
    ):
        server.answer = lambda _, a=answer: a
        with pytest.raises((IngestRefused, IngestTransportError)) as caught:
            ingest.post(MACHINE, PROJECT, RECORDS, GAPS)
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__
        texts.append(str(caught.value))
        texts.append(repr(caught.value))
    with pytest.raises(IngestTransportError) as refused:
        HttpIngestClient("http://127.0.0.1:9", TOKEN).post(MACHINE, PROJECT, [], [])
    texts.append(str(refused.value))
    assert all(TOKEN not in text for text in texts)
    assert TOKEN not in caplog.text

"""Typed judgment wire validation and cancellation; synthetic state and credentials."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from rheo_contracts.judgment import JudgmentFailed, JudgmentQuestion, JudgmentRequest
from rheo_core.runtime.judgment import JudgmentRunner
from rheo_runtimes.jev import (
    ENDPOINT,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    JevProvider,
    credential,
    parse_result,
)

REQUEST = JudgmentRequest(
    "jev-latest",
    {"text": "Synthetic posting"},
    {
        "fit": JudgmentQuestion(
            "score", "How much direct experience?", ("None", "Some", "Repeated")
        ),
        "priced": JudgmentQuestion("noul", "Does the posting name a price?"),
    },
)


def response():
    return {
        "model": "jev-1.13.0",
        "answers": {
            "fit": {
                "type": "score",
                "score": 1.8,
                "confidence": 0.75,
                "probabilities": {"0": 0, "1": 0.2, "2": 0.8},
            },
            "priced": {"type": "noul", "noul": 0.91},
        },
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }


def runner(handler, **kwargs):
    return JudgmentRunner(
        JevProvider(
            key=lambda: "synthetic-key", transport=httpx.MockTransport(handler)
        ),
        **kwargs,
    )


def test_wire_and_usage_are_typed_and_bounded():
    seen = []

    def handle(req):
        seen.append(req)
        assert str(req.url) == ENDPOINT
        assert req.headers["authorization"] == "Bearer synthetic-key"
        sent = json.loads(req.content)
        assert sent["state"] == REQUEST.state
        assert sent["questions"]["fit"]["criteria"] == ["None", "Some", "Repeated"]
        assert "criteria" not in sent["questions"]["priced"]
        return httpx.Response(200, json=response())

    usage, beats = [], []
    result = runner(handle).run(
        REQUEST, heartbeat=lambda: beats.append(1), on_usage=lambda *v: usage.append(v)
    )
    assert result.values == {"fit": 1.8, "priced": 0.91}
    assert result.confidence == {"fit": 0.75}
    assert result.model == "jev-1.13.0" and usage == [(100, 10)]
    assert len(seen) == 1 and len(beats) >= 2


@pytest.mark.parametrize(
    "status,kind",
    [
        (301, "provider_unavailable"),
        (401, "credential_invalid"),
        (403, "credential_invalid"),
        (429, "rate_limited"),
        (529, "rate_limited"),
        (500, "provider_unavailable"),
        (413, "input_invalid"),
    ],
)
def test_errors_never_echo_the_response_or_retry(status, kind):
    seen = []

    def handle(req):
        seen.append(req)
        return httpx.Response(
            status,
            text="private-marker synthetic-key",
            headers={"location": "https://example.com/other"},
        )

    with pytest.raises(JudgmentFailed) as caught:
        runner(handle).run(REQUEST, heartbeat=lambda: None, on_usage=lambda *_: None)
    assert str(caught.value) == kind and caught.value.__context__ is None
    assert len(seen) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["answers"].pop("priced"),
        lambda r: r["answers"].update(extra={"type": "noul", "noul": 0.8}),
        lambda r: r["answers"]["priced"].update(noul=True),
        lambda r: r["answers"]["priced"].update(noul="0.9"),
        lambda r: r["answers"]["priced"].update(noul=float("nan")),
        lambda r: r["answers"]["priced"].update(noul=1.01),
        lambda r: r["answers"]["priced"].update(type="score"),
        lambda r: r["answers"]["fit"].update(score=3),
        lambda r: r["answers"]["fit"].update(score=0.2),
        lambda r: r["answers"]["fit"].update(confidence=float("inf")),
        lambda r: r["answers"]["fit"].update(probabilities={"0": 0, "1": 0.2}),
        lambda r: r["usage"].update(input_tokens=-1),
        lambda r: r["usage"].update(output_tokens=True),
        lambda r: r.update(model="private-marker"),
    ],
)
def test_malformed_answers_never_become_decisions(mutation):
    raw = response()
    mutation(raw)
    with pytest.raises(JudgmentFailed, match="^output_invalid$") as caught:
        parse_result(json.dumps(raw).encode(), REQUEST)
    assert caught.value.__context__ is None


def test_oversized_input_does_not_read_the_key_or_send():
    def forbidden(*_):
        pytest.fail("oversized request reached I/O")

    provider = JevProvider(key=forbidden, transport=httpx.MockTransport(forbidden))
    request = replace(REQUEST, state={"text": "x" * MAX_REQUEST_BYTES})
    with pytest.raises(JudgmentFailed, match="input_too_large"):
        JudgmentRunner(provider).run(
            request, heartbeat=lambda: None, on_usage=forbidden
        )


def test_oversized_and_non_json_responses_are_refused():
    for body in (b"x" * (MAX_RESPONSE_BYTES + 1), b"not json private-marker"):
        with pytest.raises(JudgmentFailed, match="output_invalid"):
            runner(lambda _, body=body: httpx.Response(200, content=body)).run(
                REQUEST, heartbeat=lambda: None, on_usage=lambda *_: None
            )


def test_deadline_cancels_pending_io():
    closed = []

    async def slow(_):
        try:
            await asyncio.sleep(10)
        finally:
            closed.append(True)

    with pytest.raises(JudgmentFailed, match="deadline_exceeded"):
        runner(slow, deadline_seconds=0.02).run(
            REQUEST, heartbeat=lambda: None, on_usage=lambda *_: None
        )
    assert closed == [True]


def test_heartbeat_cancellation_propagates_and_closes_io():
    closed, beats = [], []

    class Cancelled(Exception):
        pass

    async def slow(_):
        try:
            await asyncio.sleep(10)
        finally:
            closed.append(True)

    def heartbeat():
        beats.append(1)
        if len(beats) == 2:
            raise Cancelled

    with pytest.raises(Cancelled):
        runner(slow).run(REQUEST, heartbeat=heartbeat, on_usage=lambda *_: None)
    assert closed == [True]


def test_secret_scope_and_disabled_gate(monkeypatch):
    monkeypatch.setenv("RHEO__runtime__jev__enabled", "false")
    monkeypatch.setenv(
        "RHEO__runtime__jev__credential_ref", "secret://env/RHEO_TYPESAFE_API_KEY"
    )
    monkeypatch.setenv("RHEO_TYPESAFE_API_KEY", "synthetic-key")
    with pytest.raises(JudgmentFailed, match="disabled"):
        credential()
    monkeypatch.setenv("RHEO__runtime__jev__enabled", "true")
    assert credential() == "synthetic-key"
    monkeypatch.setenv(
        "RHEO__runtime__jev__credential_ref", "secret://env/RHEO_CLUSTER_DSN"
    )
    with pytest.raises(JudgmentFailed, match="credential_invalid") as caught:
        credential()
    assert caught.value.__context__ is None
    monkeypatch.setenv(
        "RHEO__runtime__jev__credential_ref", "secret://env/RHEO_TYPESAFE_API_KEY"
    )
    monkeypatch.setenv("RHEO_TYPESAFE_API_KEY", "comment\nsynthetic-key")
    with pytest.raises(JudgmentFailed, match="credential_invalid"):
        credential()


@pytest.mark.parametrize(
    "error,kind",
    [
        (httpx.ReadTimeout("private-marker"), "deadline_exceeded"),
        (httpx.ConnectError("private-marker"), "provider_unavailable"),
    ],
)
def test_transport_failures_are_sanitized(error, kind):
    def handle(_):
        raise error

    with pytest.raises(JudgmentFailed) as caught:
        runner(handle).run(REQUEST, heartbeat=lambda: None, on_usage=lambda *_: None)
    assert str(caught.value) == kind and caught.value.__context__ is None

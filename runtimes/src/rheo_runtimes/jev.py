"""TypeSafe System One transport: pinned HTTPS destination, no redirects or tools.

One bounded attempt; the caller falls through to its full scorer on failure. No
retry storm on rate limiting. Raw bodies, exception text and credentials never
cross this adapter. HTTP clients close on cancellation and on every failure.
"""

import json
import math
import re
from collections.abc import Callable
from typing import Any

import httpx
from rheo_contracts.judgment import JudgmentFailed, JudgmentRequest, JudgmentResult
from rheo_core.secrets import SecretRef, SecretRefusal, SecretStore
from rheo_core.settings import resolve
from rheo_core.storage.data_root import resolve_data_root

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_REQUEST_BYTES = 120_000
MAX_RESPONSE_BYTES = 262_144
MAX_QUESTIONS = 128


def credential() -> str:
    settings = resolve()
    if not settings.get_bool("runtime.jev.enabled"):
        raise JudgmentFailed("disabled")
    value = ""
    try:
        store = SecretStore(resolve_data_root().path)
        scope = store.scope_for(
            "runtime.jev",
            "secret://file/runtime/jev/",
            "secret://env/RHEO_TYPESAFE_API_KEY",
        )
        secret = store.resolve(
            SecretRef.parse(settings.get_str("runtime.jev.credential_ref")), scope
        )
        value = secret.expose().decode("utf-8").strip()
    except (SecretRefusal, TypeError, ValueError, OSError):
        pass
    if not value or not value.isascii() or any(c.isspace() for c in value):
        raise JudgmentFailed("credential_invalid")
    return value


def _number(value: object, top: float) -> float:
    if type(value) not in (int, float):
        raise ValueError
    number = float(value)  # type: ignore[arg-type]
    if not math.isfinite(number) or not 0 <= number <= top:
        raise ValueError
    return number


def parse_result(raw: bytes, request: JudgmentRequest) -> JudgmentResult:
    """Validate every requested answer, including score distributions. Never coerce."""
    try:
        payload = json.loads(raw)
        answers = payload["answers"]
        if not isinstance(answers, dict) or set(answers) != set(request.questions):
            raise ValueError
        values: dict[str, float] = {}
        confidence: dict[str, float] = {}
        for name, question in request.questions.items():
            answer = answers[name]
            if answer["type"] != question.kind:
                raise ValueError
            if question.kind == "noul":
                values[name] = _number(answer["noul"], 1)
            else:
                top = len(question.levels) - 1
                values[name] = _number(answer["score"], top)
                confidence[name] = _number(answer["confidence"], 1)
                probs = answer["probabilities"]
                if set(probs) != {str(i) for i in range(top + 1)}:
                    raise ValueError
                numbers = [_number(probs[str(i)], 1) for i in range(top + 1)]
                if (
                    abs(sum(numbers) - 1) > 0.01
                    or abs(sum(i * p for i, p in enumerate(numbers)) - values[name])
                    > 0.02
                ):
                    raise ValueError
        model = payload["model"]
        if not isinstance(model, str) or not re.fullmatch(
            r"jev-[A-Za-z0-9_.-]{1,80}", model
        ):
            raise ValueError
        usage = payload["usage"]
        counts = [usage["input_tokens"], usage["output_tokens"]]
        if any(type(v) is not int or not 0 <= v <= 2_147_483_647 for v in counts):
            raise ValueError
        return JudgmentResult(values, model, counts[0], counts[1], confidence)
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
        pass
    raise JudgmentFailed("output_invalid")


class JevProvider:
    def __init__(
        self,
        *,
        key: Callable[[], str] = credential,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = key
        self._transport = transport

    async def evaluate(self, request: JudgmentRequest) -> JudgmentResult:
        questions: dict[str, Any] = {}
        if not 1 <= len(request.questions) <= MAX_QUESTIONS:
            raise JudgmentFailed("input_too_large")
        if not re.fullmatch(r"jev-[A-Za-z0-9_.-]{1,80}", request.model):
            raise JudgmentFailed("input_invalid")
        for name, q in request.questions.items():
            if not q.instructions or q.kind not in ("noul", "score"):
                raise JudgmentFailed("input_invalid")
            questions[name] = {"type": q.kind, "instructions": q.instructions}
            if q.kind == "score":
                if not 2 <= len(q.levels) <= 10 or not all(q.levels):
                    raise JudgmentFailed("input_invalid")
                questions[name]["criteria"] = list(q.levels)
        body: bytes | None = None
        try:
            body = json.dumps(
                {
                    "model": request.model,
                    "state": request.state,
                    "questions": questions,
                },
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            pass
        if body is None:
            raise JudgmentFailed("input_invalid")
        if len(body) > MAX_REQUEST_BYTES:
            raise JudgmentFailed("input_too_large")
        headers = {
            "Authorization": "Bearer " + self._key(),
            "Content-Type": "application/json",
        }
        failure = "provider_unavailable"
        raw = bytearray()
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(10, connect=5),
            ) as client:
                async with client.stream(
                    "POST", ENDPOINT, content=body, headers=headers
                ) as response:
                    if response.status_code == 200:
                        async for chunk in response.aiter_bytes():
                            if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise JudgmentFailed("output_invalid")
                            raw.extend(chunk)
                        return parse_result(bytes(raw), request)
                    if response.status_code in (401, 403):
                        failure = "credential_invalid"
                    elif response.status_code in (429, 529):
                        failure = "rate_limited"
                    elif response.status_code in (400, 413, 422):
                        failure = "input_invalid"
        except httpx.TimeoutException:
            failure = "deadline_exceeded"
        except (httpx.HTTPError, ValueError):
            pass
        raise JudgmentFailed(failure)

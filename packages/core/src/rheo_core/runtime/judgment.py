"""Synchronous worker seam for bounded, cancellable typed inference.

The composition root supplies the provider. Domain modules never import a vendor
adapter or resolve its secret. Heartbeat exceptions propagate and cancel pending
I/O before this call returns. There is no database transaction during inference.
"""

import asyncio
from collections.abc import Callable
from contextlib import suppress

from rheo_contracts.judgment import (
    JudgmentFailed,
    JudgmentProvider,
    JudgmentRequest,
    JudgmentResult,
)


class JudgmentRunner:
    def __init__(
        self, provider: JudgmentProvider, *, deadline_seconds: float = 30
    ) -> None:
        self._provider = provider
        self._deadline = min(max(deadline_seconds, 0.01), 30)

    def run(
        self,
        request: JudgmentRequest,
        *,
        heartbeat: Callable[[], None],
        on_usage: Callable[[int, int], None],
    ) -> JudgmentResult:
        heartbeat()
        result = asyncio.run(self._await(request, heartbeat))
        on_usage(result.input_tokens, result.output_tokens)
        return result

    async def _await(
        self, request: JudgmentRequest, heartbeat: Callable[[], None]
    ) -> JudgmentResult:
        task = asyncio.create_task(self._provider.evaluate(request))
        deadline = asyncio.get_running_loop().time() + self._deadline
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise JudgmentFailed("deadline_exceeded")
                done, _ = await asyncio.wait({task}, timeout=min(0.25, remaining))
                heartbeat()
                if done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


_registered: JudgmentRunner | None = None


def set_judgment_runner(runner: JudgmentRunner | None) -> None:
    """Composition roots and tests only."""
    global _registered
    _registered = runner


def judgment_runner() -> JudgmentRunner | None:
    return _registered

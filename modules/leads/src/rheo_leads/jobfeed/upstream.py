"""The collector's read-only view of the job feed's MCP tools.

:class:`Upstream` wraps one MCP session for one run. It is the only place the run
talks to the vendor, and it owns three rules:

**Lease checkpoints around every call.** Each upstream call (``initialize``, the
``initialized`` notification, ``list_accounts`` and every ``find_jobs``) runs as:
the pacing wait (``find_jobs`` only), ``token.checkpoint(force=True)``, the one
session method, then the ordinary ``token.checkpoint()`` straight after it returns
or fails, before any parse, pacing wait or retry delay. A call starts with a full
lease, and a long call is followed at once by a fresh one.

**Pacing.** ``find_jobs`` calls, all three actions, start at least
:data:`FIND_JOBS_SPACING` seconds apart, start to start. When the wait would reach
the deadline, or less than :data:`START_SECONDS` would remain at the start (checked
before the wait and again after the forced checkpoint), the call is not made and
:class:`RunCut` is raised.

**Exactly one retry.** A ``find_jobs`` tool error whose code is
:data:`SENSITIVE_RATE_LIMIT` is retried once with the same arguments, starting at
least :data:`RETRY_SPACING` seconds after the failure (and never before the normal
spacing). A second one fails the call as ``rate_limited``. Nothing else is retried:
not a transport error, a timeout, a 5xx, an HTTP 429, any other tool error.

Failures are :class:`UpstreamFailure` with a fixed code, or :class:`RunCut`. Their
``str`` and ``repr`` are the code alone, and each is raised outside any ``except``
block, so ``__cause__`` and ``__context__`` are ``None``: no message, body or
organization id reaches an exception. :class:`~rheo_core.connectors.mcp_client.
ToolRefused` (a call outside :data:`READ_ONLY_POLICY`) and the token's own
``JobCancelled``/``JobLeaseLost`` pass through unchanged.
"""

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Literal, Protocol, Self

from rheo_core.connectors.mcp_client import (
    MINIMUM_START_SECONDS,
    McpFailure,
    ToolPolicy,
)

from rheo_leads.jobfeed.presets import Action

READ_ONLY_POLICY: Final = ToolPolicy(
    {"list_accounts": None, "find_jobs": {"smart_search", "search", "get"}}
)
"""The only tools and actions the collector may call; the session refuses anything
else before a byte is sent."""

START_SECONDS: Final = MINIMUM_START_SECONDS + 0.5
"""The least time a ``find_jobs`` call may start with. The session refuses a call
under :data:`MINIMUM_START_SECONDS` as a failure; the half-second margin covers the
forced checkpoint and the gap before the session's own check, so a call near the
deadline is cut instead."""
FIND_JOBS_SPACING: Final = 5.3
RETRY_SPACING: Final = 5.5
SENSITIVE_RATE_LIMIT: Final = "SENSITIVE_RATE_LIMIT_EXCEEDED"
TALENT_ROLE: Final = "TALENT"

FindAction = Action | Literal["get"]
UpstreamCode = Literal[
    "unauthorized",
    "rate_limited",
    "unavailable",
    "endpoint_misconfigured",
    "protocol",
    "tool_error",
]


class UpstreamFailure(Exception):
    """A failed upstream call, carrying a fixed code only. ``terminal`` is true
    only for a refused credential (HTTP 401 or 403)."""

    def __init__(
        self,
        code: UpstreamCode,
        retry_after: int | None = None,
        terminal: bool = False,
    ) -> None:
        super().__init__(code)
        self.code: UpstreamCode = code
        self.retry_after = retry_after
        self.terminal = terminal

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return self.code


class RunCut(Exception):
    """The run's deadline leaves no room for the next call; it was not made."""

    code: Final = "run_cut"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return self.code


class Session(Protocol):
    """What :class:`~rheo_core.connectors.mcp_client.McpSession` offers."""

    def __enter__(self) -> object: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> object: ...

    def initialize(self) -> None: ...

    def notify_initialized(self) -> None: ...

    def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> dict[str, object]: ...


class Checkpoint(Protocol):
    """What :class:`~rheo_core.work.cancellation.CancellationToken` offers."""

    def checkpoint(self, *, force: bool = False) -> None: ...


@dataclass(frozen=True)
class Account:
    organization_id: str
    label: str | None
    is_talent: bool


def _translate(failure: McpFailure) -> UpstreamFailure:
    if failure.code == "unauthorized":
        return UpstreamFailure("unauthorized", terminal=True)
    if failure.code == "rate_limited":
        return UpstreamFailure("rate_limited", retry_after=failure.retry_after)
    return UpstreamFailure(failure.code)


def _document(result: dict[str, object]) -> dict[str, object] | None:
    """The JSON object in the result's single text block, or ``None``."""
    content = result.get("content")
    if not isinstance(content, list):
        return None
    texts = [
        block.get("text")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    if len(texts) != 1 or not isinstance(texts[0], str):
        return None
    try:
        document = json.loads(texts[0])
    except (ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def _accounts(document: dict[str, object]) -> tuple[Account, ...] | None:
    listed = document.get("accounts")
    if not isinstance(listed, list):
        return None
    accounts: list[Account] = []
    for entry in listed:
        if not isinstance(entry, dict):
            return None
        organization_id = entry.get("org_uid")
        if not isinstance(organization_id, str) or not organization_id:
            return None
        label = entry.get("name")
        accounts.append(
            Account(
                organization_id=organization_id,
                label=label if isinstance(label, str) and label else None,
                is_talent=entry.get("role") == TALENT_ROLE,
            )
        )
    return tuple(accounts)


class Upstream:
    """One run's paced, checkpointed, read-only calls (module docstring).

    Entering enters ``session`` (which sends nothing) and then makes the two set-up
    calls; leaving exits the session. ``deadline`` is the run's absolute instant on
    ``monotonic``.
    """

    def __init__(
        self,
        session: Session,
        token: Checkpoint,
        *,
        deadline: float,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._session = session
        self._token = token
        self._deadline = deadline
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_start = -math.inf

    def __enter__(self) -> Self:
        self._session.__enter__()
        ready = False
        try:
            self._raise(self._checkpointed(self._session.initialize))
            self._raise(self._checkpointed(self._session.notify_initialized))
            ready = True
        finally:
            if not ready:
                self._session.__exit__(None, None, None)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._session.__exit__(exc_type, exc, traceback)

    def list_accounts(self) -> tuple[Account, ...]:
        """The accounts the connected login can act as."""
        result = self._raise(
            self._checkpointed(lambda: self._session.call_tool("list_accounts", {}))
        )
        document = _document(result)
        accounts = None if document is None else _accounts(document)
        if accounts is None:
            raise UpstreamFailure("protocol")
        return accounts

    def find_jobs(
        self,
        action: FindAction,
        org_uid: str,
        params: Mapping[str, object],
        *,
        deadline: float | None = None,
    ) -> dict[str, object]:
        """One paced ``find_jobs`` call and its JSON document. ``deadline`` may set
        an earlier cut-off than the run's for this call."""
        cutoff = self._deadline if deadline is None else min(deadline, self._deadline)
        arguments = {"action": action, "org_uid": org_uid, "params": dict(params)}
        retried = False
        while True:
            self._pace(cutoff)
            outcome = self._checkpointed(
                lambda: self._session.call_tool("find_jobs", arguments), cutoff=cutoff
            )
            if (
                isinstance(outcome, McpFailure)
                and outcome.code == "tool_error"
                and outcome.tool_code == SENSITIVE_RATE_LIMIT
            ):
                if retried:
                    raise UpstreamFailure("rate_limited")
                retried = True
                self._next_start = max(
                    self._next_start, self._monotonic() + RETRY_SPACING
                )
                continue
            document = _document(self._raise(outcome))
            if document is None:
                raise UpstreamFailure("protocol")
            return document

    def _pace(self, cutoff: float) -> None:
        now = self._monotonic()
        start = max(now, self._next_start)
        if cutoff - start < START_SECONDS:
            raise RunCut()
        if start > now:
            self._sleep(start - now)

    def _checkpointed[T](
        self, call: Callable[[], T], *, cutoff: float | None = None
    ) -> T | McpFailure:
        """Forced checkpoint, the call, ordinary checkpoint; a failure is returned,
        not raised, so the caller raises its translation outside any handler. A
        ``cutoff`` marks a paced call, which is cut, not made, when the checkpoint
        left it under :data:`START_SECONDS`."""
        self._token.checkpoint(force=True)
        if cutoff is not None:
            if cutoff - self._monotonic() < START_SECONDS:
                raise RunCut()
            # Start to start: the spacing counts from the moment this call begins.
            self._next_start = self._monotonic() + FIND_JOBS_SPACING
        outcome: T | McpFailure
        try:
            outcome = call()
        except McpFailure as failure:
            outcome = failure
        self._token.checkpoint()
        return outcome

    @staticmethod
    def _raise[T](outcome: T | McpFailure) -> T:
        if isinstance(outcome, McpFailure):
            raise _translate(outcome)
        return outcome

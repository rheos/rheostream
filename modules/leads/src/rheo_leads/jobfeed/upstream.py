"""The collector's read-only view of the job feed's MCP tools.

:class:`Upstream` wraps one MCP session for one run. It is the only place the run
talks to the vendor, and it owns three rules:

**Lease checkpoints around every call.** Each upstream call (``initialize``, the
``initialized`` notification, ``list_accounts`` and every paced call) runs as:
the pacing wait (paced calls only), ``token.checkpoint(force=True)``, the one
session method, then the ordinary ``token.checkpoint()`` straight after it returns
or fails, before any parse, pacing wait or retry delay. A call starts with a full
lease, and a long call is followed at once by a fresh one.

**Pacing.** ``find_jobs`` calls, all three actions, and the invitations listing
start at least :data:`FIND_JOBS_SPACING` seconds apart, start to start. When the
wait would reach the deadline, or less than :data:`START_SECONDS` would remain at
the start (checked before the wait and again after the forced checkpoint), the call
is not made and :class:`RunCut` is raised.

**Exactly one retry.** A paced call's tool error whose code is
:data:`SENSITIVE_RATE_LIMIT` is retried once with the same arguments, starting at
least :data:`RETRY_SPACING` seconds after the failure (and never before the normal
spacing). A second one fails the call as ``rate_limited``. Nothing else is retried:
not a transport error, a timeout, a 5xx, an HTTP 429, any other tool error.

**Listing is best effort.** :meth:`Upstream.list_tools` (only when the operator
asked for it) sends ``tools/list`` between the same checkpoints, following
``nextCursor`` for at most :data:`TOOL_LIST_PAGES` pages within
:data:`TOOL_LIST_SECONDS` (each request is cut off at that limit), and never raises an
:class:`UpstreamFailure` or :class:`RunCut`: a failure ends the listing with its
code and the run carries on (after a rate-limited listing, it first waits the
provider's ``Retry-After`` or :data:`RETRY_SPACING`, whichever is longer, inside the
same budget). It keeps only values that pass a fixed pattern (a
tool's name, its ``action`` values, its argument names, its read-only hint), never
a description or any other text.

Failures are :class:`UpstreamFailure` with a fixed code, or :class:`RunCut`. Their
``str`` and ``repr`` are the code alone, and each is raised outside any ``except``
block, so ``__cause__`` and ``__context__`` are ``None``: no message, body or
organization id reaches an exception. :class:`~rheo_core.connectors.mcp_client.
ToolRefused` (a call outside :func:`read_only_policy`) and the token's own
``JobCancelled``/``JobLeaseLost`` pass through unchanged.
"""

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from types import TracebackType
from typing import Final, Literal, Protocol, Self

from rheo_core.connectors.mcp_client import (
    MINIMUM_START_SECONDS,
    McpFailure,
    ToolPolicy,
)

from rheo_leads.jobfeed.presets import Action

LIST_ACCOUNTS: Final = "list_accounts"
FIND_JOBS: Final = "find_jobs"
PROPOSALS: Final = "list_freelancer_proposals"
INVITATIONS: Final = "invitations"
TOOL_PREFIX: Final = re.compile(r"[A-Za-z0-9_-]{0,64}")
"""What the ``leads.jobfeed.tool_prefix`` setting may hold; anything else is a
misconfigured endpoint."""


def read_only_policy(prefix: str = "") -> ToolPolicy:
    """The only tools and actions the collector may call, named on the wire with
    ``prefix`` in front; the session refuses anything else before a byte is sent.
    Of the proposals tool, only the read-only invitations listing is allowed."""
    return ToolPolicy(
        {
            LIST_ACCOUNTS: None,
            FIND_JOBS: {"smart_search", "search", "get"},
            PROPOSALS: {INVITATIONS},
        },
        prefix=prefix,
    )


READ_ONLY_POLICY: Final = read_only_policy()

TOOL_LIST_PAGES: Final = 5
"""The most ``tools/list`` pages one listing reads."""
TOOL_LIST_SECONDS: Final = 60.0
"""The most time one listing may take, start to end, every request included; the
rest of the run's budget stays with collection."""
TOOL_LIST_MAX: Final = 200
"""The most tools one listing keeps; the rest are counted as skipped."""
SCHEMA_LIST_MAX: Final = 32
"""The most ``action`` values, and argument names, kept for one tool."""
_TOOL_NAME: Final = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")
_SCHEMA_WORD: Final = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

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
    only for a refused credential (HTTP 401 or 403). A ``tool_error`` keeps the
    tool's own upper-snake error code when it gave one (the MCP client accepts
    nothing else), so a run's history can say which error it was."""

    def __init__(
        self,
        code: UpstreamCode,
        retry_after: int | None = None,
        terminal: bool = False,
        *,
        tool_code: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code: UpstreamCode = code
        self.retry_after = retry_after
        self.terminal = terminal
        self.tool_code = tool_code

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

    def list_tools(
        self, cursor: str | None = None, *, deadline: float | None = None
    ) -> dict[str, object]: ...

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


@dataclass(frozen=True)
class ListedTool:
    """One tool as ``tools/list`` names it: only pattern-checked values.
    ``read_only`` is the server's ``readOnlyHint``, ``None`` when it gave none."""

    name: str
    actions: tuple[str, ...]
    arguments: tuple[str, ...]
    read_only: bool | None


@dataclass(frozen=True)
class ToolListing:
    """What one best-effort listing saw. ``outcome`` is ``ok`` when the server
    said there was no more; else ``more`` (the page limit came first), ``run_cut``
    (the run's deadline), ``budget`` (:data:`TOOL_LIST_SECONDS`), ``malformed`` or
    the :data:`UpstreamCode` of the failed request. ``skipped``
    counts tools whose name failed the pattern or that came past the limit."""

    tools: tuple[ListedTool, ...]
    pages: int
    skipped: int
    outcome: str


def _words(values: object) -> tuple[str, ...]:
    if not isinstance(values, list | dict):
        return ()
    kept = [v for v in values if isinstance(v, str) and _SCHEMA_WORD.fullmatch(v)]
    return tuple(kept[:SCHEMA_LIST_MAX])


def _listed_tool(entry: object) -> ListedTool | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        return None
    schema = entry.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    properties = properties if isinstance(properties, dict) else {}
    action = properties.get("action")
    annotations = entry.get("annotations")
    hint = annotations.get("readOnlyHint") if isinstance(annotations, dict) else None
    return ListedTool(
        name=name,
        actions=_words(action.get("enum") if isinstance(action, dict) else None),
        arguments=_words(properties),
        read_only=hint if isinstance(hint, bool) else None,
    )


def _translate(failure: McpFailure) -> UpstreamFailure:
    if failure.code == "unauthorized":
        return UpstreamFailure("unauthorized", terminal=True)
    if failure.code == "rate_limited":
        return UpstreamFailure("rate_limited", retry_after=failure.retry_after)
    if failure.code == "tool_error":
        return UpstreamFailure("tool_error", tool_code=failure.tool_code)
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
    ``monotonic``. ``tool_prefix`` goes in front of each tool name sent; the
    session's policy must be :func:`read_only_policy` with the same prefix.
    """

    def __init__(
        self,
        session: Session,
        token: Checkpoint,
        *,
        deadline: float,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        tool_prefix: str = "",
    ) -> None:
        self._session = session
        self._list_accounts = tool_prefix + LIST_ACCOUNTS
        self._find_jobs = tool_prefix + FIND_JOBS
        self._proposals = tool_prefix + PROPOSALS
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
            self._checkpointed(lambda: self._session.call_tool(self._list_accounts, {}))
        )
        document = _document(result)
        accounts = None if document is None else _accounts(document)
        if accounts is None:
            raise UpstreamFailure("protocol")
        return accounts

    def list_tools(self) -> ToolListing:
        """Best effort: the tools the server lists (module docstring)."""
        tools: list[ListedTool] = []
        pages = skipped = 0
        cursor: str | None = None
        budget = self._monotonic() + TOOL_LIST_SECONDS
        cutoff = min(budget, self._deadline)
        while True:
            if cutoff - self._monotonic() < START_SECONDS:
                outcome = RunCut.code if cutoff == self._deadline else "budget"
                break
            if pages == TOOL_LIST_PAGES:
                outcome = "more"
                break
            listed = self._checkpointed(
                partial(self._session.list_tools, cursor, deadline=cutoff)
            )
            if isinstance(listed, McpFailure):
                outcome = _translate(listed).code
                if outcome == "rate_limited":
                    # Give the provider room before ``list_accounts``, inside the
                    # listing's own budget, so a throttled listing does not turn
                    # into a throttled run.
                    self._hold_off(listed.retry_after, cutoff)
                break
            pages += 1
            entries = listed.get("tools")
            if not isinstance(entries, list):
                outcome = "malformed"
                break
            for entry in entries:
                tool = _listed_tool(entry)
                if tool is None or len(tools) == TOOL_LIST_MAX:
                    skipped += 1
                else:
                    tools.append(tool)
            following = listed.get("nextCursor")
            if following is None:
                outcome = "ok"
                break
            if not isinstance(following, str) or not following:
                outcome = "malformed"
                break
            cursor = following
        return ToolListing(
            tools=tuple(tools), pages=pages, skipped=skipped, outcome=outcome
        )

    def _hold_off(self, retry_after: int | None, cutoff: float) -> None:
        wait = max(RETRY_SPACING, float(retry_after or 0))
        wait = min(wait, cutoff - self._monotonic())
        if wait > 0:
            self._sleep(wait)

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
        return self._paced(self._find_jobs, arguments, cutoff)

    def invitations(
        self, org_uid: str, params: Mapping[str, object]
    ) -> dict[str, object]:
        """One paced listing of the account's invitations and its JSON document."""
        arguments = {
            "action": INVITATIONS,
            "org_uid": org_uid,
            "params": dict(params),
        }
        return self._paced(self._proposals, arguments, self._deadline)

    def _paced(
        self, name: str, arguments: Mapping[str, object], cutoff: float
    ) -> dict[str, object]:
        """One paced tool call and its JSON document, with the one rate-limit
        retry (module docstring)."""
        retried = False
        while True:
            self._pace(cutoff)
            outcome = self._checkpointed(
                lambda: self._session.call_tool(name, arguments),
                cutoff=cutoff,
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

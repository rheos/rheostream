"""The job feed's upstream wrapper on a fake clock: no Postgres, no network.

A fake session and a fake lease token share one event log, so a test reads the exact
order of pacing waits, forced and ordinary checkpoints, session calls and parses.
The read-only policy is also driven through a real MCP session over a counting
``httpx.MockTransport``.
"""

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

import httpx
import pytest
from rheo_core.connectors.mcp_client import McpFailure, McpSession, ToolRefused
from rheo_core.connectors.oauth import AccessToken
from rheo_leads.jobfeed.upstream import (
    READ_ONLY_POLICY,
    Account,
    RunCut,
    Upstream,
    UpstreamFailure,
)

# The specified values, written out so a change to the module's constants fails.
FIND_JOBS_SPACING = 5.3
RETRY_SPACING = 5.5
SENSITIVE_RATE_LIMIT = "SENSITIVE_RATE_LIMIT_EXCEEDED"

ORG = "org-marker-5d2a7c19-3e8b-4f60-a1d4-7b9c2e5f8a03"
ACCESS = "access-marker-0c7e4a92-6b1d-4e35-8f27-3a9d5c1b7e64"
MARKERS = (ORG, ACCESS)
RESOURCE = "https://mcp.example.com/mcp"
PUBLIC = "2600::1"
START = 1000.0
DEADLINE = START + 420.0
EPSILON = 1e-9

Script = Callable[[str, Mapping[str, object]], dict[str, object]]


class Clock:
    def __init__(self, log: list[str]) -> None:
        self.now = START
        self.log = log
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.log.append("sleep")
        self.slept.append(seconds)
        self.now += seconds


class LeaseToken:
    """The worker's lease semantics: 60 s lease, a 20 s throttle that ``force``
    skips. Records every checkpoint and every extension time."""

    LEASE = 60.0
    HEARTBEAT = 20.0

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.extensions: list[float] = []

    def checkpoint(self, now: object = None, *, force: bool = False) -> None:
        at = self.clock.now
        self.clock.log.append("forced" if force else "ordinary")
        last = self.extensions[-1] if self.extensions else None
        if force or last is None or at - last >= self.HEARTBEAT:
            self.extensions.append(at)

    def covered(self, at: float) -> bool:
        """Whether the lease was live at ``at``."""
        before = [e for e in self.extensions if e <= at]
        return bool(before) and at - before[-1] <= self.LEASE


class LoggedContent(list[Any]):
    """A content list that records when the wrapper reads it."""

    log: list[str]

    def __iter__(self) -> Iterator[Any]:
        self.log.append("parse")
        return super().__iter__()


@dataclass
class FakeSession:
    """Records which of its three methods ran, and when each tool call started."""

    clock: Clock
    script: Script = field(default_factory=lambda: lambda name, args: {})
    duration: float = 0.0
    set_up_failure: McpFailure | None = None
    starts: list[tuple[float, str, object]] = field(default_factory=list)
    arguments: list[Mapping[str, object]] = field(default_factory=list)
    exited: int = 0

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exited += 1

    def initialize(self) -> None:
        self.clock.log.append("initialize")
        if self.set_up_failure is not None:
            raise self.set_up_failure

    def notify_initialized(self) -> None:
        self.clock.log.append("notify_initialized")

    def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        self.clock.log.append(f"call_tool:{name}")
        self.starts.append((self.clock.now, name, arguments.get("action")))
        self.arguments.append(arguments)
        self.clock.now += self.duration
        return self.script(name, arguments)

    def attempts(self, name: str = "find_jobs") -> int:
        return sum(1 for _, called, _ in self.starts if called == name)


def text_result(log: list[str], document: object) -> dict[str, object]:
    content = LoggedContent([{"type": "text", "text": json.dumps(document)}])
    content.log = log
    return {"content": content}


@dataclass
class Rig:
    log: list[str]
    clock: Clock
    token: LeaseToken
    session: FakeSession

    def upstream(self, *, deadline: float = DEADLINE) -> Upstream:
        return Upstream(
            self.session,
            self.token,
            deadline=deadline,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )

    def answer(self, document: object) -> dict[str, object]:
        return text_result(self.log, document)


@pytest.fixture
def rig() -> Rig:
    log: list[str] = []
    clock = Clock(log)
    rig = Rig(log, clock, LeaseToken(clock), FakeSession(clock))
    rig.session.script = lambda name, args: rig.answer(
        {"accounts": []} if name == "list_accounts" else {"status": "ok"}
    )
    return rig


def assert_content_free(exc: BaseException, code: str) -> None:
    assert str(exc) == code
    assert repr(exc) == code
    assert exc.args == (code,)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    for marker in MARKERS:
        assert marker not in str(exc) and marker not in repr(exc)


def raises(fn: Callable[[], object], kind: type[Exception]) -> Exception:
    with pytest.raises(kind) as caught:
        fn()
    return caught.value


def sensitive() -> McpFailure:
    return McpFailure("tool_error", tool_code=SENSITIVE_RATE_LIMIT)


# --- the read-only policy ---------------------------------------------------------


def test_read_only_policy_is_exactly_the_allowlist() -> None:
    assert dict(READ_ONLY_POLICY) == {
        "list_accounts": None,
        "find_jobs": frozenset({"smart_search", "search", "get"}),
    }


@dataclass
class CountingMcp:
    """A real MCP endpoint stand-in behind a counting mock transport."""

    tool: Callable[[dict[str, Any]], httpx.Response] | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-4e1b"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": "2025-06-18"},
                },
            )
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if self.tool is not None:
            return self.tool(body)
        name = body["params"]["name"]
        document: dict[str, Any] = (
            {"accounts": [{"org_uid": ORG, "role": "TALENT", "name": "Freelancer"}]}
            if name == "list_accounts"
            else {"status": "ok", "jobs": []}
        )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": json.dumps(document)}]},
            },
        )

    def methods(self) -> list[tuple[str, str | None, str | None]]:
        found = []
        for request in self.requests:
            body = json.loads(request.content)
            params = body.get("params") or {}
            arguments = params.get("arguments") or {}
            found.append((body["method"], params.get("name"), arguments.get("action")))
        return found


def real_upstream(mcp: CountingMcp, rig: Rig) -> Upstream:
    session = McpSession(
        RESOURCE,
        AccessToken(ACCESS),
        READ_ONLY_POLICY,
        deadline=DEADLINE,
        transport=httpx.MockTransport(mcp.handle),
        resolver=lambda host: [PUBLIC],
        monotonic=rig.clock.monotonic,
    )
    return Upstream(
        session,
        rig.token,
        deadline=DEADLINE,
        monotonic=rig.clock.monotonic,
        sleep=rig.clock.sleep,
    )


def test_read_only_session_carries_the_token_and_the_exact_argument_shape(
    rig: Rig,
) -> None:
    """Positive control for the refusals below: the allowed calls go through."""
    mcp = CountingMcp()
    with real_upstream(mcp, rig) as upstream:
        accounts = upstream.list_accounts()
        upstream.find_jobs("search", ORG, {"limit": 10, "cursor": "c-2"})
    assert accounts == (Account(ORG, "Freelancer", is_talent=True),)
    assert mcp.methods() == [
        ("initialize", None, None),
        ("notifications/initialized", None, None),
        ("tools/call", "list_accounts", None),
        ("tools/call", "find_jobs", "search"),
    ]
    assert json.loads(mcp.requests[-1].content)["params"] == {
        "name": "find_jobs",
        "arguments": {
            "action": "search",
            "org_uid": ORG,
            "params": {"limit": 10, "cursor": "c-2"},
        },
    }
    for request in mcp.requests:
        assert str(request.url) == RESOURCE
        assert request.headers["authorization"] == f"Bearer {ACCESS}"


@pytest.mark.parametrize(
    "call",
    [
        lambda u: u._session.call_tool("submit_proposal", {"job": "~0212"}),
        lambda u: u._session.call_tool("remote__find_jobs", {"action": "search"}),
        lambda u: u.find_jobs("apply", ORG, {}),
        lambda u: u.find_jobs("withdraw", ORG, {"id": "12"}),
        lambda u: u._session.call_tool("list_accounts", {"action": "get"}),
    ],
    ids=["write-shaped", "compound-name", "apply", "withdraw", "accounts-action"],
)
def test_read_only_policy_refuses_before_any_send(
    rig: Rig, call: Callable[[Any], object]
) -> None:
    mcp = CountingMcp()
    with real_upstream(mcp, rig) as upstream:
        sent_before = len(mcp.requests)
        refused = raises(lambda: call(upstream), ToolRefused)
    assert str(refused) in {"tool_not_allowed", "action_not_allowed"}
    assert len(mcp.requests) == sent_before == 2


def test_refused_credential_is_terminal_and_content_free(rig: Rig) -> None:
    mcp = CountingMcp(tool=lambda body: httpx.Response(401, text=f"bad {ACCESS}"))
    with real_upstream(mcp, rig) as upstream:
        failure = raises(lambda: upstream.list_accounts(), UpstreamFailure)
    assert isinstance(failure, UpstreamFailure)
    assert failure.terminal is True
    assert_content_free(failure, "unauthorized")


@pytest.mark.parametrize(
    ("answer", "code", "retry_after"),
    [
        (lambda body: httpx.Response(503, text=ORG), "unavailable", None),
        (
            lambda body: httpx.Response(429, headers={"Retry-After": "120"}),
            "rate_limited",
            120,
        ),
        (
            lambda body: (_ for _ in ()).throw(httpx.ConnectError(f"refused {ORG}")),
            "unavailable",
            None,
        ),
    ],
    ids=["5xx", "429", "transport"],
)
def test_wire_failures_are_attempted_once(
    rig: Rig,
    answer: Callable[[dict[str, Any]], httpx.Response],
    code: str,
    retry_after: int | None,
) -> None:
    mcp = CountingMcp(tool=answer)
    with real_upstream(mcp, rig) as upstream:
        failure = raises(lambda: upstream.find_jobs("get", ORG, {"id": "1"}), Exception)
    assert isinstance(failure, UpstreamFailure)
    assert_content_free(failure, code)
    assert failure.retry_after == retry_after
    assert failure.terminal is False
    assert mcp.methods().count(("tools/call", "find_jobs", "get")) == 1


def tool_error_then(
    errors: int, error: Mapping[str, object]
) -> Callable[[dict[str, Any]], httpx.Response]:
    """``errors`` ``isError`` results carrying ``error``, then a successful page."""
    sent = iter(range(1_000))

    def answer(body: dict[str, Any]) -> httpx.Response:
        failed = next(sent) < errors
        document: object = error if failed else {"status": "ok", "jobs": []}
        result: dict[str, Any] = {
            "content": [{"type": "text", "text": json.dumps(document)}]
        }
        if failed:
            result["isError"] = True
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    return answer


SENSITIVE_ON_THE_WIRE = {"error_code": SENSITIVE_RATE_LIMIT, "reason": f"slow {ORG}"}


def test_a_wire_sensitive_rate_limit_is_retried_exactly_once(rig: Rig) -> None:
    mcp = CountingMcp(tool=tool_error_then(1, SENSITIVE_ON_THE_WIRE))
    with real_upstream(mcp, rig) as upstream:
        document = upstream.find_jobs("search", ORG, {"limit": 10})
    assert document == {"status": "ok", "jobs": []}
    assert mcp.methods().count(("tools/call", "find_jobs", "search")) == 2
    assert rig.clock.slept == [pytest.approx(RETRY_SPACING)]


def test_a_second_wire_sensitive_rate_limit_fails_after_two_attempts(
    rig: Rig,
) -> None:
    mcp = CountingMcp(tool=tool_error_then(2, SENSITIVE_ON_THE_WIRE))
    with real_upstream(mcp, rig) as upstream:
        failure = raises(lambda: upstream.find_jobs("get", ORG, {"id": "1"}), Exception)
    assert isinstance(failure, UpstreamFailure)
    assert_content_free(failure, "rate_limited")
    assert mcp.methods().count(("tools/call", "find_jobs", "get")) == 2


def test_a_wire_tool_error_with_another_code_is_not_retried(rig: Rig) -> None:
    other = {"error_code": "JOB_NOT_FOUND", "reason": f"missing {ORG}"}
    mcp = CountingMcp(tool=tool_error_then(1, other))
    with real_upstream(mcp, rig) as upstream:
        failure = raises(lambda: upstream.find_jobs("get", ORG, {"id": "1"}), Exception)
    assert isinstance(failure, UpstreamFailure)
    assert_content_free(failure, "tool_error")
    assert mcp.methods().count(("tools/call", "find_jobs", "get")) == 1


# --- checkpoint ordering ---------------------------------------------------------


def test_every_call_is_bracketed_by_forced_and_ordinary_checkpoints(rig: Rig) -> None:
    with rig.upstream() as upstream:
        upstream.list_accounts()
        upstream.find_jobs("search", ORG, {})
        upstream.find_jobs("get", ORG, {"id": "1"})
    assert rig.log == [
        "forced",
        "initialize",
        "ordinary",
        "forced",
        "notify_initialized",
        "ordinary",
        "forced",
        "call_tool:list_accounts",
        "ordinary",
        "parse",
        "forced",
        "call_tool:find_jobs",
        "ordinary",
        "parse",
        "sleep",
        "forced",
        "call_tool:find_jobs",
        "ordinary",
        "parse",
    ]
    assert rig.session.exited == 1


def test_the_retry_is_bracketed_and_its_delay_follows_the_ordinary_checkpoint(
    rig: Rig,
) -> None:
    answers = iter([sensitive(), None])

    def script(name: str, args: Mapping[str, object]) -> dict[str, object]:
        failure = next(answers)
        if failure is not None:
            raise failure
        return rig.answer({"status": "ok"})

    rig.session.script = script
    with rig.upstream() as upstream:
        rig.log.clear()
        upstream.find_jobs("smart_search", ORG, {"mode": "best_match"})
    assert rig.log == [
        "forced",
        "call_tool:find_jobs",
        "ordinary",
        "sleep",
        "forced",
        "call_tool:find_jobs",
        "ordinary",
        "parse",
    ]


def test_a_failed_set_up_call_is_checkpointed_exits_the_session_and_is_content_free(
    rig: Rig,
) -> None:
    rig.session.set_up_failure = McpFailure("unauthorized")
    failure = raises(lambda: rig.upstream().__enter__(), UpstreamFailure)
    assert rig.log == ["forced", "initialize", "ordinary"]
    assert rig.session.exited == 1
    assert isinstance(failure, UpstreamFailure) and failure.terminal
    assert_content_free(failure, "unauthorized")


def test_a_long_call_then_the_retry_delay_never_outlives_the_lease(rig: Rig) -> None:
    """A 55 s call that ends in the sensitive limit is followed by an ordinary
    extension before the 5.5 s retry delay, and the retry starts forced."""
    rig.session.duration = 55.0
    answers = iter([sensitive(), None])

    def script(name: str, args: Mapping[str, object]) -> dict[str, object]:
        failure = next(answers)
        if failure is not None:
            raise failure
        return rig.answer({"status": "ok"})

    rig.session.script = script
    with rig.upstream() as upstream:
        first = rig.clock.now
        upstream.find_jobs("get", ORG, {"id": "7"})
    end = rig.clock.now
    assert first + 55.0 in rig.token.extensions
    (retry_start,) = [at for at, _, _ in rig.session.starts[1:]]
    assert retry_start >= first + 55.0 + RETRY_SPACING - EPSILON
    assert retry_start in rig.token.extensions
    step = 0.5
    at = first
    while at <= end:
        assert rig.token.covered(at), at
        at += step


# --- pacing ---------------------------------------------------------------------


def test_find_jobs_starts_are_spaced_across_every_action(rig: Rig) -> None:
    durations = iter([0.0, 2.0, 0.0, 9.0, 0.0, 0.0])

    def script(name: str, args: Mapping[str, object]) -> dict[str, object]:
        rig.clock.now += next(durations)
        return rig.answer({"status": "ok"})

    rig.session.script = script
    actions = ["search", "smart_search", "get", "search", "get", "smart_search"]
    with rig.upstream() as upstream:
        for action in actions:
            upstream.find_jobs(action, ORG, {})  # type: ignore[arg-type]
    starts = [at for at, name, _ in rig.session.starts if name == "find_jobs"]
    assert [a for _, _, a in rig.session.starts] == actions
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:], strict=False)]
    assert all(gap >= FIND_JOBS_SPACING - EPSILON for gap in gaps), gaps
    # A call longer than the spacing is followed at once: no wait beyond it.
    assert gaps[3] == pytest.approx(9.0)
    assert gaps[0] == pytest.approx(FIND_JOBS_SPACING)


def test_set_up_calls_are_not_paced(rig: Rig) -> None:
    with rig.upstream() as upstream:
        upstream.list_accounts()
        upstream.find_jobs("search", ORG, {})
    assert rig.clock.slept == []


@pytest.mark.parametrize(
    ("deadline_after_next_start", "made"),
    [(-0.2, False), (0.0, False), (4.99, False), (5.0, True), (30.0, True)],
)
def test_a_pacing_wait_reaching_the_deadline_cuts_the_run(
    rig: Rig, deadline_after_next_start: float, made: bool
) -> None:
    next_start = START + FIND_JOBS_SPACING
    with rig.upstream(deadline=next_start + deadline_after_next_start) as upstream:
        upstream.find_jobs("search", ORG, {})
        if made:
            upstream.find_jobs("get", ORG, {"id": "3"})
        else:
            cut = raises(lambda: upstream.find_jobs("get", ORG, {"id": "3"}), RunCut)
            assert_content_free(cut, "run_cut")
            assert rig.clock.slept == []
    assert rig.session.attempts() == (2 if made else 1)


def test_a_call_deadline_earlier_than_the_run_cuts_too(rig: Rig) -> None:
    with rig.upstream() as upstream:
        cut = raises(
            lambda: upstream.find_jobs("get", ORG, {"id": "3"}, deadline=START + 4.0),
            RunCut,
        )
        upstream.find_jobs("get", ORG, {"id": "3"}, deadline=DEADLINE + 1000.0)
    assert_content_free(cut, "run_cut")
    assert rig.session.attempts() == 1


# --- the single retry -----------------------------------------------------------


def test_sensitive_rate_limit_is_retried_once_at_least_5_5_s_later(rig: Rig) -> None:
    answers = iter([sensitive(), None])

    def script(name: str, args: Mapping[str, object]) -> dict[str, object]:
        rig.clock.now += 1.0
        failure = next(answers)
        if failure is not None:
            raise failure
        return rig.answer({"status": "ok", "jobs": []})

    rig.session.script = script
    with rig.upstream() as upstream:
        document = upstream.find_jobs("search", ORG, {"limit": 10})
    assert document == {"status": "ok", "jobs": []}
    (first, _, _), (second, _, _) = rig.session.starts
    assert second - first >= 1.0 + RETRY_SPACING - EPSILON
    assert second - first >= FIND_JOBS_SPACING
    assert rig.session.arguments[0] == rig.session.arguments[1]


def test_a_second_sensitive_rate_limit_fails_the_call(rig: Rig) -> None:
    def script(name: str, args: Mapping[str, object]) -> dict[str, object]:
        raise sensitive()

    rig.session.script = script
    with rig.upstream() as upstream:
        failure = raises(lambda: upstream.find_jobs("get", ORG, {"id": "4"}), Exception)
    assert isinstance(failure, UpstreamFailure)
    assert_content_free(failure, "rate_limited")
    assert failure.retry_after is None and failure.terminal is False
    assert rig.session.attempts() == 2


@pytest.mark.parametrize(
    ("raised", "code", "retry_after", "terminal"),
    [
        (McpFailure("unavailable"), "unavailable", None, False),
        (McpFailure("rate_limited", retry_after=30), "rate_limited", 30, False),
        (McpFailure("tool_error", tool_code="OTHER_ERROR"), "tool_error", None, False),
        (McpFailure("tool_error"), "tool_error", None, False),
        (McpFailure("protocol"), "protocol", None, False),
        (
            McpFailure("endpoint_misconfigured"),
            "endpoint_misconfigured",
            None,
            False,
        ),
        (McpFailure("unauthorized"), "unauthorized", None, True),
    ],
    ids=[
        "unavailable",
        "http-429",
        "other-tool-error",
        "uncoded-tool-error",
        "protocol",
        "misconfigured",
        "unauthorized",
    ],
)
def test_no_other_failure_is_retried(
    rig: Rig,
    raised: McpFailure,
    code: str,
    retry_after: int | None,
    terminal: bool,
) -> None:
    def script(name: str, args: Mapping[str, object]) -> dict[str, object]:
        raise raised

    rig.session.script = script
    with rig.upstream() as upstream:
        failure = raises(lambda: upstream.find_jobs("search", ORG, {}), Exception)
    assert isinstance(failure, UpstreamFailure)
    assert_content_free(failure, code)
    assert failure.retry_after == retry_after
    assert failure.terminal is terminal
    assert rig.session.attempts() == 1
    assert rig.clock.slept == []


# --- parsing --------------------------------------------------------------------


def test_list_accounts_reads_role_and_label(rig: Rig) -> None:
    rig.session.script = lambda name, args: rig.answer(
        {
            "accounts": [
                {"org_uid": ORG, "role": "TALENT", "name": "Freelancer"},
                {"org_uid": "org-2", "role": "CLIENT", "name": ""},
                {"org_uid": "org-3", "role": "TALENT"},
            ]
        }
    )
    with rig.upstream() as upstream:
        accounts = upstream.list_accounts()
    assert accounts == (
        Account(ORG, "Freelancer", is_talent=True),
        Account("org-2", None, is_talent=False),
        Account("org-3", None, is_talent=True),
    )


UNPARSEABLE: Sequence[dict[str, object]] = [
    {"content": [{"type": "text", "text": f"not json {ORG}"}]},
    {"content": [{"type": "text", "text": json.dumps([ORG])}]},
    {
        "content": [
            {"type": "text", "text": json.dumps({"accounts": []})},
            {"type": "text", "text": json.dumps({"accounts": []})},
        ]
    },
    {"content": []},
    {},
    {"content": [{"type": "text", "text": json.dumps({"accounts": ORG})}]},
    {"content": [{"type": "text", "text": json.dumps({"accounts": [{"role": ORG}]})}]},
    {"content": [{"type": "text", "text": json.dumps({"accounts": [ORG]})}]},
]


@pytest.mark.parametrize("result", UNPARSEABLE)
def test_an_unparseable_account_list_is_a_content_free_protocol_failure(
    rig: Rig, result: dict[str, object]
) -> None:
    rig.session.script = lambda name, args: result
    with rig.upstream() as upstream:
        failure = raises(lambda: upstream.list_accounts(), UpstreamFailure)
    assert_content_free(failure, "protocol")


@pytest.mark.parametrize("result", UNPARSEABLE[:5])
def test_an_unparseable_search_result_is_a_content_free_protocol_failure(
    rig: Rig, result: dict[str, object]
) -> None:
    rig.session.script = lambda name, args: result
    with rig.upstream() as upstream:
        failure = raises(lambda: upstream.find_jobs("search", ORG, {}), UpstreamFailure)
    assert_content_free(failure, "protocol")
    assert rig.session.attempts() == 1

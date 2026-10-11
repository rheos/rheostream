"""The ``leads.jobfeed_collect`` job: gate checks, the claim, the one refresh and
its rotation, guarded run-ending writes, and the terminal and transient outcomes.

Seams under test: the job through a real scheduler tick and worker visit on a fake
clock (or the handler invoked directly, as the worker invokes it, where a test needs
its own cancellation token), against a fake authorization server with counting
token, revocation and registration endpoints. The connected collector is built by
the real OAuth client, so every refresh-token and client-secret file is a real file
under a temporary data root and every "file gone" or "file kept" assertion reads
the file system. Owner operations that race a run go through the real dispatcher on
connections of their own.
"""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from harness.jobfeed import (
    CLIENT_SECRET,
    ISSUER,
    ORGANIZATION,
    REGISTRATION_TOKEN,
    STORED_CLIENT_ID,
    WRITE_TOOLS,
    ConnectedCollector,
    FakeAuthorizationServer,
    FakeClock,
    FakeMcpServer,
    FakeResolver,
    Hold,
    Responder,
    Tokens,
    account,
    accounts_result,
    after_claim,
    after_step_one_read,
    answer,
    arguments_of,
    before,
    collect_jobs,
    configure_connector,
    connected_collector,
    grant_type,
    invoke,
    issued,
    jobsearch_pipeline,
    owner_web,
    raising,
    refresh_requests,
    serve,
    slow,
    tick,
    token_error,
    tool_result,
    visit,
)
from rheo_contracts import WorkspaceContext
from rheo_core.deletion import WORKSPACE_LIFECYCLE_LOCK_KEY
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.storage.control_plane import set_workspace_state
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import audit_record, job
from rheo_core.work.cancellation import JobCancelled
from rheo_leads.intake.accept import health_write
from rheo_leads.jobfeed.run import CollectPayload, JobfeedFailure, collect_job
from rheo_leads.jobfeed.state import AUTHORIZATION_LOST, account_choices
from rheo_leads.references import ref
from sqlalchemy import Engine, event, select, text

from postgres.test_jobfeed_authorization import (
    client_of,
    collect_schedule,
    complete,
    connected,
    health,
    restart,
    result,
    set_connection,
    stored,
)
from postgres.test_jobfeed_connect import capability, file_of, run, secret_files
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres

JOB_HOST = "www.example.com"
REVOKE = "leads.jobfeed.revoke"
SELECT = "leads.jobfeed.select_account"
HOUR = 3600.0
SLOW_REFRESH = 14.9
TWO_HOURS = timedelta(hours=2)
CHOICES = (("org-example-1", "Example organization one"),)


class InjectedFailure(Exception):
    """Raised from a test listener to fail a statement on purpose."""


# --- fixtures and helpers -----------------------------------------------------------


@pytest.fixture
def root(intake: Intake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    configure_connector(monkeypatch, tmp_path, job_hosts=JOB_HOST)
    return tmp_path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver()


@pytest.fixture
def mcp() -> FakeMcpServer:
    return FakeMcpServer()


@pytest.fixture
def server() -> FakeAuthorizationServer:
    return FakeAuthorizationServer(token=Tokens(rotating=False))


@pytest.fixture
def served(
    root: Path,
    server: FakeAuthorizationServer,
    clock: FakeClock,
    resolver: FakeResolver,
    mcp: FakeMcpServer,
) -> Iterator[FakeAuthorizationServer]:
    with serve(server, clock=clock, resolver=resolver, mcp=mcp):
        yield server


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode("ascii"))


def revoked_tokens(server: FakeAuthorizationServer) -> list[str]:
    return [form(r)["token"][0] for r in server.revocation_requests]


def run_at(c: ConnectedCollector, clock: FakeClock, hours: float = 0.0) -> None:
    clock.advance(hours * HOUR)
    visit(c.intake, clock)


def revoke(c: ConnectedCollector) -> None:
    outcome = run(c.intake, c.owner, REVOKE)
    assert outcome.ok, outcome


def reconnect(c: ConnectedCollector, server: FakeAuthorizationServer) -> None:
    params = restart(c.intake, c.owner, server)
    assert result(complete(c.intake, c.owner, **params)) == ("connected", None)


def refresh_with(responder: Responder) -> Responder:
    """``responder`` for refresh grants; any code exchange gets a fresh token."""
    exchange = Tokens()

    def respond(request: httpx.Request) -> httpx.Response:
        if grant_type(request) == "refresh_token":
            return responder(request)
        return exchange(request)

    return respond


def lost_audits(ctx: WorkspaceContext) -> list[dict[str, Any]]:
    with open_unit_of_work(ctx) as uow:
        return [
            dict(row)
            for row in uow.connection.execute(
                select(audit_record).where(
                    audit_record.c.operation_name == AUTHORIZATION_LOST
                )
            ).mappings()
        ]


def last_error(c: ConnectedCollector) -> str | None:
    row = health(c.owner, c.connection_id)
    return None if row is None else row["last_error"]


def quietly_succeeded(c: ConnectedCollector, runs: int = 1) -> None:
    """Every collector job ran once and succeeded, with no error text."""
    jobs = collect_jobs(c.owner)
    assert [(j.state, j.last_error, j.attempts) for j in jobs] == [
        ("succeeded", None, 1)
    ] * runs


def markers(root: Path, c: ConnectedCollector) -> list[str]:
    return [
        c.refresh_token,
        c.token_ref,
        CLIENT_SECRET,
        REGISTRATION_TOKEN,
        str(root),
        "SecretRefusal",
    ]


def commit_watch(prefix: str, check: Callable[[], Any]) -> Callable[[], list[Any]]:
    """Run ``check`` when the connection that executed a statement starting with
    ``prefix`` commits (the ``commit`` event fires before the database commits);
    returns a stop function giving the values seen."""
    seen: list[Any] = []
    pending: set[int] = set()

    def watch(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().upper().startswith(prefix):
            pending.add(id(conn))

    def on_commit(conn: Any) -> None:
        if id(conn) in pending:
            pending.discard(id(conn))
            seen.append(check())

    event.listen(Engine, "before_cursor_execute", watch)
    event.listen(Engine, "commit", on_commit)

    def stop() -> list[Any]:
        event.remove(Engine, "before_cursor_execute", watch)
        event.remove(Engine, "commit", on_commit)
        return seen

    return stop


@dataclass
class LeaseModel:
    """A cancellation token modelled on the worker's (60 s lease, 20 s throttle),
    recording every checkpoint and, through the fake servers, every request."""

    clock: FakeClock
    events: list[Any] = field(default_factory=list)
    extended_at: Any = None
    lease_until: Any = None

    def checkpoint(self, now: Any = None, *, force: bool = False) -> None:
        at = self.clock.now()
        self.events.append(("checkpoint", force))
        if (
            not force
            and self.extended_at is not None
            and at - self.extended_at < timedelta(seconds=20)
        ):
            return
        self.extended_at = at
        self.lease_until = at + timedelta(seconds=60)


class NoToken:
    def checkpoint(self, now: Any = None, *, force: bool = False) -> None:
        return None


@dataclass
class CancelledFrom:
    """A token whose job is cancelled from its ``stop_at``-th checkpoint (counting
    from 0) on: that call and every later one raise, as a cancelled job's do."""

    stop_at: int
    calls: list[bool] = field(default_factory=list)

    def checkpoint(self, now: Any = None, *, force: bool = False) -> None:
        self.calls.append(force)
        if len(self.calls) > self.stop_at:
            raise JobCancelled("job cancelled")


# --- the claim: one refresh, no overlap, 60-minute spacing ---------------------------


def test_two_concurrent_attempts_make_exactly_one_refresh(
    intake: Intake, served: FakeAuthorizationServer
) -> None:
    """AC 14, I8."""
    c = connected_collector(intake, served)
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def attempt() -> None:
        try:
            barrier.wait(timeout=30)
            invoke(c, NoToken())
        except BaseException as error:  # reported below
            failures.append(error)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert failures == []
    assert len(refresh_requests(served)) == 1
    assert served.registrations == []


def test_a_run_in_flight_is_not_overlapped_and_the_next_waits_60_minutes(
    intake: Intake, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """AC 25."""
    c = connected_collector(intake, served)
    hold = Hold()
    served.holds["token"] = hold
    first = threading.Thread(target=lambda: invoke(c, NoToken()))
    first.start()
    try:
        assert hold.entered.wait(timeout=30)
        invoke(c, NoToken())  # while the first run is waiting on its refresh
    finally:
        hold.release.set()
        first.join(timeout=60)
    del served.holds["token"]
    assert len(refresh_requests(served)) == 1
    claimed = stored(c.owner).run_claimed_at
    clock.advance(59 * 60)
    invoke(c, NoToken())
    assert len(refresh_requests(served)) == 1
    assert stored(c.owner).run_claimed_at == claimed
    clock.advance(2 * 60)
    claimed_at = clock.now()
    invoke(c, NoToken())
    assert len(refresh_requests(served)) == 2
    assert stored(c.owner).run_claimed_at == claimed_at


# --- the refresh: checkpoints, lease and one attempt ---------------------------------


def test_a_forced_extension_precedes_the_refresh_and_the_lost_race_revocation(
    intake: Intake, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """AC 35 refresh half, W7, AC 59 (the revocation counted apart)."""
    c = connected_collector(intake, served, registration_token=False)
    token = LeaseModel(clock)
    # A recent extension: an unforced checkpoint now would be throttled.
    token.checkpoint()
    token.events.clear()
    clock.advance(19)
    rotating = Tokens(rotating=True)
    seen: list[Any] = []

    def refresh(request: httpx.Request) -> httpx.Response:
        token.events.append("refresh")
        seen.append((clock.now(), token.lease_until))
        revoke(c)  # the owner's revoke wins the race while the refresh is slow
        # A slow answer just inside the token request's own 15 s budget.
        clock.advance(SLOW_REFRESH)
        return rotating(request)

    def revocation(request: httpx.Request) -> httpx.Response:
        token.events.append(("revocation", form(request)["token"][0]))
        return httpx.Response(200)

    served.token = refresh
    served.revoke = revocation
    invoke(c, token)

    minted = rotating.issued[-1]
    assert minted.startswith("refresh-marker-")
    assert token.events == [
        ("checkpoint", True),
        "refresh",
        ("revocation", c.refresh_token),  # the owner's revoke, inside the call
        ("checkpoint", False),
        ("checkpoint", True),
        ("revocation", minted),  # the run's own, not a refresh retry
        ("checkpoint", False),
    ]
    ((started, lease_until),) = seen
    assert lease_until == started + timedelta(seconds=60)
    assert started + timedelta(seconds=SLOW_REFRESH) < lease_until
    assert len(refresh_requests(served)) == 1
    assert len(served.revocation_requests) == 2


@pytest.mark.parametrize(
    ("responder", "code"),
    [
        (answer(503), "token_refresh_failed"),
        (token_error("invalid_request"), "token_refresh_failed"),
        (answer(429, headers={"Retry-After": "60"}), "upstream_rate_limited"),
        (answer(302, headers={"Location": f"{ISSUER}/elsewhere"}), None),
    ],
    ids=["5xx", "other-4xx", "429", "redirect"],
)
def test_a_transient_refresh_failure_is_tried_once_and_backs_off(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    responder: Responder,
    code: str | None,
) -> None:
    """AC 48 token side, AC 49, AC 59 refresh half; a redirected token endpoint is
    ``endpoint_misconfigured``, non-terminal, with the same counters."""
    expected = code or "endpoint_misconfigured"
    c = connected_collector(intake, served)
    served.token = responder
    run_at(c, clock)
    quietly_succeeded(c)
    assert len(refresh_requests(served)) == 1
    after = stored(c.owner)
    assert last_error(c) == expected
    assert after.consecutive_failures == 1
    assert after.not_before == clock.now() + TWO_HOURS
    assert after.authorization_state == "connected"
    assert after.token_ref == c.token_ref
    assert collect_schedule(c.owner)["enabled"] is True  # type: ignore[index]
    # Not re-run before the next scheduled run.
    run_at(c, clock, hours=1)
    assert len(collect_jobs(c.owner)) == 1
    assert len(refresh_requests(served)) == 1
    # The next scheduled run fails again: the wait doubles.
    run_at(c, clock, hours=1)
    assert len(refresh_requests(served)) == 2
    assert stored(c.owner).consecutive_failures == 2
    assert stored(c.owner).not_before == clock.now() + 2 * TWO_HOURS
    # A success clears the code and resets the wait.
    served.token = Tokens(rotating=False)
    run_at(c, clock, hours=4)
    assert len(refresh_requests(served)) == 3
    assert last_error(c) is None
    assert stored(c.owner).consecutive_failures == 0
    assert stored(c.owner).not_before is None
    quietly_succeeded(c, runs=3)
    assert served.registrations == []


@pytest.mark.parametrize(
    ("failures", "retry_after", "wait"),
    [
        (0, 30_000, timedelta(seconds=30_000)),
        (0, 999_999, timedelta(hours=24)),
        (5, 60, timedelta(hours=24)),
        (2, 60, timedelta(hours=8)),
    ],
    ids=["retry-after-wins", "retry-after-capped", "doubling-capped", "doubled"],
)
def test_the_backoff_takes_the_larger_of_the_doubling_and_retry_after(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    failures: int,
    retry_after: int,
    wait: timedelta,
) -> None:
    """AC 48: ``now + max(min(2 h * 2^(n-1), 24 h), min(Retry-After, 24 h))``."""
    c = connected_collector(intake, served, consecutive_failures=failures)
    served.token = answer(429, headers={"Retry-After": str(retry_after)})
    run_at(c, clock)
    assert stored(c.owner).not_before == clock.now() + wait
    assert last_error(c) == "upstream_rate_limited"
    # The next scheduled run inside the wait makes no request at all.
    if wait > TWO_HOURS:
        run_at(c, clock, hours=2)
        assert len(refresh_requests(served)) == 1
        quietly_succeeded(c, runs=2)


# --- rotation -------------------------------------------------------------------------


def test_a_rotation_switches_by_compare_and_set_before_retiring_the_old_file(
    intake: Intake, root: Path, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """AC 13 and AC 62 (winning compare-and-set; the old file is discarded only after
    that unit committed), I7."""
    c = connected_collector(intake, served)
    old_file = file_of(root, c.token_ref)
    rotating = Tokens(rotating=True)
    served.token = rotating
    stop = commit_watch(
        "UPDATE LEADS.JOBFEED_CONNECTION SET TOKEN_REF", old_file.exists
    )
    try:
        run_at(c, clock)
    finally:
        at_commit = stop()
    assert at_commit == [True]
    after = stored(c.owner)
    assert after.token_ref is not None and after.token_ref != c.token_ref
    assert after.credential_generation == 2
    assert not old_file.exists()
    minted = rotating.issued[-1]
    assert file_of(root, after.token_ref).read_text() == minted
    assert last_error(c) is None
    quietly_succeeded(c)
    # The next run refreshes with the rotated token.
    run_at(c, clock, hours=2)
    assert form(refresh_requests(served)[-1])["refresh_token"] == [minted]
    assert stored(c.owner).credential_generation == 3


def test_a_failed_store_of_the_rotated_token_keeps_the_old_reference(
    intake: Intake, root: Path, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """AC 13: storing the new token fails, so the old reference stays active and
    ``token_refresh_failed`` is recorded."""
    c = connected_collector(intake, served)
    old_file = file_of(root, c.token_ref)
    directory = old_file.parent
    before_files = secret_files(root)
    rotating = Tokens(rotating=True)

    def respond(request: httpx.Request) -> httpx.Response:
        directory.chmod(0o500)  # the refresh token was read; the write will fail
        return rotating(request)

    served.token = respond
    try:
        run_at(c, clock)
    finally:
        directory.chmod(0o700)
    after = stored(c.owner)
    assert after.token_ref == c.token_ref and after.credential_generation == 1
    assert old_file.read_text() == c.refresh_token
    assert secret_files(root) == before_files
    assert last_error(c) == "token_refresh_failed"
    assert after.consecutive_failures == 1
    quietly_succeeded(c)


def test_revoke_after_a_committed_rotation_revokes_the_new_token(
    intake: Intake, root: Path, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """AC 69 (a), I22: the rotation committed before revoke's lock; revoke sends the
    new token and the old file is already gone."""
    c = connected_collector(intake, served, registration_token=False)
    rotating = Tokens(rotating=True)
    served.token = rotating
    run_at(c, clock)
    minted = rotating.issued[-1]
    assert not file_of(root, c.token_ref).exists()
    revoke(c)
    assert revoked_tokens(served) == [minted]
    after = stored(c.owner)
    assert after.token_ref is None and after.authorization_state == "revoked"
    assert secret_files(root) == set()


def test_a_cancellation_right_after_a_rotating_refresh_still_stores_the_token(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    """The ordinary checkpoint after the refresh raises: the rotated token is
    switched to and the old file retired before the cancellation propagates, so
    a single-use rotation never strands the connection on a dead token."""
    c = connected_collector(intake, served, registration_token=False)
    rotating = Tokens(rotating=True)
    served.token = rotating
    token = CancelledFrom(stop_at=1)
    with pytest.raises(JobCancelled) as raised:
        invoke(c, token)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert token.calls == [True, False]
    after = stored(c.owner)
    assert after.token_ref is not None and after.token_ref != c.token_ref
    assert after.credential_generation == 2
    assert file_of(root, after.token_ref).read_text() == rotating.issued[-1]
    assert secret_files(root) == {file_of(root, after.token_ref)}
    assert served.revocation_requests == []


@pytest.mark.parametrize(
    "stop_at", [1, 2, 3], ids=["after-refresh", "before-revocation", "after-revocation"]
)
def test_a_cancellation_in_the_lost_race_still_revokes_and_discards(
    intake: Intake, root: Path, served: FakeAuthorizationServer, stop_at: int
) -> None:
    """A checkpoint raising at any point of the lost compare-and-set path: the
    minted token is still revoked once and its file discarded before the
    cancellation propagates."""
    c = connected_collector(intake, served, registration_token=False)
    rotating = Tokens(rotating=True)
    served.token = before(lambda: revoke(c), rotating)
    token = CancelledFrom(stop_at=stop_at)
    with pytest.raises(JobCancelled) as raised:
        invoke(c, token)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert token.calls == [True, False, True, False]
    minted = rotating.issued[-1]
    assert revoked_tokens(served) == [c.refresh_token, minted]
    assert secret_files(root) == set()
    after = stored(c.owner)
    assert after.token_ref is None and after.credential_generation == 2


@contextmanager
def failing_switch_commit(
    *, applied: bool, reread_fails: bool = False
) -> Iterator[None]:
    """The rotation's compare-and-set unit fails. ``applied``: it fails as it
    commits, after the database committed (through the driver connection
    itself), so the caller sees a failure for a commit that took. Otherwise its
    statement fails, so nothing is committed. ``reread_fails`` also fails the
    run's fresh re-read of the token reference."""
    pending: set[int] = set()

    def watch(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        upper = statement.lstrip().upper()
        if upper.startswith("UPDATE LEADS.JOBFEED_CONNECTION SET TOKEN_REF"):
            if not applied:
                raise InjectedFailure("switch failed")
            pending.add(id(conn))
        if reread_fails and upper.startswith(
            "SELECT LEADS.JOBFEED_CONNECTION.TOKEN_REF"
        ):
            raise InjectedFailure("re-read failed")

    def on_commit(conn: Any) -> None:
        if id(conn) in pending:
            pending.discard(id(conn))
            conn.connection.dbapi_connection.commit()
            raise InjectedFailure("commit outcome unknown")

    event.listen(Engine, "before_cursor_execute", watch)
    event.listen(Engine, "commit", on_commit)
    try:
        yield
    finally:
        event.remove(Engine, "before_cursor_execute", watch)
        event.remove(Engine, "commit", on_commit)


def test_a_switch_whose_commit_took_despite_an_error_goes_on_as_won(
    intake: Intake, root: Path, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """The compare-and-set committed but its commit reported a failure: the fresh
    re-read finds the new reference, so the new file is kept, the old one retired
    and the run completes on the new generation."""
    c = connected_collector(intake, served, registration_token=False)
    old_file = file_of(root, c.token_ref)
    rotating = Tokens(rotating=True)
    served.token = rotating
    with failing_switch_commit(applied=True):
        run_at(c, clock)
    after = stored(c.owner)
    assert after.token_ref is not None and after.token_ref != c.token_ref
    assert after.credential_generation == 2
    assert file_of(root, after.token_ref).read_text() == rotating.issued[-1]
    assert not old_file.exists()
    assert secret_files(root) == {file_of(root, after.token_ref)}
    assert served.revocation_requests == []
    assert last_error(c) is None
    quietly_succeeded(c)


def test_a_switch_that_failed_revokes_and_discards_the_minted_token(
    intake: Intake, root: Path, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """The compare-and-set never committed: the row still references the old
    file, which stays; the minted token is revoked once and its file discarded;
    the run fails with the fixed ``internal_error``."""
    c = connected_collector(intake, served, registration_token=False)
    old_file = file_of(root, c.token_ref)
    rotating = Tokens(rotating=True)
    served.token = rotating
    with failing_switch_commit(applied=False):
        run_at(c, clock)
    assert revoked_tokens(served) == [rotating.issued[-1]]
    after = stored(c.owner)
    assert after.token_ref == c.token_ref and after.credential_generation == 1
    assert old_file.read_text() == c.refresh_token
    assert secret_files(root) == {old_file}
    (attempt,) = collect_jobs(c.owner)
    assert attempt.last_error == "internal_error" and attempt.attempts == 1


def test_a_failed_reread_after_an_unknown_switch_discards_nothing(
    intake: Intake, root: Path, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """Neither file is discarded while what the row references is unknown."""
    c = connected_collector(intake, served, registration_token=False)
    served.token = Tokens(rotating=True)
    with failing_switch_commit(applied=True, reread_fails=True):
        run_at(c, clock)
    after = stored(c.owner)
    assert after.token_ref is not None and after.token_ref != c.token_ref
    assert file_of(root, after.token_ref).exists()
    assert file_of(root, c.token_ref).exists()
    assert served.revocation_requests == []
    (attempt,) = collect_jobs(c.owner)
    assert attempt.last_error == "internal_error"


def test_a_reconnect_between_the_first_read_and_the_claim_is_not_claimed(
    intake: Intake, root: Path, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """The claim's generation predicate: credentials stored after the run's first
    read are never refreshed as the client that read saw."""
    c = connected_collector(intake, served)
    client_before = client_of(stored(c.owner))
    served.token = Tokens()
    with after_step_one_read(lambda: reconnect(c, served)):
        run_at(c, clock)
    assert refresh_requests(served) == []
    after = stored(c.owner)
    assert after.credential_generation == 2
    assert after.token_ref is not None and after.token_ref != c.token_ref
    assert file_of(root, after.token_ref).exists()
    assert client_of(after) == client_before
    assert after.run_claimed_at is None
    assert after.authorization_state == "connected"
    assert last_error(c) is None
    quietly_succeeded(c)


def test_a_revoke_that_wins_the_race_leaves_no_live_token(
    intake: Intake, root: Path, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """AC 69 (b), AC 62 (losing compare-and-set), I22: revoke commits before the
    rotation's compare-and-set; revoke sends the old token, the run its newly minted
    one once, then discards its file and writes nothing else."""
    c = connected_collector(intake, served, registration_token=False)
    rotating = Tokens(rotating=True)
    served.token = before(lambda: revoke(c), rotating)
    run_at(c, clock)
    minted = rotating.issued[-1]
    assert sorted(revoked_tokens(served)) == sorted([c.refresh_token, minted])
    assert revoked_tokens(served) == [c.refresh_token, minted]
    assert len(refresh_requests(served)) == 1
    after = stored(c.owner)
    assert after.token_ref is None and after.authorization_state == "revoked"
    assert after.credential_generation == 2  # only revoke's bump
    assert secret_files(root) == set()
    # The run wrote nothing: revoke's own health write stands alone.
    recorded = health(c.owner, c.connection_id)
    assert recorded is not None and recorded["unresolved_failures"] == 1
    assert after.consecutive_failures == 0 and after.not_before is None
    assert lost_audits(c.owner) == []
    assert collect_schedule(c.owner)["enabled"] is False  # type: ignore[index]
    quietly_succeeded(c)


# --- gating and enrolment -------------------------------------------------------------


def test_the_capability_gates_the_ticker_and_a_direct_run(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
) -> None:
    """AC 23 and AC 24."""
    c = connected_collector(intake, served)
    capability(intake, c.owner, False)
    for _ in range(3):
        tick(intake, c.owner, clock)
        clock.advance(2 * HOUR)
    assert collect_jobs(c.owner) == []
    invoke(c, NoToken())
    assert served.requests == [] and mcp.requests == []
    capability(intake, c.owner, True)
    for count in range(1, 4):
        tick(intake, c.owner, clock)
        tick(intake, c.owner, clock)  # a second tick in the same interval adds none
        assert len(collect_jobs(c.owner)) == count
        clock.advance(2 * HOUR)
    assert served.requests == []


def test_an_installed_before_workspace_is_enrolled_by_its_completion(
    intake: Intake, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """AC 26: Leads was enabled before; no schedule row until the completion."""
    owner = owner_web(intake)
    assert collect_schedule(owner) is None
    pipeline = jobsearch_pipeline(intake, owner)
    first = connected(intake, owner, served, pipeline)
    assert collect_schedule(owner)["enabled"] is True  # type: ignore[index]
    exchanges = len(served.token_requests)
    claimed_at = clock.now()
    visit(intake, clock)
    assert len(served.token_requests) == exchanges + 1
    assert grant_type(served.token_requests[-1]) == "refresh_token"
    jobs = collect_jobs(owner)
    assert [(j.state, j.attempts) for j in jobs] == [("succeeded", 1)]
    assert stored(owner).run_claimed_at == claimed_at
    assert stored(owner).connection_id == first.connection_id


def test_a_changed_resource_url_makes_no_request_and_records_misconfigured(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 61 run half."""
    c = connected_collector(intake, served)
    monkeypatch.setenv(
        "RHEO__leads__jobfeed__resource_url", "https://mcp.example.com/v2"
    )
    run_at(c, clock)
    assert served.requests == [] and mcp.requests == []
    assert last_error(c) == "endpoint_misconfigured"
    after = stored(c.owner)
    assert after.authorization_state == "connected"
    assert after.run_claimed_at is None
    quietly_succeeded(c)


def test_a_token_endpoint_that_turned_private_gets_no_byte(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    resolver: FakeResolver,
) -> None:
    """AC 72 run half: public at connect, private at this run."""
    c = connected_collector(intake, served)
    resolver.make_private("auth.example.com")
    run_at(c, clock)
    assert served.requests == []
    assert last_error(c) == "endpoint_misconfigured"
    after = stored(c.owner)
    assert after.authorization_state == "connected"
    assert after.consecutive_failures == 1
    assert after.not_before == clock.now() + TWO_HOURS
    assert lost_audits(c.owner) == []
    quietly_succeeded(c)


@pytest.mark.parametrize("missing", ["client", "token"])
def test_a_connected_row_that_cannot_authenticate_becomes_terminal(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
    missing: str,
) -> None:
    """A connected row with no client or no token reference (a cleared client
    whose terminal write never landed) does not stay connected: the run makes it
    terminal, once, without any request."""
    c = connected_collector(intake, served)
    if missing == "client":
        set_connection(
            c.owner,
            client_id=None,
            client_auth_method=None,
            client_redirect_uri=None,
            client_secret_ref=None,
            registration_token_ref=None,
        )
    else:
        set_connection(c.owner, token_ref=None)
    run_at(c, clock)
    assert served.requests == [] and mcp.requests == []
    after = stored(c.owner)
    assert after.authorization_state == "authorization_required"
    assert after.credential_generation == 1
    assert collect_schedule(c.owner)["enabled"] is False  # type: ignore[index]
    assert last_error(c) == "authorization_required"
    assert len(lost_audits(c.owner)) == 1
    quietly_succeeded(c)
    for _ in range(2):
        run_at(c, clock, hours=2)
    assert served.requests == [] and len(lost_audits(c.owner)) == 1


def test_a_forced_run_of_a_restored_copy_calls_nothing(
    intake: Intake, served: FakeAuthorizationServer, mcp: FakeMcpServer
) -> None:
    """AC 12 (forced run): no token reference and no client."""
    c = connected_collector(intake, served)
    set_connection(
        c.owner,
        authorization_state="authorization_required",
        token_ref=None,
        client_id=None,
        client_auth_method=None,
        client_redirect_uri=None,
        client_secret_ref=None,
        registration_token_ref=None,
    )
    invoke(c, NoToken())
    assert served.requests == [] and mcp.requests == []


def test_a_forced_run_after_a_revoke_calls_nothing(
    intake: Intake, served: FakeAuthorizationServer, mcp: FakeMcpServer
) -> None:
    """AC 15 run half."""
    c = connected_collector(intake, served)
    revoke(c)
    sent = len(served.requests)
    invoke(c, NoToken())
    assert len(served.requests) == sent and mcp.requests == []


# --- terminal outcomes ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "rejected"),
    [
        ("invalid_grant", False),
        ("unauthorized_client", False),
        ("invalid_client", True),
        ("expired_secret", True),
    ],
)
def test_a_terminal_refresh_failure_ends_authorization_until_reconnect(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
    kind: str,
    rejected: bool,
) -> None:
    """AC 47 token side, AC 17 terminal half, AC 63, AC 78, AC 79 run half, I10,
    I31."""
    c = connected_collector(intake, served, auth_method="client_secret_basic")
    files = {name: file_of(root, ref_) for name, ref_ in _client_refs(c).items()}
    assert files and all(f.exists() for f in files.values())
    if kind == "expired_secret":
        set_connection(c.owner, client_secret_expires_at=clock.now() - TWO_HOURS)
    else:
        served.token = token_error(kind)
    stop = commit_watch(
        "UPDATE LEADS.JOBFEED_CONNECTION SET CLIENT_ID",
        lambda: all(f.exists() for f in files.values()),
    )
    try:
        run_at(c, clock)
    finally:
        cleared_at_commit = stop()
    sent = 0 if kind == "expired_secret" else 1
    assert len(served.token_requests) == sent
    after = stored(c.owner)
    assert after.authorization_state == "authorization_required"
    assert collect_schedule(c.owner)["enabled"] is False  # type: ignore[index]
    assert last_error(c) == "authorization_required"
    if rejected:
        # Cleared in a unit that committed with the files present; then discarded.
        assert cleared_at_commit == [True]
        assert after.client_id is None and after.client_auth_method is None
        assert after.client_secret_ref is None and after.registration_token_ref is None
        assert not any(f.exists() for f in files.values())
    else:
        assert cleared_at_commit == []
        assert after.client_id == STORED_CLIENT_ID
        assert all(f.exists() for f in files.values())
    (audit,) = lost_audits(c.owner)
    assert audit["actor_kind"] == "connection" and audit["actor_id"] == c.connection_id
    assert audit["subject_ref"] == ref("intake_connection", c.connection_id)
    assert audit["outcome"] == "succeeded"
    assert not any(m in repr(audit) for m in markers(root, c))
    quietly_succeeded(c)
    # Several later ticks: nothing upstream at all, no registration.
    for _ in range(3):
        run_at(c, clock, hours=2)
    assert len(served.token_requests) == sent
    assert served.registrations == [] and mcp.requests == []
    assert len(collect_jobs(c.owner)) == 1
    assert len(lost_audits(c.owner)) == 1


def _client_refs(c: ConnectedCollector) -> dict[str, str]:
    current = stored(c.owner)
    return {
        name: value
        for name, value in (
            ("secret", current.client_secret_ref),
            ("registration", current.registration_token_ref),
        )
        if value is not None
    }


def test_after_a_client_rejection_only_the_owner_reconnect_registers(
    intake: Intake, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """AC 79 run half, I31, AC 78: zero registrations until the reconnect, exactly
    one made by it, then a working connection."""
    c = connected_collector(intake, served)
    served.token = refresh_with(token_error("invalid_client"))
    run_at(c, clock)
    for _ in range(3):
        run_at(c, clock, hours=2)
    assert served.registrations == []
    assert len(refresh_requests(served)) == 1
    served.token = Tokens(rotating=False)
    reconnect(c, served)
    assert len(served.registrations) == 1
    after = stored(c.owner)
    assert after.authorization_state == "connected"
    assert after.client_id is not None and after.client_id != STORED_CLIENT_ID
    run_at(c, clock, hours=2)
    assert len(refresh_requests(served)) == 2
    assert form(refresh_requests(served)[-1])["client_id"] == [after.client_id]
    assert last_error(c) is None
    assert len(served.registrations) == 1


# --- the next run after a reconnect problem ------------------------------------------


def test_the_next_run_after_a_reconnect_provider_error_uses_the_old_token(
    intake: Intake, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """AC 75 (next run), and a successful run clears ``registration_failed`` but
    leaves intake's own processing codes alone."""
    c = connected_collector(intake, served)
    params = restart(c.intake, c.owner, served)
    refused = complete(c.intake, c.owner, state=params["state"], error="access_denied")
    assert result(refused) == ("refused", "provider_error")
    assert last_error(c) == "authorization_required"
    run_at(c, clock)
    (refresh,) = refresh_requests(served)
    assert form(refresh)["refresh_token"] == [c.refresh_token]
    assert last_error(c) is None
    for code, cleared in (("registration_failed", True), ("processing_failed", False)):
        with open_unit_of_work(c.owner) as uow:
            health_write(uow, c.connection_id, error=code)
            uow.commit()
        run_at(c, clock, hours=2)
        assert last_error(c) == (None if cleared else code)


# --- secret files that went missing -------------------------------------------------


def _no_secret_text(
    root: Path, c: ConnectedCollector, caplog: pytest.LogCaptureFixture
) -> None:
    logged = "\n".join(r.getMessage() for r in caplog.records)
    recorded = repr(health(c.owner, c.connection_id))
    with open_unit_of_work(c.owner) as uow:
        jobs = repr(list(uow.connection.execute(select(job)).mappings()))
    for marker in markers(root, c):
        assert marker not in logged
        assert marker not in recorded
        assert marker not in jobs
    assert "internal_error" not in jobs


def test_a_refresh_token_file_removed_by_hand_is_terminal_with_no_request(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """r2-b2 (i): the operator's capability-off revoke path, then back on."""
    caplog.set_level(logging.DEBUG)
    c = connected_collector(intake, served)
    capability(intake, c.owner, False)
    file_of(root, c.token_ref).unlink()
    capability(intake, c.owner, True)
    run_at(c, clock)
    assert served.token_requests == []
    assert stored(c.owner).authorization_state == "authorization_required"
    assert collect_schedule(c.owner)["enabled"] is False  # type: ignore[index]
    assert len(lost_audits(c.owner)) == 1
    quietly_succeeded(c)
    _no_secret_text(root, c, caplog)


def test_a_revoke_between_claim_and_refresh_leaves_the_run_writing_nothing(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """r2-b2 (ii)."""
    caplog.set_level(logging.DEBUG)
    c = connected_collector(intake, served)
    with after_claim(lambda: revoke(c)):
        run_at(c, clock)
    assert served.token_requests == []
    after = stored(c.owner)
    assert after.authorization_state == "revoked" and after.token_ref is None
    recorded = health(c.owner, c.connection_id)
    assert recorded is not None and recorded["unresolved_failures"] == 1
    assert after.consecutive_failures == 0 and after.not_before is None
    assert lost_audits(c.owner) == []
    assert collect_schedule(c.owner)["enabled"] is False  # type: ignore[index]
    quietly_succeeded(c)
    _no_secret_text(root, c, caplog)


def test_a_reconnect_between_claim_and_refresh_leaves_the_new_connection_alone(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """r2-b2 (iii)."""
    caplog.set_level(logging.DEBUG)
    c = connected_collector(intake, served)
    served.token = Tokens()
    with after_claim(lambda: reconnect(c, served)):
        run_at(c, clock)
    assert refresh_requests(served) == []
    after = stored(c.owner)
    assert after.authorization_state == "connected"
    assert after.credential_generation == 2
    assert after.token_ref is not None and after.token_ref != c.token_ref
    assert file_of(root, after.token_ref).exists()
    assert collect_schedule(c.owner)["enabled"] is True  # type: ignore[index]
    assert last_error(c) is None
    assert after.consecutive_failures == 0
    assert lost_audits(c.owner) == []
    quietly_succeeded(c)
    _no_secret_text(root, c, caplog)


def test_a_missing_client_secret_file_clears_the_client_without_a_request(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """r2-b2 (iv)."""
    caplog.set_level(logging.DEBUG)
    c = connected_collector(intake, served, auth_method="client_secret_basic")
    refs = _client_refs(c)
    file_of(root, refs["secret"]).unlink()
    run_at(c, clock)
    assert served.token_requests == [] and served.registrations == []
    after = stored(c.owner)
    assert after.authorization_state == "authorization_required"
    assert after.client_id is None
    assert not file_of(root, refs["registration"]).exists()
    assert len(lost_audits(c.owner)) == 1
    quietly_succeeded(c)
    _no_secret_text(root, c, caplog)


# --- guarded run-ending writes (W1) ---------------------------------------------------


@pytest.mark.parametrize("owner_action", ["reconnect", "revoke"])
@pytest.mark.parametrize(
    "answer_kind", ["transient", "terminal"], ids=["503", "invalid_grant"]
)
def test_a_run_ending_write_applies_only_to_the_claimed_connection(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    owner_action: str,
    answer_kind: str,
) -> None:
    """W1: the owner reconnects or revokes while the token endpoint is answering;
    the run's failure write (transient or terminal) then writes nothing at all."""
    c = connected_collector(intake, served)
    failing = (
        answer(503) if answer_kind == "transient" else token_error("invalid_grant")
    )
    action = (
        (lambda: reconnect(c, served))
        if owner_action == "reconnect"
        else lambda: revoke(c)
    )
    served.token = before(action, refresh_with(failing))
    run_at(c, clock)
    assert len(refresh_requests(served)) == 1
    after = stored(c.owner)
    assert after.consecutive_failures == 0 and after.not_before is None
    assert lost_audits(c.owner) == []
    if owner_action == "reconnect":
        assert after.authorization_state == "connected"
        assert after.credential_generation == 2
        assert collect_schedule(c.owner)["enabled"] is True  # type: ignore[index]
        assert last_error(c) is None
    else:
        assert after.authorization_state == "revoked"
        assert collect_schedule(c.owner)["enabled"] is False  # type: ignore[index]
        recorded = health(c.owner, c.connection_id)
        assert recorded is not None and recorded["unresolved_failures"] == 1
    quietly_succeeded(c)


def test_a_client_rejection_never_clears_a_client_a_reconnect_just_registered(
    intake: Intake, root: Path, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """W1: ``invalid_client`` while the owner reconnects with a freshly registered
    client; the new client and its files stay."""
    c = connected_collector(intake, served)
    # The stored client no longer matches the redirect URI, so the reconnect
    # registers a fresh client with its own secret files.
    set_connection(c.owner, client_redirect_uri="https://auth.example.com/old")
    served.registration = issued(
        client_secret="client-secret-marker-new",
        registration_access_token="registration-token-marker-new",
    )
    served.token = before(
        lambda: reconnect(c, served), refresh_with(token_error("invalid_client"))
    )
    run_at(c, clock)
    after = stored(c.owner)
    assert len(served.registrations) == 1
    assert after.authorization_state == "connected"
    assert after.client_id is not None and after.client_id != STORED_CLIENT_ID
    new_files = [
        file_of(root, value)
        for value in (after.client_secret_ref, after.registration_token_ref)
        if value is not None
    ]
    assert len(new_files) == 2 and all(f.exists() for f in new_files)
    assert lost_audits(c.owner) == []
    assert last_error(c) is None
    quietly_succeeded(c)


# --- context, content and transaction shape -------------------------------------------


def test_an_inactive_workspace_ends_the_run_with_no_call_and_no_write(
    intake: Intake, served: FakeAuthorizationServer, mcp: FakeMcpServer
) -> None:
    """``context_for_scheduled_job`` refuses: zero calls, no writes."""
    c = connected_collector(intake, served)
    before_run = stored(c.owner)
    health_before = health(c.owner, c.connection_id)
    control = intake.cluster.backend.control_engine
    # The work transaction is open before the workspace goes unavailable, as the
    # worker's would be.
    with open_unit_of_work(c.owner) as uow:
        with control.begin() as connection:
            set_workspace_state(
                connection,
                intake.workspace,
                state=WorkspaceState.UNAVAILABLE,
                state_detail="marked unavailable by the test",
            )
        try:
            collect_job(
                HandlerUnitOfWork(uow, consumers=intake.consumers),
                CollectPayload(workspace_id=intake.workspace),
                NoToken(),  # type: ignore[arg-type]
            )
        finally:
            with control.begin() as connection:
                set_workspace_state(
                    connection,
                    intake.workspace,
                    state=WorkspaceState.ACTIVE,
                    state_detail="activate",
                )
        uow.commit()
    assert served.requests == [] and mcp.requests == []
    assert stored(c.owner) == before_run
    assert health(c.owner, c.connection_id) == health_before


def test_an_unexpected_failure_is_the_fixed_internal_error_with_no_chain(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I1: a forced exception inside the run."""
    caplog.set_level(logging.DEBUG)
    c = connected_collector(intake, served)
    secret_text = f"failure-marker {c.refresh_token}"

    def explode(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "SET run_claimed_at" in statement:
            raise InjectedFailure(secret_text)

    event.listen(Engine, "before_cursor_execute", explode)
    try:
        with pytest.raises(JobfeedFailure) as raised:
            invoke(c, NoToken())
        run_at(c, clock)
    finally:
        event.remove(Engine, "before_cursor_execute", explode)
    failure = raised.value
    assert str(failure) == "internal_error" and repr(failure) == "internal_error"
    assert failure.__cause__ is None and failure.__context__ is None
    (attempt,) = collect_jobs(c.owner)
    assert attempt.last_error == "internal_error" and attempt.attempts == 1
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "InjectedFailure" in logged
    assert "failure-marker" not in logged and c.refresh_token not in logged
    assert served.token_requests == []


@dataclass(frozen=True)
class WorkShape:
    """What the database shows of the run's work transaction mid-call."""

    busy: list[dict[str, Any]]
    locks: set[tuple[str, str, str]]
    lifecycle_free: bool

    def assert_inert(self) -> None:
        """Idle in transaction, nothing written, no lock on any ``leads`` relation
        beyond the readiness check's read of the module's version tables, and the
        lifecycle lock free."""
        assert len(self.busy) == 1, self.busy
        assert self.busy[0]["state"] == "idle in transaction"
        assert self.busy[0]["xid"] is None
        assert {name for schema, name, _ in self.locks if schema == "leads"} <= {
            "alembic_version_leads",
            "alembic_version_leads_pkc",
        }
        assert {mode for schema, _, mode in self.locks if schema == "leads"} <= {
            "AccessShareLock"
        }
        assert self.lifecycle_free is True


def work_shape(c: ConnectedCollector) -> WorkShape:
    with open_unit_of_work(c.owner) as uow:
        busy = [
            dict(row)
            for row in uow.connection.execute(
                text(
                    "SELECT pid, state, backend_xid::text AS xid "
                    "FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND state <> 'idle'"
                )
            ).mappings()
        ]
        locks = {
            (row.nspname, row.relname, row.mode)
            for row in uow.connection.execute(
                text(
                    "SELECT n.nspname, c.relname, l.mode FROM pg_locks l "
                    "JOIN pg_class c ON c.oid = l.relation "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE l.pid = ANY(:pids)"
                ),
                {"pids": [row["pid"] for row in busy]},
            )
        }
        lifecycle_free = uow.connection.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": WORKSPACE_LIFECYCLE_LOCK_KEY},
        ).scalar_one()
        uow.rollback()
    return WorkShape(busy, locks, bool(lifecycle_free))


def test_no_leads_lock_or_write_is_held_during_a_slow_refresh(
    intake: Intake, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """I14, transaction shape: during the refresh the work transaction is idle,
    has written nothing and locks nothing in ``leads`` beyond the readiness check's
    read of the module's version table; select_account and revoke complete."""
    c = connected_collector(intake, served, choices=CHOICES)
    hold = Hold()
    served.holds["token"] = hold
    worker = threading.Thread(target=lambda: visit(intake, clock))
    worker.start()
    try:
        assert hold.entered.wait(timeout=30), "the refresh never arrived"
        shape = work_shape(c)
        selected = run(intake, c.owner, SELECT, organization_id=CHOICES[0][0])
        assert selected.ok, selected
        revoke(c)
    finally:
        hold.release.set()
        worker.join(timeout=60)
    shape.assert_inert()
    quietly_succeeded(c)
    assert stored(c.owner).authorization_state == "revoked"


def test_a_run_never_registers_in_any_outcome(
    intake: Intake, served: FakeAuthorizationServer, clock: FakeClock
) -> None:
    """AC 78: success, rotation, transient, terminal and rejection runs together
    call the registration endpoint zero times."""
    c = connected_collector(intake, served)
    for responder in (
        Tokens(rotating=False),
        Tokens(rotating=True),
        answer(503),
        token_error("invalid_client"),
    ):
        served.token = responder
        run_at(c, clock, hours=2)
    assert served.registrations == []


# --- the MCP session and account selection ------------------------------------------

OTHER = "org-example-2"
THIRD = "org-example-3"
SET_UP = ["initialize", "notifications/initialized", "list_accounts"]
DEFAULT_SEARCHES = [
    "find_jobs:smart_search",
    "find_jobs:smart_search",
    "find_jobs:search",
]
"""An empty feed: one page per default search, none claiming more."""


def find_jobs_arguments(mcp: FakeMcpServer) -> list[dict[str, Any]]:
    return [
        arguments_of(r)
        for r in mcp.requests
        if arguments_of(r).get("action") in {"smart_search", "search", "get"}
    ]


def choices_of(c: ConnectedCollector) -> list[tuple[str, str | None]]:
    with open_unit_of_work(c.owner) as uow:
        return account_choices(uow.connection, c.connection_id)


def no_collection(mcp: FakeMcpServer) -> None:
    """Zero ``find_jobs`` calls, and nothing but the set-up calls at all: no
    ``tools/list`` and no write-capable tool."""
    assert {method for method, _, _ in mcp.counts} <= {
        "initialize",
        "notifications/initialized",
        "tools/call",
    }
    assert {tool for _, tool, _ in mcp.counts} <= {None, "list_accounts"}
    assert not set(mcp.names()) & {*WRITE_TOOLS, "tools/list"}


@pytest.mark.parametrize(
    ("stored_org", "listed", "selected"),
    [
        (None, [account()], ORGANIZATION),
        (None, [account(OTHER, role="CLIENT"), account()], ORGANIZATION),
        (ORGANIZATION, [account(OTHER), account()], ORGANIZATION),
        (None, [], None),
        (None, [account(), account(OTHER)], None),
        (ORGANIZATION, [account(OTHER)], None),
        (ORGANIZATION, [account(), account(OTHER), account()], None),
        (ORGANIZATION, [account(role="CLIENT"), account(OTHER)], None),
    ],
    ids=[
        "one-talent",
        "one-talent-among-others",
        "stored-listed-once",
        "none-listed",
        "several-talent",
        "stored-absent",
        "stored-duplicated",
        "stored-not-talent",
    ],
)
def test_the_account_is_selected_without_fallback_after_storing_the_choices(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
    stored_org: str | None,
    listed: list[dict[str, object]],
    selected: str | None,
) -> None:
    """AC 18, I16: a stored organization is used only when listed exactly once as
    TALENT; with none stored only a single TALENT account is taken. Every stop
    (including one with exactly one other TALENT account listed) makes zero
    ``find_jobs`` calls, and the fresh choices are stored first, in received
    order, whatever the decision."""
    c = connected_collector(
        intake, served, choices=[(THIRD, "Stale choice")], organization_id=stored_org
    )
    mcp.accounts = listed
    run_at(c, clock)
    expected_choices: dict[str, str | None] = {}
    for entry in listed:
        expected_choices.setdefault(str(entry["org_uid"]), str(entry["name"]))
    assert choices_of(c) == list(expected_choices.items())
    if selected is None:
        assert mcp.names() == SET_UP
        no_collection(mcp)
    else:
        assert mcp.names() == SET_UP + DEFAULT_SEARCHES
        assert {args["org_uid"] for args in find_jobs_arguments(mcp)} == {selected}
    after = stored(c.owner)
    assert after.authorization_state == "connected"
    if selected is None:
        assert last_error(c) == "account_selection_required"
        assert after.organization_id == stored_org
        # Not a failure to back off from: the owner's choice decides the next run.
        assert after.consecutive_failures == 0 and after.not_before is None
    else:
        assert last_error(c) is None
        assert after.organization_id == selected
    assert lost_audits(c.owner) == []
    quietly_succeeded(c)


def test_a_selection_after_a_stop_runs_from_the_end_of_the_claim_window(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
) -> None:
    """AC 73 to the claim floor, I28: stopped at ``account_selection_required``;
    the owner selects five minutes later; ticks before the claim plus 60 minutes
    enqueue nothing, the first one at it enqueues exactly one job, which claims,
    refreshes, lists accounts and passes selection with the chosen organization."""
    c = connected_collector(intake, served)
    mcp.accounts = [account(), account(OTHER, "Example Freelancer two")]
    run_at(c, clock)
    assert last_error(c) == "account_selection_required"
    claimed = stored(c.owner).run_claimed_at
    assert claimed == clock.now()
    clock.advance(5 * 60)
    chosen = run(intake, c.owner, SELECT, organization_id=OTHER)
    assert chosen.ok, chosen
    assert last_error(c) is None
    for minutes in (10, 30, 59):
        clock.wall_at = claimed + timedelta(minutes=minutes)
        tick(intake, c.owner, clock)
        assert len(collect_jobs(c.owner)) == 1
    clock.wall_at = claimed + timedelta(minutes=60)
    tick(intake, c.owner, clock)
    assert len(collect_jobs(c.owner)) == 2
    claimed_at = clock.now()
    visit(intake, clock)
    quietly_succeeded(c, runs=2)
    assert len(refresh_requests(served)) == 2
    assert mcp.calls("list_accounts") == 2
    after = stored(c.owner)
    assert after.run_claimed_at == claimed_at
    assert after.organization_id == OTHER
    assert last_error(c) is None
    # The first run stopped before collecting; the second collects as the choice.
    assert mcp.names() == SET_UP + SET_UP + DEFAULT_SEARCHES
    assert {args["org_uid"] for args in find_jobs_arguments(mcp)} == {OTHER}


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("call", ["initialize", "list_accounts"])
def test_an_mcp_refusal_after_a_refresh_is_terminal_and_keeps_the_client(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
    status: int,
    call: str,
) -> None:
    """AC 47 MCP half: ``authorization_required``, schedule off, one terminal audit
    row, the stored client and its files kept, and nothing upstream again."""
    c = connected_collector(intake, served, auth_method="client_secret_basic")
    files = [file_of(root, value) for value in _client_refs(c).values()]
    mcp.script[call] = answer(status, {"error": "refused"})
    run_at(c, clock)
    assert len(refresh_requests(served)) == 1
    assert mcp.calls(call) == 1
    no_collection(mcp)
    after = stored(c.owner)
    assert after.authorization_state == "authorization_required"
    assert collect_schedule(c.owner)["enabled"] is False  # type: ignore[index]
    assert last_error(c) == "authorization_required"
    assert after.client_id == STORED_CLIENT_ID and all(f.exists() for f in files)
    assert len(lost_audits(c.owner)) == 1
    quietly_succeeded(c)
    sent, called = len(served.requests), len(mcp.requests)
    for _ in range(3):
        run_at(c, clock, hours=2)
    assert len(served.requests) == sent and len(mcp.requests) == called
    assert served.registrations == []
    assert len(lost_audits(c.owner)) == 1


@pytest.mark.parametrize("call", ["initialize", "list_accounts"])
@pytest.mark.parametrize(
    ("failure", "code", "wait"),
    [
        ("5xx", "upstream_unavailable", TWO_HOURS),
        ("network", "upstream_unavailable", TWO_HOURS),
        ("timeout", "upstream_unavailable", TWO_HOURS),
        ("malformed", "upstream_unavailable", TWO_HOURS),
        ("429", "upstream_rate_limited", timedelta(seconds=30_000)),
    ],
)
def test_a_transient_mcp_failure_backs_off_with_no_retry(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
    call: str,
    failure: str,
    code: str,
    wait: timedelta,
) -> None:
    """AC 48 MCP half: a 5xx, a network error, a timeout or a malformed answer is
    ``upstream_unavailable``; an HTTP 429 is ``upstream_rate_limited`` with the
    larger of the doubling and Retry-After; one call each, no retry."""
    responders: dict[str, Responder] = {
        "5xx": answer(503),
        "network": raising(httpx.ConnectError("connection refused")),
        "timeout": slow(clock, 55.0),
        "malformed": answer(200, content=b'{"jsonrpc":"2.0","id":'),
        "429": answer(429, headers={"Retry-After": "30000"}),
    }
    c = connected_collector(intake, served)
    mcp.script[call] = responders[failure]
    run_at(c, clock)
    assert mcp.calls(call) == 1
    assert mcp.names() == SET_UP[: SET_UP.index(call) + 1]
    no_collection(mcp)
    after = stored(c.owner)
    assert last_error(c) == code
    assert after.consecutive_failures == 1
    assert after.not_before == clock.now() + wait
    assert after.authorization_state == "connected"
    assert lost_audits(c.owner) == []
    quietly_succeeded(c)


def test_a_malformed_account_list_is_upstream_unavailable(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
) -> None:
    """A ``list_accounts`` result that is not an account list stores no choices."""
    c = connected_collector(intake, served, choices=CHOICES)
    mcp.script["list_accounts"] = tool_result({"accounts": "not a list"})
    run_at(c, clock)
    assert last_error(c) == "upstream_unavailable"
    assert choices_of(c) == list(CHOICES)
    assert stored(c.owner).consecutive_failures == 1
    no_collection(mcp)


def test_an_mcp_host_that_turned_private_gets_no_byte(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    resolver: FakeResolver,
    mcp: FakeMcpServer,
) -> None:
    """AC 72 run half, the MCP request: the token endpoint still resolves public,
    the pinned MCP host now resolves private."""
    c = connected_collector(intake, served)
    resolver.make_private("mcp.example.com")
    run_at(c, clock)
    assert len(refresh_requests(served)) == 1
    assert mcp.requests == []
    assert last_error(c) == "endpoint_misconfigured"
    after = stored(c.owner)
    assert after.authorization_state == "connected"
    assert after.consecutive_failures == 1
    assert after.not_before == clock.now() + TWO_HOURS
    assert lost_audits(c.owner) == []
    quietly_succeeded(c)


def test_each_set_up_call_runs_between_a_forced_and_an_ordinary_checkpoint(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
) -> None:
    """AC 35 set-up calls, I13: the order of checkpoints and calls, and a
    ``list_accounts`` just inside its 50 s budget, after a throttled checkpoint,
    still ends inside the lease."""
    c = connected_collector(intake, served)
    token = LeaseModel(clock)
    seen: list[Any] = []

    def recorded(name: str, seconds: float) -> Responder:
        inner = mcp.default(name)

        def respond(request: httpx.Request) -> httpx.Response:
            token.events.append(name)
            seen.append((name, clock.now(), token.lease_until))
            clock.advance(seconds)
            return inner(request)

        return respond

    # The initialize call extends the lease when it returns; the notification
    # returns 15 s later, so its ordinary checkpoint is throttled.
    for name, seconds in (
        ("initialize", 30.0),
        ("notifications/initialized", 15.0),
        ("list_accounts", 49.5),
    ):
        mcp.script[name] = recorded(name, seconds)
    invoke(c, token)
    assert token.events[2:11] == [
        ("checkpoint", True),
        "initialize",
        ("checkpoint", False),
        ("checkpoint", True),
        "notifications/initialized",
        ("checkpoint", False),
        ("checkpoint", True),
        "list_accounts",
        ("checkpoint", False),
    ]
    _, started, lease_until = seen[-1]
    assert started + timedelta(seconds=49.5) < lease_until
    assert last_error(c) is None


def test_a_revoke_while_the_accounts_are_listed_leaves_the_run_writing_nothing(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
) -> None:
    """W1, the guarded stop: the run would need a selection, but the owner revoked
    while ``list_accounts`` was open; no choices and no code are written."""
    c = connected_collector(intake, served, choices=CHOICES)
    mcp.accounts = [account(), account(OTHER)]
    hold = Hold()
    mcp.holds["list_accounts"] = hold
    worker = threading.Thread(target=lambda: visit(intake, clock))
    worker.start()
    try:
        assert hold.entered.wait(timeout=30), "list_accounts never arrived"
        revoke(c)
    finally:
        hold.release.set()
        worker.join(timeout=60)
    after = stored(c.owner)
    assert after.authorization_state == "revoked"
    assert last_error(c) == "authorization_required"
    assert after.consecutive_failures == 0 and after.not_before is None
    assert choices_of(c) == list(CHOICES)
    assert lost_audits(c.owner) == []
    quietly_succeeded(c)


@pytest.mark.parametrize("call", ["initialize", "list_accounts"])
def test_no_leads_lock_or_write_is_held_during_a_slow_set_up_call(
    intake: Intake,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
    call: str,
) -> None:
    """I14, transaction shape for the session: during a slow ``initialize`` or
    ``list_accounts`` the work transaction is inert, and select_account and revoke
    complete during the call."""
    c = connected_collector(intake, served, choices=CHOICES)
    hold = Hold()
    mcp.holds[call] = hold
    worker = threading.Thread(target=lambda: visit(intake, clock))
    worker.start()
    try:
        assert hold.entered.wait(timeout=30), f"{call} never arrived"
        shape = work_shape(c)
        selected = run(intake, c.owner, SELECT, organization_id=CHOICES[0][0])
        assert selected.ok, selected
        revoke(c)
    finally:
        hold.release.set()
        worker.join(timeout=60)
    shape.assert_inert()
    quietly_succeeded(c)
    assert stored(c.owner).authorization_state == "revoked"


ORG_MARKER = "org-marker-5b2e8c41"


@pytest.mark.parametrize(
    "kind", ["401-body", "500-html", "truncated-json", "listed-marker"]
)
def test_no_token_or_organization_marker_reaches_a_job_log_audit_or_health(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    clock: FakeClock,
    mcp: FakeMcpServer,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    """I1 for the session: failing MCP bodies carrying a token marker and an
    organization-id marker, and an account list carrying the organization-id
    marker, leave neither in ``core.job.last_error``, logs, audit rows or health
    error fields."""
    caplog.set_level(logging.DEBUG)
    c = connected_collector(intake, served)
    tokens = Tokens(rotating=False)
    served.token = tokens

    def leaking(status: int, template: str) -> Responder:
        def respond(request: httpx.Request) -> httpx.Response:
            body = template.format(token=tokens.issued[-1], org=ORG_MARKER)
            return httpx.Response(
                status, content=body.encode(), headers={"X-Org": ORG_MARKER}
            )

        return respond

    mcp.script["list_accounts"] = {
        "401-body": leaking(401, '{{"error": "{token} {org}"}}'),
        "500-html": leaking(500, "<html>{token} {org}</html>"),
        "truncated-json": leaking(
            200, '{{"jsonrpc":"2.0","id":2,"result":{{"x":"{token} {org}'
        ),
        "listed-marker": accounts_result(account(ORG_MARKER), account(OTHER)),
    }[kind]
    run_at(c, clock)
    assert mcp.calls("list_accounts") == 1
    logged = "\n".join(
        [r.getMessage() for r in caplog.records]
        + [r.exc_text or "" for r in caplog.records]
    )
    with open_unit_of_work(c.owner) as uow:
        jobs = repr(list(uow.connection.execute(select(job)).mappings()))
        audits = repr(list(uow.connection.execute(select(audit_record)).mappings()))
    recorded = repr(health(c.owner, c.connection_id))
    for marker in [*markers(root, c), *tokens.issued, ORG_MARKER]:
        for surface in (logged, jobs, audits, recorded):
            assert marker not in surface
    if kind == "listed-marker":
        # The stored choices are a carried surface.
        assert choices_of(c)[0] == (ORG_MARKER, "Example Freelancer")
        assert last_error(c) == "account_selection_required"

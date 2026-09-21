"""AC 10 and § A11: bounded tool telemetry, over the real MCP tool surface.

Seams under test, in the order the file walks them:

1. **The sink records no query text and no argument value, under any outcome.** The
   assertion reads the *stored row*, column by column, and searches every text it holds
   for the marker strings the call was made with — a whitelist over what actually
   landed, not over the model's field list. A field added to the table without being
   added to the sink's contract reddens here, because the row itself is scanned.
2. **The row-count probe is lock-free below the cap and evicts only at or over it.**
   Both halves are asserted directly: a spy counts advisory-lock acquisitions, and
   ``EXPLAIN (ANALYZE)`` over the production probe statement pins how many rows it
   actually reads. The second is the one that catches the defect § A11 names — a bare
   ``SELECT count(*) ... LIMIT cap + 1`` returns the *right answer* while scanning the
   whole table, so no correctness assertion can see it and only a cost measurement can.
3. **A sink failure never alters the wrapped tool's outcome**, and never costs the
   operation its required success audit row.
4. **The audit-read gate restricts ``include_tool_telemetry`` to owner/operator**, at
   the operation level and again at the point of disclosure.

Driven through ``apps/mcp``'s ``call_tool`` rather than the core façade underneath it,
for the reason ``test_memory_mcp.py`` gives: the surface an agent actually reaches is
the one that has to be right. The fixture is that file's, narrowed — the same
``loaded_probe_modules`` registries with the core's own registrations put back beside
them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from conftest import ClusterSession
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from harness.registry import add_member
from rheo_app_mcp.tools import call_tool
from rheo_contracts import Role, WorkspaceContext
from rheo_core.audit import AUDIT_LIST
from rheo_core.audit.telemetry_tables import (
    MAX_ARGUMENT_NAMES,
    SAFETY_CLASSES,
    TELEMETRY_MODES,
    TELEMETRY_OUTCOMES,
)
from rheo_core.audit.tool_telemetry import (
    TOOL_MAX_ROWS_KEY,
    TOOL_RETENTION_DAYS_KEY,
    capacity_probe,
    insert_tool_telemetry,
    tool_telemetry,
)
from rheo_core.boundary import context_for_harness
from rheo_core.deletion import OWNED_DELETIONS
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.refs.resolver import register_resolver
from rheo_core.settings.schema import Floor, Scope, ValueType, spec_for
from rheo_core.storage import work_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_core.tokens.sets import register_core_tools
from rheo_core.work.schedules import RetentionSweepPayload, run_retention_sweep
from rheo_recallatron import MANIFEST
from rheo_recallatron.configuration import MEMORY_RECORD_TYPE
from rheo_recallatron.resolvers import resolve_memory
from sqlalchemy import delete, func, select
from sqlalchemy.dialects import postgresql

pytestmark = pytest.mark.postgres

_MEMORY_MODULE = MANIFEST.module_id

_SECRET_QUERY: Final = "zorblattic-quicksilver-marmalade"
"""A query string nothing else in the suite contains, so a search for it across every
stored text is a search for *this* call's query and not for a coincidence."""

_SECRET_TITLE: Final = "grommelsnitch-vantablack-oriole"
_SECRET_BODY: Final = "peregrine-flapdoodle-cantilever, and nothing else besides"


# --- the workspace ---------------------------------------------------------------


@dataclass(frozen=True)
class TelemetryWorkspace:
    """Recallatron loaded and enabled, with the core operations in one registry."""

    cluster: ClusterSession
    workspace: UUID
    owner_account_id: UUID
    surfaces: LoadedSurfaces
    database_name: str

    def context(
        self, *, account_id: UUID | None = None, role: Role = Role.OWNER
    ) -> WorkspaceContext:
        ctx = context_for_harness(
            self.workspace,
            self.owner_account_id if account_id is None else account_id,
            role,
        )
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    def call(
        self,
        ctx: WorkspaceContext,
        name: str,
        arguments: Mapping[str, object],
        *,
        consumers: ConsumerRegistry | None = None,
    ) -> OperationOutcome:
        return call_tool(
            ctx,
            name,
            arguments,
            consumers=consumers,
            tools=self.surfaces.tools,
            registry=self.surfaces.operations,
        )

    def unit_of_work(self) -> UnitOfWork:
        engine = self.cluster.backend.pools.engine_for(self.database_name)
        return UnitOfWork(engine, self.database_name)

    def rows(self) -> tuple[Any, ...]:
        """Every telemetry row, oldest first, as raw rows."""
        with self.unit_of_work() as uow:
            return tuple(
                uow.connection.execute(
                    select(tool_telemetry).order_by(
                        tool_telemetry.c.occurred_at, tool_telemetry.c.id
                    )
                )
            )

    def count(self) -> int:
        with self.unit_of_work() as uow:
            return int(
                uow.connection.execute(
                    select(func.count()).select_from(tool_telemetry)
                ).scalar_one()
            )

    def clear(self) -> None:
        with self.unit_of_work() as uow:
            uow.connection.execute(delete(tool_telemetry))
            uow.commit()

    def set_cap(self, key: str, value: int) -> None:
        with self.unit_of_work() as uow:
            upsert_workspace_setting(
                uow.connection,
                key=key,
                value=str(value),
                value_type=ValueType.INT,
                updated_by=None,
            )
            uow.commit()

    def audit_list(
        self, ctx: WorkspaceContext, *, include_tool_telemetry: bool
    ) -> OperationOutcome:
        return dispatch(
            ctx,
            AUDIT_LIST,
            {"limit": 500, "include_tool_telemetry": include_tool_telemetry},
            registry=self.surfaces.operations,
            consumers=None,
        )


@pytest.fixture
def telemetry(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[TelemetryWorkspace]:
    register_core_operations()
    register_core_tools()
    register_resolver(
        _MEMORY_MODULE, MEMORY_RECORD_TYPE, resolve_memory, origin=_MEMORY_MODULE
    )
    with loaded_probe_modules(
        monkeypatch, _MEMORY_MODULE, deletions=OWNED_DELETIONS
    ) as surfaces:
        register_core_operations(surfaces.operations)
        register_core_tools(surfaces.tools)
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, _MEMORY_MODULE)
        row = cluster.registry_row(workspace)
        loaded = TelemetryWorkspace(
            cluster=cluster,
            workspace=workspace,
            owner_account_id=owner_account_id,
            surfaces=surfaces,
            database_name=row.database_name,
        )
        loaded.clear()
        yield loaded


def _remember(loaded: TelemetryWorkspace, ctx: WorkspaceContext) -> str:
    outcome = loaded.call(
        ctx,
        "recallatron_remember",
        {
            "kind": "note",
            "title": _SECRET_TITLE,
            "body": _SECRET_BODY,
            "purposes": ["respond"],
        },
        consumers=ConsumerRegistry(),
    )
    assert outcome.ok, outcome
    assert outcome.result is not None
    return str(outcome.result.ref)  # type: ignore[attr-defined]


def _texts(row: Any) -> list[str]:
    """Every text this stored row holds, columns and array members alike."""
    collected: list[str] = []
    for value in row._mapping.values():
        if isinstance(value, str):
            collected.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            collected.extend(str(item) for item in value)
    return collected


# --- 1. the privacy whitelist ----------------------------------------------------


def test_a_read_records_its_query_length_and_never_its_query(
    telemetry: TelemetryWorkspace,
) -> None:
    """The load-bearing half of AC 10, asserted against the stored bytes.

    ``recallatron_recall`` is dispatched with a query nothing else in the suite
    contains; the row it produces is then read back and every text it holds is
    searched for that string. The length is recorded because § A11 allows it for a
    ``READ``; the text is not, because § A11 forbids it — and an integer cannot be
    read back as the query it measured.
    """
    ctx = telemetry.context()
    outcome = telemetry.call(
        ctx, "recallatron_recall", {"query": _SECRET_QUERY, "k": 5}
    )
    assert outcome.ok, outcome

    (row,) = telemetry.rows()
    assert row.tool_name == "recallatron_recall"
    assert row.safety_class == "read"
    assert row.mode == "current"
    assert row.outcome == "success"
    assert row.query_length == len(_SECRET_QUERY)
    assert row.duration_ms >= 0
    # § A11: argument names are a *write* field; a read's query length is what it has.
    assert list(row.argument_names) == []
    for text in _texts(row):
        assert _SECRET_QUERY not in text, f"the query reached {text!r}"


def test_a_write_records_sorted_declared_argument_names_and_no_value(
    telemetry: TelemetryWorkspace,
) -> None:
    """The other half: names, sorted, and not one of the values beside them."""
    ctx = telemetry.context()
    _remember(telemetry, ctx)

    (row,) = telemetry.rows()
    assert row.tool_name == "recallatron_remember"
    assert row.safety_class == "mutate"
    assert row.outcome == "success"
    assert row.result_count == 1
    assert row.query_length is None
    names = list(row.argument_names)
    assert names == ["body", "kind", "purposes", "title"]
    assert names == sorted(names)
    for text in _texts(row):
        assert _SECRET_TITLE not in text, f"the title reached {text!r}"
        assert _SECRET_BODY not in text, f"the body reached {text!r}"
        assert "note" not in text or text in names, f"a value reached {text!r}"


def test_an_input_refusal_is_recorded_and_carries_no_undeclared_argument_name(
    telemetry: TelemetryWorkspace,
) -> None:
    """§ A11 emits "including input refusals" — and an *undeclared* name is content.

    The rejected argument name is a string the caller chose. Recording it would put
    caller-supplied text into a table whose whole contract is that it holds none, so
    the sink records only the names the tool's own model declares. The row still
    exists, with ``outcome = refused``, because the refusal is the diagnostic.
    """
    ctx = telemetry.context()
    outcome = telemetry.call(
        ctx,
        "recallatron_forget",
        {"ref": f"{_MEMORY_MODULE}.memory:{uuid4()}", _SECRET_QUERY: True},
    )
    assert outcome.state == "input_invalid", outcome

    (row,) = telemetry.rows()
    assert row.outcome == "refused"
    assert row.mode == "none"
    assert row.result_count == 0
    assert list(row.argument_names) == ["ref"]
    for text in _texts(row):
        assert _SECRET_QUERY not in text, f"an undeclared name reached {text!r}"


def test_history_and_current_are_the_two_recorded_modes(
    telemetry: TelemetryWorkspace,
) -> None:
    """§ A6's lifecycle mode is what § A11's "allowlisted mode" carries.

    A tool that declares no lifecycle selector records ``none``, which is a value
    rather than a NULL so "this tool has no mode" and "nobody wrote one" stay
    distinguishable.
    """
    ctx = telemetry.context()
    telemetry.call(
        ctx,
        "recallatron_recall",
        {"query": "anything at all", "include_invalidated": True},
    )
    telemetry.call(ctx, "recallatron_recall", {"query": "anything at all"})
    _remember(telemetry, ctx)

    assert [row.mode for row in telemetry.rows()] == ["history", "current", "none"]
    assert set(TELEMETRY_MODES) == {"none", "current", "history"}


def test_forget_records_an_acknowledgment_not_an_erased_row_count(
    telemetry: TelemetryWorkspace,
) -> None:
    """§ A11: "forget count means one acknowledgment or zero, not erased row count".

    A destructive call is held at ``approval_required`` and has erased nothing, so its
    count is zero — and it stays zero however large the closure behind the reference
    is, because the count is read off the *returned result*, which § A8 keeps free of
    any closure size.
    """
    ctx = telemetry.context()
    reference = _remember(telemetry, ctx)
    telemetry.clear()

    outcome = telemetry.call(
        ctx, "recallatron_forget", {"ref": reference}, consumers=ConsumerRegistry()
    )
    assert outcome.state == "approval_required", outcome

    (row,) = telemetry.rows()
    assert row.tool_name == "recallatron_forget"
    assert row.safety_class == "destructive"
    assert row.outcome == "refused"
    assert row.result_count == 0
    assert list(row.argument_names) == ["ref"]


def test_an_unavailable_tool_name_is_never_written_to_the_table(
    telemetry: TelemetryWorkspace,
) -> None:
    """A ``not_found`` writes nothing, and that is a privacy decision, not an omission.

    On that branch the name is an unvalidated string the caller chose. A row for it
    would put caller-supplied text into ``tool_name`` — the one column this table
    trusts to hold a registered name — and would also answer, in an owner-readable
    table, exactly the question ``not_found`` exists to refuse.
    """
    ctx = telemetry.context()
    outcome = telemetry.call(ctx, _SECRET_QUERY, {"anything": 1})
    assert outcome.state == "not_found", outcome
    assert telemetry.rows() == ()


# --- 2. the bounded probe, and the lock it does not take -------------------------


def test_an_insert_under_the_cap_never_takes_the_eviction_lock(
    telemetry: TelemetryWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing performance property of § A11, tested directly.

    Pruning on every call would serialize every MCP call in a workspace behind one
    advisory lock. The spy counts acquisitions rather than inspecting ``pg_locks``,
    because the sink's transaction — and therefore its transaction-scoped lock — is
    over before any other connection could look.
    """
    import rheo_core.audit.tool_telemetry as sink

    taken: list[int] = []
    real = sink.advisory_lock

    def spy(conn: Any, key: int) -> None:
        taken.append(key)
        real(conn, key)

    monkeypatch.setattr(sink, "advisory_lock", spy)

    telemetry.set_cap(TOOL_MAX_ROWS_KEY, 3)
    ctx = telemetry.context()
    for _ in range(2):
        telemetry.call(ctx, "recallatron_recall", {"query": "under the cap"})
    assert telemetry.count() == 2
    assert taken == [], "an insert below the cap took the eviction lock"

    telemetry.call(ctx, "recallatron_recall", {"query": "reaching the cap"})
    assert taken == [sink.TELEMETRY_LOCK_KEY]
    assert telemetry.count() == 3

    telemetry.call(ctx, "recallatron_recall", {"query": "over the cap"})
    assert taken == [sink.TELEMETRY_LOCK_KEY, sink.TELEMETRY_LOCK_KEY]
    assert telemetry.count() == 3, "the cap did not hold"


def test_the_capacity_probe_reads_no_more_rows_than_its_bound(
    telemetry: TelemetryWorkspace,
) -> None:
    """The nested ``LIMIT`` is the whole point, and only a cost measure can see it.

    ``EXPLAIN (ANALYZE)`` over the exact statement :func:`capacity_probe` builds, with
    a table far larger than the cap. The bounded form reads ``cap + 1`` rows and stops;
    the bare ``SELECT count(*) ... LIMIT cap + 1`` reads every row in the table and
    returns the same correct number, which is why removing the inner bound reddens
    here and nowhere else.
    """
    cap = 4
    rows = 60
    now = datetime.now(UTC)
    with telemetry.unit_of_work() as uow:
        for index in range(rows):
            insert_tool_telemetry(
                uow.connection,
                occurred_at=now - timedelta(seconds=rows - index),
                tool_name="probe_filler",
                safety_class="read",
                mode="none",
                result_count=0,
                duration_ms=0,
                outcome="success",
                query_length=None,
                argument_names=(),
            )
        uow.commit()
    assert telemetry.count() == rows

    statement = capacity_probe(cap).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    with telemetry.unit_of_work() as uow:
        plan = uow.connection.exec_driver_sql(
            f"EXPLAIN (ANALYZE, FORMAT JSON) {statement}"
        ).scalar_one()
    if isinstance(plan, str):  # the driver may hand back JSON text
        plan = json.loads(plan)

    scanned = _max_actual_rows(plan[0]["Plan"])
    assert scanned <= cap + 1, (
        f"the probe read {scanned} rows for a cap of {cap}; the inner LIMIT is gone "
        "and this probe now full-scans the table on every tool call"
    )
    assert scanned < rows


def _max_actual_rows(node: Mapping[str, Any]) -> int:
    """The largest ``Actual Rows`` anywhere in an ``EXPLAIN ANALYZE`` plan tree."""
    rows = int(node.get("Actual Rows", 0))
    for child in node.get("Plans", ()):
        rows = max(rows, _max_actual_rows(child))
    return rows


def test_the_row_cap_holds_under_concurrent_inserts(
    telemetry: TelemetryWorkspace,
) -> None:
    """§ A14's concurrency clause on the row bound.

    Each sink runs in its own short transaction, so a concurrent batch can leave the
    table briefly over the cap by however many inserts were still in flight when the
    last eviction ran — every one of those rows is invisible to every other
    transaction's probe. That is bounded, not unbounded, and the next call brings it
    back to exactly the cap. Both halves are asserted, because asserting only the
    second would pass against a sink that had simply serialized the whole batch.
    """
    cap = 5
    workers = 4
    telemetry.set_cap(TOOL_MAX_ROWS_KEY, cap)
    ctx = telemetry.context()

    def one(index: int) -> str:
        outcome = telemetry.call(
            ctx, "recallatron_recall", {"query": f"concurrent probe {index}"}
        )
        return outcome.state

    with ThreadPoolExecutor(max_workers=workers) as pool:
        states = list(pool.map(one, range(workers * 3)))
    assert set(states) == {"succeeded"}, states
    assert telemetry.count() <= cap + workers

    telemetry.call(ctx, "recallatron_recall", {"query": "the settling call"})
    assert telemetry.count() == cap


def test_eviction_removes_the_oldest_rows_and_keeps_the_newest(
    telemetry: TelemetryWorkspace,
) -> None:
    """Evicting "down to the cap" removes the oldest — which oldest, asserted."""
    telemetry.set_cap(TOOL_MAX_ROWS_KEY, 2)
    ctx = telemetry.context()
    for marker in ("first", "second", "third"):
        telemetry.call(ctx, "recallatron_recall", {"query": f"the {marker} call"})
        # A distinct ``occurred_at`` per row: the clock is real, but a same-microsecond
        # tie would make "oldest" depend on the id tiebreak rather than on the time.
    lengths = [row.query_length for row in telemetry.rows()]
    assert lengths == [len("the second call"), len("the third call")]


# --- 3. failure isolation --------------------------------------------------------


def test_a_sink_failure_changes_neither_the_outcome_nor_the_audit_row(
    telemetry: TelemetryWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§ A11's isolation clause, against the untampered call as its control.

    The same ``remember`` is run twice — once normally, once with the sink's insert
    raising on every call — and the two outcomes are compared field for field. The
    audit row is counted across both, because the point is not only that the tool
    still answered but that its *required* success audit was never at risk: telemetry
    is a separate short transaction, and a failure inside it cannot reach the
    operation's own.
    """
    import rheo_core.audit.tool_telemetry as sink

    ctx = telemetry.context()
    healthy = telemetry.call(
        ctx,
        "recallatron_remember",
        {
            "kind": "note",
            "title": "the control",
            "body": "written with a working telemetry sink",
            "purposes": ["respond"],
        },
        consumers=ConsumerRegistry(),
    )
    assert healthy.ok, healthy
    assert telemetry.count() == 1
    audits_after_control = _audit_rows(telemetry)

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the telemetry sink is down")

    monkeypatch.setattr(sink, "insert_tool_telemetry", explode)

    broken = telemetry.call(
        ctx,
        "recallatron_remember",
        {
            "kind": "note",
            "title": "the subject",
            "body": "written with a telemetry sink that raises on every insert",
            "purposes": ["respond"],
        },
        consumers=ConsumerRegistry(),
    )
    assert broken.state == healthy.state
    assert broken.error == healthy.error is None
    assert broken.ok
    assert broken.result is not None

    # No second telemetry row, and the operation's own audit row is there regardless.
    assert telemetry.count() == 1
    assert _audit_rows(telemetry) == audits_after_control + 1


def test_a_sink_failure_on_the_capacity_probe_is_swallowed_too(
    telemetry: TelemetryWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only the insert: § A11 swallows "connection, validation, capacity and
    storage exceptions" at this boundary. A capacity failure is the one most likely to
    appear first in production, because it is the only step that runs conditionally.
    """
    import rheo_core.audit.tool_telemetry as sink

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the capacity probe is broken")

    monkeypatch.setattr(sink, "capacity_probe", explode)
    outcome = telemetry.call(
        telemetry.context(), "recallatron_recall", {"query": "still answered"}
    )
    assert outcome.ok, outcome
    # The insert was rolled back with the failed transaction: no half-written row.
    assert telemetry.count() == 0


def _audit_rows(loaded: TelemetryWorkspace) -> int:
    with loaded.unit_of_work() as uow:
        return int(
            uow.connection.execute(
                select(func.count()).select_from(work_tables.audit_record)
            ).scalar_one()
        )


# --- 4. owner/operator-only disclosure -------------------------------------------


def test_the_telemetry_collection_is_owner_and_operator_only(
    telemetry: TelemetryWorkspace,
) -> None:
    """Both gates, because either alone would be a single point of failure.

    The operation's own declaration restricts ``core.audit.list`` to owner and
    operator, so a member asking for the flag is refused before the handler runs; and
    the handler applies the same pair again at the point of disclosure, so a later run
    widening the operation's roles cannot widen this collection with it.
    """
    ctx = telemetry.context()
    telemetry.call(ctx, "recallatron_recall", {"query": "something to report"})

    owner = telemetry.audit_list(ctx, include_tool_telemetry=True)
    assert owner.ok, owner
    assert owner.result is not None
    listed = owner.result.tool_telemetry  # type: ignore[attr-defined]
    assert listed is not None and len(listed) == 1
    assert listed[0].tool_name == "recallatron_recall"
    assert listed[0].query_length == len("something to report")

    member_id = add_member(
        telemetry.cluster.backend,
        telemetry.workspace,
        Role.MEMBER,
        display_name="a member who may not read telemetry",
    )
    member = telemetry.context(account_id=member_id, role=Role.MEMBER)
    refused = telemetry.audit_list(member, include_tool_telemetry=True)
    assert refused.state == "role_not_permitted", refused

    # And the handler's own gate, reached directly, so the assertion above is not the
    # only thing standing between a member and the collection.
    from rheo_core.audit.operations import AuditListInput, audit_list_handler

    with telemetry.unit_of_work() as uow:
        published = audit_list_handler(
            member, uow, AuditListInput(limit=50, include_tool_telemetry=True)
        )
    assert published.tool_telemetry is None


def test_the_collection_is_absent_unless_it_is_asked_for(
    telemetry: TelemetryWorkspace,
) -> None:
    """``None`` and ``[]`` are different answers; the default is ``None``."""
    ctx = telemetry.context()
    telemetry.call(ctx, "recallatron_recall", {"query": "something to report"})

    silent = telemetry.audit_list(ctx, include_tool_telemetry=False)
    assert silent.ok, silent
    assert silent.result is not None
    assert silent.result.tool_telemetry is None  # type: ignore[attr-defined]
    assert silent.result.records  # the existing collection still arrives


# --- 5. the age bound ------------------------------------------------------------


def test_the_two_keyspecs_carry_the_ratified_bounds() -> None:
    """§ A11's numbers, as declarations rather than as behaviour.

    The default *is* the hard maximum on both keys, which is what makes them
    tightenable and not loosenable; the ``min`` floor is what applies that to a
    workspace override, and ``minimum`` is the floor below which no layer may go.
    """
    days = spec_for(TOOL_RETENTION_DAYS_KEY)
    assert (days.default, days.minimum, days.maximum) == (7, 1, 7)
    assert (days.scope, days.floor, days.type) == (
        Scope.WORKSPACE,
        Floor.MIN,
        ValueType.INT,
    )
    rows = spec_for(TOOL_MAX_ROWS_KEY)
    assert (rows.default, rows.minimum, rows.maximum) == (10000, 1, 10000)
    assert (rows.scope, rows.floor, rows.type) == (
        Scope.WORKSPACE,
        Floor.MIN,
        ValueType.INT,
    )


def test_an_expired_row_is_excluded_from_an_audit_read_before_any_sweep_runs(
    telemetry: TelemetryWorkspace,
) -> None:
    """§ A11's immediacy clause: the read applies the age bound, not only the purge.

    Without it the daily sweep's schedule would decide what an audit read discloses,
    which is a retention policy nobody wrote. The row is seeded eight days back
    against the default seven-day window and no sweep is run at all.
    """
    now = datetime.now(UTC)
    seeded = ((timedelta(days=8), "stale_tool"), (timedelta(hours=1), "fresh_tool"))
    with telemetry.unit_of_work() as uow:
        for age, tool in seeded:
            insert_tool_telemetry(
                uow.connection,
                occurred_at=now - age,
                tool_name=tool,
                safety_class="read",
                mode="none",
                result_count=0,
                duration_ms=1,
                outcome="success",
                query_length=None,
                argument_names=(),
            )
        uow.commit()

    outcome = telemetry.audit_list(telemetry.context(), include_tool_telemetry=True)
    assert outcome.ok, outcome
    assert outcome.result is not None
    listed = outcome.result.tool_telemetry  # type: ignore[attr-defined]
    assert [row.tool_name for row in listed] == ["fresh_tool"]
    # Still physically present: the read excluded it, the sweep has not removed it.
    assert telemetry.count() == 2


def test_the_daily_core_sweep_is_what_physically_purges_expired_telemetry(
    telemetry: TelemetryWorkspace,
) -> None:
    """The other half of the age bound, and the only age deletion in the system.

    The insert path attempts none, on purpose (§ A11), so if this handler did not
    purge, nothing would and the table's age bound would be a read filter over rows
    that never leave.
    """
    now = datetime.now(UTC)
    seeded = ((timedelta(days=9), "stale_tool"), (timedelta(hours=2), "fresh_tool"))
    with telemetry.unit_of_work() as uow:
        for age, tool in seeded:
            insert_tool_telemetry(
                uow.connection,
                occurred_at=now - age,
                tool_name=tool,
                safety_class="read",
                mode="none",
                result_count=0,
                duration_ms=1,
                outcome="success",
                query_length=None,
                argument_names=(),
            )
        uow.commit()
    assert telemetry.count() == 2

    with telemetry.unit_of_work() as uow:
        run_retention_sweep(
            _handler_uow(uow),
            RetentionSweepPayload(workspace_id=telemetry.workspace),
            _NeverCancelled(),
        )
        uow.commit()

    assert [row.tool_name for row in telemetry.rows()] == ["fresh_tool"]


class _NeverCancelled:
    """A cancellation token that never fires; the sweep only calls ``checkpoint``."""

    def checkpoint(self) -> None:
        return None


def _handler_uow(uow: UnitOfWork) -> Any:
    from rheo_core.storage.backend import HandlerUnitOfWork

    return HandlerUnitOfWork(uow, operation_id=None, consumers=None)


# --- 6. the table itself ---------------------------------------------------------


def test_the_table_holds_no_column_a_payload_could_arrive_in(
    telemetry: TelemetryWorkspace,
) -> None:
    """A census of the DDL, so a later column cannot be added without an argument.

    Every column is named here. Adding one reddens this test, which is the point: the
    privacy claim in AC 10 is a claim about the *table*, and a table that can hold a
    body is one call site away from holding one.
    """
    assert set(tool_telemetry.c.keys()) == {
        "id",
        "occurred_at",
        "tool_name",
        "safety_class",
        "mode",
        "result_count",
        "duration_ms",
        "outcome",
        "query_length",
        "argument_names",
    }
    assert set(TELEMETRY_OUTCOMES) == {"success", "refused", "error"}
    assert "read" in SAFETY_CLASSES and "destructive" in SAFETY_CLASSES


def test_the_argument_name_array_is_bounded_in_the_database(
    telemetry: TelemetryWorkspace,
) -> None:
    """The cardinality bound is the database's, not only the writer's.

    A bound only the writer keeps is a bound one call site away from being gone, so
    the check constraint is exercised directly with a list the writer would have
    truncated.
    """
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with telemetry.unit_of_work() as uow:
            uow.connection.execute(
                tool_telemetry.insert().values(
                    id=uuid4(),
                    occurred_at=datetime.now(UTC),
                    tool_name="too_many_names",
                    safety_class="mutate",
                    mode="none",
                    result_count=0,
                    duration_ms=0,
                    outcome="success",
                    query_length=None,
                    argument_names=[f"f{n}" for n in range(MAX_ARGUMENT_NAMES + 1)],
                )
            )

"""The bounded tool-telemetry sink: one content-safe callable, its repository, and
the two bounds that keep ``core.tool_telemetry`` from growing without end (§ A11).

**What the façade hands over, and what it cannot.** :func:`record_tool_call` takes a
tool name, a safety class, a mode, three integers and a list of argument *names* —
scalars, every one of them, and not the arguments, the result, the context payload or
the exception. There is no parameter through which a query, a body, a title, a
credential or an exception message could arrive, which is what makes "no user payload
is logged" a property of the signature rather than a discipline at the call site.
``rheo_core.operations.tool_facade`` derives those scalars and calls this; ``apps/mcp``
calls the façade and imports no storage package, which is § A11's first sentence.

**Its own short transaction, after the tool operation.** The wrapped call has already
committed (or rolled back) by the time this runs, so the row is written on a fresh
unit of work of its own and committed alone. ``_audit_alone`` in
``operations/dispatch.py`` is the shape this copies: one write, its own transaction,
and a log rather than a raise when it cannot happen.

**Everything is swallowed at this boundary, and nothing else is.** A connection
refusal, a validation error, a capacity failure, a storage error — all of them are
caught here and logged without a payload, and the tool's own outcome is returned
unchanged by the caller. Failure to write a **success audit** row stays fatal and
transactional in ``dispatch``; this is a distinct optional diagnostic sink and must
never be read as a replacement for it.

**The row cap is enforced by a bounded probe, not by pruning every call.** Taking the
per-workspace telemetry advisory lock on every insert would serialize every MCP call
in a workspace behind a maintenance step almost none of them needs. So the insert path
runs :func:`capacity_probe` — ``SELECT count(*) FROM (SELECT 1 FROM core.tool_telemetry
LIMIT cap + 1) t`` — and only when that answers at or over the cap does it take the
lock and evict. The nesting is the whole point and is easy to lose: ``LIMIT`` on the
*outer* query would cap the single-row count result and nothing else, so the bare
``SELECT count(*) ... LIMIT cap + 1`` returns the same correct answer while
full-scanning the table on every call — a cost defect that passes its own correctness
test. ``tests/postgres/test_tool_telemetry.py`` pins the difference with
``EXPLAIN ANALYZE`` rather than by reading the SQL.

**Age is not pruned here at all.** The daily ``core.retention_sweep``
(``rheo_core.work.schedules.run_retention_sweep``) owns purging age-expired telemetry,
during idle periods and off the request path. What this module does provide is
:func:`list_tool_telemetry`'s ``not_before`` cut, so an audit read excludes an expired
row immediately whether or not the sweep has run since it expired.

**The workspace scope is the connection.** § A11 writes the probe's predicate as
``WHERE <workspace scope>``; in this system a workspace *is* a database
(``docs/architecture/storage-and-workspaces.md``), so every statement here is already
scoped to one workspace by the connection it runs on and there is no workspace column
to filter — the same reason ``core.deletion_record`` and ``core.audit_record`` have
none. The advisory lock is per-workspace by the same construction, which is what
``deletion/lifecycle.py`` spells out for the lifecycle lock.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, get_args
from uuid import UUID

from rheo_contracts import SafetyClass, WorkspaceContext
from sqlalchemy import Connection, Row, Select, delete, func, insert, literal, select

from rheo_core.audit.telemetry_tables import (
    MAX_ARGUMENT_NAMES,
    TELEMETRY_ERROR,
    TELEMETRY_MODE_CURRENT,
    TELEMETRY_MODE_HISTORY,
    TELEMETRY_MODE_NONE,
    TELEMETRY_MODES,
    TELEMETRY_OUTCOMES,
    TELEMETRY_REFUSED,
    TELEMETRY_SUCCESS,
    tool_telemetry,
)
from rheo_core.refs import uuid7
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from rheo_core.storage.postgres import advisory_lock
from rheo_core.storage.routing import open_unit_of_work

logger = logging.getLogger("rheo_core.audit")

TOOL_RETENTION_DAYS_KEY: Final = "telemetry.tool_retention_days"
TOOL_MAX_ROWS_KEY: Final = "telemetry.tool_max_rows"
"""§ A11's two KeySpecs, named here and **declared** in
``rheo_core.settings.schema.PRODUCTION_KEYS``.

They are core keys, and ``settings/defaults.py`` asserts at import time that the
``core``-origin registry and ``config/defaults.toml`` hold exactly the same key set. A
core key declared in a module the process may not have imported yet would make that
identity check depend on import order, so the declarations stay at the one core
declaration site and this module names them. Recallatron's own two retention keys are
declared by its manifest for the mirror-image reason: a *module* key must not be in
the core's TOML."""

TELEMETRY_LOCK_KEY: Final = 0x5248454F54454C45
"""``RHEOTELE`` in ASCII: the per-workspace telemetry eviction lock.

Distinct from ``RHEOLIFE`` (the workspace lifecycle lock), ``RHEOMIGR`` (the migration
chain) and ``RHEOACCT`` (the first-account lock), so no two locks in this tree can
collide over one key. Deliberately **not** the lifecycle lock: eviction is
housekeeping on a diagnostic table and must never queue behind — or in front of — a
deletion, and a telemetry insert that blocked on the lifecycle lock would make this
sink able to slow the very operations it only observes.

Transaction-scoped, like every other advisory lock here, so it is released when the
sink's own short transaction ends however it ends."""

TelemetryMode = Literal["none", "current", "history"]
TelemetryOutcome = Literal["success", "refused", "error"]

if set(get_args(TelemetryMode)) != set(TELEMETRY_MODES):  # pragma: no cover
    raise RuntimeError(
        "TelemetryMode and telemetry_tables.TELEMETRY_MODES disagree; they are the "
        "same set spelled twice and must be changed together"
    )

if set(get_args(TelemetryOutcome)) != set(TELEMETRY_OUTCOMES):  # pragma: no cover
    raise RuntimeError(
        "TelemetryOutcome and telemetry_tables.TELEMETRY_OUTCOMES disagree; they are "
        "the same set spelled twice and must be changed together"
    )


@dataclass(frozen=True, slots=True)
class ToolTelemetryRow:
    """One ``core.tool_telemetry`` row as its supported read publishes it.

    Every column, because every column is metadata: there is nothing in this table a
    reader is not entitled to see once the owner/operator gate has passed.
    """

    id: UUID
    occurred_at: datetime
    tool_name: str
    safety_class: str
    mode: str
    result_count: int
    duration_ms: int
    outcome: str
    query_length: int | None
    argument_names: tuple[str, ...]


_COLUMNS: Final = (
    tool_telemetry.c.id,
    tool_telemetry.c.occurred_at,
    tool_telemetry.c.tool_name,
    tool_telemetry.c.safety_class,
    tool_telemetry.c.mode,
    tool_telemetry.c.result_count,
    tool_telemetry.c.duration_ms,
    tool_telemetry.c.outcome,
    tool_telemetry.c.query_length,
    tool_telemetry.c.argument_names,
)


def _row(row: Row[Any]) -> ToolTelemetryRow:
    """One selected row as a :class:`ToolTelemetryRow`, field for field.

    Written out rather than splatted, for the reason ``audit/records.py``'s own
    ``_row`` gives: a splat pairs the wrong column with the wrong field the moment
    :data:`_COLUMNS` and the dataclass drift apart.
    """
    return ToolTelemetryRow(
        id=row.id,
        occurred_at=row.occurred_at,
        tool_name=str(row.tool_name),
        safety_class=str(row.safety_class),
        mode=str(row.mode),
        result_count=int(row.result_count),
        duration_ms=int(row.duration_ms),
        outcome=str(row.outcome),
        query_length=None if row.query_length is None else int(row.query_length),
        argument_names=tuple(str(name) for name in row.argument_names),
    )


def capacity_probe(max_rows: int) -> Select[tuple[int]]:
    """The bounded existence probe, as the one statement production runs.

    ``SELECT count(*) FROM (SELECT 1 FROM core.tool_telemetry LIMIT max_rows + 1) t``.
    The inner ``LIMIT`` is what bounds the scan; the count then saturates at
    ``max_rows + 1`` and the caller only needs to know whether it reached ``max_rows``.

    **Never the bare ``SELECT count(*) ... LIMIT max_rows + 1``.** That form's ``LIMIT``
    applies to the count's own single result row, so it bounds nothing: the aggregate
    still reads every row in the table, on every MCP call, and still returns the right
    number — a cost defect a correctness test cannot see. Returned as a statement
    rather than run inline so a test can put the real thing under ``EXPLAIN ANALYZE``
    and assert what it actually scanned.
    """
    bounded = select(literal(1)).select_from(tool_telemetry).limit(max_rows + 1)
    return select(func.count()).select_from(bounded.subquery())


def insert_tool_telemetry(
    conn: Connection,
    *,
    occurred_at: datetime,
    tool_name: str,
    safety_class: str,
    mode: TelemetryMode,
    result_count: int,
    duration_ms: int,
    outcome: TelemetryOutcome,
    query_length: int | None,
    argument_names: Sequence[str],
) -> UUID:
    """Write one telemetry row and return its id. Does not commit.

    Shaped after ``audit/records.py:insert_audit_record`` and for the same reasons:
    the caller's connection, the caller's transaction, an explicit ``occurred_at``
    rather than a clock read, and no argument with a default — a default on ``outcome``
    or ``result_count`` would let a call site omit a column that carries the meaning of
    the row and satisfy NOT NULL by accident.

    ``argument_names`` is truncated to :data:`~rheo_core.audit.telemetry_tables.
    MAX_ARGUMENT_NAMES` here as well as constrained in the database, so an over-long
    list is a bounded row rather than a failed insert that loses the whole record.
    """
    telemetry_id = uuid7()
    conn.execute(
        insert(tool_telemetry).values(
            id=telemetry_id,
            occurred_at=occurred_at,
            tool_name=tool_name,
            safety_class=safety_class,
            mode=mode,
            result_count=result_count,
            duration_ms=duration_ms,
            outcome=outcome,
            query_length=query_length,
            argument_names=list(argument_names[:MAX_ARGUMENT_NAMES]),
        )
    )
    return telemetry_id


def evict_oldest_beyond(conn: Connection, *, max_rows: int) -> int:
    """Delete every row outside the newest ``max_rows``, returning how many went.

    One statement, and no ``count(*)``: the rows to keep are the newest ``max_rows`` by
    ``(occurred_at, id)`` — the index § A3 names — and everything else goes, so the
    table lands on exactly the cap without the caller ever having to learn how far over
    it was. The ids are NOT NULL, so ``NOT IN`` carries none of the three-valued
    surprise it would over a nullable column.

    Does not commit, and does not take the lock: :func:`record_tool_call` takes
    :data:`TELEMETRY_LOCK_KEY` immediately before calling this, which is the only path
    that should.
    """
    keep = (
        select(tool_telemetry.c.id)
        .order_by(tool_telemetry.c.occurred_at.desc(), tool_telemetry.c.id.desc())
        .limit(max_rows)
    )
    return int(
        conn.execute(
            delete(tool_telemetry).where(tool_telemetry.c.id.not_in(keep))
        ).rowcount
    )


def purge_expired_tool_telemetry(conn: Connection, *, not_before: datetime) -> int:
    """Delete telemetry older than ``not_before``, returning how many went.

    The daily ``core.retention_sweep``'s half of § A11's two bounds, and the only
    age-based deletion in this module: the insert path attempts none. Does not commit;
    the sweep's own unit of work owns that.
    """
    return int(
        conn.execute(
            delete(tool_telemetry).where(tool_telemetry.c.occurred_at < not_before)
        ).rowcount
    )


def list_tool_telemetry(
    conn: Connection, *, limit: int, not_before: datetime
) -> tuple[ToolTelemetryRow, ...]:
    """The most recent unexpired telemetry rows, newest first, capped at ``limit``.

    ``not_before`` is § A11's "audit reads exclude expired telemetry immediately
    regardless of when the daily sweep last ran": the age bound is applied to the read
    as well as to the purge, so a row that expired an hour after the last sweep is
    already invisible here. Without it the sweep's schedule would decide what an audit
    read discloses, which is a retention policy nobody wrote.

    ``id`` breaks an ``occurred_at`` tie for the reason ``list_audit_records`` gives:
    the ids are uuid7, so descending id is the ordering ``occurred_at`` intends, and an
    unordered tie makes the page depend on the plan Postgres happened to choose.
    """
    rows = conn.execute(
        select(*_COLUMNS)
        .where(tool_telemetry.c.occurred_at >= not_before)
        .order_by(tool_telemetry.c.occurred_at.desc(), tool_telemetry.c.id.desc())
        .limit(limit)
    )
    return tuple(_row(row) for row in rows)


def telemetry_bounds(ctx: WorkspaceContext, conn: Connection) -> tuple[int, int]:
    """``(tool_retention_days, tool_max_rows)`` for this workspace, on this connection.

    Resolved with the workspace's own override rows, which is what makes the two
    ``min`` floors real — a floored key read from the deployment layer alone is a floor
    nothing applies, exactly as ``approvals/gate.py:window_seconds`` says of its own.
    Read through the **transaction-bound** source rather than
    ``PostgresOverrideSource``, so the values are the ones this transaction sees and no
    second pooled connection is checked out per tool call.
    """
    settings = resolve(
        workspace_id=ctx.workspace_id,
        source=TransactionBoundOverrideSource(conn, workspace_id=ctx.workspace_id),
    )
    return (
        settings.get_int(TOOL_RETENTION_DAYS_KEY),
        settings.get_int(TOOL_MAX_ROWS_KEY),
    )


def telemetry_horizon(
    ctx: WorkspaceContext, conn: Connection, now: datetime
) -> datetime:
    """The oldest ``occurred_at`` an audit read may still disclose."""
    retention_days, _ = telemetry_bounds(ctx, conn)
    return now - timedelta(days=retention_days)


def record_tool_call(
    ctx: WorkspaceContext,
    *,
    tool_name: str,
    safety_class: SafetyClass,
    mode: TelemetryMode,
    result_count: int,
    duration_ms: int,
    outcome: TelemetryOutcome,
    query_length: int | None = None,
    argument_names: Sequence[str] = (),
) -> None:
    """Record one tool call, or record nothing and say so in the log.

    **This function never raises and never changes what its caller returns.** Every
    exception from the connection, the settings read, the insert, the probe and the
    eviction is caught here and logged with identifiers only — no tool arguments, no
    result, no exception text from a driver that might have interpolated a value into
    its message. § A11: "sink connection, validation, capacity and storage exceptions
    are swallowed only at this telemetry boundary, with no user payload logged and no
    unbounded retry queue. The original tool outcome is returned unchanged."

    There is no retry. A lost telemetry row is a lost diagnostic; a queue of them would
    be unbounded state on the request path for a table that is itself capped at ten
    thousand rows.

    ``safety_class`` is the enum rather than its string so a caller cannot hand over a
    spelling the DDL's check constraint does not hold — the same reason
    :class:`~rheo_core.audit.sink.AuditSink` takes it that way — and ``mode`` and
    ``outcome`` are ``Literal``s checked against the DDL's own tuples at import time.
    """
    try:
        _record(
            ctx,
            tool_name=tool_name,
            safety_class=safety_class,
            mode=mode,
            result_count=result_count,
            duration_ms=duration_ms,
            outcome=outcome,
            query_length=query_length,
            argument_names=argument_names,
        )
    except Exception:
        # Identifiers and the call's own shape only: nothing here can carry an
        # argument, a result or a driver message. ``exc_info`` is deliberately absent
        # for the same reason — a traceback can quote the statement's parameters.
        logger.warning(
            "tool_telemetry_not_written",
            extra={
                "tool": tool_name,
                "outcome": outcome,
                "workspace_id": str(ctx.workspace_id),
                "request_id": str(ctx.request_id),
            },
        )


def _record(
    ctx: WorkspaceContext,
    *,
    tool_name: str,
    safety_class: SafetyClass,
    mode: TelemetryMode,
    result_count: int,
    duration_ms: int,
    outcome: TelemetryOutcome,
    query_length: int | None,
    argument_names: Sequence[str],
) -> None:
    """The write, its bounded capacity probe, and the eviction the probe may trigger.

    The order is load-bearing. The bounds are read first because the probe needs the
    cap; the row lands next, so the probe counts a table that already includes it; and
    the lock is taken **only** on the branch that evicts, which is what keeps an
    ordinary call lock-free.
    """
    with open_unit_of_work(ctx) as uow:
        conn = uow.connection
        _, max_rows = telemetry_bounds(ctx, conn)
        insert_tool_telemetry(
            conn,
            occurred_at=datetime.now(UTC),
            tool_name=tool_name,
            safety_class=safety_class.value,
            mode=mode,
            result_count=result_count,
            duration_ms=duration_ms,
            outcome=outcome,
            query_length=query_length,
            argument_names=argument_names,
        )
        if conn.execute(capacity_probe(max_rows)).scalar_one() >= max_rows:
            advisory_lock(conn, TELEMETRY_LOCK_KEY)
            evict_oldest_beyond(conn, max_rows=max_rows)
        uow.commit()


__all__ = [
    "MAX_ARGUMENT_NAMES",
    "TELEMETRY_ERROR",
    "TELEMETRY_LOCK_KEY",
    "TELEMETRY_MODE_CURRENT",
    "TELEMETRY_MODE_HISTORY",
    "TELEMETRY_MODE_NONE",
    "TELEMETRY_REFUSED",
    "TELEMETRY_SUCCESS",
    "TOOL_MAX_ROWS_KEY",
    "TOOL_RETENTION_DAYS_KEY",
    "TelemetryMode",
    "TelemetryOutcome",
    "ToolTelemetryRow",
    "capacity_probe",
    "evict_oldest_beyond",
    "insert_tool_telemetry",
    "list_tool_telemetry",
    "purge_expired_tool_telemetry",
    "record_tool_call",
    "telemetry_bounds",
    "telemetry_horizon",
    "tool_telemetry",
]

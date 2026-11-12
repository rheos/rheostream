"""``finance jobs health``: the schedule's health codes and counts, then each reminder
key a sync set aside, with its code and when it last synced.

The ``tick``, ``serve`` and ``install-agent`` commands of the standalone runtime were
not imported into rheoStream: core's worker runs the tick as a scheduled job.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from rheo_finance.clock import render_local
from rheo_finance.render import Report, emit

if TYPE_CHECKING:
    from rheo_finance.context import FinanceContext
    from rheo_finance.jobs.service import HealthOut


def _context() -> FinanceContext:
    from rheo_finance.composition import build_context
    from rheo_finance.contracts import Role

    return build_context(Role.SERVICE)


def health_lines(result: HealthOut, zone: ZoneInfo) -> list[str]:
    """The health line, then one line per set-aside reminder key (ADR-0027)."""
    lines = [
        f"finance jobs health: {result.code} failed={result.failed} "
        f"pending={result.pending} skipped_off={result.skipped_off}"
    ]
    for each in result.reminder_errors:
        synced = (
            "never synced"
            if each.last_success_at is None
            else f"last synced {render_local(each.last_success_at, zone)}"
        )
        lines.append(f"reminder {each.key}: {each.code}, {synced}")
    if result.more:
        lines.append(f"and {result.more} more")
    return lines


def _health(_args: argparse.Namespace) -> int:
    from rheo_finance.jobs.service import HealthOut, health

    zone = ZoneInfo("UTC")
    try:
        ctx = _context()
        zone = ctx.settings.timezone
        result = health(ctx)
    except Exception:
        result = HealthOut(code="database_error")
    emit(Report(health_lines(result, zone)))
    return 0 if result.code == "healthy" else 1


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("jobs", help="scheduled job catch-up")
    sub = parser.add_subparsers(dest="jobs_command", required=True)
    sub.add_parser(
        "health", help="bounded schedule health codes and counts"
    ).set_defaults(handler=_health)

"""``finance jobs health``: the schedule's health codes and counts.

The ``tick``, ``serve`` and ``install-agent`` commands of the standalone runtime were
not imported into rheoStream: core's worker runs the tick as a scheduled job.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from rheo_finance.render import Report, emit

if TYPE_CHECKING:
    from rheo_finance.context import FinanceContext


def _context() -> FinanceContext:
    from rheo_finance.composition import build_context
    from rheo_finance.contracts import Role

    return build_context(Role.SERVICE)


def _health(_args: argparse.Namespace) -> int:
    from rheo_finance.jobs.service import HealthOut, health

    try:
        result = health(_context())
    except Exception:
        result = HealthOut(code="database_error")
    emit(
        Report(
            [
                f"finance jobs health: {result.code} failed={result.failed} "
                f"pending={result.pending} skipped_off={result.skipped_off}"
            ]
        )
    )
    return 0 if result.code == "healthy" else 1


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("jobs", help="scheduled job catch-up")
    sub = parser.add_subparsers(dest="jobs_command", required=True)
    sub.add_parser(
        "health", help="bounded schedule health codes and counts"
    ).set_defaults(handler=_health)

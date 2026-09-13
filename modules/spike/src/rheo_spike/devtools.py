"""``rheo-spike``: the spike's own dev-only command, deleted with the module.

Two subcommands, both refused under ``RHEO_PROFILE=production``:

- ``rheo-spike install --workspace <id>`` — schema ``spike`` plus one enabled
  ``core.module_state`` row, in one transaction on that workspace's database. This is
  ``tests/harness/registry.py``'s ``enable_harness_module`` with the schema creation
  in front of it. It is **not** a module-lifecycle subsystem: no
  ``core.module_schema_version`` row, no advisory lock, no state machine. Release one
  has no install path for a real module at all, which is run 0v's finding **F15**;
  this command is scaffolding that makes FR 2's ``module_disabled`` branch reachable
  in both directions, not a proposal for what phase 2 should build.
- ``rheo-spike session --account <id> --host <h>`` — prints the ``rheo_session``
  cookie value for a fresh session on that host, so the driver can drive a browser
  flow without an OAuth provider.

This module builds no ``WorkspaceContext``: it asks ``context_for_operator`` for one.
``tests/test_boundary.py``'s AST scan does not reach ``modules/`` (finding **F18**),
so that rule is unenforced here and is obeyed anyway.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.boundary.context import Refusal
from rheo_core.sessions import create_session, mint_host_secret
from rheo_core.sessions.cookies import SESSION_COOKIE
from rheo_core.settings import SettingsError, current_profile
from rheo_core.storage import core_tables
from rheo_core.storage.backend import StorageRefusal
from rheo_core.storage.routing import open_unit_of_work
from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rheo_spike.manifest import MANIFEST
from rheo_spike.operations import MODULE_ID
from rheo_spike.records import create_schema

PRODUCTION = "production"
MODULE_ENABLED = "enabled"

Handler = Callable[[argparse.Namespace], int]


def _refuse_in_production() -> int | None:
    """``1`` with a message on stderr under the production profile, else ``None``."""
    profile = current_profile()
    if profile == PRODUCTION:
        print(
            f"profile_refused: rheo-spike is a development command and the resolved "
            f"profile is {profile!r}",
            file=sys.stderr,
        )
        return 1
    return None


def install_into(connection: Connection) -> None:
    """Schema ``spike``, its two tables, and the enabled ``core.module_state`` row,
    on an open workspace connection. The caller owns the transaction.

    Idempotent: ``create_schema`` is ``IF NOT EXISTS``/``checkfirst`` and the state
    row is ``on_conflict_do_nothing`` on ``module_id``, so a second install is a
    no-op rather than an error. ``tests/conftest.py``'s ``install_spike`` fixture
    calls this same function, so the fixture installs the module exactly the way the
    command does instead of keeping a second copy of these lines in step with it.
    """
    now = datetime.now(UTC)
    create_schema(connection)
    connection.execute(
        pg_insert(core_tables.module_state)
        .values(
            module_id=MODULE_ID,
            package_version=MANIFEST.package_version,
            state=MODULE_ENABLED,
            installed_at=now,
            enabled_at=now,
            disabled_at=None,
            state_detail=None,
        )
        .on_conflict_do_nothing(index_elements=[core_tables.module_state.c.module_id])
    )


def install(args: argparse.Namespace) -> int:
    """``rheo-spike install --workspace <id>``: :func:`install_into` in one
    transaction on that workspace's own database."""
    refused = _refuse_in_production()
    if refused is not None:
        return refused
    ctx = context_for_operator(args.workspace)
    if isinstance(ctx, Refusal):
        print(f"{ctx.state}: {ctx.detail}", file=sys.stderr)
        return 1
    assert isinstance(ctx, WorkspaceContext)
    uow = open_unit_of_work(ctx)
    with uow:
        install_into(uow.connection)
        uow.commit()
    print(f"{MODULE_ID} installed in workspace {args.workspace}", file=sys.stderr)
    return 0


def session(args: argparse.Namespace) -> int:
    """Print the ``rheo_session`` cookie value for a fresh session on ``--host``.

    The session is created with no active workspace, exactly as
    ``POST /auth/callback`` creates one: selecting a workspace is the spike surface's
    own form, not this command's (run 0v's carried CF1).
    """
    refused = _refuse_in_production()
    if refused is not None:
        return refused
    row = create_session(args.account)
    secret = mint_host_secret(row.id, args.host)
    print(f"{SESSION_COOKIE}={secret.hex()}", file=sys.stderr)
    print(secret.hex())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rheo-spike",
        description="the vertical-slice spike's development commands (run 0v; "
        "deleted at 0c0's branch cut)",
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    commands.required = True

    install_parser = commands.add_parser(
        "install", help="create schema spike and enable the module in a workspace"
    )
    install_parser.add_argument(
        "--workspace", required=True, type=UUID, metavar="WORKSPACE_ID"
    )
    install_parser.set_defaults(handler=install)

    session_parser = commands.add_parser(
        "session", help="print a rheo_session cookie value for an account on a host"
    )
    session_parser.add_argument(
        "--account", required=True, type=UUID, metavar="ACCOUNT_ID"
    )
    session_parser.add_argument("--host", required=True, metavar="HOST")
    session_parser.set_defaults(handler=session)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` (``sys.argv[1:]`` when ``None``) and run one subcommand.

    Returns an exit code and never calls ``sys.exit`` itself, matching ``rheo``'s own
    entry contract, so a test can call it and see a value rather than catch
    ``SystemExit``.

    **``SecretRefusal`` is deliberately not caught**, unlike ``rheo``'s own
    ``run_command``, which catches it alongside these two.
    ``tests/test_boundary_scans.py::test_no_module_distribution_imports_the_secret
    _store`` refuses any ``rheo_core.secrets`` import under ``modules/``, and the
    exception class lives in that package — so catching it would mean importing it.
    The gate is right and this command is wrong to want it: a module has no business
    reaching the secret store even to name its failures. The cost is that an
    unreachable cluster prints a traceback here instead of one refusal line, which is
    the correct trade for a development-only command in a module that is deleted at
    0c0's branch cut.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 2
    handler: Handler = args.handler
    try:
        return handler(args)
    except (StorageRefusal, SettingsError) as refusal:
        print(f"{refusal.state}: {refusal.detail}", file=sys.stderr)
        return 1

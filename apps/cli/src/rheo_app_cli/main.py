"""The ``rheo`` operator command: an argparse dispatcher over ``commands/``.

Entry contract (shared with the Makefile's ``demo`` target and ``deploy/README.md``):
``main(argv: Sequence[str] | None = None) -> int`` parses the list it is given,
falling back to ``sys.argv[1:]`` only when ``argv is None``; **returns** ``0`` after
printing usage when no subcommand is given; and never calls ``sys.exit`` itself —
the console-script wrapper (``apps/cli/pyproject.toml``) does. argparse's own exits
(``--help``, a usage error) are caught and returned as the exit code, so a caller
inside a test sees a return value, never ``SystemExit``.

The no-subcommand path bootstraps nothing: no settings resolution, no data root,
no database. Settings and the backend are bootstrapped inside a subcommand's own
handler (``context.py``). Commands at this run's merge SHA: ``migrate``,
``account create``, ``workspace create|repair|list|status``, ``doctor``,
``routing hosts``, ``member add``, ``token issue|revoke``, ``openapi`` (run 0v's,
and the only one that bootstraps nothing — see ``commands/openapi.py``).
"""

import argparse
import sys
from collections.abc import Callable, Sequence

from rheo_core.secrets import SecretRefusal
from rheo_core.settings import SettingsError
from rheo_core.storage.backend import StorageRefusal
from rheo_core.storage.data_root import DataRootRefusal

from rheo_app_cli.commands import (
    account,
    doctor,
    member,
    migrate,
    openapi,
    routing,
    token,
    workspace,
)

Handler = Callable[[argparse.Namespace], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rheo",
        description="rheoStream operator command: the control plane, workspaces, "
        "migrations and diagnostics.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for module in (
        migrate,
        account,
        workspace,
        doctor,
        routing,
        member,
        token,
        openapi,
    ):
        module.add_parser(subparsers)
    return parser


def _exit_code(code: object) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    # argparse passes a message for ``error()``; it has already printed it.
    return 2


def run_command(handler: Handler, args: argparse.Namespace) -> int:
    """Run one handler; a refusal prints its state to stderr and exits ``1``."""
    try:
        return handler(args)
    except (StorageRefusal, SettingsError, DataRootRefusal, SecretRefusal) as refusal:
        print(f"{refusal.state}: {refusal.detail}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"invalid argument: {exc}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(arguments)
    except SystemExit as exc:
        return _exit_code(exc.code)
    if args.command is None:
        parser.print_help()
        return 0
    handler: Handler = args.handler
    return run_command(handler, args)

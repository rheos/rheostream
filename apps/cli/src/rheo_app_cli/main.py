"""The ``rheo`` operator command: an argparse dispatcher over ``commands/``.

Entry contract (shared with the Makefile's ``demo`` target and ``deploy/README.md``):
``main(argv: Sequence[str] | None = None) -> int`` parses the list it is given,
falling back to ``sys.argv[1:]`` only when ``argv is None``; **returns** ``0`` after
printing usage when no subcommand is given; and never calls ``sys.exit`` itself —
the console-script wrapper (``apps/cli/pyproject.toml``) does. argparse's own exits
(``--help``, a usage error) are caught and returned as the exit code, so a caller
inside a test sees a return value, never ``SystemExit``.

The no-subcommand path bootstraps nothing: no settings resolution, no data root,
no database. Every other subcommand first registers the allowed modules' own settings
keys (``register_module_settings()``, #108, in :func:`run_command`), then settings
and the backend are bootstrapped inside its own handler (``context.py``); ``openapi``
and ``web`` skip that registration (``NO_BOOTSTRAP_COMMANDS``), and ``doctor`` runs it
as its own first check (``SELF_REGISTERING_COMMANDS``). Commands at this
run's merge SHA: ``migrate``, ``account create``,
``workspace create|repair|list|status``, ``doctor``, ``routing hosts``,
``member add``, ``token issue|revoke``, ``openapi`` (run 0v's) and ``web compose``
(run 1a3's). Those last two bootstrap nothing — see ``commands/openapi.py`` and
``commands/web.py``.

A module the early registration cannot load (its entry point fails to import, or the
loader refuses its manifest, say over ``core_contract_versions``) is a refusal like
any other: ``module_invalid: <detail>`` or ``module_load_failed: <detail>`` on stderr
and exit ``1``, for every bootstrapping command including ``migrate`` and
``token revoke``. ``doctor`` is the exception: it reports the same state and detail
as its ``module settings`` check's ``FAIL`` line, runs its remaining checks, and exits
``1`` as it does for any failed check.

**One side effect worth knowing about.** Because the allowed modules' keys are in the
settings registry before the handler runs, ``workspace create`` now writes an allowed
module's ``explicit_per_workspace`` default rows alongside core's (provisioning's
``write_default_settings`` step writes every such key the registry holds). The rows
are inert until that module is installed and enabled in the workspace, and the write
is insert-if-absent, so a repair or a later module load writes nothing twice.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import Final

from rheo_core.modules import ManifestInvalid, register_module_settings
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
    web,
    workspace,
)

Handler = Callable[[argparse.Namespace], int]

NO_BOOTSTRAP_COMMANDS: Final = frozenset({"openapi", "web"})
"""Subcommands that bootstrap nothing, so :func:`run_command` skips the early module
settings registration for them: ``make codegen`` runs both, with no deployment."""

SELF_REGISTERING_COMMANDS: Final = frozenset({"doctor"})
"""Subcommands that run the early module settings registration themselves, so
:func:`run_command` skips it for them. ``doctor`` reports it as its first check: a
refused registration is then one ``FAIL`` line among the rest of the report, not a
refusal that stops the diagnosis before it starts."""


class ModuleSettingsRefusal(Exception):
    """An allowed module the early settings registration could not load.

    Carries ``state`` and ``detail`` like the other refusals :func:`run_command`
    prints, so a broken module reads as one line and exit ``1`` rather than a
    traceback.
    """

    def __init__(self, state: str, detail: str) -> None:
        super().__init__(detail)
        self.state = state
        self.detail = detail


def register_allowed_module_settings() -> None:
    """:func:`register_module_settings`, with a module-load failure as a refusal.

    A settings failure (a malformed ``modules.installed``, a conflicting key) is
    already a ``SettingsError`` and passes through untouched. ``ManifestInvalid`` is
    the loader refusing a manifest. Anything else comes from importing a module's
    entry point, which runs that module's own import code and so can raise any
    exception at all (``ImportError``, pydantic's ``ValidationError`` on a malformed
    manifest, ``AttributeError`` on a wrong attribute name); all of those are the same
    operator fact, that this allowed module cannot load.
    """
    try:
        register_module_settings()
    except SettingsError:
        raise
    except ManifestInvalid as invalid:
        raise ModuleSettingsRefusal("module_invalid", str(invalid)) from invalid
    except Exception as failure:
        raise ModuleSettingsRefusal(
            "module_load_failed", f"{type(failure).__name__}: {failure}"
        ) from failure


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
        web,
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
    """Run one handler; a refusal prints its state to stderr and exits ``1``.

    Registers the allowed modules' settings keys first (#108), inside the ``try`` so a
    refusal it raises prints and exits like any other, except for the commands in
    :data:`NO_BOOTSTRAP_COMMANDS` and :data:`SELF_REGISTERING_COMMANDS`. A module that
    cannot load is a :class:`ModuleSettingsRefusal`, printed the same way.
    """
    command = getattr(args, "command", None)
    try:
        if (
            command not in NO_BOOTSTRAP_COMMANDS
            and command not in SELF_REGISTERING_COMMANDS
        ):
            register_allowed_module_settings()
        return handler(args)
    except (
        StorageRefusal,
        SettingsError,
        DataRootRefusal,
        SecretRefusal,
        ModuleSettingsRefusal,
    ) as refusal:
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

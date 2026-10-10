"""Host-operator handoff of an existing website key to a private file."""

import argparse
import sys
from pathlib import Path
from uuid import UUID

from rheo_core.boundary import Refusal, context_for_operator
from rheo_core.connectors.credentials import resolve_key
from rheo_core.connectors.tables import locator
from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
from rheo_core.modules import load_modules, loaded_manifests
from rheo_core.secrets.write import create_file_secret
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.repositories import list_module_states
from rheo_core.storage.routing import open_unit_of_work
from sqlalchemy import select

from rheo_app_cli.context import bootstrap


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("connector", help="connector credential handoff")
    commands = parser.add_subparsers(dest="connector_command", required=True)
    command = commands.add_parser(
        "export-key", help="write the current website key to a new private file"
    )
    command.add_argument("connection_id", type=UUID)
    command.add_argument("--output", required=True, type=Path)
    command.set_defaults(handler=export_key)


def export_key(args: argparse.Namespace) -> int:
    bootstrap()
    load_modules()
    with get_backend().control_engine.connect() as conn:
        row = (
            conn.execute(
                select(locator).where(locator.c.connection_id == args.connection_id)
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        print("connection unavailable", file=sys.stderr)
        return 1
    ctx = context_for_operator(row["workspace_id"])
    manifest = loaded_manifests().get(row["module_id"])
    if (
        isinstance(ctx, Refusal)
        or manifest is None
        or row["module_id"] not in ctx.enabled_modules
    ):
        print("connection unavailable", file=sys.stderr)
        return 1
    binding = next(
        (b for b in manifest.connector_bindings if b.transport == row["transport"]),
        None,
    )
    if binding is None or binding.read_connection is None:
        print("connection unavailable", file=sys.stderr)
        return 1
    with open_unit_of_work(ctx) as uow:
        lock_workspace_lifecycle(uow.connection)
        if not any(
            module.module_id == row["module_id"] and module.state == "enabled"
            for module in list_module_states(uow.connection)
        ):
            print("connection unavailable", file=sys.stderr)
            return 1
        state = binding.read_connection(ctx.workspace_id, uow, args.connection_id)
        if state is None or not state.active or state.current_ref is None:
            print("connection unavailable", file=sys.stderr)
            return 1
        value = resolve_key(ctx.workspace_id, args.connection_id, state.current_ref)
        output = args.output.absolute()
        create_file_secret(output.parent, output.name, value.expose())
    print(
        f"Exported generation {state.generation} to a new private file.",
        file=sys.stderr,
    )
    return 0

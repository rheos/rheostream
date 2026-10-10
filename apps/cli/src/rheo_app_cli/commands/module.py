"""Operator checks and explicit upgrades of existing module installations."""

import argparse
import json
from uuid import UUID

from rheo_core.boundary import Refusal, context_for_operator
from rheo_core.modules import load_modules, loaded_manifests
from rheo_core.modules.readiness import expected_heads, readiness_problem
from rheo_core.operations import dispatch
from rheo_core.storage.control_plane import list_workspaces
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.repositories import list_module_states

from rheo_app_cli.context import bootstrap


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "module", help="module schema readiness and explicit upgrades"
    )
    commands = parser.add_subparsers(dest="module_command", required=True)
    check = commands.add_parser(
        "check", help="read-only check; nonzero if an installed module is incompatible"
    )
    target = check.add_mutually_exclusive_group(required=True)
    target.add_argument("--workspace", type=UUID)
    target.add_argument("--all", action="store_true", help="all active workspaces")
    check.set_defaults(handler=check_modules)
    upgrade = commands.add_parser(
        "upgrade", help="upgrade one installed module; never installs or enables"
    )
    upgrade.add_argument("--workspace", required=True, type=UUID)
    upgrade.add_argument("--module", required=True)
    upgrade.add_argument("--target-version", required=True)
    upgrade.add_argument("--target-schema", required=True)
    upgrade.set_defaults(handler=upgrade_module)


def check_modules(args: argparse.Namespace) -> int:
    boot = bootstrap()
    load_modules()
    with boot.backend.control_engine.connect() as conn:
        workspaces = list_workspaces(conn, state=WorkspaceState.ACTIVE)
    if args.workspace:
        workspaces = tuple(w for w in workspaces if w.id == args.workspace)
        if not workspaces:
            print(json.dumps({"state": "workspace_unavailable"}))
            return 1
    failed = False
    for workspace in workspaces:
        with boot.backend.pools.engine_for(workspace.database_name).connect() as conn:
            for state in list_module_states(conn):
                if state.state == "removed":
                    continue
                problem = readiness_problem(conn, state)
                failed |= problem is not None
                print(
                    json.dumps(
                        {
                            "workspace_id": str(workspace.id),
                            "module_id": state.module_id,
                            "state": problem or "ready",
                            "package_version": state.package_version,
                            "target_version": loaded_manifests()[
                                state.module_id
                            ].package_version
                            if problem != "module_not_loaded"
                            else None,
                            "target_schema": sorted(expected_heads(state.module_id))
                            if problem != "module_not_loaded"
                            else [],
                        }
                    )
                )
    return 1 if failed else 0


def upgrade_module(args: argparse.Namespace) -> int:
    bootstrap()
    load_modules()
    ctx = context_for_operator(args.workspace)
    if isinstance(ctx, Refusal):
        print(json.dumps({"state": ctx.state}))
        return 1
    result = dispatch(
        ctx,
        "core.module.upgrade",
        {
            "module_id": args.module,
            "target_version": args.target_version,
            "target_schema": args.target_schema,
        },
    )
    print(
        json.dumps(
            {
                "state": result.state,
                "result": result.result.model_dump(mode="json")
                if result.result
                else None,
            }
        )
    )
    return 0 if result.ok else 1

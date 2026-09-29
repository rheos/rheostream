"""``rheo evidence enroll|rotate|revoke``: a local bridge's enrollment, operator side.

Each dispatches under ``context_for_operator`` through the real, role-checked path,
exactly like ``rheo token issue``.

Output contract. ``enroll`` and ``rotate`` without ``--json`` keep ``token issue``'s:
the raw token value alone on stdout, the ids and expiry on stderr. With ``--json``
they print exactly one JSON line, ``{"enrollment_id", "token", "expires_at"}``, and
nothing else on stdout, so the line can be piped straight into ``rheo-bridge
set-token``. ``revoke`` mints nothing and stays silent on stdout.

``revoke`` takes only an enrollment id, as ``token revoke`` takes only a token id. An
enrollment lives in its workspace's own database, so the command looks for the id in
each active workspace and dispatches in the one that holds it, for the account that
owns it.
"""

import argparse
import json
import sys
from uuid import UUID

from rheo_core.boundary import Refusal, context_for_operator
from rheo_core.evidence.enrollment import (
    ENROLLMENT_CREATE,
    ENROLLMENT_REVOKE,
    ENROLLMENT_ROTATE,
)
from rheo_core.operations import OperationOutcome, dispatch
from rheo_core.storage.control_plane import list_workspaces
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.evidence_enrollment_tables import evidence_enrollment
from rheo_core.storage.postgres import PostgresBackend
from sqlalchemy import select, text

from rheo_app_cli.context import bootstrap


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "evidence", help="local bridge enrollments (automatic memory)"
    )
    commands = parser.add_subparsers(dest="evidence_command", metavar="<command>")
    commands.required = True

    enroll = commands.add_parser(
        "enroll", help="enroll a machine and directory; prints the bridge token once"
    )
    enroll.add_argument("--account", required=True, type=UUID, metavar="ACCOUNT_ID")
    enroll.add_argument("--workspace", required=True, type=UUID, metavar="WORKSPACE_ID")
    enroll.add_argument("--machine", required=True, metavar="FINGERPRINT")
    enroll.add_argument("--project", required=True, metavar="FINGERPRINT")
    enroll.add_argument("--json", action="store_true")
    enroll.set_defaults(handler=enroll_bridge)

    rotate = commands.add_parser(
        "rotate", help="replace an enrollment's bridge token; prints the new one once"
    )
    rotate.add_argument("enrollment_id", type=UUID)
    rotate.add_argument("--account", required=True, type=UUID, metavar="ACCOUNT_ID")
    rotate.add_argument("--workspace", required=True, type=UUID, metavar="WORKSPACE_ID")
    rotate.add_argument("--json", action="store_true")
    rotate.set_defaults(handler=rotate_bridge)

    revoke = commands.add_parser("revoke", help="revoke an enrollment and its token")
    revoke.add_argument("enrollment_id", type=UUID)
    revoke.set_defaults(handler=revoke_bridge)


def _dispatch(
    workspace_id: UUID, operation: str, payload: dict[str, str]
) -> OperationOutcome | None:
    """Dispatch under the operator context; print any refusal and return ``None``."""
    ctx = context_for_operator(workspace_id)
    if isinstance(ctx, Refusal):
        print(ctx, file=sys.stderr)
        return None
    outcome = dispatch(ctx, operation, payload)
    if not outcome.ok or outcome.result is None:
        error = outcome.error
        detail = None if error is None else error.error_text
        print(
            f"{outcome.state}: {detail}" if detail else outcome.state, file=sys.stderr
        )
        return None
    return outcome


def _print_token(outcome: OperationOutcome, *, as_json: bool) -> int:
    result = outcome.result
    enrollment_id = result.enrollment_id  # type: ignore[union-attr]
    token_id = result.token_id  # type: ignore[union-attr]
    value = result.value  # type: ignore[union-attr]
    expires_at = result.expires_at.isoformat()  # type: ignore[union-attr]
    print(
        f"enrollment {enrollment_id}, token {token_id}, expires {expires_at}",
        file=sys.stderr,
    )
    if as_json:
        line = {
            "enrollment_id": str(enrollment_id),
            "token": value,
            "expires_at": expires_at,
        }
        print(json.dumps(line))
    else:
        # The ``token issue`` contract: exactly the raw value and a newline.
        print(value)
    return 0


def enroll_bridge(args: argparse.Namespace) -> int:
    bootstrap()
    outcome = _dispatch(
        args.workspace,
        ENROLLMENT_CREATE,
        {
            "account_id": str(args.account),
            "machine_fingerprint": args.machine,
            "project_fingerprint": args.project,
        },
    )
    if outcome is None:
        return 1
    return _print_token(outcome, as_json=args.json)


def rotate_bridge(args: argparse.Namespace) -> int:
    bootstrap()
    outcome = _dispatch(
        args.workspace,
        ENROLLMENT_ROTATE,
        {"enrollment_id": str(args.enrollment_id), "account_id": str(args.account)},
    )
    if outcome is None:
        return 1
    return _print_token(outcome, as_json=args.json)


def _locate(backend: PostgresBackend, enrollment_id: UUID) -> tuple[UUID, UUID] | None:
    """``(workspace_id, account_id)`` of the active workspace holding the enrollment."""
    with backend.control_engine.connect() as connection:
        rows = list_workspaces(connection, state=WorkspaceState.ACTIVE)
    for row in rows:
        with backend.pools.acquire(row.database_name) as engine:
            with engine.connect() as connection:
                present = connection.execute(
                    text("SELECT to_regclass('core.evidence_enrollment')")
                ).scalar_one()
                if present is None:
                    continue
                account_id = connection.execute(
                    select(evidence_enrollment.c.account_id).where(
                        evidence_enrollment.c.id == enrollment_id
                    )
                ).scalar_one_or_none()
        if account_id is not None:
            return row.id, account_id
    return None


def revoke_bridge(args: argparse.Namespace) -> int:
    boot = bootstrap()
    located = _locate(boot.backend, args.enrollment_id)
    if located is None:
        print(f"not_found: no enrollment {args.enrollment_id}", file=sys.stderr)
        return 1
    workspace_id, account_id = located
    outcome = _dispatch(
        workspace_id,
        ENROLLMENT_REVOKE,
        {"enrollment_id": str(args.enrollment_id), "account_id": str(account_id)},
    )
    if outcome is None:
        return 1
    print(f"enrollment {args.enrollment_id} revoked", file=sys.stderr)
    return 0

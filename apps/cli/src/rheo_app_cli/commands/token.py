"""``rheo token issue --account --workspace --set --kind [--discover]``,
``rheo token revoke <token-id>``, ``rheo token list [--workspace] [--account]`` and
``rheo token events --workspace [--limit]``.

``list`` reads the control plane directly, as ``revoke``'s lookup does, and prints
named columns only (never the token hash); a ``connector`` row also shows its
client's name and the grant's absolute end. ``events`` dispatches
``core.oauth_event.list`` under an operator context, so its role gate is
``dispatch()``'s.

``--discover`` issues the token in discover-then-call mode (issue #262): the named
set's grants plus ``core.tool.call``, so an MCP client sees a fixed handful of tools
however many modules are installed and reaches the rest through ``operations_call``
(``docs/architecture/runtime-and-mcp.md`` § Discover-then-call).

Both dispatch under an operator context (``context_for_operator``) through the
real, role-checked path -- ``context_for_operator`` -> ``registry.authorize``
-> ``dispatch`` -- exactly like ``rheo workspace status``, never a handler
call that bypasses ``authorize()``.

``issue`` loads the deployment's allowed modules first (issue #117), so a token's
named set is expanded against the same operation and tool inventory the ``core``
process serves; see :func:`load_operation_inventory`.

``revoke`` takes only a token id (no ``--workspace``): a token's own workspace
is looked up first (``get_access_token`` by id, a plain control-plane read),
and the operator context is built for *that* workspace -- the only way this
run's ``core.token.revoke`` (a ``workspace``-scoped operation) can be reached
with nothing but a bare id on the command line.

Output contract, matching ``account create``/``workspace create``: ``token
issue`` prints exactly the raw token value and a newline to stdout (the token
id, kind and expiry go to stderr, since the value is what a caller actually
needs to use); ``token revoke`` mints nothing new, so like ``member add`` it
stays silent on stdout.
"""

import argparse
import sys
from datetime import datetime
from typing import Final
from uuid import UUID

from rheo_core.boundary import Refusal, context_for_operator
from rheo_core.modules import ManifestInvalid, load_modules
from rheo_core.oauth.operations import OAUTH_EVENT_LIST, OAuthEventList
from rheo_core.oauth.service import sanitize_client_name
from rheo_core.operations import dispatch
from rheo_core.operations.core_ops import TOKEN_ISSUE, TOKEN_REVOKE
from rheo_core.operations.refusals import RegistrationRefused
from rheo_core.storage.control_plane import get_access_token, list_access_tokens
from rheo_core.storage.oauth_store import list_connector_grants
from rheo_core.tokens.sets import register_core_tools

from rheo_app_cli.context import bootstrap


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("token", help="access tokens (cli/mcp)")
    commands = parser.add_subparsers(dest="token_command", metavar="<command>")
    commands.required = True

    issue = commands.add_parser("issue", help="mint a token; prints the raw value once")
    issue.add_argument("--account", required=True, type=UUID, metavar="ACCOUNT_ID")
    issue.add_argument("--workspace", required=True, type=UUID, metavar="WORKSPACE_ID")
    issue.add_argument("--set", dest="set_name", required=True, metavar="NAME")
    issue.add_argument("--kind", required=True, choices=["cli", "mcp"])
    issue.add_argument(
        "--discover",
        action="store_true",
        help="discover-then-call mode: the MCP client sees a fixed tool list",
    )
    issue.set_defaults(handler=issue_token)

    revoke = commands.add_parser("revoke", help="revoke a token by id")
    revoke.add_argument("token_id", type=UUID)
    revoke.set_defaults(handler=revoke_token)

    listing = commands.add_parser(
        "list", help="every token row, connector grants included; no secret"
    )
    listing.add_argument("--workspace", type=UUID, metavar="WORKSPACE_ID")
    listing.add_argument("--account", type=UUID, metavar="ACCOUNT_ID")
    listing.set_defaults(handler=list_tokens)

    events = commands.add_parser("events", help="a workspace's OAuth connector events")
    events.add_argument("--workspace", required=True, type=UUID, metavar="WORKSPACE_ID")
    events.add_argument("--limit", type=int, default=50, metavar="N")
    events.set_defaults(handler=list_oauth_events)


def _print_outcome_error(state: str, error_text: str | None) -> None:
    print(f"{state}: {error_text}" if error_text else state, file=sys.stderr)


def load_operation_inventory() -> str | None:
    """Register what ``core`` registers on top of the core operations, or say why not.

    ``core.token.issue`` expands a named set against the live registries: ``cli_full``
    and ``read_only`` read ``REGISTRY``, ``agent_default`` reads ``TOOL_REGISTRY``.
    :func:`~rheo_app_cli.context.bootstrap` registers the core operations only, so
    before issue #117 a CLI-issued token could never hold a module operation, and an
    ``mcp`` token's ``agent_default`` named no tool at all. This runs the same two
    calls ``apps/core``'s startup makes after ``register_core_operations()``:
    ``register_core_tools()`` and ``load_modules()`` under the deployment's
    ``modules.installed`` allowlist. It passes no job-kind, consumer or deletion
    registry, because issuing a token runs none of them.

    Returns ``None`` on success, or the ``module_invalid: <detail>`` line to print when
    the loader, or a registry it calls, refuses the allowed set (``ManifestInvalid``
    or ``RegistrationRefused``). A conflicting settings key is a ``SettingsError``,
    which ``run_command`` already prints and exits ``1`` on.
    """
    register_core_tools()
    try:
        load_modules()
    except ManifestInvalid as invalid:
        return f"module_invalid: {invalid}"
    except RegistrationRefused as refusal:
        # A shipped registry refusing one of the loaded module's declarations (a
        # tool naming a non-token-issuable operation, say), which the loader cannot
        # run ahead of registration. The same operator fact, so the same state.
        return f"module_invalid: {refusal}"
    return None


def issue_token(args: argparse.Namespace) -> int:
    bootstrap()
    refusal = load_operation_inventory()
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1
    ctx = context_for_operator(args.workspace)
    if isinstance(ctx, Refusal):
        print(ctx, file=sys.stderr)
        return 1
    outcome = dispatch(
        ctx,
        TOKEN_ISSUE,
        {
            "account_id": str(args.account),
            "set_name": args.set_name,
            "kind": args.kind,
            "discover": args.discover,
        },
    )
    if not outcome.ok or outcome.result is None:
        error = outcome.error
        _print_outcome_error(outcome.state, None if error is None else error.error_text)
        return 1
    result = outcome.result
    print(
        f"token {result.token_id} issued, kind {result.kind}, "  # type: ignore[attr-defined]
        f"expires {result.expires_at.isoformat()}",  # type: ignore[attr-defined]
        file=sys.stderr,
    )
    # The output contract: exactly the raw value and a newline on stdout.
    print(result.value)  # type: ignore[attr-defined]
    return 0


def revoke_token(args: argparse.Namespace) -> int:
    boot = bootstrap()
    with boot.backend.control_engine.connect() as connection:
        row = get_access_token(connection, args.token_id)
    if row is None:
        print(f"access_token_missing: no token {args.token_id}", file=sys.stderr)
        return 1
    ctx = context_for_operator(row.workspace_id)
    if isinstance(ctx, Refusal):
        print(ctx, file=sys.stderr)
        return 1
    outcome = dispatch(ctx, TOKEN_REVOKE, {"token_id": str(args.token_id)})
    if not outcome.ok:
        error = outcome.error
        _print_outcome_error(outcome.state, None if error is None else error.error_text)
        return 1
    print(f"token {args.token_id} revoked", file=sys.stderr)
    return 0


NULL_FIELD: Final = "-"
LISTED_CLIENT_NAME_LENGTH: Final = 60


def _field(value: object) -> str:
    if value is None:
        return NULL_FIELD
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def list_tokens(args: argparse.Namespace) -> int:
    """One tab-separated line per ``access_token`` row, oldest first: id, kind,
    issued_from, set, account, workspace, created, expires, last used, revoked,
    client name, grant end (the last two ``-`` unless the row is a connector grant).

    Each column is projected by name from the row: ``AccessTokenRow`` carries
    ``token_hash``, which must never reach the output, so nothing iterates the row.
    """
    boot = bootstrap()
    with boot.backend.control_engine.connect() as connection:
        rows = list_access_tokens(
            connection, workspace_id=args.workspace, account_id=args.account
        )
        grants = list_connector_grants(
            connection, workspace_id=args.workspace, account_id=args.account
        )
    for row in rows:
        grant = grants.get(row.id)
        columns = (
            row.id,
            row.kind,
            row.issued_from,
            row.set_name,
            row.account_id,
            row.workspace_id,
            row.created_at,
            row.expires_at,
            row.last_used_at,
            row.revoked_at,
            None
            if grant is None
            else sanitize_client_name(
                grant.client_name, max_length=LISTED_CLIENT_NAME_LENGTH
            ),
            None if grant is None else grant.grant_expires_at,
        )
        print("\t".join(_field(column) for column in columns))
    return 0


def list_oauth_events(args: argparse.Namespace) -> int:
    """``core.oauth_event.list`` under an operator context, one tab-separated line
    per returned event in the order received (newest first): occurred_at, event,
    outcome, account_id, workspace_id, client_id, token_id (the grant id). Only the
    operation's own fields are printed, so nothing it does not return can show."""
    bootstrap()
    ctx = context_for_operator(args.workspace)
    if isinstance(ctx, Refusal):
        print(ctx, file=sys.stderr)
        return 1
    outcome = dispatch(ctx, OAUTH_EVENT_LIST, {"limit": args.limit})
    if not outcome.ok or not isinstance(outcome.result, OAuthEventList):
        error = outcome.error
        _print_outcome_error(outcome.state, None if error is None else error.error_text)
        return 1
    for record in outcome.result.events:
        columns = (
            record.occurred_at,
            record.event,
            record.outcome,
            record.account_id,
            record.workspace_id,
            record.client_id,
            record.token_id,
        )
        print("\t".join(_field(column) for column in columns))
    return 0

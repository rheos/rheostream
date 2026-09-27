"""``rheo token issue --account --workspace --set --kind`` and
``rheo token revoke <token-id>``.

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
from uuid import UUID

from rheo_core.boundary import Refusal, context_for_operator
from rheo_core.modules import ManifestInvalid, load_modules
from rheo_core.operations import dispatch
from rheo_core.operations.core_ops import TOKEN_ISSUE, TOKEN_REVOKE
from rheo_core.operations.refusals import RegistrationRefused
from rheo_core.storage.control_plane import get_access_token
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
    issue.set_defaults(handler=issue_token)

    revoke = commands.add_parser("revoke", help="revoke a token by id")
    revoke.add_argument("token_id", type=UUID)
    revoke.set_defaults(handler=revoke_token)


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

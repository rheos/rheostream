"""``rheo openapi --out <path>``: emit the OpenAPI document for every registered
operation.

**The one subcommand that does not call** :func:`~rheo_app_cli.context.bootstrap`.
It needs no settings, no data root and no database: the document is built from the
registry alone, and registration reaches ``current_profile()`` only for the
``test_harness`` origin, which neither the core operations nor a real module use.
That is what makes the CI regeneration diff environment-independent — the emitted
bytes depend on exactly two things, the installed distributions and the resolved
``modules.installed`` setting, and on nothing else about the machine it ran on.

**It registers the core operations and then loads the allowed modules**, in that
order. Without the second call the document carries no module path at all, and
every claim about a module's generated client is vacuous. ``load_modules()`` honours
the ``modules.installed`` allowlist, so a module that is installed but not named
emits nothing — which is the end-to-end proof that discovery does not activate
anything.

Output contract, as elsewhere in this CLI: the document goes to stdout (``--out -``)
or to the named file, and the narrative goes to stderr. Serialised with sorted keys
and a two-space indent so re-emitting an unchanged registry is byte-identical.
"""

import argparse
import json
import sys
from pathlib import Path

from rheo_core.modules import load_modules
from rheo_core.operations import GENERATED_BANNER, build_document
from rheo_core.operations.core_ops import register_core_operations

BANNER_FIELD = "x-rheo-generated"
STDOUT_TARGET = "-"


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "openapi", help="emit the OpenAPI document for the registered operations"
    )
    parser.add_argument(
        "--out",
        required=True,
        help=f"where to write the document; {STDOUT_TARGET!r} writes to stdout",
    )
    parser.set_defaults(handler=emit_openapi)


def render() -> str:
    """The document as the bytes that land on disk: banner, sorted keys, newline."""
    register_core_operations()
    load_modules()
    document = build_document()
    # The banner is the only field this command adds. Nothing else about the
    # invocation is recorded in the artifact: a field naming the loaded modules, or
    # the machine, or the time, would make the byte-exact CI diff report on the
    # environment rather than on the contract.
    document[BANNER_FIELD] = GENERATED_BANNER
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def emit_openapi(args: argparse.Namespace) -> int:
    text = render()
    if args.out == STDOUT_TARGET:
        sys.stdout.write(text)
        return 0
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target} ({len(text)} bytes)", file=sys.stderr)
    return 0

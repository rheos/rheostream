"""``rheo web compose --out <path|->``: emit ``apps/web/src/modules.generated.ts``.

Like ``rheo openapi``, it bootstraps nothing: no database, no data root and no
settings. Unlike it, it does not read the ``modules.installed`` allowlist either —
compose reads every installed distribution's manifest (see
:mod:`rheo_app_cli.web_compose` for why), so the emitted bytes depend only on which
distributions the environment has installed.

**Writing to a path checks ``apps/web/package.json`` first** and refuses, naming it,
any composed module's web package that is not among its ``dependencies``.
``--out -`` skips the check: nothing is written into the tree, and it is how the
output can be inspected before a module's web package exists.

Output contract, as elsewhere in this CLI: the file goes to stdout (``--out -``) or
to the named path, and the narrative goes to stderr.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Final

from rheo_app_cli.web_compose import (
    MissingWebPackage,
    check_web_dependencies,
    installed_manifests,
    render,
)

STDOUT_TARGET: Final = "-"
DEFAULT_PACKAGE_JSON: Final = "apps/web/package.json"


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("web", help="the web application's build inputs")
    commands = parser.add_subparsers(dest="web_command", metavar="<command>")
    commands.required = True
    compose = commands.add_parser(
        "compose",
        help="emit the composed module file from every installed module's manifest",
    )
    compose.add_argument(
        "--out",
        required=True,
        help=f"where to write the file; {STDOUT_TARGET!r} writes to stdout",
    )
    compose.add_argument(
        "--package-json",
        default=DEFAULT_PACKAGE_JSON,
        help="the web application's package.json, checked before writing to a path "
        f"(default: {DEFAULT_PACKAGE_JSON})",
    )
    compose.set_defaults(handler=web_compose)


def web_compose(args: argparse.Namespace) -> int:
    manifests = installed_manifests()
    text = render(manifests)
    if args.out == STDOUT_TARGET:
        sys.stdout.write(text)
        return 0
    package_json = Path(args.package_json)
    try:
        declared = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"web_package_json_unreadable: {package_json}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(declared, dict):
        print(
            f"web_package_json_unreadable: {package_json}: not a JSON object",
            file=sys.stderr,
        )
        return 1
    try:
        check_web_dependencies(manifests, declared)
    except MissingWebPackage as refusal:
        print(str(refusal), file=sys.stderr)
        return 1
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target} ({len(text)} bytes)", file=sys.stderr)
    return 0

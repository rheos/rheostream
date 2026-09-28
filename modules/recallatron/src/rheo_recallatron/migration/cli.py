"""The ``recallatron-migrate`` console script (declared in this module's
``pyproject.toml``, never under ``apps/cli``: ``packages/`` and ``apps/`` may not import
Recallatron).

Entry contract, the same as the ``rheo`` operator command's:
``main(argv: Sequence[str] | None = None) -> int`` parses the list it is given, prints
usage and returns ``0`` with no subcommand, and never calls ``sys.exit`` itself;
argparse's own exits come back as the return value.

Output discipline: a subcommand that reads real predecessor content prints counts
only, never a line of that content. A refusal prints ``<state>: <detail>`` on stderr
and returns ``1``.

``denylist`` (spec § System Components 7) writes ``<out>/denylist.txt``: every live
graph entity label, every ``memory_items`` label and every distinct harvested query
string, normalized exactly as ``scripts/check_migration_outputs.py`` normalizes its
needles (whitespace runs collapsed, stripped, casefolded), deduplicated and sorted.
A line shorter than :data:`MIN_LINE_LENGTH` characters is dropped (a short token would
match everywhere), and so is a line equal, after the same normalization, to a line of
``<out>/denylist-exempt.txt`` when that file exists. The output path passes
:func:`require_private_output` before any input is read.

``inventory`` (spec § System Components 2) takes ``--out`` as a directory, creates it
if absent, and writes ``<out>/inventory.json`` and ``<out>/inventory.md`` in the one
invocation; each full path passes :func:`require_private_output` before any input is
read.
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from rheo_recallatron.migration import inventory
from rheo_recallatron.migration.predecessor import graph_source, sqlite_source
from rheo_recallatron.migration.private_paths import (
    PrivateOutputRefusal,
    require_private_output,
)

DENYLIST_FILE_NAME: Final = "denylist.txt"
EXEMPT_FILE_NAME: Final = "denylist-exempt.txt"
MIN_LINE_LENGTH: Final = 5

HARVEST_UNREADABLE = "harvest_unreadable"
HARVEST_EMPTY = "harvest_empty"
INPUT_UNREADABLE = "input_unreadable"
OUTPUT_UNWRITABLE = "output_unwritable"

_WHITESPACE = re.compile(r"\s+")


class InputRefusal(Exception):
    def __init__(self, state: str, detail: str) -> None:
        super().__init__(f"{state}: {detail}")
        self.state = state
        self.detail = detail


def normalize(text: str) -> str:
    """The leak scan's own normalizer, so both sides compare the same form."""
    return _WHITESPACE.sub(" ", text.strip()).casefold()


@dataclass(frozen=True)
class Denylist:
    lines: tuple[str, ...]
    short_dropped: int
    exempted: int


def build_denylist(sources: Iterable[str], exempt: Iterable[str]) -> Denylist:
    candidates = {normalize(text) for text in sources} - {""}
    long_enough = {line for line in candidates if len(line) >= MIN_LINE_LENGTH}
    exempt_lines = {normalize(text) for text in exempt} - {""}
    kept = long_enough - exempt_lines
    return Denylist(
        lines=tuple(sorted(kept)),
        short_dropped=len(candidates) - len(long_enough),
        exempted=len(long_enough) - len(kept),
    )


def read_harvest_queries(path: Path) -> list[str]:
    """Every non-empty ``query`` value of a harvested ``tool_call_log`` export, a
    ``.json`` list of row objects or a ``.csv`` with a ``query`` column.

    A harvest that yields no query string at all is refused: the real harvest has
    query text, so an empty result means the file is not the shape this reads, and a
    denylist silently missing every query would pass the leak scan it feeds."""
    try:
        if path.suffix == ".json":
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise InputRefusal(HARVEST_UNREADABLE, f"not a JSON list: {path}")
            values = [row.get("query") for row in rows if isinstance(row, dict)]
        elif path.suffix == ".csv":
            with path.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None or "query" not in reader.fieldnames:
                    raise InputRefusal(HARVEST_UNREADABLE, f"no query column: {path}")
                values = [row.get("query") for row in reader]
        else:
            raise InputRefusal(
                HARVEST_UNREADABLE, f"expected a .json or .csv harvest: {path}"
            )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        csv.Error,
        RecursionError,  # a pathologically nested .json harvest
    ) as error:
        raise InputRefusal(HARVEST_UNREADABLE, f"cannot read {path}") from error
    queries = [value for value in values if isinstance(value, str) and value.strip()]
    if not queries:
        raise InputRefusal(
            HARVEST_EMPTY, f"no query string in {len(values)} harvest rows: {path}"
        )
    return queries


def _read_exempt(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise InputRefusal(
            INPUT_UNREADABLE, f"cannot read the exempt file {path}"
        ) from error


def _write_private_text(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
                handle.truncate(0)
                handle.write(text)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise InputRefusal(OUTPUT_UNWRITABLE, f"cannot write {path}") from error


def _write_private_lines(path: Path, lines: Sequence[str]) -> None:
    _write_private_text(path, "".join(f"{line}\n" for line in lines))


def _read_graph(ontology: str) -> graph_source.ParsedGraph:
    try:
        return graph_source.read_graph(ontology)
    except (OSError, UnicodeDecodeError) as error:
        raise InputRefusal(
            INPUT_UNREADABLE, f"cannot read the ontology graph under {ontology}"
        ) from error


def _run_denylist(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    target = require_private_output(out_dir / DENYLIST_FILE_NAME)

    connection = sqlite_source.open_snapshot(args.snapshot)
    try:
        memory_labels = sqlite_source.memory_item_labels(connection)
    except sqlite3.DatabaseError as error:
        raise InputRefusal(
            INPUT_UNREADABLE, f"cannot read the snapshot {args.snapshot}"
        ) from error
    finally:
        connection.close()
    graph = _read_graph(args.ontology)
    queries = read_harvest_queries(Path(args.harvest))

    denylist = build_denylist(
        [*graph_source.live_entity_labels(graph), *memory_labels, *queries],
        _read_exempt(target.parent / EXEMPT_FILE_NAME),
    )
    _write_private_lines(target, denylist.lines)
    print(
        f"denylist: written={len(denylist.lines)} "
        f"short_dropped={denylist.short_dropped} exempted={denylist.exempted}"
    )
    return 0


def _run_inventory(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    json_target = require_private_output(out_dir / inventory.INVENTORY_JSON_NAME)
    markdown_target = require_private_output(
        out_dir / inventory.INVENTORY_MARKDOWN_NAME
    )

    graph = _read_graph(args.ontology)
    connection = sqlite_source.open_snapshot(args.snapshot)
    try:
        result = inventory.build_inventory(connection, graph, args.ontology)
    except sqlite3.DatabaseError as error:
        raise InputRefusal(
            INPUT_UNREADABLE, f"cannot read the snapshot {args.snapshot}"
        ) from error
    except OSError as error:
        raise InputRefusal(
            INPUT_UNREADABLE, f"cannot list the ontology directory {args.ontology}"
        ) from error
    finally:
        connection.close()

    _write_private_text(json_target, inventory.render_json(result))
    _write_private_text(markdown_target, inventory.render_markdown(result))
    print(
        f"inventory: objects={len(result.objects)} "
        f"ontology_files={len(result.ontology_files)}"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recallatron-migrate",
        description="Migration groundwork tooling over the private predecessor data.",
    )
    subcommands = parser.add_subparsers(dest="command")
    denylist = subcommands.add_parser(
        "denylist", help="write <out>/denylist.txt for the local leak scan"
    )
    denylist.add_argument("--snapshot", required=True, help="the SQLite snapshot")
    denylist.add_argument("--ontology", required=True, help="the ontology directory")
    denylist.add_argument("--harvest", required=True, help="the harvest .json/.csv")
    denylist.add_argument(
        "--out", required=True, help="output directory inside the private root"
    )
    denylist.set_defaults(handler=_run_denylist)
    inventory_parser = subcommands.add_parser(
        "inventory",
        help="write <out>/inventory.json and <out>/inventory.md (FR 1)",
    )
    inventory_parser.add_argument(
        "--snapshot", required=True, help="the SQLite snapshot"
    )
    inventory_parser.add_argument(
        "--ontology", required=True, help="the ontology directory"
    )
    inventory_parser.add_argument(
        "--out",
        required=True,
        help="output directory inside the private root; created if absent",
    )
    inventory_parser.set_defaults(handler=_run_inventory)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else 2
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_usage()
        return 0
    try:
        return handler(args)
    except (
        PrivateOutputRefusal,
        sqlite_source.SnapshotRefusal,
        InputRefusal,
    ) as refusal:
        print(f"{refusal.state}: {refusal.detail}", file=sys.stderr)
        return 1

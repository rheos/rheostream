"""``finance state export`` and ``finance state import``: ledger state as files.

``export`` makes a directory: ``state/<table>.jsonl`` per state table (its sorted
canonical lines, each ending in a newline) and ``manifest.json`` with the format, the
schema head, the derived digest and each table's row count and sha256. The directory
is 0700 and every file 0600. The import recomputes every count and hash from the files
and refuses ``manifest_mismatch`` before anything is dispatched; ``finance rebuild``
follows it.

By default both run on the workspace's own database (``composition.build_context``).
``--source-dsn-file`` (export only) reads an old standalone ledger through a private
DSN file instead. Output is counts and hashes, never a row.

This module imports neither sqlalchemy nor ``rheo_finance.storage``: the handlers
import what they dispatch with inside themselves.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from rheo_finance.render import Report, emit, emit_error

FORMAT: Final = "finance-state/1"
MANIFEST_NAME: Final = "manifest.json"
STATE_DIR: Final = "state"
TABLE_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,62}")
DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600

Tables = list[tuple[str, tuple[str, ...]]]


class StateFilesRefused(Exception):
    """The export directory or its files cannot be used. The message is a fixed code."""


def file_bytes(lines: Sequence[str]) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def check_empty(out: Path) -> None:
    """``out`` must be absent or an empty directory."""
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise StateFilesRefused("out_not_empty")
    if out.is_dir() and any(out.iterdir()):
        raise StateFilesRefused("out_not_empty")


def _private_dir(path: Path) -> None:
    path.mkdir(mode=DIR_MODE, exist_ok=True)
    path.chmod(DIR_MODE)  # mkdir's mode is filtered by the umask


def _write(path: Path, body: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), FILE_MODE)
        handle.write(body)


def write_export(
    out: Path, *, schema_head: str, derived_digest: str, tables: Tables
) -> list[dict[str, object]]:
    """Write the directory; return the manifest's table entries."""
    check_empty(out)
    _private_dir(out)
    _private_dir(out / STATE_DIR)
    entries: list[dict[str, object]] = []
    for name, lines in tables:
        if not TABLE_NAME.fullmatch(name):
            raise StateFilesRefused("table_name_invalid")
        body = file_bytes(lines)
        _write(out / STATE_DIR / f"{name}.jsonl", body)
        entries.append(
            {
                "name": name,
                "rows": len(lines),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    manifest = {
        "format": FORMAT,
        "schema_head": schema_head,
        "derived_digest": derived_digest,
        "tables": entries,
    }
    _write(out / MANIFEST_NAME, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    return entries


def read_export(source: Path) -> tuple[str, Tables]:
    """The schema head and each table's lines, after every count and hash matched."""
    mismatch = StateFilesRefused("manifest_mismatch")
    try:
        manifest = json.loads((source / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise mismatch from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise mismatch
    head = manifest.get("schema_head")
    entries = manifest.get("tables")
    if not isinstance(head, str) or not isinstance(entries, list):
        raise mismatch
    tables: Tables = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise mismatch
        name, rows, digest = entry.get("name"), entry.get("rows"), entry.get("sha256")
        if not (isinstance(name, str) and TABLE_NAME.fullmatch(name)):
            raise mismatch
        try:
            body = (source / STATE_DIR / f"{name}.jsonl").read_bytes()
        except OSError:
            raise mismatch from None
        if hashlib.sha256(body).hexdigest() != digest:
            raise mismatch
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise mismatch from None
        if text and not text.endswith("\n"):
            raise mismatch
        lines = tuple(text[:-1].split("\n")) if text else ()
        if len(lines) != rows or isinstance(rows, bool):
            raise mismatch
        tables.append((name, lines))
    return head, tables


def _cmd_export(args: argparse.Namespace) -> int:
    from rheo_finance import composition, operations
    from rheo_finance.contracts import Role
    from rheo_finance.state.transfer import ExportOut

    label = "finance state export"
    out = Path(args.out)
    try:
        check_empty(out)
    except StateFilesRefused as exc:
        emit_error(Report([f"{label}: refused. {exc}"]))
        return 1
    ctx = (
        composition.build_source_context(Path(args.source_dsn_file))
        if args.source_dsn_file
        else composition.build_context(Role.OWNER)
    )
    try:
        result = operations.dispatch("finance.state.export", {}, Role.OWNER, ctx=ctx)
    except operations.OperationError as exc:
        emit_error(Report([f"{label}: refused. {exc}"]))
        return 1
    assert isinstance(result, ExportOut)
    try:
        entries = write_export(
            out,
            schema_head=result.schema_head,
            derived_digest=result.derived_digest,
            tables=[(table.name, table.lines) for table in result.tables],
        )
    except StateFilesRefused as exc:
        emit_error(Report([f"{label}: refused. {exc}"]))
        return 1
    total = sum(table.rows for table in result.tables)
    emit(
        Report(
            [
                f"{label}: {len(entries)} tables, {total} rows;"
                f" derived digest {result.derived_digest}"
            ],
            [f"{e['name']}: {e['rows']} rows, sha256 {e['sha256']}" for e in entries],
        )
    )
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    from rheo_finance import composition, operations
    from rheo_finance.contracts import Role
    from rheo_finance.state.transfer import ImportOut

    label = "finance state import"
    try:
        head, tables = read_export(Path(args.from_dir))
    except StateFilesRefused as exc:
        emit_error(Report([f"{label}: refused. {exc}"]))
        return 1
    ctx = composition.build_context(Role.OWNER)
    payload = {
        "schema_head": head,
        "tables": [{"name": name, "lines": list(lines)} for name, lines in tables],
    }
    try:
        result = operations.dispatch(
            "finance.state.import", payload, Role.OWNER, ctx=ctx
        )
    except operations.OperationError as exc:
        emit_error(Report([f"{label}: refused. {exc}"]))
        return 1
    assert isinstance(result, ImportOut)
    total = sum(table.rows for table in result.tables)
    emit(
        Report(
            [
                f"{label}: {len(result.tables)} tables, {total} rows;"
                " run finance rebuild"
            ],
            [f"{table.name}: {table.rows} rows" for table in result.tables],
        )
    )
    return 0


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("state", help="move ledger state between databases")
    sub = parser.add_subparsers(dest="state_command", required=True)
    export = sub.add_parser("export", help="write every state table to a directory")
    export.add_argument("--out", required=True, help="an absent or empty directory")
    export.add_argument(
        "--source-dsn-file",
        help="a private env file with FINANCE_OWNER_DSN for an old standalone ledger",
    )
    export.set_defaults(handler=_cmd_export)
    load = sub.add_parser("import", help="load an export into an empty ledger")
    load.add_argument("--from", dest="from_dir", required=True)
    load.set_defaults(handler=_cmd_import)

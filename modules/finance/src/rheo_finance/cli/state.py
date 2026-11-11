"""``finance state export`` and ``finance state import``: ledger state as files.

``export`` makes a directory: ``state/<table>.jsonl`` per state table (its sorted
canonical lines, each ending in a newline) and ``manifest.json`` with the format, the
schema head, the derived digest and each table's row count and sha256. The directory
is 0700 and every file 0600. The import recomputes every count and hash from the files
and refuses ``manifest_mismatch`` before anything is dispatched, and
``state_file_refused`` for a symlink anywhere on the path (an ancestor directory
included), a directory component that is not a directory, a file that is not a regular
file, or a file that fails once opened; ``finance rebuild`` follows it.

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
import stat
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


_DIR_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
_FILE_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _open_failed(name: str, dir_fd: int, *, want_dir: bool) -> StateFilesRefused:
    """The code for a component ``os.open`` refused: a symlink, or a file where a
    directory belongs, is refused; a missing or unreadable one is a mismatch. The
    ``lstat`` only names the code; the refusal itself is the ``O_NOFOLLOW`` open's."""
    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except OSError:
        return StateFilesRefused("manifest_mismatch")
    if stat.S_ISLNK(mode) or (want_dir and not stat.S_ISDIR(mode)):
        return StateFilesRefused("state_file_refused")
    return StateFilesRefused("manifest_mismatch")


def _open_dir(path: Path) -> int:
    """A descriptor for directory ``path``, opened one component at a time from the
    root (or the working directory) with ``O_NOFOLLOW``, so no component, an ancestor
    included, is followed through a symlink, and nothing can swap one in between a
    check and the read."""
    if path.is_absolute():
        start, parts = path.anchor, path.parts[1:]
    else:
        start, parts = ".", path.parts
    try:
        fd = os.open(start, _DIR_FLAGS)
    except OSError:
        raise StateFilesRefused("manifest_mismatch") from None
    try:
        for part in parts:
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError:
                raise _open_failed(part, fd, want_dir=True) from None
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read(dir_fd: int, name: str) -> bytes:
    """A regular file's bytes, opened relative to ``dir_fd`` and never through a
    symlink (the write side's ``O_NOFOLLOW``). A missing or unreadable file is a
    mismatch; a symlink or a non-regular file is refused outright (``O_NONBLOCK``:
    opening a FIFO does not wait for a writer), and so is a file whose ``fstat`` or
    read fails once it is open."""
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError:
        raise _open_failed(name, dir_fd, want_dir=False) from None
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise StateFilesRefused("state_file_refused")
            return handle.read()
    except OSError:
        raise StateFilesRefused("state_file_refused") from None


def read_export(source: Path) -> tuple[str, Tables]:
    """The schema head and each table's lines, after every count and hash matched.
    Nothing is read through a symlink: not an ancestor of the directory, the directory,
    ``state/`` or any file."""
    root = _open_dir(source)
    try:
        return _read_export(root)
    finally:
        os.close(root)


def _read_export(root: int) -> tuple[str, Tables]:
    mismatch = StateFilesRefused("manifest_mismatch")
    try:
        manifest = json.loads(_read(root, MANIFEST_NAME).decode("utf-8"))
    except ValueError:
        raise mismatch from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise mismatch
    head = manifest.get("schema_head")
    entries = manifest.get("tables")
    if not isinstance(head, str) or not isinstance(entries, list):
        raise mismatch
    try:
        state = os.open(STATE_DIR, _DIR_FLAGS, dir_fd=root)
    except OSError:
        raise _open_failed(STATE_DIR, root, want_dir=True) from None
    try:
        return head, _read_tables(state, entries)
    finally:
        os.close(state)


def _read_tables(state: int, entries: list[object]) -> Tables:
    mismatch = StateFilesRefused("manifest_mismatch")
    tables: Tables = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise mismatch
        name, rows, digest = entry.get("name"), entry.get("rows"), entry.get("sha256")
        if not (isinstance(name, str) and TABLE_NAME.fullmatch(name)):
            raise mismatch
        body = _read(state, f"{name}.jsonl")
        if hashlib.sha256(body).hexdigest() != digest:
            raise mismatch
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise mismatch from None
        if text and not text.endswith("\n"):
            raise mismatch
        lines = tuple(text[:-1].split("\n")) if text else ()
        # A strict integer: JSON 2.0 equals 2 and true equals 1, and neither is a count.
        if type(rows) is not int or len(lines) != rows:
            raise mismatch
        tables.append((name, lines))
    return tables


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

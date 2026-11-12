"""``finance remote``: work on the module from a laptop over SSH (ADR-0026).

Laptop commands:

- ``push`` streams the archive, new statements, the owner's data inputs, a state
  export and (only with ``--keys``) secrets into the container's layout;
- ``pull --into DIR`` copies ``data/packs/**`` out of it;
- ``exec -- ARGS`` runs ``finance ARGS`` in the container.

Server halves, which push and pull run inside the container: ``receive`` and
``send-packs``. They bind the workspace's layout and refuse when it is not there.

Every message is a fixed code and counts: no host, path or file name. Each handler
maps its own failures to codes, so ``main``'s generic failure line never sees an
``OSError`` that names a path.

Only ``cli.main`` imports this module (the import-graph guard). It imports neither
sqlalchemy, ``rheo_finance.storage``, ``rheo_finance.wiring`` nor ``rheo_core``; the
two server handlers import ``composition`` inside themselves.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Final

from rheo_finance import db
from rheo_finance.remote import receive as receiver
from rheo_finance.remote import send as sender
from rheo_finance.remote import ssh
from rheo_finance.remote.frames import (
    Readable,
    RemoteRefused,
    Writable,
    write_abort,
    write_magic,
)
from rheo_finance.render import Report, emit, emit_error

STDIN_FD: Final = 0
# After these the remote outcome is not known, so no "Nothing was written."
_OUTCOME_UNKNOWN: Final = frozenset(
    {"ssh_failed", "ssh_timeout", "remote_failed", "remote_reply_invalid"}
)


def _files(count: int) -> str:
    return f"{count} file" if count == 1 else f"{count} files"


def refusal_line(label: str, exc: RemoteRefused) -> str:
    """The refusal message. ``target_differs`` counts the differing targets, refused
    before anything landed; any other count is the files that landed before the
    refusal (the receiver's, or a push reply's ``NO <code> <n>``), whatever the code."""
    code = exc.code
    if code == "target_differs":
        return f"{label}: refused. {code} ({_files(exc.files)}). Nothing was written."
    if exc.files:
        return f"{label}: refused. {code} ({_files(exc.files)} written before it)."
    if code in _OUTCOME_UNKNOWN:
        return f"{label}: refused. {code}."
    return f"{label}: refused. {code}. Nothing was written."


def _optional(raw: str | None) -> Path | None:
    return None if raw is None else Path(raw)


def _config(flag: str | None) -> ssh.RemoteConfig:
    return ssh.load_config(ssh.config_path(flag))


# --- laptop commands ------------------------------------------------------------------


def _cmd_push(args: argparse.Namespace) -> int:
    label = "finance remote push"
    try:
        cfg = _config(args.config)
        collected = sender.collect(
            sender.Sources(
                archive=_optional(args.archive),
                inbox=_optional(args.inbox),
                data=_optional(args.data),
                state=_optional(args.state),
                keys=_optional(args.keys),
            )
        )
        counts = ssh.push(cfg, collected.items, replace=args.replace)
    except RemoteRefused as exc:
        emit_error(Report([refusal_line(label, exc)]))
        return 1
    except OSError:
        emit_error(Report([refusal_line(label, RemoteRefused("io_failed"))]))
        return 1
    emit(
        Report(
            [
                f"{label}: {counts.new} new, {counts.identical} identical,"
                f" {counts.replaced} replaced, {collected.skipped} skipped."
            ]
        )
    )
    return 0


def _check_into(into: Path) -> None:
    """``into`` must be a directory and not a link."""
    try:
        mode = into.lstat().st_mode
    except OSError:
        raise RemoteRefused("into_refused") from None
    if not stat.S_ISDIR(mode):
        raise RemoteRefused("into_refused")


def _cmd_pull(args: argparse.Namespace) -> int:
    label = "finance remote pull"
    into = Path(args.into)
    try:
        _check_into(into)
        counts = ssh.pull(_config(args.config), into)
    except RemoteRefused as exc:
        emit_error(Report([refusal_line(label, exc)]))
        return 1
    except OSError:
        emit_error(Report([refusal_line(label, RemoteRefused("io_failed"))]))
        return 1
    emit(Report([f"{label}: {counts.new} new, {counts.identical} identical."]))
    return 0


def _has_control(word: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in word)


def exec_command(
    config: str | None,
    words: list[str],
    *,
    isatty: Callable[[int], bool] = os.isatty,
) -> int:
    """``finance remote exec``: a terminal on both ends only when stdin is one, so
    ``finance sink approve`` keeps its own terminal check."""
    label = "finance remote exec"
    if words[:1] == ["--"]:
        words = words[1:]
    try:
        if not words or any(_has_control(word) for word in words):
            raise RemoteRefused("argument_refused")
        return ssh.run_exec(_config(config), words, tty=isatty(STDIN_FD))
    except RemoteRefused as exc:
        emit_error(Report([f"{label}: refused. {exc.code}."]))
        return 1
    except OSError:
        emit_error(Report([f"{label}: refused. io_failed."]))
        return 1


def _cmd_exec(args: argparse.Namespace) -> int:
    return exec_command(args.config, list(args.args))


# --- server halves --------------------------------------------------------------------


def serve_receive(raw: Readable, layout: db.FinanceLayout, *, replace: bool) -> str:
    """Receive a push into ``layout``; the reply line for the laptop."""
    areas = {
        "archive": layout.archive,
        "inbox": layout.inbox,
        "data": layout.data,
        "keys": layout.keys,
    }
    try:
        counts = receiver.receive(
            raw,
            areas,
            layout.root,
            direction="push",
            replaceable=receiver.owner_inputs if replace else receiver.never,
        )
    except RemoteRefused as exc:
        return f"NO {exc.code} {exc.files}"
    return f"OK {counts.new} {counts.identical} {counts.replaced}"


def serve_send_packs(out: Writable, layout: db.FinanceLayout) -> None:
    """Stream ``layout``'s ``data/packs`` to the laptop."""
    sender.send_packs(out, layout)


def _bound_layout() -> db.FinanceLayout:
    from rheo_finance import composition

    return db.check_layout(composition.bind_workspace_layout())


def _cmd_receive(args: argparse.Namespace) -> int:
    try:
        layout = _bound_layout()
    except db.LayoutRefused as exc:
        emit(f"NO {exc} 0")
        return 1
    reply = serve_receive(sys.stdin.buffer, layout, replace=args.replace)
    emit(reply)
    return 0 if reply.startswith("OK") else 1


def _cmd_send_packs(_args: argparse.Namespace) -> int:
    # The one binary stream of the module: frames for the laptop's receiver, so not
    # text through ``emit``. Nothing else is written to stdout in this process.
    out = sys.stdout.buffer
    try:
        layout = _bound_layout()
    except db.LayoutRefused as exc:
        write_magic(out)
        write_abort(out, str(exc))
        out.flush()
        return 1
    try:
        serve_send_packs(out, layout)
    except (RemoteRefused, OSError):
        return 1
    return 0


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "remote", help="push to, pull from or run the module over ssh"
    )
    sub = parser.add_subparsers(dest="remote_command", required=True)
    config_help = "the remote config file (else $FINANCE_REMOTE_CONFIG)"

    push = sub.add_parser("push", help="copy files into the module's layout")
    push.add_argument("--config", help=config_help)
    push.add_argument("--archive", metavar="DIR", help="the statement archive")
    push.add_argument("--inbox", metavar="DIR", help="new statements")
    push.add_argument(
        "--data",
        metavar="DIR",
        help="settings.toml, identities.toml and manual/ from an old data directory",
    )
    push.add_argument("--state", metavar="DIR", help="a finance state export")
    push.add_argument(
        "--keys", metavar="DIR", help="key files, each readable by its owner alone"
    )
    push.add_argument(
        "--replace",
        action="store_true",
        help="replace data/settings.toml, data/identities.toml and data/manual files",
    )
    push.set_defaults(handler=_cmd_push)

    pull = sub.add_parser("pull", help="copy data/packs out of the module")
    pull.add_argument("--config", help=config_help)
    pull.add_argument("--into", required=True, metavar="DIR", help="a directory")
    pull.set_defaults(handler=_cmd_pull)

    run = sub.add_parser("exec", help="run a finance command in the module")
    run.add_argument("--config", help=config_help)
    run.add_argument("args", nargs=argparse.REMAINDER, metavar="-- ARGS")
    run.set_defaults(handler=_cmd_exec)

    receive = sub.add_parser("receive", help="the server half of push")
    receive.add_argument("--replace", action="store_true")
    receive.set_defaults(handler=_cmd_receive)

    packs = sub.add_parser("send-packs", help="the server half of pull")
    packs.set_defaults(handler=_cmd_send_packs)

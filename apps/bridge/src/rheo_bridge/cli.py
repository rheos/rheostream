"""``rheo-bridge``: set up, install, inspect and remove the laptop bridge (FR 11).

Each subcommand is a named function that takes ``bridge_home`` (and, where it
needs them, ``enrolled_dir`` and ``projects_root``) as explicit paths. None of
them reads ``$HOME``, ``Path.home()``, ``~`` or the working directory; only
:func:`main` resolves the user's home and derives the two defaults from it, so
the functions run against temporary directories in tests.

Output is content-free by design. No subcommand prints a token value, a session
hash, a native key, a transcript path or any transcript text. ``install-hook``
and ``init`` print paths the operator chose; ``status`` prints counts, the
enrollment id and the token's expiry.

Exit codes: 0 on success, 1 when a subcommand refuses or cannot run (the reason
goes to stderr), and ``drain`` passes the worker's own code through: 0 ok, 1 the
worker could not run, 2 the server refused the batch (see ``worker.EXIT_*``).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TextIO
from uuid import UUID

from rheo_bridge import config as bridge_config
from rheo_bridge import hook, keys, paths, settings_file, state, worker
from rheo_bridge.client import (
    HttpIngestClient,
    IngestClient,
    check_base_url,
    check_token,
)

EXIT_OK: Final = 0
EXIT_FAILURE: Final = 1
# ``set-token`` reads one JSON line; anything longer is not what
# ``rheo evidence enroll --json`` prints.
SET_TOKEN_MAX_BYTES: Final = 16 * 1024
_SET_TOKEN_KEYS: Final = frozenset({"enrollment_id", "token", "expires_at"})
_GIT_TIMEOUT_SECONDS: Final = 30


class CliError(Exception):
    """A refusal: the message goes to stderr and the exit code is 1."""


# --- shared helpers -----------------------------------------------------------


def _load_config(bridge_home: Path) -> bridge_config.Config:
    try:
        config = bridge_config.load(bridge_home)
    except bridge_config.ConfigError as exc:
        raise CliError(str(exc)) from exc
    if config is None:
        raise CliError("the bridge is not set up here; run `rheo-bridge init` first")
    return config


def _write_private(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` at 0600: temp file, fsync, rename.

    The rename is atomic, so a reader sees the old content or the new, never an
    empty or partial file.
    """
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, paths.FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _read_token(bridge_home: Path) -> str:
    path = paths.token_path(bridge_home)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise CliError("no token stored; run `rheo-bridge set-token`") from exc
    except OSError as exc:
        raise CliError(f"the token file cannot be read: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(SET_TOKEN_MAX_BYTES + 1)
    if len(raw) > SET_TOKEN_MAX_BYTES:
        # Never truncate: a cut-down value would be sent as if it were whole.
        raise CliError("the token file is longer than any bridge token")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CliError("the token file holds invalid characters") from exc


@contextmanager
def _worker_lock(bridge_home: Path) -> Iterator[bool]:
    """Hold ``worker.lock`` without waiting; yield whether it was free."""
    fd = os.open(
        paths.worker_lock_path(bridge_home),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        paths.FILE_MODE,
    )
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _open_state(bridge_home: Path) -> sqlite3.Connection | None:
    """``state.sqlite`` if the worker has created it; never creates it."""
    path = paths.state_path(bridge_home)
    if not os.path.lexists(path):
        return None
    return state.connect(path)


def _holds_unacknowledged(row: state.SessionRow) -> bool:
    """Whether a session row still owes the server something.

    An open row has not been closed by the worker's settle rule, so its range
    is not known to be fully acknowledged. A tombstone closed with its range
    acknowledged owes nothing, unless it still carries an unsent gap key.
    """
    return row.closed_at is None or row.pending_gap_key is not None


def _unacknowledged_count(bridge_home: Path, *, now: datetime) -> int:
    """Fold the spool, then count the rows that owe the server something.

    The caller holds ``worker.lock``. Folding first counts sessions the hook
    has named but no worker has read yet: they are discarded too.
    """
    connection = state.connect(paths.state_path(bridge_home))
    try:
        worker.fold_spool(connection, bridge_home, now=now)
        rows = state.list_sessions(connection, include_closed=True)
    finally:
        state.close(connection)
    return sum(1 for row in rows if _holds_unacknowledged(row))


def _enroll_command(machine_fp: str, project_fp: str) -> str:
    return (
        "rheo evidence enroll --account <ACCOUNT_ID> --workspace <WORKSPACE_ID> "
        f"--machine {machine_fp} --project {project_fp} --json"
    )


def _revoke_command(enrollment_id: str | None) -> str:
    return f"rheo evidence revoke {enrollment_id or '<ENROLLMENT_ID>'}"


def _default_worker_argv(executable: str) -> list[str]:
    # The venv's own bin directory: not resolved, because resolving a venv's
    # python lands in the base interpreter's directory.
    return [str(Path(executable).parent / "rheo-bridge"), "drain"]


# --- init ---------------------------------------------------------------------


def init(
    bridge_home: Path,
    *,
    enrolled_dir: Path,
    api_url: str,
    executable: str | None = None,
) -> int:
    """Create ``bridge_home``, the machine key, the install salt and the config.

    No network call and no settings write. Refuses when the bridge already has
    a machine key: a second ``init`` would re-key everything the server holds.
    """
    if not enrolled_dir.is_dir():
        raise CliError(f"--enrolled-dir {enrolled_dir} is not a directory")
    try:
        check_base_url(api_url)
    except ValueError as exc:
        raise CliError(f"--api-url: {exc}") from exc
    if os.path.lexists(paths.machine_key_path(bridge_home)):
        raise CliError(
            f"{bridge_home} is already set up; run `rheo-bridge uninstall --purge` "
            "first to start over"
        )
    enrolled = os.path.realpath(enrolled_dir)
    paths.ensure_bridge_home(bridge_home)
    machine_key = keys.load_or_create_machine_key(bridge_home)
    config = bridge_config.Config(
        api_url=api_url,
        enrolled_dir=enrolled,
        worker_argv=_default_worker_argv(executable or sys.executable),
        install_salt=secrets.token_hex(16),
    )
    bridge_config.save(bridge_home, config)
    machine_fp = keys.machine_fingerprint(machine_key)
    project_fp = keys.project_fingerprint(enrolled)
    print(f"enrolled_dir: {enrolled}")
    print(f"machine_fingerprint: {machine_fp}")
    print(f"project_fingerprint: {project_fp}")
    print(
        "operator command (run on the flagship; pipe its one output line "
        "into `rheo-bridge set-token`):"
    )
    print(f"  {_enroll_command(machine_fp, project_fp)}")
    return EXIT_OK


# --- set-token ----------------------------------------------------------------


def _parse_token_line(raw: str) -> tuple[str, str, str]:
    """``(enrollment_id, token, expires_at)`` from exactly one JSON line.

    No message here quotes the input: it holds the token value.
    """
    lines = raw.splitlines()
    if len(lines) != 1:
        raise CliError("set-token reads exactly one JSON line from stdin")
    try:
        document = json.loads(lines[0])
    except json.JSONDecodeError:
        raise CliError("set-token: stdin is not a JSON line") from None
    if not isinstance(document, dict) or set(document) != _SET_TOKEN_KEYS:
        raise CliError(
            'set-token: the line must be {"enrollment_id", "token", "expires_at"}'
        )
    enrollment_id = document["enrollment_id"]
    token = document["token"]
    expires_at = document["expires_at"]
    if not all(isinstance(v, str) for v in (enrollment_id, token, expires_at)):
        raise CliError("set-token: every field must be a string")
    try:
        UUID(enrollment_id)
    except ValueError:
        raise CliError("set-token: enrollment_id is not a uuid") from None
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError:
        raise CliError("set-token: expires_at is not an ISO-8601 time") from None
    if expires.tzinfo is None or expires.utcoffset() is None:
        raise CliError("set-token: expires_at has no timezone")
    try:
        check_token(token)
    except ValueError:
        raise CliError("set-token: the token is empty or malformed") from None
    return enrollment_id, token, expires_at


def set_token(bridge_home: Path, *, stdin: TextIO) -> int:
    """Store the bridge token from a pipe; print only its enrollment id and expiry.

    A rotate is the same call: the token file is replaced atomically, and any
    back-off hold the old token earned is cleared so the next drain posts at
    once instead of waiting out a refusal the new token fixes.
    """
    if stdin.isatty():
        raise CliError(
            "set-token refuses a terminal: pipe `rheo evidence enroll --json` "
            "(or `rotate --json`) into it"
        )
    config = _load_config(bridge_home)
    raw = stdin.read(SET_TOKEN_MAX_BYTES + 1)
    if len(raw) > SET_TOKEN_MAX_BYTES:
        raise CliError("set-token: stdin is longer than one token line")
    enrollment_id, token, expires_at = _parse_token_line(raw)
    _write_private(paths.token_path(bridge_home), token.encode("ascii"))
    bridge_config.save(
        bridge_home,
        replace(config, enrollment_id=enrollment_id, token_expires_at=expires_at),
    )
    connection = _open_state(bridge_home)
    if connection is not None:
        try:
            state.clear_hold(connection)
        finally:
            state.close(connection)
    print(f"enrollment_id: {enrollment_id}")
    print(f"token_expires_at: {expires_at}")
    return EXIT_OK


# --- install-hook / remove-hook -----------------------------------------------


def _check_enrolled(config: bridge_config.Config, enrolled_dir: Path) -> Path:
    if os.path.realpath(enrolled_dir) != config.enrolled_dir:
        raise CliError(
            f"--enrolled-dir {enrolled_dir} is not the enrolled directory "
            f"{config.enrolled_dir}; the worker would refuse its sessions"
        )
    return Path(config.enrolled_dir)


def git_refusal(enrolled_dir: Path) -> str | None:
    """Why the settings file could be committed, or ``None`` if it cannot.

    Outside a Git working tree (or with no ``git`` installed) there is nothing
    to commit it to. Inside one, ``git check-ignore`` must report the file
    ignored; a tracked file is never reported ignored, so it is refused too.
    """

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(enrolled_dir), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )

    try:
        inside = run("rev-parse", "--is-inside-work-tree")
    except FileNotFoundError:
        return None
    if inside.returncode != 0:
        if "not a git repository" in inside.stderr:
            return None
        return "git could not tell whether the directory is in a working tree"
    if inside.stdout.strip() != "true":
        return None
    ignored = run("check-ignore", "-q", "--", str(settings_file.SETTINGS_RELATIVE))
    if ignored.returncode == 0:
        return None
    if ignored.returncode == 1:
        return (
            f"{settings_file.SETTINGS_RELATIVE} is not ignored by Git here, so the "
            "hook entry could be committed; add it to .gitignore first"
        )
    return "git check-ignore failed"


def _hook_commands(bridge_home: Path, interpreter: str) -> dict[str, str]:
    script = paths.hook_path(bridge_home).absolute()
    return {
        event: settings_file.hook_command(interpreter, script, event)
        for event in settings_file.HOOK_EVENTS
    }


def install_hook(
    bridge_home: Path,
    *,
    enrolled_dir: Path,
    dry_run: bool = False,
    interpreter: str | None = None,
) -> int:
    """Add the ``Stop`` and ``SessionEnd`` hooks to the enrolled directory.

    Every check runs before any write. ``dry_run`` prints the unified diff of
    the settings file and writes nothing at all.
    """
    config = _load_config(bridge_home)
    target_dir = _check_enrolled(config, enrolled_dir)
    refusal = git_refusal(target_dir)
    if refusal is not None:
        raise CliError(refusal)
    path = settings_file.settings_path(target_dir)
    try:
        document, before = settings_file.read(path)
        # [capture 10] — may be revised at reconciliation: an absolute,
        # resolved interpreter, so the command runs under CLI and Desktop.
        commands = _hook_commands(
            bridge_home, interpreter or os.path.realpath(sys.executable)
        )
        result = settings_file.merge(document, commands)
    except settings_file.SettingsError as exc:
        raise CliError(str(exc)) from exc
    merged = result.document
    after = settings_file.render(merged)
    changed = merged != document
    if dry_run:
        if changed:
            print(settings_file.unified_diff(path, before, after), end="")
        else:
            print(f"{path.absolute()}: the hooks are already installed; no change")
        return EXIT_OK
    _write_private(paths.hook_path(bridge_home), Path(hook.__file__).read_bytes())
    if not changed:
        print(f"{path.absolute()}: the hooks are already installed; no change")
        return EXIT_OK
    path.parent.mkdir(exist_ok=True)
    settings_file.write_atomic(path, after)
    # Record what this install made, so remove-hook can put the file back.
    bridge_config.save(
        bridge_home,
        replace(
            config,
            hook_created_settings=config.hook_created_settings or before is None,
            hook_created_containers=sorted(
                set(config.hook_created_containers) | result.created
            ),
        ),
    )
    print(f"{path.absolute()}: installed the Stop and SessionEnd hooks")
    return EXIT_OK


def _refuse_other_dir(config: bridge_config.Config, enrolled_dir: Path) -> None:
    if os.path.realpath(enrolled_dir) != config.enrolled_dir:
        raise CliError(
            f"--enrolled-dir {enrolled_dir} is not the enrolled directory "
            f"{config.enrolled_dir}; the hook is installed there. Nothing removed"
        )


def remove_hook(
    bridge_home: Path, *, enrolled_dir: Path, tolerate_bad_config: bool = False
) -> int:
    """Remove only this bridge's hook entries from ``enrolled_dir``'s settings.

    An emptied ``hooks`` table or event list is dropped only if install
    created it, and the file is deleted only when install created it and it
    is ``{}`` once the entries are gone: what was there before comes back.

    A readable config whose ``enrolled_dir`` is not ``enrolled_dir`` is
    refused with nothing removed: the hook would stay installed where it is.
    ``tolerate_bad_config`` (``uninstall``'s path) treats an unusable
    ``config.json`` as no record: the entries naming the bridge hook are still
    removed, and no container or file is deleted.
    """
    try:
        config = bridge_config.load(bridge_home)
    except bridge_config.ConfigError as exc:
        if not tolerate_bad_config:
            raise CliError(str(exc)) from exc
        config = None
    target_dir = Path(os.path.realpath(enrolled_dir))
    record = config
    if record is not None:
        _refuse_other_dir(record, enrolled_dir)
    created = record is not None and record.hook_created_settings
    path = settings_file.settings_path(target_dir)
    try:
        document, before = settings_file.read(path)
        pruned = settings_file.remove(
            document,
            created=record.hook_created_containers if record is not None else (),
        )
    except settings_file.SettingsError as exc:
        raise CliError(str(exc)) from exc
    if before is None:
        print(f"{path.absolute()}: no settings file; nothing to remove")
    elif created and pruned == {}:
        path.unlink()
        print(f"{path.absolute()}: removed the hooks and the file install created")
    elif pruned != document:
        settings_file.write_atomic(path, settings_file.render(pruned))
        print(f"{path.absolute()}: removed the hooks")
    else:
        print(f"{path.absolute()}: no bridge hook found; no change")
    if record is not None and (
        record.hook_created_settings or record.hook_created_containers
    ):
        # The file is gone or is the user's again; a later install that finds
        # it did not create it or anything in it.
        bridge_config.save(
            bridge_home,
            replace(record, hook_created_settings=False, hook_created_containers=[]),
        )
    return EXIT_OK


# --- uninstall ----------------------------------------------------------------


def uninstall(
    bridge_home: Path,
    *,
    enrolled_dir: Path,
    purge: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Remove the hooks, then the token and the machine key.

    ``purge`` first folds the spool and prints ``discarded_unacknowledged: N``,
    the sessions whose range the server has not acknowledged (including those
    only the spool names so far), then deletes ``bridge_home``. That line is
    the one named record of the one local discard this design allows.

    A readable config naming another enrolled directory is refused before
    anything is counted or removed: deleting ``bridge_home`` while the hook
    stays installed elsewhere would leave a hook with no script.
    """
    try:
        config = bridge_config.load(bridge_home)
    except bridge_config.ConfigError:
        # A corrupt config must not keep the credentials on disk.
        config = None
    if config is not None:
        _refuse_other_dir(config, enrolled_dir)
    enrollment_id = config.enrollment_id if config is not None else None
    if not purge:
        remove_hook(bridge_home, enrolled_dir=enrolled_dir, tolerate_bad_config=True)
        _delete_credentials(bridge_home)
        print(f"operator revoke command: {_revoke_command(enrollment_id)}")
        return EXIT_OK
    if not bridge_home.is_dir():
        remove_hook(bridge_home, enrolled_dir=enrolled_dir, tolerate_bad_config=True)
        print("discarded_unacknowledged: 0")
        print(f"operator revoke command: {_revoke_command(enrollment_id)}")
        return EXIT_OK
    with _worker_lock(bridge_home) as held:
        if not held:
            raise CliError("a bridge worker is running; retry once it exits")
        count = _unacknowledged_count(bridge_home, now=now())
        print(f"discarded_unacknowledged: {count}")
        remove_hook(bridge_home, enrolled_dir=enrolled_dir, tolerate_bad_config=True)
        _delete_credentials(bridge_home)
        shutil.rmtree(bridge_home)
    print(f"operator revoke command: {_revoke_command(enrollment_id)}")
    return EXIT_OK


def _delete_credentials(bridge_home: Path) -> None:
    paths.token_path(bridge_home).unlink(missing_ok=True)
    paths.machine_key_path(bridge_home).unlink(missing_ok=True)


# --- status -------------------------------------------------------------------


def _iso(moment: int | None) -> str:
    if moment is None:
        return "none"
    return datetime.fromtimestamp(moment, UTC).isoformat()


def _days_remaining(expires_at: str | None, now: datetime) -> str:
    if expires_at is None:
        return "unknown"
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError:
        return "unknown"
    if expires.tzinfo is None or expires.utcoffset() is None:
        expires = expires.replace(tzinfo=UTC)
    return str(math.floor((expires - now).total_seconds() / 86400))


def status(bridge_home: Path, *, now: datetime) -> int:
    """Print counts, plus the enrollment id and token expiry; never a session
    hash, a key, a path or any transcript text.

    ``sessions_pending`` counts open sessions, ``sessions_held`` those of them
    waiting out the back-off hold, ``sessions_closed`` the tombstones kept for
    resumption. ``last_accepted_at`` is the latest ``accepted:*`` ledger stamp.
    """
    config = _load_config(bridge_home)
    at = int(now.timestamp())
    connection = _open_state(bridge_home)
    if connection is None:
        rows: list[state.SessionRow] = []
        hold = state.NO_HOLD
        ledger: dict[str, state.LedgerEntry] = {}
    else:
        try:
            rows = state.list_sessions(connection, include_closed=True)
            hold = state.get_hold(connection)
            ledger = state.get_ledger(connection)
        finally:
            state.close(connection)
    pending = sum(1 for row in rows if row.closed_at is None)
    holding = hold.hold_until is not None and at < hold.hold_until
    accepted_stamps = [
        entry.last_at
        for reason, entry in ledger.items()
        if reason.startswith(state.ACCEPTED_PREFIX) and entry.last_at is not None
    ]
    # The id is not a secret (it names the enrollment to rotate or revoke).
    print(f"enrollment_id: {config.enrollment_id or 'none'}")
    print(f"token_expires_at: {config.token_expires_at or 'none'}")
    print(f"sessions_pending: {pending}")
    print(f"sessions_held: {pending if holding else 0}")
    print(f"sessions_closed: {len(rows) - pending}")
    print(f"hold_until: {_iso(hold.hold_until)}")
    for reason, entry in ledger.items():
        print(f"ledger.{reason}: {entry.count}")
    print(f"last_accepted_at: {_iso(max(accepted_stamps, default=None))}")
    print(f"token_days_remaining: {_days_remaining(config.token_expires_at, now)}")
    # The worker refuses to run on a loose bridge_home, and a hook-spawned
    # worker has no terminal, so this line is where that shows.
    problem = _privacy_problem(bridge_home)
    print(f"bridge_home_private: {'no' if problem else 'yes'}")
    if problem:
        print(f"status: the worker will not run: {problem}", file=sys.stderr)
    return EXIT_OK


def _privacy_problem(bridge_home: Path) -> str | None:
    """Why ``bridge_home`` fails the worker's privacy check, or ``None``.

    The message names only the bridge's own files and their modes.
    """
    try:
        paths.check_private(bridge_home)
    except OSError as exc:
        return str(exc)
    return None


# --- drain --------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def drain(
    bridge_home: Path,
    *,
    projects_root: Path,
    client: IngestClient | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> int:
    """Run one worker pass in the foreground and return its exit code.

    Without ``client``, builds the HTTPS client from ``config.json``'s
    ``api_url`` and the ``token`` file.
    """
    if client is None:
        config = _load_config(bridge_home)
        token = _read_token(bridge_home)
        try:
            client = HttpIngestClient(config.api_url, token)
        except ValueError as exc:
            raise CliError(f"cannot build the ingest client: {exc}") from exc
    code = worker.drain(
        bridge_home, projects_root=projects_root, client=client, now=now
    )
    if code == worker.EXIT_FAILURE:
        problem = _privacy_problem(bridge_home)
        reason = (
            f"bridge_home is not private: {problem}"
            if problem
            else "configuration or machine key unusable"
        )
        print(f"drain: the worker could not run: {reason}", file=sys.stderr)
    elif code == worker.EXIT_REFUSED:
        print(
            "drain: the server refused the batch; see `rheo-bridge status` "
            "(rotate the token or re-enroll)",
            file=sys.stderr,
        )
    return code


# --- main ---------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rheo-bridge")
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    commands.required = True

    init_parser = commands.add_parser("init", help="set up this machine's bridge")
    init_parser.add_argument("--enrolled-dir", required=True, type=Path)
    init_parser.add_argument("--api-url", required=True)

    commands.add_parser(
        "set-token", help="store the token piped from `rheo evidence enroll --json`"
    )

    install = commands.add_parser("install-hook", help="add the Claude Code hooks")
    install.add_argument("--enrolled-dir", required=True, type=Path)
    install.add_argument("--dry-run", action="store_true")

    remove = commands.add_parser("remove-hook", help="remove the Claude Code hooks")
    remove.add_argument("--enrolled-dir", required=True, type=Path)

    uninstall_parser = commands.add_parser(
        "uninstall", help="remove the hooks, the token and the machine key"
    )
    uninstall_parser.add_argument("--enrolled-dir", required=True, type=Path)
    uninstall_parser.add_argument("--purge", action="store_true")

    commands.add_parser("status", help="counts only")
    commands.add_parser("drain", help="run the worker once in the foreground")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    user_home = Path.home()
    bridge_home = user_home / ".rheo-bridge"
    projects_root = user_home / ".claude" / "projects"
    try:
        match args.command:
            case "init":
                return init(
                    bridge_home, enrolled_dir=args.enrolled_dir, api_url=args.api_url
                )
            case "set-token":
                return set_token(bridge_home, stdin=sys.stdin)
            case "install-hook":
                return install_hook(
                    bridge_home, enrolled_dir=args.enrolled_dir, dry_run=args.dry_run
                )
            case "remove-hook":
                return remove_hook(bridge_home, enrolled_dir=args.enrolled_dir)
            case "uninstall":
                return uninstall(
                    bridge_home, enrolled_dir=args.enrolled_dir, purge=args.purge
                )
            case "status":
                return status(bridge_home, now=_utc_now())
            case "drain":
                return drain(bridge_home, projects_root=projects_root)
            case _:
                raise AssertionError(args.command)
    except CliError as exc:
        print(f"rheo-bridge {args.command}: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())

"""The one hop to the worker container: ``ssh``, then ``docker exec`` (ADR-0026).

This is the one module of the remote commands that starts a process. Every process
starts from an argument list, never through a local shell. The remote command is one
argument, ``shlex.join`` of fixed or validated words, so the remote login shell sees
only single-quoted words; ``--`` always comes before the destination.

Each command takes two sessions: one finds the container by its two compose labels
(``docker ps``; exactly one running id, else a refusal), the next runs ``finance`` in
it. A container replaced in between fails closed as a docker error.

On push and pull, ssh's own diagnostics are dropped and the reply is read against a
closed grammar, so no message can carry a host, a path or remote text. ``exec``
passes the remote command's own output through.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from rheo_finance import db
from rheo_finance.remote import receive as receiver
from rheo_finance.remote import send as sender
from rheo_finance.remote.frames import REPLY_CODES, RemoteRefused

# The variable ``wiring.core`` reads; restated because this module never imports it.
WORKSPACE_VARIABLE: Final = "FINANCE_WORKSPACE_ID"
CONFIG_VARIABLE: Final = "FINANCE_REMOTE_CONFIG"
SSH: Final = "ssh"
PROJECT_LABEL: Final = "com.docker.compose.project"
SERVICE_LABEL: Final = "com.docker.compose.service"
RESOLVE_SECONDS: Final = 120
RESOLVE_MAX_BYTES: Final = 64 * 1024
REPLY_MAX_BYTES: Final = 4096

_DESTINATION: Final = re.compile(
    r"(?:[a-z_][a-z0-9_.-]{0,31}@)?[A-Za-z0-9][A-Za-z0-9.-]{0,252}"
)
_COMPOSE_NAME: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_DOCKER_WORD: Final = re.compile(r"[A-Za-z0-9_./:=@+-]{1,64}")
_CONTAINER_ID: Final = re.compile(r"[0-9a-f]{12,64}")
_COUNT: Final = r"(0|[1-9][0-9]{0,9})"
_OK: Final = re.compile(rf"OK {_COUNT} {_COUNT} {_COUNT}\n")
_NO: Final = re.compile(rf"NO ([a-z_]{{1,48}}) {_COUNT}\n")
_KEYS: Final = frozenset(
    {"destination", "compose_project", "compose_service", "workspace_id", "docker"}
)
_OPTIONAL: Final = frozenset({"docker"})


@dataclass(frozen=True)
class RemoteConfig:
    """Where the module runs: an ssh destination, the worker's compose labels, the
    workspace, and the docker command (``["sudo", "-n", "docker"]`` for example)."""

    destination: str
    compose_project: str
    compose_service: str
    workspace_id: str
    docker: tuple[str, ...] = ("docker",)


def config_path(flag: str | None, environ: Mapping[str, str] = os.environ) -> Path:
    """``--config`` when given, else ``$FINANCE_REMOTE_CONFIG``; there is no default."""
    raw = flag or environ.get(CONFIG_VARIABLE, "").strip()
    if not raw:
        raise RemoteRefused("remote_config_not_set")
    return Path(raw)


def load_config(path: Path) -> RemoteConfig:
    """The config file, read only when it is a regular file of its owner's alone and
    not a link (``db.read_private_file``); every value checked."""
    refused = RemoteRefused("remote_config_refused")
    try:
        data: dict[str, Any] = tomllib.loads(db.read_private_file(path))
    except (db.LayoutRefused, tomllib.TOMLDecodeError):
        raise refused from None
    if set(data) - _KEYS or _KEYS - _OPTIONAL - set(data):
        raise refused
    words = [data[key] for key in sorted(_KEYS - _OPTIONAL)]
    if not all(isinstance(word, str) for word in words):
        raise refused
    project, service, destination = (
        data["compose_project"],
        data["compose_service"],
        data["destination"],
    )
    if not (
        _DESTINATION.fullmatch(destination)
        and _COMPOSE_NAME.fullmatch(project)
        and _COMPOSE_NAME.fullmatch(service)
    ):
        raise refused
    try:
        workspace = str(uuid.UUID(data["workspace_id"]))
    except ValueError:
        raise refused from None
    docker = data.get("docker", ["docker"])
    if not (
        isinstance(docker, list)
        and 1 <= len(docker) <= 4
        and all(isinstance(w, str) and _DOCKER_WORD.fullmatch(w) for w in docker)
        and not docker[0].startswith("-")
    ):
        raise refused
    return RemoteConfig(
        destination=destination,
        compose_project=project,
        compose_service=service,
        workspace_id=workspace,
        docker=tuple(docker),
    )


# --- argument lists -------------------------------------------------------------------


def resolve_words(cfg: RemoteConfig) -> list[str]:
    return [
        *cfg.docker,
        "ps",
        "--filter",
        f"label={PROJECT_LABEL}={cfg.compose_project}",
        "--filter",
        f"label={SERVICE_LABEL}={cfg.compose_service}",
        "--format",
        "{{.ID}}",
    ]


def exec_words(
    cfg: RemoteConfig, cid: str, args: Sequence[str], *, stdin: bool, tty: bool
) -> list[str]:
    """``docker exec`` stops reading its own options at the container id, so ``args``
    are finance's alone."""
    return [
        *cfg.docker,
        "exec",
        *(["-i"] if stdin else []),
        *(["-t"] if tty else []),
        "-e",
        f"{WORKSPACE_VARIABLE}={cfg.workspace_id}",
        cid,
        "finance",
        *args,
    ]


def ssh_argv(
    cfg: RemoteConfig, words: Sequence[str], *, tty: bool, stream: bool
) -> list[str]:
    """ssh's argument list: a terminal only when asked, the escape character off for
    a binary stream, ``--`` before the destination, and the remote command as one
    argument."""
    return [
        SSH,
        "-t" if tty else "-T",
        *(["-e", "none"] if stream else []),
        "--",
        cfg.destination,
        shlex.join(words),
    ]


# --- sessions -------------------------------------------------------------------------


def resolve(cfg: RemoteConfig) -> str:
    """The one running container with both compose labels."""
    argv = ssh_argv(cfg, resolve_words(cfg), tty=False, stream=False)
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=RESOLVE_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise RemoteRefused("ssh_not_found") from None
    except subprocess.TimeoutExpired:
        raise RemoteRefused("ssh_timeout") from None
    except (subprocess.SubprocessError, OSError):
        raise RemoteRefused("ssh_failed") from None
    if done.returncode == 255:
        raise RemoteRefused("ssh_failed")
    if done.returncode != 0:
        raise RemoteRefused("remote_failed")
    if len(done.stdout) > RESOLVE_MAX_BYTES:
        raise RemoteRefused("remote_reply_invalid")
    try:
        text = done.stdout.decode("ascii")
    except UnicodeDecodeError:
        raise RemoteRefused("remote_reply_invalid") from None
    ids = [line.strip() for line in text.splitlines() if line.strip()]
    if not ids:
        raise RemoteRefused("container_not_found")
    if len(ids) > 1:
        raise RemoteRefused("container_ambiguous")
    if not _CONTAINER_ID.fullmatch(ids[0]):
        raise RemoteRefused("remote_reply_invalid")
    return ids[0]


def _start(argv: list[str], *, stdin: int) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except FileNotFoundError:
        raise RemoteRefused("ssh_not_found") from None
    except (subprocess.SubprocessError, OSError):
        raise RemoteRefused("ssh_failed") from None


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Kill and reap ``proc`` if it still runs. Push and pull call it in a ``finally``,
    once the outcome is set (a result returned or a refusal raised), so a failure
    here is ignored: it never replaces that outcome with a generic one."""
    with contextlib.suppress(OSError):
        if proc.poll() is None:
            proc.kill()
            proc.wait()


class _FdReader:
    """``read(n)`` as one ``os.read``: what has arrived, without waiting for ``n``."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def read(self, size: int, /) -> bytes:
        return os.read(self._fd, size)


def parse_reply(raw: bytes, returncode: int) -> receiver.Counts:
    """A push reply: ``OK <new> <identical> <replaced>`` or ``NO <code> <n>``, one line;
    anything else is ``remote_reply_invalid`` and is never shown. An ``OK`` with a
    non-zero exit is ``remote_reply_invalid`` too: the receiver exits 0 after ``OK``,
    so the outcome is not known."""
    if returncode == 255:
        raise RemoteRefused("ssh_failed")
    if not raw and returncode != 0:
        raise RemoteRefused("remote_failed")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise RemoteRefused("remote_reply_invalid") from None
    if found := _OK.fullmatch(text):
        if returncode != 0:
            raise RemoteRefused("remote_reply_invalid")
        return receiver.Counts(
            new=int(found[1]), identical=int(found[2]), replaced=int(found[3])
        )
    if (found := _NO.fullmatch(text)) and found[1] in REPLY_CODES:
        raise RemoteRefused(found[1], files=int(found[2]))
    raise RemoteRefused("remote_reply_invalid")


def push(
    cfg: RemoteConfig, items: tuple[sender.Outgoing, ...], *, replace: bool
) -> receiver.Counts:
    """Stream ``items`` to ``finance remote receive`` in the container."""
    cid = resolve(cfg)
    args = ["remote", "receive", *(["--replace"] if replace else [])]
    words = exec_words(cfg, cid, args, stdin=True, tty=False)
    proc = _start(ssh_argv(cfg, words, tty=False, stream=True), stdin=subprocess.PIPE)
    local: RemoteRefused | None = None
    try:
        assert proc.stdin is not None and proc.stdout is not None
        # From the first byte sent to the end of push, the stream may have ended whole
        # and the receiver committed: closing stdin delivers the end record, with any
        # bytes a failed flush kept. So any OSError but a broken pipe, from the send or
        # the close, is the outcome-unknown ssh_failed, never "Nothing was written". A
        # local refusal (source_changed) cut the stream before its end and wins.
        try:
            try:
                sender.send(proc.stdin, items)
            except BrokenPipeError:
                pass  # the receiver refused early; its reply says why
            except RemoteRefused as exc:
                local = exc  # the receiver sees no end and keeps nothing
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
        except OSError:
            if local is None:
                raise RemoteRefused("ssh_failed") from None
        try:
            reply = proc.stdout.read(REPLY_MAX_BYTES + 1)
        except OSError:
            raise RemoteRefused("ssh_failed") from None
        if len(reply) > REPLY_MAX_BYTES:
            raise RemoteRefused("remote_reply_invalid")
        try:
            returncode = proc.wait()
        except OSError:
            raise RemoteRefused("ssh_failed") from None
    finally:
        _stop(proc)
    if local is not None:
        raise local
    return parse_reply(reply, returncode)


def pull(cfg: RemoteConfig, into: Path) -> receiver.Counts:
    """Receive ``finance remote send-packs`` from the container into ``into``."""
    cid = resolve(cfg)
    words = exec_words(cfg, cid, ["remote", "send-packs"], stdin=False, tty=False)
    proc = _start(
        ssh_argv(cfg, words, tty=False, stream=True), stdin=subprocess.DEVNULL
    )
    try:
        assert proc.stdout is not None
        try:
            return receiver.receive(
                _FdReader(proc.stdout.fileno()),
                {"packs": into},
                into,
                direction="pull",
            )
        except RemoteRefused as exc:
            if exc.code == "stream_truncated":
                returncode = proc.wait()
                if returncode == 255:
                    raise RemoteRefused("ssh_failed", files=exc.files) from None
                if returncode != 0:
                    raise RemoteRefused("remote_failed", files=exc.files) from None
            raise
    finally:
        _stop(proc)


def run_exec(cfg: RemoteConfig, args: Sequence[str], *, tty: bool) -> int:
    """Run ``finance ARGS`` in the container with this process's streams; the status
    is ssh's: the remote command's, or 255."""
    cid = resolve(cfg)
    words = exec_words(cfg, cid, args, stdin=True, tty=tty)
    try:
        return subprocess.run(
            ssh_argv(cfg, words, tty=tty, stream=False), check=False
        ).returncode
    except FileNotFoundError:
        raise RemoteRefused("ssh_not_found") from None
    except (subprocess.SubprocessError, OSError):
        raise RemoteRefused("ssh_failed") from None

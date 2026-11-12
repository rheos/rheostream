"""A stand-in for ``ssh`` in the remote tests. It never opens a connection.

``support.FakeRemote.install`` puts a ``bin/ssh`` first on ``PATH`` that runs this
script. It logs its argument list, accepts only ``-T``, ``-t`` and ``-e none`` before
a mandatory ``--``, then exactly the destination and one remote command. That command
must be words ``shlex.join`` quoted whole (else exit 97), led by the configured docker
words. ``ps`` prints the configured container ids; ``exec ... finance remote
receive|send-packs`` runs the module's server half in this process against a layout
under ``tmp_path``; any other ``exec`` prints ``fake-finance``.

Faults (``FAKE_SSH_FAULT``): ``cut:N`` ends the stream after N bytes (reading) or exits
255 after N bytes (writing); ``kill:N`` kills this process after N bytes;
``stall:N`` writes N bytes, then sleeps; ``reply:TEXT`` answers a push with TEXT;
``ssh255`` fails as ssh does, naming the destination on stderr.

A script, not a test module: imports only the standard library and ``rheo_finance``.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import time
from pathlib import Path

STALL_SECONDS = 30


def _fault() -> tuple[str, int, str]:
    raw = os.environ.get("FAKE_SSH_FAULT", "")
    kind, _, value = raw.partition(":")
    if kind in ("cut", "kill", "stall"):
        return kind, int(value), ""
    return kind, 0, value


class _Reader:
    """stdin, one ``os.read`` at a time, with the reading faults."""

    def __init__(self) -> None:
        self.kind, self.limit, _ = _fault()
        self.seen = 0

    def read(self, size: int, /) -> bytes:
        if self.kind in ("cut", "kill"):
            if self.seen >= self.limit:
                if self.kind == "kill":
                    os.kill(os.getpid(), signal.SIGKILL)
                return b""
            size = min(size, self.limit - self.seen)
        data = os.read(0, size)
        self.seen += len(data)
        return data


class _Writer:
    """stdout's binary stream with the writing faults."""

    def __init__(self) -> None:
        self.raw = sys.stdout.buffer
        self.kind, self.limit, _ = _fault()
        self.seen = 0

    def write(self, data: bytes, /) -> int:
        if self.kind in ("cut", "kill", "stall"):
            room = self.limit - self.seen
            if len(data) >= room:
                self.raw.write(data[: max(room, 0)])
                self.raw.flush()
                self.seen += max(room, 0)
                if self.kind == "stall":
                    time.sleep(STALL_SECONDS)
                if self.kind == "kill":
                    os.kill(os.getpid(), signal.SIGKILL)
                os._exit(255)
        self.raw.write(data)
        self.seen += len(data)
        return len(data)

    def flush(self) -> None:
        self.raw.flush()


def _options(argv: list[str]) -> list[str] | None:
    """The words after ``--`` when every option before it is allowed."""
    index = 0
    while index < len(argv) and argv[index] != "--":
        if argv[index] in ("-T", "-t"):
            index += 1
        elif argv[index] == "-e" and argv[index + 1 : index + 2] == ["none"]:
            index += 2
        else:
            return None
    if index >= len(argv):
        return None
    return argv[index + 1 :]


def _ps(words: list[str], containers: list[str]) -> int:
    expected = [
        "ps",
        "--filter",
        "label=com.docker.compose.project=" + os.environ["FAKE_SSH_PROJECT"],
        "--filter",
        "label=com.docker.compose.service=" + os.environ["FAKE_SSH_SERVICE"],
        "--format",
        "{{.ID}}",
    ]
    if words != expected:
        return 1
    sys.stdout.write("".join(f"{each}\n" for each in containers))
    return 0


def _exec(words: list[str], containers: list[str]) -> int:
    words = words[1:]
    while words[:1] in (["-i"], ["-t"]):
        words = words[1:]
    workspace = "FINANCE_WORKSPACE_ID=" + os.environ["FAKE_SSH_WORKSPACE"]
    if words[:2] != ["-e", workspace] or len(words) < 4:
        return 1
    cid, program, args = words[2], words[3], words[4:]
    if cid not in containers or program != "finance":
        return 1
    # Imported here, so the resolve call starts as fast as a bare interpreter.
    from rheo_finance import db
    from rheo_finance.cli import remote as remote_cli
    from rheo_finance.render import emit

    layout = db.layout_under(Path(os.environ["FAKE_SSH_UPLOADS"]))
    kind, _, text = _fault()
    if args[:2] == ["remote", "receive"]:
        if kind == "reply":
            sys.stdout.write(text + "\n")
            return 0
        replace = "--replace" in args[2:]
        emit(remote_cli.serve_receive(_Reader(), layout, replace=replace))
        return 0
    if args == ["remote", "send-packs"]:
        remote_cli.serve_send_packs(_Writer(), layout)
        return 0
    sys.stdout.write("fake-finance\n")
    return int(os.environ.get("FAKE_SSH_EXIT", "0"))


def main() -> int:
    argv = sys.argv[1:]
    with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "stdin_tty": os.isatty(0)}) + "\n")
    rest = _options(argv)
    if rest is None or len(rest) != 2 or rest[0] != os.environ["FAKE_SSH_DEST"]:
        return 255
    destination, remote = rest
    if _fault()[0] == "ssh255":
        sys.stderr.write(f"ssh: connect to host {destination}: refused\n")
        return 255
    words = shlex.split(remote)
    if shlex.join(words) != remote:
        return 97
    docker = os.environ["FAKE_SSH_DOCKER"].split(" ")
    if words[: len(docker)] != docker:
        return 127
    words = words[len(docker) :]
    containers = [each for each in os.environ["FAKE_SSH_CONTAINERS"].split(",") if each]
    if words[:1] == ["ps"]:
        return _ps(words, containers)
    if words[:1] == ["exec"]:
        return _exec(words, containers)
    return 127


if __name__ == "__main__":
    sys.exit(main())

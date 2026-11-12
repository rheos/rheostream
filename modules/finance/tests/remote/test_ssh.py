"""The ssh hop (L11 to L16): exact argument lists, the terminal rule, hostile words and
names kept off every command line, the container rule, fixed-code messages, and the
laptop's import boundary.

No database, no network: the only ``ssh`` run is ``fake_ssh.py``.
"""

from __future__ import annotations

import errno
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import IO

import pytest

from rheo_finance.cli import remote as remote_cli
from rheo_finance.cli.main import main
from rheo_finance.remote import ssh
from rheo_finance.remote.frames import RemoteRefused

from .support import (
    CONTAINER,
    DESTINATION,
    PROJECT,
    WORKSPACE,
    FakeRemote,
    config_values,
    write_config,
)

HOSTILE_NAMES = ("-oProxyCommand=x.pdf", "; rm -rf ~.pdf", "$(x).pdf")
RESOLVE = [
    "docker",
    "ps",
    "--filter",
    "label=com.docker.compose.project=sample-project",
    "--filter",
    "label=com.docker.compose.service=worker",
    "--format",
    "{{.ID}}",
]
ENV_WORD = f"FINANCE_WORKSPACE_ID={WORKSPACE}"


def _exec_line(*args: str, flags: tuple[str, ...] = ("-i",)) -> str:
    return shlex.join(
        ["docker", "exec", *flags, "-e", ENV_WORD, CONTAINER, "finance", *args]
    )


def test_push_and_pull_argument_lists_are_exact_and_never_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    monkeypatch.setattr(os, "isatty", lambda _fd: True)
    archive = tmp_path / "laptop"
    archive.mkdir()
    for name in HOSTILE_NAMES:
        (archive / name).write_bytes(b"synthetic")
    (fake.layout.data / "packs").mkdir()
    into = tmp_path / "into"
    into.mkdir()

    config = ["--config", str(fake.config)]
    assert main(["remote", "push", *config, "--archive", str(archive)]) == 0
    assert main(["remote", "pull", *config, "--into", str(into)]) == 0
    capsys.readouterr()
    resolve = ["-T", "--", DESTINATION, shlex.join(RESOLVE)]
    assert fake.calls() == [
        resolve,
        ["-T", "-e", "none", "--", DESTINATION, _exec_line("remote", "receive")],
        resolve,
        [
            "-T",
            "-e",
            "none",
            "--",
            DESTINATION,
            _exec_line("remote", "send-packs", flags=()),
        ],
    ]
    for argv in fake.calls():
        for word in argv:
            assert not any(name in word for name in HOSTILE_NAMES)
    for name in HOSTILE_NAMES:
        assert (fake.layout.archive / name).read_bytes() == b"synthetic"


def test_replace_and_a_docker_prefix_are_single_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch, docker=("sudo", "-n", "docker"))
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.pdf").write_bytes(b"synthetic")
    push = ["remote", "push", "--config", str(fake.config), "--inbox", str(inbox)]
    assert main([*push, "--replace"]) == 0
    capsys.readouterr()
    resolve, receive = fake.calls()
    assert resolve[-1] == shlex.join(["sudo", "-n", *RESOLVE])
    assert receive[-1] == shlex.join(
        ["sudo", "-n", "docker", "exec", "-i", "-e", ENV_WORD, CONTAINER, "finance"]
        + ["remote", "receive", "--replace"]
    )


@pytest.mark.parametrize("tty", [True, False])
def test_exec_asks_for_a_terminal_only_when_stdin_is_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    tty: bool,
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SSH_EXIT", "3")
    status = remote_cli.exec_command(
        str(fake.config), ["--", "ledger", "status"], isatty=lambda _fd: tty
    )
    assert status == 3
    assert capfd.readouterr().out == "fake-finance\n"
    flags = ("-i", "-t") if tty else ("-i",)
    assert fake.calls()[1] == [
        "-t" if tty else "-T",
        "--",
        DESTINATION,
        _exec_line("ledger", "status", flags=flags),
    ]


def test_hostile_exec_words_reach_finance_as_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    words = ["; rm -rf ~", "$(x)", "-oProxyCommand=x", "-", "a b"]
    assert remote_cli.exec_command(str(fake.config), ["--", *words]) == 0
    capfd.readouterr()
    argv = fake.calls()[1]
    assert argv[:3] == ["-T", "--", DESTINATION] and len(argv) == 4
    remote = shlex.split(argv[3])
    assert remote[remote.index("finance") + 1 :] == words

    fake.log.write_text("", encoding="utf-8")
    assert remote_cli.exec_command(str(fake.config), ["--", "a\nb"]) == 1
    assert remote_cli.exec_command(str(fake.config), ["--"]) == 1
    assert capfd.readouterr().err.count("refused. argument_refused.") == 2
    assert fake.calls() == []


@pytest.mark.parametrize(
    "words",
    [["--", "ledger", "status", "--json"], ["ledger", "status", "--json"]],
)
def test_exec_through_the_parser_hands_the_words_to_finance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    words: list[str],
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    assert main(["remote", "exec", "--config", str(fake.config), *words]) == 0
    assert capfd.readouterr().out == "fake-finance\n"
    argv = fake.calls()[1]
    assert argv[0] in ("-T", "-t") and argv[1:3] == ["--", DESTINATION]
    remote = shlex.split(argv[3])
    expected = words[1:] if words[:1] == ["--"] else words
    assert remote[remote.index("finance") + 1 :] == expected


@pytest.mark.parametrize("words", [[], ["--"]])
def test_exec_through_the_parser_refuses_no_words(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    words: list[str],
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    assert main(["remote", "exec", "--config", str(fake.config), *words]) == 1
    assert "finance remote exec: refused. argument_refused." in capfd.readouterr().err
    assert fake.calls() == []


def test_an_ok_reply_with_a_failing_exit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = ssh.parse_reply(b"OK 1 2 3\n", 0)
    assert (counts.new, counts.identical, counts.replaced) == (1, 2, 3)
    for status in (1, 2, 130):
        with pytest.raises(RemoteRefused) as caught:
            ssh.parse_reply(b"OK 1 2 3\n", status)
        assert caught.value.code == "remote_reply_invalid"
    with pytest.raises(RemoteRefused, match="ssh_failed"):
        ssh.parse_reply(b"OK 1 2 3\n", 255)

    fake = FakeRemote.install(tmp_path, monkeypatch)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.pdf").write_bytes(b"synthetic")
    monkeypatch.setenv("FAKE_SSH_FAULT", "reply:OK 1 0 0")
    monkeypatch.setenv("FAKE_SSH_EXIT", "1")
    push = ["remote", "push", "--config", str(fake.config), "--inbox", str(inbox)]
    assert main(push) == 1
    assert capsys.readouterr().err.strip() == (
        "finance remote push: refused. remote_reply_invalid."
    )


def test_a_push_reply_count_is_the_files_written_whatever_the_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.pdf").write_bytes(b"synthetic")
    push = ["remote", "push", "--config", str(fake.config), "--inbox", str(inbox)]
    monkeypatch.setenv("FAKE_SSH_EXIT", "1")
    for code in ("target_not_regular", "target_appeared", "io_failed"):
        monkeypatch.setenv("FAKE_SSH_FAULT", f"reply:NO {code} 2")
        assert main(push) == 1
        assert capsys.readouterr().err == (
            f"finance remote push: refused. {code} (2 files written before it).\n"
        )
    monkeypatch.setenv("FAKE_SSH_FAULT", "reply:NO target_not_regular 0")
    assert main(push) == 1
    assert capsys.readouterr().err == (
        "finance remote push: refused. target_not_regular. Nothing was written.\n"
    )


def test_a_failed_reply_read_never_says_nothing_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.pdf").write_bytes(b"synthetic")
    real_start = ssh._start

    class _ReadFails:
        """The receiver's reply arrives in full, then the local read fails."""

        def __init__(self, real: IO[bytes]) -> None:
            self._real = real

        def read(self, _size: int, /) -> bytes:
            self._real.read()
            self._real.close()
            raise OSError(errno.EIO, "synthetic")

    def start(argv: list[str], *, stdin: int) -> subprocess.Popen[bytes]:
        proc = real_start(argv, stdin=stdin)
        assert proc.stdout is not None
        monkeypatch.setattr(proc, "stdout", _ReadFails(proc.stdout))
        return proc

    monkeypatch.setattr(ssh, "_start", start)
    push = ["remote", "push", "--config", str(fake.config), "--inbox", str(inbox)]
    assert main(push) == 1
    assert capsys.readouterr().err == "finance remote push: refused. ssh_failed.\n"
    assert (fake.layout.inbox / "a.pdf").read_bytes() == b"synthetic"


def test_a_failed_stdin_close_after_a_full_send_never_says_nothing_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.pdf").write_bytes(b"synthetic")
    real_start = ssh._start

    class _CloseFails:
        """Writes go through; close delivers the end of stream, lets the receiver
        finish, then reports a failure that is not a broken pipe."""

        def __init__(self, proc: subprocess.Popen[bytes], real: IO[bytes]) -> None:
            self._proc = proc
            self._real = real

        def write(self, data: bytes, /) -> int:
            return self._real.write(data)

        def flush(self) -> None:
            self._real.flush()

        def close(self) -> None:
            self._real.close()
            self._proc.wait(timeout=10)
            raise OSError(errno.EIO, "synthetic")

    def start(argv: list[str], *, stdin: int) -> subprocess.Popen[bytes]:
        proc = real_start(argv, stdin=stdin)
        assert proc.stdin is not None
        monkeypatch.setattr(proc, "stdin", _CloseFails(proc, proc.stdin))
        return proc

    monkeypatch.setattr(ssh, "_start", start)
    push = ["remote", "push", "--config", str(fake.config), "--inbox", str(inbox)]
    assert main(push) == 1
    captured = capsys.readouterr()
    assert captured.err == "finance remote push: refused. ssh_failed.\n"
    assert "Nothing was written" not in captured.out + captured.err
    assert (fake.layout.inbox / "a.pdf").read_bytes() == b"synthetic"


def test_a_failed_final_flush_whose_bytes_close_delivers_is_outcome_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.pdf").write_bytes(b"synthetic")
    real_start = ssh._start
    flushes: list[int] = []

    class _FlushFails:
        """The flush after the end record fails once and keeps the bytes; close
        then delivers them and lets the receiver finish."""

        def __init__(self, proc: subprocess.Popen[bytes], real: IO[bytes]) -> None:
            self._proc = proc
            self._real = real

        def write(self, data: bytes, /) -> int:
            return self._real.write(data)

        def flush(self) -> None:
            flushes.append(1)
            if len(flushes) == 1:
                raise OSError(errno.EIO, "synthetic")
            self._real.flush()

        def close(self) -> None:
            self._real.close()
            self._proc.wait(timeout=10)

    def start(argv: list[str], *, stdin: int) -> subprocess.Popen[bytes]:
        proc = real_start(argv, stdin=stdin)
        assert proc.stdin is not None
        monkeypatch.setattr(proc, "stdin", _FlushFails(proc, proc.stdin))
        return proc

    monkeypatch.setattr(ssh, "_start", start)
    push = ["remote", "push", "--config", str(fake.config), "--inbox", str(inbox)]
    assert main(push) == 1
    captured = capsys.readouterr()
    assert captured.err == "finance remote push: refused. ssh_failed.\n"
    assert "Nothing was written" not in captured.out + captured.err
    assert flushes == [1]
    assert (fake.layout.inbox / "a.pdf").read_bytes() == b"synthetic"


def _cleanup_wait_fails(
    monkeypatch: pytest.MonkeyPatch, *, first_wait_ok: bool = False
) -> None:
    """The stream process looks alive to ``_stop``, and each wait (from the second,
    with ``first_wait_ok``) reaps it, then fails with ECHILD."""
    real_start = ssh._start

    def start(argv: list[str], *, stdin: int) -> subprocess.Popen[bytes]:
        proc = real_start(argv, stdin=stdin)
        real_wait = proc.wait
        calls: list[int] = []

        def wait(timeout: float | None = None) -> int:
            code = real_wait(timeout)
            calls.append(code)
            if first_wait_ok and len(calls) == 1:
                return code
            raise OSError(errno.ECHILD, "synthetic")

        monkeypatch.setattr(proc, "poll", lambda: None)
        monkeypatch.setattr(proc, "wait", wait)
        return proc

    monkeypatch.setattr(ssh, "_start", start)


def test_a_failed_cleanup_after_a_push_never_says_nothing_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.pdf").write_bytes(b"synthetic")
    _cleanup_wait_fails(monkeypatch)
    push = ["remote", "push", "--config", str(fake.config), "--inbox", str(inbox)]
    assert main(push) == 1
    assert capsys.readouterr().err == "finance remote push: refused. ssh_failed.\n"
    assert (fake.layout.inbox / "a.pdf").read_bytes() == b"synthetic"


def test_a_failed_cleanup_after_a_completed_push_keeps_its_known_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.pdf").write_bytes(b"synthetic")
    # push's own wait succeeds; only the one in _stop fails.
    _cleanup_wait_fails(monkeypatch, first_wait_ok=True)
    push = ["remote", "push", "--config", str(fake.config), "--inbox", str(inbox)]
    assert main(push) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        "finance remote push: 1 new, 0 identical, 0 replaced, 0 skipped.\n",
        "",
    )
    assert (fake.layout.inbox / "a.pdf").read_bytes() == b"synthetic"


def test_a_failed_cleanup_after_a_pull_keeps_its_known_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    packs = fake.layout.data / "packs"
    packs.mkdir()
    (packs / "pack-a.zip").write_bytes(b"synthetic pack a")
    (packs / "pack-b.zip").write_bytes(b"synthetic pack b")
    into = tmp_path / "into"
    into.mkdir()
    _cleanup_wait_fails(monkeypatch)
    pull = ["remote", "pull", "--config", str(fake.config), "--into", str(into)]
    assert main(pull) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        "finance remote pull: 2 new, 0 identical.\n",
        "",
    )
    assert (into / "pack-b.zip").read_bytes() == b"synthetic pack b"


def _config_cases(tmp_path: Path) -> dict[str, Path]:
    good = write_config(tmp_path / "good.toml", config_values())
    link = tmp_path / "link.toml"
    link.symlink_to(good)
    cases: dict[str, dict[str, object]] = {
        "an option as destination": config_values(destination="-oProxyCommand=x"),
        "a space in the project": config_values(compose_project="sample project"),
        "not a uuid": config_values(workspace_id="not-a-uuid"),
        "an unknown key": config_values(extra="x"),
        "a missing key": {"destination": DESTINATION},
        "an option as docker": config_values(docker=["-H", "docker"]),
        "a space in docker": config_values(docker=["docker x"]),
    }
    paths = {
        name: write_config(tmp_path / f"case{index}.toml", values)
        for index, (name, values) in enumerate(cases.items())
    }
    paths["readable by others"] = write_config(
        tmp_path / "loose.toml", config_values(), mode=0o644
    )
    paths["a link"] = link
    paths["missing"] = tmp_path / "absent.toml"
    return paths


def test_a_bad_config_is_refused_before_any_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.pdf").write_bytes(b"synthetic")
    for name, path in _config_cases(tmp_path).items():
        push = ["remote", "push", "--config", str(path), "--inbox", str(inbox)]
        assert main(push) == 1, name
        assert "refused. remote_config_refused." in capsys.readouterr().err, name
    assert main(["remote", "push", "--inbox", str(inbox)]) == 1
    assert "remote_config_not_set" in capsys.readouterr().err
    monkeypatch.setenv(ssh.CONFIG_VARIABLE, str(fake.config))
    assert main(["remote", "push", "--inbox", str(inbox)]) == 0
    capsys.readouterr()
    assert len(fake.calls()) == 2


@pytest.mark.parametrize(
    ("setting", "value", "code"),
    [
        ("FAKE_SSH_CONTAINERS", "", "container_not_found"),
        ("FAKE_SSH_CONTAINERS", f"{CONTAINER},deadbeefdead", "container_ambiguous"),
        ("FAKE_SSH_CONTAINERS", "not-hex-id-at-all", "remote_reply_invalid"),
        ("FAKE_SSH_FAULT", "ssh255", "ssh_failed"),
        (
            "FAKE_SSH_FAULT",
            "reply:NO target_differs 1 /x/secret",
            "remote_reply_invalid",
        ),
        ("FAKE_SSH_FAULT", "reply:NO some_word 1", "remote_reply_invalid"),
        ("FAKE_SSH_FAULT", "reply:OK 1 2", "remote_reply_invalid"),
        ("PATH", "", "ssh_not_found"),
    ],
)
def test_the_container_rule_and_the_reply_grammar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setting: str,
    value: str,
    code: str,
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.pdf").write_bytes(b"synthetic")
    if setting == "PATH":
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        value = str(empty)
    monkeypatch.setenv(setting, value)
    assert (
        main(["remote", "push", "--config", str(fake.config), "--inbox", str(inbox)])
        == 1
    )
    assert f"refused. {code}." in capsys.readouterr().err
    assert list(fake.layout.inbox.iterdir()) == []


def _scenarios(
    fake: FakeRemote, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Callable[[], int]]:
    config = ["--config", str(fake.config)]
    laptop = tmp_path / "laptop-sources"
    laptop.mkdir()
    (laptop / "distinctive-name-one.pdf").write_bytes(b"synthetic one")
    (fake.layout.archive / "distinctive-name-one.pdf").write_bytes(b"server copy")
    bad = tmp_path / "bad-names"
    bad.mkdir()
    (bad / "distinctive\nname-two.pdf").write_bytes(b"synthetic")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "distinctive-name-three.pdf").write_bytes(b"synthetic")
    (locked / "distinctive-name-three.pdf").chmod(0)
    loose = write_config(tmp_path / "loose.toml", config_values(), mode=0o644)
    packs = fake.layout.data / "packs"
    packs.mkdir()
    (packs / "distinctive-name-four.zip").write_bytes(b"server pack")
    into = tmp_path / "into"
    into.mkdir()
    (into / "distinctive-name-four.zip").write_bytes(b"local pack")

    def push(*flags: str, fault: str = "", setting: tuple[str, str] = ("", "")) -> int:
        monkeypatch.setenv("FAKE_SSH_FAULT", fault)
        monkeypatch.setenv("FAKE_SSH_CONTAINERS", CONTAINER)
        if setting[0]:
            monkeypatch.setenv(*setting)
        return main(["remote", "push", *flags])

    def pull(fault: str = "") -> int:
        monkeypatch.setenv("FAKE_SSH_FAULT", fault)
        return main(["remote", "pull", *config, "--into", str(into)])

    archive = [*config, "--archive", str(laptop)]
    return {
        "target_differs": lambda: push(*archive),
        "name_refused": lambda: push(*config, "--archive", str(bad)),
        "source_unreadable": lambda: push(*config, "--archive", str(locked)),
        "remote_config_refused": lambda: push(
            "--config", str(loose), "--archive", str(laptop)
        ),
        "ssh_failed": lambda: push(*archive, fault="ssh255"),
        "push stream ssh_failed": lambda: push(*archive, fault="ssh255:stream"),
        "pull stream ssh_failed": lambda: pull("ssh255:stream"),
        "remote_reply_invalid": lambda: push(
            *archive, fault="reply:NO target_differs 1 /x/secret"
        ),
        "container_ambiguous": lambda: push(
            *archive, setting=("FAKE_SSH_CONTAINERS", f"{CONTAINER},deadbeefdead")
        ),
        "remote_failed": lambda: push(*archive, fault="kill:20"),
        "pull target_differs": pull,
        "argument_refused": lambda: remote_cli.exec_command(
            str(fake.config), ["--", "distinctive\nname-five"]
        ),
    }


def test_every_refusal_is_a_fixed_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # capfd, not capsys: the ssh child writes to file descriptor 2 itself, and its
    # diagnostics (the fake names the destination there) must not reach either stream.
    fake = FakeRemote.install(
        tmp_path, monkeypatch, destination="ops@finance.example.test"
    )
    forbidden = [
        "ops@finance.example.test",
        "finance.example.test",
        PROJECT,
        CONTAINER,
        "deadbeefdead",
        WORKSPACE,
        str(fake.config),
        str(tmp_path),
        "/x/secret",
        "distinctive",
        "name-",
    ]
    try:
        for name, run in _scenarios(fake, tmp_path, monkeypatch).items():
            assert run() == 1, name
            captured = capfd.readouterr()
            text = captured.out + captured.err
            assert f"refused. {name.split()[-1]}" in text, name
            for word in forbidden:
                assert word not in text, (name, word)
    finally:
        (tmp_path / "locked" / "distinctive-name-three.pdf").chmod(0o600)


def test_the_laptop_half_loads_no_database_layer_or_core() -> None:
    probe = (
        "import sys\n"
        "import rheo_finance.cli.main as entry\n"
        "import rheo_finance.cli.remote, rheo_finance.remote.ssh\n"
        "import rheo_finance.remote.frames, rheo_finance.remote.receive\n"
        "import rheo_finance.remote.send\n"
        "status = entry.main(['remote', 'push', '--help'])\n"
        "roots = ('sqlalchemy', 'rheo_core')\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in roots"
        " or m.startswith(('rheo_finance.storage', 'rheo_finance.wiring'))]\n"
        "print(status, len(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines()[-1] == "0 0"


def test_the_workspace_variable_is_wirings() -> None:
    from rheo_finance.wiring import core

    assert ssh.WORKSPACE_VARIABLE == core.WORKSPACE_VARIABLE

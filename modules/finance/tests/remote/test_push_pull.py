"""Push and pull through the fake ``ssh`` (L7 to L10, L17): copy-only on both ends,
all-or-nothing on a refusal, a cut or a killed process.

No database, no network: ``support.FakeRemote`` runs the server half in a local
process against a layout under ``tmp_path``.
"""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from rheo_finance.cli import state as state_cli
from rheo_finance.cli.main import main
from rheo_finance.remote import send
from rheo_finance.remote.receive import LOCK_NAME, STAGING_NAME

from .support import FakeRemote, staged_leftovers, staging_entries, tree_state

WAIT_SECONDS = 10.0


def _laptop(root: Path) -> dict[str, Path]:
    """One source directory per push flag, with synthetic files."""
    archive = root / "archive"
    (archive / "folder").mkdir(parents=True)
    (archive / "folder" / "doc-one.pdf").write_bytes(b"synthetic one")
    (archive / "doc-two.pdf").write_bytes(b"synthetic two" * 9000)
    (archive / ".hidden.pdf").write_bytes(b"skipped")
    inbox = root / "inbox"
    inbox.mkdir()
    (inbox / "new-three.pdf").write_bytes(b"synthetic three")
    data = root / "data"
    (data / "manual").mkdir(parents=True)
    (data / "settings.toml").write_bytes(b"# synthetic settings\n")
    (data / "identities.toml").write_bytes(b"# synthetic identities\n")
    (data / "manual" / "rows.csv").write_bytes(b"header\n")
    (data / "db.env").write_bytes(b"FINANCE_OWNER_DSN=synthetic\n")
    (data / "dumps").mkdir()
    (data / "dumps" / "old.sql").write_bytes(b"synthetic")
    state = root / "export"
    state_cli.write_export(
        state,
        schema_head="head-synthetic",
        derived_digest="0" * 64,
        tables=[("decision", ('["synthetic"]',)), ("rate", ())],
    )
    keys = root / "keys"
    keys.mkdir()
    (keys / "statement.key").write_bytes(b"synthetic key")
    (keys / "statement.key").chmod(0o600)
    return {
        "archive": archive,
        "inbox": inbox,
        "data": data,
        "state": state,
        "keys": keys,
    }


def _flags(sources: dict[str, Path]) -> list[str]:
    return [part for name, path in sources.items() for part in (f"--{name}", str(path))]


def _final_names(root: Path) -> list[str]:
    return [row[0] for row in tree_state(root) if not row[0].startswith(".")]


def test_push_lands_every_area_and_leaves_the_laptop_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    sources = _laptop(tmp_path / "laptop")
    before = {name: tree_state(path) for name, path in sources.items()}

    assert main(["remote", "push", "--config", str(fake.config), *_flags(sources)]) == 0
    assert capsys.readouterr().out == (
        "finance remote push: 10 new, 0 identical, 0 replaced, 1 skipped.\n"
    )
    assert {name: tree_state(path) for name, path in sources.items()} == before
    layout = fake.layout
    assert (layout.archive / "doc-two.pdf").read_bytes() == b"synthetic two" * 9000
    assert (layout.inbox / "new-three.pdf").read_bytes() == b"synthetic three"
    assert (layout.data / "manual" / "rows.csv").read_bytes() == b"header\n"
    assert (layout.data / "transfer" / "manifest.json").is_file()
    assert (layout.data / "transfer" / "state" / "rate.jsonl").read_bytes() == b""
    assert (layout.keys / "statement.key").stat().st_mode & 0o777 == 0o600
    assert not (layout.data / "db.env").exists()
    assert not (layout.data / "dumps").exists()
    assert not (layout.archive / ".hidden.pdf").exists()
    assert state_cli.read_export(layout.data / "transfer")[0] == "head-synthetic"
    assert staging_entries(layout.root) == [LOCK_NAME]

    assert main(["remote", "push", "--config", str(fake.config), *_flags(sources)]) == 0
    assert "0 new, 10 identical, 0 replaced" in capsys.readouterr().out


def test_replace_takes_the_edited_settings_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    sources = _laptop(tmp_path / "laptop")
    (fake.layout.data / "settings.toml").write_bytes(b"# template\n")
    (fake.layout.archive / "doc-two.pdf").write_bytes(b"server copy")
    data_only = ["--config", str(fake.config), "--data", str(sources["data"])]

    assert main(["remote", "push", *data_only]) == 1
    assert capsys.readouterr().err == (
        "finance remote push: refused. target_differs (1 file). Nothing was written.\n"
    )
    assert main(["remote", "push", *data_only, "--replace"]) == 0
    assert "2 new, 0 identical, 1 replaced" in capsys.readouterr().out
    assert (
        fake.layout.data / "settings.toml"
    ).read_bytes() == b"# synthetic settings\n"

    archive = ["--archive", str(sources["archive"]), "--replace"]
    assert main(["remote", "push", "--config", str(fake.config), *archive]) == 1
    assert "target_differs (1 file)" in capsys.readouterr().err
    assert (fake.layout.archive / "doc-two.pdf").read_bytes() == b"server copy"


def _packs(fake: FakeRemote) -> Path:
    packs = fake.layout.data / "packs"
    (packs / "folder").mkdir(parents=True)
    (packs / "folder" / "pack-a.zip").write_bytes(b"synthetic pack a" * 5000)
    (packs / "pack-b.zip").write_bytes(b"synthetic pack b")
    return packs


def test_pull_copies_the_packs_and_refuses_a_differing_local_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    packs = _packs(fake)
    before = tree_state(packs)
    into = tmp_path / "into"
    into.mkdir()
    pull = ["remote", "pull", "--config", str(fake.config), "--into", str(into)]

    assert main(pull) == 0
    assert capsys.readouterr().out == "finance remote pull: 2 new, 0 identical.\n"
    assert tree_state(packs) == before
    assert (into / "folder" / "pack-a.zip").read_bytes() == b"synthetic pack a" * 5000
    assert main(pull) == 0
    assert "0 new, 2 identical" in capsys.readouterr().out

    (into / "pack-b.zip").write_bytes(b"local edit")
    (packs / "pack-c.zip").write_bytes(b"synthetic pack c")
    assert main(pull) == 1
    assert capsys.readouterr().err == (
        "finance remote pull: refused. target_differs (1 file). Nothing was written.\n"
    )
    assert not (into / "pack-c.zip").exists()
    assert (into / "pack-b.zip").read_bytes() == b"local edit"
    assert staging_entries(into) == [LOCK_NAME]


def test_pull_refuses_without_packs_or_a_real_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    into = tmp_path / "into"
    into.mkdir()
    assert (
        main(["remote", "pull", "--config", str(fake.config), "--into", str(into)]) == 1
    )
    assert "refused. packs_missing. Nothing was written." in capsys.readouterr().err
    link = tmp_path / "link"
    link.symlink_to(into)
    for target in (link, tmp_path / "absent"):
        calls = len(fake.calls())
        assert (
            main(
                ["remote", "pull", "--config", str(fake.config), "--into", str(target)]
            )
            == 1
        )
        assert "into_refused" in capsys.readouterr().err
        assert len(fake.calls()) == calls


def _cut_point(items: tuple[send.Outgoing, ...]) -> int:
    """A byte offset in the middle of the second file's payload."""
    stream = io.BytesIO()
    send.send(stream, items)
    data = stream.getvalue()
    second = data.index(b"\nF ", data.index(b"\nF ") + 1) + 1
    return data.index(b"\n", second) + 1 + 5


def test_a_cut_push_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    sources = _laptop(tmp_path / "laptop")
    archive = sources["archive"]
    cut = _cut_point(send.collect(send.Sources(archive=archive)).items)
    monkeypatch.setenv("FAKE_SSH_FAULT", f"cut:{cut}")
    assert (
        main(
            ["remote", "push", "--config", str(fake.config), "--archive", str(archive)]
        )
        == 1
    )
    assert "refused. stream_truncated. Nothing was written." in capsys.readouterr().err
    assert _final_names(fake.layout.archive) == []
    assert staging_entries(fake.layout.root) == [LOCK_NAME]


def test_a_cut_pull_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    packs = _packs(fake)
    walker_items = send._Walker("pull")
    walker_items.tree(packs, "packs")
    items = tuple(sorted(walker_items.items, key=lambda item: item.path.encode()))
    monkeypatch.setenv("FAKE_SSH_FAULT", f"cut:{_cut_point(items)}")
    into = tmp_path / "into"
    into.mkdir()
    assert (
        main(["remote", "pull", "--config", str(fake.config), "--into", str(into)]) == 1
    )
    assert "refused. ssh_failed." in capsys.readouterr().err
    assert _final_names(into) == []
    assert staging_entries(into) == [LOCK_NAME]


def test_a_killed_receiver_leaves_only_staging_and_the_next_push_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    archive = _laptop(tmp_path / "laptop")["archive"]
    cut = _cut_point(send.collect(send.Sources(archive=archive)).items)
    push = ["remote", "push", "--config", str(fake.config), "--archive", str(archive)]
    monkeypatch.setenv("FAKE_SSH_FAULT", f"kill:{cut}")
    assert main(push) == 1
    assert capsys.readouterr().err == "finance remote push: refused. remote_failed.\n"
    assert _final_names(fake.layout.archive) == []
    leftovers = staged_leftovers(fake.layout.root)
    assert [name.rpartition("/")[2] for name in leftovers] == ["0", "1"]

    monkeypatch.setenv("FAKE_SSH_FAULT", "")
    assert main(push) == 0
    assert "2 new" in capsys.readouterr().out
    assert staged_leftovers(fake.layout.root) == []
    assert staging_entries(fake.layout.root) == [LOCK_NAME]


def test_a_killed_laptop_receiver_leaves_only_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    _packs(fake)
    into = tmp_path / "into"
    into.mkdir()
    pull = ["remote", "pull", "--config", str(fake.config), "--into", str(into)]
    environment = {**os.environ, "FAKE_SSH_FAULT": "stall:1500"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "rheo_finance.cli.main", *pull],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Three waits of at most WAIT_SECONDS each, one per process start (the laptop's,
    # then each fake ssh's), so a slow interpreter start is not a failure.
    steps: list[tuple[str, Callable[[], bool]]] = [
        ("the resolve call", lambda: len(fake.calls()) >= 1),
        ("the stream call", lambda: len(fake.calls()) >= 2),
        ("a staged file", lambda: bool(staged_leftovers(into))),
    ]
    try:
        for what, reached in steps:
            deadline = time.monotonic() + WAIT_SECONDS
            while not reached():
                assert proc.poll() is None, f"the pull ended before {what}"
                assert time.monotonic() < deadline, f"no {what} within the wait"
                time.sleep(0.05)
    finally:
        # The laptop process and the fake ssh under it: nothing is left running.
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=WAIT_SECONDS)
    assert _final_names(into) == []
    assert staged_leftovers(into) != []

    assert main(pull) == 0
    assert "2 new" in capsys.readouterr().out
    assert staged_leftovers(into) == []
    assert sorted(os.listdir(into)) == [STAGING_NAME, "folder", "pack-b.zip"]


def test_keys_go_only_with_their_flag_and_only_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRemote.install(tmp_path, monkeypatch)
    sources = _laptop(tmp_path / "laptop")
    keys = sources.pop("keys")
    assert main(["remote", "push", "--config", str(fake.config), *_flags(sources)]) == 0
    capsys.readouterr()
    assert list(fake.layout.keys.iterdir()) == []

    (keys / "loose.key").write_bytes(b"synthetic")
    (keys / "loose.key").chmod(0o644)
    calls = len(fake.calls())
    push_keys = ["remote", "push", "--config", str(fake.config), "--keys", str(keys)]
    assert main(push_keys) == 1
    assert "refused. key_mode_refused. Nothing was written." in capsys.readouterr().err
    assert len(fake.calls()) == calls
    assert list(fake.layout.keys.iterdir()) == []

    (keys / "loose.key").chmod(0o600)
    assert main(push_keys) == 0
    assert "2 new" in capsys.readouterr().out
    for name in ("loose.key", "statement.key"):
        assert (fake.layout.keys / name).stat().st_mode & 0o777 == 0o600

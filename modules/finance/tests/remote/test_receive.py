"""The receiver (L3 to L6): new files land private, identical ones are skipped, a
differing target refuses the whole stream, a file that appears during the commit is
never clobbered, and no link is followed.

No database, no network: streams are bytes in memory.
"""

from __future__ import annotations

import fcntl
import io
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from rheo_finance.cli import remote as remote_cli
from rheo_finance.remote import send
from rheo_finance.remote.frames import Direction, RemoteRefused
from rheo_finance.remote.receive import (
    LOCK_NAME,
    STAGING_NAME,
    Replaceable,
    never,
    owner_inputs,
    receive,
)

from .support import (
    frame,
    make_layout,
    push_areas,
    staging_entries,
    tree_state,
)

FILES = [
    ("archive/folder/a.pdf", b"first synthetic document"),
    ("archive/folder/sub/b.pdf", b"second synthetic document"),
    ("data/manual/c.csv", b"header\n"),
    ("inbox/d.pdf", b"fourth synthetic document"),
    ("keys/k.key", b"synthetic key"),
]


def _push(
    layout_root: Path,
    areas: dict[str, Path],
    files: list[tuple[str, bytes]],
    *,
    replaceable: Replaceable = never,
    before_commit: Callable[[], None] | None = None,
) -> tuple[int, int, int]:
    counts = receive(
        io.BytesIO(frame(files)),
        areas,
        layout_root,
        direction="push",
        replaceable=replaceable,
        before_commit=before_commit,
    )
    return counts.new, counts.identical, counts.replaced


def test_new_files_land_private_and_a_second_stream_is_identical(
    tmp_path: Path,
) -> None:
    layout = make_layout(tmp_path / "uploads")
    areas = push_areas(layout)
    assert _push(layout.root, areas, FILES) == (5, 0, 0)
    for name, body in FILES:
        target = layout.root / name
        assert target.read_bytes() == body
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    for made in ("archive/folder", "archive/folder/sub", "data/manual"):
        assert stat.S_IMODE((layout.root / made).stat().st_mode) == 0o700
    first = layout.archive / "folder" / "a.pdf"
    before = (first.stat().st_ino, first.stat().st_mtime_ns)
    snapshot = [row for row in tree_state(layout.root) if row[0][0] != "."]

    assert _push(layout.root, areas, FILES) == (0, 5, 0)
    assert (first.stat().st_ino, first.stat().st_mtime_ns) == before
    assert [row for row in tree_state(layout.root) if row[0][0] != "."] == snapshot
    assert staging_entries(layout.root) == [LOCK_NAME]


def test_a_differing_target_refuses_the_whole_stream(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / "uploads")
    (layout.archive / "b-old.pdf").write_bytes(b"owner bytes")
    before = tree_state(layout.root)
    files = [("archive/a-new.pdf", b"new"), ("archive/b-old.pdf", b"changed")]
    with pytest.raises(RemoteRefused) as caught:
        _push(layout.root, push_areas(layout), files)
    assert (str(caught.value), caught.value.files) == ("target_differs", 1)
    assert (layout.archive / "b-old.pdf").read_bytes() == b"owner bytes"
    assert not (layout.archive / "a-new.pdf").exists()
    after = [row for row in tree_state(layout.root) if not row[0].startswith(".")]
    assert after == before


def test_replace_takes_the_owner_inputs_and_nothing_else(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / "uploads")
    (layout.data / "settings.toml").write_bytes(b"# template\n")
    (layout.data / "manual").mkdir()
    (layout.data / "manual" / "m.csv").write_bytes(b"header\n")
    files = [
        ("data/manual/m.csv", b"header\nrow\n"),
        ("data/settings.toml", b"# owner settings\n"),
    ]
    assert _push(layout.root, push_areas(layout), files, replaceable=owner_inputs) == (
        0,
        0,
        2,
    )
    assert (layout.data / "settings.toml").read_bytes() == b"# owner settings\n"
    assert stat.S_IMODE((layout.data / "settings.toml").stat().st_mode) == 0o600

    for name in ("archive/x.pdf", "inbox/x.pdf", "keys/x.key", "data/transfer/x"):
        target = layout.root / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b"kept")
        with pytest.raises(RemoteRefused, match="target_differs"):
            _push(
                layout.root,
                push_areas(layout),
                [(name, b"other")],
                replaceable=owner_inputs,
            )
        assert target.read_bytes() == b"kept"


def test_replace_renames_in_the_opened_parent_not_through_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_layout(tmp_path / "uploads")
    manual = layout.data / "manual"
    manual.mkdir()
    (manual / "m.csv").write_bytes(b"header\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = layout.data / "manual-moved"
    real_replace = os.replace
    keywords: list[list[str]] = []

    def swap_then_replace(
        src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: int
    ) -> None:
        # After the parents were walked: data/manual becomes a link out of the area.
        keywords.append(sorted(kwargs))
        manual.rename(moved)
        manual.symlink_to(outside)
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", swap_then_replace)
    files = [("data/manual/m.csv", b"header\nrow\n")]
    assert _push(layout.root, push_areas(layout), files, replaceable=owner_inputs) == (
        0,
        0,
        1,
    )
    assert keywords == [["dst_dir_fd", "src_dir_fd"]]
    assert list(outside.iterdir()) == []
    assert (moved / "m.csv").read_bytes() == b"header\nrow\n"


def _folds_case(directory: Path) -> bool:
    probe = directory / "case-probe"
    probe.write_bytes(b"")
    try:
        return (directory / "CASE-PROBE").exists()
    finally:
        probe.unlink()


def test_a_case_only_name_clash_on_pull_keeps_the_first_file_and_counts_it(
    tmp_path: Path,
) -> None:
    into = tmp_path / "into"
    into.mkdir()
    files = [
        ("packs/Pack.zip", b"first synthetic pack"),
        ("packs/pack.zip", b"second synthetic pack"),
    ]
    folds = _folds_case(into)

    def clash() -> None:
        # A case-sensitive file system: plant what a folding one meets at commit.
        (into / "pack.zip").write_bytes(b"other bytes")

    with pytest.raises(RemoteRefused) as caught:
        receive(
            io.BytesIO(frame(files)),
            {"packs": into},
            into,
            direction="pull",
            before_commit=None if folds else clash,
        )
    assert (caught.value.code, caught.value.files) == ("target_appeared", 1)
    assert remote_cli.refusal_line("finance remote pull", caught.value) == (
        "finance remote pull: refused. target_appeared (1 file written before it)."
    )
    assert (into / "Pack.zip").read_bytes() == b"first synthetic pack"
    if not folds:
        assert (into / "pack.zip").read_bytes() == b"other bytes"
    assert staging_entries(into) == [LOCK_NAME]


def _plant_directory(target: Path, _outside: Path) -> None:
    target.mkdir()


def _plant_link(target: Path, outside: Path) -> None:
    target.symlink_to(outside)


@pytest.mark.parametrize("direction", ["push", "pull"])
@pytest.mark.parametrize(
    "plant", [_plant_directory, _plant_link], ids=["directory", "link"]
)
def test_a_non_regular_entry_met_at_commit_keeps_the_first_file_and_counts_it(
    tmp_path: Path, direction: Direction, plant: Callable[[Path, Path], None]
) -> None:
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside bytes")
    if direction == "pull":
        area = tmp_path / "into"
        area.mkdir()
        areas, root, prefix = {"packs": area}, area, "packs"
    else:
        layout = make_layout(tmp_path / "uploads")
        areas, root, prefix = push_areas(layout), layout.root, "archive"
        area = layout.archive
    files = [
        (f"{prefix}/first.pdf", b"first synthetic file"),
        (f"{prefix}/second.pdf", b"second synthetic file"),
    ]
    with pytest.raises(RemoteRefused) as caught:
        receive(
            io.BytesIO(frame(files)),
            areas,
            root,
            direction=direction,
            # After every target was checked: the second becomes a non-regular entry.
            before_commit=lambda: plant(area / "second.pdf", outside),
        )
    assert (caught.value.code, caught.value.files) == ("target_not_regular", 1)
    label = f"finance remote {direction}"
    assert remote_cli.refusal_line(label, caught.value) == (
        f"{label}: refused. target_not_regular (1 file written before it)."
    )
    assert (area / "first.pdf").read_bytes() == b"first synthetic file"
    assert outside.read_bytes() == b"outside bytes"
    assert staging_entries(root) == [LOCK_NAME]


def test_without_replace_an_owner_input_is_refused(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / "uploads")
    (layout.data / "settings.toml").write_bytes(b"# template\n")
    with pytest.raises(RemoteRefused, match="target_differs"):
        _push(
            layout.root,
            push_areas(layout),
            [("data/settings.toml", b"x")],
            replaceable=never,
        )
    assert (layout.data / "settings.toml").read_bytes() == b"# template\n"


def test_a_target_that_appears_before_the_commit_is_never_clobbered(
    tmp_path: Path,
) -> None:
    layout = make_layout(tmp_path / "uploads")
    target = layout.archive / "race.pdf"

    def plant_other() -> None:
        target.write_bytes(b"planted")

    with pytest.raises(RemoteRefused) as caught:
        _push(
            layout.root,
            push_areas(layout),
            [("archive/race.pdf", b"pushed")],
            before_commit=plant_other,
        )
    assert (str(caught.value), caught.value.files) == ("target_appeared", 0)
    assert target.read_bytes() == b"planted"

    target.unlink()

    def plant_same() -> None:
        target.write_bytes(b"pushed")

    assert _push(
        layout.root,
        push_areas(layout),
        [("archive/race.pdf", b"pushed")],
        before_commit=plant_same,
    ) == (0, 1, 0)
    assert staging_entries(layout.root) == [LOCK_NAME]


def test_no_link_is_followed(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / "uploads")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.pdf").write_bytes(b"outside bytes")
    (layout.archive / "real").mkdir()
    (layout.archive / "sub").symlink_to(outside)
    # A link that stays inside the area is still a link: refused all the same.
    (layout.archive / "inner").symlink_to(layout.archive / "real")
    (layout.archive / "t.pdf").symlink_to(outside / "f.pdf")
    before = tree_state(outside)
    cases = {
        "archive/sub/f.pdf": "target_outside_layout",
        "archive/sub/new.pdf": "target_outside_layout",
        "archive/inner/new.pdf": "target_outside_layout",
        "archive/t.pdf": "target_not_regular",
    }
    for name, code in cases.items():
        with pytest.raises(RemoteRefused) as caught:
            _push(layout.root, push_areas(layout), [(name, b"pushed")])
        assert str(caught.value) == code, name
    assert tree_state(outside) == before
    assert list((layout.archive / "real").iterdir()) == []


def test_a_held_lock_is_staging_busy_and_leftovers_are_removed(
    tmp_path: Path,
) -> None:
    layout = make_layout(tmp_path / "uploads")
    staging = layout.root / STAGING_NAME
    staging.mkdir(mode=0o700)
    held = os.open(staging / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(RemoteRefused, match="staging_busy"):
            _push(layout.root, push_areas(layout), [("archive/a.pdf", b"x")])
        assert not (layout.archive / "a.pdf").exists()
    finally:
        os.close(held)

    dead = staging / "run-0123456789abcdef"
    dead.mkdir()
    (dead / "0").write_bytes(b"half a file")
    (dead / "inner").mkdir()
    (staging / "stray").write_bytes(b"x")
    assert _push(layout.root, push_areas(layout), [("archive/a.pdf", b"x")]) == (
        1,
        0,
        0,
    )
    assert staging_entries(layout.root) == [LOCK_NAME]


def test_a_collected_tree_round_trips_and_its_source_is_untouched(
    tmp_path: Path,
) -> None:
    source = tmp_path / "laptop" / "archive"
    (source / "folder").mkdir(parents=True)
    (source / "folder" / "a.pdf").write_bytes(b"synthetic a")
    (source / "-oProxyCommand=x.pdf").write_bytes(b"synthetic b")
    (source / ".hidden").write_bytes(b"skipped")
    (source / "link.pdf").symlink_to(source / "folder" / "a.pdf")
    before = tree_state(source)
    collected = send.collect(send.Sources(archive=source))
    assert collected.skipped == 2
    stream = io.BytesIO()
    send.send(stream, collected.items)
    assert tree_state(source) == before
    layout = make_layout(tmp_path / "uploads")
    stream.seek(0)
    counts = receive(stream, push_areas(layout), layout.root, direction="push")
    assert (counts.new, counts.identical) == (2, 0)
    assert (layout.archive / "-oProxyCommand=x.pdf").read_bytes() == b"synthetic b"
    assert not (layout.archive / ".hidden").exists()


def test_a_staging_directory_that_is_a_link_is_refused(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / "uploads")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (layout.root / STAGING_NAME).symlink_to(elsewhere)
    with pytest.raises(RemoteRefused, match="staging_refused"):
        _push(layout.root, push_areas(layout), [("archive/a.pdf", b"x")])
    assert list(elsewhere.iterdir()) == []

"""The workspace layout and private files (L9): pure paths, a refused missing layout,
settings forced or refused under a bound layout, and ``read_private_file``'s modes.

No database: every test works under ``tmp_path``.
"""

from __future__ import annotations

import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from rheo_finance import db
from rheo_finance.init_data import init_data
from rheo_finance.settings import SettingsError, load_settings


@contextmanager
def bound(layout: db.FinanceLayout) -> Iterator[db.FinanceLayout]:
    token = db.use_layout(layout)
    try:
        yield layout
    finally:
        db.release_layout(token)


def made(uploads: Path) -> db.FinanceLayout:
    layout = db.layout_under(uploads)
    for directory in layout.directories:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return layout


def test_layout_under_names_the_four_directories_and_touches_nothing(
    tmp_path: Path,
) -> None:
    layout = db.layout_under(tmp_path / "uploads")
    root = tmp_path / "uploads" / "finance"
    assert layout == db.FinanceLayout(
        root=root,
        data=root / "data",
        archive=root / "archive",
        inbox=root / "inbox",
        keys=root / "keys",
    )
    assert not (tmp_path / "uploads").exists()


def test_a_missing_layout_directory_is_refused_and_not_created(tmp_path: Path) -> None:
    layout = db.layout_under(tmp_path)
    with pytest.raises(db.LayoutRefused, match="finance_layout_missing"):
        db.check_layout(layout)
    assert not layout.root.exists()
    made(tmp_path)
    layout.archive.rmdir()
    with pytest.raises(db.LayoutRefused, match="finance_layout_missing"):
        db.check_layout(layout)
    assert not layout.archive.exists()


def test_a_symlinked_layout_directory_is_refused(tmp_path: Path) -> None:
    layout = made(tmp_path / "uploads")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    layout.archive.rmdir()
    layout.archive.symlink_to(elsewhere)
    with pytest.raises(db.LayoutRefused, match="finance_layout_missing"):
        db.check_layout(layout)


def test_a_bound_layout_is_the_data_directory_and_released_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path / "unbound"))
    layout = made(tmp_path / "uploads")
    with bound(layout):
        assert db.data_dir() == layout.data
        assert db.check_layout(layout) == layout
    assert db.data_dir() == tmp_path / "unbound"


def test_settings_under_a_bound_layout_follow_it(tmp_path: Path) -> None:
    layout = made(tmp_path / "uploads")
    with bound(layout):
        settings = load_settings()
    assert settings.get_path("finance.paths.archive") == layout.archive
    assert settings.get_path("finance.paths.downloads") == layout.inbox


@pytest.mark.parametrize("key", ["archive", "downloads"])
def test_a_settings_file_may_not_move_a_layout_path(tmp_path: Path, key: str) -> None:
    layout = made(tmp_path / "uploads")
    (layout.data / "settings.toml").write_text(
        f'[finance.paths]\n{key} = "{tmp_path / "other"}"\n', encoding="utf-8"
    )
    with bound(layout), pytest.raises(SettingsError, match="fixed by the workspace"):
        load_settings()


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("finance.reminders", "current_credential_file"),
        ("finance.mail", "credential_file"),
    ],
)
def test_a_credential_file_outside_keys_is_refused(
    tmp_path: Path, section: str, key: str
) -> None:
    layout = made(tmp_path / "uploads")
    settings_file = layout.data / "settings.toml"
    for refused in (str(layout.data / "token.env"), "../data/token.env"):
        settings_file.write_text(
            f'[{section}]\n{key} = "{refused}"\n', encoding="utf-8"
        )
        with bound(layout), pytest.raises(SettingsError, match="keys directory"):
            load_settings()
    settings_file.write_text(f'[{section}]\n{key} = "token.env"\n', encoding="utf-8")
    with bound(layout):
        settings = load_settings()
    assert settings.get_path(f"{section}.{key}") == layout.keys / "token.env"


def test_no_layout_leaves_the_settings_file_in_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    archive = tmp_path / "archive-here"
    (tmp_path / "settings.toml").write_text(
        f'[finance.paths]\narchive = "{archive}"\n', encoding="utf-8"
    )
    assert load_settings().get_path("finance.paths.archive") == archive


def test_init_data_creates_the_layout_directories_private(tmp_path: Path) -> None:
    layout = db.layout_under(tmp_path / "uploads")
    with bound(layout):
        result = init_data()
    assert result.data_dir == layout.data
    for directory in layout.directories:
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert db.check_layout(layout) == layout


def _private(path: Path, mode: int) -> Path:
    path.write_text("SYNTHETIC_KEY=value\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_read_private_file_accepts_an_owner_only_file(tmp_path: Path) -> None:
    path = _private(tmp_path / "token.env", 0o600)
    assert db.read_private_file(path) == "SYNTHETIC_KEY=value\n"
    assert db.read_private_file(path, within=tmp_path) == "SYNTHETIC_KEY=value\n"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_read_private_file_refuses_a_file_others_can_read(
    tmp_path: Path, mode: int
) -> None:
    path = _private(tmp_path / "token.env", mode)
    with pytest.raises(db.LayoutRefused, match="finance_private_file_refused"):
        db.read_private_file(path)


def test_read_private_file_refuses_a_symlink(tmp_path: Path) -> None:
    target = _private(tmp_path / "token.env", 0o600)
    link = tmp_path / "link.env"
    link.symlink_to(target)
    with pytest.raises(db.LayoutRefused, match="finance_private_file_refused"):
        db.read_private_file(link)


def test_read_private_file_refuses_a_file_outside_within(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    keys.mkdir()
    path = _private(tmp_path / "token.env", 0o600)
    with pytest.raises(db.LayoutRefused, match="finance_private_file_refused"):
        db.read_private_file(path, within=keys)


def test_read_private_file_refuses_a_directory_and_a_missing_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(db.LayoutRefused):
        db.read_private_file(tmp_path)
    with pytest.raises(db.LayoutRefused):
        db.read_private_file(tmp_path / "absent.env")

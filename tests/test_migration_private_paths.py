"""Pure-function tests for ``rheo_recallatron.migration.private_paths`` (FR 18, AC 17).

Seams: :func:`require_private_output` (the allowlist-of-one-root guard) and
:func:`require_loopback_dsn` (the DSN-host guard), both driven over temp trees —
no postgres needed. See the module's own docstring for why the guard is an
allowlist of one root rather than an ignore check.
"""

import subprocess
from pathlib import Path

import pytest
from rheo_recallatron.migration import private_paths
from rheo_recallatron.migration.private_paths import (
    PrivateOutputRefusal,
    require_loopback_dsn,
    require_private_output,
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _git_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    return repo


# --- the private root --------------------------------------------------------------


def test_unset_root_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(private_paths.PRIVATE_ROOT_VARIABLE, raising=False)
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output("/tmp/anything")
    assert excinfo.value.state == private_paths.PRIVATE_ROOT_UNSET


def test_a_relative_root_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, "relative/path")
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output("out.txt")
    assert excinfo.value.state == private_paths.PRIVATE_ROOT_NOT_ABSOLUTE


def test_a_missing_root_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        private_paths.PRIVATE_ROOT_VARIABLE, str(tmp_path / "does-not-exist")
    )
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output("out.txt")
    assert excinfo.value.state == private_paths.PRIVATE_ROOT_MISSING


def test_a_root_inside_the_checkout_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(private_paths, "find_checkout_root", lambda: checkout)
    root = checkout / "private"
    root.mkdir()
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output(root / "out.txt")
    assert excinfo.value.state == private_paths.PRIVATE_ROOT_OVERLAPS_CHECKOUT


def test_a_root_containing_the_checkout_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(private_paths, "find_checkout_root", lambda: checkout)
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output(root / "out.txt")
    assert excinfo.value.state == private_paths.PRIVATE_ROOT_OVERLAPS_CHECKOUT


def test_a_root_inside_an_unignored_git_work_tree_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(private_paths, "find_checkout_root", lambda: checkout)
    repo = _git_repo(tmp_path, "some-other-repo")
    root = repo / "private"
    root.mkdir()
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output(root / "out.txt")
    assert excinfo.value.state == private_paths.PRIVATE_ROOT_NOT_IGNORED


def test_a_root_inside_an_ignored_git_work_tree_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(private_paths, "find_checkout_root", lambda: checkout)
    repo = _git_repo(tmp_path, "some-other-repo")
    root = repo / "private"
    root.mkdir()
    (repo / ".gitignore").write_text("private/\n")
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))

    output = root / "sub" / "out.txt"
    result = require_private_output(output)

    assert result == output.resolve()
    assert (root / "sub").is_dir()


@pytest.fixture
def ignored_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A private root that passes every check up to the output-path rules."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(private_paths, "find_checkout_root", lambda: checkout)
    repo = _git_repo(tmp_path, "some-other-repo")
    root = repo / "private"
    root.mkdir()
    (repo / ".gitignore").write_text("private/\n")
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    return root


def test_a_path_escaping_the_root_by_dotdot_is_refused(ignored_root: Path) -> None:
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output(ignored_root / ".." / "escaped.txt")
    assert excinfo.value.state == private_paths.OUTPUT_ESCAPES_ROOT


def test_a_path_escaping_by_a_symlinked_parent_is_refused(
    ignored_root: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = ignored_root / "link"
    link.symlink_to(elsewhere)
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output(link / "escaped.txt")
    assert excinfo.value.state == private_paths.OUTPUT_ESCAPES_ROOT


def test_a_symlink_target_itself_is_refused(ignored_root: Path) -> None:
    real_file = ignored_root / "real.txt"
    real_file.write_text("x")
    link = ignored_root / "alias.txt"
    link.symlink_to(real_file)
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output(link)
    assert excinfo.value.state == private_paths.OUTPUT_IS_SYMLINK


def test_a_clean_path_inside_the_root_is_created_and_returned(
    ignored_root: Path,
) -> None:
    output = ignored_root / "reports" / "out.txt"
    result = require_private_output(output)
    assert result == output.resolve()
    assert output.parent.is_dir()
    assert not output.exists()  # the file itself is never created, only its parent


# --- the loopback DSN guard ----------------------------------------------------------


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://rheo:rheo_dev_only@localhost:5433/postgres",
        "postgresql://rheo:rheo_dev_only@127.0.0.1:5433/postgres",
        "postgresql://rheo:rheo_dev_only@[::1]:5433/postgres",
    ],
)
def test_require_loopback_dsn_accepts_every_loopback_spelling(dsn: str) -> None:
    assert require_loopback_dsn(dsn) == dsn


def test_require_loopback_dsn_refuses_a_non_loopback_host() -> None:
    dsn = "postgresql://rheo:rheo_dev_only@db.example.com:5433/postgres"
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_loopback_dsn(dsn)
    assert excinfo.value.state == private_paths.DSN_NOT_LOOPBACK

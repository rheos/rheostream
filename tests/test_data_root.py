"""The data root (B17): resolution order, validation, and the closed purpose set.

Seams: ``resolve_data_root()`` (``RHEO_DATA_ROOT`` wins; with it unset and
``RHEO_IN_CONTAINER=1`` the result is ``/var/lib/rheo-stream``, the branch-order case
that fails outright if the platform directory is checked first; with both unset the
platform directory), ``validate_data_root()`` (``<checkout>/.rheo-local`` only when
named explicitly, any other in-checkout path refused, a symlink escaping its parent
refused, wide permissions warned), ``workspace_dir_for()`` over the closed
``Purpose`` enum, and ``model_cache_dir()`` (one segment under ``models/``, and the
same object ``rheo_core.modules`` publishes to modules). The
``workspace_dir(ctx, purpose)`` wrapper is not driven: no test may hand-build a
``WorkspaceContext``, and its two lines read ``ctx.workspace_id`` and call the function
under test.
"""

import inspect
import logging
import os
import sys
from pathlib import Path
from uuid import UUID

import pytest
from rheo_core.storage.data_root import (
    CONTAINER_DATA_ROOT,
    DataRootRefusal,
    DataRootSource,
    Purpose,
    model_cache_dir,
    platform_data_dir,
    resolve_data_root,
    run_dir_for,
    validate_data_root,
    workspace_dir,
    workspace_dir_for,
)

WORKSPACE = UUID("018f0000-0000-7000-8000-000000000001")
OPERATION = UUID("018f0000-0000-7000-8000-000000000003")


# --- resolution ----------------------------------------------------------------------


def test_explicit_data_root_wins_over_everything() -> None:
    resolution = resolve_data_root(
        {"RHEO_DATA_ROOT": "/srv/rheo", "RHEO_IN_CONTAINER": "1"}
    )
    assert resolution.path == Path("/srv/rheo")
    assert resolution.source is DataRootSource.EXPLICIT
    assert resolution.explicitly_named is True


def test_container_is_checked_before_the_platform_directory() -> None:
    resolution = resolve_data_root({"RHEO_IN_CONTAINER": "1"})
    assert resolution.path == CONTAINER_DATA_ROOT == Path("/var/lib/rheo-stream")
    assert resolution.source is DataRootSource.CONTAINER
    assert resolution.explicitly_named is False
    assert (
        resolve_data_root({"RHEO_IN_CONTAINER": "true"}).source
        is DataRootSource.CONTAINER
    )
    assert (
        resolve_data_root({"RHEO_IN_CONTAINER": "0"}).source is DataRootSource.PLATFORM
    )


def test_both_unset_resolves_to_the_platform_directory() -> None:
    resolution = resolve_data_root({})
    assert resolution.path == platform_data_dir(environ={})
    assert resolution.source is DataRootSource.PLATFORM
    assert resolution.explicitly_named is False


def test_empty_explicit_value_counts_as_unset() -> None:
    assert resolve_data_root({"RHEO_DATA_ROOT": "  "}).source is DataRootSource.PLATFORM


def test_resolution_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RHEO_DATA_ROOT", str(tmp_path / "root"))
    assert resolve_data_root().path == tmp_path / "root"


def test_platform_data_dir_per_system(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert platform_data_dir(system="darwin", home=home, environ={}) == (
        home / "Library" / "Application Support" / "rheo-stream"
    )
    assert platform_data_dir(system="linux", home=home, environ={}) == (
        home / ".local" / "share" / "rheo-stream"
    )
    assert platform_data_dir(
        system="linux", home=home, environ={"XDG_DATA_HOME": "/xdg"}
    ) == Path("/xdg/rheo-stream")
    assert platform_data_dir(system="win32", home=home, environ={}) == (
        home / "AppData" / "Local" / "rheo-stream"
    )
    assert platform_data_dir(
        system="win32", home=home, environ={"LOCALAPPDATA": "C:/Users/x/AppData/Local"}
    ) == Path("C:/Users/x/AppData/Local/rheo-stream")
    assert platform_data_dir(environ={}).name == "rheo-stream"
    assert platform_data_dir(environ={}) == platform_data_dir(
        system=sys.platform, environ={}
    )


# --- validation ----------------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    path = tmp_path / "checkout"
    path.mkdir()
    return path


def test_rheo_local_is_accepted_only_when_named_explicitly(checkout: Path) -> None:
    local = checkout / ".rheo-local"
    with pytest.raises(DataRootRefusal) as excinfo:
        validate_data_root(local, checkout, explicitly_named=False)
    assert excinfo.value.state == "data_root_in_checkout"
    assert not local.exists()

    root = validate_data_root(local, checkout, explicitly_named=True)
    assert root == local
    assert root.is_dir()
    assert root.stat().st_mode & 0o777 == 0o700
    assert sorted(p.name for p in root.iterdir()) == [
        "config",
        "logs",
        "secrets",
        "workspaces",
    ]
    assert (root / "secrets").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "relative",
    ["data", ".", ".rheo-local/nested", "packages/core", ".rheo-local-2"],
)
def test_any_other_in_checkout_path_is_refused_even_when_explicit(
    checkout: Path, relative: str
) -> None:
    candidate = checkout / relative
    for explicit in (True, False):
        with pytest.raises(DataRootRefusal) as excinfo:
            validate_data_root(candidate, checkout, explicitly_named=explicit)
        assert excinfo.value.state == "data_root_in_checkout"
    assert not (checkout / "data").exists()


def test_outside_path_is_created_owner_only_with_the_layout(
    tmp_path: Path, checkout: Path
) -> None:
    root = validate_data_root(
        tmp_path / "srv" / "rheo", checkout, explicitly_named=False
    )
    assert root.is_dir()
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "config").is_dir()
    assert (root / "secrets").stat().st_mode & 0o777 == 0o700
    assert (root / "workspaces").is_dir()
    assert (root / "logs").is_dir()
    assert validate_data_root(root, checkout, explicitly_named=False) == root


def test_symlink_escaping_its_parent_is_refused(tmp_path: Path, checkout: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    roots = tmp_path / "roots"
    roots.mkdir()
    link = roots / "link"
    link.symlink_to(elsewhere)
    with pytest.raises(DataRootRefusal) as excinfo:
        validate_data_root(link, checkout, explicitly_named=True)
    assert excinfo.value.state == "data_root_symlink_escapes"


def test_symlink_within_its_parent_is_accepted(tmp_path: Path, checkout: Path) -> None:
    roots = tmp_path / "roots"
    real = roots / "real"
    real.mkdir(parents=True)
    alias = roots / "alias"
    alias.symlink_to(real)
    assert validate_data_root(alias, checkout, explicitly_named=True) == alias
    assert (real / "secrets").is_dir()


def test_symlink_into_the_checkout_is_refused(tmp_path: Path, checkout: Path) -> None:
    inside = checkout / "data"
    inside.mkdir()
    link = tmp_path / "checkout" / "link"
    link.symlink_to(inside)
    with pytest.raises(DataRootRefusal):
        validate_data_root(link, checkout, explicitly_named=True)


def test_existing_non_directory_is_refused(tmp_path: Path, checkout: Path) -> None:
    file = tmp_path / "not-a-dir"
    file.write_text("x")
    with pytest.raises(DataRootRefusal) as excinfo:
        validate_data_root(file, checkout, explicitly_named=True)
    assert excinfo.value.state == "data_root_not_a_directory"


def test_wide_permissions_warn_but_do_not_refuse(
    tmp_path: Path, checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    os.chmod(root, 0o755)
    caplog.set_level(logging.WARNING, logger="rheo_core.storage.data_root")
    assert validate_data_root(root, checkout, explicitly_named=True) == root
    [record] = [
        r for r in caplog.records if r.getMessage() == "data_root_permissions_wide"
    ]
    assert record.data_root == str(root)  # type: ignore[attr-defined]
    assert record.mode == "0755"  # type: ignore[attr-defined]
    assert root.stat().st_mode & 0o777 == 0o755  # warned, not changed


def test_validation_never_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch, checkout: Path
) -> None:
    monkeypatch.setenv("RHEO_DATA_ROOT", str(checkout / ".rheo-local"))
    with pytest.raises(DataRootRefusal):
        validate_data_root(checkout / ".rheo-local", checkout, explicitly_named=False)


# --- the closed purpose set ----------------------------------------------------------


def test_purpose_is_exactly_the_three_ratified_members() -> None:
    assert {p.value for p in Purpose} == {"uploads", "exports", "scratch"}
    assert len(Purpose) == 3


@pytest.mark.parametrize(
    ("purpose", "subdir"),
    [
        (Purpose.UPLOADS, "uploads"),
        (Purpose.EXPORTS, "exports"),
        (Purpose.SCRATCH, "runtime"),
    ],
)
def test_workspace_dir_for_each_purpose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, purpose: Purpose, subdir: str
) -> None:
    monkeypatch.setenv("RHEO_DATA_ROOT", str(tmp_path / "data"))
    expected = tmp_path / "data" / "workspaces" / str(WORKSPACE) / subdir
    assert workspace_dir_for(WORKSPACE, purpose) == expected
    assert workspace_dir_for(WORKSPACE, purpose.value) == expected  # type: ignore[arg-type]
    assert not expected.exists()  # pure: nothing is created


@pytest.mark.parametrize(
    "purpose", ["runs", "runtime", "", "../etc", "UPLOADS", Path("uploads")]
)
def test_only_the_three_purposes_are_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, purpose: object
) -> None:
    monkeypatch.setenv("RHEO_DATA_ROOT", str(tmp_path / "data"))
    with pytest.raises(ValueError):
        workspace_dir_for(WORKSPACE, purpose)  # type: ignore[arg-type]


def test_workspace_id_must_be_a_uuid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RHEO_DATA_ROOT", str(tmp_path / "data"))
    with pytest.raises(TypeError):
        workspace_dir_for("../../etc", Purpose.UPLOADS)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        workspace_dir_for(str(WORKSPACE), Purpose.UPLOADS)  # type: ignore[arg-type]


def test_model_cache_dir_is_deployment_level_and_one_segment_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``<data_root>/models/<component>``, outside ``workspaces/``, and pure.

    The refusals are the point: a component carrying a separator or a parent hop
    would let a module name a path the data root does not own.
    """
    monkeypatch.setenv("RHEO_DATA_ROOT", str(tmp_path / "data"))
    expected = tmp_path / "data" / "models" / "fastembed"
    assert model_cache_dir("fastembed") == expected
    assert not expected.exists()  # pure: nothing is created
    for component in ("../x", "a/b", ""):
        with pytest.raises(ValueError):
            model_cache_dir(component)


def test_the_module_contract_package_publishes_the_model_cache_accessor() -> None:
    """The name a module reaches it by is the storage function itself, not a copy."""
    import rheo_core.modules
    import rheo_core.storage.data_root

    assert (
        rheo_core.modules.model_cache_dir is rheo_core.storage.data_root.model_cache_dir
    )


def test_workspace_dir_wrapper_takes_a_context_and_a_purpose_only() -> None:
    assert list(inspect.signature(workspace_dir).parameters) == ["ctx", "purpose"]


def test_run_dir_for_is_core_owned_and_outside_purpose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RHEO_DATA_ROOT", str(tmp_path / "data"))
    expected = (
        tmp_path / "data" / "workspaces" / str(WORKSPACE) / "runs" / str(OPERATION)
    )
    assert run_dir_for(WORKSPACE, OPERATION) == expected
    assert not expected.exists()  # pure: nothing is created
    with pytest.raises(TypeError):
        run_dir_for(str(WORKSPACE), OPERATION)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        run_dir_for(WORKSPACE, str(OPERATION))  # type: ignore[arg-type]

"""Removing one file secret: scoped, symlink-safe, content-free and idempotent."""

import os
import stat
from pathlib import Path

import pytest
from rheo_core.secrets import SecretRef, SecretRefusal, SecretStore, SecretValue
from rheo_core.secrets import write as writer

REF = SecretRef.parse("secret://file/ws/example/connection/example/refresh-1")
FRESH = SecretRef.parse("secret://file/ws/example/connection/example/refresh-2")
VALUE = b"refresh-marker-0f1e2d3c-synthetic"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "secrets").mkdir(mode=0o700)
    return tmp_path


def scope(*, writable: bool = True):
    return SecretStore.scope_for(
        "intake", "secret://file/ws/example/connection/", writable=writable
    )


def _published(root: Path) -> Path:
    SecretStore(root).create(REF, SecretValue(VALUE), scope())
    target = root / "secrets" / REF.id
    assert target.read_bytes() == VALUE
    return target


def _assert_content_free(refusal: SecretRefusal, root: Path) -> None:
    for text in (str(refusal), repr(refusal), refusal.detail):
        assert REF.id not in text and str(REF) not in text
        assert str(root) not in text
        assert "example" not in text


def test_discard_removes_the_file_and_fsyncs_its_parent(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _published(root)
    parent_inode = target.parent.stat().st_ino
    synced: list[int] = []
    original_fsync = os.fsync

    def fsync(fd: int) -> None:
        synced.append(os.fstat(fd).st_ino)
        original_fsync(fd)

    monkeypatch.setattr(writer.os, "fsync", fsync)
    SecretStore(root).discard(REF, scope())
    assert not target.exists()
    assert synced and synced[-1] == parent_inode
    with pytest.raises(SecretRefusal) as exc:
        SecretStore(root).resolve(REF, scope())
    assert exc.value.state == "secret_missing"


def test_discard_then_create_in_the_same_directory_works(root: Path) -> None:
    target = _published(root)
    store = SecretStore(root)
    store.discard(REF, scope())
    store.create(FRESH, SecretValue(b"refresh-marker-second"), scope())
    assert store.resolve(FRESH, scope()).expose() == b"refresh-marker-second"
    store.create(REF, SecretValue(b"refresh-marker-again"), scope())
    assert target.read_bytes() == b"refresh-marker-again"


def test_a_read_only_or_foreign_scope_is_refused_and_nothing_is_deleted(
    root: Path,
) -> None:
    target = _published(root)
    foreign = SecretStore.scope_for(
        "foreign", "secret://file/elsewhere/", writable=True
    )
    for denied in (scope(writable=False), foreign):
        with pytest.raises(SecretRefusal) as exc:
            SecretStore(root).discard(REF, denied)
        assert exc.value.state == "secret_scope_denied"
        _assert_content_free(exc.value, root)
    assert target.read_bytes() == VALUE


def test_an_environment_reference_is_refused(root: Path) -> None:
    env = {"SYNTHETIC_KEY": "original"}
    ref = SecretRef.parse("secret://env/SYNTHETIC_KEY")
    writable = SecretStore.scope_for("test", str(ref), writable=True)
    with pytest.raises(SecretRefusal) as exc:
        SecretStore(root, environ=env).discard(ref, writable)
    assert exc.value.state == "secret_read_only"
    assert env == {"SYNTHETIC_KEY": "original"}


def test_discard_type_checks(root: Path) -> None:
    target = _published(root)
    store = SecretStore(root)
    with pytest.raises(TypeError):
        store.discard(str(REF), scope())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        store.discard(REF, None)  # type: ignore[arg-type]
    assert target.read_bytes() == VALUE


def test_a_missing_leaf_is_a_no_op(root: Path) -> None:
    _published(root)
    store = SecretStore(root)
    store.discard(REF, scope())
    store.discard(REF, scope())
    assert not (root / "secrets" / REF.id).exists()


def test_a_missing_intermediate_directory_is_a_no_op(root: Path) -> None:
    SecretStore(root).discard(REF, scope())
    assert list((root / "secrets").iterdir()) == []


def test_a_symlinked_directory_in_the_path_is_refused_and_its_target_untouched(
    root: Path,
) -> None:
    outside = root / "outside"
    outside.mkdir(mode=0o700)
    # A tree under the symlink target that the reference would name if followed.
    mirrored = outside / "example" / "connection" / "example"
    mirrored.mkdir(parents=True, mode=0o700)
    for directory in (outside / "example", outside / "example" / "connection"):
        directory.chmod(0o700)
    leaf = mirrored / REF.id.rsplit("/", 1)[-1]
    leaf.write_bytes(b"keep")
    (root / "secrets" / "ws").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SecretRefusal) as exc:
        SecretStore(root).discard(REF, scope())
    assert exc.value.state == "secret_permissions"
    _assert_content_free(exc.value, root)
    assert leaf.read_bytes() == b"keep"


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_leaf_that_is_not_a_regular_file_is_refused_and_left_in_place(
    root: Path, kind: str
) -> None:
    target = root / "secrets" / REF.id
    target.parent.mkdir(parents=True, mode=0o700)
    for directory in target.parents:
        if directory == root:
            break
        directory.chmod(0o700)
    original = root / "original"
    original.write_bytes(b"keep")
    if kind == "symlink":
        target.symlink_to(original)
    else:
        target.mkdir(mode=0o700)
    with pytest.raises(SecretRefusal) as exc:
        SecretStore(root).discard(REF, scope())
    assert exc.value.state == "secret_permissions"
    _assert_content_free(exc.value, root)
    assert target.is_symlink() if kind == "symlink" else target.is_dir()
    assert original.read_bytes() == b"keep"


@pytest.mark.parametrize("at_root", [True, False])
def test_a_secret_directory_with_the_wrong_mode_is_refused(
    root: Path, at_root: bool
) -> None:
    target = _published(root)
    directory = root / "secrets" if at_root else root / "secrets" / "ws"
    directory.chmod(0o755)
    with pytest.raises(SecretRefusal) as exc:
        SecretStore(root).discard(REF, scope())
    assert exc.value.state == "secret_permissions"
    _assert_content_free(exc.value, root)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    directory.chmod(0o700)
    assert target.read_bytes() == VALUE


def test_a_secret_directory_owned_by_another_user_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _published(root)
    real = os.geteuid()
    monkeypatch.setattr(writer.os, "geteuid", lambda: real + 1)
    with pytest.raises(SecretRefusal) as exc:
        SecretStore(root).discard(REF, scope())
    assert exc.value.state == "secret_permissions"
    _assert_content_free(exc.value, root)
    monkeypatch.undo()
    assert target.read_bytes() == VALUE


def test_an_io_failure_is_content_free(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _published(root)

    def unlink(*args: object, **kwargs: object) -> None:
        raise OSError(f"{root}/secrets/{REF.id}: {VALUE.decode()}")

    monkeypatch.setattr(writer.os, "unlink", unlink)
    with pytest.raises(SecretRefusal) as exc:
        SecretStore(root).discard(REF, scope())
    assert exc.value.state == "secret_write_failed"
    _assert_content_free(exc.value, root)
    assert VALUE.decode() not in str(exc.value)


@pytest.mark.parametrize("ref_id", ["../escape", "/absolute", "ws//empty"])
def test_backend_discard_validates_paths_itself(root: Path, ref_id: str) -> None:
    with pytest.raises(SecretRefusal) as exc:
        writer.discard_file_secret(root / "secrets", ref_id)
    assert exc.value.state == "secret_ref_malformed"
    assert ref_id not in str(exc.value)

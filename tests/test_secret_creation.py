"""Synthetic credential provisioning: no overwrites, leaks or partial publication."""

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from rheo_core.secrets import SecretRef, SecretRefusal, SecretStore, SecretValue
from rheo_core.secrets import write as writer

REF = SecretRef.parse("secret://file/ws/example/connection/example/signing-1")
VALUE = b"synthetic-signing-key-only"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "secrets").mkdir(mode=0o700)
    return tmp_path


def scope(*, writable: bool = True):
    return SecretStore.scope_for(
        "intake", "secret://file/ws/example/connection/", writable=writable
    )


def test_create_private_value_and_keep_existing_read_scope_read_only(
    root: Path,
) -> None:
    store = SecretStore(root)
    store.create(REF, SecretValue(VALUE), scope())
    assert store.resolve(REF, scope(writable=False)).expose() == VALUE
    file = root / "secrets" / REF.id
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    for parent in file.parents:
        if parent == root:
            break
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert not list((root / "secrets").rglob(".rheo-secret-*"))


@pytest.mark.parametrize("value", [VALUE, b"replacement-must-not-land"])
def test_existing_reference_is_never_replaced(root: Path, value: bytes) -> None:
    store = SecretStore(root)
    store.create(REF, SecretValue(VALUE), scope())
    with pytest.raises(SecretRefusal) as exc:
        store.create(REF, SecretValue(value), scope())
    assert exc.value.state == "secret_exists"
    assert store.resolve(REF, scope()).expose() == VALUE
    assert not list((root / "secrets").rglob(".rheo-secret-*"))


def test_scope_refusal_happens_before_any_filesystem_write(root: Path) -> None:
    store = SecretStore(root)
    foreign = SecretStore.scope_for(
        "foreign", "secret://file/elsewhere/", writable=True
    )
    for denied in (scope(writable=False), foreign):
        with pytest.raises(SecretRefusal) as exc:
            store.create(REF, SecretValue(VALUE), denied)
        assert exc.value.state == "secret_scope_denied"
    assert list((root / "secrets").iterdir()) == []


def test_environment_backend_is_read_only(root: Path) -> None:
    env = {"SYNTHETIC_KEY": "original"}
    store = SecretStore(root, environ=env)
    ref = SecretRef.parse("secret://env/SYNTHETIC_KEY")
    writable = SecretStore.scope_for("test", str(ref), writable=True)
    with pytest.raises(SecretRefusal) as exc:
        store.create(ref, SecretValue(VALUE), writable)
    assert exc.value.state == "secret_read_only"
    assert env == {"SYNTHETIC_KEY": "original"}


def test_create_type_checks_do_not_echo_values(root: Path) -> None:
    store = SecretStore(root)
    with pytest.raises(TypeError):
        store.create(REF, VALUE, scope())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        store.create(str(REF), SecretValue(VALUE), scope())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        store.create(REF, SecretValue(VALUE), None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SecretStore.scope_for("test", str(REF), writable="yes")  # type: ignore[arg-type]
    assert list((root / "secrets").iterdir()) == []


@pytest.mark.parametrize("kind", ["root", "parent", "target"])
def test_symlinks_are_never_followed(root: Path, kind: str) -> None:
    outside = root / "outside"
    outside.mkdir(mode=0o700)
    original = outside / "original"
    original.write_bytes(b"keep")
    if kind == "root":
        (root / "secrets").rmdir()
        (root / "secrets").symlink_to(outside, target_is_directory=True)
    elif kind == "parent":
        (root / "secrets" / "ws").symlink_to(outside, target_is_directory=True)
    else:
        target = root / "secrets" / REF.id
        target.parent.mkdir(parents=True, mode=0o700)
        for directory in target.parents:
            if directory == root:
                break
            directory.chmod(0o700)
        target.symlink_to(original)
    with pytest.raises(SecretRefusal):
        SecretStore(root).create(REF, SecretValue(VALUE), scope())
    assert original.read_bytes() == b"keep"
    assert sorted(p.name for p in outside.iterdir()) == ["original"]


@pytest.mark.parametrize("at_root", [True, False])
def test_existing_unsafe_directory_is_refused_without_chmod(
    root: Path, at_root: bool
) -> None:
    directory = root / "secrets" if at_root else root / "secrets" / "ws"
    directory.mkdir(exist_ok=True)
    directory.chmod(0o755)
    with pytest.raises(SecretRefusal) as exc:
        SecretStore(root).create(REF, SecretValue(VALUE), scope())
    assert exc.value.state == "secret_permissions"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert list(directory.iterdir()) == []


def test_concurrent_creators_publish_exactly_one_complete_value(root: Path) -> None:
    store = SecretStore(root)
    barrier = Barrier(8)
    values = [bytes([i]) * 131072 for i in range(8)]

    def create(value: bytes) -> str:
        barrier.wait(timeout=10)
        try:
            store.create(REF, SecretValue(value), scope())
        except SecretRefusal as refusal:
            return refusal.state
        return "created"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(create, values))
    assert outcomes.count("created") == 1
    assert outcomes.count("secret_exists") == 7
    assert store.resolve(REF, scope()).expose() == values[outcomes.index("created")]
    assert not list((root / "secrets").rglob(".rheo-secret-*"))


def test_value_is_complete_before_publication(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_link = os.link

    def link(src, dst, **kwargs):
        target = root / "secrets" / REF.id
        assert not target.exists()
        assert (target.parent / src).read_bytes() == VALUE
        original_link(src, dst, **kwargs)
        assert target.read_bytes() == VALUE

    monkeypatch.setattr(writer.os, "link", link)
    SecretStore(root).create(REF, SecretValue(VALUE), scope())


@pytest.mark.parametrize("after_publication", [False, True])
def test_io_failure_is_content_free_and_cleans_staging(
    root: Path, monkeypatch: pytest.MonkeyPatch, after_publication: bool
) -> None:
    original_fsync = os.fsync
    target = root / "secrets" / REF.id

    def fsync(fd):
        if (after_publication and target.exists()) or (
            not after_publication and stat.S_ISREG(os.fstat(fd).st_mode)
        ):
            raise OSError(VALUE.decode())
        original_fsync(fd)

    monkeypatch.setattr(writer.os, "fsync", fsync)
    with pytest.raises(SecretRefusal) as exc:
        SecretStore(root).create(REF, SecretValue(VALUE), scope())
    assert exc.value.state == "secret_write_failed"
    assert VALUE.decode() not in str(exc.value)
    assert target.exists() is after_publication
    if after_publication:
        assert target.read_bytes() == VALUE
    assert not list((root / "secrets").rglob(".rheo-secret-*"))


def test_missing_root_is_not_created_by_store(tmp_path: Path) -> None:
    with pytest.raises(SecretRefusal) as exc:
        SecretStore(tmp_path).create(REF, SecretValue(VALUE), scope())
    assert exc.value.state == "secret_write_failed"
    assert not (tmp_path / "secrets").exists()


@pytest.mark.parametrize(
    "ref_id", ["../escape", "/absolute", "ws//empty", "ws/../escape"]
)
def test_backend_write_validates_paths_itself(root: Path, ref_id: str) -> None:
    with pytest.raises(SecretRefusal) as exc:
        writer.create_file_secret(root / "secrets", ref_id, VALUE)
    assert exc.value.state == "secret_ref_malformed"

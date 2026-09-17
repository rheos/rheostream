"""Criterion 3 foundation: checkout-local runtime artifacts are git-ignored.

Creates one representative file per checkout-local artifact family and asserts each is
reported ignored by ``git check-ignore``, then removes only what it created so the
checkout is left as it started.

**Every fixture path is derived from ``LOCAL_OPT_IN``**
(``packages/core/src/rheo_core/storage/data_root.py``), never from a typed
``.rheo-local`` literal. The criterion's own words are "every configuration file,
upload, database, database sidecar, export, and log **it produces**" — so the directory
under test has to be the one the *product* writes into, read from the product's own
constant. A literal list here would assert that a fixed set of paths somebody typed is
ignored: move the product's directory and the ignore rules would stop matching anything
it produces while this test went on passing, green and wrong. The literal-list companion
that pins ``.rheo-local`` as the accepted name lives separately, in
``tests/test_data_root.py::test_rheo_local_is_accepted_only_when_named_explicitly``, so
the pair reads the constant from two independent instruments rather than one.

Every fixture path also carries a unique per-run token (e.g.
``<LOCAL_OPT_IN>/workspaces/<uuid>/profile.json``), never a fixed shared path. Later
phases keep real workspace data under that directory, so writing then deleting a fixed
shared path like ``workspaces/demo/profile.json`` would be a data-loss footgun. The
teardown removes only this run's own subpaths.
"""

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from rheo_core.storage.data_root import LOCAL_OPT_IN

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _artifact_relpaths(token: str) -> "tuple[str, ...]":
    """Representative artifact per checkout-local family, keyed by a unique token.

    The leading directory is ``LOCAL_OPT_IN``, the product's own constant — see the
    module docstring for why it is read rather than typed.
    """
    # A workspace profile/config and a database backup are the two families
    # criterion 3 names explicitly; the rest exercise the other trees under the
    # checkout-local fallback directory.
    return (
        f"{LOCAL_OPT_IN}/workspaces/{token}/profile.json",
        f"{LOCAL_OPT_IN}/uploads/{token}/evidence.pdf",
        f"{LOCAL_OPT_IN}/transcripts/{token}/session.jsonl",
        f"{LOCAL_OPT_IN}/exports/{token}/workspace.csv",
        f"{LOCAL_OPT_IN}/backups/{token}.sql",
        f"{LOCAL_OPT_IN}/memory/{token}/index.bin",
        # 0b1: the operator config file and the file secret backend's cluster
        # secret, both checkout-local.
        f"{LOCAL_OPT_IN}/config/{token}/deployment.toml",
        f"{LOCAL_OPT_IN}/secrets/{token}/cluster/primary-dsn",
    )


def _is_ignored(relative: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", relative],
        cwd=_REPO_ROOT,
        check=False,
    )
    return result.returncode == 0


@pytest.fixture
def rheo_local_artifacts() -> "list[str]":
    token = uuid.uuid4().hex
    relpaths = _artifact_relpaths(token)
    local_root = _REPO_ROOT / LOCAL_OPT_IN
    preexisting = local_root.exists()
    # Enforce the module docstring's own rule before writing anything, in a pass
    # over every path that writes nothing: a fixed literal path here would
    # overwrite (then delete) real data at that path if it already existed.
    # Validating inside the write loop below would let an earlier iteration's
    # write stand uncleaned if a later path failed this check, since that write
    # would happen before the try/finally starts. Nothing else in this suite or
    # in check_repository.py checks this — it is otherwise pure discipline.
    for relative in relpaths:
        assert token in relative, (
            f"fixture path {relative!r} does not carry the per-run token "
            f"{token!r}; a fixed literal path here risks silently destroying "
            f"real {LOCAL_OPT_IN}/ data at that path"
        )
    created: list[Path] = []
    try:
        for relative in relpaths:
            path = _REPO_ROOT / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test artifact\n", encoding="utf-8")
            created.append(path)
        yield list(relpaths)
    finally:
        for path in created:
            path.unlink(missing_ok=True)
        if not preexisting:
            # The test brought the checkout-local directory into existence, so the
            # whole tree is ours to remove.
            shutil.rmtree(local_root, ignore_errors=True)
        else:
            # It already held (possibly real) data: remove only the unique
            # per-run token dirs this test created, never a shared family path.
            # Walk each relative path's own directories (not just the immediate
            # parent) for the first one named after the token, so a nested shape
            # like ``secrets/<token>/cluster/primary-dsn`` is cleaned up from the
            # token directory down, not left as an orphaned ``cluster/`` dir.
            for relative in relpaths:
                ancestor = (_REPO_ROOT / relative).parent
                while ancestor != _REPO_ROOT:
                    if token in ancestor.name:
                        shutil.rmtree(ancestor, ignore_errors=True)
                        break
                    ancestor = ancestor.parent


def test_checkout_local_artifacts_are_ignored(
    rheo_local_artifacts: "list[str]",
) -> None:
    not_ignored = [rel for rel in rheo_local_artifacts if not _is_ignored(rel)]
    assert not not_ignored, f"checkout-local artifacts not git-ignored: {not_ignored}"

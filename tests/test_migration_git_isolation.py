"""The migration leak gate's Git subprocesses must respect their explicit cwd."""

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_migration_outputs.py"
_LOCAL_GIT_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_git_gate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_git_subprocess_receives_a_scrubbed_copy_of_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _load_script()
    for name in _LOCAL_GIT_VARIABLES:
        monkeypatch.setenv(name, "synthetic-inherited-value")
    monkeypatch.setenv("MIGRATION_TEST_KEEP", "synthetic-preserved-value")
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs)
        output = "fixture.txt\0" if args[1] == "ls-tree" else "synthetic text"
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(script.subprocess, "run", run)
    script._run_git(["status"], cwd=tmp_path)
    assert script._tracked_text_at_ref("HEAD", cwd=tmp_path) == [
        ("fixture.txt", "synthetic text")
    ]
    assert len(calls) == 3  # Direct helper, tree listing, and separate blob read.
    for call in calls:
        assert call["cwd"] == tmp_path
        assert not set(_LOCAL_GIT_VARIABLES) & set(call["env"])
        assert call["env"]["MIGRATION_TEST_KEEP"] == "synthetic-preserved-value"
    assert all(
        os.environ[name] == "synthetic-inherited-value" for name in _LOCAL_GIT_VARIABLES
    )


def test_scratch_self_test_cannot_change_an_inherited_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _load_script()
    outer = tmp_path / "unrelated-repo"
    outer.mkdir()
    script._run_git(["init", "-q"], cwd=outer)
    script._run_git(["config", "user.email", "unrelated@example.com"], cwd=outer)
    script._run_git(["config", "user.name", "Unrelated"], cwd=outer)
    (outer / "sentinel.txt").write_text("synthetic unrelated content\n")
    script._run_git(["add", "sentinel.txt"], cwd=outer)
    script._run_git(["commit", "-q", "-m", "unrelated seed"], cwd=outer)
    config_before = (outer / ".git/config").read_bytes()
    index_before = (outer / ".git/index").read_bytes()
    head_before = script._run_git(["rev-parse", "HEAD"], cwd=outer).stdout
    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outer))
    monkeypatch.setenv("GIT_INDEX_FILE", str(outer / ".git/index"))

    assert script._self_test() is None
    assert (outer / ".git/config").read_bytes() == config_before
    assert (outer / ".git/index").read_bytes() == index_before
    assert script._run_git(["rev-parse", "HEAD"], cwd=outer).stdout == head_before
    assert (outer / "sentinel.txt").read_text() == "synthetic unrelated content\n"


def test_tree_and_blob_reads_ignore_an_inherited_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _load_script()
    repos = [tmp_path / "requested", tmp_path / "inherited"]
    for repo in repos:
        repo.mkdir()
        script._run_git(["init", "-q"], cwd=repo)
        script._run_git(["config", "user.email", "scratch@example.com"], cwd=repo)
        script._run_git(["config", "user.name", "Scratch"], cwd=repo)
        (repo / "same.txt").write_text(repo.name)
        script._run_git(["add", "same.txt"], cwd=repo)
        script._run_git(["commit", "-q", "-m", "synthetic seed"], cwd=repo)
    monkeypatch.setenv("GIT_DIR", str(repos[1] / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repos[1]))
    assert script._tracked_text_at_ref("HEAD", cwd=repos[0]) == [
        ("same.txt", "requested")
    ]

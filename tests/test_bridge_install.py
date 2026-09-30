"""``rheo-bridge`` subcommands: init, set-token, install/remove the hooks,
uninstall, status and drain (FR 11, FR 17; AC 11, AC 15).

Every test runs against a temporary user home, a temporary ``bridge_home`` and a
separate synthetic enrolled directory (plus, for the Git cases, a synthetic
checkout with its own ``git init``). An autouse fixture points ``HOME`` at an
empty temporary directory and makes ``Path.home`` and ``os.path.expanduser``
raise, so a subcommand that looked up the real home fails its test. Tokens,
ids, interpreters and transcripts are invented; no request leaves the process.

Seams under test: ``install-hook``'s idempotent merge (unrelated content keeps
its meaning) and its Git-ignore refusal; ``set-token``'s TTY refusal and
non-disclosure; the no-hook-installed inertness fixture (AC 15).
"""

from __future__ import annotations

import ast
import importlib.metadata
import io
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tomllib
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from rheo_bridge import cli, keys, paths, settings_file, state, transcript
from rheo_bridge import config as bridge_config
from rheo_bridge.client import (
    IngestGap,
    IngestRecord,
    IngestRefused,
    IngestResponse,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
NOW_TS = int(NOW.timestamp())
API_URL = "https://api.example.org"
VENV_PYTHON = "/opt/rheo-synthetic/venv/bin/python"
INTERPRETER = "/opt/rheo-synthetic/python3.12"
SESSION_ID = "7a7a7a7a-0000-4000-8000-00000000f00d"
REPO_ROOT = Path(__file__).resolve().parents[1]


# --- the no-real-home guard -----------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    empty = tmp_path_factory.mktemp("empty-home")
    monkeypatch.setenv("HOME", str(empty))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty / ".config"))
    # Git must not read the real global or system config (a global excludes
    # file could make the "not ignored" case pass for the wrong reason).
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

    def refuse(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("a subcommand looked up the real home")

    monkeypatch.setattr(Path, "home", refuse)
    monkeypatch.setattr(os.path, "expanduser", refuse)
    yield empty
    assert list(empty.iterdir()) == [], "something wrote into the empty HOME"


def test_the_guard_makes_any_home_lookup_fail() -> None:
    with pytest.raises(AssertionError):
        Path.home()
    with pytest.raises(AssertionError):
        os.path.expanduser("~")


def test_this_file_builds_claude_paths_only_under_a_tmp_root() -> None:
    """Every ``".claude"`` literal here is joined onto a tmp-derived path."""
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    tmp_roots = {"tmp_path", "user_home"}
    (this_check,) = (
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_this_file_builds_claude_paths_only_under_a_tmp_root"
    )
    assert this_check.end_lineno is not None
    found = 0
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value == ".claude"
            # The comparison just above is this check's own literal.
            and not this_check.lineno <= node.lineno <= this_check.end_lineno
        ):
            continue
        found += 1
        parent = parents[node]
        assert isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div)
        root = parent.left
        while isinstance(root, ast.BinOp):
            root = root.left
        assert isinstance(root, ast.Name) and root.id in tmp_roots, ast.dump(parent)
    assert found >= 1, "the check found no .claude literal to look at"


# --- fixtures and helpers -------------------------------------------------------


@dataclass
class Dirs:
    user_home: Path
    bridge_home: Path
    projects_root: Path
    enrolled: Path

    @property
    def settings(self) -> Path:
        return settings_file.settings_path(self.enrolled)


def make_dirs(tmp_path: Path) -> Dirs:
    user_home = tmp_path / "home"
    enrolled = user_home / "code" / "example-project"
    enrolled.mkdir(parents=True)
    projects_root = user_home / ".claude" / "projects"
    projects_root.mkdir(parents=True)
    return Dirs(
        user_home=user_home,
        bridge_home=user_home / ".rheo-bridge",
        projects_root=projects_root,
        enrolled=Path(os.path.realpath(enrolled)),
    )


@pytest.fixture
def dirs(tmp_path: Path) -> Dirs:
    return make_dirs(tmp_path)


@pytest.fixture
def initialised(dirs: Dirs, capsys: pytest.CaptureFixture[str]) -> Dirs:
    assert (
        cli.init(
            dirs.bridge_home,
            enrolled_dir=dirs.enrolled,
            api_url=API_URL,
            executable=VENV_PYTHON,
        )
        == 0
    )
    capsys.readouterr()
    return dirs


class TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def new_token() -> str:
    return "rbt_" + secrets.token_urlsafe(32)


def token_line(token: str, enrollment_id: str, expires: datetime) -> str:
    return (
        json.dumps(
            {
                "enrollment_id": enrollment_id,
                "token": token,
                "expires_at": expires.isoformat(),
            }
        )
        + "\n"
    )


def set_token(
    d: Dirs,
    token: str,
    *,
    enrollment_id: str | None = None,
    expires: datetime = NOW + timedelta(days=90),
) -> None:
    line = token_line(token, enrollment_id or str(uuid.uuid4()), expires)
    assert cli.set_token(d.bridge_home, stdin=io.StringIO(line)) == 0


def install(d: Dirs, *, dry_run: bool = False, enrolled: Path | None = None) -> int:
    return cli.install_hook(
        d.bridge_home,
        enrolled_dir=enrolled or d.enrolled,
        dry_run=dry_run,
        interpreter=INTERPRETER,
    )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


GUARD = '[ -x "$0" ] && [ -f "$1" ] && [ -r "$1" ] && exec "$0" "$1" {event}; exit 0'


def expected_command(d: Dirs, event: str, interpreter: str = INTERPRETER) -> str:
    """Written out by hand: the ``/bin/sh`` guard, then the two paths as args.
    The synthetic paths hold no character ``shlex.quote`` would quote."""
    guard = GUARD.format(event=event)
    return f"/bin/sh -c '{guard}' {interpreter} {d.bridge_home / 'hook.py'}"


def our_commands(document: dict[str, Any]) -> list[str]:
    found = []
    for groups in document.get("hooks", {}).values():
        for group in groups:
            for entry in group.get("hooks", []):
                if "hook.py" in entry.get("command", ""):
                    found.append(entry["command"])
    return found


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def config_of(d: Dirs) -> bridge_config.Config:
    loaded = bridge_config.load(d.bridge_home)
    assert loaded is not None
    return loaded


# --- the console script ---------------------------------------------------------


def test_the_console_script_resolves_to_cli_main() -> None:
    pyproject = tomllib.loads(
        (REPO_ROOT / "apps" / "bridge" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["scripts"] == {"rheo-bridge": "rheo_bridge.cli:main"}
    (entry,) = importlib.metadata.entry_points(
        group="console_scripts", name="rheo-bridge"
    )
    assert entry.load() is cli.main


# --- init -----------------------------------------------------------------------


def test_init_requires_an_explicit_enrolled_dir(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # argparse refuses before main() resolves any home.
    with pytest.raises(SystemExit) as raised:
        cli.main(["init", "--api-url", API_URL])
    assert raised.value.code == 2
    assert "--enrolled-dir" in capsys.readouterr().err


def test_init_stores_the_config_keys_and_prints_the_operator_command(
    dirs: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.init(
        dirs.bridge_home,
        enrolled_dir=dirs.enrolled,
        api_url=API_URL,
        executable=VENV_PYTHON,
    )
    assert code == 0
    out = capsys.readouterr().out

    config = config_of(dirs)
    assert config.enrolled_dir == str(dirs.enrolled)
    assert config.worker_argv == ["/opt/rheo-synthetic/venv/bin/rheo-bridge", "drain"]
    assert config.enrollment_id is None and config.token_expires_at is None
    assert config.install_salt

    assert stat.S_IMODE(dirs.bridge_home.stat().st_mode) == 0o700
    for path in (
        paths.machine_key_path(dirs.bridge_home),
        paths.config_path(dirs.bridge_home),
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    paths.check_private(dirs.bridge_home)

    machine_key = paths.machine_key_path(dirs.bridge_home).read_bytes()
    machine_fp = keys.machine_fingerprint(machine_key)
    project_fp = keys.project_fingerprint(str(dirs.enrolled))
    assert f"enrolled_dir: {dirs.enrolled}\n" in out
    assert f"machine_fingerprint: {machine_fp}\n" in out
    assert f"project_fingerprint: {project_fp}\n" in out
    assert (
        f"rheo evidence enroll --account <ACCOUNT_ID> --workspace <WORKSPACE_ID> "
        f"--machine {machine_fp} --project {project_fp} --json"
    ) in out
    # No settings write.
    assert not dirs.settings.exists()


def test_init_refuses_a_second_run_and_keeps_the_machine_key(
    initialised: Dirs,
) -> None:
    before = paths.machine_key_path(initialised.bridge_home).read_bytes()
    with pytest.raises(cli.CliError, match="already set up"):
        cli.init(
            initialised.bridge_home,
            enrolled_dir=initialised.enrolled,
            api_url=API_URL,
            executable=VENV_PYTHON,
        )
    assert paths.machine_key_path(initialised.bridge_home).read_bytes() == before


def test_init_refuses_a_plain_http_api_url(dirs: Dirs) -> None:
    with pytest.raises(cli.CliError, match="https"):
        cli.init(
            dirs.bridge_home,
            enrolled_dir=dirs.enrolled,
            api_url="http://api.example.org",
            executable=VENV_PYTHON,
        )
    assert not dirs.bridge_home.exists()


# --- set-token ------------------------------------------------------------------


def test_set_token_stores_the_value_privately_and_never_prints_it(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    token = new_token()
    enrollment_id = str(uuid.uuid4())
    expires = NOW + timedelta(days=90)
    line = token_line(token, enrollment_id, expires)

    assert cli.set_token(initialised.bridge_home, stdin=io.StringIO(line)) == 0
    captured = capsys.readouterr()

    token_file = paths.token_path(initialised.bridge_home)
    assert token_file.read_text() == token
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    config = config_of(initialised)
    assert config.enrollment_id == enrollment_id
    assert config.token_expires_at == expires.isoformat()
    assert captured.out == (
        f"enrollment_id: {enrollment_id}\ntoken_expires_at: {expires.isoformat()}\n"
    )
    printed = captured.out + captured.err
    for start in range(len(token) - 7):
        assert token[start : start + 8] not in printed
    # Nor did the value reach config.json.
    assert token not in paths.config_path(initialised.bridge_home).read_text()


def test_set_token_refuses_a_terminal_and_writes_nothing(initialised: Dirs) -> None:
    line = token_line(new_token(), str(uuid.uuid4()), NOW + timedelta(days=90))
    with pytest.raises(cli.CliError, match="terminal"):
        cli.set_token(initialised.bridge_home, stdin=TtyInput(line))
    assert not paths.token_path(initialised.bridge_home).exists()
    assert config_of(initialised).enrollment_id is None


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json\n",
        '["a", "list"]\n',
        '{"enrollment_id": "x", "token": "y"}\n',
        "{}\n{}\n",
    ],
)
def test_set_token_refuses_anything_but_one_token_line(
    initialised: Dirs, capsys: pytest.CaptureFixture[str], stdin: str
) -> None:
    with pytest.raises(cli.CliError):
        cli.set_token(initialised.bridge_home, stdin=io.StringIO(stdin))
    assert not paths.token_path(initialised.bridge_home).exists()


def test_a_refused_token_line_does_not_echo_the_value(initialised: Dirs) -> None:
    token = new_token()
    bad = json.dumps({"enrollment_id": "not-a-uuid", "token": token, "expires_at": "x"})
    with pytest.raises(cli.CliError) as raised:
        cli.set_token(initialised.bridge_home, stdin=io.StringIO(bad + "\n"))
    assert token[:8] not in str(raised.value)


def test_a_rotate_replaces_the_token_atomically(
    initialised: Dirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = new_token(), new_token()
    enrollment_id = str(uuid.uuid4())
    set_token(initialised, old, enrollment_id=enrollment_id)
    token_file = paths.token_path(initialised.bridge_home)

    seen: list[tuple[str, str]] = []
    real_replace = os.replace

    def watching_replace(src: Any, dst: Any) -> None:
        if Path(dst) == token_file:
            # At the swap the target still holds the whole old value and the
            # temp file the whole new one: no empty or partial file is ever
            # visible under the token's name.
            seen.append((Path(dst).read_text(), Path(src).read_text()))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", watching_replace)
    set_token(initialised, new, enrollment_id=enrollment_id)

    assert seen == [(old, new)]
    assert token_file.read_text() == new
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    leftovers = [
        p.name for p in initialised.bridge_home.iterdir() if ".token." in p.name
    ]
    assert leftovers == []


# --- install-hook ---------------------------------------------------------------


def test_install_on_an_absent_file_creates_exactly_the_two_groups(
    initialised: Dirs,
) -> None:
    assert install(initialised) == 0
    document = load_json(initialised.settings)
    assert document == {
        "hooks": {
            event: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": expected_command(initialised, event),
                            "timeout": 5,
                        }
                    ]
                }
            ]
            for event in ("Stop", "SessionEnd")
        }
    }
    installed = paths.hook_path(initialised.bridge_home)
    assert installed.read_bytes() == Path(cli.hook.__file__).read_bytes()
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert config_of(initialised).hook_created_settings is True
    paths.check_private(initialised.bridge_home)


def test_the_default_interpreter_is_the_resolved_absolute_one(
    initialised: Dirs,
) -> None:
    import sys

    assert (
        cli.install_hook(initialised.bridge_home, enrolled_dir=initialised.enrolled)
        == 0
    )
    commands = our_commands(load_json(initialised.settings))
    interpreter = os.path.realpath(sys.executable)
    assert os.path.isabs(interpreter)
    assert sorted(commands) == sorted(
        settings_file.hook_command(
            interpreter, initialised.bridge_home / "hook.py", event
        )
        for event in ("Stop", "SessionEnd")
    )
    assert all(shlex.split(c)[3] == interpreter for c in commands)


EXISTING: dict[str, Any] = {
    "permissions": {"allow": ["Bash(ls:*)"], "deny": []},
    "env": {"EXAMPLE_FLAG": "1"},
    "hooks": {
        "Stop": [
            {"hooks": [{"type": "command", "command": "/usr/bin/true stop-note"}]}
        ],
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "/usr/bin/true pre"}],
            }
        ],
    },
    "empty": {},
}


def test_install_keeps_unrelated_keys_and_other_hooks(initialised: Dirs) -> None:
    initialised.settings.parent.mkdir()
    initialised.settings.write_text(json.dumps(EXISTING, indent=4))

    assert install(initialised) == 0
    document = load_json(initialised.settings)

    for key in ("permissions", "env", "empty"):
        assert document[key] == EXISTING[key]
    assert document["hooks"]["PreToolUse"] == EXISTING["hooks"]["PreToolUse"]
    assert document["hooks"]["Stop"][0] == EXISTING["hooks"]["Stop"][0]
    assert len(document["hooks"]["Stop"]) == 2
    assert sorted(our_commands(document)) == sorted(
        expected_command(initialised, e) for e in ("Stop", "SessionEnd")
    )
    # Taking ours back out gives the original structure exactly.
    created = config_of(initialised).hook_created_containers
    assert created == ["hooks.SessionEnd"]
    assert settings_file.remove(document, created=created) == EXISTING
    assert config_of(initialised).hook_created_settings is False


def test_installing_twice_is_a_no_op(initialised: Dirs) -> None:
    assert install(initialised) == 0
    # The user adds a Stop hook of their own after ours.
    document = load_json(initialised.settings)
    document["hooks"]["Stop"].append(
        {"hooks": [{"type": "command", "command": "/usr/bin/true later"}]}
    )
    initialised.settings.write_text(json.dumps(document, indent=2) + "\n")
    first = initialised.settings.read_bytes()
    assert install(initialised) == 0
    assert initialised.settings.read_bytes() == first
    commands = our_commands(load_json(initialised.settings))
    assert len(commands) == 2 and len(set(commands)) == 2


def test_reinstalling_after_an_interpreter_move_keeps_one_entry_per_event(
    initialised: Dirs,
) -> None:
    assert (
        cli.install_hook(
            initialised.bridge_home,
            enrolled_dir=initialised.enrolled,
            interpreter="/opt/rheo-synthetic/old/python3.12",
        )
        == 0
    )
    assert install(initialised) == 0  # INTERPRETER, the new location
    document = load_json(initialised.settings)
    for event in ("Stop", "SessionEnd"):
        commands = [
            entry["command"]
            for group in document["hooks"][event]
            for entry in group["hooks"]
        ]
        assert commands == [expected_command(initialised, event)]
    # And removal still gives back the file install created: none at all.
    assert (
        cli.remove_hook(initialised.bridge_home, enrolled_dir=initialised.enrolled) == 0
    )
    assert not initialised.settings.exists()


@pytest.mark.parametrize(
    "original",
    [
        {"hooks": {}},
        {"hooks": {"Stop": []}},
        {"hooks": {"Stop": [], "SessionEnd": []}, "env": {"EXAMPLE_FLAG": "1"}},
    ],
    ids=["empty-hooks", "empty-stop", "both-empty"],
)
def test_remove_restores_empty_containers_that_were_there_before(
    initialised: Dirs, original: dict[str, Any]
) -> None:
    initialised.settings.parent.mkdir()
    initialised.settings.write_text(json.dumps(original))
    assert install(initialised) == 0
    assert len(our_commands(load_json(initialised.settings))) == 2
    assert (
        cli.remove_hook(initialised.bridge_home, enrolled_dir=initialised.enrolled) == 0
    )
    assert load_json(initialised.settings) == original
    config = config_of(initialised)
    assert config.hook_created_containers == []
    assert config.hook_created_settings is False


def test_a_symlinked_claude_directory_is_refused(
    initialised: Dirs, tmp_path: Path
) -> None:
    """Outside a Git tree the ignore check does not apply; this guard does."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    initialised.settings.parent.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(cli.CliError, match="symlink"):
        install(initialised)
    with pytest.raises(cli.CliError, match="symlink"):
        install(initialised, dry_run=True)
    assert list(elsewhere.iterdir()) == []
    assert not paths.hook_path(initialised.bridge_home).exists()
    (elsewhere / "settings.local.json").write_text(json.dumps(EXISTING))
    with pytest.raises(cli.CliError, match="symlink"):
        cli.remove_hook(initialised.bridge_home, enrolled_dir=initialised.enrolled)
    assert load_json(elsewhere / "settings.local.json") == EXISTING


def test_a_settings_path_through_a_symlinked_ancestor_is_refused_by_write(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    claude_dir = settings_file.settings_path(real).parent
    claude_dir.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    path = settings_file.settings_path(alias)
    with pytest.raises(settings_file.SettingsError, match="symlink"):
        settings_file.write_atomic(path, "{}\n")
    with pytest.raises(settings_file.SettingsError, match="symlink"):
        settings_file.read(path)
    assert list(claude_dir.iterdir()) == []


@pytest.mark.parametrize("content", ['["not", "an", "object"]', "42", "{not json"])
def test_a_settings_file_that_is_not_an_object_is_refused_with_no_write(
    initialised: Dirs, content: str
) -> None:
    initialised.settings.parent.mkdir()
    initialised.settings.write_text(content)
    with pytest.raises(cli.CliError):
        install(initialised)
    assert initialised.settings.read_text() == content
    assert not paths.hook_path(initialised.bridge_home).exists()


def test_a_symlinked_settings_file_is_refused(
    initialised: Dirs, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}")
    initialised.settings.parent.mkdir()
    initialised.settings.symlink_to(elsewhere)
    with pytest.raises(cli.CliError, match="symlink"):
        install(initialised)
    assert elsewhere.read_text() == "{}"


def test_dry_run_prints_the_diff_and_writes_nothing_for_an_absent_file(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    config_before = paths.config_path(initialised.bridge_home).read_bytes()
    assert install(initialised, dry_run=True) == 0
    out = capsys.readouterr().out

    absolute = str(initialised.settings.absolute())
    assert out.splitlines()[:2] == [f"--- {absolute}", f"+++ {absolute}"]
    assert str(initialised.enrolled) in out.splitlines()[0]
    for event in ("Stop", "SessionEnd"):
        # As the JSON in the diff spells it (its double quotes escaped).
        assert json.dumps(expected_command(initialised, event))[1:-1] in out
    assert not initialised.settings.exists()
    assert not initialised.settings.parent.exists()
    assert not paths.hook_path(initialised.bridge_home).exists()
    assert paths.config_path(initialised.bridge_home).read_bytes() == config_before


def test_dry_run_leaves_an_existing_file_byte_for_byte(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    initialised.settings.parent.mkdir()
    initialised.settings.write_text(json.dumps(EXISTING, indent=2) + "\n")
    before = initialised.settings.read_bytes()
    assert install(initialised, dry_run=True) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"--- {initialised.settings.absolute()}\n")
    assert "+" in out and initialised.settings.read_bytes() == before


def test_install_into_another_directory_is_refused(
    initialised: Dirs, tmp_path: Path
) -> None:
    other = tmp_path / "home" / "code" / "another-project"
    other.mkdir(parents=True)
    with pytest.raises(cli.CliError, match="not the enrolled directory"):
        install(initialised, enrolled=other)
    assert not settings_file.settings_path(other).exists()
    assert not initialised.settings.exists()
    assert not paths.hook_path(initialised.bridge_home).exists()


def _checkout_enrolled(tmp_path: Path, *, ignore: bool) -> Dirs:
    """A synthetic checkout, with its own ``git init``, as the enrolled dir."""
    d = make_dirs(tmp_path)
    git(d.enrolled, "init", "-q")
    if ignore:
        (d.enrolled / ".gitignore").write_text(".claude/\n")
    assert (
        cli.init(
            d.bridge_home,
            enrolled_dir=d.enrolled,
            api_url=API_URL,
            executable=VENV_PYTHON,
        )
        == 0
    )
    return d


def test_install_in_a_git_tree_that_does_not_ignore_the_file_is_refused(
    tmp_path: Path,
) -> None:
    d = _checkout_enrolled(tmp_path, ignore=False)
    with pytest.raises(cli.CliError, match="not ignored"):
        install(d)
    assert not d.settings.exists()
    assert not paths.hook_path(d.bridge_home).exists()
    # The dry run is refused too: gate A never shows a diff it would refuse.
    with pytest.raises(cli.CliError, match="not ignored"):
        install(d, dry_run=True)


def test_install_in_a_git_tree_that_ignores_the_file_succeeds(tmp_path: Path) -> None:
    d = _checkout_enrolled(tmp_path, ignore=True)
    assert install(d) == 0
    assert len(our_commands(load_json(d.settings))) == 2


def test_install_refuses_a_tracked_settings_file_even_when_ignored(
    tmp_path: Path,
) -> None:
    d = _checkout_enrolled(tmp_path, ignore=True)
    d.settings.parent.mkdir()
    d.settings.write_text("{}\n")
    git(d.enrolled, "add", "-f", str(settings_file.SETTINGS_RELATIVE))
    with pytest.raises(cli.CliError, match="not ignored"):
        install(d)
    assert d.settings.read_text() == "{}\n"


# --- remove-hook ----------------------------------------------------------------


def test_remove_deletes_only_ours_and_the_file_install_created(
    initialised: Dirs,
) -> None:
    assert install(initialised) == 0
    assert (
        cli.remove_hook(initialised.bridge_home, enrolled_dir=initialised.enrolled) == 0
    )
    assert not initialised.settings.exists()
    assert config_of(initialised).hook_created_settings is False


def test_remove_keeps_every_other_key_and_hook(initialised: Dirs) -> None:
    initialised.settings.parent.mkdir()
    initialised.settings.write_text(json.dumps(EXISTING))
    assert install(initialised) == 0
    assert (
        cli.remove_hook(initialised.bridge_home, enrolled_dir=initialised.enrolled) == 0
    )
    assert load_json(initialised.settings) == EXISTING


@pytest.mark.parametrize("indent,newline", [(4, "\n"), ("\t", "\r\n")])
def test_remove_restores_detectable_formatting(
    initialised: Dirs, indent: int | str, newline: str
) -> None:
    initialised.settings.parent.mkdir()
    original = (
        json.dumps(EXISTING, indent=indent, ensure_ascii=False).replace("\n", newline)
        + newline
    )
    initialised.settings.write_bytes(original.encode("utf-8"))
    assert install(initialised) == 0
    assert (
        cli.remove_hook(initialised.bridge_home, enrolled_dir=initialised.enrolled) == 0
    )
    assert initialised.settings.read_bytes() == original.encode("utf-8")


@pytest.mark.parametrize("created", [False, True])
def test_remove_refuses_a_settings_file_changed_after_read(
    initialised: Dirs, monkeypatch: pytest.MonkeyPatch, created: bool
) -> None:
    if not created:
        initialised.settings.parent.mkdir()
        initialised.settings.write_text(json.dumps(EXISTING))
    assert install(initialised) == 0
    changed = initialised.settings.read_bytes() + b" \n"
    real_read = settings_file.read
    reads = 0

    def concurrent_read(path: Path) -> tuple[settings_file.Document, str | None]:
        nonlocal reads
        reads += 1
        if reads == 2:
            path.write_bytes(changed)
        return real_read(path)

    monkeypatch.setattr(settings_file, "read", concurrent_read)
    with pytest.raises(cli.CliError, match="changed since it was read"):
        cli.remove_hook(initialised.bridge_home, enrolled_dir=initialised.enrolled)
    assert initialised.settings.read_bytes() == changed
    assert config_of(initialised).hook_created_settings is created


@pytest.mark.parametrize(
    "original",
    [{"permissions": {"allow": ["Bash(ls:*)"]}}, {}],
    ids=["other-content", "empty-object"],
)
def test_a_file_that_pre_existed_is_never_deleted(
    initialised: Dirs, original: dict[str, Any]
) -> None:
    initialised.settings.parent.mkdir()
    initialised.settings.write_text(json.dumps(original))
    assert install(initialised) == 0
    # Our entries are the only hooks in the file now.
    assert set(load_json(initialised.settings)["hooks"]) == {"Stop", "SessionEnd"}
    assert (
        cli.remove_hook(initialised.bridge_home, enrolled_dir=initialised.enrolled) == 0
    )
    assert initialised.settings.exists()
    assert load_json(initialised.settings) == original


def test_remove_with_no_settings_file_is_a_no_op(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli.remove_hook(initialised.bridge_home, enrolled_dir=initialised.enrolled) == 0
    )
    assert "nothing to remove" in capsys.readouterr().out
    assert not initialised.settings.parent.exists()


# --- AC 15: no hook installed means nothing is spooled or sent -----------------


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    def post(
        self,
        machine_fingerprint: str,
        project_fingerprint: str,
        records: Sequence[IngestRecord],
        gaps: Sequence[IngestGap],
    ) -> IngestResponse:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return IngestResponse(
            accepted=tuple(r.native_key for r in records),
            deferred=(),
            gapped=tuple(g.native_key for g in gaps),
            dropped=(),
        )


def human_line(text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "origin": {"kind": "human"},
        "sessionId": SESSION_ID,
        "uuid": str(uuid.uuid4()),
        "timestamp": (NOW - timedelta(minutes=5)).isoformat(),
        "entrypoint": "cli",
        "message": {"role": "user", "content": text},
    }


def write_transcript(d: Dirs, name: str = "session-0001.jsonl") -> Path:
    slug_dir = d.projects_root / transcript.slug(str(d.enrolled))
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / name
    lines = [human_line("please plan the example task"), human_line("thanks")]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def run_drain(d: Dirs, client: FakeClient) -> int:
    return cli.drain(
        d.bridge_home, projects_root=d.projects_root, client=client, now=lambda: NOW
    )


def _assert_inert(d: Dirs, client: FakeClient) -> None:
    if d.settings.exists():
        assert our_commands(load_json(d.settings)) == []
    spool = paths.spool_dir(d.bridge_home)
    assert not spool.exists() or list(spool.iterdir()) == []
    connection = state.connect(paths.state_path(d.bridge_home))
    try:
        assert state.list_sessions(connection, include_closed=True) == []
    finally:
        state.close(connection)
    assert client.calls == 0


@pytest.mark.parametrize("history", ["never-installed", "installed-then-removed"])
def test_no_hook_installed_means_nothing_is_spooled_or_sent(
    initialised: Dirs, history: str
) -> None:
    set_token(initialised, new_token())
    if history == "installed-then-removed":
        assert install(initialised) == 0
        assert (
            cli.remove_hook(initialised.bridge_home, enrolled_dir=initialised.enrolled)
            == 0
        )
    write_transcript(initialised)
    client = FakeClient()

    assert run_drain(initialised, client) == 0
    # The worker reads only hook-named files; it never crawls projects_root.
    _assert_inert(initialised, client)


# --- set-token clears the hold (a rotate resumes the worker) --------------------


def spool_session(d: Dirs, transcript_path: Path) -> None:
    spool = paths.spool_dir(d.bridge_home)
    spool.mkdir(mode=0o700, exist_ok=True)
    day = spool / "2026-09-29.jsonl"
    lines = [
        {
            "v": 1,
            "event": event,
            "session_hash": "5e55" * 8,
            "transcript_path": str(transcript_path),
            "at": NOW_TS - 60,
        }
        for event in ("Stop", "SessionEnd")
    ]
    day.write_text("".join(json.dumps(line) + "\n" for line in lines))
    os.chmod(day, 0o600)


def test_a_rotate_clears_the_hold_so_the_next_drain_posts(initialised: Dirs) -> None:
    enrollment_id = str(uuid.uuid4())
    set_token(initialised, new_token(), enrollment_id=enrollment_id)
    spool_session(initialised, write_transcript(initialised))
    connection = state.connect(paths.state_path(initialised.bridge_home))
    try:
        # A refused token earned a long back-off.
        state.set_hold(connection, hold_until=NOW_TS + 6 * 3600, hold_step=4)
    finally:
        state.close(connection)

    held = FakeClient()
    assert run_drain(initialised, held) == 0
    assert held.calls == 0

    set_token(initialised, new_token(), enrollment_id=enrollment_id)
    connection = state.connect(paths.state_path(initialised.bridge_home))
    try:
        assert state.get_hold(connection) == state.NO_HOLD
    finally:
        state.close(connection)

    resumed = FakeClient()
    assert run_drain(initialised, resumed) == 0
    assert resumed.calls >= 1


def test_set_token_before_any_drain_creates_no_state_file(initialised: Dirs) -> None:
    set_token(initialised, new_token())
    assert not paths.state_path(initialised.bridge_home).exists()


# --- drain exit codes -----------------------------------------------------------


def test_drain_passes_a_refusal_through_as_exit_2(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    set_token(initialised, new_token())
    spool_session(initialised, write_transcript(initialised))
    client = FakeClient(IngestRefused("token_revoked", 401))
    assert run_drain(initialised, client) == 2
    assert client.calls == 1
    assert "refused" in capsys.readouterr().err


def test_drain_passes_a_worker_failure_through_as_exit_1(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    set_token(initialised, new_token())
    os.chmod(initialised.bridge_home, 0o755)
    client = FakeClient()
    assert run_drain(initialised, client) == 1
    assert client.calls == 0
    assert "could not run" in capsys.readouterr().err


def test_drain_without_a_token_refuses(initialised: Dirs) -> None:
    with pytest.raises(cli.CliError, match="no token"):
        cli.drain(initialised.bridge_home, projects_root=initialised.projects_root)


# --- status and uninstall -------------------------------------------------------

TRANSCRIPT_MARK = "synthetic-transcript-marker"
HASHES = {
    "open_a": "a1" * 16,
    "open_b": "b2" * 16,
    "closed_clean": "c3" * 16,
    "closed_gap": "d4" * 16,
}


def seed_sessions(d: Dirs) -> None:
    """Two open sessions and two tombstones, one still owing a gap."""
    connection = state.connect(paths.state_path(d.bridge_home))
    try:
        for name, session_hash in HASHES.items():
            state.upsert_session(
                connection,
                session_hash,
                transcript_path=f"/tmp/{TRANSCRIPT_MARK}/{name}.jsonl",
                seen_at=NOW_TS - 600,
            )
        state.close_session(connection, HASHES["closed_clean"], at=NOW_TS - 300)
        state.set_pending_gap_key(connection, HASHES["closed_gap"], "cc1g:" + "e5" * 32)
        state.close_session(connection, HASHES["closed_gap"], at=NOW_TS - 300)
        state.set_pending_gap_key(connection, HASHES["open_b"], "cc1g:" + "f6" * 32)
        state.bump_ledger(connection, "accepted:cli", at=NOW_TS - 120, by=3)
        state.bump_ledger(connection, "accepted:claude-desktop", at=NOW_TS - 60, by=1)
        state.bump_ledger(connection, "project_unmatched", at=NOW_TS - 30)
        state.set_hold(connection, hold_until=NOW_TS + 900, hold_step=1)
    finally:
        state.close(connection)


def test_status_prints_counts_and_the_enrollment_only(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    enrollment_id = str(uuid.uuid4())
    expires = NOW + timedelta(days=20, hours=3)
    set_token(initialised, new_token(), enrollment_id=enrollment_id, expires=expires)
    seed_sessions(initialised)
    capsys.readouterr()

    assert cli.status(initialised.bridge_home, now=NOW) == 0
    out = capsys.readouterr().out
    lines = dict(line.split(": ", 1) for line in out.splitlines())

    # The two non-count lines, for CP-A's verify step.
    assert lines.pop("enrollment_id") == enrollment_id
    assert lines.pop("token_expires_at") == expires.isoformat()

    assert lines["sessions_pending"] == "2"
    assert lines["sessions_held"] == "2"
    assert lines["sessions_closed"] == "2"
    assert lines["hold_until"] == datetime.fromtimestamp(NOW_TS + 900, UTC).isoformat()
    assert lines["ledger.accepted:cli"] == "3"
    assert lines["ledger.accepted:claude-desktop"] == "1"
    assert lines["ledger.project_unmatched"] == "1"
    assert (
        lines["last_accepted_at"]
        == datetime.fromtimestamp(NOW_TS - 60, UTC).isoformat()
    )
    assert lines["token_days_remaining"] == "20"
    assert lines["bridge_home_private"] == "yes"
    assert lines["hook_runnable"] == "no"  # no hook installed in this fixture
    value = re.compile(r"-?\d+|none|unknown|yes|no|\d{4}-\d{2}-\d{2}T[\d:]+\+00:00")
    assert all(value.fullmatch(v) for v in lines.values()), lines
    for session_hash in HASHES.values():
        assert session_hash not in out
    assert TRANSCRIPT_MARK not in out and "cc1g:" not in out
    assert str(initialised.enrolled) not in out


def test_status_with_no_state_reports_zeroes_and_creates_nothing(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.status(initialised.bridge_home, now=NOW) == 0
    out = capsys.readouterr().out
    assert "sessions_pending: 0\n" in out
    assert "token_days_remaining: unknown\n" in out
    assert not paths.state_path(initialised.bridge_home).exists()


def test_uninstall_purge_prints_the_unacknowledged_count_before_deleting(
    initialised: Dirs,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrollment_id = str(uuid.uuid4())
    set_token(initialised, new_token(), enrollment_id=enrollment_id)
    assert install(initialised) == 0
    seed_sessions(initialised)
    capsys.readouterr()

    printed_before_delete: list[str] = []
    real_rmtree = shutil.rmtree

    def watching_rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        assert Path(path) == initialised.bridge_home
        assert Path(path).is_dir()
        printed_before_delete.append(capsys.readouterr().out)
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", watching_rmtree)
    assert (
        cli.uninstall(
            initialised.bridge_home, enrolled_dir=initialised.enrolled, purge=True
        )
        == 0
    )
    after = capsys.readouterr().out

    # Open sessions a and b, plus the tombstone still owing its gap; the clean
    # tombstone's range is acknowledged.
    assert len(printed_before_delete) == 1
    assert "discarded_unacknowledged: 3\n" in printed_before_delete[0]
    assert not initialised.bridge_home.exists()
    assert not initialised.settings.exists()
    assert f"rheo evidence revoke {enrollment_id}" in after


def test_uninstall_purge_refuses_while_a_worker_runs(initialised: Dirs) -> None:
    import fcntl

    fd = os.open(
        paths.worker_lock_path(initialised.bridge_home), os.O_RDWR | os.O_CREAT
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(cli.CliError, match="worker is running"):
            cli.uninstall(
                initialised.bridge_home, enrolled_dir=initialised.enrolled, purge=True
            )
    finally:
        os.close(fd)
    assert paths.machine_key_path(initialised.bridge_home).exists()


def test_uninstall_without_purge_keeps_bridge_home_and_pending_state(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    enrollment_id = str(uuid.uuid4())
    set_token(initialised, new_token(), enrollment_id=enrollment_id)
    assert install(initialised) == 0
    seed_sessions(initialised)
    capsys.readouterr()

    assert (
        cli.uninstall(initialised.bridge_home, enrolled_dir=initialised.enrolled) == 0
    )
    out = capsys.readouterr().out

    assert not paths.token_path(initialised.bridge_home).exists()
    assert not paths.machine_key_path(initialised.bridge_home).exists()
    assert initialised.bridge_home.is_dir()
    assert not initialised.settings.exists()
    connection = state.connect(paths.state_path(initialised.bridge_home))
    try:
        assert len(state.list_sessions(connection, include_closed=True)) == 4
    finally:
        state.close(connection)
    assert "discarded_unacknowledged" not in out
    assert f"rheo evidence revoke {enrollment_id}" in out


def test_uninstall_purge_counts_sessions_only_the_spool_names_yet(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    set_token(initialised, new_token())
    # The hook named a session, but no worker has folded the spool yet.
    spool_session(initialised, write_transcript(initialised))
    assert not paths.state_path(initialised.bridge_home).exists()
    capsys.readouterr()

    assert (
        cli.uninstall(
            initialised.bridge_home,
            enrolled_dir=initialised.enrolled,
            purge=True,
            now=lambda: NOW,
        )
        == 0
    )
    assert "discarded_unacknowledged: 1\n" in capsys.readouterr().out
    assert not initialised.bridge_home.exists()


# --- the token file is read whole or not at all ---------------------------------


def test_an_oversize_token_file_is_refused_not_truncated(initialised: Dirs) -> None:
    set_token(initialised, new_token())
    paths.token_path(initialised.bridge_home).write_bytes(
        b"a" * (cli.SET_TOKEN_MAX_BYTES + 10)
    )
    with pytest.raises(cli.CliError, match="longer than"):
        cli.drain(initialised.bridge_home, projects_root=initialised.projects_root)


# --- main() dispatch ------------------------------------------------------------


def test_main_dispatches_refusals_to_1_and_passes_drain_codes_through(
    dirs: Dirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # main() is the one place that resolves the home: point it at a tmp root.
    monkeypatch.setattr(Path, "home", lambda: dirs.user_home)

    assert cli.main(["status"]) == 1
    assert "rheo-bridge status: the bridge is not set up" in capsys.readouterr().err

    assert (
        cli.main(["init", "--enrolled-dir", str(dirs.enrolled), "--api-url", API_URL])
        == 0
    )
    assert config_of(dirs).enrolled_dir == str(dirs.enrolled)

    token = new_token()
    line = token_line(token, str(uuid.uuid4()), NOW + timedelta(days=90))
    monkeypatch.setattr("sys.stdin", io.StringIO(line))
    assert cli.main(["set-token"]) == 0
    capsys.readouterr()

    spool_session(dirs, write_transcript(dirs))
    built: list[tuple[str, str]] = []

    def fake_client(base_url: str, token_value: str) -> FakeClient:
        built.append((base_url, token_value))
        return FakeClient(IngestRefused("token_revoked", 401))

    monkeypatch.setattr(cli, "HttpIngestClient", fake_client)
    assert cli.main(["drain"]) == 2
    assert built == [(API_URL, token)]
    assert "refused" in capsys.readouterr().err


# --- a corrupt config.json never blocks uninstall -------------------------------


@pytest.mark.parametrize("purge", [False, True], ids=["plain", "purge"])
def test_uninstall_with_a_corrupt_config_still_removes_hooks_and_credentials(
    initialised: Dirs, capsys: pytest.CaptureFixture[str], purge: bool
) -> None:
    set_token(initialised, new_token())
    assert install(initialised) == 0
    paths.config_path(initialised.bridge_home).write_text("{not json")
    capsys.readouterr()

    assert (
        cli.uninstall(
            initialised.bridge_home,
            enrolled_dir=initialised.enrolled,
            purge=purge,
            now=lambda: NOW,
        )
        == 0
    )
    out = capsys.readouterr().out

    # With no usable record, only our entries go: no container, no file.
    assert load_json(initialised.settings) == {"hooks": {"Stop": [], "SessionEnd": []}}
    assert "rheo evidence revoke <ENROLLMENT_ID>" in out
    if purge:
        assert "discarded_unacknowledged: 0\n" in out
        assert not initialised.bridge_home.exists()
    else:
        assert not paths.token_path(initialised.bridge_home).exists()
        assert not paths.machine_key_path(initialised.bridge_home).exists()


def test_remove_hook_alone_still_refuses_a_corrupt_config(initialised: Dirs) -> None:
    assert install(initialised) == 0
    paths.config_path(initialised.bridge_home).write_text("[]")
    with pytest.raises(cli.CliError):
        cli.remove_hook(initialised.bridge_home, enrolled_dir=initialised.enrolled)
    assert len(our_commands(load_json(initialised.settings))) == 2


# --- an installed hook never outlives its script into a loop --------------------


@pytest.mark.parametrize("purge", [False, True], ids=["plain", "purge"])
def test_uninstall_for_another_directory_is_refused_and_removes_nothing(
    initialised: Dirs, tmp_path: Path, purge: bool
) -> None:
    set_token(initialised, new_token())
    assert install(initialised) == 0
    other = tmp_path / "home" / "code" / "another-project"
    other.mkdir(parents=True)
    settings_before = initialised.settings.read_bytes()

    with pytest.raises(cli.CliError, match="not the enrolled directory"):
        cli.uninstall(
            initialised.bridge_home, enrolled_dir=other, purge=purge, now=lambda: NOW
        )

    assert initialised.settings.read_bytes() == settings_before
    assert paths.token_path(initialised.bridge_home).exists()
    assert paths.machine_key_path(initialised.bridge_home).exists()
    assert paths.hook_path(initialised.bridge_home).exists()


def test_remove_hook_for_another_directory_is_refused(
    initialised: Dirs, tmp_path: Path
) -> None:
    other = tmp_path / "home" / "code" / "another-project"
    (other / settings_file.SETTINGS_RELATIVE).parent.mkdir(parents=True)
    foreign = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "/opt/x/python /opt/y/.rheo-bridge/hook.py Stop"
                            ),
                        }
                    ]
                }
            ]
        }
    }
    settings_file.settings_path(other).write_text(json.dumps(foreign))
    with pytest.raises(cli.CliError, match="not the enrolled directory"):
        cli.remove_hook(initialised.bridge_home, enrolled_dir=other)
    assert load_json(settings_file.settings_path(other)) == foreign


def run_hook_command(
    command: str, payload: bytes
) -> subprocess.CompletedProcess[bytes]:
    """Run a settings command the way Claude Code does: through ``/bin/sh -c``."""
    return subprocess.run(
        command, shell=True, input=payload, capture_output=True, timeout=60, check=False
    )


def hook_payload() -> bytes:
    return json.dumps(
        {"session_id": SESSION_ID, "transcript_path": "/tmp/rheo-synthetic/t.jsonl"}
    ).encode()


def installed_commands(d: Dirs) -> dict[str, str]:
    document = load_json(d.settings)
    return {
        event: document["hooks"][event][-1]["hooks"][0]["command"]
        for event in ("Stop", "SessionEnd")
    }


def _real_interpreter() -> str:
    import sys

    return os.path.realpath(sys.executable)


@pytest.mark.parametrize("space", [False, True], ids=["plain", "path-with-space"])
def test_the_installed_command_runs_the_hook_and_spools_a_line(
    tmp_path: Path, space: bool
) -> None:
    d = make_dirs(tmp_path / "with space" if space else tmp_path)
    assert (
        cli.init(
            d.bridge_home,
            enrolled_dir=d.enrolled,
            api_url=API_URL,
            executable=VENV_PYTHON,
        )
        == 0
    )
    assert (
        cli.install_hook(
            d.bridge_home, enrolled_dir=d.enrolled, interpreter=_real_interpreter()
        )
        == 0
    )
    for command in installed_commands(d).values():
        completed = run_hook_command(command, hook_payload())
        assert (completed.returncode, completed.stdout, completed.stderr) == (
            0,
            b"",
            b"",
        )
    spooled = [
        json.loads(line)
        for path in sorted(paths.spool_dir(d.bridge_home).glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]
    assert [line["event"] for line in spooled] == ["Stop", "SessionEnd"]
    assert all("session_hash" in line for line in spooled)


def test_the_installed_command_exits_0_silently_when_the_script_is_gone(
    initialised: Dirs,
) -> None:
    assert (
        cli.install_hook(
            initialised.bridge_home,
            enrolled_dir=initialised.enrolled,
            interpreter=_real_interpreter(),
        )
        == 0
    )
    commands = installed_commands(initialised)
    # A manual `rm -rf ~/.rheo-bridge`: the settings entry outlives the script.
    shutil.rmtree(initialised.bridge_home)
    for command in commands.values():
        completed = run_hook_command(command, hook_payload())
        assert (completed.returncode, completed.stdout, completed.stderr) == (
            0,
            b"",
            b"",
        )
    assert not initialised.bridge_home.exists()


def test_the_installed_command_exits_0_silently_when_the_interpreter_is_gone(
    initialised: Dirs,
) -> None:
    missing = "/opt/rheo-synthetic/pruned/python3.12"
    assert not os.path.exists(missing)
    assert (
        cli.install_hook(
            initialised.bridge_home,
            enrolled_dir=initialised.enrolled,
            interpreter=missing,
        )
        == 0
    )
    assert paths.hook_path(initialised.bridge_home).is_file()
    for command in installed_commands(initialised).values():
        completed = run_hook_command(command, hook_payload())
        assert (completed.returncode, completed.stdout, completed.stderr) == (
            0,
            b"",
            b"",
        )
    assert not paths.spool_dir(initialised.bridge_home).exists()


def test_a_reinstall_replaces_the_old_bare_python_form(initialised: Dirs) -> None:
    hook = initialised.bridge_home / "hook.py"
    old = {
        "hooks": {
            event: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{INTERPRETER} {hook} {event}",
                            "timeout": 5,
                        }
                    ]
                }
            ]
            for event in ("Stop", "SessionEnd")
        }
    }
    initialised.settings.parent.mkdir()
    initialised.settings.write_text(json.dumps(old))
    assert install(initialised) == 0
    document = load_json(initialised.settings)
    for event in ("Stop", "SessionEnd"):
        commands = [
            entry["command"]
            for group in document["hooks"][event]
            for entry in group["hooks"]
        ]
        assert commands == [expected_command(initialised, event)]


# --- OS litter does not stop the worker; a real privacy failure shows ----------


def test_finder_litter_does_not_stop_the_worker(initialised: Dirs) -> None:
    set_token(initialised, new_token())
    spool_session(initialised, write_transcript(initialised))
    for litter in (
        initialised.bridge_home / ".DS_Store",
        paths.spool_dir(initialised.bridge_home) / ".DS_Store",
        paths.spool_dir(initialised.bridge_home) / "._2026-09-29.jsonl",
    ):
        litter.write_bytes(b"\x00\x00\x00\x01Bud1")
        os.chmod(litter, 0o644)
    paths.check_private(initialised.bridge_home)
    client = FakeClient()
    assert run_drain(initialised, client) == 0
    assert client.calls >= 1


def test_a_loose_bridge_file_is_named_by_drain_and_status(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    set_token(initialised, new_token())
    os.chmod(paths.token_path(initialised.bridge_home), 0o644)
    capsys.readouterr()

    client = FakeClient()
    assert run_drain(initialised, client) == 1
    assert client.calls == 0
    err = capsys.readouterr().err
    assert "not private" in err and "token: file mode 0644" in err

    assert cli.status(initialised.bridge_home, now=NOW) == 0
    captured = capsys.readouterr()
    assert "bridge_home_private: no\n" in captured.out
    assert "token: file mode 0644" in captured.err


def test_the_installed_command_exits_0_silently_when_the_script_is_unreadable(
    initialised: Dirs,
) -> None:
    assert (
        cli.install_hook(
            initialised.bridge_home,
            enrolled_dir=initialised.enrolled,
            interpreter=_real_interpreter(),
        )
        == 0
    )
    script = paths.hook_path(initialised.bridge_home)
    os.chmod(script, 0o000)
    try:
        for command in installed_commands(initialised).values():
            completed = run_hook_command(command, hook_payload())
            assert (completed.returncode, completed.stdout, completed.stderr) == (
                0,
                b"",
                b"",
            )
    finally:
        os.chmod(script, 0o600)
    assert not paths.spool_dir(initialised.bridge_home).exists()


# --- status: hook_runnable, and a vanishing journal is not a privacy failure ---


def _status_lines(d: Dirs, capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    capsys.readouterr()
    assert cli.status(d.bridge_home, now=NOW) == 0
    captured = capsys.readouterr()
    assert str(d.user_home) not in captured.out + captured.err
    return dict(line.split(": ", 1) for line in captured.out.splitlines())


def test_status_reports_whether_the_installed_hook_can_run(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _status_lines(initialised, capsys)["hook_runnable"] == "no"  # not installed

    assert (
        cli.install_hook(
            initialised.bridge_home,
            enrolled_dir=initialised.enrolled,
            interpreter=_real_interpreter(),
        )
        == 0
    )
    assert _status_lines(initialised, capsys)["hook_runnable"] == "yes"

    script = paths.hook_path(initialised.bridge_home)
    os.chmod(script, 0o000)
    try:
        assert _status_lines(initialised, capsys)["hook_runnable"] == "no"
    finally:
        os.chmod(script, 0o600)
    script.unlink()
    assert _status_lines(initialised, capsys)["hook_runnable"] == "no"


def test_status_says_no_for_a_pruned_interpreter(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli.install_hook(
            initialised.bridge_home,
            enrolled_dir=initialised.enrolled,
            interpreter="/opt/rheo-synthetic/pruned/python3.12",
        )
        == 0
    )
    assert _status_lines(initialised, capsys)["hook_runnable"] == "no"


def test_status_reads_an_old_bare_form_install(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = paths.hook_path(initialised.bridge_home)
    hook.write_bytes(Path(cli.hook.__file__).read_bytes())
    os.chmod(hook, 0o600)
    bare = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{_real_interpreter()} {hook} Stop",
                        }
                    ]
                }
            ]
        }
    }
    initialised.settings.parent.mkdir()
    initialised.settings.write_text(json.dumps(bare))
    assert _status_lines(initialised, capsys)["hook_runnable"] == "yes"


def test_an_entry_that_vanishes_mid_check_is_not_a_privacy_failure(
    initialised: Dirs,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = initialised.bridge_home / "state.sqlite-journal"
    journal.write_bytes(b"")
    os.chmod(journal, 0o644)  # would fail the mode check if it were looked at
    real_lstat = os.lstat

    def racing_lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.path.basename(path) == journal.name:
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", racing_lstat)
    paths.check_private(initialised.bridge_home)
    assert _status_lines(initialised, capsys)["bridge_home_private"] == "yes"


def test_status_never_prints_a_full_path_for_a_privacy_failure(
    initialised: Dirs, capsys: pytest.CaptureFixture[str]
) -> None:
    set_token(initialised, new_token())
    os.chmod(paths.token_path(initialised.bridge_home), 0o644)
    capsys.readouterr()
    assert cli.status(initialised.bridge_home, now=NOW) == 0
    captured = capsys.readouterr()
    assert "bridge_home_private: no\n" in captured.out
    assert "token: file mode 0644" in captured.err
    assert str(initialised.bridge_home) not in captured.out + captured.err

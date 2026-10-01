"""The enrolled directory's Claude Code settings file, read and edited safely.

``install-hook`` and ``remove-hook`` edit ``<enrolled_dir>/.claude/settings.local.json``
through the functions here, and nothing else writes it. The rules:

- An absent file is ``{}``. A file that is not UTF-8 JSON, or whose top-level
  value is not an object, is refused before anything is written; so is a
  symlink, which a rename would silently replace with a regular file.
- :func:`merge` and :func:`remove` return a new document and never mutate the
  one passed in. Everything this tool did not add keeps its meaning: other
  keys, other events and other hooks. ``merge`` reports which containers it
  created, and ``remove`` drops an emptied container only if it did, so an
  install followed by a removal gives back the file as it was.
- A symlinked file, a symlinked ``.claude/``, or any path that resolves
  elsewhere is refused before reading or writing.
- :func:`write_atomic` writes a temp file beside the target, fsyncs it and
  renames it over the target, so a crash leaves the old file or the new one,
  never a truncated one.
- :func:`unified_diff` renders the change headed by the file's absolute path.
  ``install-hook --dry-run`` prints it, and gate A shows it unmodified.

The settings shape is Claude Code's: ``{"hooks": {"<Event>": [<group>]}}``, where
a matcher group is ``{"hooks": [{"type": "command", "command": ..., "timeout": 5}]}``.
``Stop`` and ``SessionEnd`` take no matcher, so the groups written here carry none.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import re
import shlex
import stat
import tempfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SETTINGS_RELATIVE: Final = Path(".claude") / "settings.local.json"
POSIX_SH: Final = "/bin/sh"
# ``remove`` and ``merge`` treat a hook entry as the bridge's when its command
# names this. That matches the ``/bin/sh`` wrapper and the older bare
# ``<python> .../hook.py <event>`` form alike, whatever interpreter wrote it,
# so a reinstall replaces either.
HOOK_MARKER: Final = ".rheo-bridge/hook.py"
HOOK_EVENTS: Final = ("Stop", "SessionEnd")
# [capture 6] — may be revised at reconciliation.
HOOK_TIMEOUT_SECONDS: Final = 5
NEW_FILE_MODE: Final = 0o600
# A settings file larger than this is not one a person wrote by hand.
MAX_SETTINGS_BYTES: Final = 4 * 1024 * 1024

Document = dict[str, object]


class SettingsError(ValueError):
    """The settings file cannot be edited safely; nothing was written."""


def settings_path(enrolled_dir: Path) -> Path:
    return enrolled_dir / SETTINGS_RELATIVE


def hook_command(interpreter: str, hook_script: Path, event: str) -> str:
    """The settings command: run the hook only if both of its paths still exist.

    ``/bin/sh -c '<guard>' <interpreter> <hook_script>``. The guard execs the
    interpreter on the script when the interpreter is executable and the
    script is a readable file, and otherwise exits 0 (an unreadable script
    would make Python exit 2 too). A bare ``python hook.py`` would
    exit 2 on a missing script (and 127 on a missing interpreter), and Claude
    Code reads exit 2 from a ``Stop`` hook as "keep going", so a hook that
    outlived ``bridge_home`` or a pruned interpreter would loop every turn.
    The two paths arrive as ``$0`` and ``$1``, never inside the guard's text,
    so a path with spaces needs no quoting there; the outer command is
    shell-quoted part by part. ``event`` is one of :data:`HOOK_EVENTS`.
    """
    if event not in HOOK_EVENTS:
        raise ValueError(f"not a hook event: {event!r}")
    guard = (
        f'[ -x "$0" ] && [ -f "$1" ] && [ -r "$1" ] && exec "$0" "$1" {event}; exit 0'
    )
    return shlex.join([POSIX_SH, "-c", guard, interpreter, str(hook_script)])


def check_location(path: Path) -> None:
    """Refuse a settings path that resolves anywhere but itself.

    A symlinked ``.claude/`` (or any symlink on the way) would send the write
    to another directory. Outside a Git tree the ignore check does not apply,
    so this is the guard that keeps the edit inside the enrolled directory.
    """
    if path.parent.is_symlink():
        raise SettingsError(f"{path.parent} is a symlink; refusing to edit through it")
    if os.path.realpath(path) != str(path.absolute()):
        raise SettingsError(
            f"{path} resolves elsewhere through a symlink; refusing to edit it"
        )


def read(path: Path) -> tuple[Document, str | None]:
    """Parse the settings file: ``(document, original text)``.

    The text is ``None`` when the file is absent, and the document is then
    ``{}``.
    """
    check_location(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}, None
    if stat.S_ISLNK(info.st_mode):
        raise SettingsError(f"{path} is a symlink; refusing to edit it")
    if not stat.S_ISREG(info.st_mode):
        raise SettingsError(f"{path} is not a regular file")
    if info.st_size > MAX_SETTINGS_BYTES:
        raise SettingsError(f"{path} is larger than {MAX_SETTINGS_BYTES} bytes")
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            text = handle.read()
    except UnicodeDecodeError as exc:
        raise SettingsError(f"{path} is not UTF-8") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"{path} is not valid JSON") from exc
    if not isinstance(document, dict):
        raise SettingsError(f"{path}: the top-level JSON value is not an object")
    return document, text


def _hooks_table(document: Document) -> dict[str, object] | None:
    hooks = document.get("hooks")
    if hooks is None:
        return None
    if not isinstance(hooks, dict):
        raise SettingsError('the settings file\'s "hooks" value is not an object')
    return hooks


def _commands(groups: list[object]) -> list[str]:
    found: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        entries = group.get("hooks")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("command"), str):
                found.append(entry["command"])
    return found


def names_bridge_hook(command: str) -> bool:
    return HOOK_MARKER in command


def bridge_commands(document: Document) -> list[str]:
    """Every hook command in the document, under any event."""
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return []
    found: list[str] = []
    for groups in hooks.values():
        if isinstance(groups, list):
            found.extend(_commands(groups))
    return found


def _strip(
    groups: list[object], is_ours: Callable[[str], bool]
) -> tuple[list[object], bool]:
    """``groups`` without the entries whose command ``is_ours``.

    A group this emptied is dropped; any other group is kept as it was.
    Returns the new list and whether anything was removed.
    """
    kept_groups: list[object] = []
    changed = False
    for group in groups:
        entries = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(group, dict) or not isinstance(entries, list):
            kept_groups.append(group)
            continue
        kept = [
            entry
            for entry in entries
            if not (
                isinstance(entry, dict)
                and isinstance(entry.get("command"), str)
                and is_ours(entry["command"])
            )
        ]
        if len(kept) == len(entries):
            kept_groups.append(group)
            continue
        changed = True
        if kept:
            kept_groups.append({**group, "hooks": kept})
    return kept_groups, changed


@dataclass(frozen=True)
class Merged:
    document: Document
    # The containers this merge created, named ``"hooks"`` and
    # ``"hooks.<Event>"``. :func:`remove` drops an emptied container only when
    # it is named here, so an uninstall restores what was there before.
    created: frozenset[str]


def merge(
    document: Document,
    commands: Mapping[str, str],
    *,
    timeout: int = HOOK_TIMEOUT_SECONDS,
) -> Merged:
    """Make each event carry exactly one bridge hook: ``commands[event]``.

    An event whose only bridge entry is already that exact command is left as
    it is, so a second install is a no-op. Otherwise every entry naming
    :data:`HOOK_MARKER` (say, one written with an interpreter that has since
    moved) is removed and one new matcher group is appended.
    """
    merged = copy.deepcopy(document)
    hooks = _hooks_table(merged)
    created: set[str] = set()
    for event, command in commands.items():
        groups = hooks.get(event) if hooks is not None else None
        if groups is not None and not isinstance(groups, list):
            raise SettingsError(f'the settings file\'s "hooks.{event}" is not a list')
        if groups is not None and hooks is not None:
            ours = [c for c in _commands(groups) if names_bridge_hook(c)]
            if ours == [command]:
                continue
            groups, _ = _strip(groups, names_bridge_hook)
            hooks[event] = groups
        if hooks is None:
            hooks = {}
            merged["hooks"] = hooks
            created.add("hooks")
        if groups is None:
            groups = []
            hooks[event] = groups
            created.add(f"hooks.{event}")
        groups.append(
            {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        )
    return Merged(merged, frozenset(created))


def remove(
    document: Document,
    is_ours: Callable[[str], bool] = names_bridge_hook,
    *,
    created: Collection[str] = (),
) -> Document:
    """Delete every hook entry whose command ``is_ours``, and nothing else.

    A matcher group this removal emptied is dropped. An emptied event list or
    ``hooks`` table is dropped only when ``created`` names it (the install
    made it); otherwise it stays, empty, as it was before the install. A
    ``hooks`` value of a shape this tool does not know is left untouched.
    """
    pruned = copy.deepcopy(document)
    hooks = pruned.get("hooks")
    if not isinstance(hooks, dict):
        return pruned
    emptied = False
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups, changed = _strip(groups, is_ours)
        if not changed:
            continue
        if kept_groups or f"hooks.{event}" not in created:
            hooks[event] = kept_groups
        else:
            del hooks[event]
            emptied = True
    if emptied and not hooks and "hooks" in created:
        del pruned["hooks"]
    return pruned


def render(document: Document, *, before: str | None = None) -> str:
    """Render JSON with detectable indentation and newline style from ``before``."""
    indent: int | str = 2
    if before is not None:
        match = re.search(r'(?m)^([ \t]+)"(?:[^"\\]|\\.)*"\s*:', before)
        if match is not None:
            indent = match.group(1)
    text = json.dumps(document, indent=indent, ensure_ascii=False)
    if before is None or before.endswith(("\n", "\r")):
        text += "\n"
    if (
        before is not None
        and "\r\n" in before
        and "\n" not in before.replace("\r\n", "")
    ):
        text = text.replace("\n", "\r\n")
    return text


def ensure_unchanged(path: Path, before: str) -> None:
    """Refuse to overwrite a settings file edited since the initial read."""
    try:
        _, current = read(path)
    except SettingsError as exc:
        raise SettingsError(
            f"{path} changed since it was read; refusing to edit"
        ) from exc
    if current != before:
        raise SettingsError(f"{path} changed since it was read; refusing to edit")


def unified_diff(path: Path, before: str | None, after: str) -> str:
    """A unified diff from ``before`` (``None``: absent) to ``after``.

    Both header lines name ``path``'s absolute form, so the file that would be
    written is the first thing a reader sees. An absent file diffs from
    nothing: every line is an addition.
    """
    absolute = str(path.absolute())
    lines = difflib.unified_diff(
        [] if before is None else before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=absolute,
        tofile=absolute,
    )
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in lines
    )


def write_atomic(
    path: Path,
    text: str,
    *,
    expected_before: str | None = None,
    expect_absent: bool = False,
) -> None:
    """Replace ``path`` with ``text``: temp file, fsync, rename, fsync the dir.

    A file that exists keeps its permission bits; a new one is 0600.
    ``expect_absent`` creates the file only if nothing holds the name yet:
    the temp file is hard-linked into place, so a file someone else created
    since the read (Claude Code writes the user settings file itself) is
    refused, never overwritten.
    """
    check_location(path)
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
    except FileNotFoundError:
        mode = NEW_FILE_MODE
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        if expected_before is not None:
            ensure_unchanged(path, expected_before)
        if expect_absent:
            try:
                os.link(tmp_name, path)
            except FileExistsError:
                raise SettingsError(
                    f"{path} was created since it was read; refusing to edit"
                ) from None
            Path(tmp_name).unlink()
        else:
            os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

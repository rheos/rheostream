"""The enrolled directory's Claude Code settings file, read and edited safely.

``install-hook`` and ``remove-hook`` edit ``<enrolled_dir>/.claude/settings.local.json``
through the functions here, and nothing else writes it. The rules:

- An absent file is ``{}``. A file that is not UTF-8 JSON, or whose top-level
  value is not an object, is refused before anything is written; so is a
  symlink, which a rename would silently replace with a regular file.
- :func:`merge` and :func:`remove` return a new document and never mutate the
  one passed in. Everything this tool did not add keeps its meaning: other
  keys, other events, other hooks, and any empty container that was already
  empty before the edit.
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
import shlex
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

SETTINGS_RELATIVE: Final = Path(".claude") / "settings.local.json"
# ``remove`` deletes a hook entry when its command names this; it is how the
# installed command is recognised whatever interpreter wrote it.
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
    """``"<interpreter> <hook_script> <event>"``, each part shell-quoted.

    Claude Code runs the command through a shell. For the ordinary case, paths
    with no space or shell metacharacter, quoting changes nothing.
    """
    return shlex.join([interpreter, str(hook_script), event])


def read(path: Path) -> tuple[Document, str | None]:
    """Parse the settings file: ``(document, original text)``.

    The text is ``None`` when the file is absent, and the document is then
    ``{}``.
    """
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
        text = path.read_text(encoding="utf-8")
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


def merge(
    document: Document,
    commands: Mapping[str, str],
    *,
    timeout: int = HOOK_TIMEOUT_SECONDS,
) -> Document:
    """Append one matcher group per event unless its exact command is present.

    ``commands`` maps an event name to its command. An event whose command is
    already configured anywhere in that event's groups gets nothing, so a
    second install is a no-op.
    """
    merged = copy.deepcopy(document)
    hooks = _hooks_table(merged)
    for event, command in commands.items():
        groups = hooks.get(event) if hooks is not None else None
        if groups is not None and not isinstance(groups, list):
            raise SettingsError(f'the settings file\'s "hooks.{event}" is not a list')
        if groups is not None and command in _commands(groups):
            continue
        if hooks is None:
            hooks = {}
            merged["hooks"] = hooks
        if groups is None:
            groups = []
            hooks[event] = groups
        groups.append(
            {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        )
    return merged


def names_bridge_hook(command: str) -> bool:
    return HOOK_MARKER in command


def remove(
    document: Document, is_ours: Callable[[str], bool] = names_bridge_hook
) -> Document:
    """Delete every hook entry whose command ``is_ours``, and nothing else.

    A matcher group, an event list or the ``hooks`` table is dropped only when
    this removal emptied it; one that was already empty stays. A ``hooks``
    value of a shape this tool does not know is left untouched.
    """
    pruned = copy.deepcopy(document)
    hooks = pruned.get("hooks")
    if not isinstance(hooks, dict):
        return pruned
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
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
        if not changed:
            continue
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
            if not hooks:
                del pruned["hooks"]
    return pruned


def render(document: Document) -> str:
    """The text written for ``document``: two-space JSON and a final newline."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


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


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``: temp file, fsync, rename, fsync the dir.

    A file that exists keeps its permission bits; a new one is 0600.
    """
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
    except FileNotFoundError:
        mode = NEW_FILE_MODE
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

"""Reading Claude Code transcripts: which file may be opened, which lines are human.

Everything here is pure and local. Nothing makes a request, logs a path, or reads
``$HOME``, ``Path.home()``, ``~`` or the working directory: the caller hands in
``projects_root`` (``user_home / ".claude" / "projects"`` in production) and the
enrolled directory's realpath, or ``None`` for a machine-scope bridge.

The transcript shape this module relies on is not a published contract. Each
place that depends on it carries a ``[capture N]`` tag naming its row in the
spec's "Capture-dependent design points" table; a reconciliation that moves a
row changes only the tagged code.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

RefusalReason = Literal["path_escape", "project_unmatched"]

_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")

# Flags that mark machine-generated or non-conversational text on a line that
# otherwise looks human. A line is admitted only when each is absent or
# exactly ``False``. [capture 11] — may be revised at reconciliation.
MACHINE_FLAGS: tuple[str, ...] = (
    "isMeta",
    "isSidechain",
    "isCompactSummary",
    "isVisibleInTranscriptOnly",
)


@dataclass(frozen=True)
class Ok:
    """The transcript may be read, at this resolved path."""

    path: Path


@dataclass(frozen=True)
class Missing:
    """The path is a valid location for the enrolled project, but nothing is there.

    ``path`` is the lexical absolute path. The worker records a
    ``source_truncated`` gap for it (spec step 3) instead of dropping the session.
    """

    path: Path


@dataclass(frozen=True)
class Refused:
    reason: RefusalReason


ValidationResult = Ok | Missing | Refused


def slug(path: str) -> str:
    """The project directory name Claude Code gives a session's start directory.

    [capture 12] — may be revised at reconciliation.
    """
    return _NOT_ALNUM.sub("-", path)


def validate_source(
    transcript_path: str, enrolled_dir_realpath: str | None, *, projects_root: Path
) -> ValidationResult:
    """Decide whether ``transcript_path`` belongs to the enrolled directory.

    [capture 12, 13] — may be revised at reconciliation.

    ``enrolled_dir_realpath`` is ``None`` for a machine-scope bridge, which
    admits a session of any project: see :func:`_validate_any_project`.

    The realpath must be a regular ``*.jsonl`` file directly inside
    ``realpath(projects_root / slug(enrolled_dir_realpath))``. The directory
    match is exact: a session started in a parent or a subdirectory of the
    enrolled directory has a different slug and is ``project_unmatched``. Never
    widen this to a prefix match; the answer for another directory is to enroll
    it, or to give the bridge machine scope. A file in another project's slug
    directory is ``project_unmatched``; anything else that fails (a ``..``
    component, a symlink resolving elsewhere, a relative path, a nested file,
    a non-file) is ``path_escape``.

    The slug directory must itself resolve directly under ``projects_root``: a
    slug directory symlinked elsewhere is ``path_escape`` for every path.

    A path with nothing at it is ``Missing`` only when it is otherwise valid:
    absolute, no ``..``, and a ``*.jsonl`` name directly inside the exact slug
    directory. A
    missing path under another slug stays ``project_unmatched``; any other
    missing path, a dangling symlink included, stays ``path_escape``.
    """
    lexical = Path(transcript_path)
    # A relative path would resolve against the working directory, and ``..``
    # is an escape by construction; refuse both before touching the disk.
    if not lexical.is_absolute() or ".." in lexical.parts:
        return Refused("path_escape")
    if enrolled_dir_realpath is None:
        return _validate_any_project(lexical, projects_root=projects_root)
    try:
        project_dir = Path(
            os.path.realpath(projects_root / slug(enrolled_dir_realpath))
        )
        resolved = Path(os.path.realpath(lexical))
        lexical_parent = Path(os.path.realpath(lexical.parent))
        root = Path(os.path.realpath(projects_root))
        present = os.path.lexists(lexical)
    except (OSError, ValueError):
        return Refused("path_escape")
    # The enrolled slug directory itself must resolve directly under
    # projects_root; a symlinked slug directory pointing elsewhere admits
    # nothing, present or missing.
    if project_dir.parent != root:
        return Refused("path_escape")
    if not present:
        if lexical_parent == project_dir and lexical.suffix == ".jsonl":
            return Missing(lexical)
        if lexical_parent != project_dir and lexical_parent.parent == root:
            return Refused("project_unmatched")
        return Refused("path_escape")
    try:
        info = os.stat(resolved)
    except (OSError, ValueError):
        return Refused("path_escape")
    if resolved.parent == project_dir:
        if stat.S_ISREG(info.st_mode) and resolved.suffix == ".jsonl":
            return Ok(resolved)
        return Refused("path_escape")
    # The file does not resolve into the enrolled project. If it sits, with
    # no symlink on its own name, in some other project's directory, it is a
    # different project; everything else is an escape.
    if (
        lexical_parent != project_dir
        and lexical_parent.parent == root
        and resolved == lexical_parent / lexical.name
    ):
        return Refused("project_unmatched")
    return Refused("path_escape")


def _validate_any_project(lexical: Path, *, projects_root: Path) -> ValidationResult:
    """Machine scope: a ``*.jsonl`` file directly inside any project directory.

    ``lexical`` is absolute and has no ``..``. Its parent must sit directly
    under ``projects_root`` and must not be a symlink, the way a symlinked
    slug directory admits nothing in directory scope. The file must resolve
    to a regular file in that same directory. ``project_unmatched`` cannot
    happen here; every other failure is ``path_escape``, and an absent file of
    the right shape is ``Missing``.
    """
    try:
        root = Path(os.path.realpath(projects_root))
        project_dir = Path(os.path.realpath(lexical.parent))
        lexical_grandparent = Path(os.path.realpath(lexical.parent.parent))
        parent_is_link = os.path.islink(lexical.parent)
        resolved = Path(os.path.realpath(lexical))
        present = os.path.lexists(lexical)
    except (OSError, ValueError):
        return Refused("path_escape")
    if parent_is_link or lexical_grandparent != root or project_dir.parent != root:
        return Refused("path_escape")
    if not present:
        if lexical.suffix == ".jsonl":
            return Missing(lexical)
        return Refused("path_escape")
    try:
        info = os.stat(resolved)
    except (OSError, ValueError):
        return Refused("path_escape")
    if (
        resolved.parent == project_dir
        and stat.S_ISREG(info.st_mode)
        and resolved.suffix == ".jsonl"
    ):
        return Ok(resolved)
    return Refused("path_escape")


def human_text(line: dict[str, object]) -> str | None:
    """The text of a human turn, or ``None`` for every other line.

    [capture 11] — may be revised at reconciliation.

    An allow-list: only a ``user`` line of ``human`` origin whose
    ``MACHINE_FLAGS`` are each absent or exactly ``False``, that carries no
    ``toolUseResult``, and whose ``message.content``
    is a string or a non-empty list made only of ``text`` blocks. A list is
    joined with a blank line. Any shape not recognised here fails closed.
    """
    if line.get("type") != "user":
        return None
    origin = line.get("origin", {})
    if not isinstance(origin, dict) or origin.get("kind") != "human":
        return None
    # Absent or exactly False only: 1, "true", null or a list all exclude.
    if any(line.get(flag, False) is not False for flag in MACHINE_FLAGS):
        return None
    if "toolUseResult" in line:
        return None
    message = line.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            return None
        text = block.get("text")
        if not isinstance(text, str):
            return None
        parts.append(text)
    return "\n\n".join(parts)


def is_human_line(line: dict[str, object]) -> bool:
    """[capture 11] — may be revised at reconciliation."""
    return human_text(line) is not None


def parse_clock(line: dict[str, object]) -> datetime | None:
    """The line's ISO-8601 ``timestamp``, or ``None`` if absent or unreadable.

    [capture 11] — may be revised at reconciliation.

    A timestamp without an offset is unreadable too: it cannot be compared with
    the local pending-age limit without guessing a zone.
    """
    raw = line.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def extract_ids(line: dict[str, object]) -> tuple[str, str] | None:
    """``(sessionId, uuid)`` when both are non-empty strings.

    [capture 11] — may be revised at reconciliation.
    """
    session_id = line.get("sessionId")
    line_uuid = line.get("uuid")
    if (
        isinstance(session_id, str)
        and session_id
        and isinstance(line_uuid, str)
        and line_uuid
    ):
        return session_id, line_uuid
    return None


def entrypoint(line: dict[str, object]) -> str | None:
    """The client that wrote the line (``cli`` or ``claude-desktop``).

    [capture 8] — may be revised at reconciliation.

    Kept locally for the per-client counters only; never sent to the server.
    """
    value = line.get("entrypoint")
    if isinstance(value, str) and value:
        return value
    return None

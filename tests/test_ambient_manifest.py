"""AC 8: the supported-client ambient capability manifest cites only real tests.

``docs/acceptance/ambient-capability-manifest.md`` names ``pytest:`` node ids as the
proof of each row's refusal and replay behaviour. This file checks, without running any
of them, that every cited id names a function that exists: it parses the named file with
``ast`` and looks the name up (``Class::method`` ids look in that class). It also checks
that the manifest keeps one row per path, with the CLI and Desktop Code rows of
``claude_code_local`` distinct, each carrying its own refusal and replay proof.

It cannot check that a test proves what its row says; that is signed by the row.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path
from typing import Final

import pytest

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
_MANIFEST: Final = _REPO_ROOT / "docs" / "acceptance" / "ambient-capability-manifest.md"

_ROW_HEADING: Final = re.compile(r"^## Row: (.+)$", re.MULTILINE)
_NODE_ID: Final = re.compile(r"`pytest:([^`]+)`")
_FIELD: Final = re.compile(r"^\*\*([^*]+):\*\*", re.MULTILINE)

ROWS: Final = (
    "`rheo_runtime`",
    "`claude_code_local` via Claude Code CLI",
    "`claude_code_local` via Desktop Code",
)


def _text() -> str:
    return _MANIFEST.read_text(encoding="utf-8")


def _rows(text: str) -> dict[str, str]:
    """Each row's heading to its body, up to the next row heading."""
    matches = list(_ROW_HEADING.finditer(text))
    rows: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        heading = match.group(1).strip()
        assert heading not in rows, f"row {heading!r} appears twice"
        rows[heading] = text[match.end() : end]
    return rows


def _field(body: str, label: str) -> str:
    """The text a bold ``**Label:**`` owns, up to the next bold label."""
    matches = list(_FIELD.finditer(body))
    for index, match in enumerate(matches):
        if match.group(1) == label:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            return body[match.end() : end]
    raise AssertionError(f"no **{label}:** field")


@cache
def _definitions(relative: str) -> frozenset[str]:
    """Every ``name`` and ``Class::method`` defined at the top of one test file."""
    tree = ast.parse((_REPO_ROOT / relative).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.add(f"{node.name}::{member.name}")
    return frozenset(names)


def _cited() -> list[str]:
    return sorted(set(_NODE_ID.findall(_text())))


def test_the_manifest_cites_node_ids() -> None:
    assert len(_cited()) >= 20


@pytest.mark.parametrize("node_id", _cited())
def test_every_cited_node_id_names_an_existing_test(node_id: str) -> None:
    relative, sep, name = node_id.partition("::")
    assert sep, f"{node_id} has no ::"
    assert "[" not in name, f"{node_id}: cite the test family, not one case"
    path = _REPO_ROOT / relative
    assert path.is_file(), f"{relative} does not exist"
    assert path.name.startswith("test_"), f"{relative} is not a test file"
    assert name.rsplit("::", 1)[-1].startswith("test_"), f"{name} is not a test"
    assert name in _definitions(relative), f"{relative} defines no {name}"


def test_each_path_has_exactly_one_row() -> None:
    assert tuple(_rows(_text())) == ROWS


def test_the_two_local_rows_are_distinct() -> None:
    rows = _rows(_text())
    cli, desktop = rows[ROWS[1]], rows[ROWS[2]]
    assert cli != desktop
    assert '`entrypoint = "cli"`' in _field(cli, "Client")
    assert '`entrypoint = "claude-desktop"`' in _field(desktop, "Client")


@pytest.mark.parametrize("row", ROWS)
@pytest.mark.parametrize("label", ["Refusal", "Replay"])
def test_every_row_proves_refusal_and_replay(row: str, label: str) -> None:
    assert _NODE_ID.findall(_field(_rows(_text())[row], label)), (
        f"{row} cites no {label.lower()} test"
    )


@pytest.mark.parametrize("row", ROWS[1:])
def test_each_local_row_is_pending_the_gates_and_names_its_limits(row: str) -> None:
    body = _rows(_text())[row]
    assert "operative, pending CP-A/CP-B for real use" in _field(body, "State")
    limits = _field(body, "Not guaranteed")
    for limit in (
        "R4",
        "R10",
        "one enrolled directory per directory-scope bridge home",
        "slug",
    ):
        assert limit.lower() in limits.lower(), f"{row} does not name {limit}"
    assert _NODE_ID.findall(_field(body, "Entrypoint"))

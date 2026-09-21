"""FR 14: core, contracts, and applications do not depend on Recallatron."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = ("packages", "apps", "tests")
_MODULE_TOKEN = "rheo_recallatron"
_PATH_TOKEN = "modules/recallatron"
_DECLARED_TEST_DEPENDENCIES = {
    "tests/postgres/test_memory_lifecycle.py",
    "tests/postgres/test_memory_mcp.py",
    "tests/postgres/test_memory_records.py",
    "tests/postgres/test_memory_retention.py",
    "tests/postgres/test_module_install.py",
    "tests/postgres/test_module_lifecycle.py",
    "tests/postgres/test_module_migrations.py",
    "tests/postgres/test_module_storage_ownership.py",
    "tests/postgres/test_tool_telemetry.py",
    "tests/postgres/test_workspace_status.py",
    "tests/test_module_import_graph.py",
}
"""The test files allowed to name Recallatron, each for a reason.

FR 14 is about ``packages`` and ``apps``; a *test* may reach for the one real module
distribution in the checkout when that is its subject, and this set is the record of
which ones do. ``test_memory_records.py`` is the memory schema's own census,
``test_memory_lifecycle.py`` drives the deletion coordinator against the one real
owned-deletable record type in the checkout, ``test_memory_mcp.py`` drives the MCP
tool facade against the one module in the checkout that registers a tool naming a
core operation, ``test_tool_telemetry.py`` drives the telemetry sink through that same
tool surface — it needs a ``READ`` tool that declares both a query and a lifecycle
selector and a ``DESTRUCTIVE`` one that answers ``approval_required``, and no fixture
module declares either — and ``test_workspace_status.py`` reads a real module's package
and schema versions back through ``core.workspace.status``; none has a substitute,
because a fixture module has no migration chain, no owned-delete declaration and no
installed distribution metadata to report.

The assertion is an equality, so an entry that stops being true reds here as a stale
exclusion rather than lingering as permission nobody uses."""


def _docstring_nodes(tree: ast.AST) -> set[int]:
    nodes: set[int] = set()
    for owner in ast.walk(tree):
        if not isinstance(
            owner, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(owner, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            nodes.add(id(first.value))
    return nodes


def _names_module(value: str) -> bool:
    return value == _MODULE_TOKEN or value.startswith(f"{_MODULE_TOKEN}.")


def _mentions(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _MODULE_TOKEN or alias.name.startswith(
                    f"{_MODULE_TOKEN}."
                ):
                    found.append(f"import {alias.name}@{node.lineno}")
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == _MODULE_TOKEN or node.module.startswith(
                f"{_MODULE_TOKEN}."
            ):
                found.append(f"import {node.module}@{node.lineno}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            if _names_module(node.value):
                found.append(f"module {_MODULE_TOKEN}@{node.lineno}")
            if _PATH_TOKEN in node.value:
                found.append(f"path {_PATH_TOKEN}@{node.lineno}")
    return sorted(found)


def _scan(root: Path) -> tuple[int, dict[str, list[str]]]:
    scanned = 0
    found: dict[str, list[str]] = {}
    for top in _SCAN_DIRS:
        for path in sorted((root / top).rglob("*.py")):
            scanned += 1
            mentions = _mentions(path)
            if mentions:
                found[str(path.relative_to(root))] = mentions
    return scanned, found


def test_core_contracts_and_apps_do_not_depend_on_recallatron() -> None:
    scanned, found = _scan(_REPO_ROOT)
    assert scanned > 100, f"only {scanned} Python files were scanned"

    actual = set(found)
    undeclared = sorted(actual - _DECLARED_TEST_DEPENDENCIES)
    stale = sorted(_DECLARED_TEST_DEPENDENCIES - actual)
    assert not undeclared, f"undeclared Recallatron dependencies: {undeclared}"
    assert not stale, f"stale Recallatron dependency exclusions: {stale}"
    assert actual == _DECLARED_TEST_DEPENDENCIES

    controls = {
        path: mentions
        for path, mentions in found.items()
        if path in _DECLARED_TEST_DEPENDENCIES
        and any(_MODULE_TOKEN in mention for mention in mentions)
    }
    assert controls, "the dependency scan found no declared positive control"


def test_string_dependency_routes_exclude_narrative_text(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        '"""rheo_recallatron and modules/recallatron are documentation only."""\n'
        '# importlib.import_module("rheo_recallatron")\n'
        'lookalike = "rheo_recallatronically"\n'
        'module = importlib.import_module("rheo_recallatron")\n'
        'source = resources.files("modules/recallatron")\n',
        encoding="utf-8",
    )

    assert _mentions(probe) == [
        "module rheo_recallatron@4",
        "path modules/recallatron@5",
    ]

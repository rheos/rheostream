"""FR 14: core, contracts, and applications do not depend on Recallatron."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = ("packages", "apps", "tests")
_MODULE_TOKEN = "rheo_recallatron"
_PATH_TOKEN = "modules/recallatron"
_DECLARED_TEST_DEPENDENCIES = {
    "tests/postgres/test_module_install.py",
    "tests/postgres/test_module_lifecycle.py",
    "tests/postgres/test_module_migrations.py",
    "tests/postgres/test_module_storage_ownership.py",
    "tests/test_module_import_graph.py",
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    nodes: set[int] = set()
    for owner in ast.walk(tree):
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
            and _PATH_TOKEN in node.value
        ):
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

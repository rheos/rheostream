"""FR 14: core, contracts, and applications do not depend on Recallatron."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = ("packages", "apps", "tests")
_MODULE_TOKEN = "rheo_recallatron"
_PATH_TOKEN = "modules/recallatron"
_MATRIX_PARSER = _REPO_ROOT / "tests" / "test_acceptance_matrix.py"
_ABSENCE_PROOF_MATRIX = _REPO_ROOT / "docs" / "acceptance" / "phase-1-matrix.md"
_DECLARED_TEST_DEPENDENCIES = {
    "tests/postgres/test_memory_browse.py",
    "tests/postgres/test_memory_dedup.py",
    "tests/postgres/test_memory_embedding.py",
    "tests/postgres/test_memory_export_restore.py",
    "tests/postgres/test_memory_lifecycle.py",
    "tests/postgres/test_memory_mcp.py",
    "tests/postgres/test_memory_redaction_mcp.py",
    "tests/postgres/test_memory_records.py",
    "tests/postgres/test_memory_retention.py",
    "tests/postgres/test_memory_retrieval.py",
    "tests/postgres/test_module_install.py",
    "tests/postgres/test_module_lifecycle.py",
    "tests/postgres/test_module_migrations.py",
    "tests/postgres/test_module_storage_ownership.py",
    "tests/postgres/test_tool_telemetry.py",
    "tests/test_module_import_graph.py",
    "tests/test_module_settings_order.py",
}
"""The test files allowed to name Recallatron, each for a reason.

FR 14 is about ``packages`` and ``apps``; a *test* may reach for the one real module
distribution in the checkout when that is its subject, and this set is the record of
which ones do. ``test_memory_export_restore.py`` drives the module export/import
bridge — it is the only module in the checkout with a real export format and a
migration chain a restore can rebuild — ``test_memory_records.py`` is the memory
schema's own census,
``test_memory_lifecycle.py`` drives the deletion coordinator against the one real
owned-deletable record type in the checkout, ``test_memory_mcp.py`` drives the MCP
tool facade against the one module in the checkout that registers a tool naming a
core operation, ``test_memory_redaction_mcp.py`` drives the redaction policy (contact
masking, the mask-token write-back refusal, excluded types) through Recallatron's real
tools, since memory bodies are the free text the contact mask exists for,
``test_tool_telemetry.py`` drives the telemetry sink through that same tool surface
— it needs a ``READ`` tool that declares both a query and a lifecycle selector and a
``DESTRUCTIVE`` one that answers ``approval_required``, and no fixture
module declares either — ``test_memory_embedding.py`` drives the embedding pipeline,
whose providers, jobs and rebuild are Recallatron's own — ``test_memory_retrieval.py``
drives the dense arm, which ranks over Recallatron's own vector index —
``test_memory_dedup.py`` drives the dedup-candidate operation, which pairs memories
over that same index under Recallatron's own eligibility —
``test_memory_browse.py`` drives the entity-container read, ``memory.get`` and the
operation inventory the memory screens depend on, all under that same eligibility — and
``test_module_lifecycle.py`` reads a real module's package and schema versions back
through ``core.workspace.status``, and ``test_module_settings_order.py`` (#108)
resolves a real module's own deployment key and writes its per-workspace defaults
through its real ``rheo.modules`` entry point; none has a substitute,
because a fixture module has no migration chain, no owned-delete declaration and no
installed distribution metadata to report.

``tests/postgres/test_workspace_status.py`` was in this set and is deliberately not any
more: it carries four phase-one acceptance demonstrators, and being *allowed* to name
the module is not the same as being *able* to. See
:func:`test_the_absence_proof_import_surface_never_names_recallatron`, which is the
check that makes the difference mechanical rather than remembered.

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


def _matrix_parser() -> ModuleType:
    """``tests/test_acceptance_matrix.py``, loaded as a module rather than imported.

    The same idiom ``tests/test_absence_proof.py`` uses, and for the same reason: the
    matrix grammar has exactly one parser and a second reader of these files must be
    that parser rather than a regular expression that agrees with it today. Loading a
    test module by path keeps it out of this file's own import graph; nothing at its
    module level runs a subprocess, so this costs a parse.
    """
    name = "_acceptance_matrix_for_import_graph"
    spec = importlib.util.spec_from_file_location(name, _MATRIX_PARSER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _absence_proof_import_surface() -> set[str]:
    """The test files ``make absence-proof`` imports in its stripped checkout.

    The proof deletes ``modules/recallatron``, re-syncs, and hands ``pytest`` the
    phase-one matrix's ``pytest:`` demonstrators *by node id* — which imports each of
    those files whole, every test in them, not only the cited ones. Added to that: the
    support modules the whole suite pulls in, ``tests/conftest.py`` and
    ``tests/harness``.

    Import edges past that shared surface are not followed, so this is the realistic
    surface rather than a proven-closed one. A demonstrator file that grows a private
    module-level import of some third test helper that in turn names the module would
    still reach CI — ``make absence-proof`` remains the only complete answer, and this
    is the part of it that can run in milliseconds.
    """
    rows = _matrix_parser().parse(_ABSENCE_PROOF_MATRIX)
    cited = {
        str(demo).removeprefix("pytest:").partition("::")[0]
        for row in rows
        for demo in row.demonstrators
        if str(demo).startswith("pytest:")
    }
    support = [_REPO_ROOT / "tests" / "conftest.py"]
    support.extend(sorted((_REPO_ROOT / "tests" / "harness").rglob("*.py")))
    return cited | {str(path.relative_to(_REPO_ROOT)) for path in support}


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


def test_the_absence_proof_import_surface_never_names_recallatron() -> None:
    """A phase-one demonstrator's file must be *able* to run with the module deleted.

    ``_DECLARED_TEST_DEPENDENCIES`` above answers "may this file name Recallatron",
    which is a question about FR 14. This one answers "can it", which is a question
    about ``make absence-proof`` — and the two came apart: a module-level
    ``from rheo_recallatron import MANIFEST`` was added to
    ``tests/postgres/test_workspace_status.py``, declared here, and passed every local
    gate. In the stripped checkout the import raised ``ModuleNotFoundError`` before
    collection finished, so the file's **four** criterion-10 demonstrators never ran,
    and the only job that said so was the ten-minute one in CI. The declaration was
    honest; it just did not mean what the proof needs.

    ``.bureau/regression/run.sh`` excludes ``absence-proof`` as a slow job, so nothing
    a builder runs between checkpoints covered this. This check does, in the time an
    AST parse takes.
    """
    surface = _absence_proof_import_surface()
    assert len(surface) > 20, sorted(surface)
    # The file the gap was found in, named so that a matrix that stops citing it reds
    # here rather than quietly narrowing what this check covers.
    assert "tests/postgres/test_workspace_status.py" in surface

    _, found = _scan(_REPO_ROOT)
    assert found, "the dependency scan found no Recallatron mention anywhere"
    named = sorted(surface & set(found))
    assert not named, (
        "make absence-proof deletes modules/recallatron and re-runs every phase-one "
        f"demonstrator, so these files cannot name it: {named}"
    )


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

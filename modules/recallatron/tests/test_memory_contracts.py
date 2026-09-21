"""AC 3 and AC 14's mechanical halves: two scans over this distribution's own tree.

Both claims in this file are *absences*, and an absence is the one kind of claim a
behavioural test cannot make. "1a1 performs no automatic transcript extraction" is not
demonstrated by a test that fails to observe one, and "no ``topic_thread`` or
``procedural_notes`` anywhere" is not demonstrated by prose. So each is a scan over the
real tree, and each carries a **positive control** — a probe file the scan must flag —
so a scan that silently stopped matching reds instead of passing vacuously.

1. **The import-graph scan (AC 3).** Recallatron's own source imports nothing from the
   core's runtime-transcript or hook surface. The technique is
   ``tests/test_module_import_graph.py``'s, pointed the other way: that file proves the
   core does not depend on this module, this one proves this module does not depend on
   the runtime. Together they are the two directions of one boundary.

2. **The boundary scan (AC 14).** No ``topic_thread`` or ``procedural_notes`` string,
   table or kind value appears anywhere in the module's source **or** its migration
   tree. Modelled on ``tests/test_boundary_scans.py``'s walk-and-assert-none shape, and
   over raw text rather than AST constants on purpose: a migration can name a table in
   a SQL string, a comment, or a ``.mako`` template, and an AST scan would see none of
   the three.

   Its second, independent proof is the database's own ``kind`` check constraint, which
   admits exactly the four ratified kinds. This file pins the Python vocabulary the DDL
   is built from; ``tests/postgres/test_memory_records.py`` reads the constraint back
   out of ``pg_catalog`` on a live database. Neither is the other's restatement: one
   would catch a widened tuple, the other a migration that altered the constraint
   directly.

**This file is deliberately the one place the two deferred names are written down.** It
scans ``src/``; it does not scan itself, and there is no third copy of either word in
the distribution for the scan to have to exclude.
"""

import ast
from pathlib import Path

_DISTRIBUTION = Path(__file__).resolve().parents[1]
_MODULE_SRC = _DISTRIBUTION / "src"

_FORBIDDEN_RUNTIME_MODULES = (
    "rheo_core.runtime",
    "rheo_core.storage.runtime_tables",
)
"""Every module that carries runtime transcripts or hook configuration.

``rheo_core.runtime`` is named as a package rather than by its members, so a later
submodule is covered the day it is added rather than the day somebody remembers to
list it. ``runtime_tables`` sits under ``rheo_core.storage`` and is therefore already
refused by ``tests/postgres/test_module_storage_ownership.py``'s foreign-storage scan —
it is named again here because *this* file's claim is about transcripts, and a reader
checking AC 3 should not have to know that the storage scan happens to cover half of
it.
"""

DEFERRED_FEATURE_NAMES = ("topic_thread", "procedural_notes")
"""The two features that remain explicitly deferred (§ A0).

``topic_thread`` needs an approved conversation/session-digest substrate;
``procedural_notes`` needs a candidate/confirmation/dedup lifecycle the ratified kinds
do not have. Neither has a 1a1 schema, migration, tool, export field, test or
acceptance claim, and a content-free source-unit key is neither of them.
"""

RATIFIED_KINDS = ("note", "fact", "decision", "summary")
"""§ A3's four, written out here rather than imported, so this assertion compares two
independently written lists instead of one list with itself."""


def _source_files(root: Path) -> list[Path]:
    """Every file under ``root``, compiled bytecode excluded.

    Not just ``*.py``: the migration tree carries ``script.py.mako``, and a table
    named in a template is a table named in the module.
    """
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _runtime_imports(tree: ast.AST) -> list[str]:
    """Imports of a forbidden runtime module, by name and line."""
    found: list[str] = []

    def names(node: ast.AST) -> list[tuple[int, str]]:
        if isinstance(node, ast.Import):
            return [(node.lineno, alias.name) for alias in node.names]
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            return [(node.lineno, node.module)] + [
                (node.lineno, f"{node.module}.{alias.name}") for alias in node.names
            ]
        return []

    for node in ast.walk(tree):
        for line, module in names(node):
            if any(
                module == forbidden or module.startswith(f"{forbidden}.")
                for forbidden in _FORBIDDEN_RUNTIME_MODULES
            ):
                found.append(f"{module}@{line}")
    return sorted(found)


def _scan_imports(root: Path) -> tuple[int, dict[str, list[str]]]:
    scanned = 0
    violations: dict[str, list[str]] = {}
    for path in _source_files(root):
        if path.suffix != ".py":
            continue
        scanned += 1
        sites = _runtime_imports(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        if sites:
            violations[str(path.relative_to(root))] = sites
    return scanned, violations


def _scan_deferred_names(root: Path) -> tuple[int, dict[str, list[str]]]:
    scanned = 0
    violations: dict[str, list[str]] = {}
    for path in _source_files(root):
        scanned += 1
        text = path.read_text(encoding="utf-8").casefold()
        named = [name for name in DEFERRED_FEATURE_NAMES if name in text]
        if named:
            violations[str(path.relative_to(root))] = named
    return scanned, violations


def test_recallatron_imports_no_runtime_transcript_or_hook_module() -> None:
    """AC 3: no automatic transcript extraction, proved by the import graph.

    A module that extracts from transcripts has to reach the transcript tables or the
    runtime operations that produce them. It reaches neither, and no lazily imported
    name can hide from a static walk.
    """
    scanned, violations = _scan_imports(_MODULE_SRC)
    assert scanned, f"no Recallatron Python files scanned under {_MODULE_SRC}"
    assert not violations, f"Recallatron imports a runtime module: {violations}"


def test_the_runtime_import_scan_flags_a_real_import(tmp_path: Path) -> None:
    """The positive control. Without it the scan above could match nothing at all."""
    (tmp_path / "probe.py").write_text(
        "from rheo_core.runtime.operations import build_context\n"
        "import rheo_core.storage.runtime_tables\n",
        encoding="utf-8",
    )
    scanned, violations = _scan_imports(tmp_path)
    assert scanned == 1
    assert violations == {
        "probe.py": [
            "rheo_core.runtime.operations.build_context@1",
            "rheo_core.runtime.operations@1",
            "rheo_core.storage.runtime_tables@2",
        ]
    }


def test_no_deferred_feature_name_appears_in_the_source_or_migration_tree() -> None:
    """AC 14, over every file under ``src/`` — migrations and templates included."""
    scanned, violations = _scan_deferred_names(_MODULE_SRC)
    assert scanned, f"no Recallatron files scanned under {_MODULE_SRC}"
    assert not violations, f"a deferred name appears in the module: {violations}"


def test_the_deferred_name_scan_flags_a_reintroduced_literal(tmp_path: Path) -> None:
    """The positive control, and the one that matters most.

    A scan for two words is exactly the kind that can rot into a scan for nothing —
    a changed root, a suffix filter, a casefold that stopped happening. The probe puts
    one name in a SQL string and the other in a comment, because those are the two
    places a migration would actually reintroduce them and an AST-constant scan would
    miss both.
    """
    (tmp_path / "0003_probe.py").write_text(
        'op.execute("CREATE TABLE recallatron.topic_thread (id uuid)")\n'
        "# a future revision might add procedural_notes here\n",
        encoding="utf-8",
    )
    scanned, violations = _scan_deferred_names(tmp_path)
    assert scanned == 1
    assert violations == {"0003_probe.py": ["topic_thread", "procedural_notes"]}


def test_the_declared_kinds_are_the_four_ratified_ones() -> None:
    """The Python half of AC 14's database-backed proof.

    Imported here rather than at module scope so this file stays a static scan that
    runs without a database or a loaded module.
    """
    from rheo_recallatron.storage import tables

    assert tables.MEMORY_KINDS == RATIFIED_KINDS
    for name in DEFERRED_FEATURE_NAMES:
        assert name not in tables.MEMORY_KINDS
        assert name not in tables.ENTITY_KINDS

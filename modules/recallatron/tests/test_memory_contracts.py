"""AC 3, AC 14 and AC 19's mechanical halves: scans over this distribution's own tree.

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

3. **The recall tool's description (AC 24)**, read off the declaration: it still says
   what it said before, plus the three sentences that tell an agent to distrust a
   ranked list.

4. **The dedup scan (AC 19).** ``dedup.py`` imports nothing from the lifecycle module
   and names nothing that merges, confirms or supersedes, so no path out of a
   ``dedup_candidates`` result can act on it. An AST walk this time, like the import
   scan, because the claim is about imports and calls rather than text: the module's
   own docstring has to be free to say what it does not do. Beside it, the output
   types are pinned to their fields, with no member of their own a caller could act
   on.

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

_DEDUP_SOURCE = _MODULE_SRC / "rheo_recallatron" / "dedup.py"
_LIFECYCLE_MODULE = "rheo_recallatron.lifecycle"
"""Where correction and supersession live: importing it is a route to acting on a pair,
whatever the imported name is called."""

ACTING_WORDS = ("merge", "confirm", "supersede")
"""What a dedup result must never lead to (AC 19): merging two memories, confirming a
pair, or superseding one memory with the other. Matched case-insensitively inside an
identifier, so ``merge_memories`` and ``MEMORY_SUPERSEDE`` both count."""


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


def _names_lifecycle(module: str) -> bool:
    return module == _LIFECYCLE_MODULE or module.startswith(f"{_LIFECYCLE_MODULE}.")


def _acts(name: str) -> bool:
    folded = name.casefold()
    return any(word in folded for word in ACTING_WORDS)


def _scan_acting_sites(path: Path) -> list[str]:
    """AC 19's two halves over one module's AST, as ``<kind> <name>@<line>``.

    (i) an import of the lifecycle module, or of any name that merges, confirms or
    supersedes; (ii) a called name, or an attribute anywhere (called or passed along),
    that does. A relative import is resolved against the package root, where
    ``dedup.py`` sits, so ``from .lifecycle import ...`` is not a way round (i).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _names_lifecycle(alias.name) or _acts(alias.name):
                    found.append(f"import {alias.name}@{node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = ".".join(part for part in ("rheo_recallatron", module) if part)
            if _names_lifecycle(module):
                found.append(f"import {module}@{node.lineno}")
                continue
            for alias in node.names:
                full = f"{module}.{alias.name}"
                if _names_lifecycle(full) or _acts(alias.name):
                    found.append(f"import {full}@{node.lineno}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if _acts(node.func.id):
                found.append(f"call {node.func.id}@{node.lineno}")
        elif isinstance(node, ast.Attribute) and _acts(node.attr):
            found.append(f"attribute {node.attr}@{node.lineno}")
    return sorted(found)


def _own_members(model: type) -> set[str]:
    """Public names a class declares itself, beyond its pydantic configuration: a
    method, property or class attribute a caller could reach through the type."""
    return {
        name
        for name in vars(model)
        if not name.startswith("_") and name != "model_config"
    }


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


def test_dedup_imports_and_calls_nothing_that_acts_on_a_pair() -> None:
    """AC 19: no code path out of a ``dedup_candidates`` result merges, confirms or
    supersedes. The scan is over the real module, and the handler's presence in the
    tree it parsed is what shows the scan read that module rather than an empty file.
    """
    assert _DEDUP_SOURCE.is_file(), _DEDUP_SOURCE
    tree = ast.parse(_DEDUP_SOURCE.read_text(encoding="utf-8"))
    functions = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert "dedup_candidates" in functions, sorted(functions)
    assert _scan_acting_sites(_DEDUP_SOURCE) == []


def test_the_dedup_scan_flags_a_lifecycle_import_and_a_merge_call(
    tmp_path: Path,
) -> None:
    """The positive control: every route the scan claims to close, in one probe."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import rheo_recallatron.lifecycle\n"
        "from rheo_recallatron.operations import MEMORY_SUPERSEDE\n"
        "from .lifecycle import correct\n"
        "merge_memories(first, second)\n"
        "store.confirm_pair(first, second)\n",
        encoding="utf-8",
    )
    assert _scan_acting_sites(probe) == [
        "attribute confirm_pair@5",
        "call merge_memories@4",
        "import rheo_recallatron.lifecycle@1",
        "import rheo_recallatron.lifecycle@3",
        "import rheo_recallatron.operations.MEMORY_SUPERSEDE@2",
    ]


def test_the_dedup_output_types_carry_nothing_to_act_on() -> None:
    """AC 19's type half: the three fields a pair is, the one field a result is, and no
    member either class declares for itself. The subclass is the positive control for
    the member check."""
    from rheo_recallatron.dedup import DedupCandidates, DedupPair

    assert set(DedupPair.model_fields) == {"ref_a", "ref_b", "score"}
    assert set(DedupCandidates.model_fields) == {"pairs"}
    assert _own_members(DedupPair) == set()
    assert _own_members(DedupCandidates) == set()

    class _Actionable(DedupPair):
        def merge(self) -> None:
            raise AssertionError("never called")

    assert _own_members(_Actionable) == {"merge"}


def test_the_recall_tool_teaches_distrust_of_a_ranked_list() -> None:
    """AC 24: the recall description's three epistemics sentences, and the refusal
    states it named before them.

    Substrings rather than the whole string: dropping a sentence reds this test, while
    rewording one keeps it green.
    """
    from rheo_recallatron.tools import RECALL_TOOL

    description = RECALL_TOOL.description
    # Dense answers are nearest, not relevant. ("relevant" alone proves nothing: the
    # first sentence already says "most relevant first".)
    assert "nearest" in description
    # A memory is a dated claim.
    assert "dated claim" in description
    # Read the provenance first.
    assert "provenance" in description
    for state in ("input_invalid", "purpose_mismatch", "reference_scan_limit"):
        assert state in description, state

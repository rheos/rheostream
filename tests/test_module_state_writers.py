"""FR 10 / AC 7: ``storage/repositories.py`` is the only code that inserts into
``core.module_state`` or ``core.module_schema_version``.

Seam: a static scan over every tracked Python file under ``packages/``, ``apps/``,
``modules/``, ``scripts/`` and ``tests/``, matching an ``insert(...)`` or
``pg_insert(...)`` construction whose first argument is one of the two table objects.
Static rather than behavioural, because the property is about *what the tree
contains*: a runtime check could only observe the writers that happened to run.

**Asserted both ways**, the shape ``tests/test_absent_behaviour.py`` already uses for
its own declared-survivor set. No undeclared file carries a construction, **and**
every declared file still carries one — so an exclusion that has stopped guarding
anything fails here rather than sitting in the list looking load-bearing.

(That file's set is named for a word its own AC 22 probe then greps the whole tracked
tree for, so naming it here would make this file a hit and red that probe. Worth
knowing before writing the obvious cross-reference.)

**The exception set is one file, and the migrations are deliberately not in it.** On
this tree no migration carries such a construction at all: the only mention of either
table under ``migrations/core/versions/`` is a docstring line in ``0001_core_schema.py``
naming the tables it *creates*. Because the set is asserted both ways, declaring the
migrations would red the scan on its own declared set from the first run.

**The scan gets a positive control, twice over.** It is asserted to find both
constructions inside the declared file — so a matcher that silently stopped matching
(a renamed table attribute, a changed call shape) cannot pass by finding nothing
anywhere — and it is run against a synthetic source holding one construction of each
shape, so a walker that matched nothing at all fails on a case whose answer is known
without reading the tree.

The four live construction sites this run routed rather than excepted:
``exports/artifact.py``'s two (the export-restore path, now calling both writers),
``tests/harness/registry.py``'s ``enable_harness_module``, and
``tests/postgres/test_export_restore.py``'s source-workspace seed.
"""

import ast
import subprocess
from pathlib import Path
from typing import Final

_REPO_ROOT = Path(__file__).resolve().parents[1]

SCAN_ROOTS: Final = ("packages", "apps", "modules", "scripts", "tests")
"""Every root that holds Python this repository ships or tests with.

``tests/`` is in the set and has to be: two of the four construction sites this run
fixed were under it, and a scan that could not see them would have called the property
true while the harness was breaking it.
"""

TARGET_TABLES: Final = frozenset({"module_state", "module_schema_version"})
CONSTRUCTORS: Final = frozenset({"insert", "pg_insert"})
"""Both spellings in use: ``sqlalchemy.insert`` and the dialect's own upsert-capable
``postgresql.insert``, whichever local alias a file imports either under.
"""

DECLARED_WRITERS: Final[dict[str, str]] = {
    "packages/core/src/rheo_core/storage/repositories.py": (
        "insert_module_state and insert_module_schema_version themselves: the "
        "writers every other caller goes through"
    ),
}

_SYNTHETIC = """
from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from rheo_core.storage import core_tables

insert(core_tables.module_state).values(module_id="x")
pg_insert(core_tables.module_schema_version).values(module_id="x")
insert(core_tables.workspace_setting).values(key="k")
"""
"""A source the scan's answer is known for: two hits, and one near miss it must skip."""


def _callee(func: ast.expr) -> str | None:
    """The bare name a call is made through: ``insert`` or ``sa.insert`` alike."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _constructions(source: str, label: str) -> tuple[tuple[str, int], ...]:
    """Every ``(table, line)`` an insert construction in ``source`` targets.

    Matched on the *attribute* name rather than the receiver, because an AST scan
    cannot see what a receiver is bound to — ``core_tables.module_state``,
    ``c.module_state`` and ``tables.module_state`` are all the same table and all
    spelled differently. No other object in this tree is named for either table.
    """
    tree = ast.parse(source, filename=label)
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee(node.func) not in CONSTRUCTORS:
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Attribute) and first.attr in TARGET_TABLES:
            found.append((first.attr, node.lineno))
    return tuple(found)


def _tracked_python_files() -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "--", *SCAN_ROOTS],
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return tuple(line for line in result.stdout.splitlines() if line.endswith(".py"))


def _scan() -> dict[str, tuple[tuple[str, int], ...]]:
    """Every scanned file that carries at least one construction, by repo path."""
    hits: dict[str, tuple[tuple[str, int], ...]] = {}
    for path in _tracked_python_files():
        found = _constructions(
            (_REPO_ROOT / path).read_text(encoding="utf-8"), label=path
        )
        if found:
            hits[path] = found
    return hits


# --- the instrument checks itself -----------------------------------------------------


def test_the_scan_matches_both_call_shapes_and_skips_another_table() -> None:
    """A walker that matched nothing would make every assertion below vacuous."""
    found = _constructions(_SYNTHETIC, label="<synthetic>")
    assert [table for table, _ in found] == ["module_state", "module_schema_version"]


def test_the_scan_finds_both_writers_inside_the_declared_file() -> None:
    """The positive control over the real tree, not a synthetic one.

    Both tables, because a scan that had stopped recognising one of them would
    otherwise pass the exclusivity assertion below by finding less than it should.
    """
    for path in DECLARED_WRITERS:
        found = _constructions(
            (_REPO_ROOT / path).read_text(encoding="utf-8"), label=path
        )
        assert {table for table, _ in found} == TARGET_TABLES, (path, found)


# --- FR 10's exclusivity clause -------------------------------------------------------


def test_only_the_declared_writers_insert_into_the_module_state_tables() -> None:
    """AC 7's second clause, asserted in both directions.

    An undeclared hit is a real breach and is fixed where it is, never added to
    :data:`DECLARED_WRITERS`; a declared file that no longer carries one is a stale
    exclusion and is removed.
    """
    hits = _scan()
    declared = set(DECLARED_WRITERS)

    undeclared = {path: found for path, found in hits.items() if path not in declared}
    assert not undeclared, (
        "these files construct an insert against core.module_state or "
        f"core.module_schema_version outside {sorted(declared)}: {undeclared}; route "
        "the call through storage/repositories.py rather than declaring it here"
    )

    stale = sorted(declared - set(hits))
    assert not stale, (
        f"these files are declared module-state writers but no longer construct an "
        f"insert: {stale}; remove the exclusion rather than leaving one that guards "
        "nothing"
    )

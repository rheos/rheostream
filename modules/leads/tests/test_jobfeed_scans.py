"""Only the collector's install, export restore and migrations write the mapping
tables, so no later save path can skip the job search mapping-target rule unseen.

The scan walks every Leads source file the way the repository's boundary scans do:
parse, walk, collect offenders. It resolves import aliases, catches ``insert(table)``
and ``table.insert()`` calls and raw ``INSERT INTO`` text, and is proved non-vacuous
on the real write sites and on planted snippets.
"""

import ast
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "rheo_leads"
ALLOWED = (
    SOURCE / "jobfeed" / "install.py",
    SOURCE / "export.py",
)
MIGRATIONS = SOURCE / "migrations"
TABLES = frozenset({"field_mapping", "field_mapping_rule", "field_mapping_identity"})
SQL = re.compile(
    r"\binsert\s+into\s+(?:leads\s*\.\s*)?(field_mapping(?:_rule|_identity)?)\b",
    re.IGNORECASE,
)


def _named(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def mapping_writes(source: str) -> list[str]:
    tree = ast.parse(source)
    aliases = {
        alias.asname: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname is not None and alias.name in TABLES
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.extend(
                f"sql {m.group(1)}@{node.lineno}" for m in SQL.finditer(node.value)
            )
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if _named(callee, {}) != "insert":
            continue
        # insert(table) from sqlalchemy or its dialects.
        target = _named(node.args[0], aliases) if node.args else None
        if target in TABLES:
            found.append(f"insert {target}@{node.lineno}")
        # table.insert()
        if isinstance(callee, ast.Attribute):
            owner = _named(callee.value, aliases)
            if owner in TABLES:
                found.append(f"insert {owner}@{node.lineno}")
    return found


def _sources() -> list[Path]:
    return sorted(SOURCE.rglob("*.py"))


def test_only_install_export_and_migrations_write_the_mapping_tables() -> None:
    scanned = _sources()
    assert len(scanned) > 20, scanned
    violations = {
        str(path.relative_to(SOURCE)): hits
        for path in scanned
        if path not in ALLOWED and not path.is_relative_to(MIGRATIONS)
        for hits in (mapping_writes(path.read_text(encoding="utf-8")),)
        if hits
    }
    assert violations == {}, violations


def test_the_scan_sees_the_real_write_sites() -> None:
    install = mapping_writes(ALLOWED[0].read_text(encoding="utf-8"))
    assert {hit.split("@")[0] for hit in install} == {
        "insert field_mapping",
        "insert field_mapping_rule",
        "insert field_mapping_identity",
    }
    seed = MIGRATIONS / "versions" / "0003_seed_manual_capture.py"
    assert {
        hit.split("@")[0] for hit in mapping_writes(seed.read_text(encoding="utf-8"))
    } == {"sql field_mapping", "sql field_mapping_rule", "sql field_mapping_identity"}


def test_planted_writes_are_caught_and_reads_are_not() -> None:
    planted = {
        "from sqlalchemy import insert\n"
        "from rheo_leads.storage import tables as t\n"
        "uow.connection.execute(insert(t.field_mapping_rule).values())\n": [
            "insert field_mapping_rule@3"
        ],
        "from sqlalchemy.dialects.postgresql import insert\n"
        "from rheo_leads.storage.tables import field_mapping as fm\n"
        "conn.execute(insert(fm))\n": ["insert field_mapping@3"],
        "import sqlalchemy\n"
        "conn.execute(sqlalchemy.insert(tables.field_mapping_identity))\n": [
            "insert field_mapping_identity@2"
        ],
        "conn.execute(t.field_mapping.insert().values())\n": ["insert field_mapping@1"],
        'conn.execute(text("INSERT INTO leads.field_mapping_rule VALUES (1)"))\n': [
            "sql field_mapping_rule@1"
        ],
        "conn.execute(text('insert into field_mapping_identity values (1)'))\n": [
            "sql field_mapping_identity@1"
        ],
    }
    for source, expected in planted.items():
        assert mapping_writes(source) == expected, source
    reads = (
        "conn.execute(select(t.field_mapping).where(t.field_mapping.c.id == x))\n"
        "conn.execute(insert(t.field_mapping_rules_archive))\n"
        "conn.execute(update(t.field_mapping_identity).values(a=1))\n"
        'note = "the field_mapping table is read here"\n'
    )
    assert mapping_writes(reads) == []

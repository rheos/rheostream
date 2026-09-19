"""AC 3: Recallatron owns its schema and reaches storage only through core.

The live relation diff and the two source scans are deliberately independent of the
``WorkspaceContext`` and ``SecretScope`` construction gates.  Each mechanism has a
positive control so an empty walk or a matcher that no longer matches cannot pass.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from conftest import ClusterSession
from rheo_core.migrations.module_chain import run_module_chain
from rheo_core.modules import load_modules, reset_surfaces
from rheo_core.operations.registry import OperationRegistry
from rheo_core.refs.resolver import ResolverRegistry
from rheo_core.storage.provisioning import core_version
from rheo_core.tokens.sets import ToolRegistry
from rheo_recallatron import MANIFEST
from sqlalchemy import Connection, Engine, text

pytestmark = pytest.mark.postgres

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_SRC = _REPO_ROOT / "modules" / "recallatron" / "src"
_OWNED_SCHEMA = MANIFEST.storage.schema_name
_VERSION_TABLE = "alembic_version_recallatron"
_FOREIGN_QUALIFIED = re.compile(
    r"\b(?:core|relationships|leads|current)\.[A-Za-z_][A-Za-z0-9_]*\b"
)
_FOREIGN_STORAGE_MODULES = (
    "rheo_core.storage",
    "rheo_relationships.storage",
    "rheo_leads.storage",
    "rheo_current.storage",
)
_RAW_CONNECTION_CALLS = frozenset(
    {"connect", "create_engine", "create_async_engine", "engine_from_config"}
)
_DSN_PREFIXES = ("postgresql://", "postgresql+")


@pytest.fixture
def recallatron_loaded() -> Iterator[None]:
    reset_surfaces()
    load_modules(
        registry=OperationRegistry(),
        resolvers=ResolverRegistry(),
        tools=ToolRegistry(),
        allow=frozenset({MANIFEST.module_id}),
    )
    yield
    reset_surfaces()


def _workspace_engine(cluster: ClusterSession, workspace: UUID) -> tuple[str, Engine]:
    row = cluster.registry_row(workspace)
    return row.database_name, cluster.backend.pools.engine_for(row.database_name)


def _relations(connection: Connection) -> set[tuple[str, str]]:
    rows = connection.execute(
        text(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema', 'pg_catalog')"
        )
    )
    return {(str(schema), str(name)) for schema, name in rows}


def _outside_owned_schema(relations: set[tuple[str, str]]) -> set[tuple[str, str]]:
    return {relation for relation in relations if relation[0] != _OWNED_SCHEMA}


def test_recallatron_migration_creates_only_its_version_table_in_its_schema(
    cluster: ClusterSession,
    workspace: UUID,
    recallatron_loaded: None,
) -> None:
    database_name, engine = _workspace_engine(cluster, workspace)
    with engine.begin() as connection:
        before = _relations(connection)
        run_module_chain(
            connection,
            MANIFEST,
            expected_database=database_name,
            core_version=core_version(),
        )
        created = _relations(connection) - before

    assert created == {(_OWNED_SCHEMA, _VERSION_TABLE)}
    assert not _outside_owned_schema(created)


def test_relation_diff_reports_a_relation_outside_the_owned_schema(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _workspace_engine(cluster, workspace)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            before = _relations(connection)
            connection.execute(
                text("CREATE TABLE public.recallatron_probe_leak (id int)")
            )
            created = _relations(connection) - before
            assert _outside_owned_schema(created) == {
                ("public", "recallatron_probe_leak")
            }
        finally:
            transaction.rollback()


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    imported: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append((node.lineno, node.module))
    return imported


def _foreign_storage_sites(tree: ast.AST) -> list[str]:
    sites: list[str] = []
    for line, module in _imported_modules(tree):
        if module.startswith(_FOREIGN_STORAGE_MODULES):
            sites.append(f"storage import {module}@{line}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _FOREIGN_QUALIFIED.finditer(node.value):
                sites.append(f"qualified name {match.group(0)}@{node.lineno}")
    return sorted(sites)


def _terminal_name(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _raw_database_sites(tree: ast.AST) -> list[str]:
    sites: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _terminal_name(node.func)
            if name in _RAW_CONNECTION_CALLS:
                sites.append(f"call {name}@{node.lineno}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith(_DSN_PREFIXES):
                sites.append(f"dsn@{node.lineno}")
    return sorted(sites)


def _scan_sources(root: Path, matcher: object) -> tuple[int, dict[str, list[str]]]:
    scan = matcher
    assert callable(scan)
    violations: dict[str, list[str]] = {}
    files = _python_files(root)
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        sites = scan(tree)
        if sites:
            violations[str(path.relative_to(root))] = sites
    return len(files), violations


def test_recallatron_names_no_foreign_storage(tmp_path: Path) -> None:
    scanned, violations = _scan_sources(_MODULE_SRC, _foreign_storage_sites)
    assert scanned, "no Recallatron Python files scanned"
    assert not violations, f"Recallatron names foreign storage: {violations}"

    probe = tmp_path / "probe.py"
    probe.write_text('table = "core.module_state"\n', encoding="utf-8")
    control_scanned, control = _scan_sources(tmp_path, _foreign_storage_sites)
    assert control_scanned == 1
    assert control == {"probe.py": ["qualified name core.module_state@1"]}


def test_recallatron_constructs_no_raw_database_access(tmp_path: Path) -> None:
    scanned, violations = _scan_sources(_MODULE_SRC, _raw_database_sites)
    assert scanned, "no Recallatron Python files scanned"
    assert not violations, f"Recallatron constructs raw database access: {violations}"

    probe = tmp_path / "probe.py"
    probe.write_text(
        "from sqlalchemy import create_engine\n"
        'dsn = "postgresql://probe.invalid/db"\n'
        "create_engine(dsn)\n",
        encoding="utf-8",
    )
    control_scanned, control = _scan_sources(tmp_path, _raw_database_sites)
    assert control_scanned == 1
    assert control == {"probe.py": ["call create_engine@3", "dsn@2"]}

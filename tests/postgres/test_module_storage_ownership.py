"""AC 3: Recallatron owns its schema and reaches storage only through core.

The live object diff and the two source scans are deliberately independent of the
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
_EXPECTED_SKELETON_OBJECTS = {
    (_OWNED_SCHEMA, _OWNED_SCHEMA, "schema"),
    (_OWNED_SCHEMA, _VERSION_TABLE, "r"),
    (_OWNED_SCHEMA, f"{_VERSION_TABLE}_pkc", "i"),
    (
        _OWNED_SCHEMA,
        f"{_VERSION_TABLE}_pkc on {_OWNED_SCHEMA}.{_VERSION_TABLE}",
        "table constraint",
    ),
}
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
_DATABASE_MODULES = frozenset(
    {"sqlalchemy", "psycopg", "psycopg2", "asyncpg", "pg8000", "sqlite3"}
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


def _objects(connection: Connection) -> set[tuple[str, str, str]]:
    rows = connection.execute(
        text(
            "SELECT namespace.nspname::text, object.relname::text, "
            "object.relkind::text "
            "FROM pg_catalog.pg_class AS object "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = object.relnamespace "
            "WHERE namespace.nspname <> 'information_schema' "
            "AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
            "AND object.relkind IN ('r', 'p', 'i', 'I', 'S', 't', 'v', 'm', 'f', 'c') "
            "UNION "
            "SELECT identified.schema, identified.identity, identified.type "
            "FROM (SELECT DISTINCT classid, objid FROM pg_catalog.pg_depend "
            "      WHERE objsubid = 0 AND classid <> 'pg_catalog.pg_class'::regclass) "
            "AS dependent "
            "CROSS JOIN LATERAL pg_catalog.pg_identify_object("
            "dependent.classid, dependent.objid, 0) AS identified "
            "WHERE identified.schema <> 'information_schema' "
            "AND identified.schema NOT LIKE 'pg\\_%' ESCAPE '\\' "
            # Relations already account for their automatic row types; generated
            # arrays are accounted for by their element type (including enums).
            "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_type AS type "
            "WHERE dependent.classid = 'pg_catalog.pg_type'::regclass "
            "AND dependent.objid = type.oid AND (type.typrelid <> 0 OR EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_type AS element "
            "WHERE element.typarray = type.oid))) "
            "UNION "
            "SELECT nspname, nspname, 'schema' FROM pg_catalog.pg_namespace "
            "WHERE nspname <> 'information_schema' "
            "AND nspname NOT LIKE 'pg\\_%' ESCAPE '\\'"
        )
    )
    return {
        (str(schema), str(name), str(object_kind)) for schema, name, object_kind in rows
    }


def _outside_owned_schema(
    objects: set[tuple[str, str, str]],
) -> set[tuple[str, str, str]]:
    return {object_ for object_ in objects if object_[0] != _OWNED_SCHEMA}


def test_recallatron_migration_creates_only_its_version_table_in_its_schema(
    cluster: ClusterSession,
    workspace: UUID,
    recallatron_loaded: None,
) -> None:
    database_name, engine = _workspace_engine(cluster, workspace)
    with engine.begin() as connection:
        before = _objects(connection)
        run_module_chain(
            connection,
            MANIFEST,
            expected_database=database_name,
            core_version=core_version(),
        )
        created = _objects(connection) - before

    assert created == _EXPECTED_SKELETON_OBJECTS
    assert not _outside_owned_schema(created)


def test_object_census_reports_a_non_table_object_outside_the_owned_schema(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _workspace_engine(cluster, workspace)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            before = _objects(connection)
            connection.execute(text("CREATE SEQUENCE public.recallatron_probe_leak"))
            created = _objects(connection) - before
            assert _outside_owned_schema(created) == {
                ("public", "recallatron_probe_leak", "S")
            }
        finally:
            transaction.rollback()


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        (
            "CREATE TYPE public.recallatron_enum_leak AS ENUM ('probe')",
            ("public", "public.recallatron_enum_leak", "type"),
        ),
        (
            "CREATE DOMAIN public.recallatron_domain_leak AS integer",
            ("public", "public.recallatron_domain_leak", "type"),
        ),
        (
            "CREATE FUNCTION public.recallatron_function_leak() RETURNS integer "
            "LANGUAGE SQL AS 'SELECT 1'",
            ("public", "public.recallatron_function_leak()", "function"),
        ),
        (
            "CREATE SCHEMA recallatron_schema_leak",
            ("recallatron_schema_leak", "recallatron_schema_leak", "schema"),
        ),
    ],
    ids=["enum", "domain", "function", "schema"],
)
def test_object_census_reports_non_relation_objects_outside_the_owned_schema(
    cluster: ClusterSession,
    workspace: UUID,
    statement: str,
    expected: tuple[str, str, str],
) -> None:
    _, engine = _workspace_engine(cluster, workspace)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            before = _objects(connection)
            connection.execute(text(statement))
            created = _objects(connection) - before
            assert _outside_owned_schema(created) == {expected}
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
            imported.extend(
                (node.lineno, f"{node.module}.{alias.name}") for alias in node.names
            )
    return imported


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                aliases[local] = alias.name if alias.asname else local
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _qualified_name(expr: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Name):
        return aliases.get(expr.id, expr.id)
    if isinstance(expr, ast.Attribute):
        parent = _qualified_name(expr.value, aliases)
        if parent is not None:
            return f"{parent}.{expr.attr}"
    return None


def _is_foreign_storage(name: str) -> bool:
    return any(
        name == module or name.startswith(f"{module}.")
        for module in _FOREIGN_STORAGE_MODULES
    )


def _foreign_storage_sites(tree: ast.AST) -> list[str]:
    sites: list[str] = []
    for line, module in _imported_modules(tree):
        if _is_foreign_storage(module):
            sites.append(f"storage import {module}@{line}")
    aliases = _import_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            name = _qualified_name(node, aliases)
            if name is not None and _is_foreign_storage(name):
                sites.append(f"storage access {name}@{node.lineno}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _FOREIGN_QUALIFIED.finditer(node.value):
                sites.append(f"qualified name {match.group(0)}@{node.lineno}")
    return sorted(sites)


def _raw_database_sites(tree: ast.AST) -> list[str]:
    sites: list[str] = []
    aliases = _import_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _qualified_name(node.func, aliases)
            if name is not None and (
                name in _RAW_CONNECTION_CALLS
                or (
                    name.split(".")[0] in _DATABASE_MODULES
                    and name.split(".")[-1] in _RAW_CONNECTION_CALLS
                )
            ):
                sites.append(f"call {name.split('.')[-1]}@{node.lineno}")
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


@pytest.mark.parametrize(
    ("bad", "good"),
    [
        (
            "from rheo_core import storage as store",
            "from rheo_core import storage_helpers as store",
        ),
        (
            "from rheo_leads.storage import Repository as Repo",
            "from rheo_leads.storage_helpers import Repository as Repo",
        ),
        (
            "import rheo_current as module\nmodule.storage.Repository()",
            "import rheo_current as module\nmodule.storage_helpers.Repository()",
        ),
        (
            "import rheo_relationships\nrheo_relationships.storage.Repository()",
            "import rheo_relationships\n"
            "rheo_relationships.storage_helpers.Repository()",
        ),
    ],
)
def test_foreign_storage_scan_resolves_parent_packages_and_aliases(
    tmp_path: Path, bad: str, good: str
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(bad, encoding="utf-8")
    assert _scan_sources(tmp_path, _foreign_storage_sites)[1]
    probe.write_text(good, encoding="utf-8")
    assert _scan_sources(tmp_path, _foreign_storage_sites) == (1, {})


@pytest.mark.parametrize(
    ("bad", "good", "name"),
    [
        (
            "from sqlalchemy import create_engine as make\nmake(url)",
            "from sqlalchemy import create_mock_engine as make\nmake(url)",
            "create_engine",
        ),
        (
            "from sqlalchemy.ext.asyncio import create_async_engine as make\nmake(url)",
            "from engine_tools import create_async_engine as make\nmake(url)",
            "create_async_engine",
        ),
        (
            "import sqlalchemy as sa\nsa.create_engine(url)",
            "import engine_tools as sa\nsa.create_engine(url)",
            "create_engine",
        ),
        (
            "from sqlalchemy import engine_from_config as make\nmake(config)",
            "from engine_tools import engine_from_config as make\nmake(config)",
            "engine_from_config",
        ),
        (
            "from psycopg import connect as open_db\nopen_db(url)",
            "from message_bus import connect as open_db\nopen_db(url)",
            "connect",
        ),
    ],
)
def test_raw_database_scan_resolves_aliased_calls(
    tmp_path: Path, bad: str, good: str, name: str
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(bad, encoding="utf-8")
    assert _scan_sources(tmp_path, _raw_database_sites) == (
        1,
        {"probe.py": [f"call {name}@2"]},
    )
    probe.write_text(good, encoding="utf-8")
    assert _scan_sources(tmp_path, _raw_database_sites) == (1, {})

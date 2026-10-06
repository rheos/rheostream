"""AC 3: Recallatron owns its schema and reaches storage only through core.

The live object diff and the two source scans are deliberately independent of the
``WorkspaceContext`` and ``SecretScope`` construction gates.  Each mechanism has a
positive control so an empty walk or a matcher that no longer matches cannot pass.

AC 3 is about **platform storage**: the workspace database the core owns. The raw
database scan has exactly one named exemption, and it is not platform storage: the
migration groundwork's predecessor snapshot reader,
``rheo_recallatron/migration/predecessor/sqlite_source.py``, opens a read-only
external input (a local SQLite snapshot file, ``mode=ro&immutable=1``). The rule is
exact: at most one ``sqlite3.connect`` call, passing the literal keyword ``uri=True``,
in that one file (matched on its full path relative to the module's ``src``) is
exempt. Every other raw call in that file, and ``sqlite3.connect`` in any other file,
is still a violation. The URI's own contents are built at runtime, so the pinned-URI
unit test in ``tests/test_migration_predecessor_reader.py`` covers them, not this scan
(see :func:`_raw_database_violations` and its controls).
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import create_required_extensions, loaded_probe_modules
from rheo_core.migrations.module_chain import run_module_chain
from rheo_core.storage.provisioning import core_version
from rheo_recallatron import MANIFEST
from sqlalchemy import Connection, Engine, text

pytestmark = pytest.mark.postgres

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_SRC = _REPO_ROOT / "modules" / "recallatron" / "src"
_OWNED_SCHEMA = MANIFEST.storage.schema_name
_VERSION_TABLE = "alembic_version_recallatron"
_CHAIN_VERSION_OBJECTS = {
    (_OWNED_SCHEMA, _OWNED_SCHEMA, "schema"),
    (_OWNED_SCHEMA, _VERSION_TABLE, "r"),
    (_OWNED_SCHEMA, f"{_VERSION_TABLE}_pkc", "i"),
    (
        _OWNED_SCHEMA,
        f"{_VERSION_TABLE}_pkc on {_OWNED_SCHEMA}.{_VERSION_TABLE}",
        "table constraint",
    ),
}
"""The schema and the chain's own version table: what every revision has in common.

**A subset now, where it used to be the whole set.** This was
``_EXPECTED_SKELETON_OBJECTS`` and the census below compared against it for equality,
which was exact while the chain created nothing else. Revision ``0002_memory_records``
creates the real memory schema, so the exact inventory moved to
``tests/postgres/test_memory_records.py``, which is the file that owns the contract it
is an inventory of. What stays here is this file's own claim — AC 3's, in its
docstring: whatever the chain creates, it creates **inside the owned schema**, and this
subset is the positive control that the census saw the chain run at all rather than
walking an empty diff.
"""
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
# The one named exemption from the raw database scan (see the module docstring): the
# predecessor snapshot is a read-only external input, not platform storage, so its
# reader may open it. One file (a path relative to the module's ``src``), and in it
# at most ONE ``sqlite3.connect`` call, and only one passing ``uri=True`` (keyword,
# literal ``True``): the audited read-only open. A second connect in the reader, or
# one without ``uri=True``, is reported. The URI string's own contents
# (``mode=ro&immutable=1``) are built at runtime and are not provable from the AST;
# the pinned-URI unit test in ``tests/test_migration_predecessor_reader.py`` covers
# them.
_SNAPSHOT_READER = "rheo_recallatron/migration/predecessor/sqlite_source.py"
_SNAPSHOT_READER_CALL = "sqlite3.connect"


@pytest.fixture
def recallatron_loaded(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Recallatron loaded through the harness's one recipe.

    Through :func:`loaded_probe_modules` rather than a bare ``load_modules`` here,
    because the manifest now declares a settings key marked
    ``explicit_per_workspace``. ``loader.py``'s ``_register`` writes every declared key
    into the process-global settings registry, which publishes no unregister, and
    provisioning's step 4 then writes a row for that key into every workspace
    provisioned later in the session — making other files' exact row assertions depend
    on collection order. The harness recipe rebinds the loader's registry for the
    duration, which is the isolation this file now needs and did not before.
    """
    with loaded_probe_modules(monkeypatch, MANIFEST.module_id):
        yield


def _workspace_engine(cluster: ClusterSession, workspace: UUID) -> tuple[str, Engine]:
    row = cluster.registry_row(workspace)
    return row.database_name, cluster.backend.pools.engine_for(row.database_name)


def _objects(connection: Connection) -> set[tuple[str, str, str]]:
    rows = connection.execute(
        text(
            # These objects inherit their namespace from a relation, while
            # pg_identify_object reports a NULL schema for them. Internal
            # triggers and a view's generated _RETURN rule belong to the
            # constraint/relation already represented in the census.
            "WITH table_owned (classid, objid, relid, is_internal) AS ("
            "SELECT 'pg_catalog.pg_trigger'::regclass, oid, tgrelid, tgisinternal "
            "FROM pg_catalog.pg_trigger "
            "UNION ALL "
            "SELECT 'pg_catalog.pg_policy'::regclass, oid, polrelid, false "
            "FROM pg_catalog.pg_policy "
            "UNION ALL "
            "SELECT 'pg_catalog.pg_rewrite'::regclass, rule.oid, rule.ev_class, "
            "EXISTS (SELECT 1 FROM pg_catalog.pg_depend AS dependency "
            "WHERE dependency.classid = 'pg_catalog.pg_rewrite'::regclass "
            "AND dependency.objid = rule.oid AND dependency.deptype = 'i') "
            "FROM pg_catalog.pg_rewrite AS rule) "
            "SELECT namespace.nspname::text, object.relname::text, "
            "object.relkind::text "
            "FROM pg_catalog.pg_class AS object "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = object.relnamespace "
            "WHERE namespace.nspname <> 'information_schema' "
            "AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
            "AND object.relkind IN ('r', 'p', 'i', 'I', 'S', 't', 'v', 'm', 'f', 'c') "
            "UNION "
            "SELECT COALESCE(identified.schema, owner_namespace.nspname), "
            "identified.identity, identified.type "
            "FROM (SELECT DISTINCT classid, objid FROM pg_catalog.pg_depend "
            "      WHERE objsubid = 0 AND classid <> 'pg_catalog.pg_class'::regclass) "
            "AS dependent "
            "CROSS JOIN LATERAL pg_catalog.pg_identify_object("
            "dependent.classid, dependent.objid, 0) AS identified "
            "LEFT JOIN table_owned ON table_owned.classid = dependent.classid "
            "AND table_owned.objid = dependent.objid "
            "LEFT JOIN pg_catalog.pg_class AS owner_relation "
            "ON owner_relation.oid = table_owned.relid "
            "LEFT JOIN pg_catalog.pg_namespace AS owner_namespace "
            "ON owner_namespace.oid = owner_relation.relnamespace "
            "WHERE COALESCE(identified.schema, owner_namespace.nspname) "
            "<> 'information_schema' "
            "AND COALESCE(identified.schema, owner_namespace.nspname) "
            "NOT LIKE 'pg\\_%' ESCAPE '\\' "
            "AND NOT COALESCE(table_owned.is_internal, false) "
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


def test_recallatron_migration_creates_objects_only_inside_its_own_schema(
    cluster: ClusterSession,
    workspace: UUID,
    recallatron_loaded: None,
) -> None:
    """AC 3: every object the chain creates is inside ``recallatron``.

    The extensions are installed first, exactly as ``core.module.install`` step 6 does
    and **before** the ``before`` snapshot, so the objects ``CREATE EXTENSION`` puts in
    ``public`` are not attributed to this module's migration. They are the core's
    step and the core's statement; a migration of this module's that created them
    would be a real leak, and is what the assertion below would catch.
    """
    database_name, engine = _workspace_engine(cluster, workspace)
    with engine.begin() as connection:
        create_required_extensions(connection, MANIFEST)
        before = _objects(connection)
        run_module_chain(
            connection,
            MANIFEST,
            expected_database=database_name,
            core_version=core_version(),
        )
        created = _objects(connection) - before

    assert _CHAIN_VERSION_OBJECTS <= created
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


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        (
            "CREATE TRIGGER recallatron_trigger_leak "
            "BEFORE UPDATE ON core.module_state FOR EACH ROW "
            "EXECUTE FUNCTION pg_catalog.suppress_redundant_updates_trigger()",
            ("core", "recallatron_trigger_leak on core.module_state", "trigger"),
        ),
        (
            "CREATE POLICY recallatron_policy_leak ON core.module_state USING (true)",
            ("core", "recallatron_policy_leak on core.module_state", "policy"),
        ),
        (
            "CREATE RULE recallatron_rule_leak AS ON DELETE TO core.module_state "
            "DO INSTEAD NOTHING",
            ("core", "recallatron_rule_leak on core.module_state", "rule"),
        ),
    ],
    ids=["trigger", "policy", "rule"],
)
def test_object_census_reports_table_owned_objects_outside_the_owned_schema(
    cluster: ClusterSession,
    workspace: UUID,
    recallatron_loaded: None,
    statement: str,
    expected: tuple[str, str, str],
) -> None:
    database_name, engine = _workspace_engine(cluster, workspace)
    # In its own committed transaction, so the rollback below restores the database to
    # a state the ``before`` snapshot describes: extensions installed, migration not.
    with engine.begin() as setup:
        create_required_extensions(setup, MANIFEST)
    with engine.connect() as connection:
        transaction = connection.begin()
        before = _objects(connection)
        try:
            run_module_chain(
                connection,
                MANIFEST,
                expected_database=database_name,
                core_version=core_version(),
            )
            migrated = _objects(connection) - before
            assert _CHAIN_VERSION_OBJECTS <= migrated
            assert not _outside_owned_schema(migrated)
            connection.execute(text(statement))
            created = _objects(connection) - before
            assert created == migrated | {expected}
            assert _outside_owned_schema(created) == {expected}
        finally:
            transaction.rollback()
            assert _objects(connection) == before


def test_object_census_does_not_double_count_internal_triggers_and_view_rules(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _workspace_engine(cluster, workspace)
    with engine.connect() as connection:
        transaction = connection.begin()
        before = _objects(connection)
        try:
            connection.execute(
                text(
                    "CREATE TABLE public.recallatron_automatic_probe ("
                    "id integer PRIMARY KEY, parent_id integer "
                    "REFERENCES public.recallatron_automatic_probe(id))"
                )
            )
            connection.execute(
                text(
                    "CREATE VIEW public.recallatron_automatic_view AS "
                    "SELECT id FROM public.recallatron_automatic_probe"
                )
            )
            assert _objects(connection) - before == {
                ("public", "recallatron_automatic_probe", "r"),
                ("public", "recallatron_automatic_probe_pkey", "i"),
                (
                    "public",
                    "recallatron_automatic_probe_pkey "
                    "on public.recallatron_automatic_probe",
                    "table constraint",
                ),
                (
                    "public",
                    "recallatron_automatic_probe_parent_id_fkey "
                    "on public.recallatron_automatic_probe",
                    "table constraint",
                ),
                ("public", "recallatron_automatic_view", "v"),
            }
        finally:
            transaction.rollback()
            assert _objects(connection) == before


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


def _exempt_sites(relative: str, tree: ast.AST) -> Counter[str]:
    """The sites the one named exemption covers in ``relative``: the first
    ``sqlite3.connect(..., uri=True)`` call in the snapshot reader's own file, and
    nothing else (see ``_SNAPSHOT_READER``'s comment)."""
    if relative != _SNAPSHOT_READER:
        return Counter()
    aliases = _import_aliases(tree)
    audited = sorted(
        (node.lineno, node.col_offset)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _qualified_name(node.func, aliases) == _SNAPSHOT_READER_CALL
        and any(
            keyword.arg == "uri"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
    )
    return Counter(f"call connect@{line}" for line, _ in audited[:1])


def _raw_database_violations(root: Path) -> tuple[int, dict[str, list[str]]]:
    scanned, violations = _scan_sources(root, _raw_database_sites)
    remaining: dict[str, list[str]] = {}
    for relative, sites in violations.items():
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        # Counter subtraction removes one site per exempt call, so a second raw call
        # on the same line (say a psycopg.connect) is still reported.
        kept = sorted((Counter(sites) - _exempt_sites(relative, tree)).elements())
        if kept:
            remaining[relative] = kept
    return scanned, remaining


def _foreign_storage_violations(root: Path) -> tuple[int, dict[str, list[str]]]:
    scanned, violations = _scan_sources(root, _foreign_storage_sites)
    relative = "rheo_recallatron/lifecycle.py"
    if relative not in violations:
        return scanned, violations
    # Canonical deletion subscription names are reference values, not table access.
    # Exempt only these exact literals in the one declared tuple in this file.
    # Imports, SQL strings and all other occurrences still go through the scanner.
    tree = ast.parse((root / relative).read_text(encoding="utf-8"))
    allowed = {"leads.observation", "leads.opportunity", "relationships.party"}
    exempt: Counter[str] = Counter()
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "DELETION_PARTICIPANT_TYPES"
            and isinstance(node.value, ast.Tuple)
        ):
            for element in node.value.elts:
                if (
                    isinstance(element, ast.Constant)
                    and isinstance(element.value, str)
                    and element.value in allowed
                ):
                    exempt[f"qualified name {element.value}@{element.lineno}"] += 1
    kept = sorted((Counter(violations[relative]) - exempt).elements())
    if kept:
        violations[relative] = kept
    else:
        del violations[relative]
    return scanned, violations


def test_record_subscription_exception_still_catches_storage_access(
    tmp_path: Path,
) -> None:
    source = tmp_path / "rheo_recallatron" / "lifecycle.py"
    source.parent.mkdir()
    source.write_text(
        'DELETION_PARTICIPANT_TYPES: Final = ("leads.observation",)\n',
        encoding="utf-8",
    )
    assert _foreign_storage_violations(tmp_path) == (1, {})
    source.write_text(
        source.read_text() + 'sql = "SELECT * FROM leads.observation"\n'
        'table = "relationships.party"\n'
        "from rheo_leads.storage import tables\n",
        encoding="utf-8",
    )
    _, violations = _foreign_storage_violations(tmp_path)
    sites = violations["rheo_recallatron/lifecycle.py"]
    assert "qualified name leads.observation@2" in sites
    assert "qualified name relationships.party@3" in sites
    assert any(site.startswith("storage import rheo_leads.storage") for site in sites)
    # The same declaration in another file does not get the exception.
    other = source.with_name("other.py")
    other.write_text('DELETION_PARTICIPANT_TYPES: Final = ("leads.observation",)\n')
    assert "rheo_recallatron/other.py" in _foreign_storage_violations(tmp_path)[1]


def test_recallatron_names_no_foreign_storage(tmp_path: Path) -> None:
    scanned, violations = _foreign_storage_violations(_MODULE_SRC)
    assert scanned, "no Recallatron Python files scanned"
    assert not violations, f"Recallatron names foreign storage: {violations}"

    probe = tmp_path / "probe.py"
    probe.write_text('table = "core.module_state"\n', encoding="utf-8")
    control_scanned, control = _scan_sources(tmp_path, _foreign_storage_sites)
    assert control_scanned == 1
    assert control == {"probe.py": ["qualified name core.module_state@1"]}


def test_recallatron_constructs_no_raw_database_access(tmp_path: Path) -> None:
    scanned, violations = _raw_database_violations(_MODULE_SRC)
    assert scanned, "no Recallatron Python files scanned"
    assert not violations, f"Recallatron constructs raw database access: {violations}"
    # The exemption is in use: without it the real tree would report the reader.
    assert _scan_sources(_MODULE_SRC, _raw_database_sites)[1].keys() == {
        _SNAPSHOT_READER
    }

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


def test_the_snapshot_reader_exemption_covers_one_file_and_one_call(
    tmp_path: Path,
) -> None:
    """Positive controls for the one named exemption. Every probe outside the exempt
    path uses the exact accepted form, ``sqlite3.connect(uri, uri=True)``, so only
    the path decides: a sibling in ``migration/``, a same-named file elsewhere, and
    the full reader path nested under a prefix are all reported. So is any other raw
    call inside the exempt file itself."""
    accepted = "import sqlite3\nsqlite3.connect(uri, uri=True)\n"
    nested = f"extra/{_SNAPSHOT_READER}"
    probes = {
        _SNAPSHOT_READER: (
            "import sqlite3\n"
            "import psycopg\n"
            "sqlite3.connect(uri, uri=True)\n"
            "psycopg.connect(url)\n"
            'dsn = "postgresql://probe.invalid/db"\n'
        ),
        "rheo_recallatron/migration/cli.py": accepted,
        "rheo_recallatron/sqlite_source.py": accepted,
        nested: accepted,
    }
    for relative, source in probes.items():
        probe = tmp_path / relative
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(source, encoding="utf-8")

    scanned, violations = _raw_database_violations(tmp_path)

    assert scanned == 4
    assert violations == {
        _SNAPSHOT_READER: ["call connect@4", "dsn@5"],
        "rheo_recallatron/migration/cli.py": ["call connect@2"],
        "rheo_recallatron/sqlite_source.py": ["call connect@2"],
        nested: ["call connect@2"],
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import sqlite3\n"
            "sqlite3.connect(uri, uri=True)\n"
            "sqlite3.connect(other, uri=True)\n",
            ["call connect@3"],
        ),
        ("import sqlite3\nsqlite3.connect(path)\n", ["call connect@2"]),
        ("import sqlite3\nsqlite3.connect(path, uri=False)\n", ["call connect@2"]),
        ("import sqlite3\nsqlite3.connect(path, uri=flag)\n", ["call connect@2"]),
        ("import psycopg\npsycopg.connect(u, uri=True)\n", ["call connect@2"]),
    ],
    ids=["second-connect", "no-uri", "uri-false", "uri-not-literal", "other-callee"],
)
def test_the_snapshot_reader_exemption_covers_only_one_uri_open(
    tmp_path: Path, source: str, expected: list[str]
) -> None:
    """Inside the exempt file itself: a second ``sqlite3.connect``, one that does not
    pass the literal ``uri=True``, or any other callee's ``connect`` (even with
    ``uri=True`` and alone in the file) is still reported."""
    probe = tmp_path / _SNAPSHOT_READER
    probe.parent.mkdir(parents=True)
    probe.write_text(source, encoding="utf-8")

    assert _raw_database_violations(tmp_path) == (1, {_SNAPSHOT_READER: expected})


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

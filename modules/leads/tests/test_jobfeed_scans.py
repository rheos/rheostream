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


# --- token bytes stay inside core ---------------------------------------------------

MODULES = Path(__file__).resolve().parents[2]
CORE_OAUTH = (
    MODULES.parent
    / "packages"
    / "core"
    / "src"
    / "rheo_core"
    / "connectors"
    / "oauth.py"
)
SECRET_API = frozenset({"SecretRef", "SecretStore", "SecretValue", "SecretScope"})
SECRET_SEGMENTS = frozenset({"oauth", "secrets", "connector-oauth"})
REFERENCE_NAMES = re.compile(
    r"(token|secret)_ref|refresh_ref|(registration|refresh)_token"
)
OPENERS = frozenset({"open", "fdopen", "read_bytes", "read_text"})


def expose_uses(source: str) -> list[str]:
    """Every ``.expose`` attribute (a call or a bound method taken) and every
    ``getattr(x, "expose")``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "expose":
            found.append(f"expose@{node.lineno}")
        if (
            isinstance(node, ast.Call)
            and _named(node.func, {}) == "getattr"
            and any(
                isinstance(arg, ast.Constant) and arg.value == "expose"
                for arg in node.args
            )
        ):
            found.append(f"getattr expose@{node.lineno}")
    return found


def _segments(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and bool(SECRET_SEGMENTS & set(node.value.strip("/").split("/")))
    )


def _names_reference(node: ast.AST) -> bool:
    return any(
        (isinstance(sub, ast.Name) and REFERENCE_NAMES.search(sub.id))
        or (isinstance(sub, ast.Attribute) and REFERENCE_NAMES.search(sub.attr))
        for sub in ast.walk(node)
    )


def token_reads(source: str) -> list[str]:
    """Every way a module could read token bytes: resolving a secret reference,
    naming the secret store's types or the data root, building a path into a
    connection's ``oauth/`` or the ``secrets`` directory, or opening a file named
    by a reference column."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module.endswith("data_root") or node.module.startswith(
                "rheo_core.secrets"
            ):
                found.append(f"import {node.module}@{node.lineno}")
            for alias in node.names:
                if alias.name in SECRET_API or alias.name == "workspace_dir_for":
                    found.append(f"import {alias.name}@{node.lineno}")
        if isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in SECRET_API or name == "workspace_dir_for":
                found.append(f"name {name}@{node.lineno}")
        if isinstance(node, ast.Constant) and node.value == "RHEO_DATA_ROOT":
            found.append(f"data root@{node.lineno}")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if _segments(node.left) or _segments(node.right):
                found.append(f"secret path@{node.lineno}")
        if not isinstance(node, ast.Call):
            continue
        callee = _named(node.func, {})
        # A store's ``resolve`` is a method taking a reference; ``Path.resolve()``
        # takes at most ``strict``, and the settings ``resolve`` is a plain function.
        if (
            isinstance(node.func, ast.Attribute)
            and callee == "resolve"
            and (node.args or any(k.arg != "strict" for k in node.keywords))
        ):
            found.append(f"resolve@{node.lineno}")
        if any(_segments(arg) for arg in node.args):
            found.append(f"secret path@{node.lineno}")
        if callee in OPENERS and _names_reference(node):
            found.append(f"open reference@{node.lineno}")
    return found


def _module_sources() -> list[Path]:
    return sorted(
        path
        for path in MODULES.rglob("*.py")
        if not {"__pycache__", ".venv"} & set(path.parts)
    )


def test_no_module_file_exposes_a_secret_value() -> None:
    scanned = _module_sources()
    assert len(scanned) > 100, len(scanned)
    assert Path(__file__) in scanned and SOURCE / "jobfeed" / "run.py" in scanned
    violations = {
        str(path.relative_to(MODULES)): hits
        for path in scanned
        for hits in (expose_uses(path.read_text(encoding="utf-8")),)
        if hits and path != Path(__file__)
    }
    assert violations == {}, violations


def test_no_leads_source_reads_token_bytes() -> None:
    scanned = _sources()
    assert SOURCE / "jobfeed" / "authorization.py" in scanned
    violations = {
        str(path.relative_to(SOURCE)): hits
        for path in scanned
        for hits in (token_reads(path.read_text(encoding="utf-8")),)
        if hits
    }
    assert violations == {}, violations


def test_the_token_scans_see_cores_real_reads() -> None:
    """Core's OAuth client does read token bytes; both scans report it."""
    core = CORE_OAUTH.read_text(encoding="utf-8")
    assert expose_uses(core)
    kinds = {hit.split("@")[0] for hit in token_reads(core)}
    assert {"resolve", "name SecretRef", "import SecretRef"} <= kinds, kinds


def test_planted_token_reads_are_caught_and_ordinary_code_is_not() -> None:
    planted = {
        "value.expose()\n": (expose_uses, ["expose@1"]),
        "reveal = value.expose\n": (expose_uses, ["expose@1"]),
        'getattr(value, "expose")()\n': (expose_uses, ["getattr expose@1"]),
        "store().resolve(reference, scope)\n": (token_reads, ["resolve@1"]),
        "from rheo_core.secrets import SecretRef\n": (
            token_reads,
            ["import rheo_core.secrets@1", "import SecretRef@1"],
        ),
        'root = Path(os.environ["RHEO_DATA_ROOT"])\n': (token_reads, ["data root@1"]),
        'path = root / "secrets" / name\n': (token_reads, ["secret path@1"]),
        'path = base.joinpath("connector-oauth/x/oauth")\n': (
            token_reads,
            ["secret path@1"],
        ),
        "data = Path(connection.token_ref).read_bytes()\n": (
            token_reads,
            ["open reference@1"],
        ),
        "with open(client_secret_ref) as handle:\n    pass\n": (
            token_reads,
            ["open reference@1"],
        ),
        "from rheo_core.storage.data_root import workspace_dir_for\n": (
            token_reads,
            ["import rheo_core.storage.data_root@1", "import workspace_dir_for@1"],
        ),
    }
    for source, (scan, expected) in planted.items():
        assert sorted(scan(source)) == sorted(expected), source
    ordinary = (
        "settings = resolve(workspace_id=ctx.workspace_id, connection=c)\n"
        "here = Path(__file__).resolve().parent\n"
        "real = here.resolve(strict=True)\n"
        "text = here.joinpath('presets.json').read_text()\n"
        "oauth.refresh(endpoint, refresh_ref=connection.token_ref)\n"
        "oauth.discard(reference)\n"
        'note = "the oauth/ directory holds the files"\n'
        "exposed = flags.exposed\n"
    )
    assert expose_uses(ordinary) == []
    assert token_reads(ordinary) == []

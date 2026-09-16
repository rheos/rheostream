"""Criterion 20, both clauses: what the façade may import, and what its tools accept.

The import scan below is the criterion's third sentence. The argument enumeration
at the foot of the file is its second, over the live tool registry rather than a
list of names typed out here, so a tool registered later is covered without an
edit.

**The enumeration deliberately does not call ``reserved_input_fields``.** That is
the function registration refuses with, and a test that reused it would be blind
in exactly the way registration would be: disable it and both the refusal and the
check that is supposed to catch what the refusal missed go quiet together. So the
enumeration reads the ratified :data:`RESERVED_INPUT_FIELDS` (importing the one
list is not restating it — ``manifest.py`` forbids a second copy, not a reader)
and intersects it with the argument names the tool's own **published JSON schema**
exposes. That is a different instrument for the same claim, and it is also the
more literal reading of the criterion: "accepts ... as an argument" is about what
a client may send, which is what the schema describes.

**The import scan.** The MCP facade imports the contracts, and, landed in 0b2
(C8) rather than "later", ``rheo_core.boundary``, ``rheo_core.operations`` and
``rheo_core.tokens`` -- and nothing from ``rheo_core.storage``,
``rheo_core.migrations``, or a database driver. This is a static AST scan of
every ``.py`` file under ``apps/mcp/`` — not a runtime import-graph walk,
which could trip on a transitive import pulled in by contracts. It asserts an
allowlist over ``rheo_core``'s three permitted subpackages (each one, and
anything under it, by dotted prefix), keeping the driver denylist as before.
``rheo_core.tokens`` is on the allowlist for :func:`_offending_imports`'s
purposes because nothing under it touches storage or a driver -- consistent
with the rule this scan exists to enforce, not a carve-out from it.
"""

import ast
from pathlib import Path
from typing import Final

from pydantic import BaseModel
from rheo_contracts import RESERVED_INPUT_FIELDS
from rheo_core.tokens.sets import TOOL_REGISTRY, register_core_tools

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MCP_ROOT = _REPO_ROOT / "apps" / "mcp"
_FORBIDDEN_DRIVER_ROOTS = frozenset({"psycopg", "sqlalchemy", "asyncpg"})
_ALLOWED_RHEO_CORE_PREFIXES: Final = (
    "rheo_core.boundary",
    "rheo_core.operations",
    "rheo_core.tokens",
)


def _allowed_rheo_core_import(name: str) -> bool:
    """True for ``rheo_core.boundary``/``.operations``/``.tokens`` and their
    submodules; false for everything else under ``rheo_core`` -- in
    particular ``rheo_core.storage`` and ``rheo_core.migrations``, and a bare
    ``rheo_core`` import naming no subpackage at all."""
    return any(
        name == prefix or name.startswith(f"{prefix}.")
        for prefix in _ALLOWED_RHEO_CORE_PREFIXES
    )


def _offending_imports(tree: ast.AST) -> list[str]:
    """Return the imported module names that cross the MCP boundary.

    A ``rheo_core`` import outside the allowlist above, or any known
    DB-driver import, offends. Relative imports (level > 0) stay inside
    apps/mcp and are ignored.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        module_names: list[str] = []
        if isinstance(node, ast.Import):
            module_names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module is None:
                continue
            module_names = [node.module]
        for name in module_names:
            root = name.split(".")[0]
            if root in _FORBIDDEN_DRIVER_ROOTS:
                offenders.append(name)
            elif root == "rheo_core" and not _allowed_rheo_core_import(name):
                offenders.append(name)
    return offenders


def test_mcp_imports_no_core_storage_or_db_driver() -> None:
    sources = sorted(_MCP_ROOT.rglob("*.py"))
    assert sources, f"no source files found under {_MCP_ROOT}"
    violations: dict[str, list[str]] = {}
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders = _offending_imports(tree)
        if offenders:
            violations[str(path.relative_to(_REPO_ROOT))] = offenders
    assert not violations, f"apps/mcp imports a forbidden module: {violations}"


def _accepted_argument_names(model: type[BaseModel]) -> set[str]:
    """Every argument name the tool's published schema lets a caller send.

    The schema is generated ``by_alias``, which is what the transport hands a
    client, so a field reachable only under an alias is counted under the name a
    caller would actually use. A model configured ``extra = "allow"`` declares no
    reserved field and accepts every one of them anyway, so it counts as accepting
    all twelve rather than none.
    """
    schema = model.model_json_schema()
    names = set(schema.get("properties", {}))
    if model.model_config.get("extra") == "allow":
        names |= set(RESERVED_INPUT_FIELDS)
    return names


def test_registered_tools_accept_no_sql_table_name_or_query_fragment() -> None:
    """Criterion 20's second clause, over the live registry.

    ``register_core_tools()`` first, and the emptiness guard after it, because an
    empty registry would satisfy "no tool accepts a reserved argument" by having
    no tools — the vacuous pass this assertion is most likely to decay into.
    """
    register_core_tools()
    declarations = TOOL_REGISTRY.declarations()
    assert declarations, "the tool registry is empty; the enumeration proves nothing"
    offenders: dict[str, list[str]] = {}
    for declaration in declarations:
        reserved = sorted(
            _accepted_argument_names(declaration.input_model) & RESERVED_INPUT_FIELDS
        )
        if reserved:
            offenders[declaration.name] = reserved
    assert not offenders, (
        f"registered tool(s) accept a reserved argument: {offenders}; workspace, "
        "actor and storage identity come from the context only"
    )

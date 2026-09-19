"""Static AST scans for the secret store's boundary (B9) and, from 0b2 (C7a), the
identity-provider boundary (B5).

Both copy the walk shape of ``tests/test_imports.py``: parse every Python source, walk
the tree, collect offenders, assert none. Static rather than a runtime import-graph
walk so a lazily imported name cannot hide.

1. ``SecretScope(`` is constructed only in files under
   ``packages/core/src/rheo_core/secrets/``. The scan root is the repository root,
   walking ``packages/``, ``apps/``, ``scripts/`` and ``tests/`` — tests included, so a
   test cannot forge a scope either.
2. No file under the top-level ``modules/`` distribution directory imports
   ``rheo_core.secrets``: domain modules are constructed without a scope and must have
   no way to reach the store.
3. The scope privates ``_SENTINEL`` and ``_new_scope`` are imported or named (as a
   name, an attribute, or a string constant, which is the ``getattr`` route) in no
   file outside ``rheo_core/secrets/``, walking ``modules/`` too. This file is the one
   exemption: it must spell the names it hunts for.

``SKIP_DIRS`` is the shipped ``scripts/check_web_platform.py`` set plus ``__pycache__``
and ``.venv``; C4's ``tests/test_boundary.py`` declares its own copy rather than
importing this one, because the two files sit in different chunks' file maps.
"""

import ast
import os
from collections.abc import Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = ("packages", "apps", "scripts", "tests", "modules")
_SECRETS_PACKAGE = _REPO_ROOT / "packages" / "core" / "src" / "rheo_core" / "secrets"
_MODULES_DIR = _REPO_ROOT / "modules"
_MODULE_SCAN_CONTROL = (
    _REPO_ROOT / "tests" / "fixtures" / "modules" / "probe_pkg" / "src" / "probe.py.txt"
)
_SECRETS_MODULE = "rheo_core.secrets"
_IDENTITY_MODULE = "rheo_core.identity"

# Generated build output and vendored dependencies, skipped while walking.
SKIP_DIRS = frozenset(
    {".next", "node_modules", "dist", "build", ".turbo", "__pycache__", ".venv"}
)


def _python_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        found.extend(
            Path(dirpath) / name for name in sorted(filenames) if name.endswith(".py")
        )
    return found


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _secret_scope_call_lines(tree: ast.AST) -> list[int]:
    """Line numbers of every call whose callee is named ``SecretScope``."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name):
            name = callee.id
        elif isinstance(callee, ast.Attribute):
            name = callee.attr
        else:
            continue
        if name == "SecretScope":
            lines.append(node.lineno)
    return lines


def _scan(root: Path, scan_dirs: Sequence[str]) -> tuple[int, dict[str, list[int]]]:
    scanned = 0
    violations: dict[str, list[int]] = {}
    for top in scan_dirs:
        for path in _python_files(root / top):
            scanned += 1
            if path.is_relative_to(_SECRETS_PACKAGE):
                continue
            lines = _secret_scope_call_lines(_parse(path))
            if lines:
                violations[str(path.relative_to(root))] = lines
    return scanned, violations


def test_secret_scope_is_constructed_only_inside_the_secrets_package() -> None:
    scanned, violations = _scan(_REPO_ROOT, _SCAN_DIRS)
    assert scanned, "no Python files scanned"
    assert not violations, (
        f"SecretScope( is constructed outside rheo_core/secrets/: {violations}"
    )
    # The scan is not vacuous: the one legitimate construction site exists.
    sites = [
        path.name
        for path in _python_files(_SECRETS_PACKAGE)
        if _secret_scope_call_lines(_parse(path))
    ]
    assert sites == ["scope.py"], sites


def test_secret_scope_scan_walks_the_modules_root(tmp_path: Path) -> None:
    assert "modules" in _SCAN_DIRS
    source = tmp_path / "modules" / "probe_pkg" / "src" / "probe.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        _MODULE_SCAN_CONTROL.read_text(encoding="utf-8"), encoding="utf-8"
    )

    _, violations = _scan(tmp_path, _SCAN_DIRS)
    assert violations == {"modules/probe_pkg/src/probe.py": [2]}

    without_modules = tuple(top for top in _SCAN_DIRS if top != "modules")
    _, missed = _scan(tmp_path, without_modules)
    assert missed == {}


def _secrets_imports(tree: ast.AST) -> list[str]:
    """Every import that reaches ``rheo_core.secrets`` or anything beneath it."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _SECRETS_MODULE or alias.name.startswith(
                    _SECRETS_MODULE + "."
                ):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) cannot leave the module distribution.
            if node.level != 0 or node.module is None:
                continue
            if node.module == _SECRETS_MODULE or node.module.startswith(
                _SECRETS_MODULE + "."
            ):
                offenders.append(node.module)
            elif node.module == "rheo_core" and any(
                alias.name == "secrets" for alias in node.names
            ):
                offenders.append(_SECRETS_MODULE)
    return offenders


def test_no_module_distribution_imports_the_secret_store() -> None:
    assert _MODULES_DIR.is_dir(), f"missing {_MODULES_DIR}"
    violations: dict[str, list[str]] = {}
    # An empty walk is not an error: the module distributions may hold no source yet.
    for path in _python_files(_MODULES_DIR):
        offenders = _secrets_imports(_parse(path))
        if offenders:
            violations[str(path.relative_to(_REPO_ROOT))] = offenders
    assert not violations, (
        f"a module distribution imports rheo_core.secrets: {violations}"
    )


def _identity_imports(tree: ast.AST) -> list[str]:
    """Every import that reaches ``rheo_core.identity`` or anything beneath it
    (``rheo_core.identity.providers`` included): B5's boundary — a domain module
    imports neither the providers package nor the boundary."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _IDENTITY_MODULE or alias.name.startswith(
                    _IDENTITY_MODULE + "."
                ):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) cannot leave the module distribution.
            if node.level != 0 or node.module is None:
                continue
            if node.module == _IDENTITY_MODULE or node.module.startswith(
                _IDENTITY_MODULE + "."
            ):
                offenders.append(node.module)
            elif node.module == "rheo_core" and any(
                alias.name == "identity" for alias in node.names
            ):
                offenders.append(_IDENTITY_MODULE)
    return offenders


def test_no_module_distribution_imports_the_identity_boundary() -> None:
    assert _MODULES_DIR.is_dir(), f"missing {_MODULES_DIR}"
    violations: dict[str, list[str]] = {}
    for path in _python_files(_MODULES_DIR):
        offenders = _identity_imports(_parse(path))
        if offenders:
            violations[str(path.relative_to(_REPO_ROOT))] = offenders
    assert not violations, (
        f"a module distribution imports rheo_core.identity: {violations}"
    )


# The sentinel and the private constructor: named nowhere outside the package.
_SCOPE_PRIVATES = frozenset({"_SENTINEL", "_new_scope"})


def _scope_private_mentions(tree: ast.AST) -> list[str]:
    """Every name, attribute, import alias or string constant naming a scope private."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _SCOPE_PRIVATES:
            found.append(f"name {node.id} at line {node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr in _SCOPE_PRIVATES:
            found.append(f"attribute .{node.attr} at line {node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _SCOPE_PRIVATES:
                    found.append(f"import {alias.name} at line {node.lineno}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _SCOPE_PRIVATES
        ):
            # getattr(module, "_SENTINEL") names it as a string.
            found.append(f"string {node.value!r} at line {node.lineno}")
    return found


def test_scope_privates_are_named_only_inside_the_secrets_package() -> None:
    this_file = Path(__file__).resolve()
    violations: dict[str, list[str]] = {}
    for top in _SCAN_DIRS:
        for path in _python_files(_REPO_ROOT / top):
            # This scan must spell the names it hunts for; no other file may.
            if path.is_relative_to(_SECRETS_PACKAGE) or path.resolve() == this_file:
                continue
            mentions = _scope_private_mentions(_parse(path))
            if mentions:
                violations[str(path.relative_to(_REPO_ROOT))] = mentions
    assert not violations, (
        f"scope privates named outside rheo_core/secrets/: {violations}"
    )
    # Not vacuous: the store reaches the private constructor from inside the package.
    inside = {
        path.name
        for path in _python_files(_SECRETS_PACKAGE)
        if _scope_private_mentions(_parse(path))
    }
    assert inside == {"scope.py", "store.py"}, inside

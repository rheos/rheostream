"""The owner's identities file is read when a document is read, never at import.

``identities.real``, ``real_re``, ``layout`` and ``forms`` read the identities file of
the data directory in force: the bound workspace layout's, else ``$FINANCE_DATA_DIR``.
A call that runs at import (a module-level pattern, a class attribute, a default
argument, a decorator) reads whichever file was in force when the module was first
imported, often none at all, since ``finance rebuild`` and the tick import the derive
before they bind the layout. Its value then stays for the life of the process and
for every workspace that process serves. This guard fails on any such call in the
package, the vendored files included; ``identities.per_identities`` is the way to
build a value from the file once per file.
"""

from __future__ import annotations

import ast
from pathlib import Path

from .source_tree import PACKAGE, Module, copy_package, dotted, modules, plant

IDENTITIES = "rheo_finance.identities"
READERS = frozenset({"real", "real_re", "layout", "forms"})


def _aliases(module: Module) -> tuple[dict[str, str], set[str]]:
    """Local names bound to a reader (``real as _real``) and local names bound to the
    identities module itself (``from rheo_finance import identities``)."""
    readers: dict[str, str] = {}
    modules_: set[str] = set()
    for node in ast.walk(module.tree):
        if isinstance(node, ast.ImportFrom) and node.module == IDENTITIES:
            for alias in node.names:
                if alias.name in READERS:
                    readers[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module == "rheo_finance":
            for alias in node.names:
                if alias.name == "identities":
                    modules_.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == IDENTITIES:
                    modules_.add(alias.asname or alias.name)
    if module.name == IDENTITIES:
        readers.update({name: name for name in READERS})
    return readers, modules_


def _at_import(node: ast.AST) -> list[ast.AST]:
    """The nodes under ``node`` that run when the module is imported: everything but
    the bodies of functions and lambdas (their decorators and defaults do run)."""
    found: list[ast.AST] = []
    stack = [node]
    while stack:
        each = stack.pop()
        found.append(each)
        if isinstance(each, ast.FunctionDef | ast.AsyncFunctionDef):
            stack += each.decorator_list
            stack += [d for d in each.args.defaults if d is not None]
            stack += [d for d in each.args.kw_defaults if d is not None]
            continue
        if isinstance(each, ast.Lambda):
            stack += [d for d in each.args.defaults if d is not None]
            continue
        stack += ast.iter_child_nodes(each)
    return found


def import_time_reads(root: Path) -> list[str]:
    problems: list[str] = []
    for module in modules(root):
        readers, names = _aliases(module)
        for statement in module.tree.body:
            for node in _at_import(statement):
                if not isinstance(node, ast.Call):
                    continue
                callee = dotted(node.func)
                if callee is None:
                    continue
                head, _, attr = callee.rpartition(".")
                if callee in readers or (head in names and attr in READERS):
                    problems.append(
                        f"{module.name}:{node.lineno} calls {callee} at import"
                    )
    return sorted(problems)


def test_no_module_reads_the_identities_file_at_import() -> None:
    assert import_time_reads(PACKAGE) == []


def test_an_import_time_read_is_found_and_a_call_time_read_is_not(
    tmp_path: Path,
) -> None:
    root = copy_package(tmp_path)
    plant(
        root,
        "parsers/planted.py",
        "import re\n"
        "from rheo_finance import identities\n"
        "from rheo_finance.identities import layout, real_re as _rr\n\n"
        "TITLE = re.compile(_rr(r'Zq Title'))\n\n"
        "class Shape:\n"
        "    COLUMNS = layout('planted.columns', ('a', 'b'))\n\n"
        "def read(text, mark=identities.real('Zq Mark')):\n"
        "    return re.compile(_rr(r'Zq Line')).search(text)\n",
    )
    assert import_time_reads(root) == [
        "rheo_finance.parsers.planted:10 calls identities.real at import",
        "rheo_finance.parsers.planted:5 calls _rr at import",
        "rheo_finance.parsers.planted:8 calls layout at import",
    ]

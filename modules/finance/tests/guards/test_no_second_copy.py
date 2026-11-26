"""AC-7 (part), FR-7: the Alder and Yew parsing rules are imported, not copied.

Three checks over ``src/rheo_finance``, its vendored copy of the four ``docextract``
modules aside (``rheo_finance.parsers.vendored``, byte-pinned upstream files, the one
copy):

- only the adapter imports the vendored modules (``test_docextract_boundary`` owns the
  scan; this test runs it again so AC-7 reads as one place);
- the parsers registered for the Alder statement and the Yew bill take their
  reading from the adapter's ``alder_statement`` and ``reference_bill``;
- no other module defines a docextract parsing name (a function, class or
  module-level pattern of ``alder``, ``reference``, ``pdftext`` or ``schema``), and
  no string in code carries a distinctive fragment of docextract's regular
  expressions. Docstrings are not code and are skipped. Each name and fragment is
  checked against the vendored source, so the lists cannot go stale unnoticed.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from rheo_finance.parsers.registry import parsers_for

from .source_tree import (
    PACKAGE,
    VENDORED,
    Module,
    copy_package,
    imports,
    modules,
    own_modules,
    plant,
)
from .test_docextract_boundary import ADAPTER, boundary_problems

# Names docextract defines that a copy of its rules would define again.
NAMES: dict[str, tuple[str, ...]] = {
    f"{VENDORED}.alder": (
        "AlderParseError", "error_report_deadline", "SUMMARY_LINE",
        "ACCOUNT_HEADER", "TXN_START", "AMOUNT_TAIL", "PRINTED_DATE", "KNOWN_FEES",
    ),
    f"{VENDORED}.reference": ("usage_lines",),
    f"{VENDORED}.pdftext": ("pdf_to_text", "NotTextPDF"),
    f"{VENDORED}.schema": ("to_money",),
}  # fmt: skip
# Fragments of the vendored modules' regular expressions (and one layout anchor):
# invented stand-in wording, checked against the vendored source below.
TOKENS: dict[str, tuple[str, ...]] = {
    f"{VENDORED}.alder": (
        r"ALDER CYCLE\s+(",
        r"ALDER START FIGURE\s+(",
        r"{MARK}(\d{{6,}})",
        r"(?:- ?(?:{re.escape(OD)})?)?",
        r"NON[- ]?SUFFICIENT",
        r"DISHONOU?RED",
    ),
    f"{VENDORED}.reference": (
        "Yew cut line",
        r"Yew settle by",
        r"Yew fresh sum\s+Yew with levies",
        r"Yew bytes sum\s+",
        r"Yew handset part",
        r"Yew fixups",
    ),
}


def _source(module: str) -> str:
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin, module
    return Path(spec.origin).read_text(encoding="utf-8")


def _defined(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            found |= {each.id for each in node.targets if isinstance(each, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
    return found


def _docstrings(tree: ast.AST) -> set[int]:
    """The ids of the docstring nodes of every module, class and function."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def _strings(module: Module) -> list[str]:
    skip = _docstrings(module.tree)
    return [
        node.value
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
    ]


def copy_problems(root: Path) -> list[str]:
    names = {name for group in NAMES.values() for name in group}
    tokens = [token for group in TOKENS.values() for token in group]
    problems: list[str] = []
    for module in own_modules(root):
        for name in sorted(_defined(module.tree) & names):
            problems.append(f"{module.name} defines {name}")
        strings = _strings(module)
        for token in tokens:
            if any(token in each for each in strings):
                problems.append(f"{module.name} carries {token!r}")
    return sorted(problems)


def test_the_lists_are_docextracts_own_names_and_fragments() -> None:
    for module, names in NAMES.items():
        defined = _defined(ast.parse(_source(module)))
        assert set(names) <= defined, (module, set(names) - defined)
    for module, tokens in TOKENS.items():
        source = _source(module)
        assert [token for token in tokens if token not in source] == [], module


def test_only_the_adapter_imports_docextract() -> None:
    assert boundary_problems(PACKAGE) == []


def test_the_alder_and_yew_parsers_read_through_the_adapter() -> None:
    expected = {
        ("alder", "pdf"): f"{ADAPTER}.alder_statement",
        ("yew", "pdf"): f"{ADAPTER}.reference_bill",
    }
    by_name = {module.name: module for module in modules(PACKAGE)}
    for (issuer, fmt), entry in expected.items():
        registered = parsers_for(issuer, fmt)
        assert registered, (issuer, fmt)
        for parser in registered:
            module = by_name[parser.parse.__module__]
            assert entry in {each.name for each in imports(module)}, parser.parser_id


def test_no_module_copies_the_parsing_rules() -> None:
    assert copy_problems(PACKAGE) == []


def test_a_copied_rule_is_found_and_a_docstring_is_not(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(
        root,
        "parsers/copied.py",
        '"""Reads the bill up to "Yew cut line"."""\n'
        "import re\n\n"
        "CHARGES = re.compile(\n"
        "    r'Yew fresh sum\\s+Yew with levies\\s+(\\S+)'\n"
        ")\n\n"
        "def usage_lines(text):\n"
        '    """Like docextract\'s, with "NON[- ]?SUFFICIENT" in the docstring."""\n'
        "    return []\n",
    )
    assert copy_problems(root) == [
        "rheo_finance.parsers.copied carries 'Yew fresh sum\\\\s+Yew with levies'",
        "rheo_finance.parsers.copied defines usage_lines",
    ]

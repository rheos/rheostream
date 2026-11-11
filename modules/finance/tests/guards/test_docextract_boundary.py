"""FR-7, FR-47 and Key decision 1: the vendored ``docextract`` modules have one door.

Only ``rheo_finance.parsers.docextract_adapter`` imports
``rheo_finance.parsers.vendored`` (an import, a ``from`` import, or a literal
``importlib.import_module`` / ``__import__`` name, at any depth), and it imports only
the names Key decision 1 lists. No module imports an installed ``docextract`` or its
``monitor`` package at all: the four files are vendored, and upstream's
``extractor`` (the model step), ``api`` and ``validate`` were never copied.
"""

from __future__ import annotations

import ast
from pathlib import Path

from .source_tree import (
    PACKAGE,
    SCRIPTS,
    Module,
    calls,
    copy_package,
    imports,
    modules,
    names_module,
    plant,
)

ADAPTER = "rheo_finance.parsers.docextract_adapter"
VENDORED = "rheo_finance.parsers.vendored"
ALLOWED = frozenset(
    f"{VENDORED}.{name}"
    for name in (
        "UPSTREAM_COMMIT", "UPSTREAM_VERSION",
        "alder", "alder.parse", "alder.AlderParseError",
        "reference", "reference.parse",
        "pdftext", "pdftext.pdf_to_text", "pdftext.NotTextPDF",
        "schema", "schema.to_money",
    )
)  # fmt: skip
#: Names nothing may import: the upstream packages themselves.
UPSTREAM = ("monitor", "docextract")


def _named(module: Module) -> list[str]:
    found = [each.name for each in imports(module)]
    for name, call in calls(module):
        if name in ("importlib.import_module", "__import__") and call.args:
            first = call.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append(first.value)
    return found


def boundary_problems(*roots: Path) -> list[str]:
    problems: list[str] = []
    for root in roots:
        for module in modules(root):
            for name in _named(module):
                if any(names_module(name, each) for each in UPSTREAM):
                    problems.append(f"{module.name} imports {name}")
                    continue
                if not names_module(name, VENDORED):
                    continue
                if module.name != ADAPTER:
                    problems.append(f"{module.name} imports {name}")
                elif name not in ALLOWED and name != VENDORED:
                    problems.append(
                        f"{module.name} imports {name}, not an allowed name"
                    )
    return sorted(problems)


def test_only_the_adapter_imports_docextract() -> None:
    assert boundary_problems(PACKAGE, SCRIPTS) == []
    adapter = next(each for each in modules(PACKAGE) if each.name == ADAPTER)
    assert {f"{VENDORED}.alder.parse", f"{VENDORED}.pdftext.pdf_to_text"} <= set(
        _named(adapter)
    ), "the control: the adapter's imports are read"


def test_a_second_importer_and_a_forbidden_name_are_found(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(
        root,
        "parsers/sneaky.py",
        f"def f():\n    from {VENDORED} import alder\n    import docextract\n",
    )
    plant(
        root,
        "jobs/dynamic.py",
        f"import importlib\nimportlib.import_module('{VENDORED}.schema')\n",
    )
    adapter = root / "parsers" / "docextract_adapter.py"
    adapter.write_text(
        adapter.read_text(encoding="utf-8")
        + f"\nfrom {VENDORED}.extractor import extract\n"
        + "import monitor.yew_monitor\n",
        encoding="utf-8",
    )
    assert boundary_problems(root) == sorted(
        [
            f"rheo_finance.jobs.dynamic imports {VENDORED}.schema",
            f"{ADAPTER} imports {VENDORED}.extractor, not an allowed name",
            f"{ADAPTER} imports {VENDORED}.extractor.extract, not an allowed name",
            f"{ADAPTER} imports monitor.yew_monitor",
            "rheo_finance.parsers.sneaky imports docextract",
            f"rheo_finance.parsers.sneaky imports {VENDORED}",
            f"rheo_finance.parsers.sneaky imports {VENDORED}.alder",
        ]
    )

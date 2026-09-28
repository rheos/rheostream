"""Acceptance criterion 11: this run's own proof of the FR 2 boundary between
Recallatron and core's evidence package (spec § Acceptance criteria 11, § A1, R12).

Three scans, each with its own positive control:

- **(a) Import source.** Every import of core's evidence code anywhere under
  Recallatron's source tree is ``from rheo_core.evidence import <name>``, and each
  name is in ``rheo_core.evidence.__all__``. A submodule
  (``rheo_core.evidence.service``), the evidence table module, a bare package handle
  and a star import are all refused.
- **(b) Public surface.** ``rheo_core.evidence.__all__`` is exactly seven names, and
  none of them is, returns, or carries in a field a raw database handle: a SQLAlchemy
  ``Connection``, ``Engine``, ``Table``, ``Select`` or ``Row``, or core's
  ``UnitOfWork``. The runtime half, on a batch a real claim returned, is in
  ``tests/postgres/test_automatic_memory_shapes.py`` because it needs a database.
- **(c) One hop to the runtime (R12).** No module in ``rheo_core.evidence`` imports
  ``rheo_core.runtime`` or ``rheo_core.storage.runtime_tables``, at module level or
  inside a function.

**Why not 1a1's scan.**
``test_recallatron_imports_no_runtime_transcript_or_hook_module``
(``modules/recallatron/tests/test_memory_contracts.py``, with its positive control
``test_the_runtime_import_scan_flags_a_real_import``) walks Recallatron's direct imports
and proves it names no runtime module. It says nothing about *which* evidence names
Recallatron reaches, or what they hand back: a module can import only sanctioned
packages and still leak a raw handle through a return value. And importing
``rheo_recallatron.manifest`` already loads the runtime transitively through other
core packages (§ A1), so no closure scan could pass. These three are the claims that
can be made, and each is made mechanically.
"""

import ast
import dataclasses
import importlib.util
import inspect
import subprocess
import sys
import types
import typing
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import rheo_core.evidence as evidence
from rheo_contracts import WorkspaceContext
from rheo_contracts.source_units import SourceAuthority, TrustedSourceUnit
from rheo_core.evidence import ClaimedBatch, ClaimedUnit
from rheo_core.storage.backend import UnitOfWork
from sqlalchemy import Connection, Engine, Row, Select, Table

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
_RECALLATRON_SRC: Final = _REPO_ROOT / "modules/recallatron/src"
_EVIDENCE_SRC: Final = _REPO_ROOT / "packages/core/src/rheo_core/evidence"

_EVIDENCE_PACKAGE: Final = "rheo_core.evidence"
_EVIDENCE_TABLES: Final = "rheo_core.storage.evidence_tables"

_PUBLIC_NAMES: Final = (
    "claim_units",
    "settle_unit",
    "ClaimedBatch",
    "ClaimedUnit",
    "EvidenceClaimInconsistent",
    "EvidenceAuditUnwritable",
    "EVIDENCE_RECORDED",
)
"""Prompt 10's seven, written out here rather than read back from ``__all__``, so the
pin compares two independently written lists."""

_NEVER_PUBLIC: Final = (
    "EvidenceWorkspaceUnresolved",
    "RuntimeEvidenceAuthority",
    "workspace_id_for_database",
    "evidence_unit",
)
"""Names that exist in core's evidence code or beside it and must never join the
public surface: the workspace resolution, the authority, and the table."""

_RAW_HANDLES: Final[tuple[type, ...]] = (
    Connection,
    Engine,
    Table,
    Select,
    Row,
    UnitOfWork,
)
"""What no public name may be, return, or carry. ``UnitOfWork`` covers its subclass
``HandlerUnitOfWork`` through ``issubclass``."""

_FIELD_TYPES: Final[tuple[Any, ...]] = (
    TrustedSourceUnit,
    WorkspaceContext,
    SourceAuthority,
    UUID,
    datetime | None,
    tuple[ClaimedUnit, ...],
)
"""The only field types the two public dataclasses may declare."""

_FORBIDDEN_RUNTIME: Final = ("rheo_core.runtime", "rheo_core.storage.runtime_tables")


def _is_or_under(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def _python_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*.py") if "__pycache__" not in path.parts
    )


# --- (a) import source ---------------------------------------------------------------


def _evidence_imports(
    tree: ast.AST, public: frozenset[str]
) -> tuple[list[str], list[str]]:
    """``(violations, sanctioned)`` for one module, each as ``<kind> <name>@<line>``.

    Relative imports are skipped: inside Recallatron's tree they resolve to
    ``rheo_recallatron``, never to core.
    """
    violations: list[str] = []
    sanctioned: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_or_under(alias.name, _EVIDENCE_PACKAGE) or _is_or_under(
                    alias.name, _EVIDENCE_TABLES
                ):
                    violations.append(f"import {alias.name}@{node.lineno}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module = node.module
            if module == _EVIDENCE_PACKAGE:
                for alias in node.names:
                    site = f"{module}.{alias.name}@{node.lineno}"
                    if alias.name in public:
                        sanctioned.append(f"name {site}")
                    else:
                        violations.append(f"name {site}")
            elif _is_or_under(module, _EVIDENCE_PACKAGE) or _is_or_under(
                module, _EVIDENCE_TABLES
            ):
                violations.append(f"submodule {module}@{node.lineno}")
            else:
                for alias in node.names:
                    full = f"{module}.{alias.name}"
                    if _is_or_under(full, _EVIDENCE_PACKAGE) or _is_or_under(
                        full, _EVIDENCE_TABLES
                    ):
                        violations.append(f"handle {full}@{node.lineno}")
    return sorted(violations), sorted(sanctioned)


def _scan_evidence_imports(
    root: Path, public: frozenset[str]
) -> tuple[int, dict[str, list[str]], list[str]]:
    scanned = 0
    violations: dict[str, list[str]] = {}
    sanctioned: list[str] = []
    for path in _python_files(root):
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found, allowed = _evidence_imports(tree, public)
        relative = str(path.relative_to(root))
        if found:
            violations[relative] = found
        sanctioned.extend(f"{relative}: {site}" for site in allowed)
    return scanned, violations, sorted(sanctioned)


def test_recallatron_reaches_core_evidence_only_through_its_public_names() -> None:
    public = frozenset(evidence.__all__)
    scanned, violations, sanctioned = _scan_evidence_imports(_RECALLATRON_SRC, public)

    assert scanned > 20, f"only {scanned} Recallatron files were scanned"
    assert not violations, f"Recallatron reaches past rheo_core.evidence: {violations}"
    # The drain and the manifest are the two real consumers. Were the scan to match
    # nothing, it would pass over a tree that no longer imports the package at all.
    assert any("automatic.py" in site for site in sanctioned), sanctioned
    assert any("manifest.py" in site for site in sanctioned), sanctioned


def test_the_evidence_import_scan_flags_every_other_route(tmp_path: Path) -> None:
    """The positive control: each refused shape, once, beside the one allowed."""
    (tmp_path / "probe.py").write_text(
        "from rheo_core.evidence import claim_units\n"
        "from rheo_core.evidence.service import settle_unit\n"
        "from rheo_core.evidence import RuntimeEvidenceAuthority\n"
        "import rheo_core.evidence\n"
        "from rheo_core import evidence\n"
        "from rheo_core.storage.evidence_tables import evidence_unit\n"
        "from rheo_core.storage import evidence_tables\n"
        "from rheo_core.evidence import *\n"
        "def later():\n"
        "    import rheo_core.evidence.authority\n",
        encoding="utf-8",
    )
    scanned, violations, sanctioned = _scan_evidence_imports(
        tmp_path, frozenset(evidence.__all__)
    )

    assert scanned == 1
    assert sanctioned == ["probe.py: name rheo_core.evidence.claim_units@1"]
    assert violations == {
        "probe.py": sorted(
            [
                "submodule rheo_core.evidence.service@2",
                "name rheo_core.evidence.RuntimeEvidenceAuthority@3",
                "import rheo_core.evidence@4",
                "handle rheo_core.evidence@5",
                "submodule rheo_core.storage.evidence_tables@6",
                "handle rheo_core.storage.evidence_tables@7",
                "name rheo_core.evidence.*@8",
                "import rheo_core.evidence.authority@10",
            ]
        )
    }


# --- (b) public surface --------------------------------------------------------------


def _types_in(annotation: Any) -> list[Any]:
    """``annotation``, its generic origin, and every type argument nested inside it,
    so ``Select[Any]`` counts as ``Select``."""
    found = [annotation, typing.get_origin(annotation)]
    for argument in typing.get_args(annotation):
        found.extend(_types_in(argument))
    return found


def _raw_handles_in(annotation: Any) -> list[str]:
    """The raw handle types ``annotation`` is or contains, by name."""
    return sorted(
        {
            handle.__name__
            for part in _types_in(annotation)
            if isinstance(part, type)
            for handle in _RAW_HANDLES
            if issubclass(part, handle)
        }
    )


def test_the_public_surface_is_exactly_the_seven_names() -> None:
    assert len(evidence.__all__) == len(set(evidence.__all__))
    assert sorted(evidence.__all__) == sorted(_PUBLIC_NAMES)
    for name in _NEVER_PUBLIC:
        assert name not in evidence.__all__, name


def test_no_public_name_is_returns_or_carries_a_raw_handle() -> None:
    """Every name in ``__all__`` is classified, and each class answers its own rule.

    A name that fits none of the four branches reds here, so a new kind of public
    name cannot join the surface without this test saying what it may be.
    """
    classified: dict[str, str] = {}
    for name in evidence.__all__:
        value = getattr(evidence, name)
        if inspect.isfunction(value):
            returned = typing.get_type_hints(value)["return"]
            assert returned in (ClaimedBatch, types.NoneType), (name, returned)
            assert not _raw_handles_in(returned), name
            classified[name] = "function"
        elif inspect.isclass(value) and dataclasses.is_dataclass(value):
            hints = typing.get_type_hints(value)
            assert set(hints) == {field.name for field in dataclasses.fields(value)}
            for field_name, annotation in hints.items():
                assert annotation in _FIELD_TYPES, (name, field_name, annotation)
                assert not _raw_handles_in(annotation), (name, field_name)
            classified[name] = "dataclass"
        elif inspect.isclass(value) and issubclass(value, Exception):
            classified[name] = "exception"
        elif isinstance(value, str):
            classified[name] = "constant"
        else:
            raise AssertionError(f"{name} is none of the four public kinds: {value!r}")
        if inspect.isclass(value):
            assert not _raw_handles_in(value), name
        assert not any(value is handle for handle in _RAW_HANDLES), name

    assert classified == {
        "claim_units": "function",
        "settle_unit": "function",
        "ClaimedBatch": "dataclass",
        "ClaimedUnit": "dataclass",
        "EvidenceClaimInconsistent": "exception",
        "EvidenceAuditUnwritable": "exception",
        "EVIDENCE_RECORDED": "constant",
    }


def test_the_surface_check_catches_a_leaked_handle() -> None:
    """The positive control, over the helpers the surface check relies on."""

    @dataclasses.dataclass(frozen=True)
    class Leaky:
        connection: Connection
        units: tuple[UnitOfWork, ...]

    hints = typing.get_type_hints(Leaky)
    assert hints["connection"] not in _FIELD_TYPES
    assert _raw_handles_in(hints["connection"]) == ["Connection"]
    assert _raw_handles_in(hints["units"]) == ["UnitOfWork"]
    assert _raw_handles_in(Engine | None) == ["Engine"]
    assert _raw_handles_in(Select[Any]) == ["Select"]
    # ``HandlerUnitOfWork`` is caught through its base class.
    assert _raw_handles_in(typing.get_type_hints(evidence.claim_units)["uow"]) == [
        "UnitOfWork"
    ]
    assert _raw_handles_in(ClaimedBatch) == []


# --- (c) one hop to the runtime ------------------------------------------------------


def _package_of(path: Path, root: Path, root_package: str) -> str:
    """The package a module file's relative imports resolve against: its own
    directory's dotted name, ``root`` being ``root_package``."""
    parts = path.parent.relative_to(root).parts
    return ".".join([root_package, *parts])


def _runtime_imports(tree: ast.AST, package: str) -> list[str]:
    """Imports of a runtime module, top-level or deferred, as ``<name>@<line>``.

    A relative import is resolved from the importing file's own package, through
    :func:`importlib.util.resolve_name`, so ``from ..runtime import x`` in
    ``rheo_core/evidence/`` or ``from ...runtime import x`` one subpackage deeper is
    not a way round the scan.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name("." * node.level + module, package)
            names = [module] + [f"{module}.{alias.name}" for alias in node.names]
        for name in names:
            if any(_is_or_under(name, forbidden) for forbidden in _FORBIDDEN_RUNTIME):
                found.append(f"{name}@{node.lineno}")
    return sorted(found)


def _scan_runtime_imports(
    root: Path, root_package: str = _EVIDENCE_PACKAGE
) -> tuple[int, dict[str, list[str]]]:
    scanned = 0
    violations: dict[str, list[str]] = {}
    for path in _python_files(root):
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = _runtime_imports(tree, _package_of(path, root, root_package))
        if found:
            violations[str(path.relative_to(root))] = found
    return scanned, violations


def test_core_evidence_imports_no_runtime_module_one_hop() -> None:
    """R12: no module in the package Recallatron imports names the runtime itself,
    so 1a4b must register its provider from the runtime side.

    One hop only, and deliberately: ``import rheo_core.evidence`` already loads
    ``rheo_core.storage.runtime_tables`` transitively through other core packages
    (§ A1), so no closure claim about that module could pass."""
    scanned, violations = _scan_runtime_imports(_EVIDENCE_SRC)

    files = {path.name for path in _python_files(_EVIDENCE_SRC)}
    # ``record.py`` is named because #197 edited it after this scan was planned.
    assert {"__init__.py", "record.py", "service.py", "providers.py"} <= files
    assert scanned == len(files)
    assert not violations, f"rheo_core.evidence imports the runtime: {violations}"


def test_importing_core_evidence_does_not_load_the_runtime_package() -> None:
    """What the closure does hold today, in a fresh interpreter: the runtime package
    itself stays unloaded (its table module does not; see the test above)."""
    probe = (
        "import sys\n"
        "import rheo_core.evidence\n"
        "loaded = sorted(m for m in sys.modules\n"
        "    if m == 'rheo_core.runtime' or m.startswith('rheo_core.runtime.'))\n"
        "sys.exit(f'loaded: {loaded}' if loaded else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_the_one_hop_scan_flags_a_runtime_import(tmp_path: Path) -> None:
    """The positive control: deferred imports, and relative imports at level 2 and,
    one subpackage deeper, at level 3, each resolved from its own file's package."""
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "probe.py").write_text(
        "from ...runtime import x\nfrom ..runtime import not_the_runtime\n",
        encoding="utf-8",
    )
    (tmp_path / "probe.py").write_text(
        "from rheo_core.runtime import RUNTIME_RUN\n"
        "import rheo_core.storage.runtime_tables\n"
        "from rheo_core.storage import runtime_tables\n"
        "from rheo_core.runtime_extra import unrelated\n"
        "def later():\n"
        "    from rheo_core.runtime.operations import build\n"
        "    from ..runtime import adapters\n",
        encoding="utf-8",
    )
    scanned, violations = _scan_runtime_imports(tmp_path)

    assert scanned == 2
    # In ``rheo_core.evidence.nested``, ``...runtime`` is ``rheo_core.runtime`` and
    # ``..runtime`` is ``rheo_core.evidence.runtime``, which is not the runtime.
    assert violations == {
        "nested/probe.py": ["rheo_core.runtime.x@1", "rheo_core.runtime@1"],
        "probe.py": sorted(
            [
                "rheo_core.runtime@1",
                "rheo_core.runtime.RUNTIME_RUN@1",
                "rheo_core.storage.runtime_tables@2",
                "rheo_core.storage.runtime_tables@3",
                "rheo_core.runtime.operations@6",
                "rheo_core.runtime.operations.build@6",
                "rheo_core.runtime@7",
                "rheo_core.runtime.adapters@7",
            ]
        ),
    }

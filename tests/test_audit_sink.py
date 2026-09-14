"""AC 11: the module-keyed sink registry is substrate, and nothing calls a sink.

Seams under test: ``install_sink``/``sink_for``/``reset_sinks``'s module keying — a
sink installed under one module id is never returned for another; installing the same
sink twice is a no-op while a *different* sink under a taken key is refused; a nameless
key and a writerless sink are both refused; and a module id with no registration
resolves to ``None`` rather than to a default object.

Issue #45 split run 0v's audit seam. This registry and the ``AuditSink`` protocol are
the half that stays; the dispatcher's call site, its subject-reference resolution and
its missing-registration fallback are the half 0c2 is scored on building, and they are
gone. So no test here drives ``dispatch()``, and
``test_the_audit_module_does_not_import_the_dispatcher`` pins the other direction: the
audit module names the dispatcher nowhere.

**What this file deliberately does NOT cover.** That *nothing in the tree* calls a sink
is C8's ``test_dispatch_calls_no_audit_sink``, which walks ``packages/``, ``apps/`` and
``modules/`` and asserts zero ``.record(`` callers where this file's predecessor
asserted exactly one. ``tests/`` is not one of those three roots and must not become
one: an AST scan matching ``Call(func=Attribute(attr="record"))`` matches by attribute
name and cannot see the receiver's type, and ``ClusterSession.record(...)`` — the test
cluster's own database bookkeeping — has six call sites under ``tests/``
(``tests/conftest.py``, ``tests/postgres/test_provisioning.py`` three times,
``tests/postgres/test_migrations.py``, ``tests/postgres/test_cli.py``), five of them in
files this run may not edit. Three roots is the shipped scope, not a narrowing.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest
from rheo_contracts import RecordRef, WorkspaceContext
from rheo_core.audit import (
    AuditSinkRefused,
    install_sink,
    reset_sinks,
    sink_for,
)
from rheo_core.operations import CORE_MODULE_ID
from rheo_core.storage.backend import UnitOfWork

_REPO_ROOT = Path(__file__).resolve().parents[1]

OTHER_MODULE_ID = "other"
"""A plain string. All this file needs is *a module id that is not* ``core``; no
module of this name is installed, and the registry never asks whether one exists."""


class RecordingSink:
    """Remembers what it was handed, and nothing else."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, RecordRef | None]] = []

    def record(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        operation: str,
        subject_ref: RecordRef | None,
    ) -> None:
        self.calls.append((operation, subject_ref))


@pytest.fixture(autouse=True)
def clean_sinks() -> Iterator[None]:
    """The sink table is process-wide, like the registries. Empty it either side."""
    reset_sinks()
    yield
    reset_sinks()


# --- the sink table -------------------------------------------------------------------


def test_sink_for_is_none_until_one_is_installed() -> None:
    """A missing registration answers ``None``, not a fabricated no-op.

    The mutant this kills is a ``_SINKS.get(module_id, SOME_DEFAULT)``: a default
    object would decide, here in the substrate, that a missing registration is a
    silent skip — which is one of the cases 0c2 is scored on deciding for itself.
    """
    assert sink_for(CORE_MODULE_ID) is None
    assert sink_for(OTHER_MODULE_ID) is None


def test_install_sink_is_idempotent_and_refuses_a_different_sink() -> None:
    sink = RecordingSink()
    install_sink(OTHER_MODULE_ID, sink)
    # Idempotent: ``apps/core``'s lifespan runs more than once in one process, so
    # ``run_startup()`` -> ``load_modules()`` installs the same sink twice.
    install_sink(OTHER_MODULE_ID, sink)
    assert sink_for(OTHER_MODULE_ID) is sink
    with pytest.raises(AuditSinkRefused) as excinfo:
        install_sink(OTHER_MODULE_ID, RecordingSink())
    assert excinfo.value.module_id == OTHER_MODULE_ID
    assert sink_for(OTHER_MODULE_ID) is sink


def test_a_sink_installed_for_one_module_is_not_returned_for_another() -> None:
    """F19's fix, at the table: the key is the owning module id, not one slot."""
    sink = RecordingSink()
    install_sink(OTHER_MODULE_ID, sink)
    assert sink_for(OTHER_MODULE_ID) is sink
    assert sink_for(CORE_MODULE_ID) is None
    assert sink_for("recallatron") is None


def test_install_sink_refuses_a_nameless_key_or_a_writerless_sink() -> None:
    with pytest.raises(ValueError):
        install_sink("", RecordingSink())
    with pytest.raises(TypeError):
        install_sink(OTHER_MODULE_ID, object())  # type: ignore[arg-type]


# --- the seam points one way ----------------------------------------------------------


def test_the_audit_module_does_not_import_the_dispatcher() -> None:
    """``packages/core/src/rheo_core/audit/sink.py`` never names ``dispatch``.

    The subject is that production module's own source, not this test file's imports:
    this file importing the dispatcher would prove nothing either way. After issue
    #45's split the dependency runs one way — a caller reaches for the registry, and
    the registry reaches for nothing — so an import of
    ``rheo_core.operations.dispatch`` there, or a deferred one inside a function,
    would put a call site back into the substrate 0c2 is scored on building.

    Both halves are checked: the import statements by name, and every ``Name``/
    ``Attribute`` node, which is what a deferred ``importlib`` route or a plain call
    would show up as.
    """
    target = _REPO_ROOT / "packages/core/src/rheo_core/audit/sink.py"
    tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))

    imported: list[str] = []
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.append(module)
            imported.extend(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)

    # Positive controls: the walk really parsed that module and saw both kinds of
    # node, so a scan that silently read an empty or wrong file cannot pass.
    assert "rheo_contracts" in imported, imported
    assert "_SINKS" in referenced, sorted(referenced)

    assert [name for name in imported if "dispatch" in name] == [], imported
    assert "dispatch" not in referenced, sorted(referenced)

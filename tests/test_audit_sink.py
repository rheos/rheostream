"""FR 4 / AC 11: the dispatcher writes the audit row, keyed by the operation's owning
module, and nothing else calls the sink.

Seams under test: ``install_sink``/``sink_for``/``reset_sinks``'s module keying;
``dispatch()``'s sink call site — that it fires only when the declaration carries an
``AuditSpec``, resolves the sink from ``operation.module_id``, hands over the
``subject_ref`` the spec names, and sits inside the existing ``try``/``with uow:`` so a
raising sink rolls the handler's own write back; and the static scan asserting
``dispatch.py`` is the only caller.

**The scan's roots are ``("packages", "apps", "modules")`` — ``tests`` is deliberately
absent.** An AST scan matching ``Call(func=Attribute(attr="record"))`` matches by
attribute name and cannot see the receiver's type, and ``ClusterSession.record(...)``
— the test cluster's own database bookkeeping — already has six call sites, every one
of them under ``tests/`` (``tests/conftest.py``, ``tests/postgres/test_provisioning.py``
three times, ``tests/postgres/test_migrations.py``, ``tests/postgres/test_cli.py``).
Five of those files are outside run 0v's file map and ``test_migrations.py`` may not be
edited at all, so a scan including ``tests/`` would fail on merged ``main`` code with no
legal fix. Restricting the roots is also what AC 11 actually claims: that no *module*
and no *application* calls the sink. ``dispatch.py`` is under ``packages/``, so the
scan still has exactly one expected hit and is not vacuous.

Two consequences, both intended. This file needs **no** self-exemption — it lives under
``tests/``, outside its own roots by construction, unlike
``tests/test_boundary_scans.py``'s resolved-path exemption. And ``NullAuditSink.record``
(and, from C2, ``SpikeAuditSink.record``) are *definitions*, not call nodes, so they are
not hits even though they sit inside the roots.
"""

import ast
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from conftest import ClusterSession
from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.audit import (
    NULL_SINK,
    AuditSinkRefused,
    NullAuditSink,
    install_sink,
    reset_sinks,
    sink_for,
)
from rheo_core.boundary import context_for_harness
from rheo_core.operations import (
    CORE_MODULE_ID,
    FAILED,
    HANDLER_FAILED,
    SETTINGS_SET,
    WORKSPACE_STATUS,
    OperationRegistry,
    dispatch,
    register_core_operations,
)
from rheo_core.operations.core_ops import SettingWritten
from rheo_core.refs import uuid7
from rheo_core.settings import CORE_ORIGIN
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.repositories import workspace_settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_ROOTS = ("packages", "apps", "modules")
_DISPATCH = "packages/core/src/rheo_core/operations/dispatch.py"
_SINK_MODULE = "packages/core/src/rheo_core/audit/sink.py"

# The shipped ``scripts/check_web_platform.py`` set plus ``__pycache__`` and ``.venv``,
# the same copy ``tests/test_boundary_scans.py`` keeps for the same reason: the two
# files sit in different chunks' file maps.
SKIP_DIRS = frozenset(
    {".next", "node_modules", "dist", "build", ".turbo", "__pycache__", ".venv"}
)

PROBE_KEY = "identity.token_max_days.cli"
PROBE_PAYLOAD = {"key": PROBE_KEY, "value": 30}
SPIKE_MODULE_ID = "spike"
"""A plain string here. ``modules/spike`` is C2's and does not exist at this
checkpoint; what this file needs is only *a module id that is not* ``core``."""


# --- AC 11's static half --------------------------------------------------------------


def _python_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        found.extend(
            Path(dirpath) / name for name in sorted(filenames) if name.endswith(".py")
        )
    return found


def _record_call_lines(tree: ast.AST) -> list[int]:
    """Lines of every call whose callee is an attribute named ``record``.

    Call nodes only: a ``def record`` is a definition and never a hit, which is why
    ``NullAuditSink`` and the spike's own sink do not trip their own scan.
    """
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "record"
    ]


def test_only_the_dispatcher_calls_the_sink() -> None:
    callers: dict[str, list[int]] = {}
    scanned = 0
    for top in _SCAN_ROOTS:
        for path in _python_files(_REPO_ROOT / top):
            scanned += 1
            lines = _record_call_lines(
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            )
            if lines:
                callers[str(path.relative_to(_REPO_ROOT))] = lines
    assert scanned > 0, "the scan walked no files"
    # Exactly one caller, and it is the dispatcher: not vacuous, and not widened.
    assert set(callers) == {_DISPATCH}, callers
    # The two ``def record`` bodies are inside the roots and are not hits.
    assert _SINK_MODULE not in callers


# --- the sink table -------------------------------------------------------------------


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


class ExplodingSink:
    """Writes through the real unit of work, then raises: AC 12(b)'s shape, driven
    here against a core operation because ``modules/spike`` is C2's."""

    def record(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        operation: str,
        subject_ref: RecordRef | None,
    ) -> None:
        raise RuntimeError("the sink failed after the handler succeeded")


@pytest.fixture(autouse=True)
def clean_sinks() -> Iterator[None]:
    """The sink table is process-wide, like the registries. Empty it either side."""
    reset_sinks()
    yield
    reset_sinks()


def test_sink_for_is_the_null_sink_until_one_is_installed() -> None:
    assert sink_for(CORE_MODULE_ID) is NULL_SINK
    assert isinstance(NULL_SINK, NullAuditSink)
    # The default really is a no-op, so a core dispatch behaves as it did before.
    assert NULL_SINK.record(None, None, operation="x", subject_ref=None) is None  # type: ignore[arg-type]


def test_install_sink_is_idempotent_and_refuses_a_different_sink() -> None:
    sink = RecordingSink()
    install_sink(SPIKE_MODULE_ID, sink)
    # Idempotent: ``apps/core``'s lifespan runs more than once in one process, so
    # ``run_startup()`` -> ``load_modules()`` installs the same sink twice.
    install_sink(SPIKE_MODULE_ID, sink)
    assert sink_for(SPIKE_MODULE_ID) is sink
    with pytest.raises(AuditSinkRefused) as excinfo:
        install_sink(SPIKE_MODULE_ID, RecordingSink())
    assert excinfo.value.module_id == SPIKE_MODULE_ID
    assert sink_for(SPIKE_MODULE_ID) is sink


def test_a_sink_installed_for_one_module_is_not_returned_for_another() -> None:
    """F19's fix, at the table: the key is the owning module id, not one slot."""
    sink = RecordingSink()
    install_sink(SPIKE_MODULE_ID, sink)
    assert sink_for(SPIKE_MODULE_ID) is sink
    assert sink_for(CORE_MODULE_ID) is NULL_SINK
    assert sink_for("recallatron") is NULL_SINK


def test_install_sink_refuses_a_nameless_key_or_a_writerless_sink() -> None:
    with pytest.raises(ValueError):
        install_sink("", RecordingSink())
    with pytest.raises(TypeError):
        install_sink(SPIKE_MODULE_ID, object())  # type: ignore[arg-type]


# --- through the real dispatch path ---------------------------------------------------


@pytest.fixture
def owner(workspace: UUID, owner_account_id: UUID) -> WorkspaceContext:
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


@pytest.fixture
def core_operations() -> None:
    register_core_operations()


def _rows(cluster: ClusterSession, workspace: UUID) -> dict[str, str]:
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        return workspace_settings(uow.connection)


@pytest.mark.postgres
def test_a_core_dispatch_never_reaches_another_modules_sink(
    cluster: ClusterSession,
    workspace: UUID,
    owner: WorkspaceContext,
    core_operations: None,
) -> None:
    """The defect F19 names, driven end to end.

    ``core.settings.set`` declares ``AuditSpec(subject_field=None)`` — as do all four
    core mutate operations — so a single process-global sink fired on
    ``declaration.audit is not None`` alone would fire here, in a workspace the
    ``spike`` module was never installed into. The mutant this kills is exactly that:
    one slot instead of ``sink_for(operation.module_id)``.
    """
    spike_sink = RecordingSink()
    install_sink(SPIKE_MODULE_ID, spike_sink)

    outcome = dispatch(owner, SETTINGS_SET, PROBE_PAYLOAD)

    assert outcome.ok, outcome
    assert spike_sink.calls == []
    assert _rows(cluster, workspace)[PROBE_KEY] == "30"


@pytest.mark.postgres
def test_the_dispatcher_calls_the_owning_modules_sink_once(
    cluster: ClusterSession,
    workspace: UUID,
    owner: WorkspaceContext,
    core_operations: None,
) -> None:
    """The positive direction of the same keying, and the whole of what the sink is
    handed: the operation's registered name, and the ``AuditSpec``'s subject (``None``
    here, as every release-one ``AuditSpec`` declares — finding F4)."""
    core_sink = RecordingSink()
    install_sink(CORE_MODULE_ID, core_sink)

    assert dispatch(owner, SETTINGS_SET, PROBE_PAYLOAD).ok
    assert core_sink.calls == [(SETTINGS_SET, None)]
    assert _rows(cluster, workspace)[PROBE_KEY] == "30"


@pytest.mark.postgres
def test_a_declaration_without_an_audit_spec_reaches_no_sink(
    workspace: UUID, owner: WorkspaceContext, core_operations: None
) -> None:
    """``core.workspace.status`` declares ``audit=None``. The mutant this kills is a
    call site that fires unconditionally."""
    core_sink = RecordingSink()
    install_sink(CORE_MODULE_ID, core_sink)

    assert dispatch(owner, WORKSPACE_STATUS, {}).ok
    assert core_sink.calls == []


@pytest.mark.postgres
def test_a_raising_sink_rolls_the_handlers_own_write_back(
    cluster: ClusterSession,
    workspace: UUID,
    owner: WorkspaceContext,
    core_operations: None,
) -> None:
    """The call site is **inside** the existing ``try``/``with uow:``.

    The mutant this kills is moving the sink call out of that block: the handler's
    write would already be committed, and the sink's exception would escape
    ``dispatch()`` rather than becoming a ``failed`` outcome.
    """
    install_sink(CORE_MODULE_ID, ExplodingSink())
    before = _rows(cluster, workspace)

    outcome = dispatch(owner, SETTINGS_SET, PROBE_PAYLOAD)

    assert outcome.state == FAILED, outcome
    assert outcome.error is not None
    assert outcome.error.error_code == HANDLER_FAILED
    # The handler succeeded and its write is gone anyway: one transaction, both halves.
    assert _rows(cluster, workspace) == before


class _SubjectInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str


@pytest.mark.postgres
def test_the_subject_ref_comes_from_the_field_the_audit_spec_names(
    workspace: UUID, owner: WorkspaceContext
) -> None:
    """The only exercise of ``AuditSpec.subject_field`` in release one.

    No shipped declaration names a field — every one of them is
    ``subject_field=None``, because ``AuditSpec`` cannot describe a create (F4). So the
    resolving branch is driven here, off a local registry, rather than shipping
    untested into 0c2's inheritance.
    """
    core_sink = RecordingSink()
    install_sink(CORE_MODULE_ID, core_sink)
    subject = RecordRef(module="harness", record_type="note", id=uuid7())

    def _touch(
        ctx: WorkspaceContext, uow: UnitOfWork, model_input: _SubjectInput
    ) -> SettingWritten:
        return SettingWritten(key="k", value="v", scope="workspace")

    registry = OperationRegistry()
    registry.register(
        OperationDeclaration(
            name="core.probe.touch",
            safety_class=SafetyClass.MUTATE,
            roles=frozenset({Role.OWNER}),
            input_model=_SubjectInput,
            output=SettingWritten,
            idempotency=Idempotency.NONE,
            audit=AuditSpec(subject_field="ref"),
        ),
        _touch,
        origin=CORE_ORIGIN,
    )

    outcome = dispatch(
        owner, "core.probe.touch", {"ref": subject.format()}, registry=registry
    )

    assert outcome.ok, outcome
    assert core_sink.calls == [("core.probe.touch", subject)]

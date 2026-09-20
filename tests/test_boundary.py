"""B15 and the factory refusals: no service call without a context.

- ``dispatch(None, ...)`` and ``dispatch(object(), ...)`` are refused
  ``context_required`` **before any lookup** (the name used is unregistered, so a
  lookup-first dispatcher would say ``operation_unknown`` instead).
- A static AST scan rooted at the repository root, walking ``packages/``, ``apps/``,
  ``scripts/`` and ``tests/`` (tests included: no test may hand-build a context),
  asserts a ``WorkspaceContext`` is constructed only in files under
  ``packages/core/src/rheo_core/boundary/``. It matches the call node, not text
  (``class WorkspaceContext(BaseModel):`` is the one textual occurrence and is not a
  construction). The name is unique in this repository, so a call to a bare
  ``WorkspaceContext`` is a construction **whatever module the file imported it
  from** — six in-tree modules re-export it, and provenance is not a defence. The
  forms matched: the direct call; pydantic's ``model_validate`` /
  ``model_construct`` / ``model_validate_json`` classmethods; either of those
  through a ``from <any module> import WorkspaceContext as X`` alias or any
  ``<module>.WorkspaceContext`` attribute chain; a class definition with
  ``WorkspaceContext`` among its bases (a subclass passes ``isinstance`` in
  ``dispatch``); an assignment that aliases the class (``C = WorkspaceContext``);
  and ``model_copy(update=...)`` on a receiver whose terminal name reads as a
  context (``ctx``, ``context``, ``owner_ctx``, ``self.request_ctx``,
  ``context_for_operator(...)``), narrowed to that receiver on purpose so an
  unrelated pydantic model's ``model_copy(update=)`` cannot fail this gate as a
  false positive and get the gate weakened. Plain attribute reads
  (``WorkspaceContext.model_fields``, ``.model_config``), annotations and
  ``isinstance`` checks are not constructions.

  **Limits, so nobody reads B15 as airtight:** a static scan cannot see
  ``type(ctx)(...)``, ``ctx.__class__(...)``, ``getattr(module, "WorkspaceContext")``,
  ``model_copy(**{"update": ...})``, a ``model_copy(update=)`` on a receiver not
  named like a context, or a class reached through a container; those routes are
  for review, exactly as C2's ``SecretScope`` scan documents its own ceiling.
- ``context_for_harness`` is refused ``profile_required`` under ``profile != test``,
  ``membership_missing`` without a row or with a row of another role; both
  factories refuse ``workspace_unavailable`` with the row's state as the detail for
  an ``unavailable`` row and for a ``schema_ahead`` one (B16's factory clause); and
  a real context is immutable.

Marked ``postgres``: the factories read the control plane. ``SKIP_DIRS`` is the
shipped ``scripts/check_web_platform.py`` set plus ``__pycache__`` and ``.venv``;
``tests/test_boundary_scans.py`` declares its own copy.
"""

import ast
import os
import re
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.registry import add_member
from pydantic import ValidationError
from rheo_contracts import (
    ALL_OPERATIONS,
    ActorKind,
    AudienceKind,
    Entry,
    Role,
    WorkspaceContext,
)
from rheo_core.boundary import (
    CONTEXT_REQUIRED,
    MEMBERSHIP_MISSING,
    PROFILE_REQUIRED,
    WORKSPACE_MISSING_DETAIL,
    WORKSPACE_UNAVAILABLE,
    Refusal,
    context_for_harness,
    context_for_operator,
)
from rheo_core.boundary.factories import context_from_operation
from rheo_core.migrations.orchestrator import migrate_workspace
from rheo_core.operations import OPERATION_UNKNOWN, dispatch
from rheo_core.refs import uuid7
from rheo_core.storage.control_plane import set_workspace_state
from rheo_core.storage.control_tables import WorkspaceState
from sqlalchemy import text

pytestmark = pytest.mark.postgres

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = ("packages", "apps", "scripts", "tests", "modules")
_BOUNDARY_PACKAGE = _REPO_ROOT / "packages" / "core" / "src" / "rheo_core" / "boundary"
_MODULE_SCAN_CONTROL = (
    _REPO_ROOT / "tests" / "fixtures" / "modules" / "probe_pkg" / "src" / "probe.py.txt"
)
_CLASS = "WorkspaceContext"
_CONSTRUCTORS = frozenset({"model_validate", "model_construct", "model_validate_json"})
# A ``model_copy(update=...)`` receiver that reads as a context: ``ctx``, ``context``,
# ``owner_ctx``, ``self.request_ctx``, ``context_for_operator(...)``.
_CONTEXT_RECEIVER = re.compile(r"(?i)(^|_)(ctx|context)(_|$)")

# Generated build output and vendored dependencies, skipped while walking.
SKIP_DIRS = frozenset(
    {".next", "node_modules", "dist", "build", ".turbo", "__pycache__", ".venv"}
)


# --- the scan -------------------------------------------------------------------------


def _python_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        found.extend(
            Path(dirpath) / name for name in sorted(filenames) if name.endswith(".py")
        )
    return found


def _bound_names(tree: ast.AST) -> set[str]:
    """Every local name bound to the class: the bare name — unique in this
    repository, whatever module a file imported it from — plus any ``as`` alias of
    it from **any** module (six in-tree modules re-export it)."""
    names: set[str] = {_CLASS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == _CLASS and alias.asname:
                    names.add(alias.asname)
    return names


def _is_class_reference(expr: ast.expr, bound: set[str]) -> bool:
    """``WorkspaceContext`` / ``WC`` (a bound name) or ``<anything>.WorkspaceContext``
    (an attribute chain through a module alias — matched on the attribute regardless
    of the base, which over-approximates on purpose: the only legitimate sites are in
    the boundary)."""
    if isinstance(expr, ast.Name):
        return expr.id in bound
    return isinstance(expr, ast.Attribute) and expr.attr == _CLASS


def _receiver_name(expr: ast.expr) -> str | None:
    """The terminal identifier of a ``model_copy`` receiver: ``ctx`` for ``ctx`` and
    ``self.ctx``, ``context_for_operator`` for ``context_for_operator(ws)``."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Call):
        return _receiver_name(expr.func)
    return None


def context_construction_sites(tree: ast.AST) -> list[str]:
    """Every node that yields a ``WorkspaceContext`` class or instance outside the
    factories, as ``form@line``: see the module docstring for the forms."""
    bound = _bound_names(tree)
    sites: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if any(_is_class_reference(base, bound) for base in node.bases):
                sites.append(f"subclass@{node.lineno}")
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            if node.value is not None and _is_class_reference(node.value, bound):
                sites.append(f"alias-assignment@{node.lineno}")
        elif isinstance(node, ast.Call):
            callee = node.func
            if _is_class_reference(callee, bound):
                sites.append(f"call@{node.lineno}")
            elif isinstance(callee, ast.Attribute):
                if callee.attr in _CONSTRUCTORS and _is_class_reference(
                    callee.value, bound
                ):
                    sites.append(f"{callee.attr}@{node.lineno}")
                elif callee.attr == "model_copy" and any(
                    keyword.arg == "update" for keyword in node.keywords
                ):
                    receiver = _receiver_name(callee.value)
                    if receiver is not None and _CONTEXT_RECEIVER.search(receiver):
                        sites.append(f"model_copy(update=)@{node.lineno}")
    return sites


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _scan(root: Path, scan_dirs: Sequence[str]) -> tuple[int, dict[str, list[str]]]:
    scanned = 0
    violations: dict[str, list[str]] = {}
    for top in scan_dirs:
        for path in _python_files(root / top):
            scanned += 1
            if path.is_relative_to(_BOUNDARY_PACKAGE):
                continue
            sites = context_construction_sites(_parse(path))
            if sites:
                violations[str(path.relative_to(root))] = sites
    return scanned, violations


def test_workspace_context_is_constructed_only_in_the_boundary_package() -> None:
    scanned, violations = _scan(_REPO_ROOT, _SCAN_DIRS)
    assert scanned, "no Python files scanned"
    assert not violations, (
        f"WorkspaceContext is constructed outside rheo_core/boundary/: {violations}"
    )
    # Not vacuous: the factories are the construction sites, and they exist.
    inside = [
        path.name
        for path in _python_files(_BOUNDARY_PACKAGE)
        if context_construction_sites(_parse(path))
    ]
    assert inside == ["factories.py"], inside


def test_workspace_context_scan_walks_the_modules_root(tmp_path: Path) -> None:
    assert "modules" in _SCAN_DIRS
    source = tmp_path / "modules" / "probe_pkg" / "src" / "probe.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        _MODULE_SCAN_CONTROL.read_text(encoding="utf-8"), encoding="utf-8"
    )

    _, violations = _scan(tmp_path, _SCAN_DIRS)
    assert violations == {
        "modules/probe_pkg/src/probe.py": ["call@1"],
    }

    without_modules = tuple(top for top in _SCAN_DIRS if top != "modules")
    _, missed = _scan(tmp_path, without_modules)
    assert missed == {}


_CONSTRUCTION_FORMS = {
    "direct": "from rheo_contracts import WorkspaceContext\nWorkspaceContext(a=1)\n",
    "model_validate": (
        "from rheo_contracts import WorkspaceContext\n"
        "WorkspaceContext.model_validate({})\n"
    ),
    "model_construct": (
        "from rheo_contracts.context import WorkspaceContext\n"
        "WorkspaceContext.model_construct()\n"
    ),
    "model_validate_json": (
        "from rheo_contracts import WorkspaceContext\n"
        "WorkspaceContext.model_validate_json('{}')\n"
    ),
    "aliased_direct": "from rheo_contracts import WorkspaceContext as WC\nWC(a=1)\n",
    "aliased_model_validate": (
        "from rheo_contracts import WorkspaceContext as WC\nWC.model_validate({})\n"
    ),
    "aliased_model_construct": (
        "from rheo_contracts.context import WorkspaceContext as Ctx\n"
        "Ctx.model_construct()\n"
    ),
    "module_attribute": "import rheo_contracts\nrheo_contracts.WorkspaceContext(a=1)\n",
    "module_alias_attribute": (
        "import rheo_contracts as rc\nrc.context.WorkspaceContext.model_construct()\n"
    ),
    "model_copy_update": "ctx.model_copy(update={'role': 'owner'})\n",
    "model_copy_update_suffix": "owner_ctx.model_copy(update={'role': 'owner'})\n",
    "model_copy_update_attribute": "self.request_ctx.model_copy(update={})\n",
    "model_copy_update_factory": "context_for_operator(ws).model_copy(update={})\n",
    "bare_name_no_import": "WorkspaceContext(a=1)\n",
    "reexport_direct": (
        "from rheo_core.boundary.factories import WorkspaceContext\n"
        "WorkspaceContext(a=1)\n"
    ),
    "reexport_model_construct": (
        "from rheo_core.operations.registry import WorkspaceContext\n"
        "WorkspaceContext.model_construct()\n"
    ),
    "reexport_alias": (
        "from rheo_core.storage.routing import WorkspaceContext as W\nW(a=1)\n"
    ),
    "subclass": "class Sub(WorkspaceContext):\n    pass\n",
    "subclass_attribute": (
        "import rheo_contracts\nclass Sub(rheo_contracts.WorkspaceContext):\n    pass\n"
    ),
    "subclass_alias": (
        "from rheo_contracts import WorkspaceContext as WC\nclass Sub(WC):\n    pass\n"
    ),
    "alias_assignment": "C = WorkspaceContext\n",
    "alias_annotated_assignment": "C: type = WorkspaceContext\n",
}

_NOT_CONSTRUCTIONS = {
    "attribute_reads": (
        "from rheo_contracts import WorkspaceContext\n"
        "WorkspaceContext.model_fields\nWorkspaceContext.model_config['frozen']\n"
    ),
    "class_definition": "class WorkspaceContext(BaseModel):\n    pass\n",
    "isinstance": "isinstance(ctx, WorkspaceContext)\n",
    "string": "text = 'WorkspaceContext('\n",
    "plain_copy": "ctx.model_copy()\n",
    "model_copy_other_receiver": "record.model_copy(update={'body': 'x'})\n",
    "annotation_only": (
        "def f(ctx: WorkspaceContext) -> WorkspaceContext:\n    return ctx\n"
    ),
    "annotated_none": "x: WorkspaceContext | None = None\n",
    "type_reference": "T = type[WorkspaceContext]\n",
}


@pytest.mark.parametrize("form", sorted(_CONSTRUCTION_FORMS))
def test_scan_matches_each_construction_form(form: str) -> None:
    tree = ast.parse(_CONSTRUCTION_FORMS[form])
    assert context_construction_sites(tree), form


@pytest.mark.parametrize("form", sorted(_NOT_CONSTRUCTIONS))
def test_scan_ignores_non_constructions(form: str) -> None:
    tree = ast.parse(_NOT_CONSTRUCTIONS[form])
    assert context_construction_sites(tree) == [], form


# --- context_required before any lookup ----------------------------------------------


@pytest.mark.parametrize("not_a_context", [None, object(), "ctx", {"workspace_id": 1}])
def test_dispatch_refuses_without_a_context_before_any_lookup(
    not_a_context: object,
) -> None:
    outcome = dispatch(not_a_context, "nothing.is.registered", {})
    assert outcome.state == CONTEXT_REQUIRED
    assert outcome.error is not None
    assert outcome.error.error_code == CONTEXT_REQUIRED
    assert outcome.result is None
    assert outcome.state != OPERATION_UNKNOWN


# --- the factories --------------------------------------------------------------------


def test_context_for_harness_is_refused_outside_the_test_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RHEO_PROFILE", "production")
    # No rows exist for these ids: the profile gate must fire before any read.
    refusal = context_for_harness(uuid7(), uuid7(), Role.OWNER)
    assert isinstance(refusal, Refusal)
    assert refusal.state == PROFILE_REQUIRED
    assert refusal.detail is not None and "production" in refusal.detail
    monkeypatch.setenv("RHEO_PROFILE", "development")
    refusal = context_for_harness(uuid7(), uuid7(), Role.OWNER)
    assert isinstance(refusal, Refusal) and refusal.state == PROFILE_REQUIRED


def test_context_for_harness_is_refused_membership_missing(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    # No membership row at all.
    stranger = uuid7()
    refusal = context_for_harness(workspace, stranger, Role.OWNER)
    assert isinstance(refusal, Refusal)
    assert refusal.state == MEMBERSHIP_MISSING
    # A row exists, but with another role than the one asked for.
    refusal = context_for_harness(workspace, owner_account_id, Role.MEMBER)
    assert isinstance(refusal, Refusal)
    assert refusal.state == MEMBERSHIP_MISSING
    member = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="member-two"
    )
    refusal = context_for_harness(workspace, member, Role.OWNER)
    assert isinstance(refusal, Refusal)
    assert refusal.state == MEMBERSHIP_MISSING
    # Exactly that role: a context.
    assert isinstance(
        context_for_harness(workspace, owner_account_id, "owner"), WorkspaceContext
    )
    assert isinstance(
        context_for_harness(workspace, member, Role.MEMBER), WorkspaceContext
    )


def test_harness_context_carries_the_ratified_account_values_and_is_immutable(
    workspace: UUID, owner_account_id: UUID
) -> None:
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext)
    assert ctx.workspace_id == workspace
    assert ctx.actor.kind is ActorKind.ACCOUNT and ctx.actor.id == owner_account_id
    assert ctx.role is Role.OWNER
    assert ctx.entry is Entry.CLI
    assert ctx.audience is not None
    assert (
        ctx.audience.kind is AudienceKind.SESSION
        and ctx.audience.id == owner_account_id
    )
    assert ctx.operation_set is ALL_OPERATIONS
    assert ctx.enabled_modules == frozenset()
    assert ctx.request_id.version == 7
    with pytest.raises(ValidationError):
        ctx.role = Role.OPERATOR  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ctx.workspace_id = uuid7()  # type: ignore[misc]
    with pytest.raises((ValidationError, AttributeError)):
        del ctx.role
    assert ctx.role is Role.OWNER and ctx.workspace_id == workspace
    # Two contexts are two requests.
    again = context_for_harness(workspace, owner_account_id, Role.OWNER, entry="web")
    assert isinstance(again, WorkspaceContext)
    assert again.entry is Entry.WEB and again.request_id != ctx.request_id


def test_operator_context_carries_the_ratified_operator_values(workspace: UUID) -> None:
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext)
    assert ctx.workspace_id == workspace
    assert ctx.actor.kind is ActorKind.OPERATOR and ctx.actor.id is None
    assert ctx.role is Role.OPERATOR
    assert ctx.entry is Entry.CLI
    assert ctx.audience is None
    assert ctx.operation_set is ALL_OPERATIONS
    assert ctx.enabled_modules == frozenset()
    assert ctx.request_id.version == 7
    with pytest.raises(ValidationError):
        ctx.audience = None  # type: ignore[misc]
    with pytest.raises(TypeError):
        context_for_operator(str(workspace))  # type: ignore[arg-type]


def test_both_factories_refuse_a_missing_workspace() -> None:
    missing = uuid7()
    for refusal in (
        context_for_operator(missing),
        context_for_harness(missing, uuid7(), Role.OWNER),
    ):
        assert isinstance(refusal, Refusal)
        assert refusal.state in {WORKSPACE_UNAVAILABLE, MEMBERSHIP_MISSING}
    operator = context_for_operator(missing)
    assert isinstance(operator, Refusal)
    assert operator == Refusal(WORKSPACE_UNAVAILABLE, WORKSPACE_MISSING_DETAIL)


def _assert_both_factories_refuse(
    workspace: UUID, owner_account_id: UUID, expected_detail: str
) -> None:
    for refusal in (
        context_for_operator(workspace),
        context_for_harness(workspace, owner_account_id, Role.OWNER),
    ):
        assert isinstance(refusal, Refusal), refusal
        assert refusal.state == WORKSPACE_UNAVAILABLE
        assert refusal.detail == expected_detail


def test_both_factories_refuse_an_unavailable_workspace(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    assert isinstance(context_for_operator(workspace), WorkspaceContext)
    with cluster.backend.control_engine.begin() as connection:
        set_workspace_state(
            connection,
            workspace,
            state=WorkspaceState.UNAVAILABLE,
            state_detail="marked unavailable by the test",
        )
    _assert_both_factories_refuse(workspace, owner_account_id, "unavailable")
    with cluster.backend.control_engine.begin() as connection:
        set_workspace_state(
            connection, workspace, state=WorkspaceState.ACTIVE, state_detail="activate"
        )
    assert isinstance(context_for_operator(workspace), WorkspaceContext)


def test_both_factories_refuse_a_schema_ahead_workspace(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE core.alembic_version_core SET version_num = :v"),
            {"v": "9999_from_the_future"},
        )
    result = migrate_workspace(cluster.backend, workspace)
    assert result.ok is False and result.detail == "schema_ahead"
    assert cluster.registry_row(workspace).state_detail == "schema_ahead"
    _assert_both_factories_refuse(workspace, owner_account_id, "unavailable")


def test_factories_refuse_a_workspace_still_provisioning(
    cluster: ClusterSession, make_workspace: MakeWorkspace, owner_account_id: UUID
) -> None:
    class Halt(Exception):
        pass

    def stop_after_first_step(step: str) -> None:
        raise Halt

    workspace_id = uuid7()
    with pytest.raises(Halt):
        make_workspace(workspace_id=workspace_id, after_step=stop_after_first_step)
    _assert_both_factories_refuse(workspace_id, owner_account_id, "provisioning")


def test_context_from_operation_rebuilds_an_account_context(
    workspace: UUID, owner_account_id: UUID
) -> None:
    ctx = context_from_operation(
        workspace,
        actor_kind=ActorKind.ACCOUNT.value,
        actor_id=owner_account_id,
        audience_kind=AudienceKind.SESSION.value,
        audience_id=owner_account_id,
        entry=Entry.CLI.value,
    )
    assert isinstance(ctx, WorkspaceContext)
    assert ctx.actor.kind is ActorKind.ACCOUNT
    assert ctx.actor.id == owner_account_id
    assert ctx.audience is not None
    assert ctx.audience.kind is AudienceKind.SESSION
    assert ctx.audience.id == owner_account_id
    assert ctx.entry is Entry.CLI
    assert ctx.operation_set is ALL_OPERATIONS


def test_context_from_operation_refuses_operator_runtime_actor_required(
    workspace: UUID,
) -> None:
    refusal = context_from_operation(
        workspace,
        actor_kind=ActorKind.OPERATOR.value,
        actor_id=None,
        audience_kind=AudienceKind.JOB.value,
        audience_id=None,
        entry=Entry.JOB.value,
    )
    assert isinstance(refusal, Refusal)
    assert refusal.state == "runtime_actor_required"

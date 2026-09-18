"""The ``rheo`` operator command through ``main(argv)``, and the ``core`` process's
startup sequence through the FastAPI lifespan.

- ``main([])`` returns ``0`` and prints usage; ``--help`` returns ``0``; a usage
  error returns ``2`` — never a ``SystemExit`` out of ``main``.
- ``account create`` writes one ``account`` row and its ``identity`` row and prints
  **exactly the new id and a newline** to stdout; a duplicate identity is refused
  ``identity_exists`` on stderr with no second account row.
- A fresh control plane must run ``account create`` before ``workspace create
  --owner``: the owner membership is a foreign key to ``account``, so the create
  with an unknown owner is refused ``account_missing`` and leaves no registry row.
- ``workspace create`` prints exactly the id; ``list``, ``status`` (JSON through the
  registry under an operator context), ``repair``, ``migrate`` and ``doctor``
  return ``0``; a refusal prints its state name on stderr and returns ``1``.
- ``doctor``'s connection-budget line reads ``warn`` on a stock cluster and says why:
  86 connections per process against a ``max_connections`` of 100 carries one process
  and not the two release one ends up with. That is the truthful reading of the
  shipped defaults, not a fixture to size around.
- ``doctor``'s reconcile-interval line names both keys and both resolved values, which
  is the half of AC 18 a packaged-defaults assertion cannot reach: either key can be
  overridden at deployment scope.
- The lifespan runs startup (control chain, active workspaces, the registry) and
  ``/healthz`` answers inside it with no database call of its own.
- ``run_startup()`` **refuses to complete** when a registered operation above the read
  class has no installed audit sink, naming it (AC 22's own subject, which is startup
  and not the checking function).
"""

import json
from collections.abc import Iterator
from uuid import UUID

import httpx
import pytest
from conftest import ClusterSession
from harness.registry import NOTE_WRITE, register_harness
from rheo_app_cli.main import main
from rheo_app_core.main import app, lifespan
from rheo_app_core.startup import run_startup
from rheo_core.approvals import (
    APPROVAL_APPROVE,
    APPROVAL_REFUSE,
    STANDING_GRANT_CREATE,
    STANDING_GRANT_REVOKE,
)
from rheo_core.audit import (
    CORE_AUDIT_SINK,
    install_sink,
    reset_sinks,
)
from rheo_core.operations import (
    CORE_MODULE_ID,
    HARNESS_MODULE_ID,
    OPERATION_GET,
    OPERATION_LIST,
    OPERATION_RESOLVE,
    REGISTRY,
    SETTINGS_SET,
    WORK_FAILURES,
    WORKSPACE_DIGEST,
    WORKSPACE_EXPORT,
    WORKSPACE_RESTORE,
    WORKSPACE_STATUS,
)
from rheo_core.operations.core_ops import TOKEN_ISSUE, TOKEN_REVOKE
from rheo_core.refs import uuid7
from rheo_core.storage import control_tables
from rheo_core.storage.control_plane import (
    get_account,
    get_identity,
    get_membership,
    list_workspaces,
)
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.provisioning import database_name_for
from sqlalchemy import event, func, select

pytestmark = pytest.mark.postgres

Run = tuple[int, str, str]


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> Run:
    capsys.readouterr()
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _count_accounts(cluster: ClusterSession) -> int:
    with cluster.backend.control_engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count()).select_from(control_tables.account)
            ).scalar_one()
        )


def _workspace_ids(cluster: ClusterSession) -> set[UUID]:
    with cluster.backend.control_engine.connect() as connection:
        return {row.id for row in list_workspaces(connection)}


@pytest.fixture
def created_workspaces(cluster: ClusterSession) -> Iterator[None]:
    """Record every workspace database the commands under test create, so the
    session teardown drops them (the CLI mints the ids, so this runs afterwards)."""
    before = _workspace_ids(cluster)
    yield
    for workspace_id in _workspace_ids(cluster) - before:
        cluster.record(database_name_for(workspace_id))


# --- the entry contract ---------------------------------------------------------------


def test_no_subcommand_prints_usage_and_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _run(capsys)
    assert code == 0
    assert out.startswith("usage: rheo")
    assert err == ""


def test_help_and_usage_errors_return_instead_of_exiting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = _run(capsys, "--help")
    assert code == 0 and "usage: rheo" in out
    code, out, err = _run(capsys, "no-such-command")
    assert code == 2 and out == "" and "usage: rheo" in err
    code, out, err = _run(capsys, "workspace")
    assert code == 2 and out == "" and "usage" in err
    code, out, err = _run(capsys, "workspace", "create")
    assert code == 2 and out == "" and "--owner" in err
    code, out, err = _run(capsys, "workspace", "status", "not-a-uuid")
    assert code == 2 and out == ""


# --- account create, then workspace create --owner ------------------------------------


def test_account_create_prints_exactly_the_id_and_writes_both_rows(
    cluster: ClusterSession, capsys: pytest.CaptureFixture[str]
) -> None:
    subject = f"subject-{uuid7().hex[:12]}"
    code, out, err = _run(
        capsys,
        "account", "create",
        "--provider", "github",
        "--subject", subject,
        "--display-name", "owner-one",
    )  # fmt: skip
    assert code == 0, err
    assert out.endswith("\n") and out.count("\n") == 1
    account_id = UUID(out.strip())
    assert out == f"{account_id}\n"
    assert "created" in err

    with cluster.backend.control_engine.connect() as connection:
        account = get_account(connection, account_id)
        identity = get_identity(
            connection, provider_id="github", provider_subject=subject
        )
    assert account is not None and account.display_name == "owner-one"
    assert identity is not None and identity.account_id == account_id
    assert identity.email is None and identity.email_verified is False

    # The same identity again: refused, and no second account row is left behind.
    count = _count_accounts(cluster)
    code, out, err = _run(
        capsys,
        "account", "create",
        "--provider", "github",
        "--subject", subject,
        "--display-name", "owner-again",
    )  # fmt: skip
    assert code == 1 and out == ""
    assert "identity_exists" in err
    assert _count_accounts(cluster) == count


def test_a_fresh_control_plane_needs_account_create_before_workspace_create(
    cluster: ClusterSession,
    capsys: pytest.CaptureFixture[str],
    created_workspaces: None,
) -> None:
    workspaces_before = _workspace_ids(cluster)
    code, out, err = _run(capsys, "workspace", "create", "--owner", str(uuid7()))
    assert code == 1 and out == ""
    assert "account_missing" in err
    assert _workspace_ids(cluster) == workspaces_before

    code, out, err = _run(
        capsys,
        "account", "create",
        "--provider", "github",
        "--subject", f"subject-{uuid7().hex[:12]}",
        "--display-name", "owner-one",
    )  # fmt: skip
    assert code == 0, err
    owner = UUID(out.strip())

    code, out, err = _run(
        capsys, "workspace", "create", "--owner", str(owner), "--slug", "demo-one"
    )
    assert code == 0, err
    assert out.endswith("\n") and out.count("\n") == 1
    workspace_id = UUID(out.strip())
    assert out == f"{workspace_id}\n"
    row = cluster.registry_row(workspace_id)
    assert row.state is WorkspaceState.ACTIVE
    assert row.slug == "demo-one"
    assert row.database_name == database_name_for(workspace_id)
    assert cluster.backend.database_exists(row.database_name)
    with cluster.backend.control_engine.connect() as connection:
        membership = get_membership(
            connection, account_id=owner, workspace_id=workspace_id
        )
    assert membership is not None and membership.role.value == "owner"

    # A second create for the same owner: a second id, a second database.
    code, out, err = _run(capsys, "workspace", "create", "--owner", str(owner))
    assert code == 0, err
    second = UUID(out.strip())
    assert second != workspace_id
    assert cluster.registry_row(second).state is WorkspaceState.ACTIVE

    # list, status, repair on what was just created.
    code, out, err = _run(capsys, "workspace", "list")
    assert code == 0
    lines = {line.split("\t")[0]: line for line in out.splitlines()}
    assert (
        str(workspace_id) in lines
        and "\tdemo-one\tactive\t" in lines[str(workspace_id)]
    )
    assert str(second) in lines

    code, out, err = _run(capsys, "workspace", "status", str(workspace_id))
    assert code == 0, err
    status = json.loads(out)
    assert status["core_contract_version"] == 1
    assert status["modules"] == []
    assert isinstance(status["core_version"], str) and status["core_version"]
    assert REGISTRY.lookup(WORKSPACE_STATUS) is not None
    assert REGISTRY.lookup(SETTINGS_SET) is not None

    code, out, err = _run(capsys, "workspace", "export", str(workspace_id))
    assert code == 0 and out.endswith("\n") and "queued" in err
    UUID(out.strip())

    code, out, err = _run(capsys, "workspace", "repair", str(workspace_id))
    assert code == 0 and out == ""
    assert cluster.registry_row(workspace_id).state is WorkspaceState.ACTIVE

    # A refusal prints its state name on stderr and returns 1.
    unknown = uuid7()
    code, out, err = _run(capsys, "workspace", "status", str(unknown))
    assert code == 1 and out == "" and "workspace_unavailable" in err
    code, out, err = _run(capsys, "workspace", "repair", str(unknown))
    assert code == 1 and out == "" and "workspace_missing" in err


def test_migrate_and_doctor_return_zero(
    cluster: ClusterSession, capsys: pytest.CaptureFixture[str], workspace: UUID
) -> None:
    code, out, err = _run(capsys, "migrate")
    assert code == 0, err
    assert out == ""
    assert cluster.control_database in err
    assert f"workspace {workspace}" in err
    assert cluster.registry_row(workspace).state is WorkspaceState.ACTIVE

    code, out, err = _run(capsys, "doctor")
    assert code == 0, (out, err)
    report = out.splitlines()
    assert any(line.startswith("ok   data root:") for line in report), report
    assert any(line.startswith("ok   settings: profile test") for line in report)
    assert any(line.startswith("ok   cluster: reachable") for line in report)
    assert any(
        line.startswith(f"ok   control plane {cluster.control_database}: at revision")
        for line in report
    )
    assert any(
        f"workspace {workspace}" in line and ": active" in line for line in report
    )
    for extension in ("vector", "pg_trgm"):
        (line,) = [line for line in report if f"extension {extension}:" in line]
        assert "role may CREATE EXTENSION in" in line
        assert "trusted:" in line

    # The connection budget, asserted on both halves. The level is the half a
    # hard-coded ``"ok"`` would pass silently through, and it is a checkable fact on
    # these exact numbers rather than a value chosen to make the test pass: the
    # shipped defaults put one process at 16 * 5 + 6 = 86, a stock cluster allows
    # 100, so one process fits and two (core and worker, from 0c1) do not.
    (budget,) = [line for line in report if "connection budget:" in line]
    assert "cluster max_connections = 100" in budget, (
        "this cluster is not stock; the level below is asserted against 100",
        budget,
    )
    assert budget.startswith("warn "), budget
    assert "16 * 5 + 6 = 86 per process" in budget, budget
    assert "2 processes configured = 172" in budget, budget
    assert "serialized maintenance" in budget, budget
    assert "held now =" in budget, budget
    # The detail names every lever an operator could move, not merely a number.
    for lever in (
        "max_connections",
        "storage.pool_cache_size",
        "storage.pool_max_connections",
    ):
        assert lever in budget, (lever, budget)

    # The reconcile interval, AC 18's resolved-settings half. Asserted on the line's
    # content, not merely that a line printed: a check naming neither key would leave
    # an operator with a level and nothing to act on.
    (reconcile,) = [line for line in report if "reconcile interval:" in line]
    assert reconcile.startswith("ok   "), reconcile
    assert "work.due_reconcile_seconds = 900" in reconcile, reconcile
    assert "storage.pool_idle_close_seconds = 300" in reconcile, reconcile

    assert not any(line.startswith("FAIL") for line in report)


# --- the core process startup sequence -----------------------------------------------


async def test_lifespan_runs_startup_and_healthz_stays_database_free(
    cluster: ClusterSession, workspace: UUID
) -> None:
    async with lifespan(app):
        report = app.state.startup
        assert report.profile == "test"
        assert report.control_database == cluster.control_database
        assert "RHEO_CLUSTER_DSN" in report.env_references
        # Sorted, and now fourteen: 0b1's three, 0b2's C8 adds core.token.issue and
        # core.token.revoke, 0c1's C3 adds core.work.failures, 0c2's C4 adds
        # core.operation.get/list/resolve, 0c2's C5 adds core.audit.list, 0c3's
        # C6 adds core.approval.approve and core.approval.refuse, and 0c3's C7 adds
        # core.standing_grant.create and core.standing_grant.revoke. Sorted by the
        # name string, so the two core.approval.* names lead ("approval" before
        # "audit"), the three core.operation.* names follow core.audit.list, the two
        # core.standing_grant.* names land between core.settings.set_member and
        # core.token.issue, and core.work.failures lands immediately before
        # core.workspace.status ("." sorts before "s").
        #
        # **Written out as literals on purpose.** Deriving this tuple from the
        # registry would make the assertion unfailable: an operation registered by
        # accident, or one silently dropped, would match a derived expectation
        # exactly. The literal list is the regression guard, and updating it by hand
        # when a run adds an operation is the point rather than the cost.
        #
        # The two approval operations appearing here is also criterion 18's other
        # half at work: they are ``core`` registrations and belong in a production
        # start, while the two upper-class *harness* fixtures 0c3 registers are
        # test-profile only and must never appear — which
        # ``tests/test_production_registration.py`` asserts from the other side.
        assert report.operations == (
            APPROVAL_APPROVE,
            APPROVAL_REFUSE,
            "core.audit.list",
            OPERATION_GET,
            OPERATION_LIST,
            OPERATION_RESOLVE,
            SETTINGS_SET,
            "core.settings.set_member",
            STANDING_GRANT_CREATE,
            STANDING_GRANT_REVOKE,
            TOKEN_ISSUE,
            TOKEN_REVOKE,
            WORK_FAILURES,
            WORKSPACE_DIGEST,
            WORKSPACE_EXPORT,
            WORKSPACE_RESTORE,
            WORKSPACE_STATUS,
        )
        by_id = {result.workspace_id: result for result in report.workspaces}
        assert by_id[workspace].ok is True
        # /healthz makes no database call: capture every statement on the control
        # engine and on this workspace's engine while it answers.
        database_name = cluster.registry_row(workspace).database_name
        engines = (
            cluster.backend.control_engine,
            cluster.backend.pools.engine_for(database_name),
        )
        captured: list[str] = []

        def capture(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            captured.append(statement)

        for engine in engines:
            event.listen(engine, "before_cursor_execute", capture)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t"
            ) as client:
                response = await client.get("/healthz")
        finally:
            for engine in engines:
                event.remove(engine, "before_cursor_execute", capture)
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "contract_version": 1}
        assert captured == []
    # Shutdown disposed the engines and left the backend bound: the session's
    # backend is still the process-wide one, and its engines recreate on use.
    assert get_backend() is cluster.backend
    assert get_backend().control_database == cluster.control_database
    assert cluster.registry_row(workspace).state is WorkspaceState.ACTIVE


def test_startup_refuses_to_complete_when_an_operation_has_no_audit_sink(
    cluster: ClusterSession, workspace: UUID
) -> None:
    """AC 22, whose subject is **startup** and not the function startup calls.

    ``check_audit_paths`` has its own unit test in
    ``tests/postgres/test_audit_dispatch.py``, driving it on a private registry. That
    pins the function and says nothing about whether anything calls it: deleting
    ``check_audit_paths(REGISTRY)`` from ``startup.py`` left the whole suite green, so
    the wiring layer's only production call site shipped unasserted. This drives
    ``run_startup()`` itself.

    ``harness`` is the module left unwired: ``run_startup`` calls
    ``register_core_operations()``, which installs the core's own sink one step before
    the check, so emptying the table cannot make *core* the offender no matter what
    else is registered. ``register_harness()`` puts three mutating operations on the
    process-wide registry whose module has nothing to write with, which is exactly the
    misassembled deployment the layer exists to refuse.
    """
    register_harness()
    reset_sinks()
    try:
        with pytest.raises(RuntimeError) as excinfo:
            run_startup()
    finally:
        install_sink(CORE_MODULE_ID, CORE_AUDIT_SINK)
        install_sink(HARNESS_MODULE_ID, CORE_AUDIT_SINK)

    message = str(excinfo.value)
    assert NOTE_WRITE in message, message
    assert HARNESS_MODULE_ID in message, message
    # And startup completes once the sinks are back, so the refusal is about the
    # wiring and not about anything else this sequence does.
    assert run_startup().profile == "test"
    assert cluster.registry_row(workspace).state is WorkspaceState.ACTIVE

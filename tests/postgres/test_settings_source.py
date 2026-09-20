"""Both ``OverrideSource`` implementations feeding C2's resolver.

Seams: ``rheo_core.settings.storage_source.PostgresOverrideSource`` against
``resolve()``: a workspace row overrides the deployment value for a
``scope = workspace`` key, a member row for a ``scope = member`` key (and only for
that account), a ``deployment``-scope key with a row is ignored, a floored key's row
is clamped by the deployment value on read, and the source routes exactly as the
storage layer does (a non-active workspace is ``workspace_unavailable``).

And 1a1's ``TransactionBoundOverrideSource``: it reads on the connection it was handed
— proved by the two things that separate a shared transaction from a second one, the
caller's own uncommitted row being visible and the engine pool recording no checkout —
and it refuses a workspace other than the one it was bound to.
"""

import logging
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.settings_keys import (
    HARNESS_EXPLICIT,
    HARNESS_FLOOR_MIN,
    HARNESS_MEMBER,
    HARNESS_RETENTION_DAYS,
    HARNESS_RETENTION_DEFAULT,
)
from rheo_core.refs import uuid7
from rheo_core.settings import ValueType, resolve
from rheo_core.settings.storage_source import (
    PostgresOverrideSource,
    TransactionBoundOverrideSource,
)
from rheo_core.storage.backend import (
    DATABASE_MISMATCH,
    WORKSPACE_UNAVAILABLE,
    StorageRefusal,
    UnitOfWork,
)
from rheo_core.storage.provisioning import STEP_CREATE_DATABASE
from rheo_core.storage.repositories import (
    upsert_member_setting,
    upsert_workspace_setting,
)
from sqlalchemy import Engine, event

pytestmark = pytest.mark.postgres


def write_workspace_row(
    cluster: ClusterSession, workspace_id: UUID, key: str, value: str, kind: ValueType
) -> None:
    row = cluster.registry_row(workspace_id)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        upsert_workspace_setting(
            uow.connection, key=key, value=value, value_type=kind, updated_by=None
        )
        uow.commit()


def write_member_row(
    cluster: ClusterSession,
    workspace_id: UUID,
    account_id: UUID,
    key: str,
    value: str,
    kind: ValueType,
) -> None:
    row = cluster.registry_row(workspace_id)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        upsert_member_setting(
            uow.connection, account_id=account_id, key=key, value=value, value_type=kind
        )
        uow.commit()


class CheckoutCounter:
    """Counts pool checkouts on one engine for the length of a ``with`` block.

    The property under test is "no second connection", and the only place that is
    observable is the pool: both sources hand back the same rows, and the one that
    opened its own connection has already returned it by the time the call ends, so a
    count taken afterwards sees nothing. The pool's own ``checkout`` event fires while
    it is happening.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.checkouts = 0

    def _count(self, *_: object) -> None:
        self.checkouts += 1

    def __enter__(self) -> "CheckoutCounter":
        event.listen(self._engine, "checkout", self._count)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self._engine, "checkout", self._count)


def test_transaction_bound_source_reads_the_callers_own_open_transaction(
    cluster: ClusterSession, workspace: UUID
) -> None:
    """The row this transaction wrote and has not committed, and no second checkout.

    Two assertions because either alone would pass on the wrong implementation: a
    source that opened its own connection would still return the right rows for
    everything already committed, and a source that reused the connection but read
    through a nested transaction would still see the uncommitted row. Together they
    say the read happened on this connection, inside this transaction.
    """
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        upsert_workspace_setting(
            uow.connection,
            key=HARNESS_RETENTION_DAYS,
            value="3",
            value_type=ValueType.INT,
            updated_by=None,
        )
        bound = TransactionBoundOverrideSource(uow.connection, workspace_id=workspace)
        with CheckoutCounter(engine) as counted:
            resolved = resolve(workspace_id=workspace, source=bound)
        assert resolved[HARNESS_RETENTION_DAYS] == 3
        assert counted.checkouts == 0

        # The other source, in the same block, as the control: it does check one out,
        # and its own connection cannot see a row this transaction has not committed.
        with CheckoutCounter(engine) as counted_other:
            elsewhere = resolve(workspace_id=workspace, source=PostgresOverrideSource())
        assert counted_other.checkouts >= 1
        assert elsewhere[HARNESS_RETENTION_DAYS] == HARNESS_RETENTION_DEFAULT
        # Nothing is committed: the row above exists only inside this transaction.
        uow.rollback()


def test_transaction_bound_source_accepts_a_unit_of_work_and_reads_member_rows(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        upsert_member_setting(
            uow.connection,
            account_id=owner_account_id,
            key=HARNESS_MEMBER,
            value="bound-row",
            value_type=ValueType.STR,
        )
        bound = TransactionBoundOverrideSource(uow, workspace_id=workspace)
        resolved = resolve(
            workspace_id=workspace, account_id=owner_account_id, source=bound
        )
        assert resolved[HARNESS_MEMBER] == "bound-row"
        assert dict(bound.member_overrides(workspace, uuid7())) == {}
        uow.rollback()


def test_transaction_bound_source_refuses_a_workspace_it_is_not_bound_to(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """It opens nothing, so it cannot route — and a source that answered anyway would
    be reading one workspace's database and calling the rows another's."""
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    other = uuid7()
    with UnitOfWork(engine, row.database_name) as uow:
        bound = TransactionBoundOverrideSource(uow.connection, workspace_id=workspace)
        with pytest.raises(StorageRefusal) as excinfo:
            bound.workspace_overrides(other)
        assert excinfo.value.state == DATABASE_MISMATCH
        with pytest.raises(StorageRefusal) as excinfo:
            bound.member_overrides(other, owner_account_id)
        assert excinfo.value.state == DATABASE_MISMATCH


def test_workspace_row_overrides_the_deployment_value_for_a_workspace_key(
    cluster: ClusterSession, workspace: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = PostgresOverrideSource()
    # Provisioning step 4 wrote the explicit row from the package default.
    assert dict(source.workspace_overrides(workspace)) == {
        HARNESS_EXPLICIT: "harness-package-default"
    }

    monkeypatch.setenv("RHEO__harness__explicit_per_workspace", "from-deployment")
    assert resolve()[HARNESS_EXPLICIT] == "from-deployment"
    # The explicit per-workspace row is a per-workspace fact, above the deployment.
    resolved = resolve(workspace_id=workspace, source=source)
    assert resolved[HARNESS_EXPLICIT] == "harness-package-default"

    write_workspace_row(cluster, workspace, HARNESS_EXPLICIT, "from-row", ValueType.STR)
    assert (
        resolve(workspace_id=workspace, source=source)[HARNESS_EXPLICIT] == "from-row"
    )
    # Without a workspace the row is never consulted.
    assert resolve(source=source)[HARNESS_EXPLICIT] == "from-deployment"


def test_floored_workspace_row_is_clamped_by_the_deployment_value_on_read(
    cluster: ClusterSession, workspace: UUID
) -> None:
    source = PostgresOverrideSource()
    assert resolve(workspace_id=workspace, source=source)[HARNESS_FLOOR_MIN] == 100
    write_workspace_row(cluster, workspace, HARNESS_FLOOR_MIN, "50", ValueType.INT)
    assert resolve(workspace_id=workspace, source=source)[HARNESS_FLOOR_MIN] == 50
    write_workspace_row(cluster, workspace, HARNESS_FLOOR_MIN, "500", ValueType.INT)
    assert resolve(workspace_id=workspace, source=source)[HARNESS_FLOOR_MIN] == 100


def test_member_row_applies_to_a_member_key_for_that_account_only(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    source = PostgresOverrideSource()
    other_account = uuid7()
    write_member_row(
        cluster,
        workspace,
        owner_account_id,
        HARNESS_MEMBER,
        "member-row",
        ValueType.STR,
    )
    assert dict(source.member_overrides(workspace, owner_account_id)) == {
        HARNESS_MEMBER: "member-row"
    }
    assert dict(source.member_overrides(workspace, other_account)) == {}

    by_owner = resolve(
        workspace_id=workspace, account_id=owner_account_id, source=source
    )
    assert by_owner[HARNESS_MEMBER] == "member-row"
    by_other = resolve(workspace_id=workspace, account_id=other_account, source=source)
    assert by_other[HARNESS_MEMBER] == "member-default"
    assert (
        resolve(workspace_id=workspace, source=source)[HARNESS_MEMBER]
        == "member-default"
    )

    # A member row for a workspace-scope key is ignored.
    write_member_row(
        cluster,
        workspace,
        owner_account_id,
        HARNESS_EXPLICIT,
        "member-row",
        ValueType.STR,
    )
    assert by_owner[HARNESS_EXPLICIT] == "harness-package-default"
    again = resolve(workspace_id=workspace, account_id=owner_account_id, source=source)
    assert again[HARNESS_EXPLICIT] == "harness-package-default"


def test_deployment_scope_key_with_a_row_is_ignored(
    cluster: ClusterSession, workspace: UUID, caplog: pytest.LogCaptureFixture
) -> None:
    source = PostgresOverrideSource()
    caplog.set_level(logging.WARNING, logger="rheo_core.settings")
    write_workspace_row(
        cluster, workspace, "storage.pool_cache_size", "1", ValueType.INT
    )
    resolved = resolve(workspace_id=workspace, source=source)
    assert resolved["storage.pool_cache_size"] == 16
    ignored = [
        record
        for record in caplog.records
        if record.getMessage() == "setting_override_ignored"
    ]
    assert [(r.setting_key, r.state) for r in ignored] == [  # type: ignore[attr-defined]
        ("storage.pool_cache_size", "setting_scope")
    ]


def test_source_routes_like_storage_and_refuses_a_non_active_workspace(
    cluster: ClusterSession, make_workspace: MakeWorkspace
) -> None:
    class Halt(Exception):
        pass

    def stop_after_create_database(step: str) -> None:
        if step == STEP_CREATE_DATABASE:
            raise Halt

    workspace_id = uuid7()
    with pytest.raises(Halt):
        make_workspace(workspace_id=workspace_id, after_step=stop_after_create_database)
    source = PostgresOverrideSource()
    with pytest.raises(StorageRefusal) as excinfo:
        source.workspace_overrides(workspace_id)
    assert excinfo.value.state == WORKSPACE_UNAVAILABLE
    assert excinfo.value.workspace_state == "provisioning"
    with pytest.raises(StorageRefusal) as excinfo:
        source.member_overrides(uuid7(), uuid7())
    assert excinfo.value.state == WORKSPACE_UNAVAILABLE
    assert excinfo.value.workspace_state is None

"""``PostgresOverrideSource`` feeding C2's resolver.

Seams: ``rheo_core.settings.storage_source.PostgresOverrideSource`` against
``resolve()``: a workspace row overrides the deployment value for a
``scope = workspace`` key, a member row for a ``scope = member`` key (and only for
that account), a ``deployment``-scope key with a row is ignored, a floored key's row
is clamped by the deployment value on read, and the source routes exactly as the
storage layer does (a non-active workspace is ``workspace_unavailable``).
"""

import logging
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.settings_keys import (
    HARNESS_EXPLICIT,
    HARNESS_FLOOR_MIN,
    HARNESS_MEMBER,
)
from rheo_core.refs import uuid7
from rheo_core.settings import ValueType, resolve
from rheo_core.settings.storage_source import PostgresOverrideSource
from rheo_core.storage.backend import WORKSPACE_UNAVAILABLE, StorageRefusal, UnitOfWork
from rheo_core.storage.provisioning import STEP_CREATE_DATABASE
from rheo_core.storage.repositories import (
    upsert_member_setting,
    upsert_workspace_setting,
)

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

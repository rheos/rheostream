"""Upgrade an existing Leads schema through the audited operator boundary."""

from typing import Any

import pytest
from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.migrations.orchestrator import recorded_revisions
from rheo_core.modules import loader
from rheo_core.operations import dispatch
from rheo_core.storage.core_tables import module_schema_version, module_state
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads import MANIFEST
from sqlalchemy import delete, select, text, update

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import (
    capture,
    ok,
    one,
    remove_jobsearch_preset,
    setup,
)

pytestmark = pytest.mark.postgres
TARGET = {
    "module_id": "leads",
    "target_version": MANIFEST.package_version,
    "target_schema": "0007_jobsearch_preset",
}


def predecessor(i: Intake) -> str:
    setup(i)
    capture(i, "upgrade", {"subject": "Preserved inquiry"})
    reference = one(i).ref
    with open_unit_of_work(i.ctx) as uow:
        remove_jobsearch_preset(uow.connection)
        uow.connection.execute(text("DROP TABLE leads.followup_reminder"))
        uow.connection.execute(
            text("ALTER TABLE leads.qualification DROP COLUMN evidence_digest")
        )
        uow.connection.execute(
            text(
                "UPDATE leads.alembic_version_leads "
                "SET version_num = '0004_opportunities'"
            )
        )
        uow.connection.execute(
            delete(module_schema_version).where(
                module_schema_version.c.module_id == "leads",
                module_schema_version.c.schema_version.in_(
                    (
                        "0005_followup_reminders",
                        "0006_assessment_evidence_digest",
                        "0007_jobsearch_preset",
                    )
                ),
            )
        )
        uow.commit()
    return reference


def upgrade(i: Intake, **changes: Any) -> Any:
    ctx = context_for_operator(i.workspace)
    assert isinstance(ctx, WorkspaceContext)
    return dispatch(
        ctx,
        "core.module.upgrade",
        {**TARGET, **changes},
        registry=i.surfaces.operations,
        consumers=i.consumers,
    )


def test_upgrade_unblocks_existing_records_and_retry_is_noop(intake: Intake) -> None:
    reference = predecessor(intake)
    refused = intake.call("leads.opportunity.get", ref=reference)
    assert refused.state == "module_unavailable"
    status = ok(intake, "core.workspace.status")
    assert (
        next(m.state for m in status.modules if m.module_id == "leads") == "unavailable"
    )
    assert (
        next(m.state for m in status.modules if m.module_id == "relationships")
        == "enabled"
    )
    assert intake.call("core.module.upgrade", **TARGET).state == "role_not_permitted"
    assert upgrade(intake, target_schema="wrong").state == "upgrade_target_changed"
    result = upgrade(intake)
    assert result.ok and result.result.changed, result
    restored = one(intake)
    assert restored.ref == reference and restored.data["title"] == "Preserved inquiry"
    result = upgrade(intake)
    assert result.ok and not result.result.changed, result
    with open_unit_of_work(intake.ctx) as uow:
        assert recorded_revisions(uow.connection, "leads") == {TARGET["target_schema"]}
        versions = uow.connection.execute(
            select(module_schema_version.c.schema_version).where(
                module_schema_version.c.module_id == "leads",
                module_schema_version.c.schema_version == TARGET["target_schema"],
            )
        ).all()
        assert len(versions) == 1


def test_failed_health_rolls_back_schema_and_can_retry(
    intake: Intake, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = predecessor(intake)

    def fail(_uow: object) -> None:
        raise RuntimeError("synthetic health check failure")

    monkeypatch.setitem(
        loader._LOADED, "leads", MANIFEST.model_copy(update={"health_checks": (fail,)})
    )
    assert not upgrade(intake).ok
    with open_unit_of_work(intake.ctx) as uow:
        assert recorded_revisions(uow.connection, "leads") == {"0004_opportunities"}
        assert (
            uow.connection.execute(
                text("SELECT to_regclass('leads.followup_reminder')")
            ).scalar_one()
            is None
        )
    assert (
        intake.call("leads.opportunity.get", ref=reference).state
        == "module_unavailable"
    )
    monkeypatch.setitem(loader._LOADED, "leads", MANIFEST)
    assert upgrade(intake).ok
    assert one(intake).ref == reference


@pytest.mark.parametrize(
    "marker, destructive", [(False, False), (None, False), (True, True)]
)
def test_unclassified_or_destructive_upgrade_never_runs(
    intake: Intake, monkeypatch: pytest.MonkeyPatch, marker: object, destructive: bool
) -> None:
    from alembic.script import ScriptDirectory
    from rheo_core.migrations.orchestrator import build_config
    from rheo_core.modules import upgrade as service

    predecessor(intake)
    scripts = ScriptDirectory.from_config(build_config("leads"))
    revision = scripts.get_revision("head")
    assert revision is not None
    monkeypatch.setattr(revision.module, "non_destructive_upgrade", marker)
    monkeypatch.setattr(revision.module, "destructive", destructive, raising=False)
    original = ScriptDirectory.from_config
    location = build_config("leads").get_main_option("script_location")
    monkeypatch.setattr(
        service.ScriptDirectory,
        "from_config",
        lambda config: (
            scripts
            if config.get_main_option("script_location") == location
            else original(config)
        ),
    )
    assert upgrade(intake).state == "upgrade_requires_review"
    with open_unit_of_work(intake.ctx) as uow:
        assert recorded_revisions(uow.connection, "leads") == {"0004_opportunities"}


def test_disabled_installation_is_upgraded_without_enabling(intake: Intake) -> None:
    predecessor(intake)
    with open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            update(module_state)
            .where(module_state.c.module_id == "leads")
            .values(state="disabled")
        )
        uow.commit()
    assert upgrade(intake).ok
    with open_unit_of_work(intake.ctx) as uow:
        assert (
            uow.connection.execute(
                select(module_state.c.state).where(module_state.c.module_id == "leads")
            ).scalar_one()
            == "disabled"
        )


def test_operator_cli_reports_mismatch_and_upgrade_result(
    intake: Intake, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse
    import json
    from types import SimpleNamespace

    from rheo_app_cli.commands import module as command

    predecessor(intake)
    # The fixture has already loaded/registered exactly these installed modules.
    monkeypatch.setattr(
        command, "bootstrap", lambda: SimpleNamespace(backend=intake.cluster.backend)
    )
    monkeypatch.setattr(command, "load_modules", lambda: None)
    args = argparse.Namespace(workspace=intake.workspace)
    assert command.check_modules(args) == 1
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert (
        next(r for r in rows if r["module_id"] == "leads")["state"]
        == "schema_version_mismatch"
    )
    args.module = "leads"
    args.target_version = TARGET["target_version"]
    args.target_schema = TARGET["target_schema"]
    assert command.upgrade_module(args) == 0
    assert json.loads(capsys.readouterr().out)["result"]["changed"]
    assert command.check_modules(args) == 0
    assert all(
        json.loads(line)["state"] == "ready"
        for line in capsys.readouterr().out.splitlines()
    )


def test_dependency_and_package_checks_precede_ddl(
    intake: Intake, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rheo_core.modules.manifest import Dependency

    predecessor(intake)
    monkeypatch.setitem(
        loader._LOADED,
        "leads",
        MANIFEST.model_copy(
            update={
                "dependencies": (
                    Dependency(module_id="relationships", version_range=">=99"),
                )
            }
        ),
    )
    assert upgrade(intake).state == "dependency_version"
    monkeypatch.setitem(
        loader._LOADED,
        "leads",
        MANIFEST.model_copy(update={"package_version": "0.0.1"}),
    )
    assert upgrade(intake, target_version="0.0.1").state == "downgrade_refused"
    monkeypatch.setitem(
        loader._LOADED,
        "leads",
        MANIFEST.model_copy(update={"package_version": "0.2.0"}),
    )
    assert upgrade(intake, target_version="0.2.0").ok
    with open_unit_of_work(intake.ctx) as uow:
        assert (
            uow.connection.execute(
                select(module_state.c.package_version).where(
                    module_state.c.module_id == "leads"
                )
            ).scalar_one()
            == "0.2.0"
        )


def test_active_execution_refuses_upgrade_without_blocking(intake: Intake) -> None:
    from rheo_core.modules.readiness import require_ready

    with open_unit_of_work(intake.ctx) as running:
        require_ready(running.connection, "leads")
        # Other normal calls still run, including a separate export transaction.
        assert intake.call("leads.opportunity.list").ok
        assert upgrade(intake).state == "upgrade_busy"
    assert upgrade(intake).ok


def test_upgrade_lock_refuses_execution_without_blocking(intake: Intake) -> None:
    from rheo_core.modules.readiness import lock_module_execution

    with open_unit_of_work(intake.ctx) as upgrading:
        lock_module_execution(upgrading.connection, upgrade=True)
        assert intake.call("leads.opportunity.list").state == "module_unavailable"
    assert intake.call("leads.opportunity.list").ok


def test_dependency_schema_mismatch_refuses_dependent_execution(intake: Intake) -> None:
    with open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            text(
                "UPDATE relationships.alembic_version_relationships "
                "SET version_num = 'unknown'"
            )
        )
        uow.commit()
    assert intake.call("leads.opportunity.list").state == "module_unavailable"
    assert upgrade(intake).state == "dependency_unavailable"


def test_missing_extension_refuses_before_ddl(
    intake: Intake, monkeypatch: pytest.MonkeyPatch
) -> None:
    predecessor(intake)
    monkeypatch.setitem(
        loader._LOADED,
        "leads",
        MANIFEST.model_copy(
            update={
                "storage": MANIFEST.storage.model_copy(
                    update={
                        "required_extensions": ("synthetic_missing_extension",),
                    }
                ),
            }
        ),
    )
    assert upgrade(intake).state == "extension_missing"
    with open_unit_of_work(intake.ctx) as uow:
        assert recorded_revisions(uow.connection, "leads") == {"0004_opportunities"}

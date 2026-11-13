"""The manifest (L1, L2, RS-6d L8): one job, one gated schedule, the settings keys,
the eight opt-in reads and nothing else core can reach, refusing export stubs, and the
flagship still loading nothing of finance. No database.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from rheo_core.settings import Scope, ValueType

import rheo_finance
from rheo_finance.wiring import manifest as wiring
from rheo_finance.wiring.manifest import MANIFEST
from rheo_finance.wiring.tick_job import TickPayload, finance_tick_job

REPO = Path(__file__).resolve().parents[4]
FLAGSHIP = REPO / "deploy" / "compose.flagship.yaml"


def test_the_package_attribute_is_the_manifest() -> None:
    assert rheo_finance.MANIFEST is MANIFEST
    with pytest.raises(AttributeError):
        _ = rheo_finance.NOT_A_NAME


def test_importing_the_package_imports_no_core() -> None:
    probe = (
        "import sys, rheo_finance, rheo_finance.cli.main\n"
        "print(sum(m.split('.')[0] in ('rheo_core', 'rheo_contracts')"
        " for m in sys.modules))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "0"


def test_one_job_and_one_gated_schedule() -> None:
    assert MANIFEST.module_id == "finance"
    (job,) = MANIFEST.jobs
    assert (job.name, job.input_model, job.handler) == (
        "finance.tick",
        TickPayload,
        finance_tick_job,
    )
    assert (job.max_attempts, job.cancellable) == (3, False)
    (schedule,) = MANIFEST.schedules
    assert schedule.model_dump() == {
        "name": "tick",
        "job_kind": "finance.tick",
        "cron": "*/15 * * * *",
        "enabled_by_default": True,
        "interval_seconds": 900,
        "gate_setting": "finance.jobs.enabled",
    }
    gate = {spec.key: spec for spec in MANIFEST.configuration_schema}[
        "finance.jobs.enabled"
    ]
    assert (gate.type, gate.scope, gate.default, gate.explicit_per_workspace) == (
        ValueType.BOOL,
        Scope.WORKSPACE,
        False,
        False,
    )


def test_only_the_eight_opt_in_reads_reach_finance_through_core() -> None:
    """L8: eight READ operations and their tools, opt-in only; nothing else core can
    reach (no record type, resolver, deletion, event, subscription, sensitivity map,
    web contribution, audit sink, connector, secret scope, authorization callback or
    agent guidance). The reads themselves are pinned in ``test_reads``."""
    assert len(MANIFEST.operations) == 8
    assert len(MANIFEST.tools) == 8
    assert MANIFEST.package_sets == "explicit_only"
    assert MANIFEST.record_types == ()
    assert MANIFEST.resolvers == ()
    assert MANIFEST.deletion_participants == ()
    assert MANIFEST.events == ()
    assert MANIFEST.subscriptions == ()
    assert dict(MANIFEST.sensitivity) == {}
    assert MANIFEST.audit_sink is None
    assert MANIFEST.web is None
    assert MANIFEST.connector_bindings == ()
    assert MANIFEST.secret_scopes == ()
    assert MANIFEST.authorization_completion is None
    assert MANIFEST.agent_guidance is None
    assert MANIFEST.storage.schema_name == "finance"
    assert MANIFEST.storage.migrations_path == "rheo_finance.migrations"
    assert MANIFEST.health_checks == (wiring.install_check,)


def test_the_flagship_does_not_load_finance() -> None:
    anchor = re.search(
        r"RHEO__modules__installed:\s*(\S+)", FLAGSHIP.read_text(encoding="utf-8")
    )
    assert anchor is not None
    installed = anchor.group(1).split(",")
    assert "recallatron" in installed  # the control: the anchor was read
    assert "finance" not in installed


def test_the_model_and_account_keys() -> None:
    specs = {spec.key: spec for spec in MANIFEST.configuration_schema}
    for stage in ("classify", "suggest", "extract"):
        spec = specs[f"finance.model.{stage}_model_id"]
        assert (spec.type, spec.scope, spec.default) == (
            ValueType.STR,
            Scope.DEPLOYMENT,
            "",
        )
    account = specs["finance.model.account_id"]
    assert (account.type, account.scope, account.default) == (
        ValueType.STR,
        Scope.WORKSPACE,
        "",
    )
    assert set(specs) == {
        "finance.jobs.enabled",
        "finance.model.classify_model_id",
        "finance.model.suggest_model_id",
        "finance.model.extract_model_id",
        "finance.model.account_id",
    }


def test_the_export_stubs_refuse() -> None:
    assert MANIFEST.export.schema_path == "reference/export.schema.json"
    schema = (
        Path(rheo_finance.__file__).parent / MANIFEST.export.schema_path
    ).read_text(encoding="utf-8")
    assert '"not": {}' in schema
    with pytest.raises(RuntimeError, match="finance state export"):
        wiring.refuse_export(object())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="finance state export"):
        wiring.refuse_import(object(), [])  # type: ignore[arg-type]

"""The export manifest records each module's own export format version (#129).

Restore refuses a module whose recorded ``export_format_version`` differs from the one
the restoring host's manifest declares. The manifest writer used to record ``1`` for
every module, so a module at version 2 would have had every one of its own fresh
exports refused. These tests run both halves, the writer and restore's check, against
a probe module at version 2, with no database: the writer's two repository reads are
replaced by rows built here.
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from harness.modules import manifest as probe_manifest
from rheo_core.exports import artifact as artifact_module
from rheo_core.exports.artifact import (
    APPROVALS_NAME,
    AUDIT_NAME,
    CONTRACT_VERSION_SUPPORTED,
    DELETIONS_NAME,
    MANIFEST_NAME,
    OPERATIONS_NAME,
    SETTINGS_NAME,
    ArtifactRefused,
)
from rheo_core.modules import loader as loader_module
from rheo_core.modules.manifest import ExportDeclaration, ModuleManifest
from rheo_core.storage import repositories
from rheo_core.storage.repositories import ModuleSchemaVersionRow, ModuleStateRow

_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _versioned(module_id: str, format_version: int) -> ModuleManifest:
    base = probe_manifest(module_id)
    return probe_manifest(
        module_id,
        export=ExportDeclaration(
            format_version=format_version,
            schema_path=base.export.schema_path,
            exporter=base.export.exporter,
            importer=base.export.importer,
        ),
    )


def _state(module_id: str) -> ModuleStateRow:
    return ModuleStateRow(
        module_id=module_id,
        package_version="0.0.0",
        state="enabled",
        installed_at=_NOW,
        enabled_at=_NOW,
        disabled_at=None,
        state_detail=None,
    )


def _host(
    monkeypatch: pytest.MonkeyPatch,
    loaded: Mapping[str, ModuleManifest],
    installed: tuple[str, ...],
) -> None:
    monkeypatch.setattr(loader_module, "loaded_manifests", lambda: dict(loaded))
    monkeypatch.setattr(
        repositories,
        "list_module_states",
        lambda conn: tuple(_state(module_id) for module_id in installed),
    )
    monkeypatch.setattr(
        repositories,
        "list_module_schema_versions",
        lambda conn: tuple(
            ModuleSchemaVersionRow(
                module_id=module_id,
                schema_version="0001",
                applied_at=_NOW,
                core_version_at_apply="0001",
            )
            for module_id in installed
        ),
    )


def _entries(modules: list[dict[str, object]]) -> dict[str, bytes]:
    manifest = {"contract_version": CONTRACT_VERSION_SUPPORTED, "modules": modules}
    return {
        MANIFEST_NAME: json.dumps(manifest).encode(),
        SETTINGS_NAME: b"",
        APPROVALS_NAME: b"",
        OPERATIONS_NAME: b"",
        AUDIT_NAME: b"",
        DELETIONS_NAME: b"",
    }


def test_the_manifest_records_the_loaded_modules_own_format_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host(monkeypatch, {"probe": _versioned("probe", 2)}, installed=("probe",))

    (entry,) = artifact_module._module_manifest(connection=None)  # type: ignore[arg-type]

    assert entry["module_id"] == "probe"
    assert entry["export_format_version"] == 2


def test_a_version_two_module_round_trips_through_restores_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer's output passes the same host's restore check. Before #129 the
    writer recorded ``1`` and this refused with "exported format version 1"."""
    _host(monkeypatch, {"probe": _versioned("probe", 2)}, installed=("probe",))
    modules = artifact_module._module_manifest(connection=None)  # type: ignore[arg-type]

    checked = artifact_module._manifest(_entries(modules))

    assert checked["modules"] == modules


def test_restore_still_refuses_a_real_format_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check itself is unchanged: an artifact written at version 2 does not
    restore into a host whose module implements version 3."""
    _host(monkeypatch, {"probe": _versioned("probe", 2)}, installed=("probe",))
    modules = artifact_module._module_manifest(connection=None)  # type: ignore[arg-type]
    _host(monkeypatch, {"probe": _versioned("probe", 3)}, installed=("probe",))

    with pytest.raises(ArtifactRefused, match="exported format version 2"):
        artifact_module._manifest(_entries(modules))

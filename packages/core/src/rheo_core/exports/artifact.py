"""Canonical workspace export bytes and their restore path."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Final
from uuid import UUID

import zstandard as zstd
from rheo_contracts import CONTRACT_VERSION
from sqlalchemy import Connection, delete, insert, select, update

from rheo_core.approvals import tables as approval_tables
from rheo_core.exports import tables as export_tables
from rheo_core.refs import uuid7
from rheo_core.settings import current_profile
from rheo_core.storage import core_tables, repositories, work_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import get_workspace, set_workspace_state
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.provisioning import core_version, provision

CONTRACT_VERSION_SUPPORTED: Final = CONTRACT_VERSION
MANIFEST_NAME: Final = "manifest.json"
SETTINGS_NAME: Final = "settings.jsonl"
APPROVALS_NAME: Final = "core/approvals.jsonl"
OPERATIONS_NAME: Final = "core/operations.jsonl"
AUDIT_NAME: Final = "core/audit.jsonl"
DELETIONS_NAME: Final = "core/deletions.jsonl"
REQUIRED_ENTRIES: Final = frozenset(
    {
        MANIFEST_NAME,
        SETTINGS_NAME,
        APPROVALS_NAME,
        OPERATIONS_NAME,
        AUDIT_NAME,
        DELETIONS_NAME,
    }
)
NON_TERMINAL_APPROVALS: Final = (
    approval_tables.PENDING,
    approval_tables.APPROVED,
    approval_tables.REQUIRES_REAPPROVAL,
)
NON_TERMINAL_OPERATIONS: Final = (
    "pending",
    "running",
    "approval_required",
    "unresolved",
)
RESTORED_OPERATION_STATE: Final = "approval_required"
MAX_COMPRESSED_BYTES: Final = 256 * 1024 * 1024
MAX_DECOMPRESSED_BYTES: Final = 1024 * 1024 * 1024
MAX_MEMBER_BYTES: Final = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES: Final = 1024 * 1024 * 1024


class ArtifactRefused(Exception):
    """A malformed or unsupported artifact, safe to show to an operator."""


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    workspace_id: UUID
    owner_account_id: UUID
    source_digest: bytes


class _BoundedReader(io.RawIOBase):
    """Count decompressed bytes while tarfile consumes the zstd stream."""

    def __init__(self, source: BinaryIO, limit: int) -> None:
        self._source = source
        self._limit = limit
        self._read = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        remaining = self._limit - self._read
        requested = remaining + 1 if size < 0 else min(size, remaining + 1)
        body = self._source.read(requested)
        self._read += len(body)
        if self._read > self._limit:
            raise ArtifactRefused(
                f"artifact expands beyond {self._limit} decompressed bytes"
            )
        return body


def _json_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _line(row: Mapping[str, object]) -> bytes:
    encoded = {key: _json_value(value) for key, value in row.items()}
    return (
        json.dumps(encoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode()


def _lines(rows: Iterable[Mapping[str, object]]) -> bytes:
    return b"".join(_line(row) for row in rows)


def _mapping_rows(connection: Connection, statement: Any) -> list[dict[str, object]]:
    return [dict(row) for row in connection.execute(statement).mappings()]


def _composition_bytes(connection: Connection) -> bytes:
    rows: list[dict[str, object]] = []
    composition = repositories.read_composition(connection)
    if composition is not None:
        rows.append(
            {
                "kind": "core",
                "core_version": composition.core_version,
                "core_contract_version": composition.core_contract_version,
            }
        )
    rows.extend(
        {
            "kind": "module_state",
            "module_id": row.module_id,
            "package_version": row.package_version,
            "state": row.state,
        }
        for row in repositories.list_module_states(connection)
    )
    rows.extend(
        {
            "kind": "module_schema_version",
            "module_id": row.module_id,
            "schema_version": row.schema_version,
        }
        for row in repositories.list_module_schema_versions(connection)
    )
    return _lines(rows)


def serialised_categories(
    connection: Connection, *, skip_operation_id: UUID | None = None
) -> dict[str, bytes]:
    """The bytes both export and ``core.workspace.digest`` compare."""
    workspace_settings = _mapping_rows(
        connection,
        select(core_tables.workspace_setting).order_by(
            core_tables.workspace_setting.c.key
        ),
    )
    for row in workspace_settings:
        row["scope"] = "workspace"
    member_settings = _mapping_rows(
        connection,
        select(core_tables.member_setting).order_by(
            core_tables.member_setting.c.account_id, core_tables.member_setting.c.key
        ),
    )
    for row in member_settings:
        row["scope"] = "member"

    approval_rows = _mapping_rows(
        connection,
        select(approval_tables.approval)
        .where(approval_tables.approval.c.state.in_(NON_TERMINAL_APPROVALS))
        .order_by(approval_tables.approval.c.id),
    )
    for row in approval_rows:
        payload = (
            connection.execute(
                select(approval_tables.approval_payload).where(
                    approval_tables.approval_payload.c.approval_id == row["id"]
                )
            )
            .mappings()
            .first()
        )
        row["state"] = approval_tables.REQUIRES_REAPPROVAL
        row["payload_body"] = None if payload is None else dict(payload["body"])
        row["payload_byte_length"] = None if payload is None else payload["byte_length"]

    operations = select(work_tables.operation).where(
        work_tables.operation.c.state.in_(NON_TERMINAL_OPERATIONS)
    )
    if skip_operation_id is not None:
        operations = operations.where(work_tables.operation.c.id != skip_operation_id)
    operation_rows = _mapping_rows(
        connection, operations.order_by(work_tables.operation.c.id)
    )
    for row in operation_rows:
        row["state"] = "unresolved"
    exported_operation_ids = {row["id"] for row in operation_rows}

    audit_rows = _mapping_rows(
        connection,
        select(work_tables.audit_record).order_by(work_tables.audit_record.c.id),
    )
    for row in audit_rows:
        if row["operation_id"] not in exported_operation_ids:
            row["operation_id"] = None
    return {
        "composition": _composition_bytes(connection),
        "settings": _lines((*workspace_settings, *member_settings)),
        "approvals": _lines(approval_rows),
        "operations": _lines(operation_rows),
        "audit": _lines(audit_rows),
        "deletions": b"",
    }


def digest_categories(
    connection: Connection, *, skip_operation_id: UUID | None = None
) -> dict[str, dict[str, object]]:
    return {
        name: {
            "count": 0 if not body else body.count(b"\n"),
            "digest": hashlib.sha256(body).hexdigest(),
        }
        for name, body in serialised_categories(
            connection, skip_operation_id=skip_operation_id
        ).items()
    }


def _module_manifest(connection: Connection) -> list[dict[str, object]]:
    latest = {
        row.module_id: row.schema_version
        for row in repositories.list_module_schema_versions(connection)
    }
    return [
        {
            "module_id": row.module_id,
            "package_version": row.package_version,
            "state": row.state,
            "schema_version": latest.get(row.module_id),
            "export_format_version": 1,
        }
        for row in repositories.list_module_states(connection)
        if row.state != "removed"
    ]


def write_archive(entries: Mapping[str, bytes], destination: Path) -> None:
    """Pack exact regular-file entries into one zstd-compressed tar."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as temporary:
        tar_path = Path(temporary.name)
    try:
        with tarfile.open(tar_path, "w") as archive:
            for name in sorted(entries):
                body = entries[name]
                info = tarfile.TarInfo(name)
                info.size = len(body)
                info.mtime = 0
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(body))
        compressor = zstd.ZstdCompressor(level=10)
        with tar_path.open("rb") as source, destination.open("wb") as target:
            compressor.copy_stream(source, target)
    finally:
        tar_path.unlink(missing_ok=True)


def read_archive(path: Path) -> dict[str, bytes]:
    """Read regular files only; paths and links cannot escape extraction."""
    try:
        compressed_size = path.stat().st_size
        if compressed_size > MAX_COMPRESSED_BYTES:
            raise ArtifactRefused(
                f"artifact exceeds {MAX_COMPRESSED_BYTES} compressed bytes"
            )
        entries: dict[str, bytes] = {}
        total_size = 0
        with path.open("rb") as compressed:
            with zstd.ZstdDecompressor().stream_reader(compressed) as decompressed:
                bounded = _BoundedReader(decompressed, MAX_DECOMPRESSED_BYTES)
                with tarfile.open(fileobj=bounded, mode="r|") as archive:
                    for member in archive:
                        pure = PurePosixPath(member.name)
                        if (
                            not member.isfile()
                            or pure.is_absolute()
                            or ".." in pure.parts
                            or member.name in entries
                        ):
                            raise ArtifactRefused(
                                f"artifact member {member.name!r} is not a unique "
                                "regular file"
                            )
                        if member.size > MAX_MEMBER_BYTES:
                            raise ArtifactRefused(
                                f"artifact member {member.name!r} exceeds "
                                f"{MAX_MEMBER_BYTES} bytes"
                            )
                        total_size += member.size
                        if total_size > MAX_ARCHIVE_BYTES:
                            raise ArtifactRefused(
                                f"artifact members exceed {MAX_ARCHIVE_BYTES} bytes"
                            )
                        extracted = archive.extractfile(member)
                        if extracted is None:
                            raise ArtifactRefused(
                                f"artifact member {member.name!r} has no body"
                            )
                        body = extracted.read(member.size + 1)
                        if len(body) != member.size:
                            raise ArtifactRefused(
                                f"artifact member {member.name!r} size is invalid"
                            )
                        entries[member.name] = body
    except ArtifactRefused:
        raise
    except (OSError, tarfile.TarError, zstd.ZstdError) as exc:
        raise ArtifactRefused(f"cannot read artifact {path}: {exc}") from None
    return entries


def create_artifact(
    connection: Connection,
    *,
    workspace_id: UUID,
    slug: str,
    owner_account_id: UUID,
    export_id: UUID,
    destination: Path,
    skip_operation_id: UUID | None,
    created_at: datetime,
) -> int:
    categories = serialised_categories(connection, skip_operation_id=skip_operation_id)
    manifest = {
        "core_version": core_version(),
        "contract_version": CONTRACT_VERSION,
        "workspace_id": str(workspace_id),
        "workspace_slug": slug,
        "owner_account_id": str(owner_account_id),
        "created_at": created_at.astimezone(UTC).isoformat(),
        "modules": _module_manifest(connection),
        "settings_digest": hashlib.sha256(categories["settings"]).hexdigest(),
        "record_counts": {
            name: 0 if not body else body.count(b"\n")
            for name, body in categories.items()
            if name != "composition"
        },
        "approval_count": categories["approvals"].count(b"\n"),
    }
    entries = {
        MANIFEST_NAME: json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode(),
        SETTINGS_NAME: categories["settings"],
        APPROVALS_NAME: categories["approvals"],
        OPERATIONS_NAME: categories["operations"],
        AUDIT_NAME: categories["audit"],
        DELETIONS_NAME: categories["deletions"],
    }
    write_archive(entries, destination)
    byte_length = destination.stat().st_size
    connection.execute(
        update(export_tables.export_record)
        .where(export_tables.export_record.c.id == export_id)
        .values(
            artifact_path=str(destination),
            state="complete",
            byte_length=byte_length,
        )
    )
    return byte_length


def _manifest(entries: Mapping[str, bytes]) -> dict[str, object]:
    missing = REQUIRED_ENTRIES - entries.keys()
    if missing:
        raise ArtifactRefused(f"artifact is missing {sorted(missing)}")
    try:
        value = json.loads(entries[MANIFEST_NAME])
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ArtifactRefused(f"manifest.json is invalid: {exc}") from None
    if not isinstance(value, dict):
        raise ArtifactRefused("manifest.json must be an object")
    if value.get("contract_version") != CONTRACT_VERSION_SUPPORTED:
        raise ArtifactRefused(
            f"unsupported contract version {value.get('contract_version')!r}"
        )
    modules = value.get("modules")
    if not isinstance(modules, list):
        raise ArtifactRefused("manifest modules must be a list")
    for index, module in enumerate(modules):
        if not isinstance(module, dict):
            raise ArtifactRefused(f"manifest module {index} must be an object")
        module_id = module.get("module_id")
        package_version = module.get("package_version")
        schema_version = module.get("schema_version")
        export_format_version = module.get("export_format_version")
        if not isinstance(module_id, str) or not module_id:
            raise ArtifactRefused(f"manifest module {index} has no module_id")
        if not isinstance(package_version, str) or not package_version:
            raise ArtifactRefused(
                f"manifest module {module_id!r} has no package_version"
            )
        if schema_version is not None and not isinstance(schema_version, str):
            raise ArtifactRefused(
                f"manifest module {module_id!r} has invalid schema_version"
            )
        if (
            not isinstance(export_format_version, int)
            or isinstance(export_format_version, bool)
            or export_format_version < 1
        ):
            raise ArtifactRefused(
                f"manifest module {module_id!r} has invalid export_format_version"
            )
    unsupported = [
        module
        for module in modules
        if not (
            isinstance(module, dict)
            and module.get("module_id") == "harness"
            and current_profile() == "test"
        )
    ]
    if unsupported:
        names = [
            str(module.get("module_id", "<unnamed>"))
            for module in unsupported
            if isinstance(module, dict)
        ]
        raise ArtifactRefused(f"host cannot load artifact modules: {', '.join(names)}")
    return value


def _identity(manifest: Mapping[str, object], source_digest: bytes) -> ArtifactIdentity:
    try:
        return ArtifactIdentity(
            workspace_id=UUID(str(manifest["workspace_id"])),
            owner_account_id=UUID(str(manifest["owner_account_id"])),
            source_digest=source_digest,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactRefused(f"manifest identity is invalid: {exc}") from None


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ArtifactRefused(f"cannot read artifact {path}: {exc}") from None
    return digest.digest()


def artifact_identity(path: Path) -> ArtifactIdentity:
    """Validated manifest identity plus a digest binding a later queued restore."""
    entries = read_archive(path)
    return _identity(_manifest(entries), _file_digest(path))


def _jsonl(body: bytes, name: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for number, line in enumerate(body.splitlines(), 1):
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ArtifactRefused(f"{name}:{number} is invalid JSON: {exc}") from None
        if not isinstance(value, dict):
            raise ArtifactRefused(f"{name}:{number} must be an object")
        rows.append(value)
    return rows


_UUID_COLUMNS: Final = frozenset(
    {
        "id",
        "actor_id",
        "audience_id",
        "operation_id",
        "approval_id",
        "payload_ref",
        "approved_by_id",
        "updated_by",
        "account_id",
    }
)
_DATETIME_COLUMNS: Final = frozenset(
    {
        "updated_at",
        "window_start",
        "window_end",
        "approved_at",
        "executed_at",
        "occurred_at",
        "created_at",
        "started_at",
        "terminal_at",
        "terminal_check_at",
    }
)
_BYTES_COLUMNS: Final = frozenset({"payload_digest", "request_digest"})


def _decoded(row: Mapping[str, object], columns: Iterable[str]) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for name in columns:
        value = row.get(name)
        if value is not None and name in _UUID_COLUMNS:
            value = UUID(str(value))
        elif value is not None and name in _DATETIME_COLUMNS:
            value = datetime.fromisoformat(str(value))
        elif value is not None and name in _BYTES_COLUMNS:
            value = bytes.fromhex(str(value))
        decoded[name] = value
    return decoded


def _import_settings(connection: Connection, body: bytes) -> None:
    connection.execute(delete(core_tables.member_setting))
    connection.execute(delete(core_tables.workspace_setting))
    for row in _jsonl(body, SETTINGS_NAME):
        scope = row.get("scope")
        table = (
            core_tables.workspace_setting
            if scope == "workspace"
            else core_tables.member_setting
            if scope == "member"
            else None
        )
        if table is None:
            raise ArtifactRefused(f"settings row has unsupported scope {scope!r}")
        connection.execute(insert(table).values(**_decoded(row, table.c.keys())))


def _install_modules(connection: Connection, manifest: Mapping[str, object]) -> None:
    modules = manifest["modules"]
    assert isinstance(modules, list)
    manifest_core_version = str(manifest["core_version"])
    for module in modules:
        assert isinstance(module, dict)
        now = datetime.now(UTC)
        connection.execute(
            insert(core_tables.module_state).values(
                module_id=str(module["module_id"]),
                package_version=str(module["package_version"]),
                state=str(module.get("state", "enabled")),
                installed_at=now,
                enabled_at=now,
                disabled_at=None,
                state_detail=None,
            )
        )
        schema_version = module["schema_version"]
        if schema_version is not None:
            connection.execute(
                insert(core_tables.module_schema_version).values(
                    module_id=str(module["module_id"]),
                    schema_version=str(schema_version),
                    applied_at=now,
                    core_version_at_apply=manifest_core_version,
                )
            )


def _import_operations(connection: Connection, body: bytes) -> set[UUID]:
    rows = _jsonl(body, OPERATIONS_NAME)
    approval_ids = {
        UUID(str(row["approval_id"]))
        for row in rows
        if row.get("approval_id") is not None
    }
    for row in rows:
        values = _decoded(row, work_tables.operation.c.keys())
        values["state"] = (
            RESTORED_OPERATION_STATE
            if values.get("approval_id") in approval_ids
            else "unresolved"
        )
        connection.execute(insert(work_tables.operation).values(**values))
    return approval_ids


def _import_approvals(connection: Connection, body: bytes) -> set[UUID]:
    restored: set[UUID] = set()
    for row in _jsonl(body, APPROVALS_NAME):
        values = _decoded(row, approval_tables.approval.c.keys())
        values.update(
            state=approval_tables.REQUIRES_REAPPROVAL,
            approved_by_kind=None,
            approved_by_id=None,
            approved_at=None,
            approved_entry=None,
            executed_at=None,
        )
        connection.execute(insert(approval_tables.approval).values(**values))
        approval_id = values["id"]
        assert isinstance(approval_id, UUID)
        restored.add(approval_id)
        body_value = row.get("payload_body")
        length_value = row.get("payload_byte_length")
        if body_value is not None and length_value is not None:
            connection.execute(
                insert(approval_tables.approval_payload).values(
                    approval_id=approval_id,
                    body=body_value,
                    byte_length=int(str(length_value)),
                )
            )
    return restored


def _import_audit(connection: Connection, body: bytes) -> None:
    for row in _jsonl(body, AUDIT_NAME):
        connection.execute(
            insert(work_tables.audit_record).values(
                **_decoded(row, work_tables.audit_record.c.keys())
            )
        )


def restore_artifact(
    path: Path,
    *,
    restore_id: UUID | None = None,
    expected_identity: ArtifactIdentity | None = None,
) -> UUID:
    """Provision the manifest workspace, import it, and activate the restore record."""
    entries = read_archive(path)
    manifest = _manifest(entries)
    identity = _identity(manifest, _file_digest(path))
    if expected_identity is not None and identity != expected_identity:
        raise ArtifactRefused(
            "artifact identity or digest changed after restore was authorized"
        )
    workspace_id = identity.workspace_id
    owner_account_id = identity.owner_account_id
    try:
        slug = str(manifest["workspace_slug"])
    except KeyError as exc:
        raise ArtifactRefused(f"manifest identity is invalid: {exc}") from None
    backend = get_backend()
    source_digest = identity.source_digest
    with backend.control_engine.connect() as control:
        row = get_workspace(control, workspace_id)
    record_id = uuid7() if restore_id is None else restore_id
    if row is not None:
        with backend.pools.acquire(row.database_name) as engine:
            with engine.connect() as connection:
                completed = connection.execute(
                    select(export_tables.export_record.c.id).where(
                        export_tables.export_record.c.id == record_id,
                        export_tables.export_record.c.kind == "restore",
                        export_tables.export_record.c.state == "complete",
                        export_tables.export_record.c.source_digest == source_digest,
                    )
                ).scalar_one_or_none()
        if completed is not None:
            if row.state is WorkspaceState.RESTORING:
                with backend.control_engine.begin() as control:
                    set_workspace_state(
                        control,
                        workspace_id,
                        state=WorkspaceState.ACTIVE,
                        state_detail="restore_complete",
                        expected_states=frozenset({WorkspaceState.RESTORING}),
                    )
            return workspace_id
        if row.state is not WorkspaceState.RESTORING:
            raise ArtifactRefused(f"workspace {workspace_id} already exists")
    else:
        provision(workspace_id, owner_account_id=owner_account_id, slug=slug)
        with backend.control_engine.begin() as control:
            set_workspace_state(
                control,
                workspace_id,
                state=WorkspaceState.RESTORING,
                state_detail="importing_artifact",
                expected_states=frozenset({WorkspaceState.ACTIVE}),
            )
        with backend.control_engine.connect() as control:
            row = get_workspace(control, workspace_id)
    if row is None:  # pragma: no cover - provision either raises or writes the row
        raise RuntimeError(f"provisioned workspace {workspace_id} has no registry row")
    engine = backend.pools.engine_for(row.database_name, pin=True)
    with UnitOfWork(engine, row.database_name, pool=backend.pools) as uow:
        _install_modules(uow.connection, manifest)
        _import_settings(uow.connection, entries[SETTINGS_NAME])
        approval_ids = _import_approvals(uow.connection, entries[APPROVALS_NAME])
        operation_approval_ids = _import_operations(
            uow.connection, entries[OPERATIONS_NAME]
        )
        if operation_approval_ids - approval_ids:
            raise ArtifactRefused(
                "an operation names an approval absent from the artifact"
            )
        _import_audit(uow.connection, entries[AUDIT_NAME])
        # Module installation/upgrade is deliberately a no-op until a host-loadable
        # module can appear in the manifest; validation above refuses one today.
        uow.connection.execute(
            insert(export_tables.export_record).values(
                id=record_id,
                kind="restore",
                artifact_path=None,
                source_digest=source_digest,
                created_at=datetime.now(UTC),
                created_by_id=owner_account_id,
                state="complete",
                removed_at=None,
                deletion_record_id=None,
                byte_length=None,
            )
        )
        uow.commit()
    with backend.control_engine.begin() as control:
        set_workspace_state(
            control,
            workspace_id,
            state=WorkspaceState.ACTIVE,
            state_detail="restore_complete",
            expected_states=frozenset({WorkspaceState.RESTORING}),
        )
    return workspace_id

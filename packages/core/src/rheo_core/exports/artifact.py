"""Canonical workspace export bytes and their restore path."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO, Final
from uuid import UUID

import zstandard as zstd

# ``jsonschema`` ships no inline types and this tree installs no stub package for it,
# so the import carries the same ignore ``runtimes/claude_cli.py`` already uses for
# the same library. Adding ``types-jsonschema`` would make *that* file's ignores
# unused, which is a change to a package outside this work's scope.
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    ValidationError,
)
from rheo_contracts import CONTRACT_VERSION
from sqlalchemy import Connection, delete, insert, select, text

from rheo_core.approvals import tables as approval_tables
from rheo_core.deletion.records import DeletionRecordRow, list_deletion_records
from rheo_core.deletion.records import (
    insert_imported_deletion_record as _write_imported_deletion,
)
from rheo_core.deletion.tables import (
    DELETION_CAUSES,
    RETENTION_EXPIRY,
    deletion_record,
)
from rheo_core.exports import tables as export_tables
from rheo_core.refs import uuid7
from rheo_core.settings import current_profile
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from rheo_core.storage import core_tables, repositories, work_tables
from rheo_core.storage.backend import (
    DATABASE_MISMATCH,
    ExportSnapshotUnitOfWork,
    StorageRefusal,
    UnitOfWork,
)
from rheo_core.storage.control_plane import get_workspace, set_workspace_state
from rheo_core.storage.control_tables import WorkspaceState
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.provisioning import core_version, provision
from rheo_core.storage.routing import active_workspace

if TYPE_CHECKING:  # pragma: no cover - see ``_exportable`` on why this is deferred
    from rheo_core.modules.manifest import ModuleManifest

CONTRACT_VERSION_SUPPORTED: Final = CONTRACT_VERSION
MANIFEST_NAME: Final = "manifest.json"
SETTINGS_NAME: Final = "settings.jsonl"
APPROVALS_NAME: Final = "core/approvals.jsonl"
OPERATIONS_NAME: Final = "core/operations.jsonl"
AUDIT_NAME: Final = "core/audit.jsonl"
DELETIONS_NAME: Final = "core/deletions.jsonl"
MODULE_ENTRY_PREFIX: Final = "modules/"
MODULE_ENTRY_SUFFIX: Final = ".jsonl"


def module_entry_name(module_id: str) -> str:
    """Where one module's exported rows live inside the archive.

    ``modules/<module_id>.jsonl``, beside ``core/``'s own five. One file per module
    rather than one per table: the table list is the module's private business and
    the artifact should not have to change shape when a module adds one. Each line
    carries a ``record`` discriminator instead, which is what the module's own JSON
    Schema branches on.
    """
    return f"{MODULE_ENTRY_PREFIX}{module_id}{MODULE_ENTRY_SUFFIX}"


def module_id_for_entry(name: str) -> str | None:
    """The module id an archive member names, or ``None`` when it names none."""
    if not name.startswith(MODULE_ENTRY_PREFIX) or not name.endswith(
        MODULE_ENTRY_SUFFIX
    ):
        return None
    return name[len(MODULE_ENTRY_PREFIX) : -len(MODULE_ENTRY_SUFFIX)]


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

EXPORT_RESOURCE_UNAVAILABLE: Final = "export_resource_unavailable"
"""Refused before a source snapshot is opened, when the configured per-workspace
connection budget cannot hold one. See :data:`MINIMUM_SNAPSHOT_CONNECTIONS`."""

MINIMUM_SNAPSHOT_CONNECTIONS: Final = 3
"""Pooled connections to one workspace database an export needs at once.

The worker's own work transaction, this source snapshot, and — because a handler's
duration is unbounded and ``CancellationToken`` opens its own short transactions —
a heartbeat. ``storage.pool_max_connections`` defaults to 5, so the check below is
about a deployment that has lowered it, not about the shipped configuration. A12 is
explicit that the answer to a pool too small is to refuse the export, never to raise
the configured pool or open an engine the pool cannot see."""


class ArtifactRefused(Exception):
    """A malformed or unsupported artifact, safe to show to an operator."""


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    workspace_id: UUID
    owner_account_id: UUID
    source_digest: bytes


@dataclass(frozen=True, slots=True)
class ExportSnapshot:
    """One committed source view, and everything read from it must come through here.

    Produced only by :func:`collect_export_snapshot`, and valid only inside that
    context manager's block: :attr:`uow` is an open
    :class:`~rheo_core.storage.backend.ExportSnapshotUnitOfWork`, and reading
    :attr:`connection` after the block has closed raises ``unit_of_work_closed``
    rather than answering from a second, later view.

    :attr:`snapshot_at` is the instant the snapshot's own first statement took, from
    ``statement_timestamp()`` on the source connection — not a Python clock reading,
    and not resampled per category. It is both the artifact's ``created_at`` and (from
    Prompt 13 on) the one instant retention is evaluated against, which is why it is
    carried on the value rather than read again wherever it is wanted.

    :attr:`overrides` is the settings source bound to this same transaction, so a
    workspace override a category read depends on is the value *this* snapshot sees.
    Resolving through the process-wide layered resolver instead would open a second
    connection with its own view, which is the disagreement A2 removed.
    """

    uow: UnitOfWork
    snapshot_at: datetime
    overrides: TransactionBoundOverrideSource
    workspace_id: UUID
    database_name: str

    @property
    def connection(self) -> Connection:
        """The one source connection, inside the one read-only transaction."""
        return self.uow.connection


@contextmanager
def collect_export_snapshot(
    workspace_id: UUID, *, worker_connection: Connection
) -> Iterator[ExportSnapshot]:
    """Open A12's one export source snapshot, before any source query runs.

    The order of the four steps is the contract, not an implementation detail:

    1. **Refuse a pool that cannot hold the snapshot, before anything is opened.**
       :data:`MINIMUM_SNAPSHOT_CONNECTIONS` against the pool's configured
       ``pool_size`` (``storage.pool_max_connections``). Read off the pool rather than
       re-resolving the setting, so the number checked is the number the engines were
       actually built with.
    2. **Route the workspace through the control plane** — ``active_workspace`` reads
       the registry row on every call and refuses a workspace that is not ``active``,
       exactly as ordinary routing does.
    3. **Verify the caller's own routed database against it.** ``worker_connection``
       is the worker's (or the dispatcher's) transaction, and a job payload naming a
       workspace whose database is not the one the worker is visiting would otherwise
       export one workspace's rows under another's envelope. That is the silent
       cross-workspace failure ``DATABASE_MISMATCH`` exists for, so it is refused
       loudly and before a second connection is taken.
    4. **Open the snapshot on a fresh pooled connection**, never
       ``worker_connection``: that transaction may already have queried, so its
       snapshot — if it even had one — is older than this export, and changing its
       isolation or committing it inside a handler is not this function's to do.

    The first two statements on the new connection are ``current_database()`` and
    ``statement_timestamp()``, in that order. The first is the cross-workspace probe
    again on the connection that will actually be read (the inherited test-profile
    probe covers it under ``profile = test`` only; this one is unconditional). Either
    of them establishes the repeatable-read snapshot, so taking the instant from the
    second means ``snapshot_at`` names a moment inside the view, never before it.

    **``OperationRefused`` is imported inside this function, and the reason is
    structural rather than stylistic.** ``rheo_core.operations.__init__`` imports
    ``core_ops`` as its first statement, ``core_ops`` imports
    ``rheo_core.exports.operations``, and that module imports this one — so a
    module-level ``from rheo_core.operations.refusals import ...`` here closes that
    cycle and ``import rheo_core.exports`` fails outright with half of this module's
    names still unbound. ``exports/operations.py`` can import it at module level only
    because it sits on the far side of the same chain. Deferred to call time, when
    every module in it is loaded.
    """
    from rheo_core.operations.refusals import OperationRefused

    pools = get_backend().pools
    if pools.pool_size < MINIMUM_SNAPSHOT_CONNECTIONS:
        raise OperationRefused(
            EXPORT_RESOURCE_UNAVAILABLE,
            f"an export holds {MINIMUM_SNAPSHOT_CONNECTIONS} connections to one "
            f"workspace database at once (the worker's transaction, the source "
            f"snapshot, and a heartbeat) but storage.pool_max_connections is "
            f"{pools.pool_size}",
        )
    row = active_workspace(workspace_id)
    routed = str(
        worker_connection.execute(text("SELECT current_database()")).scalar_one()
    )
    if routed != row.database_name:
        raise StorageRefusal(
            DATABASE_MISMATCH,
            f"the export names workspace {workspace_id}, whose database is "
            f"{row.database_name!r}, but its caller is connected to {routed!r}",
        )
    with pools.acquire(row.database_name) as engine:
        with ExportSnapshotUnitOfWork(engine, row.database_name) as uow:
            source = str(
                uow.connection.execute(text("SELECT current_database()")).scalar_one()
            )
            if source != row.database_name:
                raise StorageRefusal(
                    DATABASE_MISMATCH,
                    f"the export source snapshot expected database "
                    f"{row.database_name!r} but opened on {source!r}",
                )
            snapshot_at = uow.connection.execute(
                text("SELECT statement_timestamp()")
            ).scalar_one()
            assert isinstance(snapshot_at, datetime), snapshot_at
            yield ExportSnapshot(
                uow=uow,
                snapshot_at=snapshot_at,
                overrides=TransactionBoundOverrideSource(
                    uow.connection, workspace_id=workspace_id
                ),
                workspace_id=workspace_id,
                database_name=row.database_name,
            )


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


def _encoded(row: Mapping[str, object]) -> dict[str, object]:
    """One row as the JSON object the artifact carries.

    Split out of :func:`_line` because a module's rows are validated against that
    module's JSON Schema, and the schema describes **this** shape — the encoded one,
    where a UUID is a string and a ``bytea`` is lowercase hex — rather than the
    Python values the exporter handed over. Validating before encoding would check a
    different document from the one the artifact stores and the importer reads back.
    """
    return {key: _json_value(value) for key, value in row.items()}


def _line(row: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            _encoded(row), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
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


_DELETION_FIELDS: Final = tuple(deletion_record.c.keys())
"""The ledger's column order, taken from the table rather than written out again.

``_line`` sorts its keys, so the order does not reach the bytes; reading it off the
table is what makes a column added by a later migration appear in the category
without anybody remembering to add it here. A column that travels and a column that
does not is exactly the asymmetry a restore compares byte-for-byte and fails on.
"""


def _deletion_bytes(connection: Connection) -> bytes:
    """The content-free deletion ledger, every row, ascending by id.

    **Determinism is the whole requirement here**, because these bytes are compared
    against a restore of themselves. Ascending ``id`` is ascending mint time (UUIDv7)
    and is total, so the ordering does not depend on how many rows exist, on when the
    snapshot opened, or on the physical order Postgres happens to return.

    ``id`` and ``deleted_at`` travel verbatim and are written back verbatim
    (``deletion/records.py:insert_imported_deletion_record``); re-minting either on
    import would make a faithful restore report a digest its own source never had.

    This replaces the skeleton's unconditional ``b""``. It is core-owned metadata
    under export's existing owner/operator authority, not telemetry: the row carries
    no title, body, payload or json column, so there is nothing on it to withhold and
    no filtering to get wrong.
    """
    return _lines(
        {field: getattr(row, field) for field in _DELETION_FIELDS}
        for row in list_deletion_records(connection)
    )


def _exportable(connection: Connection) -> tuple[ModuleManifest, ...]:
    """The loaded manifests this workspace has installed, in dependency order.

    Two conditions, and both are needed. *Loaded* comes from the process — a manifest
    this interpreter cannot resolve has no exporter to call — and *installed* comes
    from the workspace's own ``module_state`` rows read on the snapshot connection, so
    an export describes the modules **that workspace** holds rather than the set this
    particular process happens to have loaded. Dependency order is the loader's own,
    because a dependant's rows may reference a dependency's and restoring them the
    other way round would have to defer the reference.

    ``loader`` is imported inside the function for the same structural reason
    :func:`collect_export_snapshot` defers ``OperationRefused``: the loader's manifest
    module carries this package's export protocols, so a module-level import here
    would close the cycle.
    """
    from rheo_core.modules.loader import loaded_in_dependency_order

    installed = {
        row.module_id
        for row in repositories.list_module_states(connection)
        if row.state != "removed"
    }
    return tuple(
        manifest
        for manifest in loaded_in_dependency_order()
        if manifest.module_id in installed
        and any(record.exportable for record in manifest.record_types)
    )


def _module_validator(manifest: ModuleManifest) -> Draft202012Validator:
    """The module's own JSON Schema, resolved inside its installed distribution.

    A12: ``resolve its schema resource inside the installed distribution``. The
    package is read off the exporter's own ``__module__`` rather than declared a
    second time on the manifest — the callable and the schema that describes what it
    writes ship in the same wheel by construction, and a second declaration is a
    second thing to keep in step.
    """
    package = str(getattr(manifest.export.exporter, "__module__", "")).partition(".")[0]
    if not package:
        raise ArtifactRefused(
            f"module {manifest.module_id!r} has no resolvable export package"
        )
    try:
        body = (
            resources.files(package)
            .joinpath(manifest.export.schema_path)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise ArtifactRefused(
            f"module {manifest.module_id!r} has no export schema "
            f"{manifest.export.schema_path!r}: {exc}"
        ) from None
    return Draft202012Validator(json.loads(body))


def _validated_module_rows(
    manifest: ModuleManifest, rows: Sequence[Mapping[str, object]], name: str
) -> list[dict[str, object]]:
    """Every line of one module's category, against that module's schema.

    Every line, not a sample: the schema is the only thing standing between a
    hand-edited artifact and a module importer, and a validator that checks the first
    row is a validator that checks nothing.
    """
    validator = _module_validator(manifest)
    validated: list[dict[str, object]] = []
    for number, row in enumerate(rows, 1):
        encoded = _encoded(row)
        try:
            validator.validate(encoded)
        except ValidationError as exc:
            raise ArtifactRefused(
                f"{name}:{number} is invalid: {exc.message}"
            ) from None
        validated.append(encoded)
    return validated


def module_categories(snapshot: ExportSnapshot) -> dict[str, bytes]:
    """Each installed exportable module's rows, keyed by their archive entry name.

    Read through the same :class:`ExportSnapshot` every core category is, so a module
    category and a core category describe one committed instant rather than two. The
    exporter receives the snapshot itself — never its connection — which is what
    keeps the fixed ``snapshot_at`` and the transaction-bound settings source in the
    module's hands rather than tempting it to sample a second clock.

    A module that refuses raises, and the refusal propagates: A12's
    ``export_requires_retention_sweep`` must leave no successful artifact, so it is
    not caught, downgraded or turned into an empty category here.
    """
    categories: dict[str, bytes] = {}
    for manifest in _exportable(snapshot.connection):
        name = module_entry_name(manifest.module_id)
        rows = manifest.export.exporter(snapshot)
        categories[name] = b"".join(
            (
                json.dumps(
                    encoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                + "\n"
            ).encode()
            for encoded in _validated_module_rows(manifest, rows, name)
        )
    return categories


def serialised_categories(
    snapshot: ExportSnapshot, *, skip_operation_id: UUID | None = None
) -> dict[str, bytes]:
    """The bytes both export and ``core.workspace.digest`` compare.

    Takes the established :class:`ExportSnapshot`, not a bare ``Connection``. Every
    category below is a separate statement, so on an ordinary read-committed
    connection each would see whatever had committed by the time it ran and the
    category set as a whole would describe no single instant — a signature that is
    invisible in the returned bytes and shows up only as an export that does not
    restore as one valid source state. The parameter is what makes the guarantee
    unavoidable rather than a caller's convention.
    """
    connection = snapshot.connection
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
        "deletions": _deletion_bytes(connection),
    }


def all_categories(
    snapshot: ExportSnapshot, *, skip_operation_id: UUID | None = None
) -> dict[str, bytes]:
    """The six core categories and every installed module's, in one mapping.

    The core six keep their short names and the module categories keep their archive
    entry names, so a key in this mapping is unambiguous about which half it came
    from and a module id can never collide with ``settings`` or ``audit``.
    """
    return {
        **serialised_categories(snapshot, skip_operation_id=skip_operation_id),
        **module_categories(snapshot),
    }


def digest_categories(
    snapshot: ExportSnapshot, *, skip_operation_id: UUID | None = None
) -> dict[str, dict[str, object]]:
    """Every category the artifact would carry, counted and hashed.

    Over :func:`all_categories` rather than the core six: the digest is what an
    operator compares a restore against, and a comparison that silently omitted the
    module rows would call two workspaces equal on the strength of their settings.
    """
    return {
        name: {
            "count": 0 if not body else body.count(b"\n"),
            "digest": hashlib.sha256(body).hexdigest(),
        }
        for name, body in all_categories(
            snapshot, skip_operation_id=skip_operation_id
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
    snapshot: ExportSnapshot,
    *,
    slug: str,
    owner_account_id: UUID,
    destination: Path,
    skip_operation_id: UUID | None,
) -> int:
    """Write the snapshot's bytes to ``destination`` and return their length.

    **It writes no row.** The ``core.export_record`` completion ``UPDATE`` this
    function used to make at its own tail is ``run_export_job``'s, on the worker's own
    unit of work, so publication and status stay inside the ordinary
    lease-conditional finish that commits the handler's effects and the job's
    ``succeeded`` together. Here that write would have committed (or not) on a
    connection with no lease predicate on it at all. The snapshot connection is
    ``READ ONLY`` besides, so the old shape is no longer expressible from this side.

    ``created_at`` is :attr:`ExportSnapshot.snapshot_at` rather than a parameter: the
    artifact records the instant its contents describe, and a caller-supplied clock
    reading taken around the collection would name a different one.

    ``workspace_id`` likewise comes off the snapshot, which is the id the routing and
    the ``current_database()`` check were performed against; taking it again from the
    payload would be a second value that could disagree with the rows being read.
    """
    connection = snapshot.connection
    categories = all_categories(snapshot, skip_operation_id=skip_operation_id)
    manifest = {
        "core_version": core_version(),
        "contract_version": CONTRACT_VERSION,
        "workspace_id": str(snapshot.workspace_id),
        "workspace_slug": slug,
        "owner_account_id": str(owner_account_id),
        "created_at": snapshot.snapshot_at.astimezone(UTC).isoformat(),
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
        **{
            name: body
            for name, body in categories.items()
            if name.startswith(MODULE_ENTRY_PREFIX)
        },
    }
    write_archive(entries, destination)
    return destination.stat().st_size


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
    from rheo_core.modules.loader import loaded_manifests

    loadable = loaded_manifests()
    unsupported = []
    for module in modules:
        assert isinstance(module, dict)
        module_id = str(module.get("module_id"))
        loaded = loadable.get(module_id)
        if loaded is not None:
            declared = module.get("export_format_version")
            if declared != loaded.export.format_version:
                raise ArtifactRefused(
                    f"module {module_id!r} exported format version {declared!r} "
                    f"but this host implements "
                    f"{loaded.export.format_version}"
                )
            continue
        if module_id == "harness" and current_profile() == "test":
            continue
        unsupported.append(module)
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


def _install_module_schemas(
    connection: Connection, manifest: Mapping[str, object], *, database_name: str
) -> None:
    """Build each loadable module's schema before a single one of its rows lands.

    A restore into an empty deployment provisions the **core** schema and nothing
    else, so without this a module category would be inserted into tables that do not
    exist. The chain is the same one ``core.module.install`` runs — extensions first,
    then every revision, on this transaction's connection — so a restored module
    schema is the schema an installed one has, rather than a second definition kept in
    step by hand.

    It runs on the restore's own connection inside the restore's own transaction,
    which is what keeps "any failure leaves no imported memory or deletion evidence"
    true of the DDL as well as of the rows.

    ``run_module_chain`` writes one ``module_schema_version`` row per applied
    revision, which is why :func:`_install_modules` writes none for a module that
    reaches here: the chain's rows *are* the source's rows, revision for revision and
    in the same order, and the artifact's single latest-version row would be a third.
    """
    from rheo_core.migrations.module_chain import run_module_chain
    from rheo_core.modules.loader import loaded_manifests

    declared = manifest["modules"]
    assert isinstance(declared, list)
    loadable = loaded_manifests()
    preparer = connection.dialect.identifier_preparer
    for module in declared:
        assert isinstance(module, dict)
        loaded = loadable.get(str(module["module_id"]))
        if loaded is None:
            continue
        for extension in loaded.storage.required_extensions:
            connection.execute(
                text(f"CREATE EXTENSION IF NOT EXISTS {preparer.quote(extension)}")
            )
        run_module_chain(
            connection,
            loaded,
            expected_database=database_name,
            core_version=core_version(),
        )


def _install_modules(connection: Connection, manifest: Mapping[str, object]) -> None:
    """Replay the artifact's module rows through the two repository writers.

    **The third caller of those writers, beside install and enable, and the reason
    ``storage/repositories.py`` can claim to be the only code that inserts into either
    table** (FR 10). This function used to hand-build both statements; routing them
    changed no column and widened no signature, which is what made the sole-writer
    property something a static scan can assert rather than something a reader has to
    take on trust.

    The values stay the *artifact's*, not today's: the recorded package version, the
    recorded state, and the source's ``core_version`` for the schema-version row. Only
    ``installed_at``/``enabled_at``/``applied_at`` are this restore's own instant,
    exactly as before.

    **The schema-version row is written only for a module whose chain did not run.**
    :func:`_install_module_schemas` runs the real chain for every loadable module and
    ``run_module_chain`` records a row per applied revision, so writing the
    artifact's single latest-version row on top would add a third row to a two-step
    chain and make a faithful restore disagree with its source's composition
    category. What is left here is the module a host cannot load and therefore cannot
    migrate — the test-profile harness — whose one recorded version is all there is.
    """
    from rheo_core.modules.loader import loaded_manifests

    modules = manifest["modules"]
    assert isinstance(modules, list)
    loadable = loaded_manifests()
    manifest_core_version = str(manifest["core_version"])
    for module in modules:
        assert isinstance(module, dict)
        now = datetime.now(UTC)
        module_id = str(module["module_id"])
        repositories.insert_module_state(
            connection,
            module_id=module_id,
            package_version=str(module["package_version"]),
            state=str(module.get("state", "enabled")),
            installed_at=now,
            enabled_at=now,
        )
        schema_version = module["schema_version"]
        if schema_version is not None and module_id not in loadable:
            repositories.insert_module_schema_version(
                connection,
                module_id=module_id,
                schema_version=str(schema_version),
                applied_at=now,
                core_version_at_apply=manifest_core_version,
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


_DELETION_COUNTERS: Final = (
    "cancelled_job_count",
    "cancelled_action_count",
    "removed_export_count",
    "invalidated_memory_count",
)


def _deletion_row(row: Mapping[str, object], number: int) -> DeletionRecordRow:
    """One validated ledger row, or :class:`ArtifactRefused`.

    Validated **here** rather than left to the table's own check constraints, even
    though both refuse. A constraint violation surfaces as a driver error with a
    constraint name in it, which is a database fault to whoever reads it; a
    hand-edited artifact is an invalid *artifact*, and an operator is owed that
    answer with the line number on it. The constraints stay as the floor under this.
    """

    def fail(detail: str) -> ArtifactRefused:
        return ArtifactRefused(f"{DELETIONS_NAME}:{number} {detail}")

    try:
        identity = UUID(str(row["id"]))
        deleted_at = datetime.fromisoformat(str(row["deleted_at"]))
        record_type = str(row["record_type"])
        record_id = UUID(str(row["record_id"]))
        actor_kind = str(row["actor_kind"])
    except (KeyError, TypeError, ValueError) as exc:
        raise fail(f"is not a deletion record: {exc}") from None
    cause = str(row.get("cause"))
    if cause not in DELETION_CAUSES:
        raise fail(f"has unsupported cause {cause!r}")
    successor = row.get("retained_successor_ref")
    if successor is not None and cause != RETENTION_EXPIRY:
        raise fail("names a retained successor on a row that is not an expiry")
    participants = row.get("participants")
    if not isinstance(participants, list) or not all(
        isinstance(item, str) for item in participants
    ):
        raise fail("has invalid participants")
    counters: dict[str, int] = {}
    for field in _DELETION_COUNTERS:
        value = row.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise fail(f"has invalid {field}")
        counters[field] = value
    if not record_type or "." not in record_type:
        raise fail(f"names unqualified record type {record_type!r}")
    optional: dict[str, UUID | None] = {}
    for field in ("actor_id", "approval_id"):
        value = row.get(field)
        try:
            optional[field] = None if value is None else UUID(str(value))
        except (TypeError, ValueError):
            raise fail(f"has invalid {field}") from None
    return DeletionRecordRow(
        id=identity,
        deleted_at=deleted_at,
        record_type=record_type,
        record_id=record_id,
        actor_kind=actor_kind,
        actor_id=optional["actor_id"],
        approval_id=optional["approval_id"],
        participants=tuple(str(item) for item in participants),
        cause=cause,
        retained_successor_ref=None if successor is None else str(successor),
        **counters,
    )


def _import_deletions(connection: Connection, body: bytes) -> None:
    """A12 step 5's core half: the ledger, verbatim, in the restore transaction.

    Identifiers and timestamps are preserved exactly as memory identifiers already
    are, which is what makes the deletion-evidence digest compute identically over a
    source workspace and a restore of it. Duplicates are refused rather than left to
    the primary key, for the reason :func:`_deletion_row` gives.
    """
    seen: set[UUID] = set()
    for number, row in enumerate(_jsonl(body, DELETIONS_NAME), 1):
        record = _deletion_row(row, number)
        if record.id in seen:
            raise ArtifactRefused(
                f"{DELETIONS_NAME}:{number} repeats deletion record {record.id}"
            )
        seen.add(record.id)
        _write_imported_deletion(connection, record)


def _import_modules(
    uow: UnitOfWork, entries: Mapping[str, bytes], manifest: Mapping[str, object]
) -> list[Callable[[], None]]:
    """A12 steps 2-4, first pass, for every module the artifact carries.

    Returns each module's second pass rather than running it, because A12 puts core's
    deletion-evidence import **between** the two: a module's marked-ancestry
    validation reads that evidence to tell a legitimately expired predecessor from a
    missing one, so resolving self-references before the ledger lands would fail
    every valid post-expiry chain.

    An artifact naming a module this host has not loaded never reaches here —
    :func:`_manifest` refuses it — and a module in the manifest with no category is
    an empty one, not a missing one: a workspace may hold an installed module with no
    rows in it.
    """
    from rheo_core.modules.loader import loaded_manifests

    declared = manifest["modules"]
    assert isinstance(declared, list)
    loadable = loaded_manifests()
    unexpected = sorted(
        name
        for name in entries
        if (found := module_id_for_entry(name)) is not None
        and found not in {str(module["module_id"]) for module in declared}
    )
    if unexpected:
        raise ArtifactRefused(f"artifact carries undeclared module rows: {unexpected}")
    continuations: list[Callable[[], None]] = []
    for module in declared:
        assert isinstance(module, dict)
        module_id = str(module["module_id"])
        loaded = loadable.get(module_id)
        if loaded is None:
            continue
        name = module_entry_name(module_id)
        rows = _jsonl(entries.get(name, b""), name)
        validator = _module_validator(loaded)
        for number, row in enumerate(rows, 1):
            try:
                validator.validate(row)
            except ValidationError as exc:
                raise ArtifactRefused(
                    f"{name}:{number} is invalid: {exc.message}"
                ) from None
        continuations.append(loaded.export.importer(uow, rows))
    return continuations


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
        _install_module_schemas(
            uow.connection, manifest, database_name=row.database_name
        )
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
        # A12's ordering, and the three steps are not rearrangeable. Each module
        # inserts its parents with self-references null and hands back what finishes;
        # the core deletion evidence lands next, because a module's marked-ancestry
        # check reads it; only then does the second pass resolve self-references and
        # validate every chain. Any one of them raising leaves the whole transaction
        # unwritten, which is what "no imported memory or deletion evidence" means.
        continuations = _import_modules(uow, entries, manifest)
        _import_deletions(uow.connection, entries[DELETIONS_NAME])
        for finish in continuations:
            finish()
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

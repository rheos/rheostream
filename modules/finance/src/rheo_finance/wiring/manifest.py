"""Finance's ``ModuleManifest``: one job, one gated schedule, its core settings keys,
an install check, eight READ operations with their tools, and no exportable record
type.

- The eight reader reads (``wiring.reads``, ADR-0028) are owner-only and
  ``package_sets="explicit_only"``: no named package set, connector grant or routine
  token reaches them, only a ``module_read:finance`` token. Their registered outputs
  leave out every restricted field. ``audit_sink`` is ``None`` because nothing above
  READ exists on core's side.
- ``export`` is required, but core exports only modules with an exportable record
  type, and finance declares none: ledger state never enters a workspace export. The
  two callables refuse loudly if that ever changes; state moves with
  ``finance state export``.
- The ``tick`` schedule is written at enable but enqueues only once the workspace
  itself sets ``finance.jobs.enabled`` to true.

Loading the manifest loads the ledger's operation catalogue (``wiring.reads``);
importing ``rheo_finance`` alone still imports no core.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import version
from typing import TYPE_CHECKING, Final

from rheo_contracts import CONTRACT_VERSION
from rheo_core.modules.manifest import (
    ExportDeclaration,
    JobKind,
    ModuleManifest,
    Schedule,
    StorageDeclaration,
)
from rheo_core.settings import KeySpec, Scope, ValueType
from sqlalchemy import text

from rheo_finance import db
from rheo_finance.wiring.reads import OPERATIONS, TOOLS
from rheo_finance.wiring.tick_job import (
    TICK_JOB_KIND,
    TICK_MAX_ATTEMPTS,
    TickPayload,
    finance_tick_job,
)

if TYPE_CHECKING:
    from rheo_core.exports.artifact import ExportSnapshot
    from rheo_core.storage.backend import UnitOfWork

MODULE_ID: Final = "finance"
JOBS_ENABLED_KEY: Final = "finance.jobs.enabled"
STAGES: Final = ("classify", "suggest", "extract")

SETTINGS: Final[tuple[KeySpec, ...]] = (
    # The schedule's gate: off until the workspace itself sets it.
    KeySpec(
        key=JOBS_ENABLED_KEY,
        type=ValueType.BOOL,
        scope=Scope.WORKSPACE,
        floor=None,
        explicit_per_workspace=False,
        default=False,
    ),
    *(
        KeySpec(
            key=f"finance.model.{stage}_model_id",
            type=ValueType.STR,
            scope=Scope.DEPLOYMENT,
            floor=None,
            explicit_per_workspace=False,
            default="",
        )
        for stage in STAGES
    ),
    KeySpec(
        key="finance.model.account_id",
        type=ValueType.STR,
        scope=Scope.WORKSPACE,
        floor=None,
        explicit_per_workspace=False,
        default="",
    ),
)

EXPORT_REFUSED: Final = "finance state moves with finance state export"


def refuse_export(_snapshot: ExportSnapshot, /) -> Sequence[Mapping[str, object]]:
    raise RuntimeError(EXPORT_REFUSED)


def refuse_import(
    _uow: UnitOfWork, _rows: Sequence[Mapping[str, object]], /
) -> Callable[[], None]:
    raise RuntimeError(EXPORT_REFUSED)


def install_check(uow: UnitOfWork) -> None:
    """The schema is at the code's head, read as ``finance_owner``."""
    connection = uow.connection
    with db.acting_as(connection, db.OWNER_ROLE):
        version_num = connection.execute(
            text(f"SELECT version_num FROM {db.SCHEMA}.{db.VERSION_TABLE}")
        ).scalar_one_or_none()
    if version_num != db.head_revision():
        raise RuntimeError("finance schema is not at head")


MANIFEST = ModuleManifest(
    module_id=MODULE_ID,
    package_version=version("rheo-finance"),
    core_contract_versions=(CONTRACT_VERSION,),
    dependencies=(),
    record_types=(),
    storage=StorageDeclaration(
        schema_name=MODULE_ID,
        migrations_path="rheo_finance.migrations",
        required_extensions=(),
    ),
    configuration_schema=SETTINGS,
    operations=OPERATIONS,
    tools=TOOLS,
    events=(),
    subscriptions=(),
    jobs=(
        JobKind(
            name=TICK_JOB_KIND,
            input_model=TickPayload,
            handler=finance_tick_job,
            max_attempts=TICK_MAX_ATTEMPTS,
            cancellable=False,
        ),
    ),
    schedules=(
        Schedule(
            name="tick",
            job_kind=TICK_JOB_KIND,
            cron="*/15 * * * *",
            enabled_by_default=True,
            interval_seconds=900,
            gate_setting=JOBS_ENABLED_KEY,
        ),
    ),
    resolvers=(),
    deletion_participants=(),
    export=ExportDeclaration(
        format_version=1,
        # Under reference/: the repository check admits package JSON only there.
        schema_path="reference/export.schema.json",
        exporter=refuse_export,
        importer=refuse_import,
    ),
    web=None,
    agent_guidance=None,
    secret_scopes=(),
    connector_bindings=(),
    health_checks=(install_check,),
    contract_tests="modules/finance/tests",
    sensitivity={},
    audit_sink=None,
    package_sets="explicit_only",
)

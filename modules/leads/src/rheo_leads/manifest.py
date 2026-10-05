"""Leads intake-only module declaration."""

from importlib.metadata import version

from rheo_contracts import CONTRACT_VERSION, SafetyClass, ToolDeclaration
from rheo_core.audit.core_sink import CORE_AUDIT_SINK
from rheo_core.events.consumers import ConsumerSubscription
from rheo_core.modules.manifest import (
    ConnectorBinding,
    DeletionParticipant,
    ExportDeclaration,
    ModuleManifest,
    RecordType,
    StorageDeclaration,
)

from rheo_leads.configuration import SETTINGS
from rheo_leads.contracts import CaptureInput
from rheo_leads.events import CONSUMER_ID, DELIVERY_RECEIVED, EVENTS
from rheo_leads.export import export_records, import_records
from rheo_leads.intake.process import process_delivery
from rheo_leads.lifecycle import on_observation_deleted
from rheo_leads.operations import OPERATIONS
from rheo_leads.resolvers import RECORD_TABLES, resolve_record

MANIFEST = ModuleManifest(
    module_id="leads",
    package_version=version("rheo-leads"),
    core_contract_versions=(CONTRACT_VERSION,),
    dependencies=(),
    record_types=tuple(
        RecordType(
            name=name,
            table=f"leads.{name}",
            deletable=False,
            delete_roles=frozenset(),
            exportable=name not in {"delivery_receipt", "observation"},
            audience_field=None,
        )
        for name in RECORD_TABLES
    ),
    storage=StorageDeclaration(
        schema_name="leads",
        migrations_path="rheo_leads.migrations",
        required_extensions=(),
    ),
    configuration_schema=SETTINGS,
    operations=OPERATIONS,
    tools=(
        ToolDeclaration(
            name="leads_capture",
            operation="leads.intake.capture",
            safety_class=SafetyClass.MUTATE,
            input_model=CaptureInput,
            description="Capture an inquiry into a chosen workspace funnel. "
            "A funnel is required; "
            "missing or malformed arguments return input_invalid, "
            "unavailable attribution returns not_found, "
            "an inactive manual connection returns connection_revoked, "
            "and oversized content returns payload_too_large. "
            "Acceptance queues processing; it does not create an opportunity.",
        ),
    ),
    events=EVENTS,
    subscriptions=(
        ConsumerSubscription(
            CONSUMER_ID, DELIVERY_RECEIVED, "leads", False, process_delivery
        ),
    ),
    jobs=(),
    schedules=(),
    resolvers=tuple((name, resolve_record) for name in RECORD_TABLES),
    deletion_participants=(
        DeletionParticipant(
            record_types=("leads.observation",), handler=on_observation_deleted
        ),
    ),
    export=ExportDeclaration(
        format_version=1,
        schema_path="export.schema.json",
        exporter=export_records,
        importer=import_records,
    ),
    web=None,
    agent_guidance=None,
    secret_scopes=(),
    connector_bindings=tuple(
        ConnectorBinding(
            transport=transport,
            service_operation="leads.intake.accept_delivery",
            route="/api/v1/intake/webhook/<connection_id>"
            if transport == "webhook"
            else None,
        )
        for transport in ("webhook", "import", "manual")
    ),
    health_checks=(),
    contract_tests="modules/leads/tests",
    sensitivity={},
    audit_sink=CORE_AUDIT_SINK,
)

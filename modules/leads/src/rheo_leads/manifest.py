"""Leads intake-only module declaration."""

from importlib.metadata import version

from rheo_contracts import CONTRACT_VERSION, Role, SafetyClass, ToolDeclaration
from rheo_core.audit.core_sink import CORE_AUDIT_SINK
from rheo_core.events.consumers import ConsumerSubscription
from rheo_core.modules.manifest import (
    ConnectorBinding,
    DeletionParticipant,
    Dependency,
    ExportDeclaration,
    FormDeclaration,
    ModuleManifest,
    NavigationEntry,
    RecordType,
    StorageDeclaration,
    WebContribution,
    WebRoute,
    WebSurface,
)

from rheo_leads.configuration import SETTINGS
from rheo_leads.connections import OPERATIONS as CONNECTION_OPERATIONS
from rheo_leads.connections import TOOLS as CONNECTION_TOOLS
from rheo_leads.connections import read_connection, record_refusal
from rheo_leads.contracts import CaptureInput
from rheo_leads.events import CONSUMER_ID, DELIVERY_RECEIVED, EVENTS
from rheo_leads.export import export_records, import_records
from rheo_leads.intake.process import process_delivery
from rheo_leads.lifecycle import (
    authorize_delete,
    delete_owned,
    on_observation_deleted,
    on_party_deleted,
)
from rheo_leads.operations import OPERATIONS
from rheo_leads.pipeline_common import RELATIONSHIPS
from rheo_leads.pipeline_contracts import DeleteInput
from rheo_leads.pipeline_operations import OPERATIONS as PIPELINE_OPERATIONS
from rheo_leads.pipeline_operations import TOOLS
from rheo_leads.resolvers import RECORD_TABLES, resolve_record
from rheo_leads.web_reads import OPERATIONS as WEB_OPERATIONS

MANIFEST = ModuleManifest(
    module_id="leads",
    package_version=version("rheo-leads"),
    core_contract_versions=(CONTRACT_VERSION,),
    dependencies=(Dependency(module_id=RELATIONSHIPS, version_range=">=0.1,<1"),),
    record_types=tuple(
        RecordType(
            name=name,
            table=f"leads.{name}",
            deletable=name in {"observation", "opportunity"},
            delete_roles=frozenset({Role.OWNER})
            if name in {"observation", "opportunity"}
            else frozenset(),
            authorize_delete=authorize_delete
            if name in {"observation", "opportunity"}
            else None,
            delete_owned=delete_owned
            if name in {"observation", "opportunity"}
            else None,
            exportable=name != "delivery_receipt",
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
    operations=OPERATIONS
    + PIPELINE_OPERATIONS
    + WEB_OPERATIONS
    + CONNECTION_OPERATIONS,
    tools=(
        *TOOLS,
        *CONNECTION_TOOLS,
        ToolDeclaration(
            name="leads_delete",
            operation="core.record.delete",
            safety_class=SafetyClass.DESTRUCTIVE,
            input_model=DeleteInput,
            description=(
                "Erase an observation or opportunity and its derived records. "
                "Requires per-action approval_required confirmation; missing records "
                "return not_found and changed records record_stale."
            ),
        ),
        ToolDeclaration(
            name="leads_capture",
            operation="leads.intake.capture",
            safety_class=SafetyClass.MUTATE,
            input_model=CaptureInput,
            description="Capture an inquiry into a chosen workspace funnel. "
            "For pasted inquiries, keep the original text in body.message and supply "
            "reviewed flat fields such as subject, person.name, person.email, "
            "person.phone and organization.name. "
            "Do not infer consent or verified identity. "
            "To add only a contact, use relationships_create_contact instead. "
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
            CONSUMER_ID,
            DELIVERY_RECEIVED,
            "leads",
            False,
            process_delivery,
            operation_names=frozenset(
                {
                    f"{RELATIONSHIPS}.party.resolve_or_create",
                    "leads.opportunity.create_from_observation",
                    "leads.opportunity.attach_observation",
                }
            ),
        ),
    ),
    jobs=(),
    schedules=(),
    resolvers=tuple((name, resolve_record) for name in RECORD_TABLES),
    deletion_participants=(
        DeletionParticipant(
            record_types=(f"{RELATIONSHIPS}.party",), handler=on_party_deleted
        ),
        DeletionParticipant(
            record_types=("leads.observation",), handler=on_observation_deleted
        ),
    ),
    export=ExportDeclaration(
        format_version=2,
        schema_path="export.schema.json",
        exporter=export_records,
        importer=import_records,
    ),
    web=WebContribution(
        surface=WebSurface(surface="leads", host="leads", path="/leads"),
        package_name="@rheo-stream/leads-web",
        navigation=(NavigationEntry(id="leads", label="Leads", path="/"),),
        routes=tuple(
            WebRoute(id=name, path=path, screen=name)
            for name, path in (
                ("list", "/"),
                ("board", "/board"),
                ("detail", "/detail"),
                ("capture", "/capture"),
                ("receipt", "/receipt"),
                ("connections", "/connections"),
            )
        ),
        record_views=(),
        search_providers=(),
        forms=tuple(
            FormDeclaration(operation="leads." + name, component="ActionForm")
            for name in (
                "intake.capture",
                "pipeline.create",
                "connection.set_routing",
                "opportunity.create_from_observation",
                "opportunity.update",
                "opportunity.transition",
                "opportunity.add_note",
                "qualification.assess",
                "followup.draft",
            )
        ),
    ),
    agent_guidance=None,
    secret_scopes=(),
    connector_bindings=tuple(
        ConnectorBinding(
            transport=transport,
            service_operation="leads.intake.accept_delivery",
            route="/api/v1/intake/webhook/<connection_id>"
            if transport == "webhook"
            else None,
            read_connection=read_connection if transport == "webhook" else None,
            record_refusal=record_refusal if transport == "webhook" else None,
        )
        for transport in ("webhook", "import", "manual")
    ),
    health_checks=(),
    contract_tests="modules/leads/tests",
    sensitivity={},
    audit_sink=CORE_AUDIT_SINK,
)

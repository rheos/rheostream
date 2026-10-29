"""Current: work items, projects, dependencies and areas. Requires no other module."""

from importlib.metadata import version

from rheo_contracts import CONTRACT_VERSION, Role
from rheo_core.audit.core_sink import CORE_AUDIT_SINK
from rheo_core.modules.manifest import (
    EventDeclaration,
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

from rheo_current.contracts import ItemEvent
from rheo_current.export import export_records, import_records
from rheo_current.external import DESTINATION
from rheo_current.lifecycle import authorize_delete, delete_owned
from rheo_current.operations import OPERATIONS, TOOLS
from rheo_current.references import MODULE_ID
from rheo_current.resolvers import resolve_record

MANIFEST = ModuleManifest(
    module_id=MODULE_ID,
    package_version=version("rheo-current"),
    core_contract_versions=(CONTRACT_VERSION,),
    dependencies=(),
    record_types=(
        *(
            RecordType(
                name=name,
                table=f"current.{name}",
                deletable=True,
                delete_roles=frozenset({Role.OWNER}),
                exportable=True,
                audience_field=None,
                authorize_delete=authorize_delete,
                delete_owned=delete_owned,
            )
            for name in ("item", "project")
        ),
        RecordType(
            name="comment",
            table="current.comment",
            deletable=False,
            delete_roles=frozenset(),
            exportable=True,
            audience_field=None,
        ),
    ),
    storage=StorageDeclaration(
        schema_name=MODULE_ID,
        migrations_path="rheo_current.migrations",
        required_extensions=(),
    ),
    configuration_schema=(),
    operations=OPERATIONS,
    destinations=(DESTINATION,),
    tools=TOOLS,
    events=tuple(
        EventDeclaration(type=f"current.item.{verb}", schema_version=1, data=ItemEvent)
        for verb in ("created", "completed", "reopened")
    ),
    subscriptions=(),
    jobs=(),
    schedules=(),
    resolvers=(("item", resolve_record), ("project", resolve_record)),
    deletion_participants=(),
    export=ExportDeclaration(
        format_version=1,
        schema_path="export.schema.json",
        exporter=export_records,
        importer=import_records,
    ),
    web=WebContribution(
        surface=WebSurface(surface="current", host="current", path="/current"),
        package_name="@rheo-stream/current-web",
        navigation=(NavigationEntry(id="current", label="Current", path="/"),),
        # Exact paths only; a record travels as ?ref= in the query string.
        routes=tuple(
            WebRoute(id=name, path=path, screen=screen)
            for name, path, screen in (
                ("queue", "/", "queue"),
                ("item", "/item", "item"),
                ("new", "/new", "newItem"),
                ("projects", "/projects", "projects"),
                ("project", "/project", "project"),
                ("areas", "/areas", "areas"),
            )
        ),
        record_views=(),
        search_providers=(),
        # Every MUTATE operation the screens submit. Deletion stays with
        # core.record.delete and its per-action approval, outside the form path.
        forms=tuple(
            FormDeclaration(operation="current." + name, component="ActionForm")
            for name in (
                "item.create",
                "item.update",
                "item.set_status",
                "item.comment",
                "dependency.add",
                "dependency.remove",
                "project.create",
                "project.update",
                "area.create",
                "area.update",
            )
        ),
    ),
    agent_guidance=None,
    secret_scopes=(),
    connector_bindings=(),
    health_checks=(),
    contract_tests="modules/current/tests",
    sensitivity={},
    audit_sink=CORE_AUDIT_SINK,
)

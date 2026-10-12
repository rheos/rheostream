"""Operation, role and MCP declarations: the permission and audit surface."""

from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    ToolDeclaration,
)
from rheo_core.operations.registry import Handler

from rheo_current import areas, items, projects
from rheo_current import contracts as c

MEMBERS = frozenset({Role.OWNER, Role.MEMBER})
READERS = MEMBERS | {Role.SERVICE}
OWNER = frozenset({Role.OWNER})
READ, MUTATE = SafetyClass.READ, SafetyClass.MUTATE
REFUSALS = (
    " Missing records return not_found; a revision that moved on returns record_stale."
)
SPECS = (
    (
        "item.create",
        c.ItemCreate,
        c.ItemCreated,
        items.create,
        MUTATE,
        MEMBERS,
        None,
        "current_create",
        "Create a work item. Without source_ref this always creates, so search with "
        "current_list first. With source_kind and source_ref (for example an email "
        "thread or alert id) a repeat signal folds into the existing item: action "
        "grouped, reopened (was done) or woke (was snoozed); pass fingerprint (such as "
        "the latest message id) so the same signal twice changes nothing. A repeat "
        "never changes title, body, area or project, and only raises priority. "
        "Areas come from current_areas; dates are YYYY-MM-DD. area_unknown and "
        "area_retired refuse an unusable area.",
    ),
    (
        "item.get",
        c.ItemInput,
        c.ItemDetail,
        items.get,
        READ,
        READERS,
        None,
        "current_get",
        "Read one work item with its comments, project, unfinished blockers and the "
        "items it blocks.",
    ),
    (
        "item.list",
        c.ItemList,
        c.ItemPage,
        items.listing,
        READ,
        READERS,
        None,
        "current_list",
        "List work items, highest priority then most recently updated first. status "
        "active (the default) means open, waiting and snoozed items whose snooze has "
        "passed, flagged woke. Filter by area, project_ref, priority, due_before "
        "(inclusive date), overdue, woke, blocked, or query (full-text over title, "
        "body and comments). Page with offset and next_offset.",
    ),
    (
        "item.update",
        c.ItemUpdate,
        c.ItemDetail,
        items.edit,
        MUTATE,
        MEMBERS,
        "ref",
        "current_update",
        "Edit title, body, priority, area, project_ref or due_on, with the item's "
        "current revision. Omitted or null fields stay as they are; list area, "
        "project_ref or due_on in clear to empty them. Status changes use "
        "current_status.",
    ),
    (
        "item.set_status",
        c.ItemStatusInput,
        c.ItemDetail,
        items.set_status,
        MUTATE,
        MEMBERS,
        "ref",
        "current_status",
        "Move an item to open, waiting, snoozed or done, with its current revision. "
        "snoozed needs a future snoozed_until (ISO time with offset) and no other "
        "status takes one. done can only be reopened to open "
        "(transition_not_allowed otherwise). Completing a blocked item is allowed. "
        "snooze_invalid refuses a missing, past or misplaced snooze time.",
    ),
    (
        "item.comment",
        c.CommentInput,
        c.ItemDetail,
        items.comment,
        MUTATE,
        MEMBERS,
        "ref",
        "current_comment",
        "Append a comment to an item. Comments cannot be edited; the author is the "
        "signed-in caller.",
    ),
    (
        "dependency.add",
        c.DependencyInput,
        c.ItemDetail,
        items.add_dependency,
        MUTATE,
        MEMBERS,
        "item_ref",
        "current_add_dependency",
        "Record that item_ref is blocked by blocker_ref. Adding an existing edge "
        "changes nothing; dependency_cycle refuses an edge that would make a loop.",
    ),
    (
        "dependency.remove",
        c.DependencyInput,
        c.ItemDetail,
        items.remove_dependency,
        MUTATE,
        MEMBERS,
        "item_ref",
        "current_remove_dependency",
        "Remove the edge saying item_ref is blocked by blocker_ref.",
    ),
    (
        "project.create",
        c.ProjectCreate,
        c.ProjectOutput,
        projects.create,
        MUTATE,
        MEMBERS,
        None,
        "current_create_project",
        "Create a project (active, paused or done) that work items can belong to. "
        "Check current_list_projects first.",
    ),
    (
        "project.get",
        c.ProjectInput,
        c.ProjectOutput,
        projects.get,
        READ,
        READERS,
        None,
        None,
        "",
    ),
    (
        "project.list",
        c.ProjectList,
        c.ProjectPage,
        projects.listing,
        READ,
        READERS,
        None,
        "current_list_projects",
        "List projects with item counts by status, optionally by status or area.",
    ),
    (
        "project.update",
        c.ProjectUpdate,
        c.ProjectOutput,
        projects.edit,
        MUTATE,
        MEMBERS,
        "ref",
        "current_update_project",
        "Edit a project's title, description, status, area or due_on, with its "
        "current revision. Omitted or null fields stay as they are; list area or "
        "due_on in clear to empty them.",
    ),
    (
        "area.list",
        c.AreaList,
        c.AreaPage,
        areas.listing,
        READ,
        READERS,
        None,
        "current_areas",
        "List the workspace's areas. Use an area's key on items and projects.",
    ),
    ("area.create", c.AreaCreate, c.Area, areas.create, MUTATE, OWNER, None, None, ""),
    ("area.update", c.AreaUpdate, c.Area, areas.edit, MUTATE, OWNER, None, None, ""),
    (
        "summary.get",
        c.Empty,
        c.Summary,
        items.summarize,
        READ,
        READERS,
        None,
        "current_summary",
        "Counts of work items by status and by area, plus woke, overdue and blocked.",
    ),
)
OPERATIONS: tuple[tuple[OperationDeclaration, Handler], ...] = tuple(
    (
        OperationDeclaration(
            name="current." + name,
            safety_class=safety,
            roles=roles,
            input_model=model,
            output=output,
            idempotency=Idempotency.NONE,
            audit=None if safety is READ else AuditSpec(subject_field=subject),
        ),
        handler,
    )
    for name, model, output, handler, safety, roles, subject, _, _ in SPECS
)
TOOLS = (
    *(
        ToolDeclaration(
            name=tool,
            operation="current." + name,
            safety_class=safety,
            input_model=model,
            description=description + REFUSALS,
        )
        for name, model, _, _, safety, _, _, tool, description in SPECS
        if tool
    ),
    *(
        ToolDeclaration(
            name=f"current_delete_{kind}",
            operation="core.record.delete",
            safety_class=SafetyClass.DESTRUCTIVE,
            input_model=model,
            description=description
            + " Owner only. Requires per-action approval_required confirmation; "
            "a record that changed since the request returns record_stale.",
        )
        for kind, model, description in (
            (
                "item",
                c.DeleteItemInput,
                "Erase a work item with its comments and dependency edges.",
            ),
            (
                "project",
                c.DeleteProjectInput,
                "Erase a project. Its items stay and are detached from it.",
            ),
        )
    ),
)

"""Leads intake settings and the closed release-one fact vocabulary."""

from typing import Final

from rheo_core.settings import KeySpec, Scope, ValueType
from rheo_core.settings.schema import Floor

MODULE_ID: Final = "leads"
FACTS: Final = (
    "person.name",
    "person.email",
    "person.phone",
    "organization.name",
    "organization.domain",
    "subject",
    "message",
    "interest",
    "source_url",
    "locale",
    "value.amount",
    "value.currency",
    "value.basis",
)
JOBSEARCH_KEY: Final = "leads.capabilities.jobsearch"
# Off unless the workspace itself turned it on: the reader requires a workspace row.
# The deployment default permits that opt-in; ``Floor.AND`` lets an operator who sets
# ``false`` veto it for every workspace.
JOBSEARCH_SETTING: Final = KeySpec(
    key=JOBSEARCH_KEY,
    type=ValueType.BOOL,
    scope=Scope.WORKSPACE,
    floor=Floor.AND,
    explicit_per_workspace=False,
    default=True,
)
# The job feed's endpoint configuration, set by the operator. Empty means not
# configured; no vendor host is a package default.
JOBFEED_SETTINGS: Final = (
    KeySpec(
        key="leads.jobfeed.resource_url",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="",
    ),
    KeySpec(
        key="leads.jobfeed.job_hosts",
        type=ValueType.STR_LIST,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=(),
    ),
    # Put in front of each tool name the collector calls, for a provider that
    # names its tools with a prefix. The run checks its shape.
    KeySpec(
        key="leads.jobfeed.tool_prefix",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="",
    ),
)
SETTINGS: Final = (
    (JOBSEARCH_SETTING,)
    + tuple(
        KeySpec(
            key=f"leads.intake.{name}",
            type=ValueType.INT,
            scope=Scope.WORKSPACE,
            floor=Floor.MIN,
            explicit_per_workspace=False,
            default=default,
            minimum=1,
        )
        for name, default in (
            ("max_payload_bytes", 262144),
            ("replay_window_seconds", 300),
            ("health_write_interval_seconds", 60),
            ("max_rotation_overlap_seconds", 3600),
            ("max_import_rows", 10000),
        )
    )
    + JOBFEED_SETTINGS
)

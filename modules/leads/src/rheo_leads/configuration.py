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
SETTINGS: Final = tuple(
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

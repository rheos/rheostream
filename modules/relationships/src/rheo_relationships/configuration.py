"""Workspace matching limits; integer percent matches the core setting vocabulary."""

from rheo_core.settings import KeySpec, Scope, ValueType

SETTINGS = tuple(
    KeySpec(
        key=f"relationships.match.{name}",
        type=ValueType.INT,
        scope=Scope.WORKSPACE,
        floor=None,
        explicit_per_workspace=False,
        default=default,
        minimum=minimum,
        maximum=maximum,
    )
    for name, default, minimum, maximum in (
        ("name_similarity_percent", 60, 0, 100),
        ("max_candidates", 10, 1, 100),
    )
)

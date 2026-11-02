"""Fail-closed production policy for upper-class external composition.

This is deliberately a final-composition assertion rather than a registration-time
allowance.  Modules still register through the ordinary exact registries; the two
deployed composition roots call this assertion only after every selected module has
loaded, when declarations and destinations can be compared as one immutable set.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from rheo_contracts import ExternalSpec, Idempotency, SafetyClass

from rheo_core.modules.execution import DESTINATIONS, DestinationRegistry
from rheo_core.modules.manifest import DestinationMode
from rheo_core.operations.registry import REGISTRY, OperationRegistry

CURRENT_EXTERNAL_SPEC: Final = ExternalSpec(
    destination_field="destination_ref",
    destination_ref="current",
    result_ref_field="item_ref",
)
PRODUCTION_EXTERNAL_ALLOWLIST: Final[Mapping[str, tuple[str, ExternalSpec]]] = (
    MappingProxyType(
        {
            "current.item.create_external": (
                "current",
                CURRENT_EXTERNAL_SPEC,
            )
        }
    )
)
"""The complete production EXTERNAL policy; absence is permitted, widening is not."""


class ProductionExternalCompositionError(RuntimeError):
    """The final process composition is unsafe for production execution."""


def assert_production_external_composition(
    registry: OperationRegistry = REGISTRY,
    destinations: DestinationRegistry = DESTINATIONS,
) -> None:
    """Refuse every composition outside the one exact production tuple.

    Current may be absent.  When it is present, its declaration must be KEYED and
    match the immutable :class:`ExternalSpec`; its sole destination registration
    must agree on canonical destination, operation, owner and in-process mode.
    FINANCIAL declarations, unkeyed inline EXTERNAL holds, non-allowlisted external
    declarations and orphan registrations always fail.
    """

    problems: list[str] = []
    for operation_name in sorted(registry.names()):
        registered = registry.lookup(operation_name)
        if registered is None:  # pragma: no cover - names/lookup share one table
            problems.append(
                f"operation {operation_name!r} disappeared during inspection"
            )
            continue
        declaration = registered.declaration
        safety_class = getattr(declaration, "safety_class", None)
        if safety_class is SafetyClass.FINANCIAL:
            problems.append(
                f"operation {operation_name!r} is FINANCIAL; production permits none"
            )
            continue
        if safety_class is not SafetyClass.EXTERNAL:
            continue

        allowed = PRODUCTION_EXTERNAL_ALLOWLIST.get(operation_name)
        if allowed is None:
            problems.append(
                f"operation {operation_name!r} is EXTERNAL but is not allowlisted"
            )
            continue
        expected_owner, expected_spec = allowed
        if registered.module_id != expected_owner:
            problems.append(
                f"operation {operation_name!r} is owned by {registered.module_id!r}, "
                f"not {expected_owner!r}"
            )
        if (
            declaration.idempotency is not Idempotency.KEYED
            or declaration.keyed is None
        ):
            problems.append(
                f"operation {operation_name!r} is not KEYED; production forbids "
                "inline EXTERNAL holds"
            )
        if declaration.external != expected_spec:
            problems.append(
                f"operation {operation_name!r} has ExternalSpec "
                f"{declaration.external!r}, not {expected_spec!r}"
            )

        found = destinations.lookup(expected_spec.destination_ref)
        if found is None:
            problems.append(
                f"operation {operation_name!r} has no destination registration for "
                f"{expected_spec.destination_ref!r}"
            )
            continue
        registration = found.registration
        if found.origin != expected_owner:
            problems.append(
                f"destination {expected_spec.destination_ref!r} is owned by "
                f"{found.origin!r}, not {expected_owner!r}"
            )
        if registration.destination_ref != expected_spec.destination_ref:
            problems.append(
                f"destination registration key {expected_spec.destination_ref!r} "
                f"contains destination {registration.destination_ref!r}"
            )
        if registration.operation_name != operation_name:
            problems.append(
                f"destination {expected_spec.destination_ref!r} names operation "
                f"{registration.operation_name!r}, not {operation_name!r}"
            )
        if registration.mode is not DestinationMode.IN_PROCESS:
            problems.append(
                f"destination {expected_spec.destination_ref!r} uses mode "
                f"{registration.mode.value!r}, not {DestinationMode.IN_PROCESS.value!r}"
            )

    for destination_ref in sorted(destinations.names()):
        found = destinations.lookup(destination_ref)
        if found is None:  # pragma: no cover - names/lookup share one table
            problems.append(
                f"destination {destination_ref!r} disappeared during inspection"
            )
            continue
        registration = found.registration
        operation = registry.lookup(registration.operation_name)
        if operation is None:
            problems.append(
                f"destination {destination_ref!r} is orphaned from missing operation "
                f"{registration.operation_name!r}"
            )
            continue
        allowed = PRODUCTION_EXTERNAL_ALLOWLIST.get(registration.operation_name)
        if allowed is None:
            problems.append(
                f"destination {destination_ref!r} registers nonallowlisted operation "
                f"{registration.operation_name!r}"
            )
            continue
        expected_owner, expected_spec = allowed
        if operation.declaration.safety_class is not SafetyClass.EXTERNAL:
            problems.append(
                f"destination {destination_ref!r} names operation "
                f"{registration.operation_name!r}, which is not EXTERNAL"
            )
        if destination_ref != expected_spec.destination_ref:
            problems.append(
                f"operation {registration.operation_name!r} is registered under "
                f"destination {destination_ref!r}, not "
                f"{expected_spec.destination_ref!r}"
            )
        if found.origin != expected_owner:
            problems.append(
                f"destination {destination_ref!r} is owned by {found.origin!r}, "
                f"not {expected_owner!r}"
            )

    if problems:
        raise ProductionExternalCompositionError(
            "unsafe production external composition:\n  " + "\n  ".join(problems)
        )

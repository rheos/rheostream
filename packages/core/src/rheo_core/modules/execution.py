"""Process-wide module guard and exact external-destination registrations."""

from dataclasses import dataclass
from typing import Final

from rheo_contracts import WorkspaceContext

from rheo_core.modules.manifest import DestinationRegistration, GuardRegistration
from rheo_core.storage.backend import UnitOfWork


@dataclass(frozen=True, slots=True)
class RegisteredGuard:
    registration: GuardRegistration
    origin: str


class GuardRegistry:
    """Qualified module guards, with idempotent exact re-registration."""

    def __init__(self) -> None:
        self._guards: dict[str, RegisteredGuard] = {}

    def register(self, registration: GuardRegistration, *, origin: str) -> None:
        if not registration.name.startswith(f"{origin}."):
            raise ValueError(
                f"guard {registration.name!r} is not owned by origin {origin!r}"
            )
        existing = self._guards.get(registration.name)
        candidate = RegisteredGuard(registration, origin)
        if existing is not None:
            if existing == candidate:
                return
            raise ValueError(f"guard {registration.name!r} is already registered")
        self._guards[registration.name] = candidate

    def lookup(self, name: str) -> object | None:
        found = self._guards.get(name)
        return None if found is None else found.registration.guard

    def names(self) -> frozenset[str]:
        return frozenset(self._guards)


@dataclass(frozen=True, slots=True)
class RegisteredDestination:
    registration: DestinationRegistration
    origin: str


class DestinationRegistry:
    """One owner and one operation for each canonical destination reference."""

    def __init__(self) -> None:
        self._destinations: dict[str, RegisteredDestination] = {}

    def register(self, registration: DestinationRegistration, *, origin: str) -> None:
        if not registration.operation_name.startswith(f"{origin}."):
            raise ValueError(
                f"destination operation {registration.operation_name!r} is not "
                f"owned by origin {origin!r}"
            )
        existing = self._destinations.get(registration.destination_ref)
        candidate = RegisteredDestination(registration, origin)
        if existing is not None:
            if existing == candidate:
                return
            raise ValueError(
                f"destination {registration.destination_ref!r} is already registered"
            )
        self._destinations[registration.destination_ref] = candidate

    def lookup(self, destination_ref: str) -> RegisteredDestination | None:
        return self._destinations.get(destination_ref)

    def names(self) -> frozenset[str]:
        return frozenset(self._destinations)

    def availability_problem(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        destination_ref: str,
        operation_name: str,
    ) -> str | None:
        found = self.lookup(destination_ref)
        if found is None:
            return f"destination {destination_ref!r} is not registered"
        registration = found.registration
        if registration.operation_name != operation_name:
            return (
                f"destination {destination_ref!r} is registered for "
                f"{registration.operation_name!r}, not {operation_name!r}"
            )
        try:
            if not registration.is_installed():
                return f"destination {destination_ref!r} is not installed"
            if not registration.is_enabled(ctx):
                return f"destination {destination_ref!r} is not enabled"
            if not registration.is_ready(ctx, uow):
                return f"destination {destination_ref!r} is not ready"
            credentials = registration.credentials_available
            if credentials is not None and not credentials(ctx, uow):
                return f"destination {destination_ref!r} credentials are unavailable"
        except Exception:
            return f"destination {destination_ref!r} availability could not be proven"
        return None


EXECUTION_GUARDS: Final = GuardRegistry()
DESTINATIONS: Final = DestinationRegistry()

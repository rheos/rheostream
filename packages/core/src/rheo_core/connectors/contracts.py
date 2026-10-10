"""Module-owned metadata callbacks; credential bytes never enter a module."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from rheo_core.storage.backend import UnitOfWork


@dataclass(frozen=True)
class ConnectionState:
    active: bool
    generation: int
    current_ref: str | None = field(repr=False)
    previous_ref: str | None = field(repr=False)
    previous_until: datetime | None
    max_bytes: int
    replay_seconds: int


ConnectionReader = Callable[[UUID, UnitOfWork, UUID], ConnectionState | None]
FailureRecorder = Callable[[UUID, UnitOfWork, UUID], None]

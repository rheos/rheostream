"""The scheduled job feed collection.

The ticker enqueues ``leads.jobfeed_collect`` for a workspace whose collector is
connected and whose job search capability is on. The handler does not collect yet:
it returns at once and reads, writes and calls nothing.
"""

from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.work.cancellation import CancellationToken

COLLECT_JOB_KIND: Final = "leads.jobfeed_collect"
SCHEDULE_NAME: Final = "jobfeed_collect"
COLLECT_MAX_ATTEMPTS: Final = 8
"""The retry budget, on the precedent of the other scheduled sweeps."""


class CollectPayload(BaseModel):
    """What the ticker enqueues: the workspace it visited, and nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID


def collect_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """Does not collect yet."""
    return None

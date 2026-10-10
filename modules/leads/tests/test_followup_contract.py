"""The calendar date boundary never coerces a timestamp or numeric epoch."""

import pytest
from pydantic import ValidationError
from rheo_leads.pipeline_contracts import FollowupSchedule, ListInput

BASE = {
    "ref": "leads.opportunity:00000000-0000-7000-8000-000000000000",
    "revision": 1,
    "action": "Discuss scope",
    "due_on": "2026-10-12",
}


@pytest.mark.parametrize(
    "due",
    ["2026-10-12T00:00:00Z", 1791763200, "2026-02-30", "20261012", "2026-1-2", ""],
)
def test_non_calendar_dates_are_invalid(due: object) -> None:
    with pytest.raises(ValidationError):
        FollowupSchedule.model_validate({**BASE, "due_on": due})
    with pytest.raises(ValidationError):
        ListInput.model_validate({"followup_due_by": due})


@pytest.mark.parametrize("action", [" ", "x" * 2001])
def test_action_is_meaningful_and_bounded(action: str) -> None:
    with pytest.raises(ValidationError):
        FollowupSchedule.model_validate({**BASE, "action": action})

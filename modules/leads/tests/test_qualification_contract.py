"""Qualification judgments are explicit, bounded and provenance-bearing."""

import pytest
from pydantic import ValidationError
from rheo_leads.pipeline_contracts import Assessment

BASE = {
    "fit": "high",
    "intent": "medium",
    "urgency": "needs_information",
    "evidence_completeness": "low",
    "uncertainty": "high",
    "explanation": "The budget and date are unconfirmed.",
    "author": "human",
}


@pytest.mark.parametrize(
    "changes",
    [
        {"fit": "certain"},
        {"uncertainty": "needs_information"},
        {"explanation": "   "},
        {"explanation": "x" * 16001},
        {"author": "model"},
        {"author": "model", "model_id": "example"},
        {"author": "model", "model_id": " ", "prompt_version": "v1"},
        {"model_id": "example", "prompt_version": "v1"},
        {"evidence_refs": ["unlinked-evidence"]},
        {"input_revision": 7},
        {"created_by_id": "someone-else"},
        {"rubric_version": 2},
    ],
)
def test_invalid_or_caller_supplied_binding_is_refused(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Assessment.model_validate({**BASE, **changes})


def test_dimensions_cannot_be_omitted() -> None:
    for name in ("fit", "intent", "urgency", "evidence_completeness"):
        payload = {k: v for k, v in BASE.items() if k != name}
        with pytest.raises(ValidationError):
            Assessment.model_validate(payload)

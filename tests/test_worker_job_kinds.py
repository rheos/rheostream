"""Production ``JOB_KINDS`` lives on the worker composition root."""

import pytest
from rheo_app_worker.main import JOB_KINDS
from rheo_core.exports import (
    EXPORT_JOB_KIND,
    RESTORE_JOB_KIND,
    ExportJobPayload,
    RestoreJobPayload,
    run_export_job,
    run_restore_job,
)
from rheo_core.work.kinds import JobKindUnknown


def test_production_job_kinds_are_exactly_export_and_restore() -> None:
    assert JOB_KINDS.names() == frozenset({EXPORT_JOB_KIND, RESTORE_JOB_KIND})

    export_model, export_handler = JOB_KINDS.lookup(EXPORT_JOB_KIND)
    assert export_model is ExportJobPayload
    assert export_handler is run_export_job

    restore_model, restore_handler = JOB_KINDS.lookup(RESTORE_JOB_KIND)
    assert restore_model is RestoreJobPayload
    assert restore_handler is run_restore_job


def test_production_job_kinds_still_refuse_an_unknown_kind() -> None:
    with pytest.raises(JobKindUnknown, match="core.missing"):
        JOB_KINDS.lookup("core.missing")

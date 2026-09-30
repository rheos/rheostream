"""Issue #12: ``record_stale`` is one word across the core, the module and the API.

The Postgres behaviour (two racing writers, exactly one ``StaleRecord``) is in
``tests/postgres/test_memory_lifecycle.py``. This file holds the pieces that need no
database: the refusal name the dispatcher answers with, and the HTTP listener answering
it as a conflict the caller can retry rather than as a malformed request or a missing
record. Recallatron imports this same constant rather than spelling it again; this file
stays off the module on purpose, because core and app tests may not depend on it
(``tests/test_module_import_graph.py``).
"""

from rheo_app_core.api_routes import outcome_status
from rheo_core.operations import RECORD_STALE, OperationOutcome
from rheo_core.operations.dispatch import OperationError


def test_record_stale_is_the_documented_refusal_name() -> None:
    assert RECORD_STALE == "record_stale"


def test_a_stale_refusal_answers_409_conflict() -> None:
    outcome = OperationOutcome(
        RECORD_STALE,
        error=OperationError(RECORD_STALE, "that memory has moved"),
    )

    assert outcome_status(outcome) == 409

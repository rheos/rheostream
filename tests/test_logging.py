"""``rheo_core.log_config``, the one logging setup every entry point shares (issue
#229).

Two things this file proves that no test proved before:

1. **A record logged with ``extra`` comes out with its fields.** Before this module
   existed the tree had no formatter that read ``extra`` at all, so
   ``evidence_acceptance_failed`` and its siblings reached stderr as a bare message
   name and every diagnostic field (``error_type``, ``evidence_id``, ...) was
   dropped. :class:`JsonLogFormatter` is what renders them; this file logs a record
   with ``extra`` and parses the formatter's own output back as JSON to check.
2. **A content-free line's fields stay content-free.** :func:`content_free_extra` is
   the guard the evidence paths' five log call sites wrap their ``extra`` mapping in
   (``packages/core/src/rheo_core/evidence/service.py``,
   ``packages/core/src/rheo_core/runtime/operations.py``,
   ``modules/recallatron/src/rheo_recallatron/automatic.py``): a field name outside
   the allowlist, or a value shaped like a sentence rather than a token, raises
   before the record is ever built.
"""

import json
import logging

import pytest
from rheo_core.log_config import (
    ALLOWED_EXTRA_FIELDS,
    ContentFreeExtraError,
    JsonLogFormatter,
    configure_logging,
    content_free_extra,
)


def _make_record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="rheo_core.evidence",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="evidence_acceptance_failed",
        args=None,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_log_formatter_renders_extra_fields() -> None:
    record = _make_record(
        evidence_id="11111111-1111-1111-1111-111111111111", attempts=5
    )
    rendered = json.loads(JsonLogFormatter().format(record))
    assert rendered["message"] == "evidence_acceptance_failed"
    assert rendered["level"] == "WARNING"
    assert rendered["logger"] == "rheo_core.evidence"
    assert rendered["evidence_id"] == "11111111-1111-1111-1111-111111111111"
    assert rendered["attempts"] == 5
    # No standard LogRecord attribute leaks in under its own name a second time.
    assert "msg" not in rendered
    assert "args" not in rendered


def test_json_log_formatter_renders_no_extra_fields_unchanged() -> None:
    record = _make_record()
    rendered = json.loads(JsonLogFormatter().format(record))
    assert rendered == {
        "timestamp": rendered["timestamp"],
        "level": "WARNING",
        "logger": "rheo_core.evidence",
        "message": "evidence_acceptance_failed",
    }


def test_configure_logging_attaches_one_json_handler(caplog) -> None:
    # A prior test, or a fixture that already imported an entry point module (each
    # of which calls ``configure_logging()`` itself), may have configured the root
    # logger before this test runs; start from a clean slate rather than assume
    # this is the first call in the process.
    root = logging.getLogger()
    before = list(root.handlers)
    was_configured = getattr(root, "_rheo_log_config_configured", False)
    try:
        if hasattr(root, "_rheo_log_config_configured"):
            delattr(root, "_rheo_log_config_configured")
        configure_logging()
        configure_logging()  # idempotent: a second call adds no second handler
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        assert isinstance(added[0].formatter, JsonLogFormatter)
    finally:
        for handler in root.handlers:
            if handler not in before:
                root.removeHandler(handler)
        if was_configured:
            root._rheo_log_config_configured = True
        elif hasattr(root, "_rheo_log_config_configured"):
            delattr(root, "_rheo_log_config_configured")


@pytest.mark.parametrize("field", sorted(ALLOWED_EXTRA_FIELDS))
def test_content_free_extra_allows_every_allowlisted_field(field: str) -> None:
    assert content_free_extra({field: "x"}) == {field: "x"}


def test_content_free_extra_rejects_an_unregistered_field_name() -> None:
    with pytest.raises(ContentFreeExtraError):
        content_free_extra({"body": "the evidence text itself"})


def test_content_free_extra_rejects_a_long_string_value() -> None:
    with pytest.raises(ContentFreeExtraError):
        content_free_extra({"error_type": "x" * 200})


def test_content_free_extra_returns_a_plain_dict_for_downstream_extra() -> None:
    fields = content_free_extra({"error_type": "ValueError", "unit_count": 3})
    assert fields == {"error_type": "ValueError", "unit_count": 3}

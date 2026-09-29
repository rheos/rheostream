"""``rheo_core.log_config``, the one logging setup every entry point shares (issue
#228).

Three things this file proves that no test proved before:

1. **A record logged with ``extra`` comes out with its fields.** Before this module
   existed the tree had no formatter that read ``extra`` at all, so
   ``evidence_acceptance_failed`` and its siblings reached stderr as a bare message
   name and every diagnostic field (``error_type``, ``evidence_id``, ...) was
   dropped. :class:`JsonLogFormatter` is what renders them; this file logs a record
   with ``extra`` and parses the formatter's own output back as JSON to check.
2. **A content-free line's fields stay content-free, and the guard never raises.**
   :func:`content_free_extra` is the sanitiser the evidence paths' five log call
   sites wrap their ``extra`` mapping in (``packages/core/src/rheo_core/evidence/
   service.py``, ``packages/core/src/rheo_core/runtime/operations.py``,
   ``modules/recallatron/src/rheo_recallatron/automatic.py``): a field name outside
   the allowlist, or a value shaped like a sentence rather than a token, is dropped
   rather than raised, because every one of those call sites logs a deliberately
   non-fatal failure (#197, #215, #220) that a raise would turn back into a failed
   run or a rolled-back drain.
3. **Every evidence call site only ever names an allowlisted field.** A static
   AST scan of the three files above, so a new field added at a call site without
   being registered in :data:`ALLOWED_EXTRA_FIELDS` fails this test instead of
   silently being dropped in production.
"""

import ast
import json
import logging
from pathlib import Path
from uuid import uuid4

import pytest
from rheo_core.log_config import (
    ALLOWED_EXTRA_FIELDS,
    JsonLogFormatter,
    configure_logging,
    content_free_extra,
)
from rheo_core.runtime import operations as runtime_operations

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EVIDENCE_LOG_CALL_SITES = (
    _REPO_ROOT / "packages" / "core" / "src" / "rheo_core" / "evidence" / "service.py",
    _REPO_ROOT
    / "packages"
    / "core"
    / "src"
    / "rheo_core"
    / "runtime"
    / "operations.py",
    _REPO_ROOT
    / "modules"
    / "recallatron"
    / "src"
    / "rheo_recallatron"
    / "automatic.py",
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


def test_content_free_extra_drops_an_unregistered_field_name_without_raising() -> None:
    # Never raises (issue #228 review): every call site logs a deliberately
    # non-fatal failure, so a validator that raised would turn a logging mistake
    # into a failed run or a rolled-back drain.
    fields = content_free_extra({"body": "the evidence text itself"})
    assert fields == {"extra_dropped": 1}


def test_content_free_extra_drops_a_long_string_value_without_raising() -> None:
    fields = content_free_extra({"error_type": "x" * 500})
    assert fields == {"extra_dropped": 1}


def test_content_free_extra_reports_every_field_dropped() -> None:
    fields = content_free_extra(
        {
            "error_type": "ValueError",
            "body": "the evidence text itself",
            "unit_count": "x" * 500,
        }
    )
    assert fields == {"error_type": "ValueError", "extra_dropped": 2}


def test_content_free_extra_returns_a_plain_dict_for_downstream_extra() -> None:
    fields = content_free_extra({"error_type": "ValueError", "unit_count": 3})
    assert fields == {"error_type": "ValueError", "unit_count": 3}


# --- every evidence call site only ever names an allowlisted field --------------


def _extra_dict_keys(call: ast.Call) -> set[str] | None:
    """The literal string keys of a ``content_free_extra({...})`` call's argument, or
    ``None`` if the argument is not a literal dict (which would defeat this scan)."""
    if not call.args or not isinstance(call.args[0], ast.Dict):
        return None
    keys: set[str] = set()
    for key in call.args[0].keys:
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            "a non-literal-string key at a content_free_extra() call site defeats "
            "this static scan"
        )
        keys.add(key.value)
    return keys


def _content_free_extra_call_field_names(path: Path) -> list[set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[set[str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "content_free_extra"
        ):
            keys = _extra_dict_keys(node)
            assert keys is not None, f"{path}: non-literal content_free_extra() call"
            calls.append(keys)
    return calls


@pytest.mark.parametrize("path", _EVIDENCE_LOG_CALL_SITES, ids=lambda p: p.name)
def test_evidence_log_call_sites_only_name_allowlisted_fields(path: Path) -> None:
    calls = _content_free_extra_call_field_names(path)
    assert calls, f"{path}: expected at least one content_free_extra() call"
    for keys in calls:
        assert keys <= ALLOWED_EXTRA_FIELDS, (
            f"{path}: {keys - ALLOWED_EXTRA_FIELDS} not in ALLOWED_EXTRA_FIELDS"
        )


# --- a disallowed field never fails the run (#197) -------------------------------


class _StubRuntimeJobPayload:
    """A stand-in for ``RuntimeJobPayload`` carrying only what
    ``_log_evidence_failure`` reads: no other attribute is touched, so a real
    payload (every field required, several database-backed) is unnecessary here."""

    def __init__(self) -> None:
        self.operation_id = uuid4()


def test_a_disallowed_field_forced_into_the_runtime_hook_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Even if ``ALLOWED_EXTRA_FIELDS`` no longer covers what
    ``_log_evidence_failure`` passes — the scan above is what keeps that from
    happening in review, not this test — the hook itself must never raise: a
    caller in ``operations.py`` treats recording as opt-in and the run must
    continue regardless (#197). Emptying ``ALLOWED_EXTRA_FIELDS`` forces every
    field the hook passes to be dropped, standing in for an unregistered field
    reaching the guard.
    """
    monkeypatch.setattr("rheo_core.log_config.ALLOWED_EXTRA_FIELDS", frozenset())
    with caplog.at_level(logging.WARNING, logger="rheo_core.evidence"):
        runtime_operations._log_evidence_failure(
            ValueError("boom"), _StubRuntimeJobPayload()
        )
    [record] = [r for r in caplog.records if r.getMessage() == "evidence_record_failed"]
    assert vars(record)["extra_dropped"] == 2
    assert "error_type" not in vars(record)
    assert "operation_id" not in vars(record)

"""The fixed codes finance's failures leave the wiring as.

Core shows what it is handed: the worker stores ``str(exc)`` of a failed job, and a
dispatch answers an ``OperationRefused`` with its ``detail``. So a finance failure
crosses into core as one of these codes and nothing else; the tick (``tick_job``) and
the read adapter (``reads``) both map through this one table.
"""

from __future__ import annotations


def failure_codes() -> tuple[tuple[type[BaseException], str], ...]:
    """Each failure class with its code, subclasses before their bases."""
    from rheo_core.storage.backend import StorageRefusal
    from sqlalchemy.exc import SQLAlchemyError

    from rheo_finance.db import LayoutRefused, WriterBusy
    from rheo_finance.jobs.tick import SchemaBehind, SchemaUnknown
    from rheo_finance.operations import OperationError
    from rheo_finance.settings import SettingsError

    return (
        (SchemaBehind, "finance_schema_behind"),
        (SchemaUnknown, "finance_schema_unknown"),
        (WriterBusy, "finance_writer_busy"),
        (SettingsError, "finance_settings_invalid"),
        (LayoutRefused, "finance_layout_invalid"),
        (OperationError, "finance_operation_refused"),
        (StorageRefusal, "finance_workspace_unavailable"),
        (SQLAlchemyError, "finance_storage_failed"),
    )


def matching_code(exc: BaseException) -> str | None:
    """The code of the first entry whose class ``exc`` is, else ``None``."""
    lineage = type(exc).__mro__
    for kind, code in failure_codes():
        if kind in lineage:
            return code
    return None

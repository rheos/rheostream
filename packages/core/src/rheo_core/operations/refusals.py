"""The registry's refusal states, the registration exception, and the handler-side
refusal.

State names are string constants, matching ``rheo_core.boundary.context``. The
dispatcher folds every refusal into an ``OperationOutcome`` whose ``state`` is one of
these (or a state a handler raised through :class:`OperationRefused`), so the API
envelope's ``error_code`` (C8, 0b2) is always a name from this vocabulary.

``HANDLER_MAY_NOT_COMMIT`` is re-exported, not declared: it is raised as a
``StorageRefusal`` by ``HandlerUnitOfWork``, so it is declared beside that class in
``rheo_core.storage.backend`` with the other storage states. It appears here because
a caller meets it as a dispatch outcome state, alongside the names in this module.
"""

from typing import Final

from rheo_core.storage.backend import HANDLER_MAY_NOT_COMMIT as HANDLER_MAY_NOT_COMMIT

OPERATION_UNKNOWN: Final = "operation_unknown"
OPERATION_NOT_PERMITTED: Final = "operation_not_permitted"
MODULE_DISABLED: Final = "module_disabled"
ROLE_NOT_PERMITTED: Final = "role_not_permitted"
INPUT_INVALID: Final = "input_invalid"
OUTPUT_INVALID: Final = "output_invalid"

SUCCEEDED: Final = "succeeded"
FAILED: Final = "failed"
HANDLER_FAILED: Final = "handler_failed"
"""The fixed ``error_code`` of a ``failed`` outcome whose handler raised."""

AUTHORIZATION_STATES: Final = frozenset(
    {OPERATION_UNKNOWN, OPERATION_NOT_PERMITTED, MODULE_DISABLED, ROLE_NOT_PERMITTED}
)
"""The four authorization refusals, in the order ``authorize`` checks them.
``authorize`` also returns ``context_required`` (from ``rheo_core.boundary``) before
any of them when handed something that is not a context."""


class RegistrationRefused(Exception):
    """A declaration the registry will not accept. Raised, never returned: a bad
    registration is a programming error found at startup, not a caller's mistake.

    ``operation_name`` names the operation; ``detail`` says why (the offending
    reserved field, the wrong prefix, the missing safety class, ...).
    """

    def __init__(self, operation_name: str, detail: str) -> None:
        super().__init__(f"{operation_name}: {detail}")
        self.operation_name = operation_name
        self.detail = detail


class OperationRefused(Exception):
    """Raised by a handler to refuse with a named state.

    The dispatcher rolls the unit of work back and returns an outcome whose
    ``state`` is ``state`` (for example ``setting_floor_violation`` from
    ``core.settings.set``, or ``not_found`` from a record lookup). ``detail`` must
    be safe to show: it becomes the API envelope's ``error_text``.
    """

    def __init__(self, state: str, detail: str | None = None) -> None:
        super().__init__(state if detail is None else f"{state}: {detail}")
        self.state = state
        self.detail = detail

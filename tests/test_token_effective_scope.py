"""``tokens.sets.effective_operations``: a stored snapshot as it may be used now.

No database. The process-wide operation registry is swapped for a copy for the length
of each test (``monkeypatch``, as ``tests/postgres/test_cli_token_modules.py``'s
``isolated_process_tables`` does), and two fabricated modules are registered into
that copy: one ``explicit_only`` and one default. The rule (issue #537): ``None`` and
``module_read:<module_id>`` keep the snapshot exactly; every other set name, the four
routine sets and any unknown one, loses every operation of a module that is
``explicit_only`` now, and nothing else.
"""

from typing import Final

import pytest
from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.operations.registry import REGISTRY, RegisteredOperation
from rheo_core.storage.backend import UnitOfWork
from rheo_core.tokens.sets import (
    DISCOVER_THEN_CALL_OPERATION,
    MODULE_READ_PREFIX,
    PACKAGE_SETS,
    effective_operations,
)

OPT_IN: Final = "optin_probe"
OPT_IN_GET: Final = f"{OPT_IN}.entry.get"
OPT_IN_ADD: Final = f"{OPT_IN}.entry.add"
PLAIN: Final = "plain_probe"
PLAIN_GET: Final = f"{PLAIN}.entry.get"
PLAIN_ADD: Final = f"{PLAIN}.entry.add"

OPT_IN_OPERATIONS: Final = frozenset({OPT_IN_GET, OPT_IN_ADD})
PLAIN_OPERATIONS: Final = frozenset({PLAIN_GET, PLAIN_ADD})
SNAPSHOT: Final = OPT_IN_OPERATIONS | PLAIN_OPERATIONS


class _ProbeInput(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _ProbeOutput(BaseModel):
    model_config = ConfigDict(frozen=True)


def _handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: _ProbeInput
) -> _ProbeOutput:
    raise AssertionError("effective_operations never dispatches")


def _registered(
    name: str, module_id: str, *, explicit_only: bool
) -> RegisteredOperation:
    return RegisteredOperation(
        declaration=OperationDeclaration(
            name=name,
            safety_class=SafetyClass.READ,
            roles=frozenset({Role.OWNER}),
            input_model=_ProbeInput,
            output=_ProbeOutput,
            idempotency=Idempotency.NONE,
            audit=None,
        ),
        handler=_handler,
        origin=module_id,
        module_id=module_id,
        explicit_only=explicit_only,
    )


@pytest.fixture(autouse=True)
def probe_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A copy of the registry holding one opt-in and one default module."""
    operations = dict(REGISTRY._operations)
    for name in OPT_IN_OPERATIONS:
        operations[name] = _registered(name, OPT_IN, explicit_only=True)
    for name in PLAIN_OPERATIONS:
        operations[name] = _registered(name, PLAIN, explicit_only=False)
    monkeypatch.setattr(REGISTRY, "_operations", operations)


ROUTINE_NAMES: Final = (*sorted(PACKAGE_SETS), "legacy_probe")
"""The four routine sets, and an unknown name, which fails closed as routine."""


def test_the_routine_names_are_the_four_package_sets() -> None:
    """The parametrisation below covers every routine name there is."""
    assert set(PACKAGE_SETS) == {
        "read_only",
        "agent_default",
        "cli_full",
        "alert_writer",
    }


@pytest.mark.parametrize("set_name", ROUTINE_NAMES)
def test_a_routine_set_loses_exactly_the_opt_in_modules_operations(
    set_name: str,
) -> None:
    assert effective_operations(set_name, SNAPSHOT) == PLAIN_OPERATIONS


@pytest.mark.parametrize("set_name", [None, f"{MODULE_READ_PREFIX}{OPT_IN}"])
def test_an_explicit_list_and_a_module_read_token_keep_the_snapshot(
    set_name: str | None,
) -> None:
    assert effective_operations(set_name, SNAPSHOT) == SNAPSHOT


@pytest.mark.parametrize("set_name", ROUTINE_NAMES)
def test_a_snapshot_of_only_opt_in_operations_narrows_to_nothing(
    set_name: str,
) -> None:
    assert effective_operations(set_name, OPT_IN_OPERATIONS) == frozenset()


@pytest.mark.parametrize("set_name", ROUTINE_NAMES)
def test_the_discover_grant_is_kept(set_name: str) -> None:
    snapshot = frozenset({DISCOVER_THEN_CALL_OPERATION, OPT_IN_GET, PLAIN_GET})
    assert effective_operations(set_name, snapshot) == {
        DISCOVER_THEN_CALL_OPERATION,
        PLAIN_GET,
    }


@pytest.mark.parametrize("set_name", ROUTINE_NAMES)
def test_switching_the_module_back_restores_the_snapshot(
    monkeypatch: pytest.MonkeyPatch, set_name: str
) -> None:
    """Computed, never stored: the opt-in module loaded as default again narrows
    nothing."""
    for name in OPT_IN_OPERATIONS:
        monkeypatch.setitem(
            REGISTRY._operations, name, _registered(name, OPT_IN, explicit_only=False)
        )
    assert effective_operations(set_name, SNAPSHOT) == SNAPSHOT

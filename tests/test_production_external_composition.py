"""Exact production EXTERNAL policy through both shipped composition roots."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic import BaseModel
from rheo_contracts import (
    AuditSpec,
    ExternalSpec,
    Idempotency,
    KeyedSpec,
    OperationDeclaration,
    Role,
    SafetyClass,
)
from rheo_core.modules import (
    DESTINATIONS,
    DestinationMode,
    DestinationRegistration,
)
from rheo_core.modules.execution import RegisteredDestination
from rheo_core.modules.production import (
    PRODUCTION_EXTERNAL_ALLOWLIST,
    ProductionExternalCompositionError,
)
from rheo_core.operations.registry import REGISTRY


class _CurrentInput(BaseModel):
    idempotency_key: str
    destination_ref: Literal["current"] = "current"


class _OtherInput(BaseModel):
    idempotency_key: str
    destination_ref: Literal["other"] = "other"


class _Output(BaseModel):
    item_ref: str
    other_ref: str = "other:item"


EXACT_SPEC = ExternalSpec(
    destination_field="destination_ref",
    destination_ref="current",
    result_ref_field="item_ref",
)


def _handler(ctx: object, uow: object, model: object) -> _Output:
    raise AssertionError("composition probes are never dispatched")


def _declaration(
    *,
    name: str = "current.item.create_external",
    safety_class: SafetyClass = SafetyClass.EXTERNAL,
    spec: ExternalSpec | None = EXACT_SPEC,
    keyed: bool = True,
) -> OperationDeclaration:
    input_model = (
        _OtherInput if spec and spec.destination_ref == "other" else _CurrentInput
    )
    keyed_spec = (
        KeyedSpec(
            key_field="idempotency_key",
            request_schema_version=1,
            result_schema_version=1,
        )
        if keyed
        else None
    )
    if spec is None:
        return OperationDeclaration.model_construct(
            name=name,
            safety_class=safety_class,
            roles=frozenset({Role.OWNER}),
            input_model=input_model,
            output=_Output,
            idempotency=Idempotency.KEYED if keyed else Idempotency.NONE,
            keyed=keyed_spec,
            external=None,
            audit=AuditSpec(subject_field=None),
        )
    return OperationDeclaration(
        name=name,
        safety_class=safety_class,
        roles=frozenset({Role.OWNER}),
        input_model=input_model,
        output=_Output,
        idempotency=Idempotency.KEYED if keyed else Idempotency.NONE,
        keyed=keyed_spec,
        external=spec,
        audit=AuditSpec(subject_field=None),
    )


def _destination(
    *,
    destination_ref: str = "current",
    operation_name: str = "current.item.create_external",
    mode: DestinationMode = DestinationMode.IN_PROCESS,
) -> DestinationRegistration:
    return DestinationRegistration(
        destination_ref=destination_ref,
        operation_name=operation_name,
        mode=mode,
        is_installed=lambda: True,
        is_enabled=lambda ctx: True,
        is_ready=lambda ctx, uow: True,
        credentials_available=(
            (lambda ctx, uow: True) if mode is DestinationMode.PROVIDER else None
        ),
        execute=lambda ctx, uow, model: _Output(item_ref="current.item:synthetic"),
        reconcile=(
            (lambda ctx, uow, request_id: None)
            if mode is DestinationMode.PROVIDER
            else None
        ),
    )


def _exact() -> None:
    REGISTRY.register(_declaration(), _handler, origin="current")
    DESTINATIONS.register(_destination(), origin="current")


def _financial() -> None:
    REGISTRY.register(
        _declaration(safety_class=SafetyClass.FINANCIAL),
        _handler,
        origin="current",
    )
    DESTINATIONS.register(_destination(), origin="current")


def _nonallowlisted() -> None:
    REGISTRY.register(
        _declaration(
            name="other.item.create_external",
            spec=ExternalSpec(
                destination_field="destination_ref",
                destination_ref="other",
                result_ref_field="item_ref",
            ),
        ),
        _handler,
        origin="other",
    )
    DESTINATIONS.register(
        _destination(
            destination_ref="other", operation_name="other.item.create_external"
        ),
        origin="other",
    )


def _missing_spec() -> None:
    REGISTRY.register(_declaration(spec=None), _handler, origin="current")


def _wrong_spec() -> None:
    spec = ExternalSpec(
        destination_field="destination_ref",
        destination_ref="current",
        result_ref_field="other_ref",
    )
    REGISTRY.register(_declaration(spec=spec), _handler, origin="current")
    DESTINATIONS.register(_destination(), origin="current")


def _wrong_destination() -> None:
    REGISTRY.register(_declaration(), _handler, origin="current")
    DESTINATIONS._destinations["other"] = RegisteredDestination(  # noqa: SLF001
        _destination(destination_ref="other"), "current"
    )


def _missing_destination() -> None:
    REGISTRY.register(_declaration(), _handler, origin="current")


def _mismatched_operation() -> None:
    REGISTRY.register(_declaration(), _handler, origin="current")
    DESTINATIONS.register(
        _destination(operation_name="current.item.other"), origin="current"
    )


def _mismatched_owner() -> None:
    REGISTRY.register(_declaration(), _handler, origin="current")
    DESTINATIONS._destinations["current"] = RegisteredDestination(  # noqa: SLF001
        _destination(), "other"
    )


def _mismatched_mode() -> None:
    REGISTRY.register(_declaration(), _handler, origin="current")
    DESTINATIONS.register(_destination(mode=DestinationMode.PROVIDER), origin="current")


def _orphan_registration() -> None:
    DESTINATIONS.register(
        _destination(operation_name="current.item.orphan"), origin="current"
    )


def _unkeyed_external() -> None:
    REGISTRY.register(_declaration(keyed=False), _handler, origin="current")
    DESTINATIONS.register(_destination(), origin="current")


def _unkeyed_financial() -> None:
    REGISTRY.register(
        _declaration(safety_class=SafetyClass.FINANCIAL, keyed=False),
        _handler,
        origin="current",
    )


SCENARIOS: dict[str, tuple[Callable[[], None], bool]] = {
    "absent_current": (lambda: None, True),
    "exact_current": (_exact, True),
    "financial": (_financial, False),
    "nonallowlisted": (_nonallowlisted, False),
    "missing_external_spec": (_missing_spec, False),
    "wrong_external_spec": (_wrong_spec, False),
    "wrong_destination": (_wrong_destination, False),
    "missing_destination": (_missing_destination, False),
    "mismatched_operation": (_mismatched_operation, False),
    "mismatched_owner": (_mismatched_owner, False),
    "mismatched_mode": (_mismatched_mode, False),
    "orphan_registration": (_orphan_registration, False),
    "unkeyed_external_inline_hold": (_unkeyed_external, False),
    "unkeyed_financial_inline_hold": (_unkeyed_financial, False),
}


def test_production_external_allowlist_is_the_one_immutable_tuple() -> None:
    assert PRODUCTION_EXTERNAL_ALLOWLIST == {
        "current.item.create_external": ("current", EXACT_SPEC)
    }
    with pytest.raises(TypeError):
        PRODUCTION_EXTERNAL_ALLOWLIST["other.item.create_external"] = (  # type: ignore[index]
            "other",
            EXACT_SPEC,
        )


class _ProductionSettings:
    def get_str(self, key: str) -> str:
        return "production" if key == "profile" else "https"


def _prepare_core_root(
    monkeypatch: pytest.MonkeyPatch, scenario: Callable[[], None], tmp_path: Path
) -> None:
    from rheo_app_core import startup

    def _load_modules(**kwargs: object) -> tuple[()]:
        scenario()
        return ()

    monkeypatch.setattr(startup, "register_module_settings", lambda: None)
    monkeypatch.setattr(startup, "resolve", lambda: _ProductionSettings())
    monkeypatch.setattr(
        startup,
        "resolve_data_root",
        lambda: SimpleNamespace(path=tmp_path, explicitly_named=True),
    )
    monkeypatch.setattr(startup, "find_checkout_root", lambda: tmp_path / "checkout")
    monkeypatch.setattr(startup, "validate_data_root", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(startup, "check_env_references", lambda settings: ())
    monkeypatch.setattr(startup, "get_backend", lambda: object())
    monkeypatch.setattr(
        startup,
        "migrate_control",
        lambda backend: SimpleNamespace(database_name="synthetic_control"),
    )
    monkeypatch.setattr(startup, "sync_providers", lambda backend, settings: None)
    monkeypatch.setattr(startup, "migrate_active_workspaces", lambda backend: ())
    monkeypatch.setattr(startup, "register_core_operations", lambda: ())
    monkeypatch.setattr(
        startup, "register_claude_cli_extraction", lambda adapters: None
    )
    monkeypatch.setattr(startup, "register_core_tools", lambda: None)
    monkeypatch.setattr(
        startup,
        "load_modules",
        _load_modules,
    )
    monkeypatch.setattr(
        startup, "check_keyed_replay_compatibility", lambda backend, registry: None
    )
    monkeypatch.setattr(startup, "check_audit_paths", lambda registry: None)


@pytest.mark.parametrize("root", ["core", "worker"])
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_production_external_composition_through_actual_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root: str, scenario: str
) -> None:
    """Both shipped roots accept only the exact tuple or a wholly absent Current."""
    builder, accepted = SCENARIOS[scenario]
    call: Callable[[], object]
    monkeypatch.setattr(REGISTRY, "_operations", {})
    monkeypatch.setattr(DESTINATIONS, "_destinations", {})

    if root == "core":
        from rheo_app_core import startup

        _prepare_core_root(monkeypatch, builder, tmp_path)
        call = startup.run_startup
    else:
        from rheo_app_worker import main as worker_main

        def _load_worker_modules(**kwargs: object) -> tuple[()]:
            builder()
            return ()

        monkeypatch.setattr(
            worker_main,
            "load_modules",
            _load_worker_modules,
        )
        call = worker_main.register_modules

    if accepted:
        call()
    else:
        with pytest.raises(ProductionExternalCompositionError):
            call()


def test_core_keyed_replay_compatibility_fails_before_startup_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from rheo_app_core import startup

    reached_after: list[str] = []
    monkeypatch.setattr(REGISTRY, "_operations", {})
    monkeypatch.setattr(DESTINATIONS, "_destinations", {})
    _prepare_core_root(monkeypatch, lambda: None, tmp_path)
    monkeypatch.setattr(
        startup,
        "check_keyed_replay_compatibility",
        lambda backend, registry: (_ for _ in ()).throw(RuntimeError("incompatible")),
    )
    monkeypatch.setattr(
        startup, "check_audit_paths", lambda registry: reached_after.append("audit")
    )

    with pytest.raises(RuntimeError, match="incompatible"):
        startup.run_startup()
    assert reached_after == []


def test_worker_keyed_replay_compatibility_fails_before_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rheo_app_worker import main as worker_main

    calls: list[str] = []
    monkeypatch.setattr(worker_main, "configure_logging", lambda: None)
    monkeypatch.setattr(worker_main, "register_module_settings", lambda: None)
    monkeypatch.setattr(worker_main, "refuse_misconfigured_login", lambda: None)
    monkeypatch.setattr(
        worker_main, "register_claude_cli_extraction", lambda adapters: None
    )
    monkeypatch.setattr(worker_main, "register_oneshot_adapters", lambda adapters: None)
    monkeypatch.setattr(worker_main, "register_modules", lambda: None)
    monkeypatch.setattr(worker_main, "get_backend", lambda: object())
    monkeypatch.setattr(
        worker_main,
        "check_keyed_replay_compatibility",
        lambda backend, registry: (_ for _ in ()).throw(RuntimeError("incompatible")),
    )
    monkeypatch.setattr(
        worker_main, "worker_loop", lambda **kwargs: calls.append("leased")
    )

    with pytest.raises(RuntimeError, match="incompatible"):
        worker_main.main()
    assert calls == []

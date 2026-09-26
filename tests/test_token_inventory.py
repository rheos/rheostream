"""Issue #117's refusal path: ``rheo token issue`` when the loader refuses the set.

``load_operation_inventory()`` runs ``register_core_tools()`` and ``load_modules()``
before the token is issued. A ``ManifestInvalid`` from the loader becomes the one
``module_invalid: <detail>`` line the command prints before exiting ``1``, the same
state the early settings registration prints for a module it cannot load. The
success path is ``tests/postgres/test_cli_token_modules.py``'s.
"""

import pytest
from rheo_app_cli.commands import token as token_command
from rheo_core.modules import ManifestInvalid


def test_a_loader_refusal_becomes_a_module_invalid_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def refuse() -> tuple[str, ...]:
        calls.append("load_modules")
        raise ManifestInvalid(
            "broken_probe", "declares job kind 'core.retention_sweep'"
        )

    monkeypatch.setattr(
        token_command, "register_core_tools", lambda: calls.append("tools")
    )
    monkeypatch.setattr(token_command, "load_modules", refuse)

    assert token_command.load_operation_inventory() == (
        "module_invalid: broken_probe: declares job kind 'core.retention_sweep'"
    )
    assert calls == ["tools", "load_modules"]


def test_a_clean_load_returns_nothing_to_print(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        token_command, "register_core_tools", lambda: calls.append("tools")
    )
    monkeypatch.setattr(
        token_command, "load_modules", lambda: calls.append("load_modules") or ()
    )

    assert token_command.load_operation_inventory() is None
    assert calls == ["tools", "load_modules"]

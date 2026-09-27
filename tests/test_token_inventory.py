"""Issue #117's refusal path: ``rheo token issue`` when the loader refuses the set.

``load_operation_inventory()`` runs ``register_core_tools()`` and ``load_modules()``
before the token is issued. A ``ManifestInvalid`` from the loader becomes the one
``module_invalid: <detail>`` line the command prints before exiting ``1``, the same
state the early settings registration prints for a module it cannot load. The
success path is ``tests/postgres/test_cli_token_modules.py``'s.
"""

import argparse
from uuid import uuid4

import pytest
from rheo_app_cli.commands import token as token_command
from rheo_core.modules import ManifestInvalid
from rheo_core.operations.refusals import RegistrationRefused


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


def test_a_registry_refusal_becomes_a_module_invalid_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``load_modules`` can also raise a shipped registry's ``RegistrationRefused``
    part-way through; it must print and exit ``1``, never escape as a traceback."""

    def refuse() -> tuple[str, ...]:
        raise RegistrationRefused(
            "broken_probe_approve", "names non-token-issuable operation"
        )

    monkeypatch.setattr(token_command, "register_core_tools", lambda: None)
    monkeypatch.setattr(token_command, "load_modules", refuse)

    assert token_command.load_operation_inventory() == (
        "module_invalid: broken_probe_approve: names non-token-issuable operation"
    )


def test_the_command_exits_one_on_a_registry_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through ``issue_token`` itself: the refusal line on stderr, exit ``1``, and no
    dispatch. ``bootstrap`` is stubbed so no database is needed."""

    def refuse() -> tuple[str, ...]:
        raise RegistrationRefused("broken_probe_tool", "already registered")

    dispatched: list[object] = []
    monkeypatch.setattr(token_command, "bootstrap", lambda: None)
    monkeypatch.setattr(token_command, "register_core_tools", lambda: None)
    monkeypatch.setattr(token_command, "load_modules", refuse)
    monkeypatch.setattr(
        token_command, "dispatch", lambda *args: dispatched.append(args)
    )
    args = argparse.Namespace(
        account=uuid4(), workspace=uuid4(), set_name="read_only", kind="cli"
    )

    assert token_command.issue_token(args) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert err == "module_invalid: broken_probe_tool: already registered\n"
    assert dispatched == []


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

"""The ``finance`` entry point: help, usage errors, and its import boundary."""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import NoReturn

import pytest

from rheo_finance.cli.main import HEADLINE, main


def test_help_returns_zero_and_leads_with_headline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("usage: finance")
    assert HEADLINE in out
    assert chr(0x2014) not in out  # no em dash in output


def test_unknown_subcommand_exits_with_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["no-such-command"]) == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err


def test_exit_with_no_code_counts_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exit_without_code(*_args: object, **_kwargs: object) -> NoReturn:
        raise SystemExit(None)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", exit_without_code)
    assert main([]) == 0


def test_the_cli_entry_module_exit_codes() -> None:
    # No console script is installed yet (it arrives with the module's wiring), so the
    # entry module runs as one would: a wrong ``main`` fails here.
    entry = [sys.executable, "-m", "rheo_finance.cli.main"]

    ok = subprocess.run(
        [*entry, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0
    assert HEADLINE in ok.stdout

    bad = subprocess.run(
        [*entry, "no-such-command"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 2


def test_cli_main_does_not_load_the_database_layer() -> None:
    probe = (
        "import sys, rheo_finance.cli.main\n"
        "loaded = [m for m in sys.modules"
        " if m == 'sqlalchemy' or m.startswith('sqlalchemy.')"
        " or m == 'rheo_finance.storage' or m.startswith('rheo_finance.storage.')]\n"
        "print(len(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "0"

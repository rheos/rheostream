"""The two server handlers, ``finance remote receive`` and ``send-packs``, through
``main``: a bound layout takes a stream or gives its packs, and each layout refusal
reaches the laptop as ``NO <code> 0`` or ``X <code>``, a code the laptop reads back.

No database, no network: the layout is bound by a stand-in, or the real wiring
refuses before it reaches core.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rheo_core.boundary.context import Refusal

from rheo_finance import composition, db
from rheo_finance.cli.main import main
from rheo_finance.remote import ssh
from rheo_finance.remote.frames import MAGIC, FrameReader, RemoteRefused
from rheo_finance.remote.receive import receive
from rheo_finance.wiring import core

from .support import WORKSPACE, frame, make_layout


def _stdin(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))


def _bind(monkeypatch: pytest.MonkeyPatch, layout: db.FinanceLayout) -> None:
    monkeypatch.setattr(composition, "bind_workspace_layout", lambda: layout)


def test_receive_lands_a_stream_in_the_bound_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = make_layout(tmp_path / "uploads")
    (layout.data / "settings.toml").write_bytes(b"# template\n")
    _bind(monkeypatch, layout)
    _stdin(monkeypatch, frame([("archive/a.pdf", b"synthetic")]))
    assert main(["remote", "receive"]) == 0
    assert capsys.readouterr().out == "OK 1 0 0\n"
    assert (layout.archive / "a.pdf").read_bytes() == b"synthetic"

    _stdin(monkeypatch, frame([("data/settings.toml", b"# owner\n")]))
    assert main(["remote", "receive"]) == 1
    assert capsys.readouterr().out == "NO target_differs 1\n"
    _stdin(monkeypatch, frame([("data/settings.toml", b"# owner\n")]))
    assert main(["remote", "receive", "--replace"]) == 0
    assert capsys.readouterr().out == "OK 0 0 1\n"
    assert (layout.data / "settings.toml").read_bytes() == b"# owner\n"


def test_send_packs_streams_the_bound_layouts_packs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    layout = make_layout(tmp_path / "uploads")
    (layout.data / "packs" / "month").mkdir(parents=True)
    (layout.data / "packs" / "month" / "p.zip").write_bytes(b"synthetic pack")
    _bind(monkeypatch, layout)
    assert main(["remote", "send-packs"]) == 0
    stream = capsysbinary.readouterr().out
    into = tmp_path / "into"
    into.mkdir()
    counts = receive(io.BytesIO(stream), {"packs": into}, into, direction="pull")
    assert (counts.new, counts.identical) == (1, 0)
    assert (into / "month" / "p.zip").read_bytes() == b"synthetic pack"


def _refuse(code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the server's layout binding refuse with ``code``: through the real
    ``wiring.core`` where it refuses before core, else with a stand-in for core."""
    monkeypatch.setenv(core.WORKSPACE_VARIABLE, WORKSPACE)
    if code == "finance_workspace_not_set":
        monkeypatch.delenv(core.WORKSPACE_VARIABLE)
    elif code == "finance_workspace_unavailable":
        monkeypatch.setattr(
            core, "context_for_operator", lambda _id: Refusal(state="synthetic")
        )
    elif code == "finance_module_not_enabled":
        monkeypatch.setattr(
            core,
            "context_for_operator",
            lambda _id: SimpleNamespace(enabled_modules=frozenset()),
        )
    else:
        _bind(monkeypatch, db.layout_under(tmp_path / "absent"))


CODES = (
    "finance_workspace_not_set",
    "finance_workspace_unavailable",
    "finance_module_not_enabled",
    "finance_layout_missing",
)


@pytest.mark.parametrize("code", CODES)
def test_a_layout_refusal_is_a_reply_code_on_receive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: str,
) -> None:
    _refuse(code, tmp_path, monkeypatch)
    _stdin(monkeypatch, frame([("archive/a.pdf", b"synthetic")]))
    assert main(["remote", "receive"]) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (f"NO {code} 0\n", "")
    with pytest.raises(RemoteRefused) as caught:
        ssh.parse_reply(captured.out.encode(), 1)
    assert (caught.value.code, caught.value.files) == (code, 0)


@pytest.mark.parametrize("code", CODES)
def test_a_layout_refusal_is_an_abort_frame_on_send_packs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
    code: str,
) -> None:
    _refuse(code, tmp_path, monkeypatch)
    assert main(["remote", "send-packs"]) == 1
    captured = capsysbinary.readouterr()
    assert captured.out == MAGIC + b"\nX " + code.encode() + b"\n"
    assert captured.err == b""
    reader = FrameReader(io.BytesIO(captured.out), "pull")
    reader.start()
    with pytest.raises(RemoteRefused) as caught:
        reader.next_file()
    assert caught.value.code == code

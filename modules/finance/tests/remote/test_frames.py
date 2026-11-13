"""The stream grammar (L1, L2): the path rule, and every malformed stream refused with
its code before any area is touched.

No database, no network: streams are bytes in memory.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path, PurePosixPath

import pytest

from rheo_finance.remote import frames
from rheo_finance.remote.frames import MAGIC, RemoteRefused, check_path
from rheo_finance.remote.receive import receive

from .support import frame, make_layout, push_areas, staging_entries, tree_state

REFUSED_PUSH = [
    "/abs",
    "archive//b",
    "archive/../x",
    "archive/./x",
    "archive/.h",
    "archive/",
    "archive",
    "data/db.env",
    "data/dumps/x.sql",
    "data/logs/x.log",
    "data/packs/x.zip",
    "data/transfer",
    "data/manual",
    "keys/a/b",
    "packs/x.zip",
    "other/x.pdf",
    "archive/a\x00b",
    "archive/a\nb",
    "archive/a\rb",
    "archive/a\tb",
    "archive/a\x7fb",
    "archive/a\x85b",
    "archive/a b",
    "archive/a b",
    "archive/a\\b",
    "archive/" + "x" * 256,
    "archive/" + "/".join(["d" * 200] * 6),
    "archive/a\udcffb",
]
ACCEPTED_PUSH = [
    "archive/-oProxyCommand=x.pdf",
    "inbox/; rm -rf ~.pdf",
    "archive/$(x).pdf",
    "archive/a b/c d.pdf",
    "archive/" + "x" * 255,
    "data/settings.toml",
    "data/identities.toml",
    "data/manual/sub/x.csv",
    "data/transfer/state/decision.jsonl",
    "keys/statement.key",
]


@pytest.mark.parametrize("text", REFUSED_PUSH)
def test_a_refused_name_is_name_refused(text: str) -> None:
    with pytest.raises(RemoteRefused) as caught:
        check_path(text, "push")
    assert str(caught.value) == "name_refused"


@pytest.mark.parametrize("text", ACCEPTED_PUSH)
def test_an_accepted_name_keeps_its_exact_form(text: str) -> None:
    area, rest = check_path(text, "push")
    assert f"{area}/{rest}" == text


def test_pull_takes_packs_alone() -> None:
    assert check_path("packs/folder/a.zip", "pull") == (
        "packs",
        PurePosixPath("folder/a.zip"),
    )
    for text in ("archive/a.pdf", "data/settings.toml", "keys/k"):
        with pytest.raises(RemoteRefused, match="name_refused"):
            check_path(text, "pull")


def _body(size: int) -> bytes:
    return bytes(range(256)) * (size // 256) + b"x" * (size % 256)


GOOD = [("archive/a.pdf", _body(1000)), ("inbox/b.pdf", _body(70000))]
_H = hashlib.sha256(GOOD[0][1]).hexdigest().encode()

BROKEN: dict[str, tuple[bytes, str]] = {
    "magic": (b"finance-remote/2\n" + frame(GOOD)[len(MAGIC) + 1 :], "frame_invalid"),
    "overlong line": (
        MAGIC + b"\nF 1 archive/" + b"a" * frames.LINE_MAX + b"\n",
        "frame_invalid",
    ),
    "unknown record": (MAGIC + b"\nZ 1\n", "frame_invalid"),
    "duplicate path": (
        frame(GOOD[:1] + GOOD[:1], end=False) + b"E 2\n",
        "frame_invalid",
    ),
    "byte after the end": (frame(GOOD) + b"x", "frame_invalid"),
    "wrong count": (frame(GOOD, end=False) + b"E 3\n", "frame_invalid"),
    "size with a leading zero": (MAGIC + b"\nF 01 archive/a.pdf\n", "frame_invalid"),
    "size over the limit": (
        MAGIC + b"\nF %d archive/a.pdf\n" % (frames.MAX_FILE_BYTES + 1),
        "frame_invalid",
    ),
    "unknown abort": (MAGIC + b"\nX some_host_name\n", "frame_invalid"),
    "abort": (MAGIC + b"\nX source_changed\n", "source_changed"),
    "a refused name": (frame([("data/db.env", b"x")]), "name_refused"),
    "end of stream mid-payload": (
        MAGIC + b"\nF 1000 archive/a.pdf\n" + GOOD[0][1][:300],
        "stream_truncated",
    ),
    "end of stream before the hash": (
        MAGIC + b"\nF 1000 archive/a.pdf\n" + GOOD[0][1],
        "stream_truncated",
    ),
    "end of stream before the end record": (frame(GOOD, end=False), "stream_truncated"),
    "empty": (b"", "stream_truncated"),
    "wrong hash": (
        frame(GOOD[:1], end=False).replace(_H, b"0" * 64) + b"E 1\n",
        "hash_mismatch",
    ),
}


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_a_broken_stream_is_refused_and_touches_no_area(
    tmp_path: Path, name: str
) -> None:
    data, code = BROKEN[name]
    layout = make_layout(tmp_path / "uploads")
    (layout.archive / "kept.pdf").write_bytes(b"kept")
    before = tree_state(layout.root)
    with pytest.raises(RemoteRefused) as caught:
        receive(io.BytesIO(data), push_areas(layout), layout.root, direction="push")
    assert str(caught.value) == code
    after = [row for row in tree_state(layout.root) if not row[0].startswith(".")]
    assert after == [row for row in before if not row[0].startswith(".")]
    assert staging_entries(layout.root) == ["lock"]


def test_a_well_formed_stream_reads_back(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / "uploads")
    counts = receive(
        io.BytesIO(frame(GOOD)), push_areas(layout), layout.root, direction="push"
    )
    assert (counts.new, counts.identical, counts.replaced) == (2, 0, 0)
    assert (layout.inbox / "b.pdf").read_bytes() == GOOD[1][1]
    assert staging_entries(layout.root) == ["lock"]

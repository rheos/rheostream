"""The push and pull stream: line frames, the path rule and the fixed codes.

A stream is ASCII record lines, each ending in a newline and at most ``LINE_MAX``
bytes, with raw file bytes between them::

    finance-remote/1         the magic, first line
    F <size> <path>          <size> bytes follow; <path> is the rest of the line
    <size raw bytes>
    H <sha256>               the hash of the bytes just sent, 64 lower-case hex digits
    E <count>                the end; <count> equals the F records; then end of stream
    X <code>                 the sender gave up between files

File names travel here, in-band, and never on a command line. ``check_path`` is the
one rule for a name, used by the sender and the receiver alike: a name it refuses
cannot be sent and is refused if received.

Every refusal is a ``RemoteRefused`` whose message is a fixed code; ``files`` carries
the one count that goes with it. No message here holds a path, a host or file bytes.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Literal, Protocol

Direction = Literal["push", "pull"]

MAGIC: Final = b"finance-remote/1"
LINE_MAX: Final = 4096
CHUNK: Final = 65536
MAX_FILE_BYTES: Final = 2**31
MAX_FILES: Final = 100_000
PATH_MAX_BYTES: Final = 1024
COMPONENT_MAX_BYTES: Final = 255

# The four codes a server refuses with before it reads a byte (``db.check_layout`` and
# ``wiring.core``); a push reply and a pull abort may carry them.
LAYOUT_CODES: Final = frozenset(
    {
        "finance_layout_missing",
        "finance_workspace_not_set",
        "finance_workspace_unavailable",
        "finance_module_not_enabled",
    }
)
RECEIVER_CODES: Final = frozenset(
    {
        "frame_invalid",
        "stream_truncated",
        "hash_mismatch",
        "name_refused",
        "staging_refused",
        "staging_busy",
        "staging_cross_device",
        "target_outside_layout",
        "target_not_regular",
        "target_differs",
        "target_appeared",
        "io_failed",
    }
)
# What a push reply may carry: ``OK <new> <identical> <replaced>`` or ``NO <code> <n>``.
REPLY_CODES: Final = RECEIVER_CODES | LAYOUT_CODES
# What a sender may abort with (``X <code>``); anything else is ``frame_invalid``.
ABORT_CODES: Final = LAYOUT_CODES | frozenset(
    {
        "packs_missing",
        "source_missing",
        "source_changed",
        "source_unreadable",
        "source_not_regular",
        "name_refused",
        "io_failed",
    }
)

_F_LINE: Final = re.compile(rb"F (0|[1-9][0-9]{0,10}) (.+)", re.DOTALL)
_H_LINE: Final = re.compile(rb"H ([0-9a-f]{64})")
_E_LINE: Final = re.compile(rb"E (0|[1-9][0-9]{0,5})")
_X_LINE: Final = re.compile(rb"X ([a-z_]{1,48})")
_PARAGRAPH_BREAKS: Final = frozenset({chr(0x2028), chr(0x2029)})


class RemoteRefused(Exception):
    """A push, pull or exec cannot go on. The message is a fixed code; ``files`` is the
    count that goes with it (the differing targets, or the files already committed)."""

    def __init__(self, code: str, files: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.files = files


class Readable(Protocol):
    def read(self, size: int, /) -> bytes: ...


class Writable(Protocol):
    def write(self, data: bytes, /) -> object: ...

    def flush(self) -> object: ...


# --- names ----------------------------------------------------------------------------

# The shapes each direction may name, by area. ``data`` takes the owner's inputs and
# the transfer directory, never ``db.env``, ``dumps/`` or ``logs/``.
_DATA_FILES: Final = frozenset({"settings.toml", "identities.toml"})
_DATA_TREES: Final = frozenset({"manual", "transfer"})


def check_path(text: str, direction: Direction) -> tuple[str, PurePosixPath]:
    """``(area, rest)`` for a name this direction may carry, else ``name_refused``.

    UTF-8 only (a surrogate-escaped name is refused), at most 1024 bytes, each
    component 1 to 255 bytes; no control character, no line or paragraph separator,
    no backslash; no empty component and none that starts with a dot. The bytes are
    kept as they are: no normalization.
    """
    refused = RemoteRefused("name_refused")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise refused from None
    if not encoded or len(encoded) > PATH_MAX_BYTES:
        raise refused
    for char in text:
        if (
            char == "\\"
            or char in _PARAGRAPH_BREAKS
            or unicodedata.category(char) == "Cc"
        ):
            raise refused
    parts = text.split("/")
    for part in parts:
        if not part or part.startswith(".") or len(part.encode()) > COMPONENT_MAX_BYTES:
            raise refused
    area, rest = parts[0], parts[1:]
    if not rest or not _allowed(direction, area, rest):
        raise refused
    return area, PurePosixPath(*rest)


def _allowed(direction: Direction, area: str, rest: list[str]) -> bool:
    if direction == "pull":
        return area == "packs"
    if area in ("archive", "inbox"):
        return True
    if area == "keys":
        return len(rest) == 1
    if area == "data":
        if len(rest) == 1:
            return rest[0] in _DATA_FILES
        return rest[0] in _DATA_TREES
    return False


# --- reading --------------------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    """One ``F`` record: the name as sent, its area and rest, and its size."""

    path: str
    area: str
    rest: PurePosixPath
    size: int


class FrameReader:
    """Reads a stream with ``raw.read(n)`` alone, so a test can wrap that one method.

    ``start`` reads the magic; ``next_file`` the next ``F`` record (or ``None`` at a
    valid end); ``chunks`` that file's bytes; ``expect_hash`` its ``H`` record.
    """

    def __init__(self, raw: Readable, direction: Direction) -> None:
        self._raw = raw
        self._direction: Direction = direction
        self._buffer = bytearray()
        self._eof = False
        self._seen: set[str] = set()
        self._pending: int | None = None

    def _fill(self) -> bool:
        if self._eof:
            return False
        chunk = self._raw.read(CHUNK)
        if not chunk:
            self._eof = True
            return False
        self._buffer += chunk
        return True

    def _line(self) -> bytes:
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                if end >= LINE_MAX:
                    raise RemoteRefused("frame_invalid")
                line = bytes(self._buffer[:end])
                del self._buffer[: end + 1]
                return line
            if len(self._buffer) >= LINE_MAX:
                raise RemoteRefused("frame_invalid")
            if not self._fill():
                raise RemoteRefused("stream_truncated")

    def start(self) -> None:
        if self._line() != MAGIC:
            raise RemoteRefused("frame_invalid")

    def next_file(self) -> Header | None:
        line = self._line()
        if found := _F_LINE.fullmatch(line):
            size = int(found[1])
            if size > MAX_FILE_BYTES or len(self._seen) >= MAX_FILES:
                raise RemoteRefused("frame_invalid")
            path = found[2].decode("utf-8", errors="surrogateescape")
            area, rest = check_path(path, self._direction)
            if path in self._seen:
                raise RemoteRefused("frame_invalid")
            self._seen.add(path)
            self._pending = size
            return Header(path=path, area=area, rest=rest, size=size)
        if found := _E_LINE.fullmatch(line):
            if int(found[1]) != len(self._seen):
                raise RemoteRefused("frame_invalid")
            if self._buffer or self._fill():
                raise RemoteRefused("frame_invalid")
            return None
        if (found := _X_LINE.fullmatch(line)) and found[1].decode() in ABORT_CODES:
            raise RemoteRefused(found[1].decode())
        raise RemoteRefused("frame_invalid")

    def chunks(self) -> Iterator[bytes]:
        """The pending file's bytes as they arrive (``stream_truncated`` if cut)."""
        left = self._pending
        if left is None:
            raise RemoteRefused("frame_invalid")
        while left:
            if not self._buffer and not self._fill():
                raise RemoteRefused("stream_truncated")
            piece = bytes(self._buffer[:left])
            del self._buffer[: len(piece)]
            left -= len(piece)
            yield piece

    def expect_hash(self, digest: str) -> None:
        found = _H_LINE.fullmatch(self._line())
        if found is None:
            raise RemoteRefused("frame_invalid")
        self._pending = None
        if found[1].decode() != digest:
            raise RemoteRefused("hash_mismatch")


# --- writing --------------------------------------------------------------------------


def write_magic(out: Writable) -> None:
    out.write(MAGIC + b"\n")


def write_header(out: Writable, size: int, path: str) -> None:
    out.write(b"F %d " % size + path.encode("utf-8") + b"\n")


def write_hash(out: Writable, digest: str) -> None:
    out.write(b"H " + digest.encode("ascii") + b"\n")


def write_end(out: Writable, count: int) -> None:
    out.write(b"E %d\n" % count)


def write_abort(out: Writable, code: str) -> None:
    out.write(b"X " + code.encode("ascii") + b"\n")

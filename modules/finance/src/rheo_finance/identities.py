"""Institution wording, kept out of the public source.

The code names every institution by a neutral codename (``alder``, ``olive``,
``sumac`` and so on): parser ids, issuer directories, institution keys and sink names
are those codenames, and the module stores nothing else. Where code has to match an
institution's own wording in a document (a bill's title, a CSV column header, a
transaction description, an API host), it builds the pattern through ``real``, which
replaces each codename form with the wording the owner's private identities file
gives.

That file is ``$FINANCE_DATA_DIR/identities.toml``, mode 0600, never committed:

    [forms]
    "Codename" = "the institution's own wording"

    [layout]
    "parser.part" = "a pattern, or a list of column names in the file's order"

``[forms]`` holds wording; ``[layout]`` holds structure: the order a CSV export prints
its columns in, an id scheme, a line's shape. ``layout`` returns the owner's value for
a key, or the parser's own default, which describes the invented layout the synthetic
fixtures use.

Keys are matched whole (never inside a longer word), longest first, case as written.
Without the file every form stands for itself. The tests' synthetic documents are
written in the codenames, so they parse either way; an owner document is recognised only
where the owner's file names its wording.
"""

from __future__ import annotations

import functools
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from rheo_finance.db import data_dir

FILE_NAME: Final = "identities.toml"


class IdentitiesError(Exception):
    """The identities file exists but cannot be used. The message names no form."""


def identities_path() -> Path:
    return data_dir() / FILE_NAME


@functools.cache
def _load(
    path: str, _mtime_ns: int
) -> tuple[Mapping[str, str], re.Pattern[str] | None]:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise IdentitiesError(f"{FILE_NAME} cannot be read") from None
    table = data.get("forms", {})
    if not isinstance(table, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) for k, v in table.items()
    ):
        raise IdentitiesError(f"{FILE_NAME}: [forms] must map strings to strings")
    forms = MappingProxyType(dict(table))
    if not forms:
        return forms, None
    keys = sorted(forms, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![A-Za-z0-9])("
        + "|".join(re.escape(k) for k in keys)
        + r")(?![A-Za-z0-9])"
    )
    return forms, pattern


def forms() -> Mapping[str, str]:
    """The owner's codename-to-wording forms; empty when there is no file."""
    path = identities_path()
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return MappingProxyType({})
    return _load(str(path), mtime)[0]


def real(text: str) -> str:
    """``text`` with each codename form replaced by the owner's wording for it."""
    path = identities_path()
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return text
    table, pattern = _load(str(path), mtime)
    if pattern is None:
        return text
    return pattern.sub(lambda m: table[m.group(1)], text)


def real_re(pattern: str) -> str:
    """``pattern``, regular-expression source, with each codename form replaced by the
    owner's wording escaped for a pattern."""
    path = identities_path()
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return pattern
    table, compiled = _load(str(path), mtime)
    if compiled is None:
        return pattern
    return compiled.sub(lambda m: re.escape(table[m.group(1)]), pattern)


@functools.cache
def _load_layout(path: str, _mtime_ns: int) -> Mapping[str, str | tuple[str, ...]]:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise IdentitiesError(f"{FILE_NAME} cannot be read") from None
    table = data.get("layout", {})
    if not isinstance(table, dict):
        raise IdentitiesError(f"{FILE_NAME}: [layout] must be a table")
    found: dict[str, str | tuple[str, ...]] = {}
    for key, value in table.items():
        if isinstance(value, str):
            found[key] = value
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            found[key] = tuple(value)
        else:
            raise IdentitiesError(
                f"{FILE_NAME}: [layout] values are strings or lists of strings"
            )
    return MappingProxyType(found)


def layout[T: (str, tuple[str, ...])](key: str, default: T) -> T:
    """The owner's ``[layout]`` value for ``key``, or ``default`` when there is no
    file or no such key. A value of the other kind is an ``IdentitiesError``."""
    path = identities_path()
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return default
    value = _load_layout(str(path), mtime).get(key)
    if value is None:
        return default
    if type(value) is not type(default):
        raise IdentitiesError(f"{FILE_NAME}: [layout] {key} has the wrong kind")
    return value

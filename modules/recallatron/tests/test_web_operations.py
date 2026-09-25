"""Every operation the memory screens call is a registered read-class operation.

The web package spells operation names in exactly one file, ``web/src/operations.ts``;
its own vitest suite holds that no other file in the package names one, and that the
file names only the read-only allowlist. This is the Python half: each name that file
exports is registered by this module with ``SafetyClass.READ``. A screen that started
calling a write, however it was spelled on the TypeScript side, would have to add its
name there first, and this test refuses it.

A separate small file rather than an addition to the Postgres browse suite, because
nothing here needs a database: it reads the declarations the manifest carries.
"""

import re
from pathlib import Path

from rheo_contracts import SafetyClass
from rheo_recallatron.operations import OPERATIONS

_OPERATIONS_TS = Path(__file__).resolve().parents[1] / "web" / "src" / "operations.ts"
_EXPORT = re.compile(r'^export const [A-Z_]+ = "([^"]+)";$', re.MULTILINE)

_READ_ONLY_ALLOWLIST = frozenset(
    {
        "recallatron.memory.recall",
        "recallatron.memory.read",
        "recallatron.memory.get",
        "recallatron.entity.list",
        "recallatron.entity.get",
        "recallatron.memory.dedup_candidates",
        "recallatron.embedding.coverage",
    }
)


def _web_operation_names() -> list[str]:
    return _EXPORT.findall(_OPERATIONS_TS.read_text(encoding="utf-8"))


def test_the_web_package_names_exactly_the_read_only_allowlist() -> None:
    names = _web_operation_names()
    assert len(names) == len(set(names))
    assert set(names) == _READ_ONLY_ALLOWLIST


def test_every_web_operation_is_a_registered_read() -> None:
    declared = {declaration.name: declaration for declaration, _handler in OPERATIONS}
    for name in _web_operation_names():
        assert name in declared, f"{name} is not a registered operation"
        assert declared[name].safety_class is SafetyClass.READ, (
            f"{name} is {declared[name].safety_class}, not a read"
        )

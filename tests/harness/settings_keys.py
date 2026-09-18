"""Six harness settings keys, registered under ``profile = test`` with origin
``test_harness``.

Four floored keys, one per comparator, because ``union`` and ``and`` have
no production key in this run (``subset`` now has ``runtime.allowed_runtimes`` and
``runtime.allowed_models``) and the floor engine must not be "tested later"; one
``explicit_per_workspace`` key, which C3's provisioning step 4 writes as a row from its
package default and is tested against; and one ``member``-scope key, because every
other key in this run is workspace- or deployment-scope and the member-row path of the
resolver and the write path needs one. Every key carries a default: that default is
literally what provisioning writes.

Registration is explicit (:func:`register_harness_keys`), not an import side effect, so
a module may import the key names without ``RHEO_PROFILE=test`` being set at import
time, and no test module mutates the process-global registry on its own. Registration
is idempotent, so calling it once per test is fine.
"""

from rheo_core.settings import (
    TEST_HARNESS_ORIGIN,
    Floor,
    KeySpec,
    Scope,
    ValueType,
    register,
)

HARNESS_FLOOR_MIN = "harness.floor_min"
HARNESS_FLOOR_UNION = "harness.floor_union"
HARNESS_FLOOR_SUBSET = "harness.floor_subset"
HARNESS_FLOOR_AND = "harness.floor_and"
HARNESS_EXPLICIT = "harness.explicit_per_workspace"
HARNESS_MEMBER = "harness.member_preference"

HARNESS_KEYS: tuple[KeySpec, ...] = (
    KeySpec(
        key=HARNESS_FLOOR_MIN,
        type=ValueType.INT,
        scope=Scope.WORKSPACE,
        floor=Floor.MIN,
        explicit_per_workspace=False,
        default=100,
    ),
    KeySpec(
        key=HARNESS_FLOOR_UNION,
        type=ValueType.STR_LIST,
        scope=Scope.WORKSPACE,
        floor=Floor.UNION,
        explicit_per_workspace=False,
        default=("harness.confirm",),
    ),
    KeySpec(
        key=HARNESS_FLOOR_SUBSET,
        type=ValueType.STR_LIST,
        scope=Scope.WORKSPACE,
        floor=Floor.SUBSET,
        explicit_per_workspace=False,
        default=("read", "draft", "mutate"),
    ),
    KeySpec(
        key=HARNESS_FLOOR_AND,
        type=ValueType.BOOL,
        scope=Scope.WORKSPACE,
        floor=Floor.AND,
        explicit_per_workspace=False,
        default=False,
    ),
    KeySpec(
        key=HARNESS_EXPLICIT,
        type=ValueType.STR,
        scope=Scope.WORKSPACE,
        floor=None,
        explicit_per_workspace=True,
        default="harness-package-default",
    ),
    KeySpec(
        key=HARNESS_MEMBER,
        type=ValueType.STR,
        scope=Scope.MEMBER,
        floor=None,
        explicit_per_workspace=False,
        default="member-default",
    ),
)


def register_harness_keys() -> None:
    """Register the six keys (idempotent). The resolved profile must be ``test``."""
    for spec in HARNESS_KEYS:
        register(spec, origin=TEST_HARNESS_ORIGIN)

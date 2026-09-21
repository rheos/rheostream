"""Eight harness settings keys, registered under ``profile = test`` with origin
``test_harness``.

Four floored keys, one per comparator, because ``union`` and ``and`` have
no production key in this run (``subset`` now has ``runtime.allowed_runtimes`` and
``runtime.allowed_models``) and the floor engine must not be "tested later"; one
``explicit_per_workspace`` key, which C3's provisioning step 4 writes as a row from its
package default and is tested against; one ``member``-scope key, because every
other key in this run is workspace- or deployment-scope and the member-row path of the
resolver and the write path needs one; one **bounded** key, because 1a1 added
``minimum``/``maximum`` to ``KeySpec`` and the first production key to carry them
(``recallatron.retention.days``) is a later prompt's; and one **gate** key beside it,
because the core's verified-expiry entry rechecks a gate and a window together and
core declares neither of them itself. Every key carries a default: that default is
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
HARNESS_RETENTION_DAYS = "harness.retention_days"
HARNESS_RETENTION_MINIMUM = 1
HARNESS_RETENTION_MAXIMUM = 10
HARNESS_RETENTION_DEFAULT = 7
"""The bounded key, and the shape ``recallatron.retention.days`` has.

Bounded rather than floored, and the two are different tools: a floor lets a workspace
tighten what the deployment allows, while these bounds are the range **no** layer may
leave. Deliberately not ``explicit_per_workspace``: provisioning writes a row for every
such key into every workspace, and ``tests/postgres/test_settings_source.py`` pins the
exact set of rows a fresh workspace has.
"""

HARNESS_EXPIRE_BY_AGE = "harness.expire_by_age"
HARNESS_EXPIRE_BY_AGE_DEFAULT = True
"""The gate beside the window, standing in for ``recallatron.retention.expire_by_age``.

The core's verified-expiry entry reads **two** rows under one lock — is age-based
expiry on at all, and is the window still the one the sweep selected against — and
core declares neither key itself, so the harness has to supply both to exercise the
entry at all.

Its default is ``True`` where the production key's is ``False``, and the inversion is
deliberate: this key exists so the generic core entry can be driven, and a default of
``False`` would make every capability, type and window case in
``test_worker_loop.py`` refuse on the gate before reaching the check it is about. The
gate's *own* recheck has one case that writes the row false explicitly. Recallatron's
default-off posture is proved against Recallatron's own key, in
``tests/postgres/test_memory_retention.py``.
"""

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
    KeySpec(
        key=HARNESS_RETENTION_DAYS,
        type=ValueType.INT,
        scope=Scope.WORKSPACE,
        floor=None,
        explicit_per_workspace=False,
        default=HARNESS_RETENTION_DEFAULT,
        minimum=HARNESS_RETENTION_MINIMUM,
        maximum=HARNESS_RETENTION_MAXIMUM,
    ),
    KeySpec(
        key=HARNESS_EXPIRE_BY_AGE,
        type=ValueType.BOOL,
        scope=Scope.WORKSPACE,
        floor=None,
        explicit_per_workspace=False,
        default=HARNESS_EXPIRE_BY_AGE_DEFAULT,
    ),
)


def register_harness_keys() -> None:
    """Register the eight keys (idempotent). The resolved profile must be ``test``."""
    for spec in HARNESS_KEYS:
        register(spec, origin=TEST_HARNESS_ORIGIN)

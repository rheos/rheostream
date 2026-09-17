"""The declared settings key registry (A5).

Every settings key is declared exactly once, here, as a :class:`KeySpec`: its value
type, its scope (who may override it), its floor comparator when the operator's policy
owns it, whether provisioning writes it as an explicit per-workspace row, and its
package default. ``config/defaults.toml`` repeats the *values* of the production keys
and nothing else; ``defaults.py`` asserts at import time that the two agree.

Why the default lives on the registry and not only in the TOML: provisioning (C3)
writes the ``explicit_per_workspace`` rows from package defaults, and the only such key
in this run is a harness key registered from ``tests/harness/settings_keys.py`` under
``profile = test``. **Harness keys are registry-only by design**: the TOML holds the
production keys and the identity check would fail if it held more. So every
registration carries its default, and for the production keys the registry default and
the TOML value are the same value in two places, which the identity check also asserts.

C2 (run 0b1) declared eight production keys; C6 (run 0b2) added the sixteen
``routing.*`` keys below them, for twenty-four; C7a (run 0b2) adds the eight
``identity.*``/``internal.secret_ref`` keys below those, for thirty-two; C4 (run 0c0)
adds ``storage.pool_idle_close_seconds``, for thirty-three, and C7 (run 0c0) adds
``work.due_reconcile_seconds``, for thirty-four. C1 (run 0c1) adds
``work.max_attempts``, for thirty-five; C6 (run 0c3) adds the three ``approvals.*``
keys, for thirty-eight.
``api.cors_origins`` and ``modules.installed`` (the runs that read them) are not
declared here: a key with no reader is machinery with no caller, and the
registry/TOML identity check holds per merge SHA — every later chunk that adds a key
adds it to both files.

The text codec (:func:`decode_text` / :func:`encode_text`) also lives here because the
same encoding serves three readers: environment variables, the ``value text`` column
of the override rows (C3), and the write path's accepted value.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Scope(StrEnum):
    """Who may override a key below the deployment."""

    DEPLOYMENT = "deployment"
    WORKSPACE = "workspace"
    MEMBER = "member"


class Floor(StrEnum):
    """The comparator that combines a deployment value with an override.

    The member is named ``AND`` because ``and`` is a keyword; its value is the
    ratified ``"and"``.
    """

    MIN = "min"
    UNION = "union"
    SUBSET = "subset"
    AND = "and"


class ValueType(StrEnum):
    """The four supported value types, as text so C3 can store ``value_type``."""

    STR = "str"
    INT = "int"
    BOOL = "bool"
    STR_LIST = "list[str]"


SettingValue = str | int | bool | list[str]
"""A value as callers pass and receive it."""

FrozenValue = str | int | bool | tuple[str, ...]
"""A value as the registry and the resolved mapping store it: lists become tuples."""

PROFILES: Final[tuple[str, ...]] = ("production", "development", "test")
PROFILE_KEY: Final = "profile"
CORE_ORIGIN: Final = "core"
TEST_HARNESS_ORIGIN: Final = "test_harness"


class SettingsError(Exception):
    """A settings failure raised as an exception. ``state`` names it.

    The write path never raises one of these for a writer's mistake: it returns a
    :class:`~rheo_core.settings.write_path.SettingRefusal`. These are for the
    deployment layer (an operator's file or environment is wrong, so startup fails
    loudly) and for programming errors (a redeclared key, a default of the wrong type).
    """

    state: str = "settings_error"

    def __init__(self, detail: str, *, key: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.key = key

    def __str__(self) -> str:
        return self.detail


class SettingUndeclared(SettingsError, KeyError):
    """Also a ``KeyError``, so ``in`` and ``.get()`` on a resolved mapping behave."""

    state = "setting_undeclared"


class SettingRedeclared(SettingsError):
    state = "setting_redeclared"


class SettingTypeMismatch(SettingsError):
    state = "setting_type"


class SettingOriginRefused(SettingsError):
    state = "setting_origin_refused"


class SettingsDeclarationMismatch(SettingsError):
    state = "settings_declaration_mismatch"


_KEY_SHAPE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_FLOOR_TYPES: Final[Mapping[Floor, ValueType]] = {
    Floor.MIN: ValueType.INT,
    Floor.UNION: ValueType.STR_LIST,
    Floor.SUBSET: ValueType.STR_LIST,
    Floor.AND: ValueType.BOOL,
}
_TRUE_WORDS: Final = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS: Final = frozenset({"0", "false", "no", "off"})


def freeze_value(value: object) -> FrozenValue:
    """Return ``value`` in its stored form: a list becomes a tuple, all else is as-is.

    Shape checking is :func:`matches_type`'s job; this only removes mutability so a
    frozen :class:`KeySpec` or a resolved mapping cannot be edited through a list it
    handed out.
    """
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, str | int | bool | tuple):
        return value
    raise TypeError(f"unsupported settings value of type {type(value).__name__}")


def thaw_value(value: FrozenValue) -> SettingValue:
    """Return ``value`` in its caller-facing form: a tuple becomes a fresh list."""
    if isinstance(value, tuple):
        return list(value)
    return value


def matches_type(value_type: ValueType, value: object) -> bool:
    """True when ``value`` is a well-formed instance of ``value_type``.

    ``bool`` is checked before ``int`` because ``True`` is an ``int`` in Python and a
    boolean must not pass as an integer setting (or the reverse).
    """
    match value_type:
        case ValueType.STR:
            return isinstance(value, str)
        case ValueType.INT:
            return isinstance(value, int) and not isinstance(value, bool)
        case ValueType.BOOL:
            return isinstance(value, bool)
        case ValueType.STR_LIST:
            return isinstance(value, list | tuple) and all(
                isinstance(item, str) for item in value
            )


@dataclass(frozen=True, slots=True)
class KeySpec:
    """One declared settings key.

    ``default`` is mandatory and is the package default (see the module docstring for
    why it lives here). ``choices`` constrains a ``str`` key to a closed set of values,
    which is how ``profile`` is held to ``production | development | test``.
    """

    key: str
    type: ValueType
    scope: Scope
    floor: Floor | None
    explicit_per_workspace: bool
    default: FrozenValue
    choices: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not _KEY_SHAPE.fullmatch(self.key):
            raise ValueError(
                f"settings key {self.key!r} is not dotted lowercase segments"
            )
        object.__setattr__(self, "default", freeze_value(self.default))
        if not matches_type(self.type, self.default):
            raise TypeError(
                f"settings key {self.key!r}: default {self.default!r} is not "
                f"a {self.type.value}"
            )
        if self.floor is not None and _FLOOR_TYPES[self.floor] is not self.type:
            raise TypeError(
                f"settings key {self.key!r}: floor {self.floor.value!r} applies to "
                f"{_FLOOR_TYPES[self.floor].value} keys, not {self.type.value}"
            )
        if self.choices is not None:
            if self.type is not ValueType.STR:
                raise TypeError(
                    f"settings key {self.key!r}: choices apply to str keys only"
                )
            if self.default not in self.choices:
                raise ValueError(
                    f"settings key {self.key!r}: default {self.default!r} is not "
                    f"one of {list(self.choices)}"
                )


def check_value(spec: KeySpec, value: object, *, source: str) -> FrozenValue:
    """Type-check a natively typed value (TOML, a write) against ``spec``.

    Raises :class:`SettingTypeMismatch` naming the key and ``source`` otherwise.
    """
    if not matches_type(spec.type, value):
        raise SettingTypeMismatch(
            f"{spec.key}: {source} holds a value of type {type(value).__name__}, "
            f"declared type is {spec.type.value}",
            key=spec.key,
        )
    frozen = freeze_value(value)
    if spec.choices is not None and frozen not in spec.choices:
        raise SettingTypeMismatch(
            f"{spec.key}: {source} holds {frozen!r}, which is not one of "
            f"{list(spec.choices)}",
            key=spec.key,
        )
    return frozen


def decode_text(spec: KeySpec, text: str, *, source: str) -> FrozenValue:
    """Coerce a text value (an environment variable, an override row) by ``spec.type``.

    Integers are decimal; booleans accept ``true/false``, ``1/0``, ``yes/no``,
    ``on/off`` case-insensitively; ``list[str]`` is comma-separated with items
    stripped and empty items dropped, so the empty string is the empty list. A value
    that will not coerce raises :class:`SettingTypeMismatch` naming the key and
    ``source`` (the variable name or the row), never echoing the text itself, because
    a mistyped setting can be a pasted secret.
    """
    value: FrozenValue
    match spec.type:
        case ValueType.STR:
            value = text
        case ValueType.INT:
            try:
                value = int(text.strip())
            except ValueError:
                raise SettingTypeMismatch(
                    f"{spec.key}: {source} is not an integer", key=spec.key
                ) from None
        case ValueType.BOOL:
            word = text.strip().lower()
            if word in _TRUE_WORDS:
                value = True
            elif word in _FALSE_WORDS:
                value = False
            else:
                raise SettingTypeMismatch(
                    f"{spec.key}: {source} is not a boolean", key=spec.key
                )
        case ValueType.STR_LIST:
            value = tuple(item.strip() for item in text.split(",") if item.strip())
    if spec.choices is not None and value not in spec.choices:
        raise SettingTypeMismatch(
            f"{spec.key}: {source} is not one of {list(spec.choices)}", key=spec.key
        )
    return value


def encode_text(spec: KeySpec, value: FrozenValue | SettingValue) -> str:
    """The inverse of :func:`decode_text`: the text form C3 stores in ``value``."""
    match spec.type:
        case ValueType.STR:
            return str(value)
        case ValueType.INT:
            return str(int(value))  # type: ignore[arg-type]
        case ValueType.BOOL:
            return "true" if value else "false"
        case ValueType.STR_LIST:
            if not isinstance(value, list | tuple):
                raise TypeError(f"{spec.key}: expected a list of str")
            return ",".join(value)


class SettingsRegistry:
    """The process-wide declaration table.

    Registration is idempotent per key: registering the identical spec under the same
    origin again is a no-op, and a different spec (or origin) for a declared key raises
    :class:`SettingRedeclared`. ``origin = "test_harness"`` is accepted only when the
    resolved ``profile`` is ``test``; production keys register with ``origin =
    "core"``.
    """

    def __init__(self) -> None:
        self._specs: dict[str, KeySpec] = {}
        self._origins: dict[str, str] = {}

    def register(self, spec: KeySpec, *, origin: str) -> None:
        if not origin:
            raise ValueError("a settings registration needs a non-empty origin")
        if origin == TEST_HARNESS_ORIGIN:
            # deployment.py imports this module, so the profile lookup is imported
            # here, at call time, rather than at the top of the file.
            from rheo_core.settings.deployment import current_profile

            profile = current_profile()
            if profile != "test":
                raise SettingOriginRefused(
                    f"{spec.key}: origin {origin!r} is accepted only under "
                    f"profile = test (resolved profile is {profile!r})",
                    key=spec.key,
                )
        existing = self._specs.get(spec.key)
        if existing is not None:
            if existing == spec and self._origins[spec.key] == origin:
                return
            raise SettingRedeclared(
                f"{spec.key} is already declared (origin "
                f"{self._origins[spec.key]!r}) with a different spec or origin",
                key=spec.key,
            )
        self._specs[spec.key] = spec
        self._origins[spec.key] = origin

    def lookup(self, key: str) -> KeySpec | None:
        return self._specs.get(key)

    def get(self, key: str) -> KeySpec:
        spec = self._specs.get(key)
        if spec is None:
            raise SettingUndeclared(f"{key} is not a declared settings key", key=key)
        return spec

    def origin_of(self, key: str) -> str:
        self.get(key)
        return self._origins[key]

    def keys(self, *, origin: str | None = None) -> frozenset[str]:
        if origin is None:
            return frozenset(self._specs)
        return frozenset(k for k, o in self._origins.items() if o == origin)

    def specs(self, *, origin: str | None = None) -> tuple[KeySpec, ...]:
        return tuple(self._specs[k] for k in sorted(self.keys(origin=origin)))

    def explicit_per_workspace(self) -> tuple[KeySpec, ...]:
        """The keys provisioning writes as rows from their package default (C3)."""
        return tuple(s for s in self.specs() if s.explicit_per_workspace)

    def __contains__(self, key: object) -> bool:
        return key in self._specs


PRODUCTION_KEYS: Final[tuple[KeySpec, ...]] = (
    KeySpec(
        key="storage.cluster_dsn_ref",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="secret://env/RHEO_CLUSTER_DSN",
    ),
    KeySpec(
        key="storage.control_database",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="rheo_control",
    ),
    # The ratified default (storage-and-workspaces.md § Extensions and the application
    # role): an operator who needs pre-installed extensions points this at their own
    # template database.
    KeySpec(
        key="storage.template_database",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="template1",
    ),
    # The hard ceiling on cached engines. Its product with
    # ``storage.pool_max_connections``, plus the connections the backend reserves
    # outside the cache (its control engine, itself sized at
    # ``storage.pool_max_connections``), is this process's worst-case connection count,
    # and core and worker are separate processes each holding their own: the default
    # pair is 16 * 5 + 5 = 85 per process. The previous default of 32 put that at 165
    # against a stock Postgres ``max_connections`` of 100 (issue #12).
    KeySpec(
        key="storage.pool_cache_size",
        type=ValueType.INT,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=16,
    ),
    # The bound that normally binds. An engine untouched this long is disposed, so a
    # caller that walks every workspace reclaims what nobody wanted rather than
    # evicting what it is about to need.
    KeySpec(
        key="storage.pool_idle_close_seconds",
        type=ValueType.INT,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=300,
    ),
    KeySpec(
        key="storage.pool_max_connections",
        type=ValueType.INT,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=5,
    ),
    KeySpec(
        key=PROFILE_KEY,
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="development",
        choices=PROFILES,
    ),
    # The two token_max_days keys are the only floored production keys in the design
    # and criterion 69 needs one; nothing in 0b1 reads them except that test.
    KeySpec(
        key="identity.token_max_days.cli",
        type=ValueType.INT,
        scope=Scope.WORKSPACE,
        floor=Floor.MIN,
        explicit_per_workspace=False,
        default=90,
    ),
    KeySpec(
        key="identity.token_max_days.mcp",
        type=ValueType.INT,
        scope=Scope.WORKSPACE,
        floor=Floor.MIN,
        explicit_per_workspace=False,
        default=30,
    ),
    # --- routing (C6, run 0b2) -------------------------------------------------------
    # Sixteen flat keys for what the ratified `RoutingConfig` draws as a nested object.
    # That is a volume consequence of this registry having four scalar value types and
    # no nested or dict type, not a design choice: every scalar the object needs gets
    # its own key. Two of the object's fields are deliberately absent — the identity
    # surface's `fixed_path` is an invariant hardcoded in `routing/config.py` (every
    # application host serves `/auth/*` in both modes, so an operator must not be able
    # to relocate it), and `routing.modules.*` declares nothing in 0b because the
    # module surface map is statically empty until modules exist.
    KeySpec(
        key="routing.mode",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="path",
        choices=("path", "subdomain"),
    ),
    KeySpec(
        key="routing.scheme",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="https",
        choices=("https", "http"),
    ),
    KeySpec(
        key="routing.base_host",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="localhost",
    ),
    KeySpec(
        key="routing.shell.host",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="circuit",
    ),
    KeySpec(
        key="routing.shell.path",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="/",
    ),
    KeySpec(
        key="routing.identity.host",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="auth",
    ),
    KeySpec(
        key="routing.identity.path",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="/auth",
    ),
    KeySpec(
        key="routing.api.host",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="api",
    ),
    KeySpec(
        key="routing.api.path",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="/api",
    ),
    KeySpec(
        key="routing.mcp.host",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="mcp",
    ),
    KeySpec(
        key="routing.mcp.path",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="/mcp",
    ),
    KeySpec(
        key="routing.docs.host",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="docs",
    ),
    KeySpec(
        key="routing.docs.external",
        type=ValueType.BOOL,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=True,
    ),
    KeySpec(
        key="routing.integration.host",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="tuttle",
    ),
    KeySpec(
        key="routing.integration.external",
        type=ValueType.BOOL,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=True,
    ),
    KeySpec(
        key="routing.integration.reserved",
        type=ValueType.BOOL,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=True,
    ),
    # --- identity (C7a, run 0b2) ------------------------------------------------------
    # ``identity.allow_signup`` and ``identity.allow_workspace_create`` are plain bools,
    # not "nullable" (fit-check.md D6): the registry has no None-typed default, and a
    # first-account bootstrap is already computed in ``accounts.py`` regardless of the
    # flag's value, so ``false`` expresses the ratified behaviour identically to a
    # tri-state default would.
    KeySpec(
        key="identity.allow_signup",
        type=ValueType.BOOL,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=False,
    ),
    KeySpec(
        key="identity.allow_workspace_create",
        type=ValueType.BOOL,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=False,
    ),
    KeySpec(
        key="identity.session_idle_days",
        type=ValueType.INT,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=14,
    ),
    KeySpec(
        key="identity.session_max_days",
        type=ValueType.INT,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=30,
    ),
    KeySpec(
        key="identity.providers.github.enabled",
        type=ValueType.BOOL,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=False,
    ),
    KeySpec(
        key="identity.providers.github.client_id",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="",
    ),
    # Empty-string package default, deliberately not ``secret://env/...``
    # (fit-check.md D4): the real reference is a deployment-time concern 11 wires into
    # compose. A package default of a live ``secret://env/`` reference would make
    # ``check_env_references`` refuse every startup — compose, ``make demo``, CI, a
    # fresh clone's ``uv run pytest`` — before an operator ever configures the
    # provider, because the named environment variable is not set until 11 wires it.
    # ``is_secret_reference("")`` is ``False``, so the startup check stays quiet until
    # an operator writes a real reference into deployment settings.
    KeySpec(
        key="identity.providers.github.client_secret_ref",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="",
    ),
    # Same empty-string reasoning as the GitHub client secret above: the real
    # ``secret://env/RHEO_INTERNAL_SECRET`` reference is 11's deployment concern.
    KeySpec(
        key="internal.secret_ref",
        type=ValueType.STR,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default="",
    ),
    # The due-work index's reconcile floor: the longest a lost mark can leave work
    # undiscovered. It must stay **above** ``storage.pool_idle_close_seconds`` (300),
    # or a reconcile pass re-touches every cached engine inside the idle window and
    # the count cap becomes the only bound again — the thrash issue #12 removed. Both
    # keys are deployment-scope, so the relation is checked on the *resolved* settings
    # by ``rheo doctor`` as well as on the packaged defaults in the test suite.
    KeySpec(
        key="work.due_reconcile_seconds",
        type=ValueType.INT,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=900,
    ),
    # The job retry budget a caller gets when it does not choose one. ``enqueue``
    # reads this key and writes the value into the row's ``max_attempts`` column; the
    # column is the authority from then on, so changing this key never re-budgets a
    # job already queued. Deployment scope: a workspace able to raise its own budget
    # could hold a shared worker on one poison job for as long as it liked.
    KeySpec(
        key="work.max_attempts",
        type=ValueType.INT,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=8,
    ),
    # The bound on one approval's stored payload snapshot
    # (``confirmation-and-safety.md`` § The approval record). The snapshot is what a
    # person reads at the point of confirmation, so the bound is on what may be
    # stored and an input over it is refused rather than truncated: an approval whose
    # stored payload is not the payload would be a confirmation of something else.
    # Deployment scope, like every other storage bound here — a workspace able to
    # raise its own would be raising a limit on a shared database.
    KeySpec(
        key="approvals.max_payload_bytes",
        type=ValueType.INT,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=65536,
    ),
    # The execution window an approval is created with, and the ceiling on it. No
    # caller chooses a length in release one (no operation takes one as an input), so
    # the default is the length every approval gets; the maximum is applied as a
    # clamp on the resolved default rather than only on a caller, which is what makes
    # it a bound rather than a comment. ``rheo_core.approvals.gate.window_seconds``
    # is the one reader of both.
    KeySpec(
        key="approvals.default_window_seconds",
        type=ValueType.INT,
        scope=Scope.DEPLOYMENT,
        floor=None,
        explicit_per_workspace=False,
        default=900,
    ),
    # **Workspace scope with a ``min`` floor — the third floored key in the tree.**
    # ``confirmation-and-safety.md``'s own row reads "maximum
    # ``approvals.max_window_seconds`` 86400, floor ``min``", and in this codebase a
    # floor only does anything on a key a workspace may override: the two
    # ``identity.token_max_days.*`` keys above are the pattern, and
    # ``rheo_core.approvals.gate.window_seconds`` resolves this one *with the
    # workspace's rows* so the floor is applied rather than declared.
    # What separates it from ``approvals.max_payload_bytes`` beside it is what kind of
    # bound it is: the payload bound protects a shared database, so a workspace must
    # not touch it, while this one is a workspace's own safety margin — shortening how
    # long its approvals stay executable costs no one else anything, and ``min`` is
    # what stops the same override *lengthening* it.
    KeySpec(
        key="approvals.max_window_seconds",
        type=ValueType.INT,
        scope=Scope.WORKSPACE,
        floor=Floor.MIN,
        explicit_per_workspace=False,
        default=86400,
    ),
)

REGISTRY: Final = SettingsRegistry()
for _spec in PRODUCTION_KEYS:
    REGISTRY.register(_spec, origin=CORE_ORIGIN)


def register(spec: KeySpec, *, origin: str) -> None:
    """Declare a key on the process-wide registry; see ``SettingsRegistry.register``."""
    REGISTRY.register(spec, origin=origin)


def spec_for(key: str) -> KeySpec:
    """The declaration for ``key``, or :class:`SettingUndeclared`."""
    return REGISTRY.get(key)

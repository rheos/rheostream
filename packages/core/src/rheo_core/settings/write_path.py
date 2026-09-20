"""The settings write path: ``validate_override`` and its four refusal states.

Called before a workspace or member row is written (``core.settings.set`` and
``core.settings.set_member`` in C4). It refuses with a :class:`SettingRefusal` whose
``state`` is exactly one of:

- ``setting_undeclared`` — no ``KeySpec`` for that key.
- ``setting_scope`` — the writer may not set a key of that scope.
- ``setting_type`` — the value does not match the declared type, is not one of a
  ``choices`` key's values, or is outside a bounded ``int`` key's declared range.
- ``setting_floor_violation`` — a floored value looser than the deployment value; the
  refusal names the key.

These are the state names public criterion 69 quotes. They are not renamed and there
is no fifth. Success is a :class:`SettingAccepted` carrying the frozen value, its text
encoding for the ``value`` column and its ``value_type``.
"""

from dataclasses import dataclass
from typing import Final

from rheo_core.settings.resolver import is_looser
from rheo_core.settings.schema import (
    REGISTRY,
    FrozenValue,
    Scope,
    SettingTypeMismatch,
    ValueType,
    check_bounds,
    check_value,
    encode_text,
    freeze_value,
    matches_type,
    thaw_value,
)

REFUSAL_STATES: Final = frozenset(
    {"setting_undeclared", "setting_scope", "setting_type", "setting_floor_violation"}
)


@dataclass(frozen=True, slots=True)
class SettingRefusal:
    state: str
    key: str
    detail: str


@dataclass(frozen=True, slots=True)
class SettingAccepted:
    key: str
    value: FrozenValue
    encoded: str
    value_type: ValueType


def validate_override(
    key: str,
    value: object,
    deployment_value: object,
    *,
    scope: Scope = Scope.WORKSPACE,
) -> SettingAccepted | SettingRefusal:
    """Validate one override before it is stored.

    ``scope`` is the row kind being written: ``workspace`` for ``core.settings.set``,
    ``member`` for ``core.settings.set_member``. ``deployment_value`` is the value the
    deployment layer resolves for ``key`` (its package default when the deployment sets
    nothing); it is consulted only for floored keys.
    """
    spec = REGISTRY.lookup(key)
    if spec is None:
        return SettingRefusal(
            "setting_undeclared", key, f"{key} is not a declared settings key"
        )
    if scope is Scope.DEPLOYMENT or spec.scope is not scope:
        return SettingRefusal(
            "setting_scope",
            key,
            f"{key} has scope {spec.scope.value}; it cannot be set as a "
            f"{scope.value} override",
        )
    if not matches_type(spec.type, value) or (
        spec.choices is not None and value not in spec.choices
    ):
        return SettingRefusal(
            "setting_type",
            key,
            f"{key} is declared {spec.type.value}; the value does not match",
        )
    frozen = freeze_value(value)
    try:
        # The write is the third configuration layer a bounded ``int`` key has to be
        # held at — the TOML and an environment variable go through ``check_value``,
        # an override row through ``decode_text``, and a caller's write through here.
        # Folded into ``setting_type`` rather than given a state of its own: criterion
        # 69 quotes exactly four refusal states and there is no fifth, and "the value
        # is not one this key accepts" is what that state already says for a
        # ``choices`` key one branch above.
        check_bounds(spec, frozen, source="the write")
    except SettingTypeMismatch as exc:
        return SettingRefusal("setting_type", key, str(exc))
    if spec.floor is not None:
        base = check_value(spec, deployment_value, source="deployment value")
        if is_looser(spec.floor, base, frozen):
            return SettingRefusal(
                "setting_floor_violation",
                key,
                f"{key}: {thaw_value(frozen)!r} is looser than the deployment value "
                f"{thaw_value(base)!r} under floor {spec.floor.value!r}",
            )
    return SettingAccepted(key, frozen, encode_text(spec, frozen), spec.type)

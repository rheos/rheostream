"""Resolution: package default → deployment → override, then the floor.

Overrides arrive through :class:`OverrideSource`, the protocol C3 implements in
``settings/storage_source.py`` over the ``core.workspace_setting`` and
``core.member_setting`` rows. Nothing here opens a connection. The protocol is a
contract: once C3 implements it, it must not change.

A row applies only where the key's scope allows: ``scope = workspace`` keys take
workspace rows and ``scope = member`` keys take member rows. A row for a
``deployment``-scope key, for an undeclared key, or with a value that will not coerce
is ignored and logged (key and state only, never the value), so a stale or hostile row
can neither crash a request nor take effect.

For keys that carry a floor, the override is combined with the deployment value under
the comparator on **every read**, regardless of how the row got there: a row written
before an operator tightened the deployment value is neutralised the moment the floor
changes, without being deleted (the write path, ``write_path.py``, is the other half).
"""

import logging
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from rheo_core.settings.deployment import deployment_layer
from rheo_core.settings.schema import (
    REGISTRY,
    Floor,
    FrozenValue,
    Scope,
    SettingTypeMismatch,
    SettingUndeclared,
    SettingValue,
    ValueType,
    decode_text,
    thaw_value,
)

logger = logging.getLogger("rheo_core.settings")


class OverrideSource(Protocol):
    """Where workspace and member override rows come from (C3 implements it)."""

    def workspace_overrides(self, workspace_id: UUID) -> Mapping[str, str]: ...

    def member_overrides(
        self, workspace_id: UUID, account_id: UUID
    ) -> Mapping[str, str]: ...


class ResolvedSettings(Mapping[str, SettingValue]):
    """The settings for one request: a frozen mapping with typed lookup.

    Lists are handed out as fresh copies, so nothing a caller does to a returned value
    reaches the mapping. An undeclared key raises ``SettingUndeclared`` rather than
    ``KeyError`` so the refusal state is named at the read seam too.
    """

    __slots__ = ("_values", "_workspace_keys")
    _values: Mapping[str, FrozenValue]
    _workspace_keys: frozenset[str]

    def __init__(
        self,
        values: Mapping[str, FrozenValue],
        workspace_keys: frozenset[str] = frozenset(),
    ) -> None:
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))
        object.__setattr__(self, "_workspace_keys", frozenset(workspace_keys))

    def set_by_workspace(self, key: str) -> bool:
        """Whether a valid workspace override row supplied ``key``'s value.

        For a key whose policy is an explicit workspace opt-in on top of the
        operator's permission (``redaction.contact_points_to_model``, issue #130):
        there, "no row" must mean "not opted in", not "inherit the operator's value",
        and only the resolver knows which values came from a row.
        """
        return key in self._workspace_keys

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ResolvedSettings is frozen")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("ResolvedSettings is frozen")

    def __getitem__(self, key: str) -> SettingValue:
        try:
            return thaw_value(self._values[key])
        except KeyError:
            raise SettingUndeclared(
                f"{key} is not a declared settings key", key=key
            ) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, key: object) -> bool:
        return key in self._values

    def __repr__(self) -> str:
        # Key names only, never values (fit-check.md Q2a): a resolved value can be a
        # secret:// reference (or, one day, something worse), and this repr fires
        # implicitly — a pytest assertion diff, a traceback frame, a stray
        # logger.debug("%r", settings). Selective redaction via is_secret_reference
        # was considered and rejected: it would add a settings -> secrets import edge
        # that does not exist today, for no gain over not printing values at all.
        return (
            f"ResolvedSettings({len(self._values)} keys: "
            f"{', '.join(sorted(self._values))})"
        )

    def _typed(self, key: str, value_type: ValueType) -> FrozenValue:
        spec = REGISTRY.get(key)
        if spec.type is not value_type:
            raise SettingTypeMismatch(
                f"{key} is declared {spec.type.value}, not {value_type.value}",
                key=key,
            )
        return self._values[key]

    def get_str(self, key: str) -> str:
        return str(self._typed(key, ValueType.STR))

    def get_int(self, key: str) -> int:
        value = self._typed(key, ValueType.INT)
        assert isinstance(value, int)
        return value

    def get_bool(self, key: str) -> bool:
        value = self._typed(key, ValueType.BOOL)
        assert isinstance(value, bool)
        return value

    def get_list(self, key: str) -> list[str]:
        value = self._typed(key, ValueType.STR_LIST)
        assert isinstance(value, tuple)
        return list(value)


def _as_int(value: FrozenValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"floor 'min' needs an int, got {type(value).__name__}")
    return value


def _as_items(value: FrozenValue) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"a list floor needs a list, got {type(value).__name__}")
    return value


def _as_bool(value: FrozenValue) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"floor 'and' needs a bool, got {type(value).__name__}")
    return value


def apply_floor(
    floor: Floor, deployment_value: FrozenValue, override_value: FrozenValue
) -> FrozenValue:
    """The effective value of an override under the deployment value's floor.

    ``min`` takes the lower; ``union`` the union (deployment order first, then the
    override's new items in its order); ``subset`` the intersection (in deployment
    order); ``and`` the logical conjunction.
    """
    match floor:
        case Floor.MIN:
            return min(_as_int(deployment_value), _as_int(override_value))
        case Floor.UNION:
            base = _as_items(deployment_value)
            extra = tuple(x for x in _as_items(override_value) if x not in base)
            return base + extra
        case Floor.SUBSET:
            allowed = _as_items(override_value)
            return tuple(x for x in _as_items(deployment_value) if x in allowed)
        case Floor.AND:
            return _as_bool(deployment_value) and _as_bool(override_value)


def is_looser(
    floor: Floor, deployment_value: FrozenValue, candidate: FrozenValue
) -> bool:
    """True when ``candidate`` would relax ``deployment_value`` under ``floor``.

    This is the write-time question. ``min``: a higher number. ``union``: a list
    missing something the deployment requires (the list can only grow). ``subset``: a
    list adding something the deployment does not allow (the list can only shrink).
    ``and``: ``True`` where the deployment says ``False``.
    """
    match floor:
        case Floor.MIN:
            return _as_int(candidate) > _as_int(deployment_value)
        case Floor.UNION:
            return not set(_as_items(deployment_value)) <= set(_as_items(candidate))
        case Floor.SUBSET:
            return not set(_as_items(candidate)) <= set(_as_items(deployment_value))
        case Floor.AND:
            return _as_bool(candidate) and not _as_bool(deployment_value)


def _apply_rows(
    values: dict[str, FrozenValue],
    rows: Mapping[str, str],
    *,
    scope: Scope,
    workspace_id: UUID,
    account_id: UUID | None,
) -> frozenset[str]:
    """Apply ``rows`` over ``values``; return the keys a valid row supplied."""
    applied: set[str] = set()
    for key, text in rows.items():
        spec = REGISTRY.lookup(key)
        if spec is None:
            _ignore(key, "setting_undeclared", scope, workspace_id, account_id)
            continue
        if spec.scope is not scope:
            _ignore(key, "setting_scope", scope, workspace_id, account_id)
            continue
        try:
            value = decode_text(spec, str(text), source=f"{scope.value} row")
        except SettingTypeMismatch:
            _ignore(key, "setting_type", scope, workspace_id, account_id)
            continue
        if spec.floor is not None:
            value = apply_floor(spec.floor, values[key], value)
        values[key] = value
        applied.add(key)
    return frozenset(applied)


def _ignore(
    key: str, state: str, scope: Scope, workspace_id: UUID, account_id: UUID | None
) -> None:
    logger.warning(
        "setting_override_ignored",
        extra={
            "setting_key": key,
            "state": state,
            "override_scope": scope.value,
            "workspace_id": str(workspace_id),
            "account_id": None if account_id is None else str(account_id),
        },
    )


def resolve(
    *,
    workspace_id: UUID | None = None,
    account_id: UUID | None = None,
    source: OverrideSource | None = None,
) -> ResolvedSettings:
    """Resolve every declared key for one request.

    Package default (from the registry; identical to ``defaults.toml`` for production
    keys by the import-time check) → deployment layer → workspace rows (when a
    ``workspace_id`` and ``source`` are given) → member rows (when an ``account_id``
    is also given), with the floor applied to every floored override.
    """
    values: dict[str, FrozenValue] = {
        spec.key: spec.default for spec in REGISTRY.specs()
    }
    values.update(deployment_layer())
    workspace_keys: frozenset[str] = frozenset()
    if source is not None and workspace_id is not None:
        workspace_keys = _apply_rows(
            values,
            source.workspace_overrides(workspace_id),
            scope=Scope.WORKSPACE,
            workspace_id=workspace_id,
            account_id=None,
        )
        if account_id is not None:
            _apply_rows(
                values,
                source.member_overrides(workspace_id, account_id),
                scope=Scope.MEMBER,
                workspace_id=workspace_id,
                account_id=account_id,
            )
    return ResolvedSettings(values, workspace_keys)

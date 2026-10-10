"""Pure pinned-mapping evaluation: absence is different from an explicit clear."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rheo_leads.configuration import FACTS

MISSING = object()


class MappingRefused(ValueError):
    """Carries only a target/path label, never source contents."""


@dataclass(frozen=True)
class Fact:
    target: str
    value_kind: str
    value_text: str | None
    source_path: str
    transform: str | None


def value_at(payload: object, path: str | None, source_kind: str) -> object:
    if path is None:
        return MISSING
    if source_kind == "csv":
        return payload.get(path, MISSING) if isinstance(payload, dict) else MISSING
    if path == "":
        return payload
    if not path.startswith("/"):
        raise MappingRefused("invalid_json_pointer")
    value = payload
    for encoded in path[1:].split("/"):
        if re.search(r"~(?![01])", encoded):
            raise MappingRefused("invalid_json_pointer")
        part = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value.get(part, MISSING)
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            index = int(part)
            value = value[index] if index < len(value) else MISSING
        else:
            return MISSING
    return value


def transform_value(value: object, transform: str | None, arg: str | None) -> str:
    if transform == "const":
        return arg or ""
    if not isinstance(value, str | int | float | bool):
        raise ValueError("scalar_required")
    result = str(value)
    if transform is not None:
        result = result.strip()
    if transform in (None, "trim"):
        return result
    if transform in ("lower", "email_normalize"):
        return result.lower()
    if transform == "phone_normalize":
        # Preserve an international prefix, remove presentation punctuation only.
        if re.search(r"[^0-9+().\s-]", result) or "+" in result[1:]:
            raise ValueError("invalid_phone")
        return ("+" if result.startswith("+") else "") + re.sub(r"\D", "", result)
    if transform == "datetime":
        parsed = datetime.strptime(result, arg or "%Y-%m-%dT%H:%M:%S%z")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.isoformat()
    raise ValueError("unknown_transform")


def resolve_identity(
    identity: Mapping[str, object], payload: object, *, source_kind: str
) -> dict[str, object]:
    """Address identity fields without promoting them to trusted routing claims.

    Missing stays distinct from null. Transport callers choose their own event-id
    policy (manual always mints a new one) and validate the scalars they consume.
    """
    return {
        key: value_at(payload, str(path) if path is not None else None, source_kind)
        for key in (
            "event_id_path",
            "occurred_at_path",
            "subject_id_path",
            "verified_email_path",
        )
        for path in (identity.get(key),)
    }


def apply_mapping(
    rules: Sequence[Mapping[Any, Any]],
    payload: object,
    *,
    source_kind: str,
    extension_targets: frozenset[str] = frozenset(),
) -> list[Fact]:
    facts = []
    for rule in rules:
        target, path = str(rule["target"]), str(rule["source_path"])
        if target not in FACTS and target not in extension_targets:
            raise MappingRefused("unknown_target")
        transform = str(rule["transform"]) if rule["transform"] is not None else None
        arg = str(rule["transform_arg"]) if rule["transform_arg"] is not None else None
        value = value_at(payload, path, source_kind)
        if transform == "const":
            value = arg or ""
        if value is MISSING or value is None:
            if value is None and rule["clear_on_null"]:
                facts.append(Fact(target, "cleared", None, path, transform))
            elif rule["required"]:
                raise MappingRefused(target)
            continue
        try:
            normalized = transform_value(value, transform, arg)
        except (ValueError, TypeError, OverflowError):
            raise MappingRefused(target) from None
        facts.append(Fact(target, "value", normalized, path, transform))
    return facts

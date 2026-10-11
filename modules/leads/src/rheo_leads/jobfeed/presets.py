"""The search presets a collection run issues, and the validator that bounds them.

v1 offers no preset configuration: every run uses ``DEFAULT_PRESETS``. The validator
still exists as tested code so the bounds hold the moment configuration arrives, and
the defaults are built through it, so they can never drift outside those bounds.
Every refusal happens here, before any upstream call.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

Action = Literal["search", "smart_search"]
Purpose = Literal["discover", "refresh_only"]

# The closed set of upstream search filters a preset may carry. Credentials,
# organization ids and pagination cursors are deliberately absent: a preset is
# reusable configuration, never a secret or a position in someone's result list.
ALLOWED_FILTERS: frozenset[str] = frozenset(
    {
        "query",
        "title",
        "job_type",
        "experience_level",
        "budget_min",
        "budget_max",
        "rate_min",
        "rate_max",
        "skills",
        "category",
        "subcategory",
        "workload",
        "duration",
        "verified_payment_only",
        "proposals_min",
        "proposals_max",
        "client_hires_min",
        "client_hires_max",
        "previous_clients_only",
        "timezone",
        "location",
        "sort",
        "include_full_details",
        "mode",
        "days_posted",
        "from_date",
        "to_date",
        "limit",
    }
)
PRESET_KEYS: frozenset[str] = frozenset(
    {"name", "action", "purpose", "pages", "params"}
)
MAX_PRESETS = 8
MAX_PAGES = 5
MAX_TOTAL_PAGES = 8
MAX_LIMIT = 10
# Discovery takes postings at or under this many proposals (inclusive).
DISCOVERY_CAP = 50
# The over-cap refresh looks only above the discovery cap.
REFRESH_MINIMUM = DISCOVERY_CAP + 1
SMART_MODES: frozenset[str] = frozenset({"best_match", "most_recent"})


class PresetInvalid(ValueError):
    """A preset breaks a bound. ``code`` is fixed per rule and carries no input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Preset:
    """A validated preset. ``params`` is read-only and carries every default the
    validator applies (``limit``; ``proposals_max`` for discovery, ``proposals_min``
    for refresh; ``mode`` for ``smart_search``; ``include_full_details`` for
    ``search``), so a caller sends exactly what was validated."""

    name: str
    action: Action
    purpose: Purpose
    pages: int
    params: Mapping[str, object]


def _action(value: object) -> Action:
    if value == "search":
        return "search"
    if value == "smart_search":
        return "smart_search"
    raise PresetInvalid("action_invalid")


def _purpose(value: object) -> Purpose:
    if value == "discover":
        return "discover"
    if value == "refresh_only":
        return "refresh_only"
    raise PresetInvalid("purpose_invalid")


def _integer(value: object) -> int | None:
    # ``type(...) is int`` refuses booleans, which are ints to ``isinstance``.
    return value if type(value) is int else None


def _validate_one(raw: object, names: set[str]) -> Preset:
    if not isinstance(raw, Mapping) or set(raw) - PRESET_KEYS:
        raise PresetInvalid("preset_keys_invalid")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or name in names:
        raise PresetInvalid("preset_name_invalid")
    names.add(name)
    action = _action(raw.get("action"))
    pages = _integer(raw.get("pages", 1))
    if pages is None or not 1 <= pages <= MAX_PAGES:
        raise PresetInvalid("pages_invalid")
    params = raw.get("params", {})
    if not isinstance(params, Mapping) or set(params) - ALLOWED_FILTERS:
        raise PresetInvalid("params_not_allowed")
    limit = _integer(params.get("limit", MAX_LIMIT))
    if limit is None or not 1 <= limit <= MAX_LIMIT:
        raise PresetInvalid("limit_invalid")
    purpose = _purpose(raw.get("purpose", "discover"))
    filled: dict[str, object] = {**params, "limit": limit}
    if purpose == "refresh_only":
        if action != "search":
            raise PresetInvalid("refresh_requires_search")
        minimum = _integer(params.get("proposals_min", REFRESH_MINIMUM))
        if minimum is None or minimum < REFRESH_MINIMUM:
            raise PresetInvalid("refresh_minimum_invalid")
        if "proposals_max" in params:
            raise PresetInvalid("refresh_maximum_not_allowed")
        filled["proposals_min"] = minimum
    else:
        cap = _integer(params.get("proposals_max", DISCOVERY_CAP))
        if cap is None or not 0 <= cap <= DISCOVERY_CAP:
            raise PresetInvalid("discovery_maximum_invalid")
        filled["proposals_max"] = cap
    if action == "smart_search":
        mode = params.get("mode", "best_match")
        if not isinstance(mode, str) or mode not in SMART_MODES:
            raise PresetInvalid("mode_invalid")
        filled["mode"] = mode
    else:
        # Discovery reads full details; the over-cap refresh needs only the counts.
        filled.setdefault("include_full_details", purpose == "discover")
    return Preset(
        name=name,
        action=action,
        purpose=purpose,
        pages=pages,
        params=MappingProxyType(filled),
    )


def validate_presets(presets: object) -> tuple[Preset, ...]:
    """Validate a preset list and return it with defaults applied, or raise
    ``PresetInvalid`` for the first bound it breaks."""
    if not isinstance(presets, list | tuple) or not 1 <= len(presets) <= MAX_PRESETS:
        raise PresetInvalid("preset_count_invalid")
    names: set[str] = set()
    validated = tuple(_validate_one(raw, names) for raw in presets)
    if sum(preset.pages for preset in validated) > MAX_TOTAL_PAGES:
        raise PresetInvalid("total_pages_invalid")
    return validated


# Best Matches is one page; Most Recent is the initial page plus four load-more
# pages; the over-cap refresh reads one page of postings above the discovery cap so
# tracked postings keep their counts. At ten rows a page that is at most 70 rows.
DEFAULT_PRESETS: tuple[Preset, ...] = validate_presets(
    [
        {
            "name": "Best Matches",
            "action": "smart_search",
            "pages": 1,
            "params": {"mode": "best_match"},
        },
        {
            "name": "Most Recent",
            "action": "smart_search",
            "pages": 5,
            "params": {"mode": "most_recent"},
        },
        {
            "name": "Over-cap refresh",
            "action": "search",
            "purpose": "refresh_only",
            "pages": 1,
            "params": {"proposals_min": REFRESH_MINIMUM, "sort": "recency"},
        },
    ]
)

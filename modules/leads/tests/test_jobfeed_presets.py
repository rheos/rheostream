"""The preset validator's bounds, one refusal per rule, and the three defaults."""

from typing import Any

import pytest
from rheo_leads.jobfeed.presets import (
    ALLOWED_FILTERS,
    DEFAULT_PRESETS,
    PresetInvalid,
    validate_presets,
)


def preset(**changes: Any) -> dict[str, Any]:
    return {"name": "Synthetic search", "action": "search", "pages": 1, **changes}


def refresh(**params: Any) -> dict[str, Any]:
    return preset(purpose="refresh_only", params=params)


def numbered(count: int, **changes: Any) -> list[dict[str, Any]]:
    return [preset(name=f"Search {index}", **changes) for index in range(count)]


@pytest.mark.parametrize(
    ("presets", "code"),
    [
        ([], "preset_count_invalid"),
        (numbered(9), "preset_count_invalid"),
        ("not a list", "preset_count_invalid"),
        ([preset(extra=1)], "preset_keys_invalid"),
        (["not a preset"], "preset_keys_invalid"),
        ([preset(name="Same"), preset(name="Same")], "preset_name_invalid"),
        ([preset(name="  ")], "preset_name_invalid"),
        ([preset(name=None)], "preset_name_invalid"),
        ([preset(action="get")], "action_invalid"),
        ([preset(pages=0)], "pages_invalid"),
        ([preset(pages=6)], "pages_invalid"),
        ([preset(pages=True)], "pages_invalid"),
        (numbered(3, pages=3), "total_pages_invalid"),
        ([preset(params={"limit": 0})], "limit_invalid"),
        ([preset(params={"limit": 11})], "limit_invalid"),
        ([preset(params={"limit": "10"})], "limit_invalid"),
        ([preset(params={"access_token": "synthetic"})], "params_not_allowed"),
        ([preset(params={"organization_id": "org-0001"})], "params_not_allowed"),
        ([preset(params={"cursor": "synthetic-cursor"})], "params_not_allowed"),
        ([preset(params=["query"])], "params_not_allowed"),
        ([preset(purpose="archive")], "purpose_invalid"),
        ([preset(params={"proposals_max": 51})], "discovery_maximum_invalid"),
        ([preset(params={"proposals_max": -1})], "discovery_maximum_invalid"),
        ([preset(params={"proposals_max": False})], "discovery_maximum_invalid"),
        (
            [preset(action="smart_search", purpose="refresh_only")],
            "refresh_requires_search",
        ),
        ([refresh(proposals_min=50)], "refresh_minimum_invalid"),
        ([refresh(proposals_max=500)], "refresh_maximum_not_allowed"),
        ([refresh(proposals_max=50)], "refresh_maximum_not_allowed"),
        (
            [preset(action="smart_search", params={"mode": "top_rated"})],
            "mode_invalid",
        ),
    ],
)
def test_each_bound_refuses(presets: object, code: str) -> None:
    with pytest.raises(PresetInvalid) as refused:
        validate_presets(presets)
    assert refused.value.code == code


def test_the_total_page_case_is_nine_pages() -> None:
    presets = [preset(name="A", pages=5), preset(name="B", pages=4)]
    with pytest.raises(PresetInvalid) as refused:
        validate_presets(presets)
    assert refused.value.code == "total_pages_invalid"
    assert len(validate_presets([preset(name="A", pages=5), preset(name="B", pages=3)]))


def test_secrets_and_positions_are_not_allowed_filters() -> None:
    for name in ("access_token", "organization_id", "org_uid", "cursor", "next_cursor"):
        assert name not in ALLOWED_FILTERS


def test_bounds_are_inclusive() -> None:
    validated = validate_presets(
        [
            *numbered(1, pages=1, params={"limit": 1, "proposals_max": 0}),
            preset(name="Upper", pages=5, params={"limit": 10, "proposals_max": 50}),
            preset(
                name="Refresh",
                purpose="refresh_only",
                pages=2,
                params={"proposals_min": 51},
            ),
        ]
    )
    assert [p.pages for p in validated] == [1, 5, 2]
    assert len(validate_presets(numbered(8))) == 8


def test_defaults_are_applied() -> None:
    discover, smart, over = validate_presets(
        [
            preset(name="Discover"),
            preset(name="Smart", action="smart_search"),
            refresh(),
        ]
    )
    assert dict(discover.params) == {
        "limit": 10,
        "proposals_max": 50,
        "include_full_details": True,
    }
    assert discover.purpose == "discover"
    assert dict(smart.params) == {
        "limit": 10,
        "proposals_max": 50,
        "mode": "best_match",
    }
    assert dict(over.params) == {
        "limit": 10,
        "proposals_min": 51,
        "include_full_details": False,
    }
    assert "proposals_max" not in over.params


def test_explicit_values_are_kept() -> None:
    (validated,) = validate_presets(
        [preset(params={"include_full_details": False, "query": "synthetic"})]
    )
    assert validated.params["include_full_details"] is False
    assert validated.params["query"] == "synthetic"


def test_validated_params_are_read_only() -> None:
    with pytest.raises(TypeError):
        DEFAULT_PRESETS[0].params["limit"] = 1  # type: ignore[index]


def test_default_presets() -> None:
    best, recent, over = DEFAULT_PRESETS
    assert (best.name, best.action, best.pages, best.purpose) == (
        "Best Matches",
        "smart_search",
        1,
        "discover",
    )
    assert best.params["mode"] == "best_match"
    assert (recent.name, recent.action, recent.pages, recent.purpose) == (
        "Most Recent",
        "smart_search",
        5,
        "discover",
    )
    assert recent.params["mode"] == "most_recent"
    assert (over.action, over.pages, over.purpose) == ("search", 1, "refresh_only")
    assert over.params["proposals_min"] == 51
    assert over.params["sort"] == "recency"
    assert "proposals_max" not in over.params
    assert over.params["include_full_details"] is False
    assert sum(p.pages for p in DEFAULT_PRESETS) == 7
    rows = 0
    for p in DEFAULT_PRESETS:
        limit = p.params["limit"]
        assert isinstance(limit, int)
        rows += limit * p.pages
    assert rows == 70
    # The defaults pass the validator unchanged.
    assert (
        validate_presets(
            [
                {
                    "name": p.name,
                    "action": p.action,
                    "purpose": p.purpose,
                    "pages": p.pages,
                    "params": dict(p.params),
                }
                for p in DEFAULT_PRESETS
            ]
        )
        == DEFAULT_PRESETS
    )

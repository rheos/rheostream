"""Mapping contracts: missing values, clears, pointers and bounded transforms."""

import pytest
from rheo_leads.intake.mapping import (
    MISSING,
    MappingRefused,
    apply_mapping,
    resolve_identity,
    transform_value,
    value_at,
)


def rule(**changes: object) -> dict[str, object]:
    return {
        "target": "message",
        "source_path": "/message",
        "transform": "trim",
        "transform_arg": None,
        "required": False,
        "clear_on_null": True,
        **changes,
    }


def test_absent_null_and_value_remain_distinct() -> None:
    assert apply_mapping([rule()], {}, source_kind="json") == []
    cleared = apply_mapping([rule()], {"message": None}, source_kind="json")
    assert len(cleared) == 1
    assert (cleared[0].value_kind, cleared[0].value_text) == ("cleared", None)
    assert (
        apply_mapping(
            [rule(clear_on_null=False)], {"message": None}, source_kind="json"
        )
        == []
    )
    value = apply_mapping(
        [rule()], {"message": "  Example inquiry  "}, source_kind="json"
    )
    assert (value[0].value_kind, value[0].value_text) == ("value", "Example inquiry")
    assert (value[0].source_path, value[0].transform) == ("/message", "trim")


@pytest.mark.parametrize("payload", [{}, {"message": None}])
def test_missing_required_target_names_target_without_source(payload: object) -> None:
    with pytest.raises(MappingRefused, match="^message$"):
        apply_mapping(
            [rule(required=True, clear_on_null=False)], payload, source_kind="json"
        )


def test_transform_refusal_never_echoes_source() -> None:
    with pytest.raises(MappingRefused, match="^message$"):
        apply_mapping(
            [rule(transform="datetime")],
            {"message": "private invalid value"},
            source_kind="json",
        )


@pytest.mark.parametrize(
    ("transform", "value", "arg", "expected"),
    [
        ("trim", " a ", None, "a"),
        ("lower", " A ", None, "a"),
        ("email_normalize", " A@EXAMPLE.COM ", None, "a@example.com"),
        ("phone_normalize", " (555) 0101 ", None, "5550101"),
        ("datetime", "2026-10-05", "%Y-%m-%d", "2026-10-05T00:00:00+00:00"),
        ("const", None, "fixed", "fixed"),
    ],
)
def test_closed_transform_set(
    transform: str, value: object, arg: str | None, expected: str
) -> None:
    assert transform_value(value, transform, arg) == expected


def test_constant_needs_no_source_and_csv_uses_literal_column_name() -> None:
    constant = apply_mapping(
        [rule(transform="const", transform_arg="inquiry")], {}, source_kind="json"
    )
    assert constant[0].value_text == "inquiry"
    csv = apply_mapping(
        [rule(source_path="message")], {"message": " hello "}, source_kind="csv"
    )
    assert csv[0].value_text == "hello"


def test_json_pointer_escapes_and_arrays() -> None:
    payload = {"a/b": {"~key": ["first", None]}}
    assert value_at(payload, "/a~1b/~0key/0", "json") == "first"
    assert value_at(payload, "/a~1b/~0key/1", "json") is None
    assert value_at(payload, "/a~1b/~0key/2", "json") is MISSING
    assert value_at(payload, "/a~1b/~0key/01", "json") is MISSING
    with pytest.raises(MappingRefused):
        value_at(payload, "/a~2b", "json")


def test_identity_paths_share_addressing_and_preserve_missing() -> None:
    fields = resolve_identity(
        {"event_id_path": "/meta/id", "subject_id_path": "/subject"},
        {"meta": {"id": "sample-event"}, "subject": None},
        source_kind="json",
    )
    assert fields["event_id_path"] == "sample-event"
    assert fields["subject_id_path"] is None
    assert fields["occurred_at_path"] is MISSING
    assert (
        resolve_identity(
            {"event_id_path": "Event ID"}, {"Event ID": "row-1"}, source_kind="csv"
        )["event_id_path"]
        == "row-1"
    )


def test_unknown_transform_and_non_scalar_values_refuse() -> None:
    with pytest.raises(MappingRefused, match="^unknown_target$"):
        apply_mapping([rule(target="unconfigured.fact")], {}, source_kind="json")
    with pytest.raises(ValueError, match="unknown_transform"):
        transform_value("a", "execute", None)
    with pytest.raises(ValueError, match="scalar_required"):
        transform_value({"nested": "value"}, "trim", None)

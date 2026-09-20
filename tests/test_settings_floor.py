"""The policy floor: all four comparators, both directions, and the read-path clamp.

Seams: ``validate_override()`` (its four refusal states, and a loosening override
refused naming the key) and ``resolve()`` (a tightening row applied, a loosening row
clamped on read, and a row that predates a tightened deployment value neutralised
without being deleted). ``union`` and ``and`` have no production key in
this run, so the harness keys carry them; ``subset`` now has production keys
(``runtime.allowed_runtimes``, ``runtime.allowed_models``); ``min`` is also proved on
the production key ``identity.token_max_days.cli``, the one criterion 69 quotes.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from harness import isolate_rheo_environment
from harness.settings_keys import (
    HARNESS_FLOOR_AND,
    HARNESS_FLOOR_MIN,
    HARNESS_FLOOR_SUBSET,
    HARNESS_FLOOR_UNION,
    HARNESS_RETENTION_DAYS,
    register_harness_keys,
)
from rheo_core.settings import (
    REFUSAL_STATES,
    Floor,
    FrozenValue,
    Scope,
    SettingAccepted,
    SettingRefusal,
    ValueType,
    apply_floor,
    encode_text,
    is_looser,
    resolve,
    spec_for,
    validate_override,
)

WORKSPACE = UUID("018f0000-0000-7000-8000-000000000001")
TOKEN_DAYS = "identity.token_max_days.cli"


class Rows:
    def __init__(self, workspace: dict[str, str]) -> None:
        self.workspace = workspace

    def workspace_overrides(self, workspace_id: UUID) -> Mapping[str, str]:
        return self.workspace

    def member_overrides(
        self, workspace_id: UUID, account_id: UUID
    ) -> Mapping[str, str]:
        return {}


@dataclass(frozen=True)
class Case:
    key: str
    variable: str
    deployment_raw: str
    deployment: object
    tighter: object
    looser: object
    effective_tighter: object
    clamped_looser: object


CASES = [
    Case(HARNESS_FLOOR_MIN, "RHEO__harness__floor_min", "50", 50, 40, 60, 40, 50),
    Case(
        HARNESS_FLOOR_UNION,
        "RHEO__harness__floor_union",
        "a,b",
        ["a", "b"],
        ["a", "b", "c"],
        ["a"],
        ["a", "b", "c"],
        ["a", "b"],
    ),
    Case(
        HARNESS_FLOOR_SUBSET,
        "RHEO__harness__floor_subset",
        "read,draft,mutate",
        ["read", "draft", "mutate"],
        ["read"],
        ["read", "external"],
        ["read"],
        ["read"],
    ),
    Case(
        HARNESS_FLOOR_AND,
        "RHEO__harness__floor_and",
        "false",
        False,
        False,
        True,
        False,
        False,
    ),
    Case(TOKEN_DAYS, "RHEO__identity__token_max_days__cli", "60", 60, 30, 61, 30, 60),
]
CASE_IDS = [c.key for c in CASES]


@pytest.fixture
def data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "data"
    isolate_rheo_environment(monkeypatch, root)
    register_harness_keys()
    return root


def encoded(key: str, value: object) -> str:
    return encode_text(spec_for(key), value)  # type: ignore[arg-type]


# --- each comparator, both directions, at the write path and the read path ------------


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_tightening_override_is_accepted_and_applied(
    data_root: Path, monkeypatch: pytest.MonkeyPatch, case: Case
) -> None:
    monkeypatch.setenv(case.variable, case.deployment_raw)
    deployment_value = resolve()[case.key]
    assert deployment_value == case.deployment

    verdict = validate_override(case.key, case.tighter, deployment_value)
    assert isinstance(verdict, SettingAccepted), verdict
    assert verdict.encoded == encoded(case.key, case.tighter)

    rows = Rows({case.key: verdict.encoded})
    assert (
        resolve(workspace_id=WORKSPACE, source=rows)[case.key] == case.effective_tighter
    )


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_loosening_override_is_refused_naming_the_key(
    data_root: Path, monkeypatch: pytest.MonkeyPatch, case: Case
) -> None:
    monkeypatch.setenv(case.variable, case.deployment_raw)
    verdict = validate_override(case.key, case.looser, resolve()[case.key])
    assert isinstance(verdict, SettingRefusal), verdict
    assert verdict.state == "setting_floor_violation"
    assert verdict.key == case.key
    assert case.key in verdict.detail


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_read_path_clamps_a_stored_loosening_row(
    data_root: Path, monkeypatch: pytest.MonkeyPatch, case: Case
) -> None:
    monkeypatch.setenv(case.variable, case.deployment_raw)
    stored = encoded(case.key, case.looser)
    rows = Rows({case.key: stored})
    assert resolve(workspace_id=WORKSPACE, source=rows)[case.key] == case.clamped_looser
    assert rows.workspace == {case.key: stored}  # the row is left in place


def test_tightened_deployment_value_neutralises_an_older_row(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 69's third clause on the quoted key, ``identity.token_max_days.cli``.

    A row accepted against the package default is neutralised on read once the
    deployment value tightens beneath it, and the row itself is left in place.
    """
    assert resolve()[TOKEN_DAYS] == 90  # the package default
    verdict = validate_override(TOKEN_DAYS, 60, resolve()[TOKEN_DAYS])
    assert isinstance(verdict, SettingAccepted)
    rows = Rows({TOKEN_DAYS: verdict.encoded})
    assert resolve(workspace_id=WORKSPACE, source=rows)[TOKEN_DAYS] == 60

    monkeypatch.setenv("RHEO__identity__token_max_days__cli", "30")
    assert resolve(workspace_id=WORKSPACE, source=rows)[TOKEN_DAYS] == 30
    assert rows.workspace == {TOKEN_DAYS: "60"}
    # And the same write would now be refused.
    refusal = validate_override(TOKEN_DAYS, 60, resolve()[TOKEN_DAYS])
    assert isinstance(refusal, SettingRefusal)
    assert refusal.state == "setting_floor_violation"
    assert TOKEN_DAYS in refusal.detail


# --- the four refusal states ----------------------------------------------------------


def test_refusal_states_are_exactly_the_four_criterion_69_names() -> None:
    assert REFUSAL_STATES == {
        "setting_undeclared",
        "setting_scope",
        "setting_type",
        "setting_floor_violation",
    }


def test_setting_undeclared(data_root: Path) -> None:
    verdict = validate_override("nope.key", 1, 1)
    assert isinstance(verdict, SettingRefusal)
    assert (verdict.state, verdict.key) == ("setting_undeclared", "nope.key")


def test_setting_scope_for_a_deployment_key(data_root: Path) -> None:
    verdict = validate_override("profile", "test", "test")
    assert isinstance(verdict, SettingRefusal)
    assert verdict.state == "setting_scope"
    verdict = validate_override("storage.pool_cache_size", 8, 32)
    assert isinstance(verdict, SettingRefusal)
    assert verdict.state == "setting_scope"


def test_setting_scope_for_the_wrong_row_kind(data_root: Path) -> None:
    member_row = validate_override(HARNESS_FLOOR_MIN, 10, 100, scope=Scope.MEMBER)
    assert isinstance(member_row, SettingRefusal)
    assert member_row.state == "setting_scope"
    as_deployment = validate_override(
        HARNESS_FLOOR_MIN, 10, 100, scope=Scope.DEPLOYMENT
    )
    assert isinstance(as_deployment, SettingRefusal)
    assert as_deployment.state == "setting_scope"


@pytest.mark.parametrize(
    ("key", "value", "deployment_value"),
    [
        (HARNESS_FLOOR_MIN, "10", 100),
        (HARNESS_FLOOR_MIN, True, 100),  # a bool is not an int setting
        (HARNESS_FLOOR_AND, 1, False),  # an int is not a bool setting
        (HARNESS_FLOOR_UNION, "a,b", ["a"]),
        (HARNESS_FLOOR_UNION, ["a", 1], ["a"]),
        (HARNESS_FLOOR_MIN, None, 100),
    ],
)
def test_setting_type(
    data_root: Path, key: str, value: object, deployment_value: object
) -> None:
    verdict = validate_override(key, value, deployment_value)
    assert isinstance(verdict, SettingRefusal), verdict
    assert (verdict.state, verdict.key) == ("setting_type", key)


def test_a_write_outside_a_bounded_keys_range_is_refused_setting_type(
    data_root: Path,
) -> None:
    """The write is the third layer a bounded ``int`` key is held at, and the last one.

    Folded into ``setting_type`` rather than given a fifth state:
    :data:`REFUSAL_STATES` is what public criterion 69 quotes and the test above pins
    it at exactly four, so a new state here would break a published contract to say
    something ``setting_type`` already says for a ``choices`` key.

    A bounded key is **not** a floored key, which is why the deployment value below
    does not change the outcome: a floor is the deployment's current position and moves
    with it, while these bounds are fixed at declaration and no layer may leave them.
    """
    for value in (0, 11):
        verdict = validate_override(HARNESS_RETENTION_DAYS, value, 7)
        assert isinstance(verdict, SettingRefusal), verdict
        assert (verdict.state, verdict.key) == ("setting_type", HARNESS_RETENTION_DAYS)
        assert "the declared range 1-10" in verdict.detail
    for value in (1, 10):
        accepted = validate_override(HARNESS_RETENTION_DAYS, value, 7)
        assert isinstance(accepted, SettingAccepted), accepted
        assert accepted.value == value
    assert spec_for(HARNESS_RETENTION_DAYS).floor is None


def test_accepted_override_carries_its_storage_encoding(data_root: Path) -> None:
    verdict = validate_override(HARNESS_FLOOR_UNION, ["a", "b"], ["a"])
    assert isinstance(verdict, SettingAccepted)
    assert verdict.value == ("a", "b")
    assert verdict.encoded == "a,b"
    assert verdict.value_type is ValueType.STR_LIST
    assert validate_override(HARNESS_FLOOR_AND, False, True) == SettingAccepted(
        HARNESS_FLOOR_AND, False, "false", ValueType.BOOL
    )


# --- the comparators as pure functions ----------------------------------------------


@pytest.mark.parametrize(
    ("floor", "deployment", "override", "effective", "looser"),
    [
        (Floor.MIN, 50, 40, 40, False),
        (Floor.MIN, 50, 50, 50, False),
        (Floor.MIN, 50, 60, 50, True),
        (Floor.UNION, ("a", "b"), ("a", "b", "c"), ("a", "b", "c"), False),
        (Floor.UNION, ("a", "b"), ("b", "a"), ("a", "b"), False),
        (Floor.UNION, ("a", "b"), ("b", "c"), ("a", "b", "c"), True),
        (Floor.UNION, ("a", "b"), (), ("a", "b"), True),
        (Floor.SUBSET, ("r", "d", "m"), ("d",), ("d",), False),
        (Floor.SUBSET, ("r", "d", "m"), ("m", "r", "d"), ("r", "d", "m"), False),
        (Floor.SUBSET, ("r", "d", "m"), ("d", "x"), ("d",), True),
        (Floor.SUBSET, ("r", "d", "m"), (), (), False),
        (Floor.AND, False, False, False, False),
        (Floor.AND, False, True, False, True),
        (Floor.AND, True, False, False, False),
        (Floor.AND, True, True, True, False),
    ],
)
def test_comparators(
    floor: Floor,
    deployment: FrozenValue,
    override: FrozenValue,
    effective: FrozenValue,
    looser: bool,
) -> None:
    assert apply_floor(floor, deployment, override) == effective
    assert is_looser(floor, deployment, override) is looser

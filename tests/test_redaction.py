"""The redaction contract's pieces that need no database (issue #130).

Four seams, each driven directly:

1. **The tier policy over real settings resolution**: ``redaction.internal_purposes``
   and ``redaction.contact_points_to_model`` resolved through ``resolve()`` with the
   deployment layer and a workspace row, so the ``subset`` and ``and`` floors are the
   resolver's own and not re-implemented here.
2. **Masking**: every pattern's matches and, as hard, its non-matches. A mask that
   fires on a timestamp or a record reference would corrupt every tool result.
3. **The default record renderer**: per purpose, the fail-closed undeclared field,
   and ``restricted`` withheld whatever the allowance says.
4. **The tool-output renderer**: an untiered model is exactly its JSON dump, a tiered
   one loses what the policy does not allow and every unmarked field, nested models
   and lists included.

Every contact value is fictional: ``example.com``/``example.org`` addresses and
555-01xx numbers.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Optional, Union
from uuid import UUID

import pytest
from harness import isolate_rheo_environment
from pydantic import (
    BaseModel,
    ConfigDict,
    RootModel,
    computed_field,
    field_serializer,
)
from rheo_contracts import ContextPurpose, RecordRef
from rheo_core.redaction.masking import (
    EMAIL_MASK,
    MASK_TOKENS,
    PHONE_MASK,
    SECRET_MASK,
    mask_contact_values,
    mask_for_model,
    mask_secret_references,
    mask_tokens_in,
)
from rheo_core.redaction.policy import (
    CONTACT_POINTS_KEY,
    INTERNAL_PURPOSES_KEY,
    TierPolicy,
    exclude_types_spec,
)
from rheo_core.redaction.render import RenderedResult, render_output, render_record
from rheo_core.redaction.tiers import SensitivityTier, Tiered, model_field_tiers
from rheo_core.settings import (
    ResolvedSettings,
    encode_text,
    register,
    resolve,
    spec_for,
)

WORKSPACE = UUID("018f0000-0000-7000-8000-0000000001a0")
EMAIL = "pat.rivera@example.com"
PHONE = "250-555-0142"

PUBLIC = SensitivityTier.PUBLIC
INTERNAL = SensitivityTier.INTERNAL
RESTRICTED = SensitivityTier.RESTRICTED


class Rows:
    def __init__(self, workspace: Mapping[str, str]) -> None:
        self.workspace = dict(workspace)

    def workspace_overrides(self, workspace_id: UUID) -> Mapping[str, str]:
        return self.workspace

    def member_overrides(
        self, workspace_id: UUID, account_id: UUID
    ) -> Mapping[str, str]:
        return {}


@pytest.fixture(autouse=True)
def data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "data"
    isolate_rheo_environment(monkeypatch, root)
    return root


def _settings(**rows: object) -> ResolvedSettings:
    encoded = {
        key: encode_text(spec_for(key), value)  # type: ignore[arg-type]
        for key, value in rows.items()
    }
    return resolve(workspace_id=WORKSPACE, source=Rows(encoded))


def _policy(
    purpose: ContextPurpose = ContextPurpose.INTERNAL_ANALYSIS,
    settings: ResolvedSettings | None = None,
) -> TierPolicy:
    return TierPolicy.from_settings(purpose, settings or _settings())


# --- 1. the policy over the resolver --------------------------------------------------


@pytest.mark.parametrize(
    ("purpose", "internal"),
    [
        (ContextPurpose.RESPOND, True),
        (ContextPurpose.FOLLOW_UP, True),
        (ContextPurpose.INTERNAL_ANALYSIS, True),
        (ContextPurpose.SHARE_WITH_REFERRAL, False),
    ],
)
def test_the_package_default_admits_internal_for_three_purposes(
    purpose: ContextPurpose, internal: bool
) -> None:
    policy = _policy(purpose)
    assert policy.allows(PUBLIC) is True
    assert policy.allows(INTERNAL) is internal
    assert policy.allows(RESTRICTED) is False


def test_a_workspace_may_narrow_the_internal_purposes() -> None:
    settings = _settings(**{INTERNAL_PURPOSES_KEY: ["respond"]})
    assert _policy(ContextPurpose.RESPOND, settings).allows(INTERNAL) is True
    assert _policy(ContextPurpose.FOLLOW_UP, settings).allows(INTERNAL) is False


def test_a_workspace_row_cannot_add_a_purpose_the_operator_did_not_allow() -> None:
    """``subset``: a stored row naming a purpose outside the deployment's list is
    clamped on read, so ``share_with_referral`` never gains ``internal`` fields."""
    settings = _settings(**{INTERNAL_PURPOSES_KEY: ["respond", "share_with_referral"]})
    assert settings.get_list(INTERNAL_PURPOSES_KEY) == ["respond"]
    assert _policy(ContextPurpose.SHARE_WITH_REFERRAL, settings).allows(INTERNAL) is (
        False
    )


def test_the_operator_can_withdraw_internal_from_every_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RHEO__redaction__internal_purposes", "")
    settings = _settings()
    for purpose in ContextPurpose:
        assert _policy(purpose, settings).allows(INTERNAL) is False


@pytest.mark.parametrize(
    ("deployment", "workspace", "expected"),
    [
        (None, None, False),  # the package default
        (None, True, False),  # a workspace cannot switch on what the operator did not
        ("true", None, True),  # the operator's value stands
        ("true", True, True),
        ("true", False, False),  # a workspace may keep it off
        ("false", True, False),
    ],
)
def test_the_contact_point_allowance_is_an_and_floor(
    monkeypatch: pytest.MonkeyPatch,
    deployment: str | None,
    workspace: bool | None,
    expected: bool,
) -> None:
    if deployment is not None:
        monkeypatch.setenv("RHEO__redaction__contact_points_to_model", deployment)
    rows = {} if workspace is None else {CONTACT_POINTS_KEY: workspace}
    settings = _settings(**rows)
    assert settings.get_bool(CONTACT_POINTS_KEY) is expected
    assert _policy(ContextPurpose.RESPOND, settings).contact_points_allowed is expected


def test_the_allowance_applies_to_every_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Robin's decision in the #130 review: with both floors true, contact values are
    released whatever the purpose, not for ``respond`` only."""
    monkeypatch.setenv("RHEO__redaction__contact_points_to_model", "true")
    settings = _settings()
    for purpose in ContextPurpose:
        policy = _policy(purpose, settings)
        assert policy.contact_points_allowed is True, purpose
        assert mask_for_model(f"mail {EMAIL}", policy) == f"mail {EMAIL}", purpose


def test_without_the_allowance_every_purpose_is_masked() -> None:
    for purpose in ContextPurpose:
        policy = _policy(purpose)
        assert policy.contact_points_allowed is False, purpose
        assert mask_for_model(f"mail {EMAIL}", policy) == f"mail {EMAIL_MASK}"


def test_the_allowance_never_releases_a_restricted_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RHEO__redaction__contact_points_to_model", "true")
    policy = _policy(ContextPurpose.RESPOND)
    assert policy.contact_points_allowed is True
    assert policy.allows(RESTRICTED) is False
    assert policy.ceiling is RESTRICTED


def test_a_policy_that_needs_no_setting_reads_none() -> None:
    """The facade's laziness claim: ``public`` fields and text with no contact value
    or reference are answered without calling the settings source at all."""
    calls: list[int] = []

    def settings() -> ResolvedSettings:
        calls.append(1)
        return _settings()

    policy = TierPolicy(ContextPurpose.FOLLOW_UP, settings)
    assert policy.allows(PUBLIC) is True
    assert mask_for_model("nothing to mask here", policy) == "nothing to mask here"
    assert mask_for_model("key secret://env/X", policy) == f"key {SECRET_MASK}"
    assert calls == []
    assert mask_for_model(f"write to {EMAIL}", policy) == f"write to {EMAIL_MASK}"
    assert policy.allows(INTERNAL) is True
    assert calls == [1]


# --- 2. masking -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"mail {EMAIL} today", f"mail {EMAIL_MASK} today"),
        ("ops+alerts@sub.example.org.", f"{EMAIL_MASK}."),
        (f"call {PHONE}", f"call {PHONE_MASK}"),
        ("call 250.555.0199 now", f"call {PHONE_MASK} now"),
        ("call 250 555 0101", f"call {PHONE_MASK}"),
        ("(250) 555-0177", PHONE_MASK),
        ("+1 250 555 0123", PHONE_MASK),
        ("+44 20 5550 0188", PHONE_MASK),
        ("+12505550123", PHONE_MASK),
    ],
)
def test_contact_values_are_masked(text: str, expected: str) -> None:
    assert mask_contact_values(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-25T02:40:00+00:00",
        "2026-09-25",
        "recallatron.memory:0190b7a2-3c4d-7e8f-9a0b-1c2d3e4f5a6b",
        "0190b7a2-3c4d-7e8f-9a0b-1c2d3e4f5a6b",
        "1727231400",
        "2505550123",  # a bare run of digits is an id or a count as often as a phone
        "version 1.2.3",
        "10.0.0.1",
        "the ratio was 250-555",
        "250-555-01234",
        "a+b@c",  # no top-level domain
        "user at example dot com",
        "+00:00",
        "+1 2",  # too few digits for a number
    ],
)
def test_non_contact_text_is_left_alone(text: str) -> None:
    assert mask_contact_values(text) == text


def test_secret_references_are_masked_even_with_the_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RHEO__redaction__contact_points_to_model", "true")
    policy = _policy(ContextPurpose.RESPOND)
    text = f"key secret://file/ws/018f/signing for {EMAIL}"
    assert mask_for_model(text, policy) == f"key {SECRET_MASK} for {EMAIL}"


def test_contact_values_are_masked_without_the_allowance() -> None:
    policy = _policy(ContextPurpose.RESPOND)
    assert (
        mask_for_model(f"{EMAIL} / {PHONE}", policy) == f"{EMAIL_MASK} / {PHONE_MASK}"
    )


# --- 3. the default record renderer ---------------------------------------------------

TIERS: Mapping[str, SensitivityTier] = {
    "title": PUBLIC,
    "organization": INTERNAL,
    "email": RESTRICTED,
}
RECORD: Mapping[str, object] = {
    "title": "Quarterly check-in",
    "organization": "Harbour Supply Co-op",
    "email": EMAIL,
    "notes": "an undeclared column",
}


def test_internal_analysis_gets_public_and_internal_and_never_restricted() -> None:
    rendered = render_record(RECORD, TIERS, _policy())
    assert rendered is not None
    assert rendered.text == (
        "title: Quarterly check-in\norganization: Harbour Supply Co-op"
    )
    assert rendered.tier is INTERNAL
    assert EMAIL not in rendered.text


def test_a_purpose_without_internal_gets_public_only() -> None:
    rendered = render_record(RECORD, TIERS, _policy(ContextPurpose.SHARE_WITH_REFERRAL))
    assert rendered is not None
    assert rendered.text == "title: Quarterly check-in"
    assert rendered.tier is PUBLIC


def test_an_undeclared_field_is_withheld() -> None:
    """The fail-closed choice where the design is silent: ``notes`` has no tier."""
    rendered = render_record(RECORD, TIERS, _policy())
    assert rendered is not None
    assert "undeclared" not in rendered.text


def test_the_allowance_does_not_release_a_restricted_field_in_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RHEO__redaction__contact_points_to_model", "true")
    rendered = render_record(RECORD, TIERS, _policy(ContextPurpose.RESPOND))
    assert rendered is not None
    assert EMAIL not in rendered.text


def test_a_contact_value_inside_a_public_field_is_masked() -> None:
    record = {"title": f"Call {PHONE} or mail {EMAIL}"}
    rendered = render_record(record, {"title": PUBLIC}, _policy())
    assert rendered is not None
    assert rendered.text == f"title: Call {PHONE_MASK} or mail {EMAIL_MASK}"


def test_a_record_with_nothing_allowed_renders_to_nothing() -> None:
    record = {"email": EMAIL, "organization": "Harbour Supply Co-op"}
    policy = _policy(ContextPurpose.SHARE_WITH_REFERRAL)
    assert render_record(record, TIERS, policy) is None


def test_a_none_value_is_left_out() -> None:
    rendered = render_record({"title": "t", "organization": None}, TIERS, _policy())
    assert rendered is not None
    assert rendered.text == "title: t"
    assert rendered.tier is PUBLIC


# --- 4. the tool-output renderer ------------------------------------------------------


class PlainItem(BaseModel):
    title: str
    count: int


class PlainResult(BaseModel):
    ref: str
    items: list[PlainItem]
    labels: dict[str, str]


class ContactPoint(BaseModel):
    kind: Annotated[str, Tiered(PUBLIC)]
    value: Annotated[str, Tiered(RESTRICTED)]


class PartyResult(BaseModel):
    ref: Annotated[str, Tiered(PUBLIC)]
    display_name: Annotated[str, Tiered(INTERNAL)]
    contact_points: Annotated[list[ContactPoint], Tiered(PUBLIC)]
    primary: Annotated[ContactPoint | None, Tiered(PUBLIC)]
    unmarked: str


def _party() -> PartyResult:
    return PartyResult(
        ref="relationships.party:018f0000-0000-7000-8000-00000000abcd",
        display_name="Pat Rivera",
        contact_points=[
            ContactPoint(kind="email", value=EMAIL),
            ContactPoint(kind="phone", value=PHONE),
        ],
        primary=ContactPoint(kind="email", value=EMAIL),
        unmarked="forgotten by the author",
    )


def test_an_untiered_output_is_exactly_its_json_dump() -> None:
    """Every model in release one: Recallatron's and the core's tools answer as
    before, down to the timestamps and references the masks must not touch."""
    result = PlainResult(
        ref="recallatron.memory:0190b7a2-3c4d-7e8f-9a0b-1c2d3e4f5a6b",
        items=[PlainItem(title="2026-09-25T02:40:00+00:00", count=3)],
        labels={"a": "b"},
    )
    assert render_output(result, _policy()).value == result.model_dump(mode="json")


def test_a_tiered_output_drops_restricted_and_unmarked_fields() -> None:
    rendered = render_output(_party(), _policy()).value
    assert rendered == {
        "ref": "relationships.party:018f0000-0000-7000-8000-00000000abcd",
        "display_name": "Pat Rivera",
        "contact_points": [{"kind": "email"}, {"kind": "phone"}],
        "primary": {"kind": "email"},
    }
    assert EMAIL not in str(rendered)
    assert PHONE not in str(rendered)


def test_a_tiered_output_drops_internal_for_a_purpose_without_it() -> None:
    rendered = render_output(
        _party(), _policy(ContextPurpose.SHARE_WITH_REFERRAL)
    ).value
    assert isinstance(rendered, dict)
    assert "display_name" not in rendered
    assert rendered["ref"].startswith("relationships.party:")  # type: ignore[union-attr]


def test_an_untiered_output_still_has_its_free_text_masked() -> None:
    result = PlainResult(
        ref="r",
        items=[PlainItem(title=f"ring {PHONE}", count=1)],
        labels={EMAIL: EMAIL},
    )
    rendered = render_output(result, _policy()).value
    assert rendered == {
        "ref": "r",
        "items": [{"title": f"ring {PHONE_MASK}", "count": 1}],
        "labels": {EMAIL_MASK: EMAIL_MASK},
    }


# --- 5. the review's spellings, shapes and patterns (issue #130 cold review) ----------

R = Tiered(RESTRICTED)


class NoneableMarked(BaseModel):
    title: Annotated[str, Tiered(PUBLIC)]
    contact: Annotated[str, R] | None = None


class OptionalMarked(BaseModel):
    title: Annotated[str, Tiered(PUBLIC)]
    contact: Optional[Annotated[str, R]] = None  # noqa: UP045 - the spelling under test


class UnionArmMarked(BaseModel):
    title: Annotated[str, Tiered(PUBLIC)]
    contact: Union[int, Annotated[str, R]]  # noqa: UP007 - the spelling under test


class ElementMarked(BaseModel):
    title: Annotated[str, Tiered(PUBLIC)]
    contact: list[Annotated[str, R]]


class ValueMarked(BaseModel):
    title: Annotated[str, Tiered(PUBLIC)]
    contact: dict[str, Annotated[str, R]]


class InnerOptionalMarked(BaseModel):
    title: Annotated[str, Tiered(PUBLIC)]
    contact: Annotated[str | None, R] = None


@pytest.mark.parametrize(
    ("model", "contact"),
    [
        (NoneableMarked, EMAIL),
        (OptionalMarked, EMAIL),
        (UnionArmMarked, EMAIL),
        (ElementMarked, [EMAIL]),
        (ValueMarked, {"primary": EMAIL}),
        (InnerOptionalMarked, EMAIL),
    ],
    ids=["pipe-none", "optional", "union-arm", "list-element", "dict-value", "inner"],
)
def test_a_marker_in_any_spelling_withholds_the_field(
    model: type[BaseModel], contact: object
) -> None:
    rendered = render_output(model(title="t", contact=contact), _policy()).value
    assert rendered == {"title": "t"}
    assert model_field_tiers(model)["contact"] is RESTRICTED


class Keyed(BaseModel):
    by_id: dict[UUID, "KeyedInner"]
    by_int: dict[int, "KeyedInner"]
    by_kind: dict["Kind", "KeyedInner"]


class Kind(StrEnum):
    A = "a"


class KeyedInner(BaseModel):
    email: Annotated[str, R]
    note: Annotated[str, Tiered(PUBLIC)]


Keyed.model_rebuild()


def test_non_string_dict_keys_are_still_tier_filtered() -> None:
    key = UUID("018f0000-0000-7000-8000-00000000beef")
    inner = KeyedInner(email=EMAIL, note="n")
    rendered = render_output(
        Keyed(by_id={key: inner}, by_int={3: inner}, by_kind={Kind.A: inner}),
        _policy(),
    ).value
    assert rendered == {
        "by_id": {str(key): {"note": "n"}},
        "by_int": {"3": {"note": "n"}},
        "by_kind": {"a": {"note": "n"}},
    }


class WithSets(BaseModel):
    emails: set[str]
    frozen: frozenset[str]
    nested: list[set[str]]


def test_set_members_are_masked() -> None:
    rendered = render_output(
        WithSets(emails={EMAIL}, frozen=frozenset({PHONE}), nested=[{EMAIL}]),
        _policy(),
    ).value
    assert rendered == {
        "emails": [EMAIL_MASK],
        "frozen": [PHONE_MASK],
        "nested": [[EMAIL_MASK]],
    }


@dataclass
class PointData:
    kind: Annotated[str, Tiered(PUBLIC)]
    value: Annotated[str, R]


@dataclass
class PlainData:
    note: str


class WithDataclasses(BaseModel):
    point: PointData
    plain: PlainData


def test_a_dataclass_field_is_tiered_and_masked_like_a_model() -> None:
    rendered = render_output(
        WithDataclasses(
            point=PointData(kind="email", value=EMAIL),
            plain=PlainData(note=f"call {PHONE}"),
        ),
        _policy(),
    ).value
    assert rendered == {
        "point": {"kind": "email"},
        "plain": {"note": f"call {PHONE_MASK}"},
    }


class RootList(RootModel[list[KeyedInner]]):
    pass


class RootText(RootModel[str]):
    pass


def test_a_root_model_renders_its_root() -> None:
    assert render_output(
        RootList([KeyedInner(email=EMAIL, note="n")]), _policy()
    ).value == [{"note": "n"}]
    assert render_output(RootText(f"to {EMAIL}"), _policy()).value == f"to {EMAIL_MASK}"


class TieredWithExtras(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: Annotated[str, Tiered(PUBLIC)]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def computed(self) -> str:
        return "derived"


def test_extra_and_computed_fields_on_a_tiered_class_are_withheld() -> None:
    model = TieredWithExtras(title="t", leaked=EMAIL)  # type: ignore[call-arg]
    assert render_output(model, _policy()).value == {"title": "t"}


class UntieredWithExtras(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str


def test_extra_fields_on_an_untiered_class_are_masked() -> None:
    model = UntieredWithExtras(title="t", leaked=EMAIL)  # type: ignore[call-arg]
    assert render_output(model, _policy()).value == {"title": "t", "leaked": EMAIL_MASK}


class Serialized(BaseModel):
    inner: KeyedInner

    @field_serializer("inner")
    def _flatten(self, value: KeyedInner) -> dict[str, str]:
        return {"email": value.email, "extra": value.email}


def test_a_serializer_cannot_route_a_restricted_value_around_its_tier() -> None:
    rendered = render_output(
        Serialized(inner=KeyedInner(email=EMAIL, note="n")), _policy()
    )
    assert EMAIL not in str(rendered.value)


# --- exclusion on the output path -----------------------------------------------------

EXCL_MODULE = "excl_probe"


@pytest.fixture
def exclude_note(monkeypatch: pytest.MonkeyPatch) -> None:
    register(exclude_types_spec(EXCL_MODULE), origin=EXCL_MODULE)
    monkeypatch.setenv(f"RHEO__{EXCL_MODULE}__redaction__exclude_types", "note")


NOTE_REF = f"{EXCL_MODULE}.note:018f0000-0000-7000-8000-00000000abcd"
OTHER_REF = f"{EXCL_MODULE}.other:018f0000-0000-7000-8000-00000000abce"


class RefItem(BaseModel):
    ref: str
    title: str


class RefList(BaseModel):
    items: list[RefItem]
    see_also: list[str]
    anchor: RecordRef | None = None


def test_records_of_an_excluded_type_are_dropped_from_a_result(
    exclude_note: None,
) -> None:
    result = RefList(
        items=[
            RefItem(ref=NOTE_REF, title="hidden"),
            RefItem(ref=OTHER_REF, title="kept"),
        ],
        see_also=[NOTE_REF, OTHER_REF],
        anchor=RecordRef.parse(NOTE_REF),
    )
    rendered = render_output(result, _policy())
    assert rendered.withheld is False
    assert rendered.value == {
        "items": [{"ref": OTHER_REF, "title": "kept"}],
        "see_also": [OTHER_REF],
    }


def test_a_result_that_is_an_excluded_record_is_withheld(exclude_note: None) -> None:
    rendered = render_output(RefItem(ref=NOTE_REF, title="hidden"), _policy())
    assert rendered == RenderedResult(value=None, withheld=True)


def test_nothing_is_dropped_when_nothing_is_excluded() -> None:
    result = RefList(items=[RefItem(ref=NOTE_REF, title="kept")], see_also=[NOTE_REF])
    assert render_output(result, _policy()).value == result.model_dump(mode="json")


# --- the tightened patterns -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1-250-555-0100", PHONE_MASK),
        ("+1-250-555-0100", PHONE_MASK),
        ("tel:+1-250-555-0100", f"tel:{PHONE_MASK}"),
        ("250–555–0100", PHONE_MASK),
        ("josé@example.com", EMAIL_MASK),
        ("user@münchen.de", EMAIL_MASK),
        ("+33 6 12 34 56 78", PHONE_MASK),
    ],
)
def test_the_review_s_false_negatives_now_mask(text: str, expected: str) -> None:
    assert mask_contact_values(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "sizes 128 256 1024",
        "ids 100-200-3000",
        "192.168.100.1000",
        "+100 200 300",
        "score +12 345 678",
        "234-567-8901-2",
        "250-155-0100",  # an exchange starting 1 is not a North American number
        "150-555-0100",  # nor is an area code starting 1
        "250-555.0100",  # mixed separators
        "user@localhost",
    ],
)
def test_the_review_s_false_positives_are_left_alone(text: str) -> None:
    assert mask_contact_values(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SECRET://env/FOO", SECRET_MASK),
        ("Secret://file/ws/1/key", SECRET_MASK),
        ("secret://vault/a%2Fb", SECRET_MASK),
        ("see secret://vault/a%2Fb.", f"see {SECRET_MASK}."),
        ("(secret://env/X)", f"({SECRET_MASK})"),
    ],
)
def test_secret_references_are_masked_whole_in_any_case(
    text: str, expected: str
) -> None:
    assert mask_secret_references(text) == expected


def test_mask_tokens_are_found_anywhere_in_an_input() -> None:
    arguments = {"title": "t", "body": [f"x {EMAIL_MASK}"], "k": {PHONE_MASK: 1}}
    assert set(mask_tokens_in(arguments)) == {EMAIL_MASK, PHONE_MASK}
    assert list(mask_tokens_in({"title": "clean"})) == []
    assert set(MASK_TOKENS) == {EMAIL_MASK, PHONE_MASK, SECRET_MASK}

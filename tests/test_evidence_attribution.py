"""Which actors are a person speaking (spec Architecture, component 3 condition 4)."""

import pytest
from rheo_contracts.context import ActorKind
from rheo_core.evidence import attribution
from rheo_core.evidence.attribution import is_human_attributable


@pytest.mark.parametrize(
    ("actor_kind", "token_kind", "expected"),
    [
        ("account", None, True),
        ("token", "cli", True),
        ("token", "mcp", False),
        ("token", "runtime", False),
        ("token", "some_future_kind", False),
        ("token", None, False),
        ("system", None, False),
        ("operator", None, False),
        ("connection", None, False),
        ("not_an_actor_kind", None, False),
        (ActorKind.ACCOUNT, None, True),
        (ActorKind.TOKEN, "cli", True),
        (ActorKind.TOKEN, "mcp", False),
    ],
)
def test_the_attribution_rule(
    actor_kind: str, token_kind: str | None, expected: bool
) -> None:
    assert is_human_attributable(actor_kind, token_kind) is expected


def test_the_token_rule_reads_the_shared_allow_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The allow-list is imported, not restated: widening it widens attribution, and
    narrowing it narrows attribution, with no edit here."""
    monkeypatch.setattr(attribution, "PERSON_HELD_TOKEN_KINDS", frozenset({"mcp"}))
    assert is_human_attributable("token", "mcp") is True
    assert is_human_attributable("token", "cli") is False
    assert is_human_attributable("account", None) is True

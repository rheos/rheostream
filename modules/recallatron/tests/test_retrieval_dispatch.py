"""The strategy key's two fallbacks, decided from stored rows alone.

An absent row takes the package default; a present row that will not decode takes
``lexical`` whatever the default is. The cases run against a stand-in spec whose
default is *not* ``lexical``, written when the shipped default still was, so the two
branches must give two different answers whatever the shipped key says.
"""

import pytest
from rheo_core.settings import KeySpec, Scope, ValueType
from rheo_recallatron.configuration import (
    RETRIEVAL_STRATEGY_KEY,
    RETRIEVAL_STRATEGY_SPEC,
    STRATEGY_DENSE,
    STRATEGY_HYBRID,
    STRATEGY_LEXICAL,
)
from rheo_recallatron.retrieval import dispatch


@pytest.fixture
def hybrid_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dispatch,
        "RETRIEVAL_STRATEGY_SPEC",
        KeySpec(
            key=RETRIEVAL_STRATEGY_KEY,
            type=ValueType.STR,
            scope=Scope.WORKSPACE,
            floor=None,
            explicit_per_workspace=True,
            default=STRATEGY_HYBRID,
            choices=(STRATEGY_LEXICAL, STRATEGY_HYBRID),
        ),
    )


@pytest.mark.usefixtures("hybrid_default")
def test_an_absent_row_resolves_to_the_package_default() -> None:
    assert dispatch.strategy_name_in({}) == STRATEGY_HYBRID


@pytest.mark.usefixtures("hybrid_default")
@pytest.mark.parametrize("stored", ["not-a-strategy", "", "dense"])
def test_an_unparseable_row_resolves_to_lexical_not_the_default(stored: str) -> None:
    assert dispatch.strategy_name_in({RETRIEVAL_STRATEGY_KEY: stored}) == (
        STRATEGY_LEXICAL
    )


@pytest.mark.usefixtures("hybrid_default")
@pytest.mark.parametrize("stored", [STRATEGY_LEXICAL, STRATEGY_HYBRID])
def test_a_valid_row_resolves_to_itself(stored: str) -> None:
    assert dispatch.strategy_name_in({RETRIEVAL_STRATEGY_KEY: stored}) == stored


def test_the_shipped_key_offers_only_what_the_registry_serves() -> None:
    """All three strategies are offered, ``hybrid`` is the default, and every offered
    name is registered."""
    assert RETRIEVAL_STRATEGY_SPEC.choices == (
        STRATEGY_LEXICAL,
        STRATEGY_DENSE,
        STRATEGY_HYBRID,
    )
    assert RETRIEVAL_STRATEGY_SPEC.default == STRATEGY_HYBRID
    assert set(RETRIEVAL_STRATEGY_SPEC.choices) <= set(dispatch.STRATEGY_REGISTRY)
    assert dispatch.strategy_name_in({}) == STRATEGY_HYBRID

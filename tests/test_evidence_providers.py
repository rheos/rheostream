"""The extraction provider registry (spec Architecture, System Components item 6).

"A provider resolves" means ``resolve_provider() is not None``: the contract the
recording gate and the eligible-evidence service both read.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from rheo_core.evidence import providers
from rheo_core.evidence.providers import (
    FAKE_EXTRACTION_MARKER,
    FakeExtractionProvider,
    register_provider,
    resolve_provider,
)

PROVIDER_ENV = "RHEO__automatic_memory__extraction__provider"


@dataclass(frozen=True)
class _Item:
    item_id: str
    text: str


@dataclass(frozen=True)
class _Batch:
    items: tuple[_Item, ...]


def test_the_fake_resolves_under_the_test_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROVIDER_ENV, "fake")
    assert providers.configured_provider_name() == "fake"
    assert isinstance(resolve_provider(), FakeExtractionProvider)


def test_the_default_none_resolves_to_no_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PROVIDER_ENV, raising=False)
    assert providers.configured_provider_name() == "none"
    assert resolve_provider() is None


def test_an_unregistered_provider_name_resolves_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same shape as Recallatron's embedding registry test of that name,
    # modules/recallatron/tests/test_embedding_providers.py:191.
    monkeypatch.setenv(PROVIDER_ENV, "no_such_provider")
    assert resolve_provider() is None


def test_fake_outside_the_test_profile_resolves_to_none_and_never_registers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "current_profile", lambda: "production")
    monkeypatch.setattr(providers, "PROVIDERS", {})
    monkeypatch.setattr(providers, "_builtins_registered", False)
    monkeypatch.setenv(PROVIDER_ENV, "fake")
    assert resolve_provider() is None
    assert "fake" not in providers.providers()


def test_a_registered_variant_replaces_the_fake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """How later tests inject a faulting or malformed-output provider."""

    class _Faulting:
        @property
        def name(self) -> str:
            return "faulting"

        def extract(self, batch: Any) -> Mapping[str, object]:
            raise TimeoutError("synthetic transient fault")

    monkeypatch.setattr(providers, "PROVIDERS", {})
    monkeypatch.setattr(providers, "_builtins_registered", False)
    monkeypatch.setenv(PROVIDER_ENV, "fake")
    register_provider("fake", _Faulting())
    provider = resolve_provider()
    assert provider is not None and provider.name == "faulting"
    with pytest.raises(TimeoutError):
        provider.extract(_Batch(items=()))


def test_the_fake_is_deterministic_and_records_what_it_saw() -> None:
    fake = FakeExtractionProvider()
    marked = f"{FAKE_EXTRACTION_MARKER} The team prefers Thursday releases."
    batch = _Batch(items=(_Item("u1", marked), _Item("u2", "nothing to remember")))
    first = fake.extract(batch)
    assert first == {
        "items": [
            {
                "item": "u1",
                "memory": {
                    "kind": "note",
                    "title": marked,
                    "body": marked,
                    "confidence": 1.0,
                },
            },
            {"item": "u2", "memory": None},
        ]
    }
    assert fake.extract(batch) == first
    assert fake.calls == [batch, batch]
    assert fake.name == "fake"

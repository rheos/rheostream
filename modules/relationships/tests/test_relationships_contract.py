"""Distribution, normalization, references and model sensitivity contracts."""

import ast
from importlib.metadata import entry_points
from pathlib import Path

import pytest
from pydantic import ValidationError
from rheo_core.redaction.tiers import SensitivityTier, type_tiers
from rheo_relationships import MANIFEST
from rheo_relationships.contracts import PartyInput, PartyOutput
from rheo_relationships.normalization import normalize


def test_relationships_entry_point_and_restricted_contacts() -> None:
    found = next(
        ep for ep in entry_points(group="rheo.modules") if ep.name == "relationships"
    )
    assert found.load() is MANIFEST
    assert MANIFEST.dependencies == ()
    assert MANIFEST.web is None
    assert MANIFEST.sensitivity["party"]["contact_points"] == SensitivityTier.RESTRICTED
    assert type_tiers(PartyOutput)["contact_points"] == SensitivityTier.RESTRICTED


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("email", " Person+tag@EXAMPLE.COM ", "person+tag@example.com"),
        ("phone", "555-0100", "5550100"),
        ("url", "https://EXAMPLE.ORG/path", "example.org"),
        ("external_subject", " Subject-A ", "Subject-A"),
        ("name", " Example   Person ", "example person"),
    ],
)
def test_relationships_normalization(kind: str, value: str, expected: str) -> None:
    assert normalize(kind, value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "bad",
        "other.party:00000000-0000-7000-8000-000000000001",
        "relationships.merge_record:00000000-0000-7000-8000-000000000001",
    ],
)
def test_relationships_typed_refs_refuse_wrong_targets(value: str) -> None:
    with pytest.raises(ValidationError):
        PartyInput.model_validate({"ref": value})


def test_relationships_has_no_other_domain_import() -> None:
    source = Path(__file__).parents[1] / "src"
    for path in source.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(
                name.startswith(("rheo_leads", "rheo_recallatron")) for name in names
            )

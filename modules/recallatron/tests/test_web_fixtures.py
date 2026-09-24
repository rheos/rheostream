"""The response-shape drift guard for the web package's shared JSON fixtures.

The screens' TypeScript guards are proven against ``modules/recallatron/web/fixtures/``,
and the generated OpenAPI types cannot stand in for them: codegen runs with no module
installed, so no Recallatron operation is in the generated client. These fixtures are
the one place the two languages meet, so this file holds the Python half of that
agreement.

Each **result** fixture is the ``result`` object of a successful operation envelope. It
must validate against the Python output model the operation declares, and its keys must
equal that model's fields at every level. A fixture carrying a field the model has since
renamed would still pass a TypeScript guard written from it, which is the drift this
file exists to catch. The models forbid extra keys, so validation alone refuses an added
key; the key comparison is what names the mismatch.

Each **refusal** fixture is a whole envelope, because a refusal has no ``result`` to
stand alone: ``{"state", "operation_id", "error": {"error_code", "error_text"}}``, the
shape ``apps/core``'s ``envelope()`` answers with. Its ``error_code`` must be the
module's own refusal constant rather than a string that happens to match today.

Every JSON file in the directory must be named below, so a fixture added without a model
to validate against fails here instead of going unchecked.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from rheo_recallatron.contracts import EntityItem, EntityList
from rheo_recallatron.dedup import DedupCandidates
from rheo_recallatron.embedding.operations import EmbeddingCoverageReport
from rheo_recallatron.operations import MemoryItem, ReadWindow, RecallResult
from rheo_recallatron.refusals import REFERENCE_SCAN_LIMIT, WINDOW_SCAN_LIMIT

_FIXTURES = Path(__file__).resolve().parents[1] / "web" / "fixtures"

_RESULT_FIXTURES: dict[str, type[BaseModel]] = {
    "recall-populated.json": RecallResult,
    "recall-empty-hybrid.json": RecallResult,
    "recall-empty-degraded.json": RecallResult,
    "recall-empty-lexical.json": RecallResult,
    "recall-empty-dense.json": RecallResult,
    "recall-empty-dense-degraded.json": RecallResult,
    "read-window.json": ReadWindow,
    "read-empty.json": ReadWindow,
    "entity-list.json": EntityList,
    "entity-item.json": EntityItem,
    "memory-item.json": MemoryItem,
    "dedup-pairs.json": DedupCandidates,
    "dedup-empty.json": DedupCandidates,
    "coverage.json": EmbeddingCoverageReport,
    "coverage-no-provider.json": EmbeddingCoverageReport,
}

_REFUSAL_FIXTURES: dict[str, str] = {
    "entity-list-refused-budget.json": REFERENCE_SCAN_LIMIT,
    "read-refused-window.json": WINDOW_SCAN_LIMIT,
    "read-refused-budget.json": REFERENCE_SCAN_LIMIT,
}

_ENVELOPE_KEYS = frozenset({"state", "operation_id", "error"})
_ERROR_KEYS = frozenset({"error_code", "error_text"})


def _load(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _key_paths(value: object, path: str = "") -> set[str]:
    """Every object key in ``value``, as a dotted path, with list indices collapsed.

    Collapsing indices compares the *shape* of each list entry rather than its
    position, so a two-item fixture and its one-item dump would still disagree on a
    renamed field in either entry.
    """
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            here = f"{path}.{key}" if path else key
            paths.add(here)
            paths |= _key_paths(child, here)
    elif isinstance(value, list):
        for child in value:
            paths |= _key_paths(child, f"{path}[]")
    return paths


def test_every_fixture_is_named_here() -> None:
    on_disk = {path.name for path in _FIXTURES.glob("*.json")}
    named = set(_RESULT_FIXTURES) | set(_REFUSAL_FIXTURES)
    assert on_disk == named, (
        f"unnamed fixtures: {sorted(on_disk - named)}; "
        f"missing fixtures: {sorted(named - on_disk)}"
    )


@pytest.mark.parametrize(
    ("name", "model"), sorted(_RESULT_FIXTURES.items()), ids=sorted(_RESULT_FIXTURES)
)
def test_result_fixture_matches_its_output_model(
    name: str, model: type[BaseModel]
) -> None:
    raw = _load(name)
    assert isinstance(raw, dict), f"{name} is not a JSON object"

    fields = set(model.model_fields)
    assert set(raw) == fields, (
        f"{name} disagrees with {model.__name__}: "
        f"extra {sorted(set(raw) - fields)}, missing {sorted(fields - set(raw))}"
    )

    validated = model.model_validate(raw)
    dumped = validated.model_dump(mode="json")
    raw_paths, model_paths = _key_paths(raw), _key_paths(dumped)
    assert raw_paths == model_paths, (
        f"{name} disagrees with {model.__name__} below the top level: "
        f"extra {sorted(raw_paths - model_paths)}, "
        f"missing {sorted(model_paths - raw_paths)}"
    )


@pytest.mark.parametrize(
    ("name", "code"), sorted(_REFUSAL_FIXTURES.items()), ids=sorted(_REFUSAL_FIXTURES)
)
def test_refusal_fixture_carries_the_module_refusal(name: str, code: str) -> None:
    raw = _load(name)
    assert isinstance(raw, dict), f"{name} is not a JSON object"
    assert set(raw) == _ENVELOPE_KEYS, f"{name} is not a refusal envelope"
    assert raw["operation_id"] is None
    assert set(raw["error"]) == _ERROR_KEYS
    assert raw["error"]["error_code"] == code
    assert raw["state"] == code
    assert isinstance(raw["error"]["error_text"], str)
    assert raw["error"]["error_text"]

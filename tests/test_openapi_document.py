"""AC 15: the OpenAPI document is built from the registry, one path per operation.

The document is generated into a registry **of this test's own**, never the
process-wide ``REGISTRY``. That is not tidiness: registering ``spike.note.list``
(a ``read``-class operation) on the process registry silently widens
``rheo_core.tokens.sets.read_only()`` for every token issued afterwards, and
``tests/postgres/test_tokens.py`` asserts that set exactly — the landmine
``tests/conftest.py``'s ``_forget_registrations`` exists for. A private registry
sidesteps it entirely, and ``build_document(registry)`` takes the registry as an
argument precisely so this is possible.

The two process-wide tables the loader does write — the audit sinks and the module
surfaces — are reset around each test, mirroring ``tests/test_module_loader.py``'s
own autouse fixture.
"""

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel
from rheo_core.audit import reset_sinks
from rheo_core.modules import load_modules, reset_surfaces
from rheo_core.operations import (
    GENERATED_BANNER,
    OPERATION_PATH_PREFIX,
    OperationRegistry,
    build_document,
    operation_path,
    register_core_operations,
)
from rheo_core.operations.openapi import REF_TEMPLATE
from rheo_core.refs.resolver import ResolverRegistry
from rheo_spike.operations import (
    NOTE_ADD,
    NOTE_COMMIT_EARLY,
    NOTE_LIST,
    NoteAddInput,
    NoteList,
)

SPIKE_MODULE_ID = "spike"
GENERATED_DOCUMENT = "apps/web/src/generated/openapi.json"


@pytest.fixture(autouse=True)
def _clean_process_tables() -> Iterator[None]:
    reset_surfaces()
    reset_sinks()
    yield
    reset_surfaces()
    reset_sinks()


@pytest.fixture
def registry() -> OperationRegistry:
    """A private registry holding the core operations and the spike module, built
    exactly the way ``rheo openapi`` builds the process one: register the core
    operations, then load the allowed modules."""
    registry = OperationRegistry()
    register_core_operations(registry)
    loaded = load_modules(
        registry=registry,
        resolvers=ResolverRegistry(),
        allow=frozenset({SPIKE_MODULE_ID}),
    )
    assert loaded == (SPIKE_MODULE_ID,), (
        f"the rheo.modules entry point for {SPIKE_MODULE_ID!r} was not discovered "
        f"(loaded {loaded}); run `uv sync` so rheo-spike is installed here"
    )
    return registry


def _schema_without_defs(model: type[BaseModel]) -> dict[str, object]:
    """A model's own JSON Schema with its nested models removed, which is exactly
    what ``build_document`` puts under ``components.schemas`` for it."""
    schema = dict(model.model_json_schema(ref_template=REF_TEMPLATE))
    schema.pop("$defs", None)
    return schema


def test_every_registered_operation_has_a_path(registry: OperationRegistry) -> None:
    """AC 15's first half, in both directions: every registered name is a path key,
    and every path key is a registered name.

    The second direction is what makes it a real check — a document with a path per
    operation *plus* an invented one would satisfy "every operation has a path".
    """
    document = build_document(registry)
    names = sorted(registry.names())
    assert names, "the registry fixture registered nothing"
    assert sorted(document["paths"]) == [operation_path(name) for name in names]
    for name in names:
        assert document["paths"][operation_path(name)]["post"]["operationId"] == name


def test_the_test_only_third_operation_is_in_the_document_too(
    registry: OperationRegistry,
) -> None:
    """Three ``spike.*`` paths, not two.

    ``spike.note.commit_early`` is registered from the manifest exactly as FR 9b
    requires, so AC 15's "a path for every registered operation" includes it. Pinned
    here so a future reader does not "fix" the count down to the run card's two.
    """
    paths = build_document(registry)["paths"]
    spike_paths = sorted(
        key for key in paths if key.startswith(f"{OPERATION_PATH_PREFIX}spike.")
    )
    assert spike_paths == [
        operation_path(NOTE_ADD),
        operation_path(NOTE_COMMIT_EARLY),
        operation_path(NOTE_LIST),
    ]


def test_no_path_is_templated(registry: OperationRegistry) -> None:
    """One concrete path per operation, never ``/api/v1/operations/{name}``.

    A templated document would carry exactly one request schema and one response
    schema for every operation in the deployment, which is the whole reason the
    template is pinned in the contract the web client is generated against.
    """
    paths = build_document(registry)["paths"]
    assert all("{" not in key for key in paths), sorted(paths)
    assert len(paths) == len(registry.names())


def test_the_request_and_response_schemas_come_from_the_declared_models(
    registry: OperationRegistry,
) -> None:
    """AC 15's second half: ``spike.note.add``'s request body is ``NoteAddInput``'s
    own JSON Schema and ``spike.note.list``'s result is ``NoteList``'s, resolved
    through the document's own ``$ref``s rather than restated here."""
    document = build_document(registry)
    schemas = document["components"]["schemas"]

    add = document["paths"][operation_path(NOTE_ADD)]["post"]
    request_ref = add["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert request_ref == REF_TEMPLATE.format(model="NoteAddInput")
    assert schemas["NoteAddInput"] == _schema_without_defs(NoteAddInput)

    listed = document["paths"][operation_path(NOTE_LIST)]["post"]
    envelope = listed["responses"]["200"]["content"]["application/json"]["schema"]
    assert envelope["properties"]["result"]["$ref"] == REF_TEMPLATE.format(
        model="NoteList"
    )
    assert schemas["NoteList"] == _schema_without_defs(NoteList)
    # NoteList's $defs were lifted into components, so its own $ref resolves.
    assert "NoteHead" in schemas
    assert envelope["properties"]["operation_id"] == {"type": "null"}


def test_the_document_is_openapi_3_1(registry: OperationRegistry) -> None:
    document = build_document(registry)
    assert document["openapi"] == "3.1.0"
    assert set(document) == {"openapi", "info", "paths", "components"}


def test_a_module_that_is_not_loaded_has_no_path() -> None:
    """The registry is the only input: a document built from a registry the spike
    was never loaded into carries the core operations and nothing else."""
    registry = OperationRegistry()
    register_core_operations(registry)
    document = build_document(registry)
    assert document["paths"]
    assert not [key for key in document["paths"] if "spike." in key]


# --- the shipped `rheo openapi` command, in its own process ---------------------------
#
# In a subprocess on purpose, and it is the only way to prove these two things:
#
# - `rheo openapi` registers the core operations **and then loads the allowed
#   modules**. Calling `render()` in-process would register `spike.note.list` on the
#   process-wide REGISTRY, which silently widens `rheo_core.tokens.sets.read_only()`
#   for every token issued afterwards -- and `tests/postgres/test_tokens.py` asserts
#   that set exactly. The child process pays that cost and exits.
# - It is the one subcommand that bootstraps nothing. The child runs with **every**
#   `RHEO_*` variable stripped, so it has no profile, no data root, no cluster DSN
#   and no database, and still emits the document. That is what makes AC 3's
#   byte-exact regeneration check environment-independent.


def _run_openapi(*, modules: str | None) -> str:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("RHEO")
    }
    if modules is not None:
        env["RHEO_MODULES"] = modules
    completed = subprocess.run(
        ["uv", "run", "--frozen", "rheo", "openapi", "--out", "-"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_the_command_emits_module_paths_only_when_the_module_is_named() -> None:
    """Both directions of the allowlist, through the real command.

    The negative half alone would be vacuous — an empty document, a truncated one
    or an error string all "contain no spike path" — so the positive control on the
    core operation is asserted in the same breath.
    """
    named = json.loads(_run_openapi(modules=SPIKE_MODULE_ID))
    assert operation_path(NOTE_ADD) in named["paths"]
    assert operation_path(NOTE_LIST) in named["paths"]
    assert operation_path(NOTE_COMMIT_EARLY) in named["paths"]

    unnamed = json.loads(_run_openapi(modules=None))
    assert not [key for key in unnamed["paths"] if "spike." in key]
    assert "spike" not in json.dumps(unnamed)
    assert operation_path("core.workspace.status") in unnamed["paths"]


def test_the_emitted_document_carries_the_banner_and_is_byte_stable() -> None:
    """Re-emitting an unchanged registry produces identical bytes — the property
    AC 3's `git diff --exit-code` depends on — and the document says it is
    generated."""
    first = _run_openapi(modules=SPIKE_MODULE_ID)
    second = _run_openapi(modules=SPIKE_MODULE_ID)
    assert first == second
    assert json.loads(first)["x-rheo-generated"] == GENERATED_BANNER
    assert first.endswith("\n")


def test_the_committed_artifact_is_what_the_command_emits() -> None:
    """The committed `apps/web/src/generated/openapi.json` is the canonical
    environment's own output, byte for byte. A hand-edit fails here as well as at
    AC 3's CI step — this one runs in the `python` job, which has no Node."""
    committed = Path(__file__).resolve().parents[1] / GENERATED_DOCUMENT
    assert committed.read_text(encoding="utf-8") == _run_openapi(
        modules=SPIKE_MODULE_ID
    )

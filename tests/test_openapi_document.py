"""AC 15: the OpenAPI document is built from the registry, one path per operation.

The document is generated into a registry **of this test's own**, never the
process-wide ``REGISTRY``. That is not tidiness: ``rheo_core.tokens.sets`` is
evaluated over the whole process-wide registry at issue time, so a registration
made here would widen a named set for every token issued afterwards, in every
file that runs later in the same pytest process — and ``tests/postgres/
test_tokens.py`` asserts those sets exactly. A private registry sidesteps it
entirely, and ``build_document(registry)`` takes the registry as an argument
precisely so this is possible.

**The canonical generation environment is now "no ``RHEO_*`` variable at all".**
0c0's branch cut removed the last distribution publishing a ``rheo.modules``
entry point, so nothing is discoverable to load and ``make codegen`` passes no
``RHEO_MODULES``. The subprocess tests at the bottom run with every ``RHEO_*``
variable stripped, which is exactly what AC 3's byte-exact regeneration check
depends on.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel
from rheo_core.operations import (
    GENERATED_BANNER,
    OPERATION_PATH_PREFIX,
    OperationRegistry,
    build_document,
    operation_path,
    register_core_operations,
)
from rheo_core.operations.core_ops import (
    SETTINGS_SET,
    SETTINGS_SET_MEMBER,
    TOKEN_ISSUE,
    TOKEN_REVOKE,
    WORKSPACE_STATUS,
    SettingWrite,
    WorkspaceStatus,
)
from rheo_core.operations.openapi import REF_TEMPLATE

GENERATED_DOCUMENT = "apps/web/src/generated/openapi.json"


@pytest.fixture
def registry() -> OperationRegistry:
    """A private registry holding the core operations, built exactly the way
    ``rheo openapi`` builds the process one."""
    registry = OperationRegistry()
    registered = register_core_operations(registry)
    assert registered, "register_core_operations registered nothing"
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


def test_the_operations_built_inside_the_registrar_are_in_the_document_too(
    registry: OperationRegistry,
) -> None:
    """Five ``core.*`` paths, not three.

    ``core.token.issue`` and ``core.token.revoke`` are declared **inside**
    ``register_core_operations`` rather than in its module-level
    ``CORE_OPERATIONS`` tuple, to avoid a real import cycle. A reader who counts
    that tuple sees three. AC 15's "a path for every registered operation" is over
    what the registrar actually registered, so it is five, and it is pinned here so
    a future reader does not "fix" the count down to the tuple's length.
    """
    paths = build_document(registry)["paths"]
    core_paths = sorted(
        key for key in paths if key.startswith(f"{OPERATION_PATH_PREFIX}core.")
    )
    assert core_paths == sorted(
        operation_path(name)
        for name in (
            WORKSPACE_STATUS,
            SETTINGS_SET,
            SETTINGS_SET_MEMBER,
            TOKEN_ISSUE,
            TOKEN_REVOKE,
        )
    )


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
    """AC 15's second half: ``core.settings.set``'s request body is
    ``SettingWrite``'s own JSON Schema and ``core.workspace.status``'s result is
    ``WorkspaceStatus``'s, resolved through the document's own ``$ref``s rather
    than restated here."""
    document = build_document(registry)
    schemas = document["components"]["schemas"]

    written = document["paths"][operation_path(SETTINGS_SET)]["post"]
    request_ref = written["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ]
    assert request_ref == REF_TEMPLATE.format(model="SettingWrite")
    assert schemas["SettingWrite"] == _schema_without_defs(SettingWrite)

    status = document["paths"][operation_path(WORKSPACE_STATUS)]["post"]
    envelope = status["responses"]["200"]["content"]["application/json"]["schema"]
    assert envelope["properties"]["result"]["$ref"] == REF_TEMPLATE.format(
        model="WorkspaceStatus"
    )
    assert schemas["WorkspaceStatus"] == _schema_without_defs(WorkspaceStatus)
    # WorkspaceStatus's $defs were lifted into components, so its own $ref resolves.
    assert "ModuleStatus" in schemas
    assert envelope["properties"]["operation_id"] == {"type": "null"}


def test_the_document_is_openapi_3_1(registry: OperationRegistry) -> None:
    document = build_document(registry)
    assert document["openapi"] == "3.1.0"
    assert set(document) == {"openapi", "info", "paths", "components"}


def test_the_registry_is_the_only_input(registry: OperationRegistry) -> None:
    """An empty registry yields an empty ``paths``, while the populated fixture
    yields a full one: the document reflects what is registered and nothing else.

    Both halves in one test on purpose — an empty document alone would also
    satisfy "carries no unexpected path", which is the vacuous reading.
    """
    assert build_document(OperationRegistry())["paths"] == {}
    assert build_document(registry)["paths"]


# --- the shipped `rheo openapi` command, in its own process ---------------------------
#
# In a subprocess on purpose, and it is the only way to prove these two things:
#
# - Calling `render()` in-process would register the core operations on the
#   process-wide REGISTRY, which `rheo_core.tokens.sets` is evaluated over at issue
#   time -- and `tests/postgres/test_tokens.py` asserts those sets exactly. The child
#   process pays that cost and exits.
# - It is the one subcommand that bootstraps nothing. The child runs with **every**
#   `RHEO_*` variable stripped, so it has no profile, no data root, no cluster DSN
#   and no database, and still emits the document. That is what makes AC 3's
#   byte-exact regeneration check environment-independent.


def _run_openapi(*, modules: str | None = None) -> str:
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


def test_the_command_emits_the_core_operations_and_nothing_else() -> None:
    """The canonical generation environment, through the real command.

    Nothing in the checkout publishes a ``rheo.modules`` entry point after 0c0's
    branch cut, so the emitted document is the core registry exactly. Both
    directions in one breath: the ``core.*`` paths really are there (which is what
    keeps the second assertion from passing over an empty or errored document),
    and no path outside the ``core.`` namespace is.
    """
    document = json.loads(_run_openapi())
    assert operation_path("core.workspace.status") in document["paths"]
    assert operation_path(SETTINGS_SET) in document["paths"]
    outside = [
        key
        for key in document["paths"]
        if not key.startswith(f"{OPERATION_PATH_PREFIX}core.")
    ]
    assert outside == []


def test_naming_a_module_that_does_not_exist_changes_nothing() -> None:
    """``RHEO_MODULES`` still parses and still gates: naming an id no distribution
    publishes loads nothing and emits the same bytes as naming none at all.

    This is the surviving half of the allowlist's own check. Its positive half (a
    named module's paths appearing) went with the last module distribution and is
    unprovable here until a real one ships — ``tests/test_module_loader.py`` drives
    both directions against a fabricated entry point instead.
    """
    assert _run_openapi(modules="no_such_module") == _run_openapi()


def test_the_emitted_document_carries_the_banner_and_is_byte_stable() -> None:
    """Re-emitting an unchanged registry produces identical bytes — the property
    AC 3's `git diff --exit-code` depends on — and the document says it is
    generated."""
    first = _run_openapi()
    second = _run_openapi()
    assert first == second
    assert json.loads(first)["x-rheo-generated"] == GENERATED_BANNER
    assert first.endswith("\n")


def test_the_committed_artifact_is_what_the_command_emits() -> None:
    """The committed `apps/web/src/generated/openapi.json` is the canonical
    environment's own output, byte for byte. A hand-edit fails here as well as at
    AC 3's CI step — this one runs in the `python` job, which has no Node.

    It also pins `make codegen`'s recipe from the other side: the recipe passes no
    `RHEO_MODULES`, and so does this test.
    """
    committed = Path(__file__).resolve().parents[1] / GENERATED_DOCUMENT
    assert committed.read_text(encoding="utf-8") == _run_openapi()

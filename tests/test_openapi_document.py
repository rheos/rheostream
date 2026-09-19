"""AC 15: the OpenAPI document is built from the registry, one path per operation.

The document is generated into a registry **of this test's own**, never the
process-wide ``REGISTRY``. That is not tidiness: ``rheo_core.tokens.sets`` is
evaluated over the whole process-wide registry at issue time, so a registration
made here would widen a named set for every token issued afterwards, in every
file that runs later in the same pytest process — and ``tests/postgres/
test_tokens.py`` asserts those sets exactly. A private registry sidesteps it
entirely, and ``build_document(registry)`` takes the registry as an argument
precisely so this is possible.

**The canonical generation environment is "no ``RHEO_*`` variable and no
``deployment.toml``".** 0c0's branch cut removed the last distribution publishing
a ``rheo.modules`` entry point, so nothing is discoverable to load, and the module
allowlist is now the deployment-scope setting ``modules.installed``, which either
source can set. The subprocess tests at the bottom therefore close **both** sources:
every ``RHEO_*`` variable is stripped, and the child is given an empty temporary
``RHEO_DATA_ROOT`` so the ``deployment.toml`` it resolves is one that does not exist.
The setting lands on its empty package default either way — which is exactly what
AC 3's byte-exact regeneration check depends on.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from pydantic import BaseModel
from rheo_core.approvals import (
    APPROVAL_APPROVE,
    APPROVAL_REFUSE,
    STANDING_GRANT_CREATE,
    STANDING_GRANT_REVOKE,
)
from rheo_core.modules.loader import ALLOWLIST_KEY
from rheo_core.modules.operations import MODULE_ENABLE, MODULE_INSTALL
from rheo_core.operations import (
    GENERATED_BANNER,
    OPERATION_GET,
    OPERATION_LIST,
    OPERATION_PATH_PREFIX,
    OPERATION_RESOLVE,
    OperationRegistry,
    build_document,
    operation_path,
    register_core_operations,
)
from rheo_core.operations.core_ops import (
    AUDIT_LIST,
    SETTINGS_SET,
    SETTINGS_SET_MEMBER,
    TOKEN_ISSUE,
    TOKEN_REVOKE,
    WORK_FAILURES,
    WORKSPACE_DIGEST,
    WORKSPACE_EXPORT,
    WORKSPACE_RESTORE,
    WORKSPACE_STATUS,
    SettingWrite,
    WorkspaceStatus,
)
from rheo_core.operations.openapi import REF_TEMPLATE
from rheo_core.runtime.operations import RUNTIME_RUN
from rheo_core.settings import env_variable_names

GENERATED_DOCUMENT = "apps/web/src/generated/openapi.json"

MODULES_VARIABLE = env_variable_names(ALLOWLIST_KEY)[0]
"""How a child process sets the module allowlist: the deployment layer's own
environment spelling of ``modules.installed``, derived rather than written out, so
this test follows the key and its mapping rather than restating both."""


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
    """Every ``core.*`` path, including the four the module-level tuple does not hold.

    ``core.token.issue`` and ``core.token.revoke`` are declared **inside**
    ``register_core_operations`` rather than in its module-level
    ``CORE_OPERATIONS`` tuple, to avoid a real import cycle, so a reader who counts
    that tuple comes up short; run 0c3's ``core.approval.approve`` and
    ``.refuse`` are imported there for the same reason, and declared in
    ``rheo_core.approvals.operations`` beside their handlers. AC 15's "a path for
    every registered operation" is over what the registrar actually registered, and
    the enumeration below is that set — pinned by **name** rather than by number, so
    a run that adds a declaration updates a list it can read rather than a count it
    has to recompute.
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
            WORK_FAILURES,
            OPERATION_GET,
            OPERATION_LIST,
            OPERATION_RESOLVE,
            # 0c2's C5 adds core.audit.list. Listed in registration order, the
            # order CORE_OPERATIONS itself holds; the assertion sorts both sides.
            AUDIT_LIST,
            # 0c3's C6 and C7, registered last and from another package.
            APPROVAL_APPROVE,
            APPROVAL_REFUSE,
            STANDING_GRANT_CREATE,
            STANDING_GRANT_REVOKE,
            WORKSPACE_EXPORT,
            WORKSPACE_DIGEST,
            WORKSPACE_RESTORE,
            RUNTIME_RUN,
            # Run 1a0's ``core.module.install`` and ``core.module.enable``, both
            # declared in ``rheo_core.modules.operations`` and registered inside the
            # registrar for the same import-direction reason the token pair is.
            MODULE_INSTALL,
            MODULE_ENABLE,
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
    # A nullable uuid since run 0c2, not the literal ``null`` it was before: a
    # ``long_running`` dispatch puts a real id here, and a generated client has to be
    # able to read both. Every operation registered in release one still answers null,
    # which ``tests/postgres/test_api_surface.py`` asserts over the wire.
    assert envelope["properties"]["operation_id"] == {
        "type": ["string", "null"],
        "format": "uuid",
    }


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
# - It is the one subcommand that opens no database. The child runs with **every**
#   `RHEO_*` variable stripped and then a single one put back — an empty temporary
#   `RHEO_DATA_ROOT` — so it has no profile, no cluster DSN and no database, and
#   still emits the document. That is what makes AC 3's byte-exact regeneration
#   check environment-independent.
#
# **The empty data root is the load-bearing half, and stripping alone is not
# enough.** `load_modules()` resolves `modules.installed` now, and the deployment
# layer reads `<data_root>/config/deployment.toml` on the way. With `RHEO_DATA_ROOT`
# merely stripped, `resolve_data_root` falls through to the *platform*
# application-data directory, so a developer with a real deployment.toml there would
# have this test's byte-exactness guard reading a file outside the checkout — and, once
# a distribution publishes a `rheo.modules` entry point, possibly loading a module.
# A fresh empty directory per call closes that: it is created here rather than taken
# as a fixture argument, so no future caller can forget to isolate its child.


def _run_openapi(*, modules: str | None = None) -> str:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("RHEO")
    }
    if modules is not None:
        env[MODULES_VARIABLE] = modules
    with tempfile.TemporaryDirectory(prefix="rheo-openapi-root-") as empty_root:
        env["RHEO_DATA_ROOT"] = empty_root
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
    """``modules.installed`` still parses and still gates: naming an id no
    distribution publishes loads nothing and emits the same bytes as naming none at
    all.

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

    It also pins `make codegen`'s recipe from the other side: the recipe sets no
    module allowlist, and neither does this test, so both resolve `modules.installed`
    to its empty package default.
    """
    committed = Path(__file__).resolve().parents[1] / GENERATED_DOCUMENT
    assert committed.read_text(encoding="utf-8") == _run_openapi()

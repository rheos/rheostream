"""Criterion 18's production-profile registration assertion (AC 1, AC 2, AC 3).

Two halves, deliberately in two different processes.

**The check itself is :func:`run_production_startup_and_report`, an ordinary
module-level function run by a BARE python child — never by pytest.**
``tests/conftest.py`` sets ``RHEO_PROFILE=test`` unconditionally at import time,
and pytest collects that file in every session, a nested one included. A child
launched as ``uv run --frozen pytest`` would therefore have the profile stamped
back to ``test`` before this check ran: ``_check_production_scheme`` would be a
no-op, ``conftest``'s own profile guard raises only when the profile is *not*
``test`` so it would stay quiet too, and a fresh process that never calls
``register_harness()`` has a clean registry under any profile — so the check would
pass having never run under the production profile at all, and would stay green
under a mutation that dropped the harness gate. Nothing imports ``conftest.py``
outside pytest's collection machinery, so a bare ``python -c`` child is the one
shape that cannot be re-stamped. Everything this module imports from ``rheo_core``,
``rheo_app_core``, ``rheo_app_worker``, ``conftest`` and ``harness`` is imported
**inside a function** for the same reason: the child imports this module, and a
module-level ``from conftest import ...`` here would reintroduce exactly the
clobbering the subprocess exists to escape.

**The outer test is an ordinary pytest node** that builds the child's environment
by hand, runs it, and asserts that it exited zero, that its last line of stdout
reads ``PROFILE=production``, and that it enumerated a non-empty registration set.
A clean exit alone distinguishes neither "the registration set is clean" from "the
child never reached the production profile", nor either from "the registries were
empty, so there was nothing to find".

The operation registry is process-wide and shared by the whole pytest session —
dozens of tests call ``register_harness()`` against that one object — so an
in-process inspection here would find harness entries left behind by other tests
and would prove nothing about a deployed process either way. The subprocess is
what makes this a claim about a real startup rather than about this session.

**AC 2 needs no new workflow step.** ``.github/workflows/repository-checks.yml``'s
``python`` job runs ``uv run pytest``, whose ``testpaths = ["tests"]`` collects
this file, and a red result fails that step and the workflow with it. A nested
``uv run`` inside that step is already proven there by
``tests/postgres/test_migrations.py::test_suite_fails_fast_when_the_cluster_is_unreachable``.
A dedicated step would start a second production process for no signal the first
does not already carry.
"""

import os
import secrets
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # never at runtime: the bare child imports this module
    from conftest import ClusterSession

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TESTS_DIR = Path(__file__).resolve().parent

PRODUCTION_START_TIMEOUT_SECONDS = 300
"""Chosen for this call rather than borrowed from ``run_pytest_in_subprocess``.

That helper's ``timeout=120`` is sized for a lightweight nested pytest run. This
child creates a control database and runs the whole ``control`` chain against a
live cluster, then walks the active-workspace registry — heavier, and slower again
on a cold cluster or a loaded machine. A generous ceiling keeps a slow-but-healthy
migration reading as itself rather than as a broken environment.
"""

_CHILD_PROGRAM = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "import test_production_registration as check; "
    "check.run_production_startup_and_report()"
)
"""What the child runs. ``tests/`` reaches ``sys.path`` through ``argv``, not
through pytest's collection, because pytest is exactly what must not run here."""


def run_production_startup_and_report() -> None:
    """Start the application the way a deployed process does, then enumerate what
    it registered; raise on anything the test harness put there.

    Runs in the bare child described in the module docstring. Raises
    ``AssertionError`` naming every offender it found — a non-zero exit is the
    outer test's signal — and on success prints a small parseable report whose
    last line is the profile the child actually resolved.
    """
    from rheo_app_core.startup import run_startup
    from rheo_contracts import SafetyClass
    from rheo_core.identity.providers import GITHUB_PROVIDER_ID
    from rheo_core.operations import HARNESS_MODULE_ID, REGISTRY
    from rheo_core.settings import TEST_HARNESS_ORIGIN
    from rheo_core.storage import control_tables
    from rheo_core.storage.postgres import get_backend
    from rheo_core.tokens.sets import TOOL_REGISTRY, agent_default
    from sqlalchemy import select

    report = run_startup()

    # After ``run_startup()``: importing the worker's composition root is the
    # second half of the criterion's "consumer" clause, and it must be imported in
    # this same process so that what is asserted is one process's registration
    # set. ``main.py`` keeps ``get_backend()`` inside ``main()``, so the import
    # itself starts nothing.
    from rheo_app_worker import main as worker_main

    problems: list[str] = []

    operation_names = sorted(REGISTRY.names())
    if not operation_names:
        problems.append(
            "the operation registry is empty, so this enumeration would pass "
            "over nothing; startup did not build the registry"
        )
    for name in operation_names:
        registered = REGISTRY.lookup(name)
        assert registered is not None  # names() and lookup() share one table
        if registered.origin == TEST_HARNESS_ORIGIN:
            problems.append(
                f"operation {name!r} was registered by origin {registered.origin!r}"
            )
        if registered.module_id == HARNESS_MODULE_ID:
            problems.append(
                f"operation {name!r} belongs to module {HARNESS_MODULE_ID!r}"
            )
        safety_class = registered.declaration.safety_class
        if safety_class in (SafetyClass.EXTERNAL, SafetyClass.FINANCIAL):
            problems.append(
                f"operation {name!r} declares safety class {safety_class.value!r}"
            )

    tool_names = sorted(TOOL_REGISTRY.names())
    if not tool_names:
        problems.append(
            "the tool registry is empty; register_core_tools() did not run during "
            "startup, so the tool half of this enumeration would pass over nothing"
        )
    for name in tool_names:
        tool = TOOL_REGISTRY.lookup(name)
        assert tool is not None  # names() and lookup() share one table
        if tool.origin == TEST_HARNESS_ORIGIN:
            problems.append(f"tool {name!r} was registered by origin {tool.origin!r}")
    # The live effect the tool gate actually has: ``harness_get_note`` is a core
    # declaration, so its *origin* is clean under any profile, and what keeps it
    # out of a production token is ``agent_default``'s intersection with the
    # operation registry. Asserting the intersection is what makes this a check on
    # the gate rather than on the origin string alone.
    leaked = sorted(
        name for name in agent_default() if name.split(".", 1)[0] == HARNESS_MODULE_ID
    )
    if leaked:
        problems.append(
            f"the agent_default package set carries harness operation(s) {leaked}"
        )

    # ``ConsumerRegistry`` publishes ``for_type`` and ``lookup`` but no "everything
    # registered" accessor, and criterion 18 needs the whole set rather than one
    # event type's. Reading the one private dict is deliberate and scoped to this
    # check; adding a public accessor is an edit to ``events/consumers.py``, which
    # is outside this chunk's declared paths.
    subscriptions = tuple(worker_main.CONSUMERS._consumers.values())
    for subscription in subscriptions:
        if subscription.module_id == HARNESS_MODULE_ID:
            problems.append(
                f"consumer {subscription.consumer_id!r} belongs to module "
                f"{HARNESS_MODULE_ID!r}"
            )

    # Identity providers are registered by ``sync_providers()`` writing rows from
    # the resolved deployment settings, so the started application's provider set
    # IS ``control.identity_provider``. There is no origin column to filter on —
    # see this run's handoff note — so the assertion is an allowlist of the
    # provider ids the shipped package declares.
    shipped_provider_ids = frozenset({GITHUB_PROVIDER_ID})
    with get_backend().control_engine.connect() as connection:
        provider_ids = sorted(
            str(row[0])
            for row in connection.execute(
                select(control_tables.identity_provider.c.provider_id)
            )
        )
    for provider_id in provider_ids:
        if provider_id not in shipped_provider_ids:
            problems.append(
                f"identity provider {provider_id!r} is not one the shipped "
                f"package declares ({sorted(shipped_provider_ids)})"
            )

    if problems:
        raise AssertionError(
            "a production start registered what it must not:\n  "
            + "\n  ".join(problems)
        )

    print("PRODUCTION-REGISTRATION-REPORT")
    print(f"operations={len(operation_names)}")
    print(f"tools={len(tool_names)}")
    print(f"consumers={len(subscriptions)}")
    print(f"identity_providers={len(provider_ids)}")
    # Last line, and the outer test reads it: an exit code alone cannot say which
    # profile the child resolved.
    print(f"PROFILE={report.profile}")


@pytest.mark.postgres
def test_a_production_start_registers_nothing_from_the_test_harness(
    cluster: "ClusterSession", tmp_path: Path
) -> None:
    """AC 1 and AC 2: a real startup under ``profile = production`` registers no
    tool, operation, consumer or identity provider from the test harness, and no
    operation in the external or financial class.

    The child's environment is built rather than inherited. Every ``RHEO_*``
    variable is stripped first — this developer machine's ``.env`` sets
    ``RHEO_PROFILE``, ``RHEO_MODULES``, ``RHEO_DATA_ROOT`` and more, and CI's
    ``python`` job sets ``RHEO_PROFILE=test`` at job level, so an inherited
    environment would decide the outcome. Four variables go back in: the
    production profile; ``RHEO_CLUSTER_DSN`` bridged from
    ``RHEO_TEST_CLUSTER_DSN`` exactly as ``tests/conftest.py`` bridges it, so the
    child reaches the cluster through the settings-to-secret path production uses;
    a freshly named control database, recorded with this session so teardown drops
    it and so the child's ``control`` chain cannot touch the outer session's; and
    a private data root, so nothing is written to the developer's real
    application-data directory.

    ``routing.scheme`` is deliberately not set: the package default is ``https``,
    which is what lets ``_check_production_scheme`` pass, and stripping the
    ``RHEO_*`` variables is what stops a local ``http`` override from reaching the
    child and turning that guard into the thing this test measures.
    """
    from conftest import CONTROL_TEST_PREFIX, DEFAULT_TEST_CLUSTER_DSN

    cluster_dsn = (
        os.environ.get("RHEO_TEST_CLUSTER_DSN", "").strip() or DEFAULT_TEST_CLUSTER_DSN
    )
    # Recorded before the child can create it, so a child that fails halfway
    # through its migration still has its database dropped at teardown.
    control_database = cluster.record(f"{CONTROL_TEST_PREFIX}{secrets.token_hex(6)}")
    data_root = tmp_path / "production-data-root"

    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("RHEO_")
    }
    env.update(
        {
            "RHEO_PROFILE": "production",
            "RHEO_CLUSTER_DSN": cluster_dsn,
            "RHEO__storage__control_database": control_database,
            "RHEO_DATA_ROOT": str(data_root),
        }
    )

    completed = subprocess.run(
        ["uv", "run", "--frozen", "python", "-c", _CHILD_PROGRAM, str(_TESTS_DIR)],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=PRODUCTION_START_TIMEOUT_SECONDS,
    )
    output = f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"

    assert completed.returncode == 0, output
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    assert lines, output
    # The child really ran under the production profile. Without this an exit code
    # of zero is also what a child re-stamped to ``profile = test`` produces.
    assert lines[-1] == "PROFILE=production", output
    counts = dict(line.split("=", 1) for line in lines[:-1] if "=" in line)
    # Neither registry was empty, so the enumeration above passed over something.
    assert int(counts["operations"]) > 0, output
    assert int(counts["tools"]) > 0, output


def test_registering_an_operation_with_no_safety_class_is_refused_naming_it() -> None:
    """AC 3's operation half, the mirror of ``tests/test_contracts.py``'s tool case.

    ``registry.py``'s refusal has shipped since run 0b1 and, at this run's base,
    no test anywhere registered a class-less operation against it — the criterion
    asks for one ("a test registers one tool and one service operation with no
    declared class and asserts startup fails naming each"), and C4 closed only the
    tool side. ``model_construct`` is the whole point: pydantic refuses an ordinary
    construction without a class, so a skipped-validation declaration is the only
    shape that can reach the registry missing one. A private ``OperationRegistry``
    rather than the process-wide ``REGISTRY``, so this leaves no trace in the table
    every other test in the session reads.
    """
    from harness.registry import Nothing, probe_handler
    from pydantic import BaseModel
    from rheo_contracts import Idempotency, OperationDeclaration
    from rheo_core.operations import OperationRegistry, RegistrationRefused
    from rheo_core.settings import CORE_ORIGIN

    class _NoFields(BaseModel):
        pass

    classless = OperationDeclaration.model_construct(
        name="core.probe.act",
        input_model=_NoFields,
        output=Nothing,
        idempotency=Idempotency.NONE,
    )
    registry = OperationRegistry()

    with pytest.raises(RegistrationRefused) as raised:
        registry.register(classless, probe_handler, origin=CORE_ORIGIN)

    assert "core.probe.act" in str(raised.value)
    assert raised.value.operation_name == "core.probe.act"
    assert "safety class" in raised.value.detail
    assert "core.probe.act" not in registry

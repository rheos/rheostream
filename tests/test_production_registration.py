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

_CLASSLESS_CHILD_PROGRAM = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "import test_production_registration as check; "
    "check.run_startup_with_a_classless_registration(sys.argv[2])"
)
"""The same shape for the class-less-registration half, with the kind in ``argv``."""


def run_production_startup_and_report() -> None:
    """Start the application the way a deployed process does, then enumerate what
    it registered; raise on anything the test harness put there.

    Runs in the bare child described in the module docstring. Raises
    ``AssertionError`` naming every offender it found — a non-zero exit is the
    outer test's signal — and on success prints a small parseable report whose
    last line is the profile the child actually resolved.
    """
    from pydantic import BaseModel
    from rheo_app_core.startup import run_startup
    from rheo_contracts import (
        AuditSpec,
        Idempotency,
        OperationDeclaration,
        Role,
        SafetyClass,
    )
    from rheo_core.events.consumers import ConsumerRegistry, ConsumerSubscription
    from rheo_core.identity.providers import GITHUB_PROVIDER_ID
    from rheo_core.operations import (
        CORE_MODULE_ID,
        HARNESS_MODULE_ID,
        REGISTRY,
        OperationRegistry,
    )
    from rheo_core.settings import CORE_ORIGIN, TEST_HARNESS_ORIGIN, resolve
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

    def operation_problems(registry: OperationRegistry) -> list[str]:
        """Every reason an operation in ``registry`` may not be in a production set.

        A named function rather than an inline loop so the same code can be pointed
        at a registry built to contain a violation — see the control below.
        """
        found: list[str] = []
        for name in sorted(registry.names()):
            registered = registry.lookup(name)
            assert registered is not None  # names() and lookup() share one table
            if registered.origin == TEST_HARNESS_ORIGIN:
                found.append(
                    f"operation {name!r} was registered by origin {registered.origin!r}"
                )
            if registered.module_id == HARNESS_MODULE_ID:
                found.append(
                    f"operation {name!r} belongs to module {HARNESS_MODULE_ID!r}"
                )
            safety_class = registered.declaration.safety_class
            if safety_class in (SafetyClass.EXTERNAL, SafetyClass.FINANCIAL):
                found.append(
                    f"operation {name!r} declares safety class {safety_class.value!r}"
                )
        return found

    operation_names = sorted(REGISTRY.names())
    if not operation_names:
        problems.append(
            "the operation registry is empty, so this enumeration would pass "
            "over nothing; startup did not build the registry"
        )
    problems.extend(operation_problems(REGISTRY))

    # The external/financial half of that predicate ranges over ten real operations
    # and can never match one: nothing above ``MUTATE`` exists in shipped code, and
    # the upper-class fixtures are harness-registered and profile-gated. So it is
    # not vacuous in the "passes over an empty set" sense, but it has never been
    # seen to fire, which is the same position the consumer clause was in — and it
    # gets the same instrument. The predicate above is applied to a registry built
    # to hold exactly one EXTERNAL operation and one READ operation; it must flag
    # the first and only the first.
    class _NoFields(BaseModel):
        pass

    def _probe(ctx: object, uow: object, model_input: object) -> BaseModel:
        raise AssertionError("a control-registry probe must never be dispatched")

    control_registry = OperationRegistry()
    for probe_name, probe_class, probe_audit in (
        ("core.probe.send", SafetyClass.EXTERNAL, AuditSpec(subject_field=None)),
        ("core.probe.read", SafetyClass.READ, None),
    ):
        control_registry.register(
            OperationDeclaration(
                name=probe_name,
                safety_class=probe_class,
                roles=frozenset({Role.OWNER}),
                input_model=_NoFields,
                output=_NoFields,
                idempotency=Idempotency.NONE,
                audit=probe_audit,
            ),
            _probe,
            origin=CORE_ORIGIN,
        )
    control_flags = operation_problems(control_registry)
    if len(control_flags) != 1 or "core.probe.send" not in control_flags[0]:
        problems.append(
            "the external/financial clause's own positive control did not fire: an "
            f"EXTERNAL operation beside a READ one produced {control_flags}, not "
            "exactly one problem naming the EXTERNAL one, so the same predicate "
            "applied to the production registry would not catch one there either"
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
    # is outside this chunk's declared paths. A rename of that attribute raises
    # here rather than quietly enumerating nothing, so the read cannot decay into a
    # silent pass.
    def harness_consumer_ids(registry: ConsumerRegistry) -> list[str]:
        """Consumer ids in ``registry`` whose module is the test harness."""
        return sorted(
            subscription.consumer_id
            for subscription in registry._consumers.values()
            if subscription.module_id == HARNESS_MODULE_ID
        )

    subscriptions = tuple(worker_main.CONSUMERS._consumers.values())
    for consumer_id in harness_consumer_ids(worker_main.CONSUMERS):
        problems.append(
            f"consumer {consumer_id!r} belongs to module {HARNESS_MODULE_ID!r}"
        )

    # Release one registers no production consumer, so the real registry is
    # legitimately empty and the loop above passes over nothing. Requiring it to be
    # non-empty would be a guard that is red against correct code today; proving the
    # predicate is not a no-op is the check that is actually available. The same
    # function is applied to a throwaway registry holding one harness subscription
    # and one core subscription — it must find the first and not the second.
    control = ConsumerRegistry()
    for consumer_id, module_id in (
        ("harness.probe.consumer", HARNESS_MODULE_ID),
        ("core.probe.consumer", CORE_MODULE_ID),
    ):
        control.register(
            ConsumerSubscription(
                consumer_id=consumer_id,
                event_type="probe.happened",
                module_id=module_id,
                replay_safe=True,
                handler=lambda uow, envelope: None,
            )
        )
    if harness_consumer_ids(control) != ["harness.probe.consumer"]:
        problems.append(
            "the consumer check's own positive control did not fire, so the "
            "predicate applied to the worker's registry would not catch a harness "
            "consumer there either"
        )

    # Identity providers are "registered" by ``sync_providers()`` upserting
    # ``control.identity_provider`` rows from the resolved deployment settings, so
    # the started application's provider set IS that table. The table carries no
    # origin column, so provenance cannot be read off a row; what CAN be checked is
    # that the rows present are exactly the ones this process's own settings would
    # have produced. A row this startup did not write — whatever id it carries,
    # ``github`` included — makes the two sets differ.
    #
    # The enabled/client-id rule below is a deliberate second statement of
    # ``identity/provider_config.py``'s, not a shared import of it: two independent
    # statements can disagree, which is what makes this a check rather than a
    # restatement of the code under test. Both settings keys are declared, so a
    # typo here raises ``setting_undeclared`` rather than silently reading False.
    settings = resolve()
    github_configured = bool(
        settings.get_bool("identity.providers.github.enabled")
        and settings.get_str("identity.providers.github.client_id")
    )
    expected_provider_ids = sorted({GITHUB_PROVIDER_ID} if github_configured else set())
    shipped_provider_ids = frozenset({GITHUB_PROVIDER_ID})
    with get_backend().control_engine.connect() as connection:
        provider_ids = sorted(
            str(row[0])
            for row in connection.execute(
                select(control_tables.identity_provider.c.provider_id)
            )
        )
    if provider_ids != expected_provider_ids:
        problems.append(
            f"identity provider rows {provider_ids} are not the set this process's "
            f"own settings produce ({expected_provider_ids}); a provider row this "
            "startup did not write is in the control plane"
        )
    # Independent of the equality above, and not implied by it: an id outside the
    # shipped set is named on its own, so the failure says "unknown provider"
    # rather than only "the sets differ".
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
    # Reported beside the actual count, not folded into it: a caller that wants to
    # know the equality above compared two non-empty sets cannot tell that from the
    # actual count alone, and "one provider row exists" is true for more reasons
    # than "this process's settings called for one".
    print(f"identity_providers_expected={len(expected_provider_ids)}")
    # Last line, and the outer test reads it: an exit code alone cannot say which
    # profile the child resolved.
    print(f"PROFILE={report.profile}")


def run_startup_with_a_classless_registration(kind: str) -> None:
    """Criterion 18's own sentence, driven through ``run_startup()``: a class-less
    registration must make **startup** fail, naming what was registered.

    Runs in the same bare child as :func:`run_production_startup_and_report`.
    ``kind`` selects the half: ``"operation"`` appends a class-less
    ``OperationDeclaration`` to ``core_ops.CORE_OPERATIONS``, ``"tool"`` appends a
    class-less ``ToolDeclaration`` to ``sets.CORE_TOOLS``. Both are the tuples the
    core's own registration functions read at call time, so the injection reaches
    the registration the shipped startup performs rather than one the test performs
    for itself — which is the difference between demonstrating "the registry
    refuses" and demonstrating "startup fails".

    Rebinding a module-level ``Final`` tuple is not something production code may
    do; it is safe here because the process is created for this one check and
    thrown away, and because the alternative — a class-less declaration committed
    into shipped code behind a flag — would be a permanent hazard in exchange for
    the same signal. ``model_construct`` is what makes a class-less declaration
    constructible at all: pydantic refuses an ordinary construction without one.

    Exits zero on the expected refusal and raises when startup **completes**, so
    the polarity is right: a startup that filtered or skipped the declaration fails
    this check rather than passing it.
    """
    from pydantic import BaseModel
    from rheo_app_core.startup import run_startup
    from rheo_contracts import (
        Idempotency,
        OperationDeclaration,
        Role,
        ToolDeclaration,
    )
    from rheo_core.operations import RegistrationRefused, core_ops
    from rheo_core.tokens import sets

    class _NoFields(BaseModel):
        pass

    def _handler(ctx: object, uow: object, model_input: object) -> BaseModel:
        raise AssertionError("the class-less probe must never be dispatched")

    if kind == "operation":
        expected_name = "core.probe.act"
        core_ops.CORE_OPERATIONS = (  # type: ignore[misc]
            *core_ops.CORE_OPERATIONS,
            (
                OperationDeclaration.model_construct(
                    name=expected_name,
                    roles=frozenset({Role.OWNER}),
                    input_model=_NoFields,
                    output=_NoFields,
                    idempotency=Idempotency.NONE,
                    audit=None,
                    long_running=False,
                ),
                _handler,
            ),
        )
    elif kind == "tool":
        expected_name = "no_class_tool"
        sets.CORE_TOOLS = (  # type: ignore[misc]
            *sets.CORE_TOOLS,
            ToolDeclaration.model_construct(
                name=expected_name,
                operation="core.workspace.status",
                input_model=_NoFields,
            ),
        )
    else:
        raise ValueError(f"unknown kind {kind!r}; expected 'operation' or 'tool'")

    try:
        run_startup()
    except RegistrationRefused as refused:
        if refused.operation_name != expected_name:
            raise AssertionError(
                f"startup refused {refused.operation_name!r}, not the class-less "
                f"{expected_name!r} this child registered"
            ) from refused
        if "safety class" not in refused.detail:
            raise AssertionError(
                f"startup refused {expected_name!r} for {refused.detail!r}, which "
                "is not the missing safety class this child injected"
            ) from refused
        print(f"REFUSED={refused.operation_name}")
        return
    raise AssertionError(
        f"startup completed with a class-less {kind} registered; it did not refuse "
        f"{expected_name!r}"
    )


def _production_child_env(
    cluster: "ClusterSession", data_root: Path, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """The environment a production child runs with, built rather than inherited.

    Every ``RHEO_*`` variable is stripped first — this developer machine's ``.env``
    sets ``RHEO_PROFILE``, ``RHEO_MODULES``, ``RHEO_DATA_ROOT`` and more, and CI's
    ``python`` job sets ``RHEO_PROFILE=test`` at job level, so an inherited
    environment would decide the outcome. Four go back in: the production profile;
    ``RHEO_CLUSTER_DSN`` bridged from ``RHEO_TEST_CLUSTER_DSN`` exactly as
    ``tests/conftest.py`` bridges it, so the child reaches the cluster through the
    settings-to-secret path production uses; a freshly named control database,
    recorded with this session so teardown drops it and so the child's ``control``
    chain cannot touch the outer session's; and a private data root.

    ``routing.scheme`` is deliberately not set: the package default is ``https``,
    which is what lets ``_check_production_scheme`` pass, and stripping the
    ``RHEO_*`` variables is what stops a local ``http`` override from reaching the
    child and turning that guard into the thing these tests measure.
    """
    from conftest import CONTROL_TEST_PREFIX, DEFAULT_TEST_CLUSTER_DSN

    cluster_dsn = (
        os.environ.get("RHEO_TEST_CLUSTER_DSN", "").strip() or DEFAULT_TEST_CLUSTER_DSN
    )
    # Recorded before the child can create it, so a child that fails halfway
    # through its migration still has its database dropped at teardown.
    control_database = cluster.record(f"{CONTROL_TEST_PREFIX}{secrets.token_hex(6)}")
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
    # Applied last so a caller can add settings overrides on top. The ``RHEO__``
    # prefix with ``.`` spelled ``__`` is the only form the deployment layer reads;
    # a single-underscore ``RHEO_identity__...`` is neither consumed nor reported as
    # a stray, so it would be silently ignored and leave the setting at its default.
    env.update(extra or {})
    return env


def _run_production_child(
    cluster: "ClusterSession", data_root: Path, extra: dict[str, str] | None = None
) -> tuple[dict[str, str], str]:
    """Run the enumerating child once; return its parsed counts and its raw output.

    Asserts the two things every run of it must show whatever else is being
    measured: a zero exit, and ``PROFILE=production`` as the last line of stdout.
    """
    completed = subprocess.run(
        ["uv", "run", "--frozen", "python", "-c", _CHILD_PROGRAM, str(_TESTS_DIR)],
        cwd=_REPO_ROOT,
        env=_production_child_env(cluster, data_root, extra),
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
    return dict(line.split("=", 1) for line in lines[:-1] if "=" in line), output


@pytest.mark.postgres
def test_the_identity_provider_clause_tracks_what_is_configured(
    cluster: "ClusterSession", tmp_path: Path
) -> None:
    """The identity clause's anti-vacuity control: its answer must change when the
    deployment's own provider configuration changes.

    The other three clauses each have a guard that fires when the thing they
    enumerate is missing entirely — the two empty-registry sentences, and the
    consumer positive control. The identity clause had none, and in the shipped
    child configuration its non-trivial branch was dead: with every ``RHEO_*``
    stripped and an empty data root, ``identity.providers.github.enabled`` always
    resolved false, so ``expected_provider_ids`` was always ``[]`` and the equality
    compared two empty lists. A startup that stopped syncing providers altogether
    would have left it green.

    So the check is run twice in one node, against the same code, with the only
    difference being the deployment settings. Unconfigured it must see nothing;
    configured it must see exactly the provider it was configured with. Removing
    the producer now fails the second half, because the expectation there is
    non-empty and the table would not be.

    The client id is a fabricated placeholder and nothing reaches GitHub: startup
    only upserts the row. ``client_secret_ref`` stays empty, so
    ``check_env_references`` has no ``secret://env/...`` reference to demand.
    """
    unconfigured, unconfigured_output = _run_production_child(
        cluster, tmp_path / "identity-unconfigured-root"
    )
    assert unconfigured["identity_providers"] == "0", unconfigured_output
    assert unconfigured["identity_providers_expected"] == "0", unconfigured_output

    configured, configured_output = _run_production_child(
        cluster,
        tmp_path / "identity-configured-root",
        {
            "RHEO__identity__providers__github__enabled": "true",
            "RHEO__identity__providers__github__client_id": "placeholder-client-id",
        },
    )
    # Both halves asserted: the expectation was non-empty, so the equality inside
    # the child compared two populated sets rather than two empty ones, and the row
    # the startup actually wrote matched it.
    assert configured["identity_providers_expected"] == "1", configured_output
    assert configured["identity_providers"] == "1", configured_output


@pytest.mark.postgres
def test_a_production_start_registers_nothing_from_the_test_harness(
    cluster: "ClusterSession", tmp_path: Path
) -> None:
    """AC 1 and AC 2: a real startup under ``profile = production`` registers no
    tool, operation, consumer or identity provider from the test harness, and no
    operation in the external or financial class.
    """
    env = _production_child_env(cluster, tmp_path / "production-data-root")

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


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("kind", "expected_name"),
    [("operation", "core.probe.act"), ("tool", "no_class_tool")],
)
def test_startup_refuses_a_classless_registration_naming_it(
    cluster: "ClusterSession", tmp_path: Path, kind: str, expected_name: str
) -> None:
    """AC 3 through the sentence the criterion actually writes: "asserts **startup**
    fails naming each".

    The two unit-level companions below and in ``tests/test_contracts.py`` prove
    that each *registry* refuses a class-less declaration. Neither proves that a
    started application does, because both build a private registry the shipped
    startup never touches — so a startup that filtered the declaration out, or
    registered through some other path, would leave both green. This drives the
    real ``run_startup()`` with the declaration injected into the tuple the core's
    own registration function reads, once per half, each in its own process with
    its own control database.
    """
    env = _production_child_env(cluster, tmp_path / f"classless-{kind}-data-root")

    completed = subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "python",
            "-c",
            _CLASSLESS_CHILD_PROGRAM,
            str(_TESTS_DIR),
            kind,
        ],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=PRODUCTION_START_TIMEOUT_SECONDS,
    )
    output = f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"

    # Zero means the child caught the refusal it was looking for; a startup that
    # completed raises in the child and lands here as a non-zero exit.
    assert completed.returncode == 0, output
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    assert lines, output
    # The refusal named what was registered, which is the half of the criterion a
    # bare "startup failed" would not carry.
    assert lines[-1] == f"REFUSED={expected_name}", output


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

"""Issue #132: tool names, job kinds and consumer ids carry the module's own prefix.

``identifiers.md`` § Namespaces in one place gives all three a module prefix
(``<module>_<verb>[_<noun>]``, ``<module>.<name>``, ``<module>.<handler>``). Before
#132 nothing checked them, and ``JobKindRegistry``/``ConsumerRegistry`` are
last-writer-wins, so a module could replace a core job kind or consumer silently.

Seams under test:

- ``ModuleManifest``'s constructor refuses each name outside the prefix, naming the
  field, the offending name and the prefix it should carry.
- ``load_modules`` refuses the same names for a manifest that skipped validation
  (``model_construct``), before anything registers.
- The module-id grammar ``[a-z][a-z0-9_]{0,31}`` (``identifiers.md``'s configuration
  identifier), which the manifest now enforces instead of ``isidentifier()``.
- The shipped Recallatron manifest passes every new rule.
"""

from collections.abc import Iterator
from importlib.metadata import EntryPoint

import pydantic
import pytest
from harness.modules import install, manifest, publish
from pydantic import BaseModel
from rheo_contracts import SafetyClass, ToolDeclaration
from rheo_core.events.consumers import ConsumerRegistry, ConsumerSubscription
from rheo_core.modules import (
    ENTRY_POINT_GROUP,
    JobKind,
    ManifestInvalid,
    ModuleManifest,
    StorageDeclaration,
    load_modules,
    loaded_manifests,
    reset_surfaces,
)
from rheo_core.modules.manifest import check_name_prefixes
from rheo_core.operations import OperationRegistry
from rheo_core.refs.resolver import ResolverRegistry
from rheo_core.tokens.sets import ToolRegistry
from rheo_core.work.kinds import JobKindRegistry

MODULE_ID = "prefix_probe"


class _ProbeInput(BaseModel):
    note: str


def _job_handler(*args: object) -> None:
    """Never run: nothing here dispatches a job."""
    return None


def _consumer_handler(*args: object) -> None:
    """Never run: nothing here delivers an event."""
    return None


def _tool(name: str) -> ToolDeclaration:
    return ToolDeclaration(
        name=name,
        safety_class=SafetyClass.READ,
        operation=f"{MODULE_ID}.note.get",
        input_model=_ProbeInput,
    )


def _job(name: str) -> JobKind:
    return JobKind(
        name=name,
        input_model=_ProbeInput,
        handler=_job_handler,
        max_attempts=1,
        cancellable=False,
    )


def _subscription(consumer_id: str) -> ConsumerSubscription:
    return ConsumerSubscription(
        consumer_id=consumer_id,
        event_type=f"{MODULE_ID}.note.created",
        module_id=MODULE_ID,
        replay_safe=True,
        handler=_consumer_handler,
    )


@pytest.fixture(autouse=True)
def clean_loaded() -> Iterator[None]:
    reset_surfaces()
    yield
    reset_surfaces()


# --- the manifest refuses at construction ---------------------------------------------


def test_names_under_the_modules_own_prefix_are_accepted() -> None:
    built = manifest(
        MODULE_ID,
        tools=(_tool(f"{MODULE_ID}_get_note"),),
        jobs=(_job(f"{MODULE_ID}.reindex"),),
        subscriptions=(_subscription(f"{MODULE_ID}.note.indexer"),),
    )
    assert built.module_id == MODULE_ID


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"jobs": (_job("core.retention_sweep"),)},
            "jobs: job kind 'core.retention_sweep' does not start with "
            "'prefix_probe.'; a module's job kind is named '<module_id>.<name>'",
        ),
        (
            {"jobs": (_job(f"{MODULE_ID}."),)},
            "jobs: job kind 'prefix_probe.' does not start with 'prefix_probe.'",
        ),
        (
            {"jobs": (_job(f"{MODULE_ID}_reindex"),)},
            "jobs: job kind 'prefix_probe_reindex' does not start with 'prefix_probe.'",
        ),
        (
            {"subscriptions": (_subscription("core.on_record_deleted"),)},
            "subscriptions: consumer id 'core.on_record_deleted' does not start "
            "with 'prefix_probe.'; a module's consumer id is named "
            "'<module_id>.<handler>'",
        ),
        (
            {"tools": (_tool("workspace_status"),)},
            "tools: tool name 'workspace_status' does not start with "
            "'prefix_probe_'; a module's tool is named '<module_id>_<verb>[_<noun>]'",
        ),
        (
            {"tools": (_tool(f"{MODULE_ID}.get_note"),)},
            "tools: tool name 'prefix_probe.get_note' does not start with "
            "'prefix_probe_'",
        ),
        (
            {"tools": (_tool(f"{MODULE_ID}_"),)},
            "tools: tool name 'prefix_probe_' does not start with 'prefix_probe_'",
        ),
    ],
    ids=[
        "core-job-kind",
        "empty-job-remainder",
        "underscore-job-kind",
        "core-consumer-id",
        "unprefixed-tool",
        "dotted-tool",
        "empty-tool-remainder",
    ],
)
def test_a_name_outside_the_prefix_is_refused_naming_it(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        manifest(MODULE_ID, **overrides)
    assert expected in str(excinfo.value)


def test_a_prefix_that_merely_starts_the_same_is_refused() -> None:
    """``prefix_probe`` does not own ``prefix_probe2.sweep``: the rule is the id and
    its separator, not a string prefix of the id alone."""
    with pytest.raises(pydantic.ValidationError) as excinfo:
        manifest(MODULE_ID, jobs=(_job(f"{MODULE_ID}2.sweep"),))
    assert "job kind 'prefix_probe2.sweep' does not start with 'prefix_probe.'" in str(
        excinfo.value
    )


# --- the module-id grammar ------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["_leading", "9starts_with_digit", "café", "a" * 33, "has-hyphen", "Upper"],
)
def test_a_module_id_outside_the_configuration_identifier_grammar_is_refused(
    bad_id: str,
) -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        manifest(
            bad_id,
            storage=StorageDeclaration(
                schema_name=bad_id, migrations_path="m", required_extensions=()
            ),
        )
    assert (
        f"module_id {bad_id!r}: a module id is a lowercase identifier matching "
        "[a-z][a-z0-9_]{0,31}"
    ) in str(excinfo.value)


def test_a_thirty_two_character_module_id_is_accepted() -> None:
    longest = "a" * 32
    assert manifest(longest).module_id == longest


# --- the loader refuses a manifest that skipped validation ----------------------------


def _unvalidated(**overrides: object) -> ModuleManifest:
    """A manifest carrying ``overrides`` with the validators skipped."""
    fields = dict(manifest(MODULE_ID))
    fields.update(overrides)
    return ModuleManifest.model_construct(**fields)


CORE_JOB_MANIFEST = _unvalidated(jobs=(_job("core.retention_sweep"),))
CORE_CONSUMER_MANIFEST = _unvalidated(
    subscriptions=(_subscription("core.on_record_deleted"),)
)
UNPREFIXED_TOOL_MANIFEST = _unvalidated(tools=(_tool("workspace_status"),))


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [
        ("CORE_JOB_MANIFEST", "job kind 'core.retention_sweep'"),
        ("CORE_CONSUMER_MANIFEST", "consumer id 'core.on_record_deleted'"),
        ("UNPREFIXED_TOOL_MANIFEST", "tool name 'workspace_status'"),
    ],
)
def test_the_loader_refuses_an_unvalidated_manifest_before_registering_anything(
    monkeypatch: pytest.MonkeyPatch, attribute: str, expected: str
) -> None:
    publish(
        monkeypatch,
        EntryPoint(
            name=MODULE_ID, value=f"{__name__}:{attribute}", group=ENTRY_POINT_GROUP
        ),
    )
    install(monkeypatch, MODULE_ID)
    operations, tools = OperationRegistry(), ToolRegistry()
    kinds, consumers = JobKindRegistry(), ConsumerRegistry()

    with pytest.raises(ManifestInvalid) as excinfo:
        load_modules(
            registry=operations,
            resolvers=ResolverRegistry(),
            tools=tools,
            kinds=kinds,
            consumers=consumers,
        )

    assert str(excinfo.value).startswith(f"{MODULE_ID}: ")
    assert expected in str(excinfo.value)
    assert kinds.names() == frozenset()
    assert tools.names() == frozenset()
    assert loaded_manifests() == {}


# --- the shipped module passes --------------------------------------------------------


def test_the_shipped_recallatron_manifest_passes_the_prefix_rule() -> None:
    """Recallatron is the one shipped module with a ``rheo.modules`` entry point
    (``relationships``, ``leads`` and ``current`` publish none), and it declares tools
    and job kinds, so it is the manifest the new rule has to admit."""
    from rheo_recallatron import MANIFEST

    check_name_prefixes(
        module_id=MANIFEST.module_id,
        tools=MANIFEST.tools,
        jobs=MANIFEST.jobs,
        subscriptions=MANIFEST.subscriptions,
    )
    assert MANIFEST.tools, "the check would be vacuous over no tools"
    assert MANIFEST.jobs, "the check would be vacuous over no job kinds"

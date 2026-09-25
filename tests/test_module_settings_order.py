"""#108: an allowed module's own deployment-scoped setting resolves from the first
``resolve()`` on, and every stray ``RHEO__*`` variable is still refused.

Seams under test: ``read_deployment_value()`` (one key, nothing else read);
``allowed_module_ids()``'s read through it; ``register_module_settings()`` (only the
allowlisted modules, settings only); and the early hook at the composition roots —
``run_startup()`` through the real FastAPI lifespan, and the ``rheo`` CLI's
``run_command``, including the two subcommands it must skip.

**Recallatron is the real module in every positive case**, through its real
``rheo.modules`` entry point and its real ``recallatron.embedding.provider`` key. The
not-allowlisted case needs a module that is discoverable and never named, so it is
fabricated here (``absent_probe``, ``_probe``-suffixed per ``tests/harness/modules.py``)
and published beside the real one.

**Process state is restored by every test.** ``settings.schema.REGISTRY`` publishes no
unregister, so the autouse fixture swaps copies of its two tables in through
``monkeypatch`` (``tests/postgres/test_memory_retention.py``'s technique) and the
originals come back afterwards; environment variables go through ``monkeypatch`` too.
"""

from collections.abc import Iterator
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from harness.modules import install, manifest, publish
from rheo_app_cli import main as cli_main_module
from rheo_app_core import startup as startup_module
from rheo_app_core.main import app, lifespan
from rheo_core.modules import (
    ENTRY_POINT_GROUP,
    allowed_module_ids,
    discovered,
    register_module_settings,
    reset_surfaces,
)
from rheo_core.modules.loader import ALLOWLIST_KEY
from rheo_core.settings import KeySpec, Scope, ValueType, env_variable_names, resolve
from rheo_core.settings.deployment import read_deployment_value
from rheo_core.settings.schema import REGISTRY as SETTINGS_REGISTRY
from rheo_core.settings.schema import SettingUndeclared
from rheo_recallatron.configuration import EMBEDDING_PROVIDER_KEY

RECALLATRON = "recallatron"
PROVIDER_VARIABLE = env_variable_names(EMBEDDING_PROVIDER_KEY)[0]
STRAY_VARIABLE = "RHEO__recallatron__no_such_key"

ABSENT_ID = "absent_probe"
ABSENT_KEY = f"{ABSENT_ID}.flag"
ABSENT_VARIABLE = env_variable_names(ABSENT_KEY)[0]
ABSENT_MANIFEST = manifest(
    ABSENT_ID,
    configuration_schema=(
        KeySpec(
            key=ABSENT_KEY,
            type=ValueType.BOOL,
            scope=Scope.DEPLOYMENT,
            floor=None,
            explicit_per_workspace=False,
            default=False,
        ),
    ),
)
ABSENT_ENTRY_POINT = EntryPoint(
    name=ABSENT_ID, value=f"{__name__}:ABSENT_MANIFEST", group=ENTRY_POINT_GROUP
)


def _recallatron_entry_point() -> EntryPoint:
    (real,) = [entry for entry in discovered() if entry.name == RECALLATRON]
    return real


@pytest.fixture(autouse=True)
def restored_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A private copy of the settings tables, and both modules discoverable."""
    monkeypatch.setattr(SETTINGS_REGISTRY, "_specs", dict(SETTINGS_REGISTRY._specs))
    monkeypatch.setattr(SETTINGS_REGISTRY, "_origins", dict(SETTINGS_REGISTRY._origins))
    publish(monkeypatch, _recallatron_entry_point(), ABSENT_ENTRY_POINT)
    reset_surfaces()
    yield
    reset_surfaces()


@pytest.fixture
def provider_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, RECALLATRON)
    monkeypatch.setenv(PROVIDER_VARIABLE, "local")


# --- read_deployment_value and the allowlist ----------------------------------------


def test_read_deployment_value_ignores_an_unrelated_stray(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, RECALLATRON)
    monkeypatch.setenv(STRAY_VARIABLE, "anything")
    assert read_deployment_value(ALLOWLIST_KEY) == (RECALLATRON,)
    assert allowed_module_ids() == frozenset({RECALLATRON})


def test_the_allowlist_falls_back_to_its_empty_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    assert read_deployment_value(ALLOWLIST_KEY) is None
    assert allowed_module_ids() == frozenset()


# --- register_module_settings ---------------------------------------------------------


@pytest.mark.usefixtures("provider_in_environment")
def test_an_allowed_modules_environment_setting_resolves() -> None:
    # The pre-fix failure: the first full resolve refuses the module's own key.
    with pytest.raises(SettingUndeclared):
        resolve()
    assert register_module_settings() == (RECALLATRON,)
    assert resolve().get_str(EMBEDDING_PROVIDER_KEY) == "local"
    # Idempotent.
    assert register_module_settings() == (RECALLATRON,)


def test_an_allowed_modules_deployment_toml_setting_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch)
    monkeypatch.setenv("RHEO_DATA_ROOT", str(tmp_path))
    config = tmp_path / "config"
    config.mkdir()
    (config / "deployment.toml").write_text(
        '[modules]\ninstalled = ["recallatron"]\n\n'
        '[recallatron.embedding]\nprovider = "local"\n',
        encoding="utf-8",
    )
    assert register_module_settings() == (RECALLATRON,)
    assert resolve().get_str(EMBEDDING_PROVIDER_KEY) == "local"


def test_a_stray_module_variable_is_refused_before_and_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, RECALLATRON)
    monkeypatch.setenv(STRAY_VARIABLE, "anything")
    with pytest.raises(SettingUndeclared) as before:
        resolve()
    assert STRAY_VARIABLE in str(before.value)
    assert register_module_settings() == (RECALLATRON,)
    with pytest.raises(SettingUndeclared) as after:
        resolve()
    assert STRAY_VARIABLE in str(after.value)


def test_a_discoverable_module_not_in_the_allowlist_stays_undeclared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, RECALLATRON)
    monkeypatch.setenv(ABSENT_VARIABLE, "true")
    registered = register_module_settings()
    assert ABSENT_ID not in registered
    assert registered == (RECALLATRON,)
    assert ABSENT_KEY not in SETTINGS_REGISTRY
    with pytest.raises(SettingUndeclared) as refused:
        resolve()
    assert ABSENT_VARIABLE in str(refused.value)


# --- the composition roots ------------------------------------------------------------


@pytest.mark.usefixtures("provider_in_environment")
async def test_run_startup_resolves_with_the_module_variable_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real lifespan, with the full module load stubbed out.

    Stubbing ``load_modules`` keeps Recallatron's operations off the process-wide
    registry the rest of the session reads, and it makes the assertion sharper: with
    the full load gone, the early hook is the only thing left that could declare the
    key ``run_startup()``'s first ``resolve()`` reads.
    """
    monkeypatch.setattr(startup_module, "load_modules", lambda **_: ())
    async with lifespan(app):
        assert app.state.startup.profile == "test"
    assert resolve().get_str(EMBEDDING_PROVIDER_KEY) == "local"


@pytest.mark.usefixtures("provider_in_environment")
def test_the_cli_resolves_with_the_module_variable_set() -> None:
    assert cli_main_module.main(["routing", "hosts"]) == 0


@pytest.mark.parametrize(
    "argv", [["openapi", "--out", "-"], ["web", "compose", "--out", "-"]]
)
def test_openapi_and_web_never_register_module_settings(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    install(monkeypatch)
    calls: list[None] = []
    monkeypatch.setattr(
        cli_main_module, "register_module_settings", lambda: calls.append(None)
    )
    assert cli_main_module.main(argv) == 0
    assert calls == []


def test_every_other_command_registers_module_settings_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    calls: list[None] = []
    monkeypatch.setattr(
        cli_main_module, "register_module_settings", lambda: calls.append(None)
    )
    assert cli_main_module.main(["routing", "hosts"]) == 0
    assert calls == [None]

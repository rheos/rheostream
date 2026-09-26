"""Issue #117: ``rheo token issue`` loads the allowed modules, so a CLI-issued token
can carry a module operation.

Before the fix the command registered the core operations and nothing else, so
``cli_full`` and ``read_only`` never named a Recallatron operation and an ``mcp``
token's ``agent_default`` named no tool at all (``register_core_tools()`` was never
called either). The command now runs the same two calls ``apps/core``'s startup makes
after ``register_core_operations()``.

**Driven through ``main(argv)`` with ``modules.installed`` naming Recallatron**, the
way an operator runs it, and checked against the operations stored on the issued
token's own rows.

**The process-wide tables are isolated for the length of each test.** The command
writes into the global operation, tool and resolver registries (``dispatch`` reads
those, so they cannot be local), and the loader writes settings keys, renderings,
owned-delete declarations, audit sinks and its loaded table into process-global
tables that publish no unregister. Left behind, Recallatron's operations would widen
``read_only`` for ``tests/postgres/test_tokens.py``'s exact-set assertions, and its
``explicit_per_workspace`` key would be written into every workspace provisioned later
in the session. Each table is swapped for a copy (or a fresh instance) through
``monkeypatch``, which puts the original back on teardown.
"""

from collections.abc import Iterator
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import install
from rheo_app_cli.main import main
from rheo_core.audit import sink as sink_module
from rheo_core.deletion.registry import OwnedDeletionRegistry
from rheo_core.modules import loader as loader_module
from rheo_core.operations.registry import REGISTRY
from rheo_core.redaction.registry import RenderingRegistry
from rheo_core.refs.resolver import RESOLVERS
from rheo_core.settings.schema import REGISTRY as SETTINGS_REGISTRY
from rheo_core.settings.schema import SettingsRegistry
from rheo_core.storage.control_plane import list_access_token_operations
from rheo_core.tokens.sets import TOOL_REGISTRY

pytestmark = pytest.mark.postgres

RECALLATRON = "recallatron"


@pytest.fixture
def isolated_process_tables(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Copies of every process-global table the command and the loader write."""
    monkeypatch.setattr(REGISTRY, "_operations", dict(REGISTRY._operations))
    monkeypatch.setattr(TOOL_REGISTRY, "_tools", dict(TOOL_REGISTRY._tools))
    monkeypatch.setattr(RESOLVERS, "_resolvers", dict(RESOLVERS._resolvers))
    monkeypatch.setattr(sink_module, "_SINKS", dict(sink_module._SINKS))
    settings = SettingsRegistry()
    for key in SETTINGS_REGISTRY.keys():
        settings.register(
            SETTINGS_REGISTRY.get(key), origin=SETTINGS_REGISTRY.origin_of(key)
        )
    monkeypatch.setattr(loader_module, "SETTINGS_REGISTRY", settings)
    monkeypatch.setattr(loader_module, "RENDERINGS", RenderingRegistry())
    monkeypatch.setattr(loader_module, "OWNED_DELETIONS", OwnedDeletionRegistry())
    monkeypatch.setattr(loader_module, "_LOADED", {})
    yield


def _issue(
    capsys: pytest.CaptureFixture[str],
    workspace: UUID,
    account: UUID,
    *,
    set_name: str,
    kind: str,
) -> tuple[int, str, str]:
    capsys.readouterr()
    code = main(
        [
            "token",
            "issue",
            "--account",
            str(account),
            "--workspace",
            str(workspace),
            "--set",
            set_name,
            "--kind",
            kind,
        ]
    )
    out, err = capsys.readouterr()
    return code, out, err


def _stored_operations(cluster: ClusterSession, err: str) -> frozenset[str]:
    """The issued token's operations, read back off its rows by the id on stderr."""
    first = err.strip().splitlines()[-1].split()
    assert first[0] == "token" and first[2] == "issued,", err
    with cluster.backend.control_engine.connect() as connection:
        return list_access_token_operations(connection, UUID(first[1]))


@pytest.mark.usefixtures("isolated_process_tables")
def test_a_cli_token_carries_the_allowed_modules_operations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    install(monkeypatch, RECALLATRON)

    code, out, err = _issue(
        capsys, workspace, owner_account_id, set_name="read_only", kind="cli"
    )

    assert code == 0, err
    assert out.startswith("rheo_cli_")
    stored = _stored_operations(cluster, err)
    # The issue's own reproduction names this one.
    assert "recallatron.entity.list" in stored
    assert "recallatron.memory.recall" in stored
    assert "core.workspace.status" in stored
    # ``read_only`` still means read: a module's mutate operation stays out.
    assert "recallatron.memory.remember" not in stored


@pytest.mark.usefixtures("isolated_process_tables")
def test_an_mcp_token_holds_the_core_and_module_tools_operations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """``agent_default`` reads the tool registry, which the command never populated:
    before #117 this set was empty even for the core's own tools."""
    install(monkeypatch, RECALLATRON)

    code, out, err = _issue(
        capsys, workspace, owner_account_id, set_name="agent_default", kind="mcp"
    )

    assert code == 0, err
    assert out.startswith("rheo_mcp_")
    stored = _stored_operations(cluster, err)
    assert "recallatron.memory.recall" in stored
    assert "core.workspace.status" in stored


@pytest.mark.usefixtures("isolated_process_tables")
def test_a_module_the_allowlist_does_not_name_stays_out(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """Loading honours ``modules.installed``: an installed distribution the
    deployment did not allow contributes nothing to a token."""
    install(monkeypatch)

    code, _, err = _issue(
        capsys, workspace, owner_account_id, set_name="cli_full", kind="cli"
    )

    assert code == 0, err
    stored = _stored_operations(cluster, err)
    assert "core.workspace.status" in stored
    assert not {name for name in stored if name.startswith(f"{RECALLATRON}.")}

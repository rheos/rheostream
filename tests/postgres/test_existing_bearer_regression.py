"""What the three existing machine callers see today, pinned (issue #287, AC-16).

Seams under test: the ``mcp`` surface's bearer gate and its ``tools/list`` /
``tools/call`` replies as an external caller receives them (status, every header,
the JSON body), and the ``api`` surface's ``core.evidence.ingest`` reply.

**The goldens were captured on the tree before connector sign-in touched anything.**
Each case compares a live response with a JSON file under
``tests/fixtures/existing_bearer/``. The point of capturing first is that "identical
to the pre-change response" is only a claim when the comparison is with the old code,
not with the new code's own output. Four cases:

- ``claude_code_mcp``: a token minted exactly as ``rheo token issue --set
  agent_default --kind mcp`` mints it (through ``main(argv)``), listing tools and
  calling ``recallatron_recall`` on the mounted ``mcp`` surface.
- ``bridge_cli_ingest``: the local bridge's ``cli`` token (``issue_bridge_token``,
  reached through ``core.evidence_enrollment.create`` as an enrolled bridge gets it)
  calling ``core.evidence.ingest`` on the ``api`` surface.
- ``box_bot_mcp``: a separately issued ``agent_default`` ``mcp`` token (the box bot's
  shape, issued by ``core.token.issue`` under an operator context) listing tools and
  calling ``workspace_status``.
- ``unconfigured_401``: an unauthenticated POST through ``build_mcp_app`` directly,
  which answers 401 with ``www-authenticate: Bearer`` exactly.

**Normalization is one closed list, :data:`NORMALIZATION`, and nothing wider.** The
clock is a per-module ``_now()`` in several modules and ids come from
``rheo_core.refs.uuid7``, so there is no single seam to pin; instead the values at the
listed headers and body keys are replaced by :data:`PLACEHOLDER`. Every other value is
compared exactly. A memory's ``ref`` is not an id key by that list's rule, so the
recall case runs over a workspace holding no memory: an empty ``items`` list is the
reply's shape with nothing in it that varies.

**The list is guarded, not trusted.** Every case captures its exchanges twice and
:func:`_assert_only_listed_values_differ` fails if any value outside the list differs
between the two, so the list cannot hide a real difference. Set
``EXISTING_BEARER_CAPTURE=1`` to rewrite the goldens from the first capture (only on an
unchanged tree; the whole point of these files is that they are not regenerated to
match a change).

**Recallatron is loaded into the process-wide tables, which are copies for the length
of each test.** The command and the core lifespan both write the global operation,
tool, resolver, consumer, deletion, rendering, settings and audit-sink tables, and
those publish no unregister, so each is swapped for a copy through ``monkeypatch``
(``tests/postgres/test_cli_token_modules.py``'s reason and technique).
"""

import json
import os
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import httpx2
import pytest
from conftest import ClusterSession
from harness.evidence import EvidenceWorkspace, enable_recording, probe_registry
from harness.modules import install, install_and_enable_module
from rheo_app_cli.main import main
from rheo_app_core import api_routes
from rheo_app_core.main import app, lifespan, public_app
from rheo_app_core.startup import CONSUMERS
from rheo_app_mcp.transport import MCP_PATH, build_mcp_app
from rheo_contracts import Role, WorkspaceContext
from rheo_core.audit import sink as sink_module
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.deletion import OWNED_DELETIONS
from rheo_core.evidence.enrollment import ENROLLMENT_CREATE
from rheo_core.evidence.ingest import EVIDENCE_INGEST
from rheo_core.modules import load_modules
from rheo_core.modules import loader as loader_module
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.core_ops import TOKEN_ISSUE
from rheo_core.operations.registry import REGISTRY
from rheo_core.redaction.registry import RENDERINGS
from rheo_core.refs.resolver import RESOLVERS
from rheo_core.routing import MCP, RoutingConfig, url_for
from rheo_core.settings import resolve
from rheo_core.settings.schema import REGISTRY as SETTINGS_REGISTRY
from rheo_core.storage.evidence_enrollment_tables import evidence_enrollment
from rheo_core.storage.evidence_tables import evidence_unit
from rheo_core.tokens.sets import TOOL_REGISTRY, register_core_tools
from sqlalchemy import delete, inspect

pytestmark = pytest.mark.postgres

RECALLATRON: Final = "recallatron"
BASE_HOST: Final = "example.test"
GOLDEN_DIR: Final = Path(__file__).resolve().parents[1] / "fixtures" / "existing_bearer"
CAPTURE_ENV: Final = "EXISTING_BEARER_CAPTURE"
PLACEHOLDER: Final = "<normalized>"


@dataclass(frozen=True)
class Normalization:
    """The closed list of values that may differ run to run."""

    headers: frozenset[str]
    body_keys: frozenset[str]
    body_key_suffixes: tuple[str, ...]

    def header(self, name: str) -> bool:
        return name.lower() in self.headers

    def body_key(self, key: str) -> bool:
        return key in self.body_keys or key.endswith(self.body_key_suffixes)


NORMALIZATION: Final = Normalization(
    headers=frozenset({"date", "content-length", "mcp-session-id"}),
    body_keys=frozenset({"id"}),
    body_key_suffixes=("_id", "_at"),
)
"""Headers ``date``, ``content-length`` and the MCP session id; body values at the
key ``id`` and at any key ending ``_id`` or ``_at``. Declared once; nothing else is
normalized anywhere in this module."""

_MCP_HEADERS: Final = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
_NATIVE_KEY: Final = "cc1:" + "0123456789abcdef" * 4
"""A fixed, fictional record key, so the ingest reply's ``accepted`` list is the same
on every run. The second capture re-presents it, which the ingest answers as a replay
with the same tuples (AC 6 of the bridge run)."""
_RECORD_TEXT: Final = "Remember that the spare bike pump is under the stairs."


# --- capture, the guard, the golden ----------------------------------------------


def _exchange(label: str, response: httpx2.Response) -> dict[str, Any]:
    return {
        "label": label,
        "status": response.status_code,
        "headers": [[k.lower(), v] for k, v in response.headers.multi_items()],
        "body": response.json(),
    }


def _differing_paths(
    first: object, second: object, path: tuple[str | int, ...] = ()
) -> list[tuple[tuple[str | int, ...], bool]]:
    """Every path where two captures differ, each with whether a key-set or length
    difference (a structural one, never normalizable) is what differs there."""
    if isinstance(first, dict) and isinstance(second, dict):
        if set(first) != set(second):
            return [(path, True)]
        found: list[tuple[tuple[str | int, ...], bool]] = []
        for key in first:
            found += _differing_paths(first[key], second[key], (*path, key))
        return found
    if isinstance(first, list) and isinstance(second, list):
        if len(first) != len(second):
            return [(path, True)]
        found = []
        for index, (a, b) in enumerate(zip(first, second, strict=True)):
            found += _differing_paths(a, b, (*path, index))
        return found
    return [] if first == second else [(path, False)]


def _listed_body_path(path: tuple[str | int, ...]) -> bool:
    """Whether the body value at ``path`` in an exchange list is on the closed list."""
    if len(path) >= 2 and path[1] == "body":
        last = path[-1]
        return isinstance(last, str) and NORMALIZATION.body_key(last)
    return False


def _assert_only_listed_values_differ(
    first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> None:
    unlisted: list[tuple[str | int, ...]] = []
    for path, structural in _differing_paths(first, second):
        if structural:
            unlisted.append(path)
            continue
        if len(path) == 4 and path[1] == "headers" and path[3] == 1:
            exchange, index = path[0], path[2]
            assert isinstance(exchange, int) and isinstance(index, int)
            if NORMALIZATION.header(first[exchange]["headers"][index][0]):
                continue
        elif _listed_body_path(path):
            continue
        unlisted.append(path)
    assert not unlisted, (
        "two captures differ outside NORMALIZATION; the list would hide a real "
        f"difference at {unlisted}"
    )


def _normalize_body(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: PLACEHOLDER if NORMALIZATION.body_key(key) else _normalize_body(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_body(item) for item in value]
    return value


def _normalize(exchanges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "label": exchange["label"],
            "status": exchange["status"],
            "headers": [
                [name, PLACEHOLDER if NORMALIZATION.header(name) else value]
                for name, value in exchange["headers"]
            ],
            "body": _normalize_body(exchange["body"]),
        }
        for exchange in exchanges
    ]


def _assert_matches_golden(
    case: str, first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> None:
    _assert_only_listed_values_differ(first, second)
    normalized = _normalize(first)
    text = json.dumps({"exchanges": normalized}, indent=2, ensure_ascii=False) + "\n"
    for prefix in ("rheo_cli_", "rheo_mcp_", "rheo_rt_"):
        assert prefix not in text, f"a token value would land in the {case} golden"
    golden = GOLDEN_DIR / f"{case}.json"
    if os.environ.get(CAPTURE_ENV) == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(text, encoding="utf-8")
    assert golden.exists(), f"{golden} is missing; capture it on an unchanged tree"
    expected = json.loads(golden.read_text(encoding="utf-8"))
    assert {"exchanges": normalized} == expected


def test_the_guard_admits_only_the_listed_values() -> None:
    """The guard itself: a listed header or key may differ between captures, and an
    unlisted one, or a structural difference, fails."""

    def capture(**overrides: object) -> list[dict[str, Any]]:
        body: dict[str, object] = {"created_at": "t1", "ref": "r1", "token_id": "a"}
        headers = [["content-length", "10"], ["content-type", "application/json"]]
        exchange: dict[str, Any] = {"label": "x", "status": 200}
        exchange["headers"] = overrides.pop("headers", headers)
        exchange["body"] = {**body, **overrides}
        return [exchange]

    _assert_only_listed_values_differ(
        capture(),
        capture(
            created_at="t2",
            token_id="b",
            headers=[["content-length", "11"], ["content-type", "application/json"]],
        ),
    )
    for second in (
        capture(ref="r2"),
        capture(extra=1),
        capture(headers=[["content-length", "10"], ["content-type", "text/plain"]]),
    ):
        with pytest.raises(AssertionError):
            _assert_only_listed_values_differ(capture(), second)


# --- the process-wide tables, isolated ---------------------------------------------


@pytest.fixture
def isolated_process_tables(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A copy of every process-global table the loader, the command and the core
    lifespan write, put back on teardown."""
    monkeypatch.setattr(REGISTRY, "_operations", dict(REGISTRY._operations))
    monkeypatch.setattr(TOOL_REGISTRY, "_tools", dict(TOOL_REGISTRY._tools))
    monkeypatch.setattr(RESOLVERS, "_resolvers", dict(RESOLVERS._resolvers))
    monkeypatch.setattr(CONSUMERS, "_consumers", dict(CONSUMERS._consumers))
    monkeypatch.setattr(OWNED_DELETIONS, "_owners", dict(OWNED_DELETIONS._owners))
    monkeypatch.setattr(
        OWNED_DELETIONS, "_participants", list(OWNED_DELETIONS._participants)
    )
    monkeypatch.setattr(RENDERINGS, "_entries", dict(RENDERINGS._entries))
    monkeypatch.setattr(SETTINGS_REGISTRY, "_specs", dict(SETTINGS_REGISTRY._specs))
    monkeypatch.setattr(SETTINGS_REGISTRY, "_origins", dict(SETTINGS_REGISTRY._origins))
    monkeypatch.setattr(sink_module, "_SINKS", dict(sink_module._SINKS))
    monkeypatch.setattr(loader_module, "_LOADED", {})
    yield


@pytest.fixture
def path_mode(monkeypatch: pytest.MonkeyPatch) -> str:
    """The mounted surface in path mode on ``example.test``; the runtime's own URL."""
    monkeypatch.setenv("RHEO__routing__mode", "path")
    monkeypatch.setenv("RHEO__routing__scheme", "https")
    monkeypatch.setenv("RHEO__routing__base_host", BASE_HOST)
    monkeypatch.setenv("RHEO__routing__public_host", "")
    return url_for(RoutingConfig.from_settings(resolve()), MCP, "/")


@pytest.fixture
def recallatron_workspace(
    monkeypatch: pytest.MonkeyPatch,
    isolated_process_tables: None,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> UUID:
    """Recallatron allowed, loaded as the core process loads it, and enabled in
    ``workspace``; no memory is written."""
    register_core_operations()
    register_core_tools()
    install(monkeypatch, RECALLATRON)
    assert load_modules(consumers=CONSUMERS, deletions=OWNED_DELETIONS) == (
        RECALLATRON,
    )
    bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(bootstrap, WorkspaceContext), bootstrap
    install_and_enable_module(cluster.backend, bootstrap, workspace, RECALLATRON)
    return workspace


def _origin(url: str) -> str:
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{rest.split('/', 1)[0]}"


async def _mounted_client(url: str) -> AsyncIterator[httpx2.AsyncClient]:
    async with lifespan(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=_origin(url)
        ) as http_client:
            yield http_client


def _rpc(method: str, params: Mapping[str, object], rpc_id: int) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": dict(params)}


async def _list_and_call(
    url: str, bearer: str, tool: str, arguments: Mapping[str, object]
) -> list[dict[str, Any]]:
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {bearer}"}
    async for http_client in _mounted_client(url):
        listed = await http_client.post(
            url, json=_rpc("tools/list", {}, 1), headers=headers
        )
        called = await http_client.post(
            url,
            json=_rpc("tools/call", {"name": tool, "arguments": dict(arguments)}, 2),
            headers=headers,
        )
    return [_exchange("tools/list", listed), _exchange(f"tools/call {tool}", called)]


# --- (a) Claude Code: a `rheo token issue` mcp token on the mounted surface ---------


def _cli_issue(
    capsys: pytest.CaptureFixture[str], workspace: UUID, account: UUID
) -> str:
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
            "agent_default",
            "--kind",
            "mcp",
        ]
    )
    out, err = capsys.readouterr()
    assert code == 0, err
    return out.strip()


async def test_a_claude_code_token_lists_and_recalls_as_before(
    capsys: pytest.CaptureFixture[str],
    path_mode: str,
    recallatron_workspace: UUID,
    owner_account_id: UUID,
) -> None:
    captures = [
        await _list_and_call(
            path_mode,
            _cli_issue(capsys, recallatron_workspace, owner_account_id),
            "recallatron_recall",
            {"query": "where is the spare kettle"},
        )
        for _ in range(2)
    ]
    _assert_matches_golden("claude_code_mcp", *captures)


# --- (c) the box bot: a separately issued agent_default mcp token -------------------


def _operator_issue(workspace: UUID, account: UUID) -> str:
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext), ctx
    outcome = dispatch(
        ctx,
        TOKEN_ISSUE,
        {"kind": "mcp", "set_name": "agent_default", "account_id": str(account)},
    )
    assert outcome.ok and outcome.result is not None, outcome
    return str(outcome.result.value)  # type: ignore[attr-defined]


async def test_a_box_bot_token_lists_and_calls_as_before(
    path_mode: str, recallatron_workspace: UUID, owner_account_id: UUID
) -> None:
    captures = [
        await _list_and_call(
            path_mode,
            _operator_issue(recallatron_workspace, owner_account_id),
            "workspace_status",
            {},
        )
        for _ in range(2)
    ]
    _assert_matches_golden("box_bot_mcp", *captures)


# --- (b) the bridge: a cli token ingesting on the api surface ----------------------


@pytest.fixture
def bridge_workspace(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[EvidenceWorkspace]:
    """Recording on with the probe subscriber, as the HTTP ingest tests run it, and
    every evidence and enrollment row removed afterwards (#238's rule)."""
    register_core_operations()
    evidence = EvidenceWorkspace(cluster, workspace, owner_account_id)
    enable_recording(monkeypatch, evidence)
    monkeypatch.setattr(api_routes, "CONSUMERS", probe_registry())
    yield evidence
    engine = cluster.backend.pools.engine_for(evidence.database_name)
    inspector = inspect(engine)
    with engine.begin() as connection:
        if inspector.has_table("evidence_unit", schema="core"):
            connection.execute(delete(evidence_unit))
        if inspector.has_table("evidence_enrollment", schema="core"):
            connection.execute(delete(evidence_enrollment))


def _enrolled_bridge_token(workspace: UUID, account: UUID) -> tuple[str, str, str]:
    """An enrollment's token (minted by ``issue_bridge_token``) and its fingerprints."""
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext), ctx
    machine, project = "a1" * 32, "b2" * 32
    outcome = dispatch(
        ctx,
        ENROLLMENT_CREATE,
        {
            "machine_fingerprint": machine,
            "project_fingerprint": project,
            "account_id": str(account),
        },
    )
    assert outcome.ok and outcome.result is not None, outcome
    return str(outcome.result.value), machine, project  # type: ignore[attr-defined]


async def test_a_bridge_token_ingests_as_before(
    bridge_workspace: EvidenceWorkspace,
) -> None:
    value, machine, project = _enrolled_bridge_token(
        bridge_workspace.workspace_id, bridge_workspace.owner_account_id
    )
    recorded_at = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    payload = {
        "machine_fingerprint": machine,
        "project_fingerprint": project,
        "records": [
            {
                "native_key": _NATIVE_KEY,
                "recorded_at": recorded_at,
                "text": _RECORD_TEXT,
            }
        ],
        "gaps": [],
    }
    captures: list[list[dict[str, Any]]] = []
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=public_app), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.post(
                f"/api/v1/operations/{EVIDENCE_INGEST}",
                headers={"Authorization": f"Bearer {value}"},
                json=payload,
            )
            captures.append([_exchange(EVIDENCE_INGEST, response)])
    _assert_matches_golden("bridge_cli_ingest", *captures)


# --- the unconfigured 401 ----------------------------------------------------------


async def test_an_unauthenticated_post_is_a_bare_bearer_401_as_before() -> None:
    captures: list[list[dict[str, Any]]] = []
    for _ in range(2):
        # One application per capture: the SDK's session manager runs once only.
        mcp_app = build_mcp_app(consumers=None)
        async with mcp_app.router.lifespan_context(mcp_app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=mcp_app),
                base_url="http://127.0.0.1:8100",
            ) as http_client:
                response = await http_client.post(
                    MCP_PATH, json=_rpc("tools/list", {}, 1), headers=_MCP_HEADERS
                )
        captures.append([_exchange("tools/list", response)])
    assert captures[0][0]["status"] == 401
    _assert_matches_golden("unconfigured_401", *captures)

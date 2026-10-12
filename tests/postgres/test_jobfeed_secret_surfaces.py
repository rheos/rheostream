"""Every surface of the job feed connector, swept for secrets and organization ids.

Seams under test: the owner operations (connect, complete, revoke, reconnect) through
the real dispatcher, the collector job through a real scheduler tick and worker visit
on a fake clock, and the real workspace export and restore, against a fake
authorization server and a fake MCP server whose registration, token and MCP answers
carry marker strings minted at run time (so no tracked file can hold one). After each
stage the sweep reads every observable surface: a row dump of every table in the
workspace and control databases, column by column; the owner health read; the owner
and member catalog projections; the Leads export; captured log records (root,
``rheo_*`` and ``httpx``, messages and formatted exception text); the MCP tool list
and the result of every read tool; every operation outcome the test produced,
refusals included; the secret store's files; and every file the test touched under
the repository and the data root. The failure-path tests make the fakes answer with
markers in failing bodies, headers and malformed JSON, and also call the core OAuth
and MCP clients directly, so the raised exception's ``str``, ``repr``, ``__cause__``
and ``__context__`` can be read.
"""

import base64
import json
import logging
import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlencode
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import MakeWorkspace
from harness.jobfeed import (
    ISSUER,
    RESOURCE_URL,
    ConnectedCollector,
    FakeAuthorizationServer,
    FakeClock,
    FakeMcpServer,
    FakeResolver,
    Responder,
    Tokens,
    account,
    collect_jobs,
    configure_connector,
    connected_collector,
    feed_row,
    grant_type,
    invoke,
    issued,
    jobsearch_pipeline,
    owner_web,
    serve,
    visit,
)
from rheo_contracts import Role, SafetyClass, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.connectors import oauth
from rheo_core.connectors.mcp_client import McpFailure, McpSession
from rheo_core.connectors.oauth import AccessToken, ClientAuth, OAuthFailure
from rheo_core.exports.artifact import read_archive
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.tool_facade import call_registered_tool, visible_tools
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads import MANIFEST
from rheo_leads.jobfeed.run import JobfeedFailure
from rheo_leads.jobfeed.upstream import READ_ONLY_POLICY
from rheo_leads.references import ref
from sqlalchemy import Connection, Engine, event, text

from postgres.test_jobfeed_authorization import (
    COMPLETE,
    authorize,
    connection,
    set_connection,
    stored,
)
from postgres.test_jobfeed_connect import (
    CONNECT,
    RECONNECT,
    capability,
    file_of,
    member_context,
    pending,
    run,
)
from postgres.test_jobfeed_revoke_select import HEALTH, REVOKE, owner_token
from postgres.test_jobfeed_run import NoToken, processed
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import export_workspace
from postgres.test_memory_export_restore import _remove_workspace, _restore

pytestmark = pytest.mark.postgres

JOB_HOST = "www.example.com"
HOUR = 3600.0
TOKEN_ENDPOINT = f"{ISSUER}/token"
REVOCATION_ENDPOINT = f"{ISSUER}/revoke"
SKIPPED_DIRS = frozenset(
    {".git", "node_modules", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache"}
)
REPO_ROOT = Path(__file__).resolve().parents[2]


def marker(kind: str) -> str:
    """A marker minted now, so no tracked file can carry it."""
    return f"{kind}-marker-{uuid4()}"


class InjectedFailure(Exception):
    """Raised from a test listener to fail a statement on purpose."""


# --- the surfaces ---------------------------------------------------------------------


class Capture(logging.Handler):
    """Every record any of the watched loggers sees, at every level."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        seen: set[int] = set()
        lines: list[str] = []
        for record in self.records:
            if id(record) in seen:
                continue
            seen.add(id(record))
            # Formatting renders the message, the exception text and any stack.
            lines.append(self.formatter.format(record))
            lines.append(repr(record.args))
        return "\n".join(lines)


@pytest.fixture
def logs() -> Iterator[Capture]:
    capture = Capture()
    names = ("", "rheo_core", "rheo_leads", "rheo_app_worker", "httpx", "httpcore")
    loggers = [logging.getLogger(name) for name in names]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(capture)
        logger.setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.removeHandler(capture)
            logger.setLevel(level)


def _table_columns(connection: Connection, prefix: str) -> dict[str, str]:
    """``<prefix>:<schema>.<table>.<column>`` to the column's values as text, for
    every base table outside the system schemas."""
    tables = connection.execute(
        text(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE' AND table_schema NOT IN "
            "('pg_catalog', 'information_schema') ORDER BY 1, 2"
        )
    ).all()
    found: dict[str, list[str]] = {}
    for schema, table in tables:
        rows = connection.execute(
            text(f'SELECT * FROM "{schema}"."{table}"')  # noqa: S608 - catalog names
        ).mappings()
        for row in rows:
            # Job rows are told apart by kind, so the collector's own are named.
            name = f"{table}[{row['kind']}]" if table == "job" else table
            for column, value in row.items():
                found.setdefault(f"{prefix}:{schema}.{name}.{column}", []).append(
                    repr(value) + " " + str(value)
                )
    return {key: "\n".join(values) for key, values in found.items()}


def database_columns(i: Intake, ctx: WorkspaceContext) -> dict[str, str]:
    with open_unit_of_work(ctx) as uow:
        columns = _table_columns(uow.connection, "workspace")
    with i.cluster.backend.control_engine.connect() as control:
        columns.update(_table_columns(control, "control"))
    return columns


def health_of(i: Intake, ctx: WorkspaceContext, connection_id: UUID) -> Any:
    outcome = run(
        i, ctx, HEALTH, connection_ref=ref("intake_connection", connection_id)
    )
    assert outcome.ok, outcome
    return outcome.result


def catalog_of(i: Intake, ctx: WorkspaceContext) -> list[dict[str, Any]]:
    outcome = run(i, ctx, "leads.ui.catalog")
    assert outcome.ok, outcome
    return list(outcome.result.items)  # type: ignore[union-attr]


def touched_files(roots: Iterable[Path], since: float) -> dict[str, str]:
    """Every file under ``roots`` modified at or after ``since``, as text."""
    found: dict[str, str] = {}
    for top in roots:
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    if path.stat().st_mtime < since:
                        continue
                    found[str(path)] = path.read_bytes().decode("utf-8", "replace")
                except OSError:
                    continue
    return found


@dataclass
class Leaks:
    """The values the sweep hunts for, collected as the lifecycle issues them."""

    secrets: set[str] = field(default_factory=set)
    """Refresh and access tokens, codes, verifiers, client secrets, registration
    tokens: never readable anywhere but the store's files (the verifier: its one
    pending-row column)."""
    references: set[str] = field(default_factory=set)
    """Secret references, their file names and the data root's paths."""
    client_ids: set[str] = field(default_factory=set)
    organizations: set[str] = field(default_factory=set)


# Where a value may be read, by kind; every other surface must not carry it.
REFERENCE_COLUMNS = frozenset(
    {
        "workspace:leads.jobfeed_connection.token_ref",
        "workspace:leads.jobfeed_connection.client_secret_ref",
        "workspace:leads.jobfeed_connection.registration_token_ref",
        "workspace:leads.jobfeed_authorization.client_secret_ref",
        "workspace:leads.jobfeed_authorization.registration_token_ref",
    }
)
VERIFIER_COLUMN = "workspace:leads.jobfeed_authorization.code_verifier"
ORGANIZATION_COLUMNS = frozenset(
    {
        "workspace:leads.jobfeed_connection.organization_id",
        "workspace:leads.jobfeed_account_choice.organization_id",
    }
)
# The operator's export and restore record their own artifact paths, which sit
# under the data root: that path is the operator's, never a secret's.
OPERATOR_PATH_PREFIXES = (
    "workspace:core.job[core.workspace.export].",
    "workspace:core.job[core.workspace.restore].",
    "workspace:core.export_record.",
    "workspace:core.restore_record.",
)


def present(values: Iterable[str], surfaces: dict[str, str]) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, body in surfaces.items()
        for value in values
        if value and value in body
    ]


@dataclass
class Sweep:
    """The lifecycle's sweep: what it reads, what it has produced so far."""

    i: Intake
    root: Path
    logs: Capture
    leaks: Leaks
    started: float = field(default_factory=time.time)
    outputs: list[str] = field(default_factory=list)
    export_text: str | None = None

    def op(
        self, ctx: WorkspaceContext, name: str, /, **payload: Any
    ) -> OperationOutcome:
        outcome = run(self.i, ctx, name, **payload)
        self.outputs.append(repr(outcome))
        return outcome

    def secret_files(self) -> dict[Path, str]:
        return {
            path: path.read_bytes().decode("utf-8", "replace")
            for path in (self.root / "secrets").rglob("*")
            if path.is_file()
        }

    def projections(
        self, owner: WorkspaceContext, connection_id: UUID | None
    ) -> dict[str, str]:
        """Member- and agent-readable projections, and the owner's non-health
        reads: none may carry a secret, a reference or an organization id."""
        member = member_context(self.i)
        surfaces = {
            "catalog.owner": repr(catalog_of(self.i, owner)),
            "catalog.member": repr(catalog_of(self.i, member)),
            "outputs": "\n".join(self.outputs),
        }
        if connection_id is not None:
            refused = run(
                self.i,
                member,
                HEALTH,
                connection_ref=ref("intake_connection", connection_id),
            )
            assert not refused.ok, refused
            surfaces["health.member"] = repr(refused)
        # An owner MCP token for every read tool Leads declares: the list, and
        # each tool's answer to an empty call (refusals included).
        reads = sorted(
            tool.operation
            for tool in MANIFEST.tools
            if tool.safety_class is SafetyClass.READ
        )
        agent = owner_token(self.i, owner, "mcp", "mcp", operations=reads)
        listed = visible_tools(
            agent, tools=self.i.surfaces.tools, registry=self.i.surfaces.operations
        )
        assert listed, "the tool list is empty"
        surfaces["tools.list"] = repr(
            [
                (tool.name, tool.description, tool.input_model.model_json_schema())
                for tool in listed
            ]
        )
        surfaces["tools.results"] = repr(
            [
                call_registered_tool(
                    agent,
                    tool.name,
                    {},
                    consumers=self.i.consumers,
                    tools=self.i.surfaces.tools,
                    registry=self.i.surfaces.operations,
                )
                for tool in listed
            ]
        )
        return surfaces

    def check(self, stage: str, owner: WorkspaceContext) -> None:
        """Sweep every surface after ``stage``."""
        current = connection(owner)
        connection_id = None if current is None else current.connection_id
        leaks = self.leaks
        if current is not None:
            for reference in (
                current.token_ref,
                current.client_secret_ref,
                current.registration_token_ref,
            ):
                if reference is not None:
                    leaks.references.add(reference)
                    leaks.references.add(file_of(self.root, reference).name)
            if current.client_id is not None:
                leaks.client_ids.add(current.client_id)
        for row in pending(owner):
            leaks.secrets.add(row["code_verifier"])
            for name in ("client_secret_ref", "registration_token_ref"):
                if row[name] is not None:
                    leaks.references.add(row[name])
                    leaks.references.add(file_of(self.root, row[name]).name)
            if row["client_id"] is not None:
                leaks.client_ids.add(row["client_id"])

        columns = database_columns(self.i, owner)
        found = present(leaks.secrets, columns)
        # The verifier lives only in its one pending-row column.
        verifiers = {row["code_verifier"] for row in pending(owner)}
        assert [
            (where, value)
            for where, value in found
            if not (where == VERIFIER_COLUMN and value in verifiers)
        ] == [], stage
        assert [
            (where, value)
            for where, value in present(leaks.references, columns)
            if where not in REFERENCE_COLUMNS
            and not (
                value == str(self.root) and where.startswith(OPERATOR_PATH_PREFIXES)
            )
        ] == [], stage
        assert [
            where
            for where, _ in present(leaks.organizations, columns)
            if where not in ORGANIZATION_COLUMNS
        ] == [], stage

        surfaces = self.projections(owner, connection_id)
        surfaces["logs"] = self.logs.text()
        if connection_id is not None:
            health = health_of(self.i, owner, connection_id).model_dump(mode="json")
            collector = health.get("collector") or {}
            carried = {
                "organization_id": collector.pop("organization_id", None),
                "account_choices": collector.pop("account_choices", None),
            }
            surfaces["health.owner"] = json.dumps(health)
            assert present(leaks.secrets | leaks.references, {"x": repr(carried)}) == []
        surfaces.update(touched_files((REPO_ROOT,), self.started))
        surfaces.update(
            {
                f"data:{path}": body
                for path, body in touched_files((self.root,), self.started).items()
                if not Path(path).is_relative_to(self.root / "secrets")
                and not path.endswith(".tar.zst")
            }
        )
        everything = leaks.secrets | leaks.references | leaks.organizations
        assert present(everything, surfaces) == [], stage

        if self.export_text is not None:
            exported = {"export": self.export_text}
            assert present(leaks.secrets | leaks.references, exported) == [], stage
            assert present(leaks.client_ids, exported) == [], stage

        # The store: each file holds one issued refresh token, client secret or
        # registration token and nothing else; no access token, code or verifier
        # is ever written to it.
        storable = {
            value
            for value in leaks.secrets
            if value.startswith(
                (
                    "refresh-marker-",
                    "client-secret-marker-",
                    "registration-token-marker-",
                )
            )
        }
        for path, body in self.secret_files().items():
            assert body in storable, (stage, path)


# --- the lifecycle --------------------------------------------------------------------


@dataclass
class Upstream:
    """The fakes, issuing fresh markers on every registration and token answer."""

    method: str
    org: str = field(default_factory=lambda: marker("org"))
    client_secrets: list[str] = field(default_factory=list)
    registration_tokens: list[str] = field(default_factory=list)
    tokens: Tokens = field(default_factory=lambda: Tokens(rotating=True))
    failing_refreshes: list[Responder] = field(default_factory=list)

    def register(self, request: httpx.Request) -> httpx.Response:
        secret, registration = marker("client-secret"), marker("registration-token")
        self.client_secrets.append(secret)
        self.registration_tokens.append(registration)
        return issued(
            client_secret=secret,
            registration_access_token=registration,
            token_endpoint_auth_method=self.method,
        )(request)

    def token(self, request: httpx.Request) -> httpx.Response:
        if grant_type(request) == "refresh_token" and self.failing_refreshes:
            return self.failing_refreshes.pop(0)(request)
        return self.tokens(request)


def leaking_body(values: Iterable[str]) -> str:
    return " ".join(values)


def basic_secret(request: httpx.Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return None
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode("utf-8")
    return unquote_plus(decoded.split(":", 1)[1])


def request_text(request: httpx.Request) -> str:
    """Everything a request carries, with HTTP Basic credentials decoded."""
    headers = "\n".join(f"{k}: {v}" for k, v in request.headers.multi_items())
    body = request.content.decode("utf-8", "replace")
    basic = basic_secret(request) or ""
    return (
        f"{request.method} {request.url}\n{headers}\n{basic}\n{body}\n"
        f"{unquote_plus(body)}"
    )


def url_of(request: httpx.Request) -> str:
    return str(request.url.copy_with(query=None))


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
def test_no_secret_or_organization_id_reaches_any_surface_across_the_lifecycle(
    intake: Intake,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
    logs: Capture,
    method: str,
) -> None:
    """AC 10, AC 11, AC 12 (the export), AC 80 end to end; I1, I6, I12."""
    configure_connector(monkeypatch, tmp_path, job_hosts=JOB_HOST)
    root = tmp_path
    clock = FakeClock()
    up = Upstream(method)
    server = FakeAuthorizationServer(registration=up.register, token=up.token)
    mcp = FakeMcpServer(
        accounts=[account(up.org)],
        feeds={"best_match": [[feed_row(130), feed_row(131)]]},
    )
    leaks = Leaks(organizations={up.org})
    leaks.references.update({str(root), str(root / "secrets")})
    sweep = Sweep(intake, root, logs, leaks)

    def issued_now() -> None:
        leaks.secrets.update(up.tokens.issued)
        leaks.secrets.update(up.client_secrets)
        leaks.secrets.update(up.registration_tokens)
        leaks.secrets.update(server.codes)

    def tick_run(owner: WorkspaceContext, hours: float = 26.0) -> None:
        clock.advance(hours * HOUR)
        visit(intake, clock)

    def drained(owner: WorkspaceContext, connection_id: UUID) -> None:
        processed(
            ConnectedCollector(
                intake=intake,
                pipeline_id=pipeline_id,
                connection_id=connection_id,
                owner=owner,
                poll=owner,
            )
        )

    def reconnected(owner: WorkspaceContext) -> None:
        params = authorize(server, sweep.op(owner, RECONNECT))
        issued_now()
        sweep.check("reconnect started", owner)
        outcome = sweep.op(owner, COMPLETE, **params)
        assert outcome.ok and outcome.result.state == "connected", outcome  # type: ignore[union-attr]
        issued_now()
        assert pending(owner) == []

    with serve(server, clock=clock, resolver=FakeResolver(), mcp=mcp):
        owner = owner_web(intake)
        pipeline_id = jobsearch_pipeline(intake, owner)
        sweep.check("installed", owner)

        # Connect: one registration issuing a client secret and a registration
        # token; the pending row holds the verifier in its one column.
        files_before = set(sweep.secret_files())
        params = authorize(
            server, sweep.op(owner, CONNECT, pipeline_ref=ref("pipeline", pipeline_id))
        )
        issued_now()
        (row,) = pending(owner)
        assert row["code_verifier"] and len(row["code_verifier"]) == 86
        written = {
            body for p, body in sweep.secret_files().items() if p not in files_before
        }
        assert written == {up.client_secrets[0], up.registration_tokens[0]}
        sweep.check("connect", owner)

        # Complete: the code is exchanged, the verifier's row is consumed, and the
        # only new store write is the refresh token.
        files_before = set(sweep.secret_files())
        completed = sweep.op(owner, COMPLETE, **params)
        assert completed.ok and completed.result.state == "connected", completed  # type: ignore[union-attr]
        issued_now()
        assert pending(owner) == []
        connected_row = stored(owner)
        connection_id = connected_row.connection_id
        assert connected_row.client_auth_method == method
        first_refresh = up.tokens.issued[1]
        written = {
            body for p, body in sweep.secret_files().items() if p not in files_before
        }
        assert written == {first_refresh}
        replay = sweep.op(owner, COMPLETE, **params)
        assert replay.ok and replay.result.state == "refused", replay  # type: ignore[union-attr]
        sweep.check("complete", owner)

        # A run: the rotating refresh retires the first token, the account list
        # carries the organization id, the selection is stored and sent upstream.
        before_run = stored(owner)
        files_before = set(sweep.secret_files())
        tick_run(owner, 0)
        drained(owner, connection_id)
        issued_now()
        after_run = stored(owner)
        assert after_run.organization_id == up.org
        assert after_run.token_ref != before_run.token_ref
        rotated = up.tokens.issued[-1]
        assert rotated.startswith("refresh-marker-")
        files_after = sweep.secret_files()
        assert {b for p, b in files_after.items() if p not in files_before} == {rotated}
        assert first_refresh not in files_after.values()
        assert file_of(root, str(before_run.token_ref)) not in files_after
        changed = {
            name
            for name in before_run.__dataclass_fields__
            if getattr(before_run, name) != getattr(after_run, name)
        }
        assert {"token_ref", "credential_generation", "organization_id"} <= changed
        assert not changed & {
            "client_id",
            "client_secret_ref",
            "registration_token_ref",
            "client_auth_method",
            "client_redirect_uri",
        }
        assert [j.state for j in collect_jobs(owner)] == ["succeeded"]
        health = health_of(intake, owner, connection_id).collector
        assert health.organization_id == up.org
        assert [c.organization_id for c in health.account_choices] == [up.org]
        # Positive controls: the sweep reads the columns, the logs and the tools
        # it clears, and finds each carried value where it belongs.
        columns = database_columns(intake, owner)
        assert {w for w, _ in present([up.org], columns)} == ORGANIZATION_COLUMNS
        assert {w for w, _ in present([str(after_run.token_ref)], columns)} == {
            "workspace:leads.jobfeed_connection.token_ref"
        }
        assert "HTTP Request: POST https://auth.example.com/token" in logs.text()
        projected = sweep.projections(owner, connection_id)
        assert "leads_list" in projected["tools.list"]
        assert up.org in json.dumps(
            health_of(intake, owner, connection_id).model_dump(mode="json")
        )
        sweep.check("first run", owner)

        # A failed refresh whose answer carries every marker, the live refresh
        # token included.
        page = f"<html>{leaking_body([rotated, *leaks.secrets, up.org])}</html>"
        up.failing_refreshes.append(
            lambda request: httpx.Response(
                500,
                content=page.encode(),
                headers={"Content-Type": "text/html", "X-Org": up.org},
            )
        )
        tick_run(owner)
        assert stored(owner).token_ref == after_run.token_ref
        assert health_of(intake, owner, connection_id).last_error == (
            "token_refresh_failed"
        )
        sweep.check("failed refresh", owner)

        # Revoke a live token: the revocation endpoint gets that token and the
        # client authentication, nothing else.
        revoked = sweep.op(owner, REVOKE)
        assert revoked.ok, revoked
        assert stored(owner).token_ref is None
        assert file_of(root, str(after_run.token_ref)) not in sweep.secret_files()
        sweep.check("revoke", owner)

        # Reconnect (the stored client is reused), then a run after the refresh
        # token file was removed by hand: the store's refusal names a path, but
        # only the fixed code may surface.
        registrations = len(server.registrations)
        reconnected(owner)
        assert len(server.registrations) == registrations
        reference = stored(owner).token_ref
        assert reference is not None
        sweep.check("reconnect", owner)
        capability(intake, owner, False)
        file_of(root, reference).unlink()
        capability(intake, owner, True)
        token_calls = len(server.token_requests)
        tick_run(owner, 2)
        assert len(server.token_requests) == token_calls
        assert stored(owner).authorization_state == "authorization_required"
        sweep.check("removed refresh token file", owner)

        # A reconnect in flight (its verifier pending) while the workspace is
        # exported: neither the verifier, a reference nor the client goes out.
        started = sweep.op(owner, RECONNECT)
        assert started.ok, started
        assert len(pending(owner)) == 1
        sweep.check("pending reconnect", owner)
        artifact = export_workspace(intake)
        entries = read_archive(artifact)
        sweep.export_text = "\n".join(b.decode("utf-8") for b in entries.values())
        assert up.org in sweep.export_text  # carried: the export is the owner's
        assert str(connection_id) in sweep.export_text  # positive control
        for column in (
            "token_ref",
            "client_id",
            "client_auth_method",
            "client_redirect_uri",
            "client_secret_expires_at",
            "client_secret_ref",
            "registration_token_ref",
            "code_verifier",
            "jobfeed_authorization",
        ):
            assert f'"{column}"' not in sweep.export_text, column
        sweep.check("export", owner)

        # Restore into a fresh copy: no reference, no client, no pending row.
        # The sweep's agent tokens name the workspace; the removal needs them gone.
        with intake.cluster.backend.control_engine.begin() as control:
            control.execute(
                text("DELETE FROM control.access_token WHERE workspace_id = :id"),
                {"id": intake.workspace},
            )
        _remove_workspace(intake.cluster, intake.workspace)
        host = context_for_operator(make_workspace())
        assert isinstance(host, WorkspaceContext)
        _restore(intake.cluster, host, artifact)
        restored_ctx = context_for_harness(
            intake.workspace, owner_account_id, Role.OWNER
        )
        assert isinstance(restored_ctx, WorkspaceContext)
        intake.ctx = restored_ctx
        owner = owner_web(intake)
        restored = stored(owner)
        assert restored.token_ref is None and restored.client_id is None
        assert restored.client_secret_ref is None
        assert restored.registration_token_ref is None
        assert pending(owner) == []
        sweep.check("restore", owner)

        # The first reconnect registers a fresh client; a run then collects.
        registrations = len(server.registrations)
        reconnected(owner)
        assert len(server.registrations) == registrations + 1
        tick_run(owner)
        issued_now()
        assert stored(owner).organization_id == up.org
        sweep.check("restored reconnect and run", owner)

        # An expired client secret is never sent: the run stops before any call.
        before_expiry = len(server.requests), len(mcp.requests)
        set_connection(
            owner, client_secret_expires_at=clock.now() - timedelta(seconds=1)
        )
        tick_run(owner)
        assert (len(server.requests), len(mcp.requests)) == before_expiry
        assert stored(owner).authorization_state == "authorization_required"
        sweep.check("expired client secret", owner)

    # AC 80 and I6, from every request either fake received.
    refresh_tokens = {v for v in up.tokens.issued if v.startswith("refresh-marker-")}
    access_tokens = {v for v in up.tokens.issued if v.startswith("access-marker-")}
    codes = set(server.codes)
    secret_requests = 0
    for request in [*server.requests, *mcp.requests]:
        raw = request_text(request)
        url = url_of(request)
        for registration in up.registration_tokens:
            assert registration not in raw, url
        carried = [s for s in up.client_secrets if s in raw]
        if carried:
            secret_requests += 1
            assert url in {TOKEN_ENDPOINT, REVOCATION_ENDPOINT}, url
            form = parse_qs(request.content.decode("ascii"))
            if method == "client_secret_basic":
                assert basic_secret(request) in up.client_secrets
                assert "client_secret" not in form
            else:
                assert "authorization" not in request.headers
                assert form["client_secret"][0] in up.client_secrets
        if any(token in raw for token in refresh_tokens):
            assert url in {TOKEN_ENDPOINT, REVOCATION_ENDPOINT}, url
        if any(token in raw for token in access_tokens):
            assert url == RESOURCE_URL and request in mcp.requests, url
            assert request.headers["authorization"].removeprefix("Bearer ") in (
                access_tokens
            )
        if any(code in raw for code in codes):
            assert url == TOKEN_ENDPOINT, url
        if up.org in raw:
            assert request in mcp.requests, url
            message = json.loads(request.content)
            assert message["params"]["arguments"]["org_uid"] == up.org
            assert up.org not in json.dumps(
                {
                    k: v
                    for k, v in message["params"]["arguments"].items()
                    if k != "org_uid"
                }
            )
    assert secret_requests >= 3  # the exchanges, refreshes and the revocation
    assert any(up.org in request_text(r) for r in mcp.requests)


# --- failing answers carrying markers (AC 50, I1) ------------------------------------


@dataclass
class Planted:
    """Markers a failing answer carries."""

    token: str = field(default_factory=lambda: marker("refresh"))
    access: str = field(default_factory=lambda: marker("access"))
    secret: str = field(default_factory=lambda: marker("client-secret"))
    registration: str = field(default_factory=lambda: marker("registration-token"))
    org: str = field(default_factory=lambda: marker("org"))

    def secrets(self, *extra: str) -> list[str]:
        return [self.token, self.access, self.secret, self.registration, *extra]

    def body(self, *extra: str) -> str:
        return leaking_body([*self.secrets(*extra), self.org])


def leaking(kind: str, planted: Planted, *extra: str) -> Responder:
    """A failing answer of ``kind`` whose body and headers carry every marker."""
    body = planted.body(*extra)
    header = {"X-Leak": body}

    def respond(request: httpx.Request) -> httpx.Response:
        if kind == "400-json":
            return httpx.Response(
                400,
                json={"error": "invalid_request", "error_description": body},
                headers=header,
            )
        if kind == "truncated-json":
            return httpx.Response(
                200,
                content=('{"jsonrpc":"2.0","id":1,"result":{"x":"' + body).encode(),
                headers={"Content-Type": "application/json", **header},
            )
        if kind == "500-html":
            return httpx.Response(
                500,
                content=f"<html><body>{body}</body></html>".encode(),
                headers={"Content-Type": "text/html", **header},
            )
        assert kind == "bad-header"
        return httpx.Response(
            429,
            content=body.encode(),
            headers={
                "Retry-After": body,
                "Content-Type": f"application/json; charset={planted.org}",
                "WWW-Authenticate": f'Bearer error="{body}',
                "Mcp-Session-Id": body,
            },
        )

    return respond


KINDS = ["400-json", "truncated-json", "500-html", "bad-header"]


def chain_text(error: BaseException | None) -> str:
    """``str`` and ``repr`` of an exception, its ``args`` and every ``__cause__``
    and ``__context__`` beneath it."""
    parts: list[str] = []
    seen: set[int] = set()
    pending_errors = [error]
    while pending_errors:
        current = pending_errors.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        parts.extend([str(current), repr(current), repr(current.args)])
        pending_errors.extend([current.__cause__, current.__context__])
    return "\n".join(parts)


def assert_fixed(error: BaseException) -> None:
    """A fixed-code failure with nothing chained to it."""
    assert error.__cause__ is None and error.__context__ is None, repr(error)
    assert str(error) == repr(error), repr(error)


def failure_surfaces(
    i: Intake,
    owner: WorkspaceContext,
    logs: Capture,
    outputs: Iterable[str] = (),
    errors: Iterable[BaseException] = (),
) -> dict[str, str]:
    """The surfaces AC 50 names, plus the member and agent projections."""
    member = member_context(i)
    columns = database_columns(i, owner)
    return {
        "jobs": "\n".join(
            v for k, v in columns.items() if k.startswith("workspace:core.job[")
        ),
        "audit": "\n".join(
            v
            for k, v in columns.items()
            if k.startswith("workspace:core.audit_record.")
        ),
        "health": "\n".join(
            v
            for k, v in columns.items()
            if k.startswith("workspace:leads.connection_health.")
        ),
        "logs": logs.text(),
        "outputs": "\n".join(outputs),
        "errors": "\n".join(chain_text(e) for e in errors),
        "catalog.jobfeed": repr(
            [row for row in catalog_of(i, owner) if row["kind"] == "jobfeed"]
        ),
        "catalog.member": repr(catalog_of(i, member)),
    }


def assert_clean(
    i: Intake,
    owner: WorkspaceContext,
    logs: Capture,
    planted: Planted,
    *,
    secrets: Iterable[str] = (),
    outputs: Iterable[str] = (),
    errors: Iterable[BaseException] = (),
) -> None:
    surfaces = failure_surfaces(i, owner, logs, outputs, errors)
    forbidden = [*planted.secrets(*secrets), planted.org]
    assert present(forbidden, surfaces) == []
    # Anywhere in either database, the organization id only where it is carried.
    assert [
        where
        for where, _ in present([planted.org], database_columns(i, owner))
        if where not in ORGANIZATION_COLUMNS
    ] == []
    assert present(planted.secrets(*secrets), database_columns(i, owner)) == []


@pytest.fixture
def jobfeed_root(
    intake: Intake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    configure_connector(monkeypatch, tmp_path, job_hosts=JOB_HOST)
    return tmp_path


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("path", ["exchange", "refresh"])
def test_a_failing_token_answer_leaves_no_marker_on_any_surface(
    intake: Intake, jobfeed_root: Path, logs: Capture, path: str, kind: str
) -> None:
    planted = Planted()
    server = FakeAuthorizationServer(token=Tokens(rotating=False))
    clock = FakeClock()
    mcp = FakeMcpServer()
    outputs: list[str] = []
    errors: list[BaseException] = []
    with serve(server, clock=clock, mcp=mcp):
        c = connected_collector(intake, server, auth_method="client_secret_basic")
        server.token = leaking(kind, planted, c.refresh_token)
        client = stored(c.owner)
        auth = ClientAuth(
            client_id=str(client.client_id),
            auth_method="client_secret_basic",
            client_secret_ref=client.client_secret_ref,
            secret_expires_at=None,
        )
        common: dict[str, Any] = {
            "client": auth,
            "workspace_id": intake.workspace,
            "connection_id": c.connection_id,
            "resolver": FakeResolver(),
            "clock": clock.monotonic,
            "transport": httpx.MockTransport(server.handle),
        }
        if path == "refresh":
            clock.advance(2 * HOUR)
            visit(intake, clock)
            assert len(server.token_requests) == 1
            with pytest.raises(OAuthFailure) as raised:
                oauth.refresh(
                    TOKEN_ENDPOINT,
                    refresh_ref=c.token_ref,
                    resource=RESOURCE_URL,
                    **common,
                )
        else:
            params = authorize(server, run(intake, c.owner, RECONNECT))
            outcome = run(intake, c.owner, COMPLETE, **params)
            outputs.append(repr(outcome))
            assert not outcome.ok or outcome.result.state != "connected", outcome  # type: ignore[union-attr]
            assert len(server.token_requests) == 1  # the leaking answer was read
            with pytest.raises(OAuthFailure) as raised:
                oauth.exchange_code(
                    TOKEN_ENDPOINT,
                    code=f"code-marker-{uuid4()}",
                    redirect_uri=oauth.redirect_uri("leads"),
                    code_verifier="v" * 43,
                    resource=RESOURCE_URL,
                    **common,
                )
        assert_fixed(raised.value)
        errors.append(raised.value)
    assert_clean(
        intake,
        c.owner,
        logs,
        planted,
        secrets=[c.refresh_token],
        outputs=outputs,
        errors=errors,
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("call", ["initialize", "list_accounts", "find_jobs"])
def test_a_failing_mcp_answer_leaves_no_marker_on_any_surface(
    intake: Intake, jobfeed_root: Path, logs: Capture, call: str, kind: str
) -> None:
    planted = Planted()
    server = FakeAuthorizationServer(token=Tokens(rotating=False))
    clock = FakeClock()
    mcp = FakeMcpServer(accounts=[account(planted.org)])
    responder = leaking(kind, planted)
    if call == "find_jobs":
        for name in ("find_jobs:smart_search", "find_jobs:search", "find_jobs:get"):
            mcp.script[name] = responder
        mcp.feeds = {"best_match": [[feed_row(130)]]}
    else:
        mcp.script[call] = responder
    with serve(server, clock=clock, mcp=mcp):
        c = connected_collector(intake, server)
        clock.advance(2 * HOUR)
        visit(intake, clock)
        assert mcp.calls(call if call != "find_jobs" else "find_jobs:smart_search") or (
            mcp.calls("find_jobs:search")
        )
        last_error = health_of(intake, c.owner, c.connection_id).last_error
        assert last_error is not None and planted.org not in last_error
        # The client directly: the raised failure carries nothing of the answer.
        failure: BaseException | None = None
        session = McpSession(
            RESOURCE_URL,
            AccessToken(planted.access),
            READ_ONLY_POLICY,
            deadline=clock.monotonic() + 300,
            transport=httpx.MockTransport(responder),
            resolver=FakeResolver(),
            monotonic=clock.monotonic,
        )
        with session:
            try:
                session.initialize()
            except McpFailure as error:
                failure = error
        assert isinstance(failure, McpFailure), failure
        assert_fixed(failure)
    if call == "find_jobs":
        # Carried: the stored selection and choices; asserted below to be the only
        # columns that hold it.
        assert stored(c.owner).organization_id == planted.org
        # Positive control: the sweep below does find the carried columns.
        assert {
            where
            for where, _ in present([planted.org], database_columns(intake, c.owner))
        } == ORGANIZATION_COLUMNS
    assert_clean(
        intake,
        c.owner,
        logs,
        planted,
        secrets=[c.refresh_token],
        errors=[failure],
    )


@pytest.mark.parametrize("answer_kind", ["400-json", "malformed", "issued"])
@pytest.mark.parametrize("action", ["connect", "reconnect"])
def test_a_registration_answer_leaves_no_marker_on_any_surface(
    intake: Intake,
    jobfeed_root: Path,
    logs: Capture,
    action: str,
    answer_kind: str,
) -> None:
    """A failing registration's body carries every marker; a succeeding one issues
    a client secret and a registration token whose values stay in the store."""
    planted = Planted()
    server = FakeAuthorizationServer(token=Tokens())
    outputs: list[str] = []
    if answer_kind == "issued":
        registration = issued(
            client_secret=planted.secret,
            registration_access_token=planted.registration,
        )
    elif answer_kind == "malformed":

        def registration(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201, content=('{"client_id": "' + planted.body()).encode()
            )
    else:
        registration = leaking("400-json", planted)
    with serve(server, clock=FakeClock()):
        owner = owner_web(intake)
        if action == "connect":
            pipeline_id = jobsearch_pipeline(intake, owner)
            server.registration = registration
            outcome = run(
                intake, owner, CONNECT, pipeline_ref=ref("pipeline", pipeline_id)
            )
        else:
            c = connected_collector(intake, server)
            # A changed redirect URI forces a fresh registration on reconnect.
            set_connection(
                owner, client_redirect_uri="https://elsewhere.example.com/cb"
            )
            server.registration = registration
            outcome = run(intake, c.owner, RECONNECT)
        outputs.append(repr(outcome))
        assert len(server.registrations) == 1
        if answer_kind == "issued":
            assert outcome.ok, outcome
        else:
            assert not outcome.ok, outcome
            assert outcome.error is not None
            assert outcome.error.error_code == "registration_failed", outcome
        # The client directly.
        failure: BaseException | None = None
        endpoint = server.registration_endpoint
        assert endpoint is not None
        try:
            oauth.register(
                endpoint,
                oauth.redirect_uri("leads"),
                "Example client",
                workspace_id=intake.workspace,
                connection_id=uuid4(),
                resolver=FakeResolver(),
                clock=FakeClock().monotonic,
                transport=httpx.MockTransport(server.handle),
            )
        except OAuthFailure as error:
            failure = error
        if answer_kind == "issued":
            assert failure is None
        else:
            assert isinstance(failure, OAuthFailure)
            assert_fixed(failure)
    files = {
        p.read_text(encoding="utf-8")
        for p in (jobfeed_root / "secrets").rglob("*")
        if p.is_file()
    }
    if answer_kind == "issued":
        assert {planted.secret, planted.registration} <= files
    else:
        assert not {planted.secret, planted.registration} & files
    assert_clean(
        intake,
        owner,
        logs,
        planted,
        outputs=outputs,
        errors=[] if failure is None else [failure],
    )


def test_a_forced_internal_failure_carrying_markers_surfaces_only_its_code(
    intake: Intake, jobfeed_root: Path, logs: Capture
) -> None:
    """An unexpected exception inside the run, whose text carries a token, a client
    secret, a registration token and an organization-id marker, ends as the fixed
    ``internal_error`` with no chain, through the worker and when invoked directly."""
    planted = Planted()
    server = FakeAuthorizationServer(token=Tokens(rotating=False))
    clock = FakeClock()
    mcp = FakeMcpServer(accounts=[account(planted.org)])
    with serve(server, clock=clock, mcp=mcp):
        c = connected_collector(intake, server, auth_method="client_secret_basic")
        secret_text = planted.body(c.refresh_token)

        def explode(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            if (
                statement.lstrip()
                .upper()
                .startswith("INSERT INTO LEADS.JOBFEED_ACCOUNT_CHOICE")
            ):
                raise InjectedFailure(secret_text)

        event.listen(Engine, "before_cursor_execute", explode)
        try:
            with pytest.raises(JobfeedFailure) as raised:
                invoke(c, NoToken())
            clock.advance(2 * HOUR)
            visit(intake, clock)
        finally:
            event.remove(Engine, "before_cursor_execute", explode)
    failure = raised.value
    assert str(failure) == "internal_error"
    assert_fixed(failure)
    assert mcp.calls("list_accounts") >= 1
    assert [j.last_error for j in collect_jobs(c.owner)] == ["internal_error"]
    assert "InjectedFailure" in logs.text()  # positive control: the log was read
    assert_clean(
        intake,
        c.owner,
        logs,
        planted,
        secrets=[c.refresh_token],
        errors=[failure],
    )


# --- the loopback redirect mode: the pasted address (issue #355) ----------------------

PASTED = "leads.jobfeed.complete_pasted"


def pasted_address(params: dict[str, str], **extra: str) -> str:
    """The address the owner's browser lands on after consent in loopback mode."""
    query = urlencode({**params, **extra})
    return f"{oauth.loopback_redirect_uri()}?{query}"


def test_no_pasted_address_or_code_reaches_any_surface_in_loopback_mode(
    intake: Intake,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logs: Capture,
) -> None:
    """The pasted address and its code are swept as secrets through a loopback
    connect, its completion, a replay, refused pastes carrying markers, a run, a
    declined reconnect and a completed one."""
    configure_connector(monkeypatch, tmp_path, job_hosts=JOB_HOST, redirect="loopback")
    clock = FakeClock()
    up = Upstream("client_secret_basic")
    server = FakeAuthorizationServer(registration=up.register, token=up.token)
    mcp = FakeMcpServer(
        accounts=[account(up.org)], feeds={"best_match": [[feed_row(150)]]}
    )
    leaks = Leaks(organizations={up.org})
    leaks.references.update({str(tmp_path), str(tmp_path / "secrets")})
    sweep = Sweep(intake, tmp_path, logs, leaks)

    def issued_now() -> None:
        leaks.secrets.update(up.tokens.issued)
        leaks.secrets.update(up.client_secrets)
        leaks.secrets.update(up.registration_tokens)
        leaks.secrets.update(server.codes)

    def consented(outcome: OperationOutcome, **extra: str) -> str:
        params = authorize(server, outcome)
        address = pasted_address(params, **extra)
        leaks.secrets.update({address, params["code"]})
        issued_now()
        return address

    with serve(server, clock=clock, resolver=FakeResolver(), mcp=mcp):
        owner = owner_web(intake)
        pipeline_id = jobsearch_pipeline(intake, owner)
        connected = sweep.op(owner, CONNECT, pipeline_ref=ref("pipeline", pipeline_id))
        address = consented(connected, extra=marker("ignored-parameter"))
        sweep.check("loopback connect", owner)

        # Refused pastes that carry the live code and markers: nothing consumed.
        wrong = address.replace("127.0.0.1", "localhost", 1)
        long = f"{address}&pad={marker('pad') * 120}"
        for refused_address in (wrong, f"{address}#{marker('fragment')}", long):
            leaks.secrets.add(refused_address)
            outcome = sweep.op(owner, PASTED, address=refused_address)
            assert not outcome.ok and outcome.error is not None, outcome
            assert outcome.error.error_code == "address_invalid", outcome
        assert len(pending(owner)) == 1
        sweep.check("refused pastes", owner)

        completed = sweep.op(owner, PASTED, address=address)
        assert completed.ok and completed.result.state == "connected", completed  # type: ignore[union-attr]
        issued_now()
        replay = sweep.op(owner, PASTED, address=address)
        assert replay.ok and replay.result.state == "refused", replay  # type: ignore[union-attr]
        assert [
            parse_qs(r.content.decode("ascii"))["redirect_uri"]
            for r in server.token_requests
        ] == [[oauth.loopback_redirect_uri()]]
        sweep.check("loopback complete", owner)

        connection_id = stored(owner).connection_id
        clock.advance(0)
        visit(intake, clock)
        processed(
            ConnectedCollector(
                intake=intake,
                pipeline_id=pipeline_id,
                connection_id=connection_id,
                owner=owner,
                poll=owner,
            )
        )
        issued_now()
        assert stored(owner).organization_id == up.org
        sweep.check("loopback run", owner)

        # A declined reconnect whose address carries a provider description marker.
        declined = sweep.op(owner, RECONNECT)
        assert declined.ok, declined
        state, _code = server.authorize(declined.result.authorization_url)  # type: ignore[union-attr]
        issued_now()
        description = marker("error-description")
        refusal = pasted_address(
            {"state": state, "error": "access_denied"}, error_description=description
        )
        leaks.secrets.update({refusal, description})
        outcome = sweep.op(owner, PASTED, address=refusal)
        assert outcome.ok and outcome.result.reason == "provider_error", outcome  # type: ignore[union-attr]
        sweep.check("declined loopback reconnect", owner)

        again = consented(sweep.op(owner, RECONNECT))
        outcome = sweep.op(owner, PASTED, address=again)
        assert outcome.ok and outcome.result.state == "connected", outcome  # type: ignore[union-attr]
        issued_now()
        sweep.check("loopback reconnect", owner)

    # The codes went to the token endpoint and nowhere else.
    for request in [*server.requests, *mcp.requests]:
        raw = request_text(request)
        for code in server.codes:
            if code in raw:
                assert url_of(request) == TOKEN_ENDPOINT, url_of(request)

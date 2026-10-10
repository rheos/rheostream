"""Real signed HTTP requests, owner operations and worker processing."""

import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx
import pytest
from rheo_app_core import connector_routes
from rheo_app_core.main import app
from rheo_core.boundary.factories import context_for_connection
from rheo_core.connectors import receiver
from rheo_core.connectors.credentials import resolve_key
from rheo_core.connectors.tables import locator
from rheo_core.operations import dispatch
from rheo_core.refs import uuid7
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.references import identifier, ref
from rheo_leads.storage import tables as t
from sqlalchemy import select, update

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres


@pytest.fixture
def website(intake: Intake, monkeypatch: pytest.MonkeyPatch):
    from rheo_core.storage.data_root import ensure_layout, resolve_data_root

    ensure_layout(resolve_data_root().path)
    outcome = intake.call(
        "leads.connection.create_webhook",
        name="Synthetic website",
        funnel_ref=ref("funnel", intake.funnel_id),
    )
    assert outcome.ok, outcome
    connection_ref = outcome.result.connection_ref
    connection_id = identifier(connection_ref, "intake_connection")
    with open_unit_of_work(intake.ctx) as uow:
        reference = uow.connection.execute(
            select(t.intake_connection.c.signing_secret_ref).where(
                t.intake_connection.c.id == connection_id
            )
        ).scalar_one()
    key = resolve_key(intake.workspace, connection_id, reference).expose()
    real_receive = receiver.receive

    def receive(*args, **kwargs):
        kwargs.update(registry=intake.surfaces.operations, consumers=intake.consumers)
        return real_receive(*args, **kwargs)

    monkeypatch.setattr(connector_routes, "receive", receive)
    return connection_id, key


def headers(key: bytes, body: bytes, timestamp: str | None = None):
    timestamp = timestamp or str(int(datetime.now(UTC).timestamp()))
    return {
        "Content-Type": "application/json",
        "X-Rheo-Timestamp": timestamp,
        "X-Rheo-Signature": "v1="
        + hmac.new(key, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest(),
    }


async def send(
    website,
    body=(
        b'{"event_id":"one","person.email":"synthetic@example.test",'
        b'"subject":"Website inquiry"}'
    ),
    *,
    key=None,
    extra=None,
    path=None,
):
    connection_id, original = website
    values = headers(key or original, body)
    values.update(extra or {})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        return await client.post(
            path or f"/api/v1/intake/webhook/{connection_id}",
            content=body,
            headers=values,
        )


@pytest.mark.asyncio
async def test_signed_delivery_retry_conflict_and_worker(intake: Intake, website):
    response = await send(website)
    assert response.status_code == 202, response.text
    assert response.json()["outcome"] == "accepted"
    retry = await send(website)
    assert retry.status_code == 202 and retry.json()["outcome"] == "duplicate"
    conflict = await send(website, b'{"event_id":"one","subject":"Changed"}')
    assert conflict.status_code == 409
    assert intake.count(t.delivery_receipt) == 1
    intake.visit()
    assert intake.count(t.observation) == 1
    with open_unit_of_work(intake.ctx) as uow:
        receipt = uow.connection.execute(select(t.delivery_receipt)).mappings().one()
        assert receipt["state"] == "processed"
        assert receipt["signing_key_generation"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["signature", "old_timestamp", "future_timestamp", "unknown", "header_identity"],
)
async def test_authentication_and_identity_fail_closed(intake: Intake, website, kind):
    body = b'{"event_id":"one"}'
    extra, path = {}, None
    if kind == "signature":
        extra = {"X-Rheo-Signature": "v1=" + "0" * 64}
    elif "timestamp" in kind:
        offset = -600 if kind == "old_timestamp" else 600
        extra = headers(
            website[1], body, str(int(datetime.now(UTC).timestamp()) + offset)
        )
    elif kind == "unknown":
        path = f"/api/v1/intake/webhook/{uuid7()}"
    else:
        extra = {"X-Rheo-Event-Id": "unsigned-replacement"}
    response = await send(website, body, extra=extra, path=path)
    assert response.status_code == (422 if kind == "header_identity" else 401)
    assert intake.count(t.delivery_receipt) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,status",
    [
        (b"\xff", 422),
        (b"[]", 422),
        (b"not-json", 422),
        (b'{"event_id":"a","event_id":"b"}', 422),
        (b'{"number":NaN}', 422),
        (b"x" * 262145, 413),
    ],
)
async def test_invalid_and_oversized_body(intake: Intake, website, body, status):
    response = await send(website, body)
    assert response.status_code == status
    assert intake.count(t.delivery_receipt) == 0


@pytest.mark.asyncio
async def test_unauthenticated_health_is_debounced(intake: Intake, website):
    for _ in range(3):
        assert (
            await send(website, extra={"X-Rheo-Signature": "bad"})
        ).status_code == 401
    with open_unit_of_work(intake.ctx) as uow:
        health = (
            uow.connection.execute(
                select(t.connection_health).where(
                    t.connection_health.c.connection_id == website[0]
                )
            )
            .mappings()
            .one()
        )
        assert health["unresolved_failures"] == 1
        assert health["last_unauthenticated_write_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["rotate_secret", "revoke"])
async def test_queued_and_new_deliveries_fail_after_key_change(
    intake: Intake, website, action
):
    assert (await send(website)).status_code == 202
    changed = intake.call(
        f"leads.connection.{action}",
        connection_ref=ref("intake_connection", website[0]),
    )
    assert changed.ok, changed
    intake.visit()
    assert intake.count(t.observation) == 0
    assert (await send(website)).status_code == 401
    with open_unit_of_work(intake.ctx) as uow:
        health = (
            uow.connection.execute(
                select(t.connection_health).where(
                    t.connection_health.c.connection_id == website[0]
                )
            )
            .mappings()
            .one()
        )
        assert health["unresolved_failures"] == 2


@pytest.mark.asyncio
async def test_rotation_overlap_and_revoke_previous(intake: Intake, website):
    connection_ref = ref("intake_connection", website[0])
    result = intake.call(
        "leads.connection.rotate_secret",
        connection_ref=connection_ref,
        overlap_seconds=60,
    )
    assert result.ok
    assert (await send(website)).status_code == 202
    intake.visit()
    assert intake.count(t.observation) == 1
    assert intake.call(
        "leads.connection.revoke_secret", connection_ref=connection_ref
    ).ok
    assert (await send(website)).status_code == 401
    with open_unit_of_work(intake.ctx) as uow:
        reference = uow.connection.execute(
            select(t.intake_connection.c.signing_secret_ref).where(
                t.intake_connection.c.id == website[0]
            )
        ).scalar_one()
    new_key = resolve_key(intake.workspace, website[0], reference).expose()
    assert (await send(website, b'{"event_id":"new"}', key=new_key)).status_code == 202


@pytest.mark.asyncio
async def test_payload_workspace_claim_does_not_route(intake: Intake, website):
    claimed = str(uuid7())
    body = json.dumps(
        {"event_id": "bound", "workspace_id": claimed, "connection_ref": str(uuid7())}
    ).encode()
    assert (await send(website, body)).status_code == 202
    assert intake.count(t.delivery_receipt) == 1
    response = await send(
        website, path=f"/api/v1/intake/webhook/{website[0]}?workspace_id={claimed}"
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_disabled_module_and_revoked_connection_refuse(intake: Intake, website):
    from rheo_core.storage.core_tables import module_state

    with open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            update(module_state)
            .where(module_state.c.module_id == "leads")
            .values(state="disabled")
        )
        uow.commit()
    assert (await send(website)).status_code == 401
    assert intake.count(t.delivery_receipt) == 0


def test_connection_actor_has_only_manifest_grants(intake: Intake, website):
    ctx = context_for_connection(intake.workspace, website[0], "leads", "webhook")
    assert ctx.operation_set == frozenset({"leads.intake.accept_delivery"})
    assert ctx.principal.account_id is None
    outcome = dispatch(
        ctx,
        "relationships.party.create",
        {"kind": "person", "display_name": "Example"},
        registry=intake.surfaces.operations,
    )
    assert outcome.state == "operation_not_permitted"


def test_locator_and_key_are_not_returned_by_owner_operation(intake: Intake, website):
    result = intake.call(
        "leads.connection.rotate_secret",
        connection_ref=ref("intake_connection", website[0]),
    )
    assert result.ok
    encoded = result.result.model_dump_json()
    assert "secret://" not in encoded and website[1].decode() not in encoded
    with intake.cluster.backend.control_engine.connect() as conn:
        row = (
            conn.execute(select(locator).where(locator.c.connection_id == website[0]))
            .mappings()
            .one()
        )
    assert row["workspace_id"] == intake.workspace


@pytest.mark.asyncio
async def test_signed_website_creates_an_opportunity_without_runtime(
    intake: Intake, website
):
    from rheo_contracts import RecordRef
    from rheo_leads.storage import pipeline as p

    pipeline = intake.call("leads.pipeline.create", name="Synthetic website inquiries")
    assert pipeline.ok
    pipeline_id = RecordRef.parse(pipeline.result.ref).id
    configured = intake.call(
        "leads.connection.set_routing",
        connection_id=str(website[0]),
        rules=[{"action": "create_opportunity", "pipeline_id": str(pipeline_id)}],
    )
    assert configured.ok, configured
    assert (await send(website)).status_code == 202
    intake.visit()
    assert intake.count(p.opportunity) == 1
    assert intake.count(p.contact_permission) == 0


@pytest.mark.asyncio
async def test_concurrent_http_retries_have_one_receipt(intake: Intake, website):
    import asyncio

    replies = await asyncio.gather(*(send(website) for _ in range(6)))
    assert all(reply.status_code == 202 for reply in replies)
    assert sum(reply.json()["outcome"] == "accepted" for reply in replies) == 1
    assert intake.count(t.delivery_receipt) == 1
    intake.visit()
    assert intake.count(t.observation) == 1


@pytest.mark.asyncio
async def test_rotation_between_verification_and_dispatch_refuses(
    intake: Intake, website, monkeypatch
):
    original = receiver.context_for_connection

    def context(*args):
        assert intake.call(
            "leads.connection.rotate_secret",
            connection_ref=ref("intake_connection", website[0]),
        ).ok
        return original(*args)

    monkeypatch.setattr(receiver, "context_for_connection", context)
    assert (await send(website)).status_code == 401
    assert intake.count(t.delivery_receipt) == 0


@pytest.mark.asyncio
async def test_wrong_host_and_methods_never_reach_acceptance(intake: Intake, website):
    path = f"/api/v1/intake/webhook/{website[0]}"
    assert (
        await send(website, extra={"Host": "elsewhere.example.test"})
    ).status_code == 404
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (await client.get(path)).status_code == 405
    assert intake.count(t.delivery_receipt) == 0


@pytest.mark.asyncio
async def test_locator_cannot_override_workspace_authority(
    intake: Intake, website, make_workspace
):
    other = make_workspace()
    with intake.cluster.backend.control_engine.begin() as conn:
        conn.execute(
            update(locator)
            .where(locator.c.connection_id == website[0])
            .values(workspace_id=other)
        )
    assert (await send(website)).status_code == 401
    assert intake.count(t.delivery_receipt) == 0
    result = intake.call(
        "leads.connection.rotate_secret",
        connection_ref=ref("intake_connection", website[0]),
    )
    assert result.state == "connection_conflict"


@pytest.mark.asyncio
async def test_locator_from_rolled_back_creation_never_authenticates(
    intake: Intake, website
):
    from rheo_leads.connections import CreateWebhook, create

    with open_unit_of_work(intake.ctx) as uow:
        result = create(
            intake.ctx,
            uow,
            CreateWebhook(
                name="Rolled back", funnel_ref=ref("funnel", intake.funnel_id)
            ),
        )
        created_id = identifier(result.connection_ref, "intake_connection")
        # No commit: the workspace transaction rolls back, the locator is only a hint.
    with intake.cluster.backend.control_engine.connect() as conn:
        assert (
            conn.execute(
                select(locator.c.connection_id).where(
                    locator.c.connection_id == created_id
                )
            ).scalar_one()
            == created_id
        )
    assert (
        await send(website, path=f"/api/v1/intake/webhook/{created_id}")
    ).status_code == 401
    assert intake.count(t.delivery_receipt) == 0


def test_member_cannot_provision_rotate_or_revoke(intake: Intake, website):
    from harness.registry import add_member
    from rheo_contracts import Role
    from rheo_core.boundary import context_for_harness

    member = add_member(
        intake.cluster.backend,
        intake.workspace,
        Role.MEMBER,
        display_name="Synthetic member",
    )
    ctx = context_for_harness(intake.workspace, member, Role.MEMBER)
    for name, payload in (
        (
            "create_webhook",
            {"name": "Denied", "funnel_ref": ref("funnel", intake.funnel_id)},
        ),
        ("rotate_secret", {"connection_ref": ref("intake_connection", website[0])}),
        ("revoke_secret", {"connection_ref": ref("intake_connection", website[0])}),
        ("revoke", {"connection_ref": ref("intake_connection", website[0])}),
    ):
        result = dispatch(
            ctx,
            f"leads.connection.{name}",
            payload,
            registry=intake.surfaces.operations,
        )
        assert result.state == "role_not_permitted"


def test_operator_export_writes_private_key_without_stdout(
    intake: Intake, website, tmp_path, monkeypatch, capsys
):
    import argparse
    import stat

    from rheo_app_cli.commands import connector

    # Registries already loaded by the real module harness for this workspace.
    monkeypatch.setattr(connector, "load_modules", lambda: None)
    tmp_path.chmod(0o700)
    target = tmp_path / "website-key"
    args = argparse.Namespace(connection_id=website[0], output=target)
    assert connector.export_key(args) == 0
    assert target.read_bytes() == website[1]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    captured = capsys.readouterr()
    assert captured.out == ""
    assert website[1].decode() not in captured.err and "secret://" not in captured.err
    from rheo_core.secrets import SecretRefusal

    with pytest.raises(SecretRefusal):
        connector.export_key(args)
    assert target.read_bytes() == website[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["path", "subdomain"])
async def test_api_surface_configuration(intake: Intake, website, monkeypatch, mode):
    from rheo_core.routing import RoutingConfig

    raw = connector_routes.routing_config().model_dump(mode="json")
    raw["mode"] = mode
    raw["base_host"] = "rheo.example.test"
    raw["surfaces"]["api"]["path"] = "/gateway"
    config = RoutingConfig.model_validate(raw)
    monkeypatch.setattr(connector_routes, "routing_config", lambda: config)
    prefix = "/gateway" if mode == "path" else "/api"
    host = "rheo.example.test" if mode == "path" else "api.rheo.example.test"
    response = await send(
        website, path=f"{prefix}/v1/intake/webhook/{website[0]}", extra={"Host": host}
    )
    assert response.status_code == 202, response.text
    assert intake.count(t.delivery_receipt) == 1


@pytest.mark.asyncio
async def test_duplicate_headers_and_streaming_limit(intake: Intake, website):
    body = b'{"event_id":"headers"}'
    values = list(headers(website[1], body).items())
    values.append(("X-Rheo-Timestamp", values[1][1]))
    path = f"/api/v1/intake/webhook/{website[0]}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(path, content=body, headers=values)
        assert response.status_code == 401

        async def chunks():
            for _ in range(5):
                yield b"x" * 65536

        response = await client.post(
            path, content=chunks(), headers=headers(website[1], body)
        )
        assert response.status_code == 413
    assert intake.count(t.delivery_receipt) == 0


@pytest.mark.asyncio
async def test_expired_overlap_and_revoked_rotation(intake: Intake, website):
    from datetime import timedelta

    connection_ref = ref("intake_connection", website[0])
    assert intake.call(
        "leads.connection.rotate_secret",
        connection_ref=connection_ref,
        overlap_seconds=60,
    ).ok
    with open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            update(t.intake_connection)
            .where(t.intake_connection.c.id == website[0])
            .values(previous_valid_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        uow.commit()
    assert (await send(website)).status_code == 401
    assert intake.call("leads.connection.revoke", connection_ref=connection_ref).ok
    assert (
        intake.call(
            "leads.connection.rotate_secret", connection_ref=connection_ref
        ).state
        == "connection_revoked"
    )


@pytest.mark.asyncio
async def test_body_digest_fallback_and_matching_event_header(intake: Intake, website):
    body = b'{"subject":"No event ID"}'
    response = await send(
        website, body, extra={"X-Rheo-Event-Id": hashlib.sha256(body).hexdigest()}
    )
    assert response.status_code == 202
    retry = await send(website, body)
    assert retry.json()["receipt_ref"] == response.json()["receipt_ref"]
    assert retry.json()["outcome"] == "duplicate"


@pytest.mark.parametrize("actor", ["owner", "system", "other_connection"])
def test_acceptance_cannot_bypass_verified_connection(intake: Intake, website, actor):
    from rheo_core.boundary.factories import context_for_event_consumer
    from rheo_core.operations.refusals import OperationRefused
    from rheo_leads.contracts import AcceptDeliveryInput
    from rheo_leads.intake.accept import accept_delivery

    with open_unit_of_work(intake.ctx) as uow:
        if actor == "owner":
            ctx = intake.ctx
        elif actor == "system":
            ctx = context_for_event_consumer(
                intake.workspace, uow, request_id=intake.ctx.request_id
            )
        else:
            ctx = context_for_connection(intake.workspace, uuid7(), "leads", "webhook")
        with pytest.raises(OperationRefused, match="authenticated connection required"):
            accept_delivery(
                ctx,
                uow,
                AcceptDeliveryInput(
                    connection_ref=ref("intake_connection", website[0]),
                    source_event_id="unsigned-bypass",
                    body='{"subject":"Unsigned"}',
                    signing_key_generation=1,
                ),
            )
    assert intake.count(t.delivery_receipt) == 0

"""Credential provisioning inside the presenting core component, never a module.

The locator commits before the workspace transaction: rollback can leave a harmless
locator and an unreferenced private key, but never a usable connection. Locators
cannot be rebound to another workspace. Restored copies need a new connection ID.
"""

import secrets
from uuid import UUID

from rheo_contracts import Role, WorkspaceContext
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from rheo_core.connectors.tables import locator
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.secrets import SecretRef, SecretStore, SecretValue
from rheo_core.storage.data_root import resolve_data_root
from rheo_core.storage.postgres import get_backend


def prefix(workspace_id: UUID, connection_id: UUID) -> str:
    return f"secret://file/ws/{workspace_id}/connection/{connection_id}/signing/"


def store() -> SecretStore:
    return SecretStore(resolve_data_root().path)


def create_key(ctx: WorkspaceContext, connection_id: UUID, module_id: str) -> str:
    """Create one fresh value and return only its reference to the owning row."""
    if (
        ctx.role not in {Role.OWNER, Role.OPERATOR}
        or module_id not in ctx.enabled_modules
    ):
        raise OperationRefused(
            "role_not_permitted", "connection administration required"
        )
    # Validate the binding before registering any locator or producing key material.
    from rheo_core.modules import loaded_manifests

    manifest = loaded_manifests().get(module_id)
    if manifest is None or not any(
        b.transport == "webhook" and b.read_connection is not None
        for b in manifest.connector_bindings
    ):
        raise OperationRefused("not_found", "webhook binding unavailable")
    with get_backend().control_engine.begin() as conn:
        conn.execute(
            insert(locator)
            .values(
                connection_id=connection_id,
                workspace_id=ctx.workspace_id,
                module_id=module_id,
                transport="webhook",
            )
            .on_conflict_do_nothing()
        )
        row = (
            conn.execute(
                select(locator).where(locator.c.connection_id == connection_id)
            )
            .mappings()
            .one()
        )
        if (row["workspace_id"], row["module_id"], row["transport"]) != (
            ctx.workspace_id,
            module_id,
            "webhook",
        ):
            raise OperationRefused(
                "connection_conflict", "create a new connection for this workspace"
            )
    namespace = prefix(ctx.workspace_id, connection_id)
    ref = SecretRef.parse(namespace + str(uuid7()))
    secret_store = store()
    scope = secret_store.scope_for("connector-provisioning", namespace, writable=True)
    secret_store.create(ref, SecretValue(secrets.token_hex(32).encode("ascii")), scope)
    return str(ref)


def resolve_key(workspace_id: UUID, connection_id: UUID, reference: str) -> SecretValue:
    secret_store = store()
    scope = secret_store.scope_for(
        "connector-receiver", prefix(workspace_id, connection_id)
    )
    return secret_store.resolve(SecretRef.parse(reference), scope)

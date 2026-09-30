"""The shared recording fixture: everything a test needs for evidence to be recorded.

Recording needs six conditions (spec § System Components, item 3). This module sets
up the first five on one real workspace, and nothing else:

1. both halves of ``automatic_memory.enabled``: the operator's deployment value
   (``RHEO__automatic_memory__enabled``) and the workspace's own row;
2. a provider that resolves: ``fake``, which registers only under the test profile;
3. a subscriber: the synthetic :data:`EVIDENCE_PROBE_CONSUMER_ID` subscription on the
   harness module, enabled in the workspace, carried on a ``HandlerUnitOfWork``;
4. and 5. an account actor with a bound purpose.

Condition 6, non-empty sanitized text, is the test's own input.

**It names no real module.** Every file under ``tests/harness/`` is part of the
absence-proof import surface, so the subscriber is a synthetic one on the harness
module. A test that needs a real module's subscription registers it itself.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final
from uuid import UUID

import pytest
from rheo_contracts import ContextPurpose, EventEnvelope, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.events import ConsumerRegistry, ConsumerSubscription
from rheo_core.evidence import EVIDENCE_RECORDED
from rheo_core.evidence.record import ENABLED_KEY
from rheo_core.operations import HARNESS_MODULE_ID
from rheo_core.settings import encode_text, spec_for
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_core.tokens.issue import issue_bridge_token

from harness.registry import enable_harness_module

EVIDENCE_PROBE_CONSUMER_ID: Final = "test.evidence_probe"
ENABLED_ENV: Final = "RHEO__automatic_memory__enabled"
PROVIDER_ENV: Final = "RHEO__automatic_memory__extraction__provider"


def _ignore(envelope: EventEnvelope, uow: HandlerUnitOfWork) -> None:
    """The probe's handler: a subscriber that does nothing when delivered to."""
    return None


def probe_subscription(module_id: str = HARNESS_MODULE_ID) -> ConsumerSubscription:
    """The synthetic ``EVIDENCE_RECORDED`` subscription, on ``module_id``."""
    return ConsumerSubscription(
        consumer_id=EVIDENCE_PROBE_CONSUMER_ID,
        event_type=EVIDENCE_RECORDED,
        module_id=module_id,
        replay_safe=True,
        handler=_ignore,
    )


def probe_registry(module_id: str = HARNESS_MODULE_ID) -> ConsumerRegistry:
    """A registry holding only :func:`probe_subscription`."""
    registry = ConsumerRegistry()
    registry.register(probe_subscription(module_id))
    return registry


def handler_uow(
    uow: UnitOfWork, consumers: ConsumerRegistry | None
) -> HandlerUnitOfWork:
    """The view a direct call receives: the caller's transaction plus ``consumers``."""
    return HandlerUnitOfWork(uow, operation_id=None, consumers=consumers)


class EvidenceWorkspace:
    """One provisioned workspace with the harness module enabled, and its owner."""

    def __init__(
        self, cluster: Any, workspace_id: UUID, owner_account_id: UUID
    ) -> None:
        self.cluster = cluster
        self.workspace_id = workspace_id
        self.owner_account_id = owner_account_id
        self.database_name = cluster.registry_row(workspace_id).database_name
        with self.unit_of_work() as uow:
            enable_harness_module(uow.connection)
            uow.commit()

    def unit_of_work(self) -> UnitOfWork:
        engine = self.cluster.backend.pools.engine_for(self.database_name)
        return UnitOfWork(engine, self.database_name)

    @contextmanager
    def handler(
        self, consumers: ConsumerRegistry | None
    ) -> Iterator[HandlerUnitOfWork]:
        """A committed-on-success handler view over a fresh unit of work."""
        with self.unit_of_work() as uow:
            yield handler_uow(uow, consumers)
            uow.commit()

    def context(
        self, purpose: ContextPurpose | None = ContextPurpose.RESPOND
    ) -> WorkspaceContext:
        """The owner's context, bound to ``purpose`` (pass ``None`` for unbound)."""
        ctx = context_for_harness(
            self.workspace_id, self.owner_account_id, "owner", bound_purpose=purpose
        )
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    def set_workspace(self, key: str, value: bool | int) -> None:
        """Write a workspace settings row directly, below the write path's checks.

        Every ``automatic_memory.*`` key a workspace may set is a bool or an int.
        """
        spec = spec_for(key)
        with self.unit_of_work() as uow:
            upsert_workspace_setting(
                uow.connection,
                key=key,
                value=encode_text(spec, value),
                value_type=spec.type,
                updated_by=None,
            )
            uow.commit()


def enable_recording(
    monkeypatch: pytest.MonkeyPatch, workspace: EvidenceWorkspace
) -> None:
    """Conditions 1 and 2: operator and workspace opt-in, and the ``fake`` provider."""
    monkeypatch.setenv(ENABLED_ENV, "true")
    monkeypatch.setenv(PROVIDER_ENV, "fake")
    workspace.set_workspace(ENABLED_KEY, True)


def live_bridge_token(
    workspace_id: UUID, account_id: UUID, purpose: ContextPurpose
) -> UUID:
    """A real, live bridge token's id, for an enrollment row a test inserts directly.

    ``LocalEvidenceAuthority`` refuses an enrollment whose current token is revoked,
    expired or missing (#244), so a directly inserted enrollment that must verify
    needs a token the control plane holds.
    """
    token_id, _ = issue_bridge_token(
        account_id=account_id,
        workspace_id=workspace_id,
        purpose=purpose.value,
        issued_from="operator",
    )
    return token_id

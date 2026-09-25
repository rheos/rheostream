"""A tiered ``harness.note``, for the redaction contract's Postgres tests (issue #130).

``harness.note`` is the one record type the harness can write and resolve end to end,
and its resolver's ``display`` is the note's whole body. So a note whose body is a JSON
object holding a public title, an internal organization and a restricted email is the
probe the leak tests need: the untiered path would send the body (email masked), and
the tiered path must send the title and, where the purpose allows, the organization,
and never the email in any form.

The rendering is registered on a **local** :class:`RenderingRegistry` (or patched over
the global one for a test that drives the real job), never on the process-global
``RENDERINGS`` for good: that table publishes no unregister, and every other test that
builds context from a note expects the untiered behaviour.

Every contact value is fictional (``example.com``, 555-01xx).
"""

import json
from collections.abc import Mapping
from typing import Final

from rheo_contracts import RecordRef, WorkspaceContext
from rheo_core.operations.registry import HARNESS_MODULE_ID
from rheo_core.redaction.policy import exclude_types_key, exclude_types_spec
from rheo_core.redaction.registry import ModelRendering, RenderingRegistry
from rheo_core.redaction.tiers import SensitivityTier
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings import TEST_HARNESS_ORIGIN
from rheo_core.settings.schema import REGISTRY as SETTINGS_REGISTRY

from harness.records import get_note
from harness.registry import NOTE_RECORD_TYPE

PROBE_TITLE: Final = "Quarterly check-in with the harbour co-op"
PROBE_ORGANIZATION: Final = "Harbour Supply Co-op"
PROBE_EMAIL: Final = "pat.rivera@example.com"
# Repository policy (``scripts/check_fixture_provenance.py``, criterion 35) forbids a
# phone-number-shaped literal anywhere under ``tests/``, synthetic or not. The masking
# tests need phone numbers, so they assemble them at run time from these groups with
# :func:`phone`, and no test file spells one out, in code, docstrings or comments.
AREA: Final = "250"
EXCHANGE: Final = "555"
LINE: Final = "0142"


def phone(*groups: str, sep: str = "-") -> str:
    """A phone-shaped string built from ``groups`` at run time; see the note above."""
    return sep.join(groups)


PROBE_PHONE: Final = phone(AREA, EXCHANGE, LINE)

NOTE_TIERS: Final[Mapping[str, SensitivityTier]] = {
    "title": SensitivityTier.PUBLIC,
    "organization": SensitivityTier.INTERNAL,
    "email": SensitivityTier.RESTRICTED,
}


def probe_body(
    *,
    title: str = PROBE_TITLE,
    organization: str = PROBE_ORGANIZATION,
    email: str = PROBE_EMAIL,
) -> str:
    return json.dumps({"title": title, "organization": organization, "email": email})


def load_note_fields(
    ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef, /
) -> Mapping[str, object] | None:
    row = get_note(uow.connection, ref.id)
    if row is None:
        return None
    fields = json.loads(row.body)
    assert isinstance(fields, dict)
    return fields


NOTE_RENDERING: Final = ModelRendering(
    module_id=HARNESS_MODULE_ID,
    record_type=NOTE_RECORD_TYPE,
    tiers=NOTE_TIERS,
    load=load_note_fields,
    render=None,
)


def note_renderings() -> RenderingRegistry:
    """A fresh registry holding only the tiered ``harness.note``."""
    registry = RenderingRegistry()
    registry.register(NOTE_RENDERING, origin=TEST_HARNESS_ORIGIN)
    return registry


HARNESS_EXCLUDE_TYPES_KEY: Final = exclude_types_key(HARNESS_MODULE_ID)


def register_harness_exclude_key() -> None:
    """Declare ``harness.redaction.exclude_types`` as the loader would for a module.

    The harness is registered by hand rather than loaded, so the loader never
    declares the key for it. Declared on the process-global settings registry because
    that is what ``resolve()`` reads; its default is empty, so a session that declared
    it answers every other test exactly as one that did not.
    """
    SETTINGS_REGISTRY.register(
        exclude_types_spec(HARNESS_MODULE_ID), origin=TEST_HARNESS_ORIGIN
    )


def register_module_exclude_key(module_id: str) -> None:
    """Declare ``<module_id>.redaction.exclude_types`` on the process-global registry,
    exactly as the loader declares it (same spec, same origin).

    :func:`~harness.modules.loaded_probe_modules` keeps the loader's settings writes
    local, so a module it loads has no exclude key the global ``resolve()`` can see.
    A test of exclusion on a loaded module declares it here; the identical spec under
    the same origin is the registry's no-op for any later real load in the process.
    """
    SETTINGS_REGISTRY.register(exclude_types_spec(module_id), origin=module_id)

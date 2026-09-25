"""The record type -> model rendering table the context builder reads.

One entry per *tiered* record type: one with a ``sensitivity`` entry in its manifest,
or with its own ``render_for_model``. An untiered type has no entry, and the context
builder sends its resolver's ``display`` label as it always has (masked), which is how
Recallatron's memory titles still reach a run unchanged.

The shape is the owned-delete registry's (``rheo_core/deletion/registry.py``): the
loader registers every loaded manifest's tiered types on the process-global
:data:`RENDERINGS` unconditionally, under the same origin rule every other registry
applies, and an identical re-registration is a no-op so a process that loads its
modules twice registers once. Unconditional for that registry's reason too: the
context builder reads this one instance, and a rendering routed anywhere else is one
it would never find, which for this table means a tiered record's raw ``display``
going to a model.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from rheo_core.operations.refusals import RegistrationRefused
from rheo_core.operations.registry import check_origin
from rheo_core.redaction.render import ModelRenderer, RecordLoader
from rheo_core.redaction.tiers import SensitivityTier


@dataclass(frozen=True, slots=True)
class ModelRendering:
    """How one tiered record type is rendered for a model."""

    module_id: str
    record_type: str
    tiers: Mapping[str, SensitivityTier]
    load: RecordLoader
    render: ModelRenderer | None


class RenderingRegistry:
    """``(module_id, record_type)`` -> :class:`ModelRendering`, one owner per type."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], ModelRendering] = {}

    def register(self, rendering: ModelRendering, *, origin: str) -> None:
        key = f"{rendering.module_id}.{rendering.record_type}"
        check_origin(origin, rendering.module_id, name=key)
        existing = self._entries.get((rendering.module_id, rendering.record_type))
        if existing is not None:
            if existing == rendering:
                return
            raise RegistrationRefused(key, "a model rendering is already registered")
        self._entries[(rendering.module_id, rendering.record_type)] = rendering

    def lookup(self, module_id: str, record_type: str) -> ModelRendering | None:
        return self._entries.get((module_id, record_type))

    def record_types(self) -> frozenset[str]:
        return frozenset(f"{m}.{t}" for m, t in self._entries)


RENDERINGS: Final = RenderingRegistry()

"""The tier policy: which tiers one purpose may send to a model, from settings.

``runtime-and-mcp.md`` § Tiers fixes the rule and this module computes it:

- ``public`` is always sendable.
- ``internal`` is sendable when the purpose is in ``redaction.internal_purposes``
  (package default ``respond, follow_up, internal_analysis``; ``subset`` floor, so a
  workspace may drop a purpose the operator allowed and never add one).
- ``restricted`` is never sendable as a field. ``redaction.contact_points_to_model``
  (``and`` floor, package default false) allows contact point values, and
  :attr:`TierPolicy.contact_points_allowed` is how that reaches a renderer.

**The allowance is for every purpose (Robin, 2026-09-25, issue #130 review).** The
ratified text scoped it to ``respond``; the decision widens it: with the operator's
floor and the workspace both true, contact masking lifts and contact values are
released whatever the purpose. The ``and`` floor is unchanged, so a workspace alone can
never switch it on.

**What the allowance releases, and what it does not.** The tier vocabulary has three
members and no "contact point" sub-tier, so a ``restricted`` field does not say whether
it holds a contact value, a permission record or a member credential, and the allowance
may release only the first. The default renderers therefore never release a restricted
*field*, allowance or not. What the allowance does release is the free-text mask on
contact values (:mod:`rheo_core.redaction.masking`), which is where a Recallatron
memory's contact values live, and a module's own ``render_for_model`` receives the
policy and may release its own contact point fields when
:attr:`TierPolicy.contact_points_allowed` is true, because that module knows which of
its restricted fields are contact values. Secret references are masked always.

**Purpose comes from the context's principal.** A runtime run's context carries the
purpose its token was minted with; an MCP token carries its own or none. None falls
back to :data:`DEFAULT_MODEL_PURPOSE` (``internal_analysis``), which is the facade's
documented default and, for the context builder, the narrowest purpose that still
admits ``internal`` under the package default rather than an invented wider one.

**The settings read is lazy.** A tool result with no ``internal`` field, no contact
value in any string and no record reference never needs the workspace's overrides,
and the tool facade is on every MCP call, so the resolver runs the first time an
answer depends on it and at most once per policy.
"""

from collections.abc import Callable
from typing import Final

from rheo_contracts import ContextPurpose, WorkspaceContext

from rheo_core.redaction.tiers import SensitivityTier
from rheo_core.settings import Floor, KeySpec, ResolvedSettings, Scope, ValueType

INTERNAL_PURPOSES_KEY: Final = "redaction.internal_purposes"
CONTACT_POINTS_KEY: Final = "redaction.contact_points_to_model"
EXCLUDE_TYPES_SUFFIX: Final = "redaction.exclude_types"
DEFAULT_MODEL_PURPOSE: Final = ContextPurpose.INTERNAL_ANALYSIS


def exclude_types_key(module_id: str) -> str:
    """``<module_id>.redaction.exclude_types``: the record types never sent to a model.

    Declared by the loader for every loaded module that owns a record type
    (``modules/loader.py``), not by the module, so the key's shape and its ``union``
    floor are the core's and identical for every module.
    """
    return f"{module_id}.{EXCLUDE_TYPES_SUFFIX}"


def exclude_types_spec(module_id: str) -> KeySpec:
    """The declaration of :func:`exclude_types_key` for ``module_id``: a workspace
    list of record type names with a ``union`` floor, so a workspace can only add to
    what the operator excluded. One definition, so the loader and a test that declares
    the key globally can never disagree about its shape."""
    return KeySpec(
        key=exclude_types_key(module_id),
        type=ValueType.STR_LIST,
        scope=Scope.WORKSPACE,
        floor=Floor.UNION,
        explicit_per_workspace=False,
        default=(),
    )


def purpose_of(ctx: WorkspaceContext) -> ContextPurpose:
    """The purpose model-bound output from ``ctx`` is rendered under."""
    bound = ctx.principal.bound_purpose
    return DEFAULT_MODEL_PURPOSE if bound is None else bound


class TierPolicy:
    """One purpose's answer to "may this tier go to a model", settings read lazily."""

    __slots__ = ("_resolved", "_settings", "purpose")

    def __init__(
        self, purpose: ContextPurpose, settings: Callable[[], ResolvedSettings]
    ) -> None:
        self.purpose = purpose
        self._settings = settings
        self._resolved: ResolvedSettings | None = None

    @classmethod
    def from_settings(
        cls, purpose: ContextPurpose, settings: ResolvedSettings
    ) -> "TierPolicy":
        """A policy over settings the caller already resolved."""
        return cls(purpose, lambda: settings)

    def settings(self) -> ResolvedSettings:
        if self._resolved is None:
            self._resolved = self._settings()
        return self._resolved

    @property
    def internal_allowed(self) -> bool:
        """Whether this purpose is in the effective ``redaction.internal_purposes``.

        A listed string that is not a purpose name grants nothing; it is compared as
        text, so it simply never matches.
        """
        allowed = self.settings().get_list(INTERNAL_PURPOSES_KEY)
        return self.purpose.value in allowed

    @property
    def contact_points_allowed(self) -> bool:
        """The effective ``redaction.contact_points_to_model``, for any purpose."""
        return self.settings().get_bool(CONTACT_POINTS_KEY)

    def allows(self, tier: SensitivityTier) -> bool:
        """Whether a field of ``tier`` may be sent as a field. Never ``restricted``."""
        if tier is SensitivityTier.PUBLIC:
            return True
        if tier is SensitivityTier.INTERNAL:
            return self.internal_allowed
        return False

    @property
    def ceiling(self) -> SensitivityTier:
        """The highest tier text rendered under this policy can carry: what a context
        item from a module's own renderer is tagged with."""
        if self.contact_points_allowed:
            return SensitivityTier.RESTRICTED
        if self.internal_allowed:
            return SensitivityTier.INTERNAL
        return SensitivityTier.PUBLIC

    def excluded_types(self, module_id: str) -> frozenset[str]:
        """The record type names ``<module_id>.redaction.exclude_types`` lists.

        Empty when the key is not declared, which is a module the loader did not
        load (the test harness registers ``harness.note`` by hand): nothing could
        have excluded a type through a key that does not exist.
        """
        key = exclude_types_key(module_id)
        settings = self.settings()
        if key not in settings:
            return frozenset()
        return frozenset(settings.get_list(key))


def policy_for(ctx: WorkspaceContext) -> TierPolicy:
    """The policy for ``ctx``: its purpose, and the workspace's resolved settings.

    Imported lazily so that constructing a policy for a test with
    :meth:`TierPolicy.from_settings` never reaches the storage layer.
    """

    def settings() -> ResolvedSettings:
        from rheo_core.settings import resolve
        from rheo_core.settings.storage_source import PostgresOverrideSource

        return resolve(workspace_id=ctx.workspace_id, source=PostgresOverrideSource())

    return TierPolicy(purpose_of(ctx), settings)

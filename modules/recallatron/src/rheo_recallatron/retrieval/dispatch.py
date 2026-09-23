"""The one strategy registry in this module, and the configured choice over it.

``resolve_strategy`` looks the name up in :data:`STRATEGY_REGISTRY` **at call time**,
never capturing an implementation at import, so which strategy runs is decided by the
workspace's setting and the registry's current contents — and a test that swaps an
entry changes what ``recall()`` runs.
"""

from collections.abc import Mapping
from typing import Final

from rheo_contracts import WorkspaceContext
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings.schema import SettingTypeMismatch, decode_text
from rheo_core.settings.storage_source import TransactionBoundOverrideSource

from rheo_recallatron.configuration import (
    RETRIEVAL_STRATEGY_KEY,
    RETRIEVAL_STRATEGY_SPEC,
    STRATEGY_DENSE,
    STRATEGY_HYBRID,
    STRATEGY_LEXICAL,
)
from rheo_recallatron.retrieval.dense import DenseStrategy
from rheo_recallatron.retrieval.hybrid import HybridStrategy
from rheo_recallatron.retrieval.lexical import LexicalStrategy
from rheo_recallatron.retrieval.protocol import RetrievalStrategy

STRATEGY_REGISTRY: Final[dict[str, RetrievalStrategy]] = {
    STRATEGY_LEXICAL: LexicalStrategy(),
    STRATEGY_DENSE: DenseStrategy(),
    STRATEGY_HYBRID: HybridStrategy(),
}
"""Strategy name to implementation. A name joins the key's ``choices`` in the same
change that registers it here; the import-time check below refuses the other order."""

_unserved = set(RETRIEVAL_STRATEGY_SPEC.choices or ()) - set(STRATEGY_REGISTRY)
if _unserved:
    # Raised rather than asserted so ``python -O`` cannot skip it: a configurable
    # value with no implementation would be a workspace setting the read path
    # answers with a ``KeyError``.
    raise RuntimeError(
        f"{RETRIEVAL_STRATEGY_KEY} offers {sorted(_unserved)} with no registered "
        "strategy"
    )


def strategy_name_in(stored_rows: Mapping[str, str]) -> str:
    """The configured strategy name, decided from override rows already read.

    **Absent and unparseable are two different answers.** An absent row is ordinary
    settings resolution and takes the package default. A row that is present and will
    not parse — or names a value outside today's ``choices`` — resolves to ``lexical``
    regardless of the default: the narrowing direction, the one strategy that cannot
    start answering from an index this workspace never filled. The same split
    :func:`~rheo_recallatron.eligibility.expire_by_age_in` writes for its own key, and
    for the same reason: a read must degrade, never raise, on a bad stored value.
    """
    stored = stored_rows.get(RETRIEVAL_STRATEGY_KEY)
    if stored is None:
        return str(RETRIEVAL_STRATEGY_SPEC.default)
    try:
        value = decode_text(
            RETRIEVAL_STRATEGY_SPEC, stored, source="the stored workspace override"
        )
    except SettingTypeMismatch:
        return STRATEGY_LEXICAL
    return str(value)


def resolve_strategy(ctx: WorkspaceContext, uow: UnitOfWork) -> RetrievalStrategy:
    """This workspace's configured strategy.

    Read through the **transaction-bound** override source on the caller's own
    connection, as the retention keys are — never through the process-wide layered
    resolver, which would open a second connection and could disagree with what this
    transaction sees.
    """
    name = strategy_name_in(
        TransactionBoundOverrideSource(
            uow, workspace_id=ctx.workspace_id
        ).workspace_overrides(ctx.workspace_id)
    )
    return STRATEGY_REGISTRY[name]

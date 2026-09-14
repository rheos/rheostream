"""The one repository shape every module's repositories share, and its stale refusal.

Source of truth: ``docs/architecture/storage-and-workspaces.md`` § Storage adapter seam
(FR 8, D2) and its "``revision`` is a compare-and-set, not a counter" section.

**A repository is bound to one transaction at construction**, not handed one per call:
the caller opens a unit of work against the workspace database and constructs the
repository over it, so every method below runs inside that one transaction and a
repository never opens or commits anything itself. The transaction's type is
deliberately not named here — ``UnitOfWork`` lives in ``rheo_core.storage`` and
``rheo_contracts`` may import only the standard library, ``pydantic`` and itself
(``tests/test_imports.py``). It is the same boundary that keeps the handler off
:class:`~rheo_contracts.manifest.OperationDeclaration`, and the same answer: the
concrete repository names its own transaction type where it is defined, in a package
that may import storage.

**Declared only, and this run ships no implementation.** The six core tables carry no
mutable domain record type, so the first repository bound to this protocol is the first
one a module writes. The contract is stated here rather than there so that the first
module author inherits the compare-and-set rule instead of inventing it. Per-record
protocols (``OpportunityRepository`` and its like) stay in the module that owns the
record, or in this distribution when genuinely shared; this is the shape they all take,
not a base class they inherit.
"""

from collections.abc import Sequence
from typing import Protocol, TypeVar
from uuid import UUID

M = TypeVar("M")
"""The record model a repository reads and writes. Invariant, because it appears both
as an argument to ``save`` and as a return type."""


class StaleRecord(Exception):
    """A write named a revision that is no longer the stored one.

    Raised by :meth:`Repository.save` when ``expected_revision`` does not match, which
    means the record changed under the caller between the read and the write. The
    dispatcher surfaces it as the refusal ``record_stale`` — a name that has no constant
    yet, because no operation can raise this until the first mutable record type exists.
    """


class Repository(Protocol[M]):
    """Typed access to one record type, returning contract models and never rows."""

    def get(self, record_id: UUID) -> M | None: ...

    def list(self, **filter: object) -> Sequence[M]: ...

    def save(self, record: M, *, expected_revision: int | None) -> M:
        """Write ``record``, refusing unless the stored revision is still the one read.

        **A compare-and-set, not a plain write, and not a bare increment.** The
        implementation runs ``... WHERE id = $id AND revision = $expected`` with the
        increment in the same statement, and raises :class:`StaleRecord` when that
        affects zero rows. ``expected_revision`` is keyword-only and has no default, so
        a caller has to say which revision it read; ``None`` is how an immutable record
        type, which carries no ``revision`` column, says the comparison does not apply
        to it.

        **Why the rule belongs to the protocol rather than to each module's memory of
        it.** ``revision`` is not bookkeeping: it is the optimistic-concurrency token
        the approval binding rests on, and ``RecordStateGuard`` refuses an approved
        destructive, external or financial operation when a subject's current revision
        differs from the ``approval.subject_revision`` recorded when the person looked
        at it. Implemented as a plain ``UPDATE ... SET revision = revision + 1`` under
        ``READ COMMITTED``, two writers both read *n*, both write, one write is
        discarded, and the revision lands at *n+1* either way — so the guard compares
        equal against a revision that does not describe the state the approver saw. The
        failure is silent, and it is in the safety path. A module cannot opt out of the
        rule here, because a module does not write SQL outside its repository.
        """
        ...

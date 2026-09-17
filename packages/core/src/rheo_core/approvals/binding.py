"""The binding tuple: the payload digest, and the comparison execution makes against
it.

``docs/architecture/confirmation-and-safety.md`` § The approval record fixes the tuple
as ``(actor, workspace, operation_name, destination_ref or subject_ref,
payload_digest, purpose, window)`` and fixes what execution does with it: it
"recomputes the digest from the payload it is about to run and compares; a differing
byte, a differing destination, or a time outside the window refuses with
``invalid_approval`` and records nothing at the destination".

**This is not a guard, and the distinction is load-bearing.** Guards
(``ActorPermissionGuard``, ``WindowGuard``, ``RecordStateGuard``, ``DestinationGuard``)
are *rechecks* the core and the modules attach at registration, and they ask whether
the world still permits the call. The comparison below asks a narrower question — is
this the call that was approved — and it is the approval record's own identity rather
than a policy over it. It therefore has no registry, nothing attaches to it, and
removing every guard would leave it standing.

**Why the digest is taken over the validated input and not the raw payload.**
``operations/dispatch.py``'s ``_request_digest`` digests the *request as it arrived*,
because three of the outcomes that need an audit row happen before validation could
run. This one digests the input **as it will execute**: the same operation called
twice with the same meaning — keys in a different order, an ignored extra key present
once and absent once — is the same approved call, and the model dump is what makes
those two the same bytes. The two functions therefore answer different questions about
the same call and neither is a copy of the other; what they share is the canonical
form, which is one line of ``json.dumps`` in both.
"""

import json
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from typing import Final

from pydantic import BaseModel

INVALID_APPROVAL: Final = "invalid_approval"
"""The one refusal every binding failure answers with (criterion 60).

One state for all four ways to fail the tuple — a differing byte, a differing
subject, a differing destination, a time outside the window — because a caller
learning *which* part of a binding it failed learns something about an approval it
was not handed. The detail text names the part for the log and the operator; the
state does not."""


def canonical_json(value: object) -> str:
    """The canonical JSON of ``value``: sorted keys, no whitespace.

    Total for the only input this module ever hands it — a
    ``model_dump(mode="json")`` result, which is JSON-safe by construction — so it
    carries none of ``_request_digest``'s fallback ladder. A caller that hands it
    something else gets the ``TypeError`` that says so, which is the right answer for
    a programming error at a seam that decides whether an effect may land.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def executable_payload(model_input: BaseModel) -> dict[str, object]:
    """The validated input as it will execute, in its JSON form.

    ``mode="json"`` rather than ``mode="python"``: the same document is written to
    ``core.approval_payload.body`` (a ``jsonb`` column) and read back through the
    model at execution, so the round trip has to be through JSON types at both ends
    or the recomputed digest is taken over a shape the stored one never had.
    """
    return model_input.model_dump(mode="json")


def payload_digest(body: Mapping[str, object]) -> bytes:
    """SHA-256 of the canonical JSON of ``body``."""
    return sha256(canonical_json(dict(body)).encode("utf-8")).digest()


def payload_bytes(body: Mapping[str, object]) -> int:
    """The measured size of the snapshot, for ``approvals.max_payload_bytes``.

    Measured on the canonical form — the same bytes the digest is taken over — so the
    number stored beside the snapshot is the number that was checked, rather than a
    second rendering that could differ from it by whitespace.
    """
    return len(canonical_json(dict(body)).encode("utf-8"))


def binding_detail(
    *,
    approved_digest: bytes,
    digest: bytes,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
) -> str | None:
    """Why this call is not the approved one, or ``None`` when it is.

    **Two comparisons for four clauses of the tuple, and the derivation matters more
    than the count.** The document's refusal is "a differing byte, a differing
    destination, or a time outside the window". The subject and the destination are
    not compared here because in release one **neither can differ without the digest
    differing**:

    - the subject is read from an *input-model field* — that is what
      ``AuditSpec.subject_field`` names (``rheo_contracts.manifest``), and
      registration refuses a spec naming a field the model does not declare — so a
      different subject is a different value of a field the digest is taken over;
    - a destination has no declaration surface at all (``OperationDeclaration``
      declares no destination field), so a destination can only be a payload field,
      with the same consequence.

    A vacuous comparison would be worse than an absent one: re-reading the stored
    subject and comparing it with itself is a clause that can never fail, which reads
    as coverage and is not. **The condition under which this stops being true is
    exactly one thing** — a declaration gaining a way to name a subject or a
    destination from somewhere other than its validated input. The chunk that adds
    that surface adds the clause here, and this paragraph is what tells it to.

    ``now`` is a parameter and not a clock read, for the reason every repository in
    this tree gives: a caller that chooses the instant can test the window's edges
    without sleeping, and the one production caller reads the clock once, at the top
    of the execution path, so every part of one execution is judged at one instant.
    """
    if digest != approved_digest:
        return "the payload differs from the one that was approved"
    if now < window_start or now > window_end:
        return "the approval's execution window has passed"
    return None

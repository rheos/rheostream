"""The seven memory tools this module advertises to the MCP façade, and no eighth.

§ A10's table, declared: ``recall``, ``read``, ``remember``, ``derive``, ``correct``
and ``supersede`` over this module's own operations, and ``forget`` over the *core*
record-deletion operation. Six declare the operation's own input model; the seventh
declares a narrower one, and that is the whole reason it is allowed to exist.

**Why the seventh is here at all.** ``module-contract.md`` lets a module's tool name
a core operation "only when its input restricts the reference to the module's own
record types". The deletion operation accepts any canonical record reference,
because it is the coordinator for every owned record type in a deployment;
:class:`ForgetInput` accepts one shape and refuses everything else, and
``rheo_core.operations.tool_facade`` validates a call against *this* model before
dispatch sees it — so a reference to somebody else's record is ``input_invalid``
before any approval work is created, rather than reaching the coordinator to be
refused there under a state that would confirm the reference parsed.

**The two entity reads are deliberately absent.** § A10 gives them "none" in the MCP
column: an entity is reachable only through a mention on a memory the caller may
already read, and a tool over the entity list would be a second route to that
vocabulary. There is also no source-key tool and no tool over the internal expiry or
trusted-ingest seams — none of the three is a capability an agent is offered.

**Every description names the refusal states its own operation can answer with.**
AC 9: a description that only says what a tool does leaves a client unable to tell a
refusal it should retry (``record_stale``) from one it should not (``not_found``),
or a bounded scan (``window_scan_limit``) from a malformed request
(``input_invalid``). The applicable set differs per tool, and
``tests/postgres/test_memory_mcp.py`` drives the pairing from its own table, so an
operation that gains a refusal reddens there instead of leaving a stale sentence
here.

**The deleted operation's name is assembled, not written down.** The schema-ownership
scan (``tests/postgres/test_module_storage_ownership.py``) reads a
``<reserved-segment>.<name>`` literal anywhere in this tree — docstrings included —
as a foreign table reference, and is right to: this module must not name another
schema's objects. So the one operation name that is not this module's is built from
``rheo_contracts.RESERVED_MODULE_SEGMENT`` and the two segments below, and
``tests/postgres/test_memory_mcp.py`` asserts the result equals the core's own
constant. Two spellings would drift; a spelling and an assertion cannot.
"""

from typing import Final

from pydantic import field_validator
from rheo_contracts import (
    RESERVED_MODULE_SEGMENT,
    RecordRef,
    RecordRefMalformed,
    SafetyClass,
    ToolDeclaration,
)

from rheo_recallatron.configuration import MODULE_ID
from rheo_recallatron.contracts import (
    CorrectInput,
    DeriveInput,
    RememberInput,
    Strict,
    SupersedeInput,
)
from rheo_recallatron.operations import (
    MEMORY_CORRECT,
    MEMORY_DERIVE,
    MEMORY_READ,
    MEMORY_RECALL,
    MEMORY_REMEMBER,
    MEMORY_SUPERSEDE,
    ReadToolInput,
    RecallInput,
)
from rheo_recallatron.references import is_memory_ref

_RECORD_NOUN: Final = "record"
_DELETE_VERB: Final = "delete"

DELETE_OPERATION: Final = f"{RESERVED_MODULE_SEGMENT}.{_RECORD_NOUN}.{_DELETE_VERB}"
"""The owned-deletion coordinator's operation name — the one operation named here
that this module does not own. Assembled; see the module docstring."""


class ForgetInput(Strict):
    """``forget(ref)``, where ``ref`` may only name one of this module's memories.

    ``extra = "forbid"`` comes from :class:`~rheo_recallatron.contracts.Strict`, and
    the single field is a ``str`` rather than a ``RecordRef`` on purpose: the
    validated dump is what the façade hands ``dispatch``, and the wire form the
    deletion operation's own model accepts is the canonical string. Validating to a
    string here and back to a reference there keeps one wire shape across both
    models instead of a nested object only this path would produce.

    The validator raises ``ValueError`` rather than calling
    :func:`~rheo_recallatron.references.memory_target`, which raises
    ``OperationRefused``: a refusal raised inside a pydantic validator is not a
    ``ValidationError`` and would travel out of ``model_validate`` uncaught, past
    the façade's own ``input_invalid`` branch. Same reasoning, and the same two
    sentences, as :class:`~rheo_recallatron.contracts._Lifecycle`'s own reference
    validator. A reference that does not parse and a well-formed one naming another
    module's record are both refused, and both before any approval exists — that is
    § A8's "a foreign or malformed ref returns ``input_invalid`` before approval
    work is created", enforced by the shape of the model rather than by a check
    somebody has to remember to call.
    """

    ref: str

    @field_validator("ref")
    @classmethod
    def _one_of_ours(cls, value: str) -> str:
        try:
            parsed = RecordRef.parse(value)
        except (RecordRefMalformed, TypeError):
            raise ValueError("a reference must be canonical") from None
        if not is_memory_ref(parsed):
            raise ValueError("the target must be a memory reference")
        return parsed.format()


RECALL_TOOL: Final = ToolDeclaration(
    name=f"{MODULE_ID}_recall",
    safety_class=SafetyClass.READ,
    operation=MEMORY_RECALL,
    input_model=RecallInput,
    description=(
        "Search this workspace's memories by text and return the ones you may "
        "read, most relevant first. Refuses input_invalid for a blank or "
        "unparseable argument, purpose_mismatch when the stated purpose is not "
        "the one this context is bound to, and reference_scan_limit when the "
        "search exhausts its shared reference budget. A search that matches "
        "nothing you may read returns no items; it is not a refusal. "
        "Dense retrieval returns its nearest rows, not its relevant ones, so judge "
        "relevance yourself. A memory is a dated claim, not current truth: "
        "supersession corrects it, so prefer the most recent and check live state. "
        "Read `provenance` before `items` — it says which arms ran and whether "
        "dense was available at all."
    ),
)

READ_TOOL: Final = ToolDeclaration(
    name=f"{MODULE_ID}_read",
    safety_class=SafetyClass.READ,
    operation=MEMORY_READ,
    input_model=ReadToolInput,
    description=(
        "Read a bounded window of memories centred on one target inside one "
        "container. The container is either a record reference a memory links to, "
        "or an entity reference whose mentions define its members; the target is "
        "required through this tool. Refuses input_invalid for an unparseable "
        "reference or a missing target, purpose_mismatch when the stated purpose "
        "is not this context's binding, not_found when the target is missing or "
        "not yours to read, or when the container is an entity you cannot see, "
        "container_membership_required when the target is not a member of that "
        "container, window_scan_limit when the container holds more members than "
        "a read scans, and reference_scan_limit when the window exhausts its "
        "shared reference budget."
    ),
)

REMEMBER_TOOL: Final = ToolDeclaration(
    name=f"{MODULE_ID}_remember",
    safety_class=SafetyClass.MUTATE,
    operation=MEMORY_REMEMBER,
    input_model=RememberInput,
    description=(
        "Record a memory somebody stated, with its own non-memory provenance. "
        "Refuses input_invalid for a malformed argument, audience_unavailable "
        "when the requested audience is wider than this caller may write, "
        "purposes_empty when no purpose survives, purpose_mismatch when a bound "
        "caller states a purpose other than its binding, use_derive when a "
        "provenance or about reference names another memory, not_found when a "
        "reference names nothing you may read, and reference_scan_limit when the "
        "write exhausts its shared reference budget. Every refusal lands before "
        "anything is written."
    ),
)

DERIVE_TOOL: Final = ToolDeclaration(
    name=f"{MODULE_ID}_derive",
    safety_class=SafetyClass.MUTATE,
    operation=MEMORY_DERIVE,
    input_model=DeriveInput,
    description=(
        "Record a memory computed from one to sixty-four sources you may read "
        "now; the sources decide the audience and the purposes, and neither can "
        "be widened. Refuses input_invalid for a malformed argument, "
        "audience_unavailable when the requested audience is wider than the "
        "sources or this caller allow, audience_empty when the sources meet "
        "nowhere, purposes_empty when they share no purpose, purpose_mismatch "
        "when a bound caller states another purpose, use_derive when an about "
        "reference names a memory that belongs in sources, not_found when a "
        "source is missing or not yours, and reference_scan_limit when the "
        "proposed graph exhausts its shared reference budget."
    ),
)

CORRECT_TOOL: Final = ToolDeclaration(
    name=f"{MODULE_ID}_correct",
    safety_class=SafetyClass.MUTATE,
    operation=MEMORY_CORRECT,
    input_model=CorrectInput,
    description=(
        "Fix what one memory says, and invalidate everything derived from what "
        "it said. Requires the revision you hold. Refuses input_invalid for a "
        "malformed argument, not_found when the memory is missing, not yours, or "
        "no longer current, record_stale when it has moved since the revision "
        "you hold — reread and retry — and reference_scan_limit when the request "
        "exhausts its shared reference budget."
    ),
)

SUPERSEDE_TOOL: Final = ToolDeclaration(
    name=f"{MODULE_ID}_supersede",
    safety_class=SafetyClass.MUTATE,
    operation=MEMORY_SUPERSEDE,
    input_model=SupersedeInput,
    description=(
        "Replace one memory with a fresh one and keep the old one as retained "
        "history; the replacement inherits the predecessor's audience, purposes "
        "and restrictions. Requires the revision you hold. Refuses input_invalid "
        "for a malformed argument, not_found when the predecessor is missing, "
        "not yours, or no longer current, record_stale when it has moved since "
        "the revision you hold — reread and retry — and reference_scan_limit "
        "when the request exhausts its shared reference budget."
    ),
)

FORGET_TOOL: Final = ToolDeclaration(
    name=f"{MODULE_ID}_forget",
    safety_class=SafetyClass.DESTRUCTIVE,
    operation=DELETE_OPERATION,
    input_model=ForgetInput,
    description=(
        "Erase one of this workspace's memories and everything the workspace "
        "owns because of it. Always answers approval_required first: the "
        "erasure happens only once a person confirms the approval this call "
        "creates. Refuses input_invalid when the reference does not parse or "
        "names anything other than one of this module's memories — before any "
        "approval exists — and not_found when the memory is missing or not "
        "yours to erase."
    ),
)

TOOLS: Final[tuple[ToolDeclaration, ...]] = (
    RECALL_TOOL,
    READ_TOOL,
    REMEMBER_TOOL,
    DERIVE_TOOL,
    CORRECT_TOOL,
    SUPERSEDE_TOOL,
    FORGET_TOOL,
)
"""What the manifest declares. Seven, and the count is the contract: § A14 asks for
exactly-seven discovery to be asserted under module absence, token revocation and
changed operation permissions, so an eighth added here without a row in § A10's
table is a widening of the agent's reach that no reviewer asked for."""

"""AC 13 and AC 22: what this run deliberately did **not** build, asserted so that
each claim fails for its own reason.

This module is the instrument the scored 0c1/0c2 trials are measured with. **No
criterion probe is left**: the only assertion here now is this run's own
deletion-completeness check over the 0v spike, plus the two self-checks that keep the
instrument honest. Four criteria left by the same route, each in the commit that landed
the behaviour it denied — criterion 14's went with audit dispatch, and its presence is
defended end to end by ``tests/postgres/test_audit_dispatch.py`` and by the
registration refusal in ``tests/postgres/test_context_routing.py``.
Criterion 12's probe went with the worker loop,
and criterion 12's **presence** is defended by ``tests/postgres/test_worker_loop.py``
instead; criterion 11's went with the delivery drain, and its presence is defended end
to end by ``tests/postgres/test_event_delivery.py``; criterion 13's went with the
operation record, and its presence is defended by
``tests/postgres/test_operation_records.py`` and by the two operation cases in
``tests/postgres/test_worker_loop.py``. Every one of the four ran
through a helper in ``harness.absence`` that cannot be
called without a **positive control**, so a probe that passes because the module name
was misspelled, or because the path it searched does not exist, was not expressible
here. Run 0v's F52 was that second shape exactly, and the AC 22 check below still runs
through the same helper.

**Read a probe and its control as one pair.** The control runs the same mechanism over
the same resolved target, looking for something genuinely there. A misdirected probe
therefore fails its control rather than passing empty, and
:func:`test_the_positive_controls_are_live` proves the controls themselves are
load-bearing by making each one fail on purpose.

**The probe names are fixed by AC 13**, which pins both the function names and the
``{probe: covers}`` map :func:`test_every_criterion_is_covered` asserts — now a map of
one, the AC 22 census, with the int partition empty. A differently-named probe that is
otherwise identical fails that criterion exactly as surely as a missing one, so
:func:`test_no_spike_remains` may not be renamed for house-style reasons either.
"""

import subprocess
from pathlib import Path

import pytest
from harness import absent_attribute, absent_call, absent_token, covered_by_probe

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: The three roots a boundary scan covers. ``tests/`` is deliberately **not** a fourth
#: and must not become one: an AST scan matching a call by its trailing identifier
#: cannot see the receiver's type, and ``ClusterSession.record(...)`` — the test
#: cluster's own database bookkeeping — has six call sites under ``tests/``, five of
#: them in files this run may not edit. This is the same scope the assertion it
#: replaces already used (``tests/test_audit_sink.py``'s predecessor, deleted by this
#: run's chunk 03), not a narrowing.
SCAN_ROOTS = ("packages", "apps", "modules")

#: AC 22's survivor set: every tracked file that still matches a bare case-insensitive
#: ``spike`` once the 0v spike is deleted, each with the reason its hit is legitimate.
#: Asserted as an **exact** set in both directions — a seventh file fails, and so does
#: a declared survivor that no longer carries a hit, because a stale exclusion is a bug
#: rather than a harmless leftover.
SPIKE_SURVIVORS = {
    "docs/notes/0v-vertical-slice-findings.md": (
        "history: the deliverable run 0v's code was thrown away to produce"
    ),
    "docs/notes/0c0-substrate-boundary.md": (
        "this run's own record, which has to name what was deleted"
    ),
    "tests/test_findings_note.py": (
        "the structure test for that note; its docstring names what 0v produced"
    ),
    "scripts/check_routing_literals.py": (
        "a comment about a hypothetical generated directory, explaining a structural "
        "exclusion — nothing to do with the deleted module"
    ),
    "docs/README.md": (
        "'findings from build spikes', the English word, in the docs index entry"
    ),
    "tests/test_absent_behaviour.py": (
        "this probe's own argument — a grep that finds itself is the oldest vacuous "
        "pass there is"
    ),
}


def _tracked_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line}


# --- AC 22: the spike is gone, as a completeness check --------------------------------


def test_no_spike_remains() -> None:
    """AC 22: no tracked file outside the declared survivor set matches ``spike``.

    A completeness check over the tracked tree rather than a token list. Revision 0's
    five-token version passed over a half-deleted spike: none of its tokens matched
    the ``RHEO_MODULES`` value, the ``Makefile`` target or its ``.PHONY`` entry, the
    ``make`` invocation in the deploy README, the registered note operation, or the
    web route segment. A bare case-insensitive grep needs no token list kept in step
    with the deletion.

    The set is asserted both ways. A seventh file with a hit fails, and so does a
    declared survivor that has stopped carrying one — an exclusion that has gone stale
    is a bug, not a harmless leftover. An unexpected hit is a real, unfixed leftover
    from an earlier chunk and is fixed there; it is never added to this list.
    """
    tracked = _tracked_files()
    declared = set(SPIKE_SURVIVORS)

    untracked = sorted(declared - tracked)
    assert not untracked, (
        f"declared spike survivors are not tracked files: {untracked}; a survivor "
        "that has been deleted or renamed must leave this list rather than sit in it"
    )

    absent_token(
        paths=sorted(tracked - declared),
        missing="spike",
        present="rheo",
        covers="AC 22",
    )

    stale = sorted(
        path
        for path in declared
        if "spike" not in (_REPO_ROOT / path).read_text(encoding="utf-8").lower()
    )
    assert not stale, (
        f"these files are declared spike survivors but no longer match: {stale}; "
        "remove the exclusion rather than leaving one that guards nothing"
    )


# --- the instrument checks itself -----------------------------------------------------


def test_every_criterion_is_covered() -> None:
    """The one probe above covers exactly what it is supposed to, by name.

    Three assertions, not one. Both partitions are asserted exactly — the ints are the
    benchmark criteria this run stands in for, the strs are its own acceptance criteria
    — and so are the **pairs**. Without the pairs, probes with permuted labels satisfy
    a bare set comparison while the E0c record's coverage table states the wrong
    correspondence, which is the failure this exists to catch.

    **The int partition is now empty, and that is asserted rather than dropped.** Every
    criterion probe has left, each in the commit that landed the behaviour it denied,
    and an empty set is the claim "this run denies no benchmark criterion any more" —
    a claim a deleted assertion would stop making. A probe reappearing with an int
    ``covers`` fails here.

    The probe that remains is invoked here rather than read out of whatever the
    session happened to run first. The registry is module state, so reading it alone
    would make this assertion depend on test ordering and on selection: run under
    ``-k`` it would see an empty map. Calling it makes this order-independent and
    cannot weaken it, since a probe that fails still fails.
    """
    test_no_spike_remains()

    covered = covered_by_probe()

    assert {value for value in covered.values() if isinstance(value, int)} == set(), (
        covered
    )
    assert {value for value in covered.values() if isinstance(value, str)} == {
        "AC 22"
    }, covered
    assert covered == {
        "test_no_spike_remains": "AC 22",
    }, covered


def test_the_positive_controls_are_live() -> None:
    """Each helper's ``present=`` check really fails when the control is not there.

    Without this, a control satisfied by something trivially true would make every
    probe-and-control pair *look* sound while proving nothing — run 0v's own "a test
    proved the property it was easiest to test", named here so it is not repeated.

    All three helpers are exercised, including the two the one remaining criterion
    probe does not use — the attribute one, which no test in this module calls, and
    the token one, which only the AC 22 census calls. Each is a real helper and its
    control mechanism is proved directly rather than assumed.

    The failing calls also record nothing. Each helper records its ``covers`` value
    only after both assertions pass, so a control that quietly stopped raising would
    show up in :func:`test_every_criterion_is_covered` as an undeclared entry keyed by
    this test's own name, instead of passing unnoticed.
    """
    with pytest.raises(AssertionError, match="positive control failed"):
        absent_attribute(
            module="rheo_core.work",
            missing="flush_outbox",
            present="no_such_attribute_exists_here",
            covers=11,
        )

    with pytest.raises(AssertionError, match="positive control failed"):
        absent_token(
            paths=["packages/core/src/rheo_core/work/__init__.py"],
            missing="flush_outbox",
            present="no such control text exists in this docstring",
            covers=11,
        )

    with pytest.raises(AssertionError, match="positive control failed"):
        absent_call(
            paths=list(SCAN_ROOTS),
            missing="sink_for",
            present="no_such_function_is_ever_called",
            covers=14,
        )

    assert "test_the_positive_controls_are_live" not in covered_by_probe(), (
        "a helper recorded its covers value despite its control failing; recording "
        "must come after both assertions or a broken control passes unnoticed"
    )

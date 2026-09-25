"""AC 6, second half: the literal ``RHEO_MODULES`` has no tracked hit outside ``docs/``.

The module allowlist is the deployment-scope setting ``modules.installed`` now, and
the process variable that preceded it is deleted along with every line that read it.
This file is the completeness check over that deletion: a search of the whole tracked
tree rather than a token list, so there is no list to keep in step with the removal.

**Self-contained, and deliberately not in ``tests/test_absent_behaviour.py``.** Every
helper in ``tests/harness/absence.py`` records its ``covers`` value into a
process-global map keyed by the calling test's name, and that file's
``test_every_criterion_is_covered`` asserts the map is **exactly** one entry. A probe
here calling one of those helpers — from this file or from any other file in the same
pytest process — would add a second key and red that assertion. Loosening it is not
the repair: that exact-map assertion is the instrument an earlier run's scored trials
are measured with, and its probe names are pinned by that run's own acceptance
criterion. So nothing here imports ``harness.absence``; this file carries its own
walk, its own control and its own exclusion set.

**Case-sensitive, unlike ``harness.absence``.** AC 6 names "the literal", and an exact
match is both what it asks for and strictly stronger here, and it keeps this check
independent of the case-insensitive rule that harness is fixed at.
"""

import json
import subprocess
from pathlib import Path
from typing import Final

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]

#: The shared declaration of tracked-but-binary paths (also read by
#: scripts/check_legacy_names.py and tests/test_absent_behaviour.py), so there is
#: one list to keep honest rather than three drifting copies.
_TRACKED_BINARIES_PATH: Final = _REPO_ROOT / "scripts" / "tracked_binaries.json"

_THIS_FILE: Final = "tests/test_allowlist_variable_absent.py"

#: The token AC 6 requires to be gone, spelled in the one file that has to spell it.
_MISSING: Final = "RHEO_MODULES"

#: A literal the post-change tree genuinely carries — the settings key that replaced
#: the variable, declared in ``settings/schema.py`` and valued in ``defaults.toml``.
#: Searched by the same read over the same resolved file set, because a walk that
#: silently resolved to nothing would give a clean result for ``_MISSING`` and prove
#: nothing whatsoever about the tree.
_CONTROL: Final = "modules.installed"

#: ``docs/`` is out of scope by AC 6: the architecture documents and the committed
#: planning records are history, and history is allowed to name what was deleted.
_HISTORY_PREFIX: Final = "docs/"

#: Every tracked file outside ``docs/`` that legitimately carries ``_MISSING`` after
#: this change, with the reason its hit is not a leftover. Asserted **both ways**: an
#: undeclared hit fails, and so does a declared file that has stopped carrying one,
#: because an exclusion guarding nothing is a bug rather than a harmless remnant.
#:
#: An unexpected hit is a real leftover from the deletion and is fixed at its source.
#: It is never added here.
_EXPECTED_HITS: Final = {
    _THIS_FILE: (
        "this census has to spell the token it hunts for; the same self-exemption "
        "tests/test_boundary_scans.py takes for the scope privates"
    ),
}

#: Tracked files that are binary by format, each with its reason — read from the
#: shared declaration, not hand-kept here. They cannot be read as UTF-8, so the
#: census leaves them out by exact path, and checks both ways that each is still
#: tracked and still fails to decode: the list can neither outlive its file nor
#: hide a text file from the search.
_NOT_TEXT: Final = json.loads(_TRACKED_BINARIES_PATH.read_text(encoding="utf-8"))

#: A floor on the walk, not a count of the tree. The tracked non-``docs/`` set is in
#: the low hundreds; anything near zero means ``git ls-files`` answered from the wrong
#: directory, which is the failure the control above also catches.
_MINIMUM_FILES: Final = 100


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _read(path: str) -> str:
    """One tracked file as UTF-8, refusing loudly rather than skipping it.

    A file that will not decode is not quietly dropped from the search: skipping it
    would leave a hole in the census that nothing reports, which is the shape of
    vacuous pass this repository has already been bitten by.
    """
    try:
        return (_REPO_ROOT / path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise AssertionError(
            f"{path} is not UTF-8 text, so this census cannot search it; declare it "
            "with a stated reason rather than letting it fall out of the search"
        ) from exc


def test_the_allowlist_variable_is_gone_outside_docs() -> None:
    """AC 6: no tracked file outside ``docs/`` carries ``RHEO_MODULES``.

    Four assertions, and the first two are what stop the third from passing over a
    walk that found nothing: the file set is non-trivial, and a literal that really is
    in the post-change tree is found by the same read over that same set — in a file
    other than this one, so the control cannot be satisfied by its own declaration.
    """
    tracked = _tracked_files()
    not_text = set(_NOT_TEXT)
    assert not_text <= set(tracked), (
        f"declared binary files are not tracked: {sorted(not_text - set(tracked))}; "
        "remove them from _NOT_TEXT"
    )
    for path in sorted(not_text):
        try:
            (_REPO_ROOT / path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        raise AssertionError(
            f"{path} is declared binary but decodes as UTF-8 text; a text file "
            "belongs in the census, not in _NOT_TEXT"
        )

    scanned = [
        path
        for path in tracked
        if not path.startswith(_HISTORY_PREFIX) and path not in not_text
    ]
    assert len(scanned) >= _MINIMUM_FILES, (
        f"the walk resolved to only {len(scanned)} tracked files outside "
        f"{_HISTORY_PREFIX!r}; a census over almost nothing proves almost nothing"
    )

    hits: set[str] = set()
    control_carriers: set[str] = set()
    for path in scanned:
        text = _read(path)
        if _MISSING in text:
            hits.add(path)
        if _CONTROL in text:
            control_carriers.add(path)

    assert control_carriers - {_THIS_FILE}, (
        f"positive control failed: {_CONTROL!r} is in none of the {len(scanned)} "
        f"files searched but this one, so a clean result for {_MISSING!r} says "
        "nothing about whether they were really read"
    )

    declared = set(_EXPECTED_HITS)
    undeclared = sorted(hits - declared)
    assert not undeclared, (
        f"{_MISSING!r} still appears in {undeclared}; that is a leftover from the "
        "deletion and is fixed where it appears, never by declaring it here"
    )

    stale = sorted(declared - hits)
    assert not stale, (
        f"these files are declared to carry {_MISSING!r} and no longer do: {stale}; "
        "remove the exclusion rather than leaving one that guards nothing"
    )

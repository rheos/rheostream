"""The absence harness: three probe helpers, each with a **mandatory positive
control**.

This run's absence tests are the instrument the scored 0c1/0c2 trials are measured
with. If one asserts the wrong absence, every trial is measured with a broken
instrument and the error is invisible until the comparison has already been made.

**The standing failure mode this file exists to prevent.**
``with pytest.raises(ImportError): from rheo_core.work import Worker`` passes just as
well when the module name is misspelled as when the behaviour is genuinely gone. So
does a grep over a path that does not exist, which is run 0v's F52 in one line. The
mechanism against both is a positive control that runs the **same search, over the
same resolved target**, for something that is genuinely there — and it is mandatory,
not conventional: ``present=`` and ``covers=`` are keyword-only with no default, so a
probe without a control cannot be expressed.

Each helper does three things in this order:

1. **Resolves the target** — imports the module, reads the files, parses the trees. A
   typo in ``module=`` or ``paths=`` is an ``ImportError``/``FileNotFoundError``,
   never a silent pass.
2. **Asserts ``present`` is found**, by the same mechanism over the same target.
3. **Asserts ``missing`` is not found**, reporting the evidence when it is.

``covers`` is recorded only after all three succeed, keyed by the calling test's own
function name. :func:`covered_by_probe` reads that back, and
``test_every_criterion_is_covered`` asserts both partitions and the probe-to-``covers``
pairs. Recording last is deliberate: a helper call that raises — which is exactly what
``test_the_positive_controls_are_live`` makes each helper do — records nothing, so a
control that silently stopped raising shows up as an undeclared entry in the map
rather than passing quietly.

**The token match is case-insensitive, always.** AC 22's deletion census is specified
as a bare case-insensitive grep and the signature above is fixed at four parameters, so
there is no per-call knob. That is strictly stricter on the ``missing`` side, which is
the side that decides an absence; on the ``present`` side every control in this
repository is an exact-case literal, so it costs nothing there.

This docstring deliberately avoids naming the deleted module: this file is **not** a
declared survivor of that census, so a mention here would be a seventh hit. The fix for
an unexpected hit is at its source, never a wider exclusion list.
"""

import ast
import importlib
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: An int is a benchmark criterion the probe stands in for; a str is one of this run's
#: own acceptance criteria, for an absence that is deletion-completeness rather than a
#: scored behaviour.
Covers = int | str

#: ``{calling test name: every ``covers`` value that test recorded}``. A set rather
#: than a scalar so a test calling two helpers with the same value (which
#: ``test_dispatch_calls_no_audit_sink`` does) collapses to one entry, while a test
#: recording two *different* values is caught by :func:`covered_by_probe` instead of
#: one silently overwriting the other.
_COVERED: dict[str, set[Covers]] = {}

#: Directories never walked. ``node_modules`` is the one that matters:
#: ``pyproject.toml``'s mypy block documents this same trap — a bare ``apps`` glob
#: recurses into ``apps/web/node_modules`` once the web install has run. Every entry
#: here is gitignored vendor, cache or build output rather than code this repository
#: ships, each named rather than matched by a pattern, so the exclusion stays
#: auditable. Skipping them cannot produce a vacuous pass either way: a walk that
#: excluded everything would fail its own ``present=`` control first.
_NEVER_WALKED = frozenset({"node_modules", "__pycache__", ".venv", ".git", ".next"})


def covered_by_probe() -> dict[str, Covers]:
    """``{probe test name: the criterion it stands in for}``, as recorded so far.

    Raises if any one test recorded two different ``covers`` values — a probe that
    claims two criteria is a mislabelled probe, not a doubly-useful one.
    """
    resolved: dict[str, Covers] = {}
    for name, values in _COVERED.items():
        if len(values) != 1:
            raise AssertionError(
                f"{name} recorded more than one covers value: {sorted(values, key=str)}"
            )
        resolved[name] = next(iter(values))
    return resolved


def _record(covers: Covers, *, test_name: str) -> None:
    _COVERED.setdefault(test_name, set()).add(covers)


def _calling_test_name() -> str:
    """The name of the function that called the helper one frame above this one.

    Asserting it is a test function is part of the mechanism: a helper called from a
    module-level statement or a private fixture would otherwise record a key that
    ``test_every_criterion_is_covered``'s exact-map assertion could never account for,
    and the failure would read as a missing probe rather than as a misplaced call.
    """
    name = sys._getframe(2).f_code.co_name
    if not name.startswith("test_"):
        raise AssertionError(
            "an absence helper must be called directly from a test function so its "
            f"covers value can be keyed by that test's name; called from {name!r}"
        )
    return name


def _resolve_file(path: str) -> Path:
    resolved = _REPO_ROOT / path
    if not resolved.exists():
        raise FileNotFoundError(
            f"{path} does not exist under {_REPO_ROOT}; a probe pointed at a path "
            "that is not there is the vacuous pass this harness exists to prevent"
        )
    return resolved


def _read_text(path: Path) -> str:
    """Read one file as UTF-8, refusing loudly rather than skipping.

    A file that cannot be decoded is not silently dropped from the search. Skipping
    it would put a hole in the instrument that nothing reports; failing here forces a
    decision from whoever added the file.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise AssertionError(
            f"{path.relative_to(_REPO_ROOT)} is not UTF-8 text, so this scan cannot "
            "search it; exclude it explicitly with a stated reason rather than "
            "letting it fall out of the search unnoticed"
        ) from exc


def _walk_python(path: Path) -> Iterator[Path]:
    if path.is_file():
        yield path
        return
    for candidate in sorted(path.rglob("*.py")):
        if _NEVER_WALKED & set(candidate.parts):
            continue
        yield candidate


def absent_attribute(
    *, module: str, missing: str, present: str, covers: Covers
) -> None:
    """``module`` has no attribute ``missing``, and does have ``present``.

    The import resolves the target, so a misspelled module name raises rather than
    passing. ``present`` must be a real attribute of that module: a dunder every
    module carries regardless of its content proves only that *some* module was
    imported, which is decoration in the shape of a control.
    """
    test_name = _calling_test_name()
    imported = importlib.import_module(module)

    assert hasattr(imported, present), (
        f"positive control failed: {module} has no attribute {present!r}, so this "
        "probe cannot show that attribute lookup over this module finds anything"
    )
    assert not hasattr(imported, missing), (
        f"{module} has an attribute {missing!r}; this probe stands in for "
        f"{covers!r} and that behaviour is not this run's to provide"
    )
    _record(covers, test_name=test_name)


def absent_token(
    *, paths: Sequence[str], missing: str, present: str, covers: Covers
) -> None:
    """No file under ``paths`` contains ``missing``; at least one contains ``present``.

    ``paths`` are files or directories, relative to the repository root, and the match
    is a case-insensitive substring — grep-shaped, so it sees comments, docstrings and
    string literals as well as code. Use :func:`absent_call` where only a real call
    site counts.
    """
    test_name = _calling_test_name()
    if not paths:
        raise AssertionError(
            "absent_token was given no paths; a search over nothing finds nothing "
            "and would pass for the wrong reason"
        )

    needle = missing.lower()
    control = present.lower()
    hits: list[str] = []
    control_found = False
    scanned = 0

    for path in paths:
        resolved = _resolve_file(path)
        targets = [resolved] if resolved.is_file() else sorted(resolved.rglob("*"))
        for target in targets:
            if not target.is_file() or _NEVER_WALKED & set(target.parts):
                continue
            scanned += 1
            text = _read_text(target).lower()
            if control in text:
                control_found = True
            if needle in text:
                hits.append(str(target.relative_to(_REPO_ROOT)))

    assert scanned, f"absent_token resolved {list(paths)} to no readable file at all"
    assert control_found, (
        f"positive control failed: {present!r} is in none of the {scanned} files "
        f"searched, so a clean result for {missing!r} proves nothing about them"
    )
    assert not hits, (
        f"{missing!r} appears in {sorted(hits)}; this probe stands in for {covers!r} "
        "and that behaviour is not this run's to provide"
    )
    _record(covers, test_name=test_name)


def absent_call(
    *, paths: Sequence[str], missing: str, present: str, covers: Covers
) -> None:
    """Nothing under ``paths`` calls ``missing``; something there calls ``present``.

    Stricter than :func:`absent_token`: the files are parsed and only ``ast.Call``
    nodes count, matched on the call target's trailing identifier — a bare call
    (``ast.Name.id``) or a method/attribute call (``ast.Attribute.attr``). A name in a
    comment, a docstring, a string literal, an import or a ``def`` does not match,
    which is what the audit-dispatch probe needs: ``sink_for`` is still **defined**
    and **exported** in the substrate, and only a call to it is a boundary crossing.

    Matching by trailing identifier cannot see the receiver's type, so the roots a
    caller passes decide what the scan means. That is why the audit-dispatch probe
    walks ``packages/``, ``apps/`` and ``modules/`` and not ``tests/``.
    """
    test_name = _calling_test_name()
    if not paths:
        raise AssertionError(
            "absent_call was given no paths; a walk over nothing parses nothing "
            "and would pass for the wrong reason"
        )

    hits: list[str] = []
    control_sites: list[str] = []
    parsed = 0

    for path in paths:
        for target in _walk_python(_resolve_file(path)):
            parsed += 1
            tree = ast.parse(_read_text(target), filename=str(target))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name):
                    called = func.id
                elif isinstance(func, ast.Attribute):
                    called = func.attr
                else:
                    continue
                site = f"{target.relative_to(_REPO_ROOT)}:{node.lineno}"
                if called == present:
                    control_sites.append(site)
                if called == missing:
                    hits.append(site)

    assert parsed, f"absent_call resolved {list(paths)} to no .py file at all"
    assert control_sites, (
        f"positive control failed: no call to {present!r} was found in the {parsed} "
        f"files parsed, so a clean result for {missing!r} proves nothing about them"
    )
    assert not hits, (
        f"{missing!r} is still called at {sorted(hits)}; this probe stands in for "
        f"{covers!r} and that behaviour is not this run's to provide"
    )
    _record(covers, test_name=test_name)

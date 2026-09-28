"""Pure-function tests for ``rheo_recallatron.migration.private_paths`` (FR 18, AC 17).

Seams: :func:`require_private_output` (the allowlist-of-one-root guard) and
:func:`require_loopback_dsn` (the DSN-host guard), both driven over temp trees —
no postgres needed. See the module's own docstring for why the guard is an
allowlist of one root rather than an ignore check.

The public-checkout lookup is never stubbed here: it starts from the module's own
file, so these tests exercise the real lookup, and several run it from more than
one working directory to prove the answer does not depend on the cwd. The
``--triage-exempt`` I/O wrapper in ``scripts/check_migration_outputs.py`` is tested
here too, since it writes through this guard.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from rheo_recallatron.migration import private_paths
from rheo_recallatron.migration.private_paths import (
    PrivateOutputRefusal,
    require_loopback_dsn,
    require_private_output,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "check_migration_outputs.py"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _git_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "scratch@example.com"], cwd=repo)
    _git(["config", "user.name", "Scratch"], cwd=repo)
    return repo


def _ignored_root_in(tmp_path: Path, name: str) -> Path:
    """A ``private/`` directory its own (unrelated) temp repo ignores."""
    repo = _git_repo(tmp_path, name)
    (repo / ".gitignore").write_text("private/\n")
    root = repo / "private"
    root.mkdir()
    return root


def _refusal_state(path: Path) -> str | None:
    try:
        require_private_output(path)
    except PrivateOutputRefusal as refusal:
        return refusal.state
    return None


# --- the private root --------------------------------------------------------------


def test_unset_root_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(private_paths.PRIVATE_ROOT_VARIABLE, raising=False)
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output("/tmp/anything")
    assert excinfo.value.state == private_paths.PRIVATE_ROOT_UNSET


def test_a_relative_root_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, "relative/path")
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output("out.txt")
    assert excinfo.value.state == private_paths.PRIVATE_ROOT_NOT_ABSOLUTE


def test_a_missing_root_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        private_paths.PRIVATE_ROOT_VARIABLE, str(tmp_path / "does-not-exist")
    )
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output("out.txt")
    assert excinfo.value.state == private_paths.PRIVATE_ROOT_MISSING


def _cwds(tmp_path: Path) -> list[Path]:
    """Three unrelated working directories: a plain temp dir, an unrelated git
    repo, and the public checkout itself."""
    plain = tmp_path / "plain-cwd"
    plain.mkdir()
    return [plain, _git_repo(tmp_path, "unrelated-cwd-repo"), _REPO_ROOT]


def test_the_checkout_lookup_ignores_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real lookup, unstubbed, gives one answer from every cwd, and that
    answer includes the work tree this module actually lives in."""
    module_toplevel = Path(
        _git(
            ["rev-parse", "--show-toplevel"], cwd=Path(private_paths.__file__).parent
        ).stdout.strip()
    ).resolve()
    answers = set()
    for cwd in _cwds(tmp_path):
        monkeypatch.chdir(cwd)
        answers.add(private_paths._public_checkouts(private_paths._MODULE_DIR))
    assert len(answers) == 1
    (checkouts,) = answers
    assert module_toplevel in checkouts


def test_a_root_inside_this_modules_checkout_is_refused_from_any_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The module's own package directory as the root: refused as an overlap no
    matter where the process runs, including from inside an unrelated repo
    (where a cwd-based lookup would find the wrong checkout). Nothing is
    written: the refusal comes before any directory is created."""
    root = Path(private_paths.__file__).resolve().parent
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    for cwd in _cwds(tmp_path):
        monkeypatch.chdir(cwd)
        assert (
            _refusal_state(root / "out.txt")
            == private_paths.PRIVATE_ROOT_OVERLAPS_CHECKOUT
        )


def test_a_root_containing_this_modules_checkout_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkouts = private_paths._public_checkouts(private_paths._MODULE_DIR)
    root = checkouts[0].parent
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    for cwd in _cwds(tmp_path):
        monkeypatch.chdir(cwd)
        assert (
            _refusal_state(root / "out.txt")
            == private_paths.PRIVATE_ROOT_OVERLAPS_CHECKOUT
        )


def test_an_ignored_root_in_an_unrelated_repo_is_accepted_from_any_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The case a cwd-based lookup got wrong: run from inside an unrelated git
    repo (the root's own repo included, where a cwd-based lookup mistakes that
    repo for the public checkout), a correct ignored root must still be
    accepted."""
    root = _ignored_root_in(tmp_path, "private-home")
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    for cwd in [*_cwds(tmp_path), root.parent, root]:
        monkeypatch.chdir(cwd)
        assert _refusal_state(root / "sub" / "out.txt") is None


@pytest.fixture
def linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A synthetic main checkout and a linked ``git worktree`` of it, with a
    package directory inside the worktree for the lookup to start from."""
    main = _git_repo(tmp_path, "main-checkout")
    (main / ".gitignore").write_text("scratch/\n")
    (main / "README.md").write_text("synthetic\n")
    _git(["add", "-A"], cwd=main)
    _git(["commit", "-q", "-m", "seed"], cwd=main)
    worktree = tmp_path / "linked-worktree"
    _git(["worktree", "add", "-q", "-b", "scratch-branch", str(worktree)], cwd=main)
    (main / "scratch").mkdir()
    package = worktree / "pkg"
    package.mkdir()
    return main.resolve(), package.resolve()


def test_the_lookup_from_a_linked_worktree_includes_the_main_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    linked_worktree: tuple[Path, Path],
) -> None:
    main, package = linked_worktree
    answers = set()
    for cwd in _cwds(tmp_path):
        monkeypatch.chdir(cwd)
        answers.add(private_paths._public_checkouts(package))
    assert answers == {(package.parent, main)}


def test_a_root_in_the_main_checkouts_ignored_dir_is_refused_from_a_worktree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    linked_worktree: tuple[Path, Path],
) -> None:
    """The module running from a linked worktree must still refuse a root in a
    gitignored directory of the repository's main checkout — ignored, so the
    ignore rule alone would pass it."""
    main, package = linked_worktree
    monkeypatch.setattr(private_paths, "_MODULE_DIR", package)
    root = main / "scratch"
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    for cwd in _cwds(tmp_path):
        monkeypatch.chdir(cwd)
        assert (
            _refusal_state(root / "out.txt")
            == private_paths.PRIVATE_ROOT_OVERLAPS_CHECKOUT
        )


@pytest.fixture
def two_linked_worktrees(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A synthetic main checkout with two sibling linked worktrees under one
    parent, each ignoring ``scratch/``. Returns (main, worktree a, worktree b)."""
    main = _git_repo(tmp_path, "main-checkout")
    (main / ".gitignore").write_text("scratch/\n")
    _git(["add", "-A"], cwd=main)
    _git(["commit", "-q", "-m", "seed"], cwd=main)
    siblings = tmp_path / "worktrees"
    siblings.mkdir()
    trees = []
    for name in ("tree-a", "tree-b"):
        tree = siblings / name
        _git(["worktree", "add", "-q", "-b", name, str(tree)], cwd=main)
        (tree / "scratch").mkdir()
        (tree / "pkg").mkdir()
        trees.append(tree.resolve())
    return main.resolve(), trees[0], trees[1]


def test_a_root_in_a_sibling_worktrees_ignored_dir_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    two_linked_worktrees: tuple[Path, Path, Path],
) -> None:
    """Module loaded from linked worktree a; root in sibling b's gitignored
    dir. Neither a's own tree nor the main checkout overlaps it, so only the
    full worktree listing catches it."""
    _main, tree_a, tree_b = two_linked_worktrees
    monkeypatch.setattr(private_paths, "_MODULE_DIR", tree_a / "pkg")
    root = tree_b / "scratch"
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    for cwd in _cwds(tmp_path):
        monkeypatch.chdir(cwd)
        assert (
            _refusal_state(root / "out.txt")
            == private_paths.PRIVATE_ROOT_OVERLAPS_CHECKOUT
        )


def test_a_root_containing_the_linked_worktrees_is_refused_from_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    two_linked_worktrees: tuple[Path, Path, Path],
) -> None:
    """Module loaded from the main checkout; root is the directory holding the
    linked worktrees (it neither contains main nor sits inside it)."""
    main, tree_a, _tree_b = two_linked_worktrees
    package = main / "pkg"
    package.mkdir()
    monkeypatch.setattr(private_paths, "_MODULE_DIR", package)
    root = tree_a.parent
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    for cwd in _cwds(tmp_path):
        monkeypatch.chdir(cwd)
        assert (
            _refusal_state(root / "out.txt")
            == private_paths.PRIVATE_ROOT_OVERLAPS_CHECKOUT
        )


def test_the_lookup_fails_closed_outside_any_work_tree(tmp_path: Path) -> None:
    loose = tmp_path / "not-a-repo"
    loose.mkdir()
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        private_paths._public_checkouts(loose)
    assert excinfo.value.state == private_paths.PUBLIC_CHECKOUT_UNKNOWN


def test_a_root_inside_an_unignored_git_work_tree_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _git_repo(tmp_path, "some-other-repo")
    root = repo / "private"
    root.mkdir()
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output(root / "out.txt")
    assert excinfo.value.state == private_paths.PRIVATE_ROOT_NOT_IGNORED


def test_a_root_inside_an_ignored_git_work_tree_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _ignored_root_in(tmp_path, "some-other-repo")
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))

    output = root / "sub" / "out.txt"
    result = require_private_output(output)

    assert result == output.resolve()
    assert (root / "sub").is_dir()


@pytest.fixture
def ignored_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A private root that passes every check up to the output-path rules."""
    root = _ignored_root_in(tmp_path, "some-other-repo")
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    return root


def test_a_path_escaping_the_root_by_dotdot_is_refused(ignored_root: Path) -> None:
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output(ignored_root / ".." / "escaped.txt")
    assert excinfo.value.state == private_paths.OUTPUT_ESCAPES_ROOT


@pytest.mark.parametrize("tail", ["..", "sub/..", "sub/../.."])
def test_an_output_named_dotdot_is_refused(ignored_root: Path, tail: str) -> None:
    """A name of ``..`` joined onto a resolved parent is not normalized by
    ``os.path.commonpath``, so without its own rule it would pass containment
    while naming the root itself or a path above it."""
    (ignored_root / "sub").mkdir(exist_ok=True)
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output(f"{ignored_root}/{tail}")
    assert excinfo.value.state == private_paths.OUTPUT_ESCAPES_ROOT


@pytest.mark.parametrize("spelling", ["", ".", "{root}", "{root}/."])
def test_an_output_with_no_name_of_its_own_is_refused(
    ignored_root: Path, spelling: str
) -> None:
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output(spelling.format(root=ignored_root))
    assert excinfo.value.state == private_paths.OUTPUT_ESCAPES_ROOT


def test_a_path_escaping_by_a_symlinked_parent_is_refused(
    ignored_root: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = ignored_root / "link"
    link.symlink_to(elsewhere)
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output(link / "escaped.txt")
    assert excinfo.value.state == private_paths.OUTPUT_ESCAPES_ROOT


def test_a_symlink_target_itself_is_refused(ignored_root: Path) -> None:
    real_file = ignored_root / "real.txt"
    real_file.write_text("x")
    link = ignored_root / "alias.txt"
    link.symlink_to(real_file)
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_private_output(link)
    assert excinfo.value.state == private_paths.OUTPUT_IS_SYMLINK


def test_a_clean_path_inside_the_root_is_created_and_returned(
    ignored_root: Path,
) -> None:
    output = ignored_root / "reports" / "out.txt"
    result = require_private_output(output)
    assert result == output.resolve()
    assert output.parent.is_dir()
    assert not output.exists()  # the file itself is never created, only its parent


# --- the loopback DSN guard ----------------------------------------------------------

_LIBPQ_ENVIRONMENT = ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")


@pytest.fixture(autouse=True)
def _clean_libpq_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in _LIBPQ_ENVIRONMENT:
        monkeypatch.delenv(variable, raising=False)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://rheo:rheo_dev_only@localhost:5433/postgres",
        "postgresql://rheo:rheo_dev_only@127.0.0.1:5433/postgres",
        "postgresql://rheo:rheo_dev_only@[::1]:5433/postgres",
    ],
)
def test_require_loopback_dsn_accepts_every_loopback_spelling(dsn: str) -> None:
    assert require_loopback_dsn(dsn) == dsn


def test_require_loopback_dsn_refuses_a_non_loopback_host() -> None:
    dsn = "postgresql://rheo:rheo_dev_only@db.example.com:5433/postgres"
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_loopback_dsn(dsn)
    assert excinfo.value.state == private_paths.DSN_NOT_LOOPBACK


def test_require_loopback_dsn_refuses_a_host_query_param_override() -> None:
    """A loopback authority with a non-loopback ``host`` query parameter: libpq
    lets the query parameter override the authority host outright."""
    dsn = "postgresql://rheo:rheo_dev_only@localhost/postgres?host=db.example.com"
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_loopback_dsn(dsn)
    assert excinfo.value.state == private_paths.DSN_NOT_LOOPBACK


def test_require_loopback_dsn_refuses_a_hostaddr_query_param_override() -> None:
    """A loopback authority with a non-loopback ``hostaddr``: libpq uses
    ``hostaddr`` as the literal address it connects to."""
    dsn = "postgresql://rheo:rheo_dev_only@localhost/postgres?hostaddr=192.0.2.1"
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_loopback_dsn(dsn)
    assert excinfo.value.state == private_paths.DSN_NOT_LOOPBACK


def test_require_loopback_dsn_refuses_a_non_loopback_member_of_a_multi_host_list() -> (
    None
):
    dsn = "postgresql://rheo:rheo_dev_only@localhost,db.example.com:5433/postgres"
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_loopback_dsn(dsn)
    assert excinfo.value.state == private_paths.DSN_NOT_LOOPBACK


def test_require_loopback_dsn_accepts_a_multi_host_list_all_loopback() -> None:
    dsn = "postgresql://rheo:rheo_dev_only@localhost,127.0.0.1:5433/postgres"
    assert require_loopback_dsn(dsn) == dsn


def test_require_loopback_dsn_refuses_no_host_or_hostaddr() -> None:
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_loopback_dsn("postgresql:///postgres")
    assert excinfo.value.state == private_paths.DSN_NOT_LOOPBACK


def test_an_unparseable_dsn_refusal_never_quotes_the_dsn() -> None:
    """psycopg's parse error can quote DSN fragments; the refusal's own text is
    fixed, and the parse error is not chained (``from None``), so no traceback
    can print it either."""
    password = "Synthetic-Pw-555-0142"
    dsn = f"host=localhost password {password}"  # missing "=": unparseable
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_loopback_dsn(dsn)
    refusal = excinfo.value
    assert refusal.state == private_paths.DSN_NOT_LOOPBACK
    assert password not in str(refusal)
    assert password not in repr(refusal)
    assert password not in refusal.detail
    assert refusal.__cause__ is None
    assert refusal.__suppress_context__ is True


def test_require_loopback_dsn_refuses_a_service_parameter() -> None:
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_loopback_dsn("host=localhost service=synthetic-remote dbname=postgres")
    assert excinfo.value.state == private_paths.DSN_NOT_LOOPBACK


@pytest.mark.parametrize("variable", ["PGSERVICE", "PGSERVICEFILE"])
def test_require_loopback_dsn_refuses_while_a_service_variable_is_set(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv(variable, "synthetic-service")
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_loopback_dsn("postgresql://rheo@localhost:5433/postgres")
    assert excinfo.value.state == private_paths.DSN_NOT_LOOPBACK


@pytest.mark.parametrize("variable", ["PGHOST", "PGHOSTADDR"])
@pytest.mark.parametrize("value", ["db.example.com", "localhost,192.0.2.1"])
def test_require_loopback_dsn_refuses_a_non_loopback_host_variable(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        require_loopback_dsn("postgresql://rheo@localhost:5433/postgres")
    assert excinfo.value.state == private_paths.DSN_NOT_LOOPBACK


@pytest.mark.parametrize("variable", ["PGHOST", "PGHOSTADDR"])
def test_require_loopback_dsn_accepts_a_loopback_host_variable(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv(variable, "127.0.0.1")
    dsn = "postgresql://rheo@localhost:5433/postgres"
    assert require_loopback_dsn(dsn) == dsn


# --- scripts/check_migration_outputs.py: --triage-exempt ----------------------------


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_migration_outputs", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_run_triage_exempt_agrees_with_the_default_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``run_triage_exempt`` (the real I/O wrapper: tree read, guard, write,
    counts) against the real default-mode scan over one synthetic repo."""
    script = _load_script()
    fixture = _git_repo(tmp_path, "fixture-repo")
    (fixture / "public.txt").write_text("the contact is FOO   bar, publicly known\n")
    (fixture / "other.txt").write_text("line one\nsee Invented Clinic North\n")
    (fixture / "clean.txt").write_text("nothing sensitive\n")
    _git(["add", "-A"], cwd=fixture)
    _git(["commit", "-q", "-m", "seed"], cwd=fixture)
    denylist = ["Foo Bar", "", "invented   clinic north", "Never Matches Anything"]

    default_scan = script.scan_default(script._denylist_needles(denylist), cwd=fixture)
    assert sorted(default_scan) == [
        "other.txt:2: denylist line 3",
        "public.txt:1: denylist line 1",
    ]
    default_indices = {int(finding.rsplit(" ", 1)[1]) for finding in default_scan}

    root = _ignored_root_in(tmp_path, "private-home")
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    out_dir = root / "triage"
    exempted, remaining = script.run_triage_exempt(
        denylist, "HEAD", out_dir, cwd=fixture
    )

    written = (out_dir / "denylist-exempt.txt").read_text().splitlines()
    assert written == ["Foo Bar", "invented   clinic north"]
    assert {denylist.index(line) + 1 for line in written} == default_indices
    assert (exempted, remaining) == (2, 1)


def test_run_triage_exempt_refuses_an_out_dir_outside_the_private_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _load_script()
    fixture = _git_repo(tmp_path, "fixture-repo")
    (fixture / "a.txt").write_text("x\n")
    _git(["add", "-A"], cwd=fixture)
    _git(["commit", "-q", "-m", "seed"], cwd=fixture)
    root = _ignored_root_in(tmp_path, "private-home")
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    outside = tmp_path / "outside"
    with pytest.raises(PrivateOutputRefusal) as excinfo:
        script.run_triage_exempt(["x"], "HEAD", outside, cwd=fixture)
    assert excinfo.value.state == private_paths.OUTPUT_ESCAPES_ROOT
    assert not outside.exists()


@pytest.mark.parametrize("existing", [False, True])
def test_triage_exempt_output_is_owner_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, existing: bool
) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "_tracked_text_at_ref", lambda *a, **kw: [("a", "x")])
    root = _ignored_root_in(tmp_path, "private-home")
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    target = root / "denylist-exempt.txt"
    if existing:
        target.write_text("old content longer than x\n")
        target.chmod(0o644)
    assert script.run_triage_exempt(["x"], "HEAD", root) == (1, 0)
    assert target.read_text() == "x\n"
    assert target.stat().st_mode & 0o777 == 0o600


def test_triage_exempt_refuses_a_symlink_inserted_after_the_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "_tracked_text_at_ref", lambda *a, **kw: [("a", "x")])
    root = _ignored_root_in(tmp_path, "private-home")
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    outside = tmp_path / "outside.txt"
    outside.write_text("leave unchanged")
    real_guard = private_paths.require_private_output

    def swap_after_guard(path: Path) -> Path:
        checked = real_guard(path)
        checked.symlink_to(outside)
        return checked

    monkeypatch.setattr(private_paths, "require_private_output", swap_after_guard)
    with pytest.raises(OSError):
        script.run_triage_exempt(["x"], "HEAD", root)
    assert outside.read_text() == "leave unchanged"


def test_triage_exempt_closes_the_file_if_permission_tightening_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "_tracked_text_at_ref", lambda *a, **kw: [("a", "x")])
    root = _ignored_root_in(tmp_path, "private-home")
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))
    target = root / "denylist-exempt.txt"
    target.write_text("leave unchanged")
    descriptors: list[int] = []

    def refuse(descriptor: int, mode: int) -> None:
        descriptors.append(descriptor)
        raise OSError("synthetic permission failure")

    monkeypatch.setattr(script.os, "fchmod", refuse)
    with pytest.raises(OSError, match="synthetic permission failure"):
        script.run_triage_exempt(["x"], "HEAD", root)
    assert target.read_text() == "leave unchanged"
    (descriptor,) = descriptors
    with pytest.raises(OSError):
        script.os.fstat(descriptor)


def test_triage_exempt_mode_prints_only_the_two_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole CLI path (self-test included: exit 0 needs it to pass), pointed
    at a small synthetic repo rather than the real tree."""
    script = _load_script()
    fixture = _git_repo(tmp_path, "fixture-repo")
    (fixture / "public.txt").write_text("nothing sensitive\n")
    _git(["add", "-A"], cwd=fixture)
    _git(["commit", "-q", "-m", "seed"], cwd=fixture)
    monkeypatch.setattr(script, "ROOT", fixture)
    denylist_file = tmp_path / "synthetic-denylist.txt"
    denylist_file.write_text("Nothing Sensitive\n\nsynthetic token 555-0199\n")
    monkeypatch.setenv(script.DENYLIST_VARIABLE, str(denylist_file))
    root = _ignored_root_in(tmp_path, "private-home")
    monkeypatch.setenv(private_paths.PRIVATE_ROOT_VARIABLE, str(root))

    exit_code = script.main(["--triage-exempt", "HEAD", "--out", str(root / "t")])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "exempted=1 remaining=1\n"
    assert (root / "t" / "denylist-exempt.txt").read_text() == "Nothing Sensitive\n"
    assert captured.err == ""

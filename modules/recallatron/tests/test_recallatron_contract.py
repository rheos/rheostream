"""Recallatron's ``contract_tests`` entry: is this a loadable, valid distribution?

Deliberately a static smoke check — no database, no ``conftest.py``, no fixture. The
behavioural suite over memory records belongs to the run that ships them; what this
file has to prove is the one thing the rest of the distribution cannot prove about
itself, which is that its *packaging* is right.

**Resolved through the packaging metadata, never through a direct import.** A
``from rheo_recallatron import MANIFEST`` passes identically whether or not
``[project.entry-points."rheo.modules"]`` was written correctly, and nothing else in
this distribution exercises that table — so a direct import here would leave the
entry point, the only part of the packaging change the loader actually reads, wholly
untested. These tests walk the same path ``rheo_core.modules.loader.discovered()``
walks: ``entry_points(group="rheo.modules")``, select by name, ``.load()``.

**The file name is module-qualified rather than a bare ``test_contract.py``, and that
is not cosmetic.** ``testpaths`` now collects ``modules`` as well as ``tests``, and
pytest's default import mode keys a rootless test module by its basename. The second
module distribution to ship a ``tests/test_contract.py`` would not merely fail — it
would raise an import-file-mismatch during collection and **interrupt the whole
suite**, attributed to neither module. Neither an ``__init__.py`` nor
``importmode=importlib`` avoids that; unique basenames do. Nothing in the contract
depends on this name: ``manifest.contract_tests`` names the *directory*.
"""

import ast
from importlib import resources
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path

import rheo_recallatron
from rheo_core.migrations.orchestrator import MODULE_VERSION_TABLE_PREFIX
from rheo_core.modules import ENTRY_POINT_GROUP, ModuleManifest

MODULE_ID = "recallatron"


def _entry_point() -> EntryPoint:
    """The one ``rheo.modules`` entry point this distribution publishes.

    Fails rather than skips when nothing is found: an unpublished entry point is
    exactly the defect this file exists to catch, and a skip would report it as
    an absence of evidence.
    """
    found = entry_points(group=ENTRY_POINT_GROUP).select(name=MODULE_ID)
    assert len(found) == 1, (
        f"expected exactly one {ENTRY_POINT_GROUP} entry point named {MODULE_ID!r}, "
        f"found {[(point.name, point.value) for point in found]}"
    )
    return next(iter(found))


def test_the_entry_point_loads_this_distribution_s_manifest() -> None:
    loaded = _entry_point().load()
    assert isinstance(loaded, ModuleManifest)
    # Identity, not equality: the entry point must resolve to *this* distribution's
    # one manifest object. A value pointing at some other valid manifest would
    # satisfy the isinstance check and nothing else.
    assert loaded is rheo_recallatron.MANIFEST


def test_the_manifest_s_module_id_is_the_entry_point_name() -> None:
    # `load_modules()` refuses the pair when they disagree, because the allowlist
    # gates the entry-point name while the registrations carry the module id.
    assert rheo_recallatron.MANIFEST.module_id == _entry_point().name == MODULE_ID


def test_the_declared_migrations_path_holds_an_alembic_environment() -> None:
    # The manifest's own declaration, not a literal: what is under test is that
    # `storage.migrations_path` points somewhere real and packaged.
    migrations = resources.files(rheo_recallatron.MANIFEST.storage.migrations_path)
    assert migrations.joinpath("env.py").is_file()


def test_the_declared_migrations_path_converts_to_a_filesystem_path() -> None:
    """The conversion Alembic's ``script_location`` needs, which is a stricter claim
    than "the resource exists".

    ``orchestrator.script_location()`` resolves a chain with
    ``Path(str(resources.files(...)))`` and hands the result to Alembic as a
    ``script_location`` string, because ``Config`` takes a path and not a
    ``Traversable``. That conversion is only sound for a **regular** package: drop
    ``migrations/__init__.py`` and the import system serves a namespace package, whose
    ``files()`` returns a ``MultiplexedPath`` with no ``__str__`` of its own — so
    ``str()`` yields the repr ``MultiplexedPath('...')`` and ``Path(...)`` of that is
    a relative path to nothing.

    The test above stays green through exactly that mutation, because ``Traversable``
    navigation still works on a ``MultiplexedPath``. This one is what holds the
    ``__init__.py`` in place, so the reason it exists is enforced rather than only
    written down.
    """
    location = Path(
        str(resources.files(rheo_recallatron.MANIFEST.storage.migrations_path))
    )
    assert location.is_dir(), f"not a filesystem directory: {location}"
    # The same existence check `script_location()` makes before returning.
    assert (location / "env.py").is_file()


def _configure_kwargs() -> dict[str, str]:
    """The string keyword arguments this chain's ``env.py`` passes to
    ``context.configure``, read statically.

    Parsed rather than imported: ``env.py`` raises a ``RuntimeError`` at import unless
    the orchestrator has already put a live connection on the Alembic config, which is
    the no-Alembic-by-hand rule working exactly as intended. The AST is what lets this
    file stay a static check with no database and no fixture.
    """
    source = (
        Path(str(resources.files(rheo_recallatron.MANIFEST.storage.migrations_path)))
        / "env.py"
    ).read_text()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "configure"
        ):
            return {
                keyword.arg: keyword.value.value
                for keyword in node.keywords
                if keyword.arg is not None
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            }
    raise AssertionError("env.py makes no context.configure(...) call")


def test_the_chain_s_env_names_the_version_table_the_orchestrator_reads() -> None:
    """The one pairing nothing at runtime enforces.

    ``orchestrator.version_table_for`` *derives*
    ``<module_id>.alembic_version_<module_id>`` to read this chain's recorded head;
    ``env.py`` *writes* those two names as literals. Neither side consults the other,
    so a disagreement is silent rather than loud: the migration applies, the
    orchestrator reads a table that never fills, ``run_module_chain`` computes no newly
    applied revision and writes no ``core.module_schema_version`` row, and the
    ``schema_ahead`` guard is permanently vacuous. Left on Alembic's own default
    (``alembic_version``, no schema) every other test in this file still passes.

    Asserted against the manifest's own ``schema_name`` and the orchestrator's own
    prefix constant rather than against two more literals, so copying this file into
    the next module distribution carries the check with it.
    """
    configured = _configure_kwargs()
    schema_name = rheo_recallatron.MANIFEST.storage.schema_name
    assert configured.get("version_table_schema") == schema_name, configured
    assert (
        configured.get("version_table") == f"{MODULE_VERSION_TABLE_PREFIX}{schema_name}"
    ), configured

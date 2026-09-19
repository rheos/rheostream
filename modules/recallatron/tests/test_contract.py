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
"""

from importlib import resources
from importlib.metadata import EntryPoint, entry_points

import rheo_recallatron
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

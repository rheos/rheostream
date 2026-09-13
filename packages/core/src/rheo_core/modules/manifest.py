"""``ModuleManifest``: the minimum a distribution tells the loader about itself.

**Deliberately minimal, and deliberately not in ``rheo_contracts``.**
``module-contract.md`` names the type ``rheo_contracts.ModuleManifest``, but the
manifest carries its operations' handlers, typed against ``UnitOfWork``, and
``tests/test_imports.py`` refuses any ``rheo_contracts`` import outside the standard
library, ``pydantic`` and itself — the same collision that already forced
``OperationDeclaration.handler`` out of that model. So the manifest lives here and
the handler travels beside the declaration, exactly as the registry's own
``register(decl, handler, *, origin)`` already does. Run 0v's findings note carries
that as **F3**.

**No ``production_eligible`` field.** What a deployment loads is decided by the
loader's allowlist, not by a boolean a module sets about itself: ``Dockerfile`` COPYs
``modules/`` and runs ``uv sync --frozen``, so every distribution in the checkout is
installed into the image and discoverable at run time whatever profile it runs under.
The gate has to be configuration on the deployment's side. Run 0v's **F5** records the
ratified key this stands in for (``modules.installed``, whose stated default of "every
discovered module" defeats the sentence after it) and **F17** records that the image's
*installed* set and the deployment's *loaded* set are different things.

**Phase 2 replaces this whole model**, together with install/enable/disable lifecycle
(**F15**: nothing in release one can install a module; the ``core.module_state`` row a
test needs is written by hand). This type exists so a real distribution can be
discovered, validated and registered through the shipped registries today — not as a
proposal for what the ratified manifest should eventually carry.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from rheo_contracts import OperationDeclaration
from sqlalchemy import Connection

from rheo_core.audit.sink import AuditSink
from rheo_core.operations.registry import Handler
from rheo_core.refs.resolver import RecordResolver

_SEGMENT_MESSAGE: Final = "a module id is a lowercase identifier"


class ManifestInvalid(Exception):
    """A manifest the loader will not accept.

    Raised, never returned, mirroring ``RegistrationRefused``: a bad manifest is a
    packaging mistake found at load time, not a caller's error.
    """

    def __init__(self, module_id: str, detail: str) -> None:
        super().__init__(f"{module_id}: {detail}")
        self.module_id = module_id
        self.detail = detail


@dataclass(frozen=True, slots=True)
class WebSurface:
    """A module's own routing surface: the name it is keyed by, its host label, and
    its path prefix.

    Not ``rheo_core.routing.SurfaceConfig``, and that is the point: this package must
    not depend on ``rheo_core.routing``. The conversion happens at the point of use,
    in the internal listener's ``routing_config()``, which is the only consumer.
    """

    surface: str
    host: str
    path: str


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    """What one distribution exposes through its ``rheo.modules`` entry point."""

    module_id: str
    package_version: str
    schema_name: str
    create_schema: Callable[[Connection], None]
    operations: tuple[tuple[OperationDeclaration, Handler], ...] = ()
    resolvers: tuple[tuple[str, RecordResolver], ...] = ()
    web: WebSurface | None = None
    audit_sink: AuditSink | None = None


def validate(manifest: object) -> ModuleManifest:
    """Refuse anything that is not a well-formed manifest; return it otherwise.

    The checks are only the ones the loader itself depends on. Everything a
    *registration* refuses — the operation-name grammar, the origin/prefix agreement,
    reserved input fields, ``extra = "allow"`` — is left to
    ``OperationRegistry.register`` and ``ResolverRegistry.register``, which the loader
    calls with ``origin = manifest.module_id``. Re-checking any of it here would be a
    second, weaker copy of a shipped gate.
    """
    if not isinstance(manifest, ModuleManifest):
        raise ManifestInvalid(
            str(getattr(manifest, "module_id", manifest)),
            "a rheo.modules entry point must load to a ModuleManifest",
        )
    module_id = manifest.module_id
    if not isinstance(module_id, str) or not module_id.isidentifier():
        raise ManifestInvalid(str(module_id), _SEGMENT_MESSAGE)
    if module_id != module_id.lower():
        raise ManifestInvalid(module_id, _SEGMENT_MESSAGE)
    if manifest.schema_name != module_id:
        # One module owns one schema, named for it: the resolver, the audit sink and
        # the migration all address the same place without being told twice.
        raise ManifestInvalid(
            module_id, f"schema_name {manifest.schema_name!r} is not the module id"
        )
    if not callable(manifest.create_schema):
        raise ManifestInvalid(module_id, "create_schema must be callable")
    if manifest.web is not None and not isinstance(manifest.web, WebSurface):
        raise ManifestInvalid(module_id, "web must be a WebSurface or None")
    return manifest

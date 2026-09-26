"""The registered embedding providers, and the one reader of which one is configured.

**Two failures, kept apart.** A provider whose ``dimensions`` is not the column's width
is a packaging error, and :func:`register_provider` raises for it. A provider whose
optional extra is not installed is a deployment that chose not to run it, and
:func:`register_builtin_providers` registers nothing for it and raises for nothing.
Collapsing the two would make a broken package look like an ordinary lexical install.

**Resolution never raises for a missing provider.** Absent, unregistered, ``none``,
registered only under the test profile, named with its extra missing, or configured
through a key this process never declared: every one of those resolves ``None``, the
degraded path. Every memory write consults this, so a raise here would be a write that
fails because dense retrieval is not set up.

**Looked up at call time, through this module's globals.** Both the configured name and
the registry are read when :func:`resolve_provider` runs, never captured at import, so
a test that rebinds :func:`configured_provider_name` or edits :data:`PROVIDERS` changes
what every reader in the process gets — the enqueue helper, the embed job, the rebuild
and the dense strategy alike.

**The built-ins register on first use, never at import.** Registering reads the
deployment profile, and that read is strict: it refuses every ``RHEO__`` variable no
schema declares. The loader's early hook imports this module (through the entry point)
before it declares the module's own keys, so an import-time read refused the module's
legitimate variables, such as ``RHEO__recallatron__embedding__provider``, on every
cold start. The first :func:`providers`, :func:`register_provider` or
:func:`resolve_provider` call registers them, once, by then after the keys exist.
"""

import importlib.util
import threading
from typing import Final

from rheo_core.modules.operations import TEST_PROFILE
from rheo_core.settings import current_profile, resolve
from rheo_core.settings.schema import REGISTRY

from rheo_recallatron.configuration import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_PROVIDER_KEY,
    EMBEDDING_PROVIDER_NONE,
)
from rheo_recallatron.embedding.fake import FakeEmbeddingProvider
from rheo_recallatron.embedding.local import LocalEmbeddingProvider
from rheo_recallatron.embedding.protocol import EmbeddingProvider

LOCAL_PROVIDER: Final = "local"
FAKE_PROVIDER: Final = "fake"
LOCAL_EMBEDDINGS_EXTRA: Final = "fastembed"
"""The import the ``local-embeddings`` extra makes available."""

PROVIDERS: Final[dict[str, EmbeddingProvider]] = {}
"""Registry name to provider. Empty until first use, when
:func:`register_builtin_providers` fills it once; read it through :func:`providers`."""

_builtins_lock: Final = threading.Lock()
_builtins_registered = False


def _ensure_builtin_providers() -> None:
    """Register the built-ins the first time anything uses the registry, then never.

    Marked done only after registration returns, so a strict profile read that raises
    raises again on the next use instead of leaving a silently empty registry.
    """
    global _builtins_registered
    if _builtins_registered:
        return
    with _builtins_lock:
        if _builtins_registered:
            return
        register_builtin_providers()
        _builtins_registered = True


def providers() -> dict[str, EmbeddingProvider]:
    """The registry, with the built-ins registered."""
    _ensure_builtin_providers()
    return PROVIDERS


def _add(name: str, provider: EmbeddingProvider) -> None:
    if provider.dimensions != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"embedding provider {name!r} declares {provider.dimensions} dimensions; "
            f"the stored column holds {EMBEDDING_DIMENSIONS}"
        )
    PROVIDERS[name] = provider


def register_provider(name: str, provider: EmbeddingProvider) -> None:
    """Make ``provider`` resolvable as ``name``.

    Raises ``ValueError`` when its width is not :data:`EMBEDDING_DIMENSIONS`: the column
    is ``vector(384)``, so such a provider could only ever fail at insert, and that is a
    packaging error worth stopping on rather than an operator's to discover.

    The built-ins register first, so a provider registered under a built-in's name
    replaces it, as it did when the built-ins registered at import.
    """
    _ensure_builtin_providers()
    _add(name, provider)


def register_builtin_providers() -> None:
    """The two built-ins, each under its own condition.

    ``local`` only when the extra is importable. ``find_spec`` locates the package
    without importing it, so registering costs nothing and the ONNX runtime still loads
    only on first use. ``fake`` only under the test profile, the gate the core's own
    install step uses for a test-only behaviour, so a production ``provider = "fake"``
    degrades instead of writing meaningless vectors.
    """
    if importlib.util.find_spec(LOCAL_EMBEDDINGS_EXTRA) is not None:
        _add(LOCAL_PROVIDER, LocalEmbeddingProvider())
    if current_profile() == TEST_PROFILE:
        _add(FAKE_PROVIDER, FakeEmbeddingProvider())


def configured_provider_name() -> str:
    """The deployment's configured provider name; the key's only reader.

    **A key the process never declared reads as ``none``, without resolving.** Under
    the test harness a module's settings keys go into a registry local to the fixture,
    never the process-wide one, and resolving an undeclared key raises. Production
    declares the key globally when the module loads, and there it is read the way the
    core reads its own deployment keys, with the resolver's errors allowed out: a stray
    ``RHEO__`` variable is a startup misconfiguration and not "no provider".
    """
    if REGISTRY.lookup(EMBEDDING_PROVIDER_KEY) is None:
        return EMBEDDING_PROVIDER_NONE
    return resolve().get_str(EMBEDDING_PROVIDER_KEY)


def resolve_provider() -> EmbeddingProvider | None:
    """The configured provider, or ``None`` for every way of not having one."""
    name = configured_provider_name()
    if name == EMBEDDING_PROVIDER_NONE:
        return None
    return providers().get(name)

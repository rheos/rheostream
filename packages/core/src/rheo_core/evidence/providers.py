"""The extraction provider seam: the registered providers, and which one is configured.

Mirrors Recallatron's embedding registry (``rheo_recallatron/embedding/registry.py``)
on purpose, so the two provider seams read the same way.

**Resolution never raises for a missing provider.** ``none`` (the production default),
a name nothing registered, and ``fake`` outside the test profile all resolve ``None``.
"A provider resolves" (``resolve_provider() is not None``) is the contract the recording
gate and the eligible-evidence service both read: no provider, no evidence recorded and
none claimed. A resolver error for a stray ``RHEO__`` variable is let out, as the
embedding registry lets it out: that is a startup misconfiguration, not "no provider".

**Looked up at call time, through this module's globals.** The configured name and the
registry are read when :func:`resolve_provider` runs, never captured at import, so a
test that sets the key, rebinds :func:`current_profile` or replaces :data:`PROVIDERS`
changes what every reader gets.

**The built-ins register on first use, never at import.** Registering reads the
deployment profile, and ``fake`` registers only when it is the test profile, so a
production ``provider = "fake"`` records and extracts nothing instead of running a
stand-in. ``TEST_PROFILE`` is imported inside the registering function: its home,
``rheo_core.modules.operations``, reaches the storage and work layers, and importing it
here at module level would put all of that on this package's import path.

**A transient fault raises.** A provider signals a fault it may recover from by raising
any exception; the caller owns retry and backoff.
"""

import threading
from collections.abc import Mapping
from typing import Final, Protocol

from rheo_core.evidence.extract import DigestBatch
from rheo_core.settings import current_profile, resolve

EXTRACTION_PROVIDER_KEY: Final = "automatic_memory.extraction.provider"
EXTRACTION_PROVIDER_NONE: Final = "none"
FAKE_PROVIDER: Final = "fake"

FAKE_EXTRACTION_MARKER: Final = "[fake-extract]"
"""The fixed marker :class:`FakeExtractionProvider` answers to: an item whose text
contains it yields a candidate, and anything else yields ``null``."""


class ExtractionProvider(Protocol):
    """Proposes at most one memory per digested evidence item.

    ``extract`` takes one :class:`~rheo_core.evidence.extract.DigestBatch` and returns
    the raw structured response, unvalidated; validating it is the caller's job, never
    the provider's. A transient fault raises any exception. A provider that builds a
    prompt lists the batch's ``mention_kinds``, when there are any, as the kinds a
    proposed mention may take.
    """

    @property
    def name(self) -> str: ...

    def extract(self, batch: DigestBatch) -> Mapping[str, object]: ...


class FakeExtractionProvider:
    """A deterministic stand-in, for tests only.

    Reads each of the batch's items by ``item_id`` and ``text``, and answers
    ``{"items": [{"item": <id>, "memory": <candidate or None>}, ...]}``: a
    ``note`` built from the text when it contains :data:`FAKE_EXTRACTION_MARKER`, and
    ``None`` otherwise. Every batch it was called with is kept in :attr:`calls`, so a
    test can assert on exactly what the model would have seen. The registered instance
    lives for the whole process, so a test starts from :meth:`reset_calls` (or
    registers a fresh instance) rather than reading calls an earlier test made.

    Registered only under the test profile. It lives in the package rather than under
    the test harness for the reason the embedding fake does: the registry registers it,
    and the registry is production code.
    """

    def __init__(self) -> None:
        self.calls: list[DigestBatch] = []

    @property
    def name(self) -> str:
        return FAKE_PROVIDER

    def reset_calls(self) -> None:
        """Forget every recorded batch."""
        self.calls.clear()

    def extract(self, batch: DigestBatch) -> Mapping[str, object]:
        self.calls.append(batch)
        items: list[dict[str, object]] = []
        for item in batch.items:
            text = item.text
            memory: dict[str, object] | None = None
            if FAKE_EXTRACTION_MARKER in text:
                memory = {
                    "kind": "note",
                    "title": text.strip()[:200],
                    "body": text,
                    "confidence": 1.0,
                }
            items.append({"item": item.item_id, "memory": memory})
        return {"items": items}


PROVIDERS: Final[dict[str, ExtractionProvider]] = {}
"""Registry name to provider. Empty until first use, when
:func:`register_builtin_providers` fills it once; read it through :func:`providers`."""

_builtins_lock: Final = threading.Lock()
_builtins_registered = False


def _ensure_builtin_providers() -> None:
    """Register the built-ins the first time anything uses the registry, then never.

    Marked done only after registration returns, so a profile read that raises raises
    again on the next use instead of leaving a silently empty registry.
    """
    global _builtins_registered
    if _builtins_registered:
        return
    with _builtins_lock:
        if _builtins_registered:
            return
        register_builtin_providers()
        _builtins_registered = True


def providers() -> dict[str, ExtractionProvider]:
    """The registry, with the built-ins registered."""
    _ensure_builtin_providers()
    return PROVIDERS


def register_provider(name: str, provider: ExtractionProvider) -> None:
    """Make ``provider`` resolvable as ``name``.

    The built-ins register first, so a provider registered under a built-in's name
    replaces it; that is how a test puts a fault-injecting or malformed-output variant
    in place of ``fake``.
    """
    _ensure_builtin_providers()
    PROVIDERS[name] = provider


def register_builtin_providers() -> None:
    """The one built-in: ``fake``, only under the test profile.

    The same gate the core's own install step uses for a test-only behaviour. No real
    provider is built in yet; one registers here when it lands.
    """
    from rheo_core.modules.operations import TEST_PROFILE

    if current_profile() == TEST_PROFILE:
        PROVIDERS[FAKE_PROVIDER] = FakeExtractionProvider()


def configured_provider_name() -> str:
    """The deployment's configured extraction provider name; the key's only reader."""
    return resolve().get_str(EXTRACTION_PROVIDER_KEY)


def resolve_provider() -> ExtractionProvider | None:
    """The configured provider, or ``None`` for every way of not having one."""
    name = configured_provider_name()
    if name == EXTRACTION_PROVIDER_NONE:
        return None
    return providers().get(name)

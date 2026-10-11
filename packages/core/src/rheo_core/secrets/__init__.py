"""The secret store and ``secret://`` reference resolution (A4, FR-13, guardrail 14).

A file-backed store with an environment-variable backend, both behind
``SecretStore.resolve(ref, scope) -> SecretValue``, with ``create`` and ``discard``
of file values under an explicitly writable scope. A ``SecretScope`` is constructed
by ``SecretStore.scope_for(component, *prefixes)``; a component built without one has
no way to resolve anything. The sentinel in ``scope.py`` and the AST scans stop
*accidental* construction, and that is their ceiling: Python has no private state, so
code that has already imported this package could forge or widen a scope, just as it
could read ``os.environ`` directly. The real boundary is the module import scan (no
``modules/**`` file imports ``rheo_core.secrets``), which is what keeps domain code
away from the store. ``SecretValue`` is redacted in ``repr``/``str``, compares in
constant time, refuses every serialisation route, and exposes its bytes only through
``expose()``.

**References never travel.** A ``SecretRef`` is the only form of a secret that appears
in configuration, the control plane, workspace tables or an export, and even the
reference stays inside the component that owns it: no ``SecretRef`` ever appears in a
``WorkspaceContext``, an operation input, or a log record. A runtime request names a
credential slot, a receipt names a connection, and the owning component maps that to
its reference at the moment of use (criterion 17).

Scope constants belong to their components, not to this package: the storage
component (C3) constructs its own with ``scope_for("storage", ...)``.
"""

from rheo_core.secrets.backends import EnvBackend, FileBackend
from rheo_core.secrets.refs import (
    SECRET_MISSING,
    SECRET_PERMISSIONS,
    SECRET_REF_MALFORMED,
    SECRET_SCOPE_DENIED,
    SecretBackend,
    SecretRef,
    SecretRefusal,
    is_secret_reference,
)
from rheo_core.secrets.scope import SecretScope
from rheo_core.secrets.store import (
    SecretStore,
    check_env_references,
    env_references,
)
from rheo_core.secrets.value import (
    REDACTED,
    SecretValue,
    SecretValueNotSerialisable,
    refuse_secret_values,
)

__all__ = [
    "REDACTED",
    "SECRET_MISSING",
    "SECRET_PERMISSIONS",
    "SECRET_REF_MALFORMED",
    "SECRET_SCOPE_DENIED",
    "EnvBackend",
    "FileBackend",
    "SecretBackend",
    "SecretRef",
    "SecretRefusal",
    "SecretScope",
    "SecretStore",
    "SecretValue",
    "SecretValueNotSerialisable",
    "check_env_references",
    "env_references",
    "is_secret_reference",
    "refuse_secret_values",
]

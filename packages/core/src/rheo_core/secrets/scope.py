"""``SecretScope``: the token that says what a component may resolve.

Constructed by ``SecretStore.scope_for(component, *prefixes)``, which calls the
package-private :func:`_new_scope` here. The class's ``__init__`` demands a sentinel
this module does not export, so ``SecretScope(...)`` from anywhere else raises, and so
do ``dataclasses.replace``, subclassing, pickling and copying (a scope never needs to
travel: scope constants belong to their components). Two AST tests assert that the
call ``SecretScope(`` and the names ``_SENTINEL`` / ``_new_scope`` appear in no file
outside ``rheo_core/secrets/``.

**What that guarantees, and what it does not.** The sentinel and the scans stop
*accidental* construction: every shape an honest caller would write. They are not a
privacy boundary, because Python has none. Code that has already imported this
package can reach the sentinel through the module object, build an instance with
``object.__new__`` plus ``object.__setattr__``, or widen a legitimate scope in place
the same way; and the same code could read ``os.environ`` without forging anything.
The real boundary is the import scan: no module distribution imports
``rheo_core.secrets`` at all, so domain code has no path to a store. That residual is
recorded here rather than chased with runtime state inside a security primitive.

A scope names the **full reference prefixes** it may resolve, which accommodates both
backends' id shapes (``secret://file/cluster/`` and ``secret://env/RHEO_CLUSTER_DSN``).
Matching is segment-aligned: a prefix ending in ``/`` covers everything beneath it, and
a prefix without one covers exactly that reference or a path beneath it, so
``secret://env/RHEO_CLUSTER_DSN`` does not also cover ``RHEO_CLUSTER_DSN_ADMIN``.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, NoReturn, SupportsIndex, final

from rheo_core.secrets.refs import SCHEME, SecretBackend, SecretRef

_SENTINEL: Final = object()
_PREFIX_ROOTS: Final = tuple(f"{SCHEME}{backend.value}/" for backend in SecretBackend)
_NO_TRAVEL: Final = "a SecretScope does not travel: it is neither pickled nor copied"


@final
@dataclass(frozen=True, slots=True, init=False)
class SecretScope:
    component: str
    prefixes: tuple[str, ...]
    writable: bool

    def __init__(
        self,
        component: str,
        prefixes: tuple[str, ...],
        *,
        writable: bool = False,
        _token: object = None,
    ) -> None:
        if _token is not _SENTINEL:
            raise TypeError(
                "SecretScope is constructed only by SecretStore.scope_for()"
            )
        object.__setattr__(self, "component", component)
        object.__setattr__(self, "prefixes", prefixes)
        object.__setattr__(self, "writable", writable)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("SecretScope cannot be subclassed")

    def __reduce__(self) -> NoReturn:
        raise TypeError(_NO_TRAVEL)

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        raise TypeError(_NO_TRAVEL)

    def permits(self, ref: SecretRef) -> bool:
        text = str(ref)
        for prefix in self.prefixes:
            if text == prefix:
                return True
            covered = prefix if prefix.endswith("/") else prefix + "/"
            if text.startswith(covered):
                return True
        return False


def _new_scope(
    component: str, prefixes: Iterable[str], *, writable: bool = False
) -> SecretScope:
    """Build a scope. Package-private: ``SecretStore.scope_for`` is the public door."""
    if not isinstance(component, str) or not component:
        raise ValueError("a secret scope needs a non-empty component name")
    if not isinstance(writable, bool):
        raise TypeError("writable must be a boolean")
    names = tuple(prefixes)
    if not names:
        raise ValueError(f"scope for {component!r} names no reference prefix")
    for prefix in names:
        if not isinstance(prefix, str) or not prefix.startswith(_PREFIX_ROOTS):
            raise ValueError(
                f"scope for {component!r}: a prefix must start with one of "
                f"{list(_PREFIX_ROOTS)}"
            )
    return SecretScope(component, names, writable=writable, _token=_SENTINEL)

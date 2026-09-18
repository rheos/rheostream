"""``url_for`` and friends: the only way this codebase names a URL.

The identity document's formula is ``https://<host>.<public_host><path>`` in subdomain
mode and ``https://<public_host><surface path><path>`` in path mode, and its own worked
example names the OAuth callback ``https://auth.example.test/auth/callback``. The two
are reconciled by ``fixed_path`` on the identity surface (see ``config.py``): every
other surface drops its prefix once it has its own host. ``base_host`` stays the
port-free Host-header match; empty ``public_host`` inherits it.

The join is normalised rather than concatenated. A surface whose path is the root
(``routing.shell.path`` defaults to ``"/"``) would otherwise contribute a second slash:
``"https://example.test" + "/" + "/login"`` is ``https://example.test//login``. One
helper does the join for **every** caller — both ``url_for`` branches and
``identity_path`` — so they cannot re-diverge, and so an operator who relocates a
surface's path cannot make one of them right and another wrong.
"""

from typing import Final

from rheo_core.routing.config import RoutingConfig, RoutingMode

ROOT_PREFIXES: Final[frozenset[str]] = frozenset({"", "/"})
"""Prefixes that contribute nothing: the empty prefix and the bare root."""


def _join_prefix(prefix: str, path: str) -> str:
    """Join a surface prefix to a path with exactly one ``/`` between them.

    The rule in full, because the web tier mirrors this function rather than importing
    it (B10), and two implementations agreeing on a contract neither honours would
    satisfy the byte-identical check with two matching wrongs:

    1. ``path`` is normalised to begin with a ``/``, so ``""`` becomes ``"/"`` and
       ``"v1/ops"`` becomes ``"/v1/ops"``. Without this, ``"/api" + "v1/ops"`` is
       ``/apiv1/ops`` — a silently wrong URL rather than a loud failure.
    2. A root prefix (``""`` or ``"/"``) then contributes nothing, which is what stops
       a root surface path doubling the slash.
    3. Any other prefix (``/auth``) carries its own leading slash and never a trailing
       one, so concatenation is correct there.
    """
    if not path.startswith("/"):
        path = f"/{path}"
    if prefix in ROOT_PREFIXES:
        return path
    return prefix + path


def url_for(config: RoutingConfig, surface: str, path: str) -> str:
    """The absolute URL for ``path`` on ``surface``, under this deployment's topology.

    Raises ``ValueError`` for an unknown surface, and for the ``docs`` and
    ``integration`` surfaces: those are marked ``external``/``reserved`` because this
    application never links to them.
    """
    spec = config.surfaces.get(surface)
    if spec is None:
        raise ValueError(f"unknown routing surface {surface!r}")
    if spec.external or spec.reserved:
        raise ValueError(
            f"routing surface {surface!r} is not served by this application"
        )
    if config.mode is RoutingMode.PATH:
        return f"{config.scheme}://{config.public_host}{_join_prefix(spec.path, path)}"
    prefix = spec.path if spec.fixed_path else ""
    host = f"{spec.host}.{config.public_host}"
    return f"{config.scheme}://{host}{_join_prefix(prefix, path)}"


def identity_path(config: RoutingConfig, path: str) -> str:
    """The same-host path for an identity endpoint (``/continue``, ``/logout``,
    ``/session/workspace``).

    Not ``url_for``: these are served by every application host, so the caller stays
    on the host it is already on and this never crosses one.

    It goes through ``_join_prefix`` for the same reason ``url_for`` does.
    ``routing.identity.path`` is an operator-settable key with no closed choice set, so
    an operator who sets it to ``"/"`` would get ``"//continue"`` from raw
    concatenation — which a browser reads as protocol-relative, i.e. a URL pointing at
    a host called ``continue``, not a path on this one.
    """
    return _join_prefix(config.surfaces.identity.path, path)


def application_hosts(config: RoutingConfig) -> frozenset[str]:
    """Every host this application serves.

    Subdomain mode: the shell host, the identity host, and every enabled module host,
    each qualified by the base host. Path mode: the single base host.
    """
    if config.mode is RoutingMode.PATH:
        return frozenset({config.base_host})
    surfaces = config.surfaces
    labels = [surfaces.shell.host, surfaces.identity.host]
    labels.extend(module.host for module in surfaces.modules.values())
    return frozenset(f"{label}.{config.base_host}" for label in labels)


def is_application_host(config: RoutingConfig, host: str) -> bool:
    """Whether ``host`` is one of this application's hosts.

    Backs the ``invalid_return`` refusal and the ``Origin`` check. An exact match:
    the caller passes a host already stripped of its port and lowercased, because
    only the caller knows which header it read it from.
    """
    return host in application_hosts(config)

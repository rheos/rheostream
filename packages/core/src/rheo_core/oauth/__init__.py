"""The MCP OAuth connector front door (issue #287).

``surface`` is the pure half: what the configured feature advertises, or why it
advertises nothing. The flow service and its HTTP routes build on it.
"""

from rheo_core.oauth.surface import (
    OAuthLifetimes,
    OAuthSurface,
    OAuthUnconfigured,
    UnconfiguredReason,
    oauth_surface,
)

__all__ = [
    "OAuthLifetimes",
    "OAuthSurface",
    "OAuthUnconfigured",
    "UnconfiguredReason",
    "oauth_surface",
]

"""``ContextPurpose``, in a module that imports nothing else from this package.

It lives here rather than in ``runtime.py`` because two independent things now need
it: the runtime contract (``RuntimeRequest.purpose``) and
``context.py:AuthenticatedPrincipal.bound_purpose``. ``runtime.py`` imports
``context.py`` for ``Actor``/``Audience``, so leaving the enum there and importing it
from ``context.py`` would close a cycle. A leaf module with one class and no imports
of its own cannot.

``runtime.py`` re-exports the name, so every existing import path -- ``from
rheo_contracts import ContextPurpose`` and ``from rheo_contracts.runtime import
ContextPurpose`` alike -- keeps working unedited.
"""

from enum import StrEnum


class ContextPurpose(StrEnum):
    """Why context is assembled for a run (intake vocabulary, not a directory kind)."""

    RESPOND = "respond"
    FOLLOW_UP = "follow_up"
    SHARE_WITH_REFERRAL = "share_with_referral"
    INTERNAL_ANALYSIS = "internal_analysis"

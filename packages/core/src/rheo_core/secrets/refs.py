"""``SecretRef``: the ``secret://<file|env>/<id>`` reference, and the secret refusals.

A reference is the only form of a secret that appears in configuration, in the control
plane, in workspace tables, or in an export. ``id`` is a slug path for the ``file``
backend (``cluster/primary-dsn``, ``ws/018f.../connection/018f.../signing``) and an
environment-variable name for ``env``. Anything else is ``secret_ref_malformed``.

A malformed reference's text is never echoed in the error: the most likely way to
produce one is pasting a secret value where a reference belongs.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self

SCHEME: Final = "secret://"

SECRET_REF_MALFORMED: Final = "secret_ref_malformed"
SECRET_SCOPE_DENIED: Final = "secret_scope_denied"
SECRET_MISSING: Final = "secret_missing"
SECRET_PERMISSIONS: Final = "secret_permissions"


class SecretRefusal(Exception):
    """A secret-store refusal. ``state`` is a content-free ``secret_*`` name.

    ``detail`` names a key, a variable or a path — never a value.
    """

    def __init__(self, state: str, detail: str) -> None:
        super().__init__(f"{state}: {detail}")
        self.state = state
        self.detail = detail


class SecretBackend(StrEnum):
    FILE = "file"
    ENV = "env"


_FILE_SEGMENT = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_slug_path(text: object) -> bool:
    """True for a ``file`` id: ``/``-separated lowercase slug segments, nothing else.

    No empty segment (so no leading ``/`` and no ``//``), no ``.`` or ``..``, no
    uppercase or underscore. Shared by ``SecretRef`` and ``FileBackend.read``, so a
    backend called directly cannot be handed a path.
    """
    if not isinstance(text, str) or not text:
        return False
    return all(_FILE_SEGMENT.fullmatch(segment) for segment in text.split("/"))


def _malformed(reason: str) -> SecretRefusal:
    return SecretRefusal(
        SECRET_REF_MALFORMED,
        f"malformed secret reference ({reason}); expected secret://<file|env>/<id>",
    )


@dataclass(frozen=True, slots=True)
class SecretRef:
    backend: SecretBackend
    id: str

    def __post_init__(self) -> None:
        if not isinstance(self.backend, SecretBackend):
            raise _malformed("unknown backend")
        if self.backend is SecretBackend.FILE:
            if not is_slug_path(self.id):
                raise _malformed("file id is not a slug path")
        elif not _ENV_NAME.fullmatch(self.id):
            raise _malformed("env id is not an environment-variable name")

    @classmethod
    def parse(cls, text: object) -> Self:
        if not isinstance(text, str) or not text.startswith(SCHEME):
            raise _malformed("missing secret:// scheme")
        backend, separator, ident = text[len(SCHEME) :].partition("/")
        if not separator:
            raise _malformed("missing /<id>")
        try:
            kind = SecretBackend(backend)
        except ValueError:
            raise _malformed("unknown backend") from None
        return cls(kind, ident)

    def __str__(self) -> str:
        return f"{SCHEME}{self.backend.value}/{self.id}"


def is_secret_reference(text: object) -> bool:
    """True when ``text`` is a str carrying the ``secret://`` scheme (parsed or not)."""
    return isinstance(text, str) and text.startswith(SCHEME)

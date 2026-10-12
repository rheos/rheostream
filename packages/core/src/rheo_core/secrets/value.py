"""``SecretValue``: a resolved secret that does not leak by accident.

``repr`` and ``str`` are both exactly ``SecretValue(<redacted>)``, and so is any
``format`` spec; equality compares in constant time (``hmac.compare_digest``); the
bytes are reachable only through :meth:`SecretValue.expose`. It cannot be hashed,
pickled, copied or subclassed, it has no ``__dict__``, and every serialisation route
refuses: plain ``json.dumps`` raises the standard library's ``TypeError``, the
:func:`refuse_secret_values` hook (the ``default=`` the core's JSON encoders install)
raises :class:`SecretValueNotSerialisable`, and pydantic serialisation of a model
holding one raises through a core-schema serializer that does the same (pydantic wraps
it in ``PydanticSerializationError``, whose message names this class and carries no
bytes).

Those guards live on :class:`OpaqueValue`, the base of every in-memory holder of
secret material (``SecretValue`` here, the connector access token in core's OAuth
client), so a fix to one reaches all of them. A direct subclass of ``OpaqueValue``
sets its redacted text and its own slot; nothing can subclass it further.
"""

import hmac
from typing import Any, ClassVar, Final, NoReturn, SupportsIndex, final

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

REDACTED: Final = "SecretValue(<redacted>)"


class SecretValueNotSerialisable(TypeError):
    """Raised by every encoder hook that meets an ``OpaqueValue``."""


def _refuse(value: object) -> NoReturn:
    raise SecretValueNotSerialisable(
        f"{type(value).__name__} cannot be serialised; read it only inside the "
        "presenting component"
    )


class OpaqueValue:
    """Redacted rendering and refused hashing, pickling, copying and serialisation
    for a holder of secret material. Direct subclasses only."""

    __slots__ = ()
    _redacted: ClassVar[str] = "OpaqueValue(<redacted>)"

    def __init_subclass__(cls, **kwargs: object) -> None:
        if OpaqueValue not in cls.__bases__:
            raise TypeError(f"{cls.__bases__[0].__name__} cannot be subclassed")
        super().__init_subclass__(**kwargs)

    def __repr__(self) -> str:
        return self._redacted

    def __str__(self) -> str:
        return self._redacted

    def __format__(self, format_spec: str) -> str:
        return self._redacted

    def __hash__(self) -> int:
        raise TypeError(f"{type(self).__name__} is unhashable")

    def __reduce__(self) -> NoReturn:
        _refuse(self)

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        _refuse(self)

    def __getstate__(self) -> NoReturn:
        _refuse(self)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        # Validation is an is-instance check; serialisation, in every mode, refuses.
        return core_schema.is_instance_schema(
            cls,
            serialization=core_schema.plain_serializer_function_ser_schema(
                _refuse, when_used="always"
            ),
        )


@final
class SecretValue(OpaqueValue):
    __slots__ = ("__raw",)
    _redacted = REDACTED

    def __init__(self, raw: bytes | bytearray) -> None:
        if not isinstance(raw, bytes | bytearray):
            raise TypeError("SecretValue wraps bytes")
        self.__raw: bytes = bytes(raw)

    def expose(self) -> bytes:
        """The secret bytes, the only route to them; for the presenting component."""
        return self.__raw

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretValue):
            return NotImplemented
        return hmac.compare_digest(self.__raw, other.__raw)

    # Defining ``__eq__`` would otherwise reset ``__hash__`` to ``None``.
    __hash__ = OpaqueValue.__hash__


def refuse_secret_values(obj: object) -> NoReturn:
    """A ``json.dumps(default=)`` hook: an ``OpaqueValue`` raises; anything else too."""
    if isinstance(obj, OpaqueValue):
        _refuse(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

"""The versioned extract file (spec § Data Models, the extract file).

One model, used by the extractor to write and by the importer to read, so the two
paths cannot drift. JSON Lines: line 1 is an :class:`ExtractHeader`, every later line
one :class:`ExtractUnit`. Every model is frozen with extra fields forbidden.

Bounds come from the destination's own constants (``SOURCE_KEY_MAX_LENGTH`` from the
contracts package, title/body/name/role bounds from this module's configuration)
rather than restated numbers. The title and body rules mirror the destination's
write validators (trimmed 1-200 character title; non-blank, byte-bounded body), so a
unit that validates here passes the checks the importer's write applies.

:func:`parse_extract` refuses with a ``<state>: <detail>`` refusal naming the line
number and the failing field locations only, never a value (a validation error's own
message quotes the input, and the input is real predecessor content) and never an
unknown key's name, which prints as :data:`EXTRA_FIELD_PLACEHOLDER`.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    ValidationError,
    field_validator,
    model_validator,
)
from rheo_contracts.source_units import SOURCE_KEY_MAX_LENGTH, MemoryKind

from rheo_recallatron.configuration import (
    BODY_MAX_BYTES,
    ENTITY_NAME_MAX_LENGTH,
    ENTITY_NAME_MIN_LENGTH,
    MENTION_ROLE_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    TITLE_MIN_LENGTH,
)
from rheo_recallatron.contracts import EntityKind

EXTRACT_FORMAT: Final = "rheo.migration.extract"
EXTRACT_VERSION: Final = 1

EXTRACT_EMPTY = "extract_empty"
EXTRACT_HEADER_INVALID = "extract_header_invalid"
EXTRACT_UNIT_INVALID = "extract_unit_invalid"
EXTRACT_COUNT_MISMATCH = "extract_count_mismatch"


class ExtractRefusal(Exception):
    def __init__(self, state: str, detail: str) -> None:
        super().__init__(f"{state}: {detail}")
        self.state = state
        self.detail = detail


def _source_key(value: str) -> str:
    """``<source_class>:<native id>``, both halves non-empty."""
    source_class, separator, native_id = value.partition(":")
    if not (source_class and separator and native_id):
        raise ValueError("a source key is <source_class>:<native id>")
    return value


SourceKey = Annotated[str, Field(min_length=3, max_length=SOURCE_KEY_MAX_LENGTH)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _trimmed_title(value: str) -> str:
    """Mirrors ``rheo_recallatron.contracts._trimmed_title`` (private there, so not
    imported; ``tests/test_migration_extract.py`` holds the two in parity): the
    trimmed value is what is kept, and it must be 1-200 characters."""
    trimmed = value.strip()
    if not TITLE_MIN_LENGTH <= len(trimmed) <= TITLE_MAX_LENGTH:
        raise ValueError(
            f"title must be {TITLE_MIN_LENGTH}-{TITLE_MAX_LENGTH} trimmed characters"
        )
    return trimmed


def _bounded_body(value: str) -> str:
    """Mirrors ``rheo_recallatron.contracts._bounded_body``: not blank, at most
    ``BODY_MAX_BYTES`` UTF-8 bytes, and never trimmed."""
    if not value.strip():
        raise ValueError("body must not be blank")
    if len(value.encode("utf-8")) > BODY_MAX_BYTES:
        raise ValueError(f"body must be at most {BODY_MAX_BYTES} UTF-8 bytes")
    return value


#: A JSON ``true`` is not a count (lax mode would read it as 1).
_Count = Annotated[NonNegativeInt, Field(strict=True)]


class ExtractHeader(_Frozen):
    format: Literal["rheo.migration.extract"]
    version: Literal[1]
    unit_count: _Count
    unresolved_count: _Count

    @field_validator("version", mode="before")
    @classmethod
    def _version_is_an_integer(cls, value: object) -> object:
        # A Literal cannot be marked strict, and lax mode matches true == 1.
        if type(value) is not int:
            raise ValueError("version must be the integer 1")
        return value


class ExtractMention(_Frozen):
    kind: EntityKind
    name: Annotated[
        str, Field(min_length=ENTITY_NAME_MIN_LENGTH, max_length=ENTITY_NAME_MAX_LENGTH)
    ]
    role: Annotated[str, Field(min_length=1, max_length=MENTION_ROLE_MAX_LENGTH)]
    entity_key: SourceKey

    @field_validator("entity_key")
    @classmethod
    def _entity_key_shape(cls, value: str) -> str:
        return _source_key(value)


class ExtractUnit(_Frozen):
    external_source_key: SourceKey
    kind: MemoryKind
    title: str
    body: str
    #: Strict: a JSON ``true`` is not a confidence. An integer 0 or 1 still is.
    confidence: float | None = Field(ge=0, le=1, strict=True)
    recorded_at: AwareDatetime
    mentions: tuple[ExtractMention, ...]

    @field_validator("external_source_key")
    @classmethod
    def _key_shape(cls, value: str) -> str:
        return _source_key(value)

    @field_validator("title")
    @classmethod
    def _title_is_trimmed_and_bounded(cls, value: str) -> str:
        return _trimmed_title(value)

    @field_validator("body")
    @classmethod
    def _body_is_nonblank_and_bounded(cls, value: str) -> str:
        return _bounded_body(value)

    @model_validator(mode="after")
    def _one_mention_per_entity_key(self) -> "ExtractUnit":
        """``memory_mention``'s key is ``(memory_id, entity_id)``: the mapping merges
        same-key mentions, and a unit that still repeats one is refused here."""
        keys = [mention.entity_key for mention in self.mentions]
        if len(keys) != len(set(keys)):
            raise ValueError("a unit carries at most one mention per entity_key")
        return self


@dataclass(frozen=True)
class Extract:
    header: ExtractHeader
    units: tuple[ExtractUnit, ...]


def dump_extract(units: Sequence[ExtractUnit], *, unresolved_count: int) -> list[str]:
    """The extract's lines, header first, each without its trailing newline."""
    header = ExtractHeader(
        format=EXTRACT_FORMAT,
        version=EXTRACT_VERSION,
        unit_count=len(units),
        unresolved_count=unresolved_count,
    )
    return [header.model_dump_json(), *(unit.model_dump_json() for unit in units)]


#: Printed in place of an unknown field's name: the key came from the input, so it
#: is input too.
EXTRA_FIELD_PLACEHOLDER: Final = "<extra>"


def _location(item: Mapping[str, Any]) -> str:
    parts = [str(part) for part in item["loc"]]
    if item["type"] == "extra_forbidden" and parts:
        parts[-1] = EXTRA_FIELD_PLACEHOLDER
    return ".".join(parts) or "<line>"


def _locations(error: ValidationError) -> str:
    """Field locations and error types only; never ``input``, ``msg`` or an
    unknown key's name."""
    return ", ".join(
        f"{_location(item)}={item['type']}"
        for item in error.errors(include_url=False, include_input=False)
    )


def parse_extract(lines: Iterable[str]) -> Extract:
    """Read an extract back, refusing any line that fails the model and a header
    whose ``unit_count`` disagrees with the units that follow it."""
    header: ExtractHeader | None = None
    units: list[ExtractUnit] = []
    for line_number, line in enumerate(lines, start=1):
        if header is None:
            try:
                header = ExtractHeader.model_validate_json(line)
            except ValidationError as error:
                raise ExtractRefusal(
                    EXTRACT_HEADER_INVALID, f"line 1: {_locations(error)}"
                ) from None
            continue
        if not line.strip():
            continue
        try:
            units.append(ExtractUnit.model_validate_json(line))
        except ValidationError as error:
            raise ExtractRefusal(
                EXTRACT_UNIT_INVALID, f"line {line_number}: {_locations(error)}"
            ) from None
    if header is None:
        raise ExtractRefusal(EXTRACT_EMPTY, "the extract has no header line")
    if header.unit_count != len(units):
        raise ExtractRefusal(
            EXTRACT_COUNT_MISMATCH,
            f"header unit_count={header.unit_count}, units read={len(units)}",
        )
    return Extract(header, tuple(units))

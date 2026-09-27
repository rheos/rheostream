"""The versioned extract file (spec § Data Models, the extract file).

One model, used by the extractor to write and by the importer to read, so the two
paths cannot drift. JSON Lines: line 1 is an :class:`ExtractHeader`, every later line
one :class:`ExtractUnit`. Every model is frozen with extra fields forbidden.

Bounds come from the destination's own constants (``SOURCE_KEY_MAX_LENGTH`` from the
contracts package, title/body/name/role bounds from this module's configuration)
rather than restated numbers, so a unit that validates here fits the tables the
importer writes.

:func:`parse_extract` refuses with a ``<state>: <detail>`` refusal naming the line
number and the failing field locations only, never a value: a validation error's own
message quotes the input, and the input is real predecessor content.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated, Final, Literal

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


class ExtractHeader(_Frozen):
    format: Literal["rheo.migration.extract"]
    version: Literal[1]
    unit_count: NonNegativeInt
    unresolved_count: NonNegativeInt


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
    title: Annotated[
        str, Field(min_length=TITLE_MIN_LENGTH, max_length=TITLE_MAX_LENGTH)
    ]
    body: str
    confidence: float | None = Field(ge=0, le=1)
    recorded_at: AwareDatetime
    mentions: tuple[ExtractMention, ...]

    @field_validator("external_source_key")
    @classmethod
    def _key_shape(cls, value: str) -> str:
        return _source_key(value)

    @field_validator("body")
    @classmethod
    def _body_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > BODY_MAX_BYTES:
            raise ValueError(f"body must be at most {BODY_MAX_BYTES} UTF-8 bytes")
        return value

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


def _locations(error: ValidationError) -> str:
    """Field locations and error types only; never ``input`` or ``msg``."""
    return ", ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<line>'}={item['type']}"
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

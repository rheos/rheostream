"""Versioned, private-file contract for one-time searchable history import."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

HISTORY_EXTRACT_FORMAT: Final = "rheo.migration.history"
HISTORY_EXTRACT_VERSION: Final = 1
HISTORY_BODY_MAX_BYTES: Final = 1_048_576
HISTORY_EXTRACT_INVALID: Final = "history_extract_invalid"


class HistoryExtractRefusal(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(f"{HISTORY_EXTRACT_INVALID}: {detail}")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HistoryExtractHeader(_Frozen):
    format: Literal["rheo.migration.history"]
    version: Literal[1]
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    graph_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None
    unit_count: Annotated[int, Field(strict=True, ge=0)]
    unresolved_count: Annotated[int, Field(strict=True, ge=0)]

    @field_validator("version", mode="before")
    @classmethod
    def _version_is_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value


class HistoryExtractUnit(_Frozen):
    external_source_key: Annotated[str, Field(min_length=3, max_length=400)]
    source_reference: Annotated[str | None, Field(max_length=400)] = None
    kind: Literal[
        "conversation",
        "procedural_note",
        "session_digest",
        "memory_item",
        "graph_entity",
        "graph_event",
        "profile_item",
    ]
    status: Literal[
        "turn",
        "unconfirmed",
        "confirmed",
        "superseded",
        "current",
        "conflicted",
        "summary",
        "event",
    ]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    body: str
    occurred_at: AwareDatetime
    session_key: Annotated[str | None, Field(max_length=400)] = None
    chat_key: Annotated[str | None, Field(max_length=400)] = None
    source_role: Annotated[str | None, Field(max_length=100)] = None
    source_category: Annotated[str | None, Field(max_length=200)] = None
    source_created_at: AwareDatetime | None = None
    confirmed_at: AwareDatetime | None = None
    superseded_by_source_key: Annotated[str | None, Field(max_length=400)] = None

    @field_validator("external_source_key", "superseded_by_source_key")
    @classmethod
    def _source_key(cls, value: str | None) -> str | None:
        if value is not None:
            source_class, separator, native_id = value.partition(":")
            if not (source_class and separator and native_id):
                raise ValueError("source key must include a class and native id")
        return value

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        if not value.strip() or len(value.encode("utf-8")) > HISTORY_BODY_MAX_BYTES:
            raise ValueError("body must be nonblank and byte-bounded")
        return value

    @model_validator(mode="after")
    def _status_matches_kind(self) -> Self:
        allowed = {
            "conversation": {"turn"},
            "procedural_note": {"unconfirmed", "confirmed", "superseded"},
            "session_digest": {"summary"},
            "memory_item": {"current", "superseded", "conflicted"},
            "graph_entity": {"current", "superseded"},
            "graph_event": {"event"},
            "profile_item": {"summary"},
        }
        if self.status not in allowed[self.kind]:
            raise ValueError("history kind and status disagree")
        if self.status == "confirmed" and self.confirmed_at is None:
            raise ValueError("confirmed notes require confirmed_at")
        return self


@dataclass(frozen=True)
class HistoryExtract:
    header: HistoryExtractHeader
    units: tuple[HistoryExtractUnit, ...]


def dump_history_extract(
    units: Sequence[HistoryExtractUnit],
    *,
    source_sha256: str,
    unresolved_count: int,
    graph_sha256: str | None = None,
) -> list[str]:
    header = HistoryExtractHeader(
        format=HISTORY_EXTRACT_FORMAT,
        version=HISTORY_EXTRACT_VERSION,
        source_sha256=source_sha256,
        graph_sha256=graph_sha256,
        unit_count=len(units),
        unresolved_count=unresolved_count,
    )
    return [header.model_dump_json(), *(unit.model_dump_json() for unit in units)]


def parse_history_extract(lines: Iterable[str]) -> HistoryExtract:
    header: HistoryExtractHeader | None = None
    units: list[HistoryExtractUnit] = []
    for number, line in enumerate(lines, start=1):
        try:
            if header is None:
                header = HistoryExtractHeader.model_validate_json(line)
            elif line.strip():
                units.append(HistoryExtractUnit.model_validate_json(line))
        except ValidationError as error:
            # Pydantic messages may contain the actual private input. Field locations
            # and error types are enough to diagnose a malformed extract safely.
            locations = ", ".join(
                ".".join(
                    str(part)
                    for part in (
                        (*issue["loc"][:-1], "<extra>")
                        if issue["type"] == "extra_forbidden" and issue["loc"]
                        else issue["loc"]
                    )
                )
                + "="
                + str(issue["type"])
                for issue in error.errors(include_url=False, include_input=False)
            )
            raise HistoryExtractRefusal(f"line {number}: {locations}") from None
    if header is None:
        raise HistoryExtractRefusal("no header")
    if header.unit_count != len(units):
        raise HistoryExtractRefusal("unit count mismatch")
    keys = [unit.external_source_key for unit in units]
    if len(keys) != len(set(keys)):
        raise HistoryExtractRefusal("duplicate source key")
    return HistoryExtract(header, tuple(units))

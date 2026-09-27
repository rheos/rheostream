"""The predecessor's ontology patch log, ``graph.jsonl`` (spec § System Components 1).

One JSON object per line, in two families told apart by ``op``: an op line (``relate``,
``confirm_relate``, ``unrelate``, ``confirm``, ``supersede``) and an op-less full entity
record. A line's 1-based line number is the native id of an op line, which carries no
id of its own; an entity record's native id is its ``id``.

This module is the parser scaffold and the op-line / full-record split, plus the live
entity fold the denylist needs. The edge fold (``fold_edges``) is a later phase's.

Liveness is supersession only ("only live heads migrate", spec § Data Models): the
last full record per ``id`` wins, a ``supersede`` op naming an id as ``old``
supersedes it wherever in the file it sits, and a live head has ``superseded_by``
null. ``valid_until`` does not decide liveness here: a deadline record carries one and
still migrates, so its label belongs in the denylist.
"""

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

GRAPH_FILE_NAME: Final = "graph.jsonl"

OPS: Final = frozenset({"relate", "confirm_relate", "unrelate", "confirm", "supersede"})


@dataclass(frozen=True)
class OpLine:
    line_number: int
    op: str
    fields: Mapping[str, object]


@dataclass(frozen=True)
class EntityLine:
    line_number: int
    entity_id: str
    record: Mapping[str, object]


@dataclass(frozen=True)
class ParsedGraph:
    entity_lines: tuple[EntityLine, ...]
    op_lines: tuple[OpLine, ...]
    #: Lines that are not a JSON object, or an op-less record with no string id.
    malformed_line_numbers: tuple[int, ...]
    #: Lines whose ``op`` is present but is none of :data:`OPS`.
    unknown_op_line_numbers: tuple[int, ...]


def parse_graph_lines(lines: Iterable[str]) -> ParsedGraph:
    entities: list[EntityLine] = []
    ops: list[OpLine] = []
    malformed: list[int] = []
    unknown: list[int] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            parsed: object = json.loads(line)
        except json.JSONDecodeError:
            malformed.append(line_number)
            continue
        if not isinstance(parsed, dict):
            malformed.append(line_number)
            continue
        op = parsed.get("op")
        if op is None:
            entity_id = parsed.get("id")
            if isinstance(entity_id, str) and entity_id:
                entities.append(EntityLine(line_number, entity_id, parsed))
            else:
                malformed.append(line_number)
        elif isinstance(op, str) and op in OPS:
            ops.append(OpLine(line_number, op, parsed))
        else:
            unknown.append(line_number)
    return ParsedGraph(tuple(entities), tuple(ops), tuple(malformed), tuple(unknown))


def read_graph(ontology_dir: os.PathLike[str] | str) -> ParsedGraph:
    """Parse ``<ontology_dir>/graph.jsonl``. Read-only; never writes beside it."""
    path = Path(ontology_dir) / GRAPH_FILE_NAME
    with path.open(encoding="utf-8") as handle:
        return parse_graph_lines(handle)


def live_entities(parsed: ParsedGraph) -> list[EntityLine]:
    """The folded head of every live entity, in first-seen order."""
    heads: dict[str, EntityLine] = {}
    for entity in parsed.entity_lines:
        heads[entity.entity_id] = entity
    superseded = {
        old
        for op_line in parsed.op_lines
        if op_line.op == "supersede"
        and isinstance(old := op_line.fields.get("old"), str)
    }
    return [
        head
        for entity_id, head in heads.items()
        if entity_id not in superseded and head.record.get("superseded_by") is None
    ]


def live_entity_labels(parsed: ParsedGraph) -> list[str]:
    return [
        label
        for head in live_entities(parsed)
        if isinstance(label := head.record.get("label"), str)
    ]

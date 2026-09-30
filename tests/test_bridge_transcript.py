"""The transcript reader: the human-line predicate, the slug and source validation.

Every line and every file here is synthetic, built by the test inside a pytest
``tmp_path``. No test reads a real home directory, a real ``.claude/`` tree or
a real transcript. The session ids and uuids are invented.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from rheo_bridge import transcript
from rheo_bridge.transcript import Missing, Ok, Refused

SESSION_ID = "0b7e4f10-0000-4000-8000-00000000a11c"
LINE_UUID = "7d2c9a33-0000-4000-8000-0000000b0b01"


def human(
    content: object = "please tidy the example report", **extra: Any
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "type": "user",
        "origin": {"kind": "human"},
        "isSidechain": False,
        "sessionId": SESSION_ID,
        "uuid": LINE_UUID,
        "timestamp": "2026-01-02T03:04:05.678Z",
        "entrypoint": "cli",
        "message": {"role": "user", "content": content},
    }
    line.update(extra)
    return line


# --- is_human_line --------------------------------------------------------


def test_a_human_string_line_is_kept() -> None:
    line = human()
    assert transcript.is_human_line(line)
    assert transcript.human_text(line) == "please tidy the example report"


def test_a_human_list_of_text_blocks_is_kept_and_joined() -> None:
    line = human(
        [
            {"type": "text", "text": "first paragraph"},
            {"type": "text", "text": "second paragraph"},
        ]
    )
    assert transcript.is_human_line(line)
    assert transcript.human_text(line) == "first paragraph\n\nsecond paragraph"


def _without_origin() -> dict[str, Any]:
    line = human()
    del line["origin"]
    return line


NON_HUMAN_LINES: dict[str, dict[str, Any]] = {
    "assistant": {
        "type": "assistant",
        "sessionId": SESSION_ID,
        "uuid": LINE_UUID,
        "timestamp": "2026-01-02T03:04:05Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    },
    "tool_result": human(
        [{"type": "tool_result", "tool_use_id": "toolu_synthetic", "content": "ok"}],
        toolUseResult={"stdout": "ok"},
    ),
    "tool_use_result_key_only": human("ok", toolUseResult="ok"),
    "meta": human("<synthetic meta text>", isMeta=True),
    "sidechain": human(isSidechain=True),
    "compact_summary": human("a synthetic summary", isCompactSummary=True),
    "transcript_only": human("synthetic display text", isVisibleInTranscriptOnly=True),
    "task_notification": human("a task finished", origin={"kind": "task-notification"}),
    "peer_message": human("hello from a peer", origin={"kind": "peer"}),
    "scheduled_prompt": human("run the nightly thing", origin={"kind": "scheduled"}),
    "attachment": {
        "type": "attachment",
        "sessionId": SESSION_ID,
        "uuid": LINE_UUID,
        "timestamp": "2026-01-02T03:04:05Z",
        "attachment": {"type": "file", "path": "/tmp/example.txt"},
    },
    "missing_origin": _without_origin(),
    "origin_not_an_object": human(origin="human"),
    "mixed_content_list": human(
        [
            {"type": "text", "text": "look at this"},
            {"type": "tool_use", "id": "toolu_synthetic", "name": "Read", "input": {}},
        ]
    ),
    "image_block": human([{"type": "image", "source": {"type": "base64", "data": ""}}]),
    "empty_content_list": human([]),
    "text_block_without_text": human([{"type": "text"}]),
    "content_not_text": human({"type": "text", "text": "not a list"}),
    "no_message": {
        "type": "user",
        "origin": {"kind": "human"},
        "sessionId": SESSION_ID,
        "uuid": LINE_UUID,
    },
}


@pytest.mark.parametrize("name", sorted(NON_HUMAN_LINES))
def test_every_non_human_shape_is_skipped(name: str) -> None:
    line = NON_HUMAN_LINES[name]
    assert not transcript.is_human_line(line)
    assert transcript.human_text(line) is None


@pytest.mark.parametrize("flag", transcript.MACHINE_FLAGS)
@pytest.mark.parametrize("value", [True, 1, "true", ["x"], None, "false", 0])
def test_a_machine_flag_other_than_absent_or_false_excludes_the_line(
    flag: str, value: object
) -> None:
    line = human(**{flag: value})
    assert not transcript.is_human_line(line)


def test_machine_flags_explicitly_false_are_admitted() -> None:
    line = human(**dict.fromkeys(transcript.MACHINE_FLAGS, False))
    assert transcript.is_human_line(line)


def test_the_machine_flags_cover_meta_sidechain_compaction_and_display_only() -> None:
    assert set(transcript.MACHINE_FLAGS) == {
        "isMeta",
        "isSidechain",
        "isCompactSummary",
        "isVisibleInTranscriptOnly",
    }


# --- parse_clock, extract_ids, entrypoint ---------------------------------


def test_parse_clock_reads_a_well_formed_timestamp() -> None:
    assert transcript.parse_clock(human()) == datetime(
        2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC
    )
    assert transcript.parse_clock(
        human(timestamp="2026-01-02T05:04:05+02:00")
    ) == datetime(2026, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2)))


@pytest.mark.parametrize(
    "value",
    [None, "", "yesterday", "2026-13-45T99:00:00Z", 1767323045, "2026-01-02T03:04:05"],
)
def test_parse_clock_returns_none_for_a_missing_or_malformed_timestamp(
    value: object,
) -> None:
    line = human()
    if value is None:
        del line["timestamp"]
    else:
        line["timestamp"] = value
    assert transcript.parse_clock(line) is None


def test_extract_ids() -> None:
    assert transcript.extract_ids(human()) == (SESSION_ID, LINE_UUID)
    assert transcript.extract_ids(human(uuid=None)) is None
    assert transcript.extract_ids(human(sessionId="")) is None
    assert transcript.extract_ids({"uuid": LINE_UUID}) is None


def test_entrypoint() -> None:
    assert transcript.entrypoint(human()) == "cli"
    assert transcript.entrypoint(human(entrypoint="claude-desktop")) == "claude-desktop"
    assert transcript.entrypoint(human(entrypoint=3)) is None
    assert transcript.entrypoint({}) is None


# --- slug -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/Users/example/Code/demo", "-Users-example-Code-demo"),
        ("/home/example/my_project.v2", "-home-example-my-project-v2"),
        ("/srv/Work Space/α-beta", "-srv-Work-Space---beta"),
        ("/tmp/abc123", "-tmp-abc123"),
    ],
)
def test_slug_keeps_letters_and_digits_and_replaces_everything_else(
    path: str, expected: str
) -> None:
    assert transcript.slug(path) == expected


def test_distinct_directories_can_share_a_slug() -> None:
    """The known limit: a slug match is not a directory match."""
    slugs = {transcript.slug(p) for p in ("/work/a-b", "/work/a_b", "/work/a/b")}
    assert slugs == {"-work-a-b"}


# --- validate_source ------------------------------------------------------


class Layout:
    def __init__(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        self.projects_root = base / "home" / ".claude" / "projects"
        self.projects_root.mkdir(parents=True)
        self.work = base / "work"
        self.parent_dir = self.work / "parent"
        self.child_dir = self.parent_dir / "child"
        self.child_dir.mkdir(parents=True)
        self.outside = base / "outside"
        self.outside.mkdir()

    def project(self, enrolled: Path) -> Path:
        directory = self.projects_root / transcript.slug(str(enrolled))
        directory.mkdir(exist_ok=True)
        return directory

    def session_file(self, enrolled: Path, name: str = "synthetic.jsonl") -> Path:
        path = self.project(enrolled) / name
        path.write_text('{"type":"user"}\n', encoding="utf-8")
        return path

    def validate(self, path: Path | str, enrolled: Path) -> transcript.ValidationResult:
        return transcript.validate_source(
            str(path), str(enrolled), projects_root=self.projects_root
        )


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path)


def test_a_file_directly_in_the_enrolled_project_is_ok(layout: Layout) -> None:
    path = layout.session_file(layout.parent_dir)
    assert layout.validate(path, layout.parent_dir) == Ok(path)


def test_a_symlink_that_resolves_outside_is_a_path_escape(layout: Layout) -> None:
    target = layout.outside / "elsewhere.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    link = layout.project(layout.parent_dir) / "linked.jsonl"
    link.symlink_to(target)
    assert layout.validate(link, layout.parent_dir) == Refused("path_escape")


def test_a_dot_dot_component_is_a_path_escape(layout: Layout) -> None:
    other = layout.session_file(layout.outside, "other.jsonl")
    project = layout.project(layout.parent_dir)
    dotted = f"{project}/../{other.parent.name}/{other.name}"
    assert layout.validate(dotted, layout.parent_dir) == Refused("path_escape")
    # Even a ``..`` that lands back in the enrolled project is refused.
    own = layout.session_file(layout.parent_dir)
    back = f"{project}/../{project.name}/{own.name}"
    assert layout.validate(back, layout.parent_dir) == Refused("path_escape")


def test_a_file_under_a_different_project_is_unmatched(layout: Layout) -> None:
    path = layout.session_file(layout.outside, "other.jsonl")
    assert layout.validate(path, layout.parent_dir) == Refused("project_unmatched")


def test_a_child_directory_session_is_unmatched_when_the_parent_is_enrolled(
    layout: Layout,
) -> None:
    # The child's slug starts with the parent's; a prefix match would admit it.
    assert transcript.slug(str(layout.child_dir)).startswith(
        transcript.slug(str(layout.parent_dir))
    )
    layout.project(layout.parent_dir)
    path = layout.session_file(layout.child_dir)
    assert layout.validate(path, layout.parent_dir) == Refused("project_unmatched")


def test_a_parent_directory_session_is_unmatched_when_the_child_is_enrolled(
    layout: Layout,
) -> None:
    layout.project(layout.child_dir)
    path = layout.session_file(layout.parent_dir)
    assert layout.validate(path, layout.child_dir) == Refused("project_unmatched")


def test_a_symlinked_file_inside_another_project_is_a_path_escape(
    layout: Layout,
) -> None:
    target = layout.outside / "elsewhere.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    link = layout.project(layout.child_dir) / "linked.jsonl"
    link.symlink_to(target)
    assert layout.validate(link, layout.parent_dir) == Refused("path_escape")


@pytest.mark.parametrize(
    "case",
    [
        "nested",
        "suffix",
        "directory",
        "missing_suffix",
        "missing_nested",
        "relative",
    ],
)
def test_anything_but_a_direct_jsonl_file_is_refused(layout: Layout, case: str) -> None:
    project = layout.project(layout.parent_dir)
    candidate: Path | str
    if case == "nested":
        (project / "subagents").mkdir()
        candidate = project / "subagents" / "agent.jsonl"
        candidate.write_text("{}\n", encoding="utf-8")
    elif case == "suffix":
        candidate = project / "synthetic.json"
        candidate.write_text("{}\n", encoding="utf-8")
    elif case == "directory":
        candidate = project / "folder.jsonl"
        candidate.mkdir()
    elif case == "missing_suffix":
        candidate = project / "absent.json"
    elif case == "missing_nested":
        candidate = project / "subagents" / "absent.jsonl"
    else:
        candidate = f"{project.name}/synthetic.jsonl"
    assert layout.validate(candidate, layout.parent_dir) == Refused("path_escape")


def test_an_absent_file_in_the_exact_slug_directory_is_missing(layout: Layout) -> None:
    project = layout.project(layout.parent_dir)
    absent = project / "absent.jsonl"
    result = layout.validate(absent, layout.parent_dir)
    assert result == Missing(absent)
    assert result != Ok(absent)


def test_an_absent_file_under_another_slug_is_unmatched(layout: Layout) -> None:
    other = layout.project(layout.child_dir)
    absent = other / "absent.jsonl"
    assert layout.validate(absent, layout.parent_dir) == Refused("project_unmatched")


def test_an_absent_file_with_a_dot_dot_component_is_a_path_escape(
    layout: Layout,
) -> None:
    project = layout.project(layout.parent_dir)
    dotted = f"{project}/../{project.name}/absent.jsonl"
    assert layout.validate(dotted, layout.parent_dir) == Refused("path_escape")


def test_an_absent_file_under_a_symlinked_slug_directory_is_a_path_escape(
    layout: Layout,
) -> None:
    escaped = layout.outside / "escaped-project"
    escaped.mkdir()
    (layout.projects_root / transcript.slug(str(layout.parent_dir))).symlink_to(escaped)
    absent = layout.projects_root / transcript.slug(str(layout.parent_dir)) / "a.jsonl"
    assert layout.validate(absent, layout.parent_dir) == Refused("path_escape")


def test_an_existing_file_under_a_symlinked_slug_directory_is_a_path_escape(
    layout: Layout,
) -> None:
    escaped = layout.outside / "escaped-project"
    escaped.mkdir()
    (escaped / "present.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    slug_dir = layout.projects_root / transcript.slug(str(layout.parent_dir))
    slug_dir.symlink_to(escaped)
    assert layout.validate(slug_dir / "present.jsonl", layout.parent_dir) == Refused(
        "path_escape"
    )


def test_a_dangling_symlink_is_a_path_escape_not_missing(layout: Layout) -> None:
    link = layout.project(layout.parent_dir) / "dangling.jsonl"
    link.symlink_to(layout.outside / "gone.jsonl")
    assert layout.validate(link, layout.parent_dir) == Refused("path_escape")


def test_a_nul_byte_in_the_path_is_refused_not_raised(layout: Layout) -> None:
    project = layout.project(layout.parent_dir)
    assert layout.validate(f"{project}/bad\x00.jsonl", layout.parent_dir) == Refused(
        "path_escape"
    )


def test_validate_source_never_consults_the_home_directory(
    layout: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))

    def refuse_home() -> Path:
        raise AssertionError("validate_source must not read the home directory")

    monkeypatch.setattr(Path, "home", staticmethod(refuse_home))
    path = layout.session_file(layout.parent_dir)
    assert layout.validate(path, layout.parent_dir) == Ok(path)
    assert not any(empty_home.iterdir())

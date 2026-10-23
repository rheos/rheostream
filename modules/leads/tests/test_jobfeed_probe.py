"""The opt-in invitations probe: what its log lines keep, and that it never stops a
run. No network; a fake upstream answers each call."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

import pytest
from rheo_leads.jobfeed.probe import (
    HELP_TEXT_MAX,
    help_text,
    log_detail_shape,
    probe_invitations,
    shape,
)
from rheo_leads.jobfeed.upstream import RunCut, Upstream, UpstreamFailure

ORG = "org-marker-9b3e1f70-2c4d-4a8e-b6f1-5d7c3e9a2b48"
NAME = "Jordan Example"
TITLE = "Build a fictional analytics dashboard"
INVITE_UID = "2113000000000000001"
POSTING = "~021234567890123456789"
MARKERS = (ORG, NAME, TITLE, INVITE_UID, POSTING, "Jordan", "dashboard")

INVITATIONS: dict[str, object] = {
    "status": "ok",
    "invitations": [
        {
            "id": INVITE_UID,
            "state": "pending",
            "kind": "Jordan",
            "createdDateTime": "2026-10-22T18:04:00Z",
            "job": {
                "ciphertext": POSTING,
                "title": TITLE,
                "url": f"https://jobs.example.com/jobs/Build-a-dashboard_{POSTING}/",
                "budget": 1500,
                "rate": 45.5,
                "hourly": False,
            },
            "client": {"name": NAME, "country": "CAN", "status": "VERIFIED"},
            "message": "Hi Jordan, " + "x" * 300,
            INVITE_UID: {"nested": True},
        }
    ],
}


def _line(caplog: pytest.LogCaptureFixture, prefix: str) -> str:
    lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)
    ]
    assert len(lines) == 1, lines
    return lines[0]


def test_a_shape_keeps_keys_and_kinds_and_no_value() -> None:
    kept = shape(INVITATIONS)
    text = json.dumps(kept)
    for marker in MARKERS:
        assert marker not in text
    assert kept == {
        "status": "word:ok",
        "invitations": {
            "?len": 1,
            "?items": [
                {
                    "id": "digits19",
                    "state": "word:pending",
                    "kind": "text<32",
                    "createdDateTime": "time",
                    "job": {
                        "ciphertext": "posting_id",
                        "title": "text<256",
                        "url": "url:jobs.example.com/jobs/posting_id",
                        "budget": "int",
                        "rate": "num",
                        "hourly": "bool",
                    },
                    "client": {
                        "name": "text<32",
                        "country": "enum:CAN",
                        "status": "enum:VERIFIED",
                    },
                    "message": "text256+",
                    "?skipped": 1,
                }
            ],
        },
    }


def test_a_url_path_keeps_only_page_words_and_kinds() -> None:
    assert shape("https://jobs.example.com/nx/proposals/interview/uid/2113?x=1") == (
        "url:jobs.example.com/nx/proposals/interview/uid/N?q"
    )
    assert shape("https://jobs.example.com/jobs/Jordan-Example-App") == (
        "url:jobs.example.com/jobs/s"
    )


def test_a_lower_case_word_is_kept_only_under_a_status_key() -> None:
    assert shape({"state": "jordan", "label": "jordan"}) == {
        "state": "word:jordan",
        "label": "text<32",
    }


def test_shapes_are_bounded() -> None:
    deep: object = "leaf"
    for _ in range(20):
        deep = {"a": deep}
    assert "..." in json.dumps(shape(deep))
    wide = shape({f"k{i}": i for i in range(100)})
    assert isinstance(wide, dict)
    assert len(wide) == 61 and wide["?skipped"] == 40
    listed = shape([{"a": 1}, {"a": 1}, {"b": "x"}, {"c": 2}])
    assert listed == {"?len": 4, "?items": [{"a": "int"}, {"b": "text<32"}]}


def test_help_text_is_printable_one_line_and_cut() -> None:
    result = {
        "content": [
            {"type": "text", "text": "Actions:\n  invitations\tlists them"},
            {"type": "text", "text": "y" * (HELP_TEXT_MAX * 2)},
            {"type": "image", "data": "zzz"},
        ]
    }
    text = help_text(result)
    assert text.startswith("Actions: invitations lists them y")
    assert len(text) == HELP_TEXT_MAX
    assert all(" " <= ch <= "~" for ch in text)
    assert help_text({"content": "nope"}) == ""


@dataclass
class FakeUpstream:
    help_answers: list[dict[str, object] | Exception] = field(default_factory=list)
    invitation_answer: dict[str, object] | Exception = field(
        default_factory=lambda: dict(INVITATIONS)
    )
    calls: list[tuple[str, object]] = field(default_factory=list)

    def tool_help(self, tool_name: str) -> dict[str, object]:
        self.calls.append(("help", tool_name))
        answer = self.help_answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def invitations(
        self, org_uid: str, params: Mapping[str, object]
    ) -> dict[str, object]:
        self.calls.append(("invitations", (org_uid, dict(params))))
        if isinstance(self.invitation_answer, Exception):
            raise self.invitation_answer
        return self.invitation_answer


def _help(text: str) -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}]}


def test_the_probe_logs_the_help_and_the_invitations_shape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeUpstream(help_answers=[_help("params: limit, offset")])
    with caplog.at_level(logging.INFO, logger="rheo_leads.jobfeed.probe"):
        probe_invitations(cast(Upstream, fake), ORG, "acme__")
    assert fake.calls == [
        ("help", "acme__list_freelancer_proposals"),
        ("invitations", (ORG, {})),
    ]
    assert _line(caplog, "jobfeed_probe_help") == (
        "jobfeed_probe_help tool=acme__list_freelancer_proposals outcome=ok "
        'text="params: limit, offset"'
    )
    line = _line(caplog, "jobfeed_probe_invitations")
    assert line.startswith("jobfeed_probe_invitations outcome=ok shape=")
    assert json.loads(line.split("shape=", 1)[1]) == shape(INVITATIONS)
    for record in caplog.records:
        for marker in MARKERS:
            assert marker not in record.getMessage()


def test_a_refused_help_name_falls_back_to_the_bare_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeUpstream(
        help_answers=[
            UpstreamFailure("tool_error", tool_code="UNKNOWN_TOOL"),
            _help("ok"),
        ],
        invitation_answer=UpstreamFailure("tool_error", tool_code="BAD_PARAMS"),
    )
    with caplog.at_level(logging.INFO, logger="rheo_leads.jobfeed.probe"):
        probe_invitations(cast(Upstream, fake), ORG, "acme__")
    helps = [r.getMessage() for r in caplog.records if "probe_help" in r.getMessage()]
    assert helps == [
        "jobfeed_probe_help tool=acme__list_freelancer_proposals "
        'outcome=tool_error:UNKNOWN_TOOL text=""',
        'jobfeed_probe_help tool=list_freelancer_proposals outcome=ok text="ok"',
    ]
    assert _line(caplog, "jobfeed_probe_invitations") == (
        "jobfeed_probe_invitations outcome=tool_error:BAD_PARAMS shape=null"
    )


def test_without_a_prefix_the_help_is_asked_for_once() -> None:
    fake = FakeUpstream(help_answers=[UpstreamFailure("unavailable")])
    probe_invitations(cast(Upstream, fake), ORG, "")
    assert [c for c in fake.calls if c[0] == "help"] == [
        ("help", "list_freelancer_proposals")
    ]


def test_a_run_cut_ends_the_probe_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeUpstream(help_answers=[RunCut()])
    with caplog.at_level(logging.INFO, logger="rheo_leads.jobfeed.probe"):
        probe_invitations(cast(Upstream, fake), ORG, "acme__")
    assert fake.calls == [("help", "acme__list_freelancer_proposals")]
    assert _line(caplog, "jobfeed_probe_invitations") == (
        "jobfeed_probe_invitations outcome=run_cut shape=null"
    )


def test_a_terminal_failure_is_logged_not_raised() -> None:
    fake = FakeUpstream(
        help_answers=[UpstreamFailure("unauthorized", terminal=True)] * 2,
        invitation_answer=UpstreamFailure("unauthorized", terminal=True),
    )
    probe_invitations(cast(Upstream, fake), ORG, "acme__")
    assert len(fake.calls) == 3


def test_the_detail_line_is_a_shape_or_none(caplog: pytest.LogCaptureFixture) -> None:
    detail: dict[str, object] = {
        "status": "ok",
        "data": {"marketplaceJobPosting": {"content": {"title": TITLE}}},
    }
    with caplog.at_level(logging.INFO, logger="rheo_leads.jobfeed.probe"):
        log_detail_shape(detail)
        log_detail_shape(None)
    lines = [r.getMessage() for r in caplog.records]
    assert lines == [
        "jobfeed_probe_detail outcome=ok shape="
        + json.dumps(shape(detail), separators=(",", ":"), sort_keys=True),
        "jobfeed_probe_detail outcome=none shape=null",
    ]
    assert TITLE not in lines[0]

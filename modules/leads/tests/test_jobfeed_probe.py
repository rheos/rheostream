"""The opt-in invitations probe: what its log lines keep, and that it never stops a
run. No network; a fake upstream answers each call."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

import pytest
from rheo_core.connectors.mcp_client import ToolRefused
from rheo_leads.jobfeed import probe
from rheo_leads.jobfeed.probe import (
    HELP_TEXT_MAX,
    HELP_TOOLS,
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
HOSTS = ("jobs.example.com",)
MARKERS = (
    ORG,
    NAME,
    TITLE,
    INVITE_UID,
    POSTING,
    "Jordan",
    "jordan",
    "analytics",
    "agency.example.net",
    "JORDAN_EXAMPLE",
)

INVITATIONS: dict[str, object] = {
    "status": "ok",
    "invitations": [
        {
            "id": INVITE_UID,
            "state": "PENDING",
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
            "client": {
                "name": NAME,
                "label": "JORDAN_EXAMPLE",
                "site": "https://agency.example.net/agencies/jordan",
            },
            "message": "Hi Jordan, " + "x" * 300,
            INVITE_UID: {"nested": True},
            "jordan.example@example.com": 1,
            "abc12345def": 2,
        }
    ],
}


def _lines(caplog: pytest.LogCaptureFixture, prefix: str) -> list[str]:
    return [m for m in (r.getMessage() for r in caplog.records) if m.startswith(prefix)]


def _no_markers(caplog: pytest.LogCaptureFixture) -> None:
    for record in caplog.records:
        for marker in MARKERS:
            assert marker not in record.getMessage(), marker


# --- shapes ---------------------------------------------------------------------


def test_a_shape_keeps_keys_and_kinds_and_no_value() -> None:
    kept = shape(INVITATIONS, hosts=HOSTS)
    text = json.dumps(kept)
    for marker in MARKERS:
        assert marker not in text, marker
    assert kept == {
        "status": "word:ok",
        "invitations": {
            "?len": 1,
            "?items": [
                {
                    "id": "digits19",
                    "state": "word:PENDING",
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
                        "label": "upper<32",
                        "site": "url:host/s/s",
                    },
                    "message": "text256+",
                    "?skipped": 3,
                }
            ],
        },
    }


def test_a_url_shows_only_its_own_hosts_and_page_words() -> None:
    assert shape(
        "https://jobs.example.com/nx/proposals/interview/uid/2113?x=1", hosts=HOSTS
    ) == ("url:jobs.example.com/nx/proposals/interview/uid/N?q")
    assert shape("https://jobs.example.com/jobs/Jordan-Example-App", hosts=HOSTS) == (
        "url:jobs.example.com/jobs/s"
    )
    assert shape("https://jobs.example.com/agencies/jordan") == "url:host/s/s"


@pytest.mark.parametrize("value", ["http://[abc", "https://a℀b/x"])
def test_a_url_that_does_not_parse_is_a_kind_not_an_error(value: str) -> None:
    assert shape(value) == "url:?"


def test_a_word_or_enum_is_shown_only_under_a_status_key() -> None:
    assert shape(
        {"state": "jordan", "label": "jordan", "type": "FIXED", "code": "JORDAN"}
    ) == {
        "state": "word:jordan",
        "label": "text<32",
        "type": "word:FIXED",
        "code": "upper<32",
    }


def test_id_like_keys_are_counted_and_field_names_kept() -> None:
    keys = {
        "3f2a9c1e-7b4d-4e8a-9c2f-5d1b7e3a9c04": 1,
        "a1b2c3d4e5f6a7b8c9d0": 1,
        "jordan.example@example.com": 1,
        "order12345": 1,
        "createdDateTime": 1,
        "client_record": 1,
        "address2": 1,
    }
    assert shape(keys) == {
        "createdDateTime": "int",
        "client_record": "int",
        "address2": "int",
        "?skipped": 4,
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


# --- help text --------------------------------------------------------------------


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


def test_help_text_redacts_the_org_ids_and_credentials_but_keeps_tool_names() -> None:
    secret = "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7"
    result = {
        "content": [
            {
                "type": "text",
                "text": f"org_uid {ORG} e.g. 1234567890123 token {secret} "
                "see list_freelancer_proposals and manage_proposals_accept_invitation",
            }
        ]
    }
    text = help_text(result, (ORG,))
    assert ORG not in text and secret not in text and "1234567890123" not in text
    assert text.count("[redacted]") == 3
    assert "list_freelancer_proposals" in text
    assert "manage_proposals_accept_invitation" in text


# --- the probe --------------------------------------------------------------------


@dataclass
class FakeUpstream:
    help_answers: dict[str, dict[str, object] | Exception] = field(default_factory=dict)
    invitation_answer: dict[str, object] | Exception = field(
        default_factory=lambda: dict(INVITATIONS)
    )
    calls: list[tuple[str, object]] = field(default_factory=list)
    holds: list[int | None] = field(default_factory=list)

    def tool_help(self, tool_name: str) -> dict[str, object]:
        self.calls.append(("help", tool_name))
        answer = self.help_answers.get(tool_name, _help(f"help for {tool_name}"))
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

    def hold_off(self, retry_after: int | None) -> None:
        self.holds.append(retry_after)


def _help(text: str) -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}]}


def _run(fake: FakeUpstream, prefix: str = "acme__") -> None:
    probe_invitations(cast(Upstream, fake), ORG, prefix, HOSTS)


def test_the_probe_asks_every_tools_help_then_lists_invitations(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeUpstream()
    with caplog.at_level(logging.INFO, logger="rheo_leads.jobfeed.probe"):
        _run(fake)
    assert fake.calls == [
        *(("help", "acme__" + tool) for tool in HELP_TOOLS),
        ("invitations", (ORG, {})),
    ]
    helps = _lines(caplog, "jobfeed_probe_help")
    assert len(helps) == len(HELP_TOOLS) == 14
    assert helps[1] == (
        "jobfeed_probe_help tool=acme__manage_proposals outcome=ok "
        'text="help for acme__manage_proposals"'
    )
    (line,) = _lines(caplog, "jobfeed_probe_invitations")
    assert line.startswith("jobfeed_probe_invitations outcome=ok shape=")
    assert json.loads(line.split("shape=", 1)[1]) == shape(INVITATIONS, hosts=HOSTS)
    _no_markers(caplog)


def test_once_the_bare_name_answers_only_bare_names_are_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unknown = UpstreamFailure("tool_error", tool_code="UNKNOWN_TOOL")
    fake = FakeUpstream(
        help_answers={"acme__list_freelancer_proposals": unknown},
        invitation_answer=UpstreamFailure("tool_error", tool_code="BAD_PARAMS"),
    )
    with caplog.at_level(logging.INFO, logger="rheo_leads.jobfeed.probe"):
        _run(fake)
    assert fake.calls[:3] == [
        ("help", "acme__list_freelancer_proposals"),
        ("help", "list_freelancer_proposals"),
        ("help", "manage_proposals"),
    ]
    assert len(fake.calls) == len(HELP_TOOLS) + 2
    assert _lines(caplog, "jobfeed_probe_help")[0] == (
        "jobfeed_probe_help tool=acme__list_freelancer_proposals "
        'outcome=tool_error:UNKNOWN_TOOL text=""'
    )
    assert _lines(caplog, "jobfeed_probe_invitations") == [
        "jobfeed_probe_invitations outcome=tool_error:BAD_PARAMS shape=null"
    ]


def test_without_a_prefix_each_help_is_asked_for_once() -> None:
    fake = FakeUpstream(
        help_answers={tool: UpstreamFailure("unavailable") for tool in HELP_TOOLS}
    )
    _run(fake, prefix="")
    assert [c for c in fake.calls if c[0] == "help"] == [
        ("help", tool) for tool in HELP_TOOLS
    ]


def test_a_rate_limit_waits_out_the_provider_and_ends_the_probe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    limited = UpstreamFailure("rate_limited", retry_after=30)
    fake = FakeUpstream(help_answers={"acme__get_draft": limited})
    with caplog.at_level(logging.INFO, logger="rheo_leads.jobfeed.probe"):
        _run(fake)
    assert fake.calls[-1] == ("help", "acme__get_draft")
    assert fake.holds == [30]
    assert _lines(caplog, "jobfeed_probe_invitations") == []


def test_a_rate_limited_listing_waits_too() -> None:
    fake = FakeUpstream(invitation_answer=UpstreamFailure("rate_limited"))
    _run(fake)
    assert fake.holds == [None]


def test_a_refused_call_ends_the_probe_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeUpstream(
        help_answers={
            "acme__list_freelancer_proposals": ToolRefused("tool_not_allowed")
        }
    )
    with caplog.at_level(logging.INFO, logger="rheo_leads.jobfeed.probe"):
        _run(fake)
    assert fake.calls == [("help", "acme__list_freelancer_proposals")]
    assert _lines(caplog, "jobfeed_probe_help") == [
        "jobfeed_probe_help tool=acme__list_freelancer_proposals "
        'outcome=refused text=""'
    ]
    fake = FakeUpstream(invitation_answer=ToolRefused("action_not_allowed"))
    with caplog.at_level(logging.INFO, logger="rheo_leads.jobfeed.probe"):
        _run(fake)
    assert _lines(caplog, "jobfeed_probe_invitations")[-1] == (
        "jobfeed_probe_invitations outcome=refused shape=null"
    )


def test_a_run_cut_ends_the_probe_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeUpstream(help_answers={"acme__list_freelancer_proposals": RunCut()})
    with caplog.at_level(logging.INFO, logger="rheo_leads.jobfeed.probe"):
        _run(fake)
    assert fake.calls == [("help", "acme__list_freelancer_proposals")]
    assert _lines(caplog, "jobfeed_probe_invitations") == [
        "jobfeed_probe_invitations outcome=run_cut shape=null"
    ]


def test_a_terminal_failure_is_logged_not_raised() -> None:
    denied = UpstreamFailure("unauthorized", terminal=True)
    answers: dict[str, dict[str, object] | Exception] = {}
    for tool in HELP_TOOLS:
        answers[tool] = answers["acme__" + tool] = denied
    fake = FakeUpstream(help_answers=answers, invitation_answer=denied)
    _run(fake)
    assert len(fake.calls) == 2 * len(HELP_TOOLS) + 1


def test_a_shape_that_cannot_be_built_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        raise ValueError("boom")

    monkeypatch.setattr(probe, "shape", broken)
    with caplog.at_level(logging.INFO, logger="rheo_leads.jobfeed.probe"):
        _run(FakeUpstream())
        log_detail_shape({"status": "ok"})
    assert _lines(caplog, "jobfeed_probe_invitations") == [
        "jobfeed_probe_invitations outcome=shape_failed shape=null"
    ]
    assert _lines(caplog, "jobfeed_probe_detail") == [
        "jobfeed_probe_detail outcome=shape_failed shape=null"
    ]


def test_the_detail_line_is_a_shape_or_none(caplog: pytest.LogCaptureFixture) -> None:
    detail: dict[str, object] = {
        "status": "ok",
        "data": {"marketplaceJobPosting": {"content": {"title": TITLE}}},
    }
    with caplog.at_level(logging.INFO, logger="rheo_leads.jobfeed.probe"):
        log_detail_shape(detail, HOSTS)
        log_detail_shape(None)
    lines = [r.getMessage() for r in caplog.records]
    assert lines == [
        "jobfeed_probe_detail outcome=ok shape="
        + json.dumps(shape(detail), separators=(",", ":"), sort_keys=True),
        "jobfeed_probe_detail outcome=none shape=null",
    ]
    assert TITLE not in lines[0]

"""Queue previews drop the feed's marker tag before they are cut (#420, #392)."""

from rheo_leads.queue_view import PREVIEW_CHARS, _preview

OPEN = "<untrusted_participant_content>"
CLOSE = "</untrusted_participant_content>"


def test_a_preview_drops_the_wrapper_before_it_is_cut() -> None:
    body = "Build an example storefront. " * 20
    preview = _preview(f"{OPEN}\n{body}\n{CLOSE}", PREVIEW_CHARS)
    assert preview is not None
    assert OPEN not in preview
    assert CLOSE not in preview
    # The tag spent none of the preview's characters.
    assert preview.startswith("Build an example storefront.")
    assert len(preview) == len(body[:PREVIEW_CHARS].rstrip()) + 1


def test_only_the_outer_wrapper_goes() -> None:
    assert _preview(f"{OPEN} Use <b>bold</b> here. {CLOSE}", PREVIEW_CHARS) == (
        "Use <b>bold</b> here."
    )
    assert (
        _preview("Plain text, no wrapper.", PREVIEW_CHARS) == "Plain text, no wrapper."
    )


def test_a_wrapper_around_nothing_is_no_preview() -> None:
    assert _preview(f"{OPEN}\n  \n{CLOSE}", PREVIEW_CHARS) is None

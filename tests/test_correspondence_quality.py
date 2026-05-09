"""Tests for the 2026-05-08 correspondence quality sweep:

- Recurring stock openers ("As the morning sunlight…", "After yesterday's…")
  are detected as opener:<phrase> markers in banned_filler.
- New banned-filler entries (recommendation pile-on, meta-commentary about
  the priority-contacts block, "As previously noted, Sir,") fire correctly.
- Length checks flag over-cap and under-floor briefings.
- Mock correspondence still passes postprocess clean.
"""

from __future__ import annotations

from jeeves.correspondence import (
    BANNED_FILLER,
    BANNED_OPENERS,
    CORRESPONDENCE_LENGTH_FLOOR,
    CORRESPONDENCE_LENGTH_HARD_CAP,
    fixture_classified,
    postprocess_html,
    render_mock_correspondence,
)


# ---------------------------------------------------------- opener detection -

def test_banned_opener_detected_in_first_paragraph():
    html = (
        "<!DOCTYPE html><html><body>"
        "<h1>📫 Correspondence — Friday, 8 May 2026</h1>"
        "<p>As the morning sunlight casts a warm glow over the landscape, "
        "Sir, today's post brings a mix of routine matters.</p>"
        "<p>Beyond that, the day shall proceed as expected.</p>"
        "</body></html>"
    )
    _, _, _, _, _, banned_filler = postprocess_html(html)
    assert "opener:as the morning sunlight" in banned_filler


def test_banned_opener_after_yesterdays_demanding_post():
    html = (
        "<!DOCTYPE html><html><body>"
        "<p>After yesterday's rather demanding post, Sir, today brings a quieter inbox.</p>"
        "</body></html>"
    )
    _, _, _, _, _, banned_filler = postprocess_html(html)
    assert any(b.startswith("opener:after yesterday's") for b in banned_filler)


def test_banned_opener_only_checks_first_paragraph():
    """Mid-briefing prose containing 'the morning brings' should NOT trigger."""
    html = (
        "<!DOCTYPE html><html><body>"
        "<p>Mister Lang, the Google security alert at 04:12 leads today's post.</p>"
        "<p>Separately, the morning brings news from your wife about Piper's storytime.</p>"
        "</body></html>"
    )
    _, _, _, _, _, banned_filler = postprocess_html(html)
    opener_hits = [b for b in banned_filler if b.startswith("opener:")]
    assert opener_hits == []


def test_legitimate_specific_opener_not_flagged():
    html = (
        "<!DOCTYPE html><html><body>"
        "<p>Good morning, Mister Lang. The Edmonds School District's request "
        "for a reference letter, due Friday, leads today's post.</p>"
        "</body></html>"
    )
    _, _, _, _, _, banned_filler = postprocess_html(html)
    opener_hits = [b for b in banned_filler if b.startswith("opener:")]
    assert opener_hits == []


# ------------------------------------------------- new banned filler entries -

def test_as_previously_noted_filler_detected():
    html = (
        "<!DOCTYPE html><html><body>"
        "<p>The opener.</p>"
        "<p>As previously noted, Sir, the ongoing matter of the GitHub workflow "
        "failures requires your attention.</p>"
        "</body></html>"
    )
    _, _, _, _, _, banned_filler = postprocess_html(html)
    assert "As previously noted, Sir," in banned_filler


def test_recommendation_pile_on_detected():
    html = (
        "<!DOCTYPE html><html><body>"
        "<p>Opener naming an item.</p>"
        "<p>I would recommend reviewing the logs and identifying the root causes.</p>"
        "<p>I would also recommend taking corrective action to prevent failures.</p>"
        "</body></html>"
    )
    _, _, _, _, _, banned_filler = postprocess_html(html)
    assert "I would recommend reviewing" in banned_filler
    assert "I would also recommend" in banned_filler


def test_separate_note_and_priority_contacts_meta_commentary():
    html = (
        "<!DOCTYPE html><html><body>"
        "<p>Opener.</p>"
        "<p>On a separate note, Sir, I would like to bring to your attention the "
        "fact that the priority-contacts block is currently empty.</p>"
        "</body></html>"
    )
    _, _, _, _, _, banned_filler = postprocess_html(html)
    assert "On a separate note" in banned_filler
    assert "I would like to bring to your attention" in banned_filler
    assert "the priority-contacts block" in banned_filler


def test_inbox_lightness_meta_commentary():
    html = (
        "<!DOCTYPE html><html><body>"
        "<p>Opener.</p>"
        "<p>In conclusion, today's correspondence is relatively light, with a "
        "focus on routine updates and newsletters.</p>"
        "</body></html>"
    )
    _, _, _, _, _, banned_filler = postprocess_html(html)
    assert "In conclusion," in banned_filler
    assert "today's correspondence is relatively light" in banned_filler
    assert "with a focus on routine updates" in banned_filler


# ------------------------------------------------- length detection ----------

def test_length_over_cap_flagged():
    """Body over CORRESPONDENCE_LENGTH_HARD_CAP gets a length_over_cap marker."""
    over_words = " ".join(["padding"] * (CORRESPONDENCE_LENGTH_HARD_CAP + 50))
    html = f"<!DOCTYPE html><html><body><p>Mister Lang. {over_words}</p></body></html>"
    _, wc, _, _, _, banned_filler = postprocess_html(html)
    assert wc > CORRESPONDENCE_LENGTH_HARD_CAP
    over_markers = [b for b in banned_filler if b.startswith("length_over_cap:")]
    assert len(over_markers) == 1
    assert over_markers[0] == f"length_over_cap:{wc}"


def test_length_under_floor_flagged():
    html = "<!DOCTYPE html><html><body><p>Too short.</p></body></html>"
    _, wc, _, _, _, banned_filler = postprocess_html(html)
    assert wc < CORRESPONDENCE_LENGTH_FLOOR
    under_markers = [b for b in banned_filler if b.startswith("length_under_floor:")]
    assert len(under_markers) == 1


def test_length_in_target_band_no_length_marker():
    """A briefing in the 500-800 word target band should produce no length marker."""
    body_words = ["Mister", "Lang"] + ["item"] * 600
    body = " ".join(body_words)
    html = f"<!DOCTYPE html><html><body><p>{body}</p></body></html>"
    _, wc, _, _, _, banned_filler = postprocess_html(html)
    assert CORRESPONDENCE_LENGTH_FLOOR < wc < CORRESPONDENCE_LENGTH_HARD_CAP
    length_markers = [b for b in banned_filler if b.startswith("length_")]
    assert length_markers == []


# ------------------------------------------------- regression: mock fixture --

def test_mock_correspondence_still_clean():
    """After the BANNED_FILLER expansion, the existing mock fixture must still
    pass postprocess without flagging spurious filler hits — false positives
    on legitimate prose would break the dry-run path."""
    classified = fixture_classified()
    html_raw = render_mock_correspondence("2026-05-08", classified)
    html, _, profane, bw, bt, bf = postprocess_html(html_raw)
    assert html.startswith("<!DOCTYPE html>")
    assert profane == 0
    assert bw == []
    assert bt == []
    # The mock may legitimately use a length marker if it's short — that's
    # OK; we only assert no PHRASE-level filler hits.
    phrase_hits = [
        b for b in bf
        if not b.startswith(("opener:", "length_"))
    ]
    assert phrase_hits == [], (
        f"Mock fixture tripped phrase-level filler: {phrase_hits}"
    )


def test_banned_opener_constants_lowercased():
    """All BANNED_OPENERS entries must be lowercase since opener match is case-insensitive."""
    for opener in BANNED_OPENERS:
        assert opener == opener.lower(), (
            f"Banned opener {opener!r} is not lowercase — match will miss"
        )


def test_banned_filler_constants_unique():
    """No duplicate entries in BANNED_FILLER (a refactor regression)."""
    assert len(BANNED_FILLER) == len(set(BANNED_FILLER))

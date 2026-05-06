"""Sprint-19 regression tests.

Locks four fixes from the 2026-05-05 audit:

* ``_rewrite_bare_domain_anchors`` — bare-domain anchor text + bare-domain
  prose rewritten to prose names; href URLs preserved; idempotent.
* ``_strip_tool_call_markup`` / ``_sanitise_findings_markup`` — NIM
  tool-call leak and stream-truncation merges (e.g. ``thatily_extract:5``)
  removed without disturbing clean prose.
* ``populate_vault_insight`` — wires PART 8 Library Stacks data when
  ``JEEVES_VAULT_PATH`` is set; soft no-op when not.
* PART5 + PART6 prompt strings carry the new empty-feed and repeat-detection
  hard rules so dedup stops re-summarising covered material.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# Heavy-import-free helpers — exercise the new logic by importing the module
# directly. write.py drags in llama-index; isolate the regex-only pieces by
# loading the module via importlib without triggering the heavy initialisers.
from jeeves.write import (
    _DOMAIN_PROSE_NAMES,
    _rewrite_bare_domain_anchors,
    PART5_INSTRUCTIONS,
    PART6_INSTRUCTIONS,
    PART7_INSTRUCTIONS,
    CONTINUATION_RULES,
)
from jeeves.research_sectors import (
    _strip_tool_call_markup,
    _sanitise_findings_markup,
    SectorSpec,
)
from jeeves.vault import populate_vault_insight


# ---------------------------------------------------------------------------
# bare-domain rewriter
# ---------------------------------------------------------------------------


def test_anchor_text_bare_domain_replaced_with_prose_name() -> None:
    inp = (
        '<p><a href="https://myedmondsnews.com/2026/05/council/">'
        'myedmondsnews.com</a> reports that the council voted.</p>'
    )
    out = _rewrite_bare_domain_anchors(inp)
    assert ">My Edmonds News</a>" in out
    assert ">myedmondsnews.com</a>" not in out
    # href must survive untouched.
    assert 'href="https://myedmondsnews.com/2026/05/council/"' in out


def test_bare_domain_in_prose_replaced_unlinked() -> None:
    inp = "<p>According to bbc.co.uk, the talks resumed.</p>"
    out = _rewrite_bare_domain_anchors(inp)
    assert "the BBC" in out
    assert "bbc.co.uk" not in out


def test_href_inside_anchor_never_modified() -> None:
    inp = '<a href="https://bbc.co.uk/news/article-123">Reuters</a>'
    out = _rewrite_bare_domain_anchors(inp)
    assert 'href="https://bbc.co.uk/news/article-123"' in out
    assert ">Reuters</a>" in out


def test_rewriter_is_idempotent() -> None:
    inp = (
        '<p><a href="https://x">myedmondsnews.com</a> and '
        '<a href="https://y">edmondsbeacon.com</a> both covered the story.</p>'
    )
    once = _rewrite_bare_domain_anchors(inp)
    twice = _rewrite_bare_domain_anchors(once)
    assert once == twice
    assert ">My Edmonds News</a>" in once
    assert ">the Edmonds Beacon</a>" in once


def test_rewriter_handles_empty_input() -> None:
    assert _rewrite_bare_domain_anchors("") == ""


def test_local_publication_domains_in_prose_name_map() -> None:
    """Confirm the Edmonds-area sources are wired so TTS reads cleanly."""
    for domain in (
        "myedmondsnews.com",
        "edmondsbeacon.com",
        "edmondswa.gov",
        "sno-isle.org",
    ):
        assert domain in _DOMAIN_PROSE_NAMES, f"{domain} missing from map"


# ---------------------------------------------------------------------------
# NIM tool-call markup stripper
# ---------------------------------------------------------------------------


def test_strip_tool_call_markup_handles_real_2026_05_04_corruption() -> None:
    """Exact corruption pattern observed in session-2026-05-04 global_news."""
    real = (
        "Now let me extract full content from the key articles I found, "
        "particularly the BBC and Reuters pieces thatily_extract:5"
        "<|tool_call_argument_begin|>"
    )
    out = _strip_tool_call_markup(real)
    assert "<|tool" not in out
    assert "_extract" not in out


def test_strip_preserves_clean_prose() -> None:
    clean = "The Edmonds council voted unanimously. Putin warned of escalation."
    assert _strip_tool_call_markup(clean) == clean


def test_strip_keeps_full_sentences_before_leak() -> None:
    inp = (
        "Putin warned of nuclear escalation. The talks resumed in Geneva. "
        "functions.tavily_extract:5{...}"
    )
    out = _strip_tool_call_markup(inp)
    assert "_extract" not in out
    assert "functions." not in out
    assert out.endswith(".")
    assert "Putin warned" in out
    assert "Talks resumed" in out or "talks resumed" in out


def test_strip_does_not_false_positive_on_research_word() -> None:
    """The word 'research:' must not match (no underscore boundary)."""
    benign = "The research: a critical look at autonomous agents."
    assert _strip_tool_call_markup(benign) == benign


def test_strip_handles_none_and_non_string() -> None:
    assert _strip_tool_call_markup(None) is None  # type: ignore[arg-type]
    assert _strip_tool_call_markup("") == ""
    assert _strip_tool_call_markup(123) == 123  # type: ignore[arg-type]


def test_sanitise_findings_drops_only_modified_below_floor() -> None:
    """Items whose findings are NOT modified by stripping must be preserved
    even when below 20 chars — that's the existing list-shape filter's job.
    """
    spec = SectorSpec(name="local_news", shape="list", instruction="x", default=[])
    parsed = [
        {"category": "x", "source": "y", "findings": "x", "urls": ["https://a"]},
    ]
    out = _sanitise_findings_markup(parsed, spec)
    assert isinstance(out, list)
    assert len(out) == 1, "naturally-short item must not be dropped by sanitiser"


def test_sanitise_drops_when_markup_collapses_findings() -> None:
    spec = SectorSpec(name="global_news", shape="list", instruction="x", default=[])
    parsed = [
        {"source": "BBC", "findings": "Hi <|tool_call_argument_begin|>", "urls": []},
    ]
    out = _sanitise_findings_markup(parsed, spec)
    assert out == [], "markup-only findings should be dropped after strip"


def test_sanitise_strips_markup_and_keeps_clean_prefix() -> None:
    spec = SectorSpec(name="global_news", shape="list", instruction="x", default=[])
    parsed = [
        {
            "source": "BBC",
            "findings": (
                "Putin warned of nuclear escalation today. "
                "The talks resumed in Geneva. "
                "functions.tavily_extract:5{...}"
            ),
            "urls": ["https://bbc.com/news/x"],
        },
    ]
    out = _sanitise_findings_markup(parsed, spec)
    assert len(out) == 1
    cleaned = out[0]["findings"]
    assert "_extract" not in cleaned
    assert "Putin warned" in cleaned
    assert cleaned.endswith(".")


def test_sanitise_handles_deep_sector_dict() -> None:
    spec = SectorSpec(name="ai_systems", shape="deep", instruction="x", default={})
    parsed = {
        "findings": "Enoch is the first open control-plane. <|tool_call_argument_begin|>",
        "urls": ["https://github.com/enoch"],
    }
    out = _sanitise_findings_markup(parsed, spec)
    assert isinstance(out, dict)
    assert "<|tool" not in out["findings"]
    assert "Enoch" in out["findings"]


# ---------------------------------------------------------------------------
# Vault insight loader
# ---------------------------------------------------------------------------


def test_populate_vault_insight_no_path_is_noop() -> None:
    sess = {"date": "2026-05-05"}
    assert populate_vault_insight(sess, vault_path="") is False
    assert "vault_insight" not in sess


def test_populate_vault_insight_missing_path_is_noop() -> None:
    sess = {"date": "2026-05-05"}
    assert populate_vault_insight(sess, vault_path="/nope/does/not/exist") is False
    assert "vault_insight" not in sess


def test_populate_vault_insight_picks_moc_file() -> None:
    body = (
        "---\ntitle: Philosophy MOC\n---\n\n"
        "# Philosophy MOC\n\n"
        "The relational ontology programme rests on a wager: that the "
        "structure of being is irreducibly triadic, that the dyadic split "
        "between subject and object cannot exhaust experience. Migliorini's "
        "recent volume pushes this further into the trinitarian register, "
        "while Isabelle grounds it in primordial separation."
    )
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Philosophy_MOC.md").write_text(body, encoding="utf-8")
        sess: dict = {"date": "2026-05-05", "dedup": {"covered_headlines": []}}
        ok = populate_vault_insight(sess, vault_path=tmp)
        assert ok is True
        vi = sess["vault_insight"]
        assert vi["available"] is True
        assert "relational ontology" in vi["insight"]
        assert vi["context"] == "Philosophy_MOC"
        assert vi["note_path"].endswith("Philosophy_MOC.md")


def test_populate_vault_insight_dedup_aware() -> None:
    body = (
        "This is a long enough prose paragraph for the excerpt extractor "
        "to accept. It needs at least eighty characters of real prose "
        "without headings, bullets, or wikilink-only lines."
    )
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Philosophy_MOC.md").write_text(body, encoding="utf-8")
        Path(tmp, "AI_MOC.md").write_text(body, encoding="utf-8")
        sess: dict = {
            "date": "2026-05-05",
            "dedup": {"covered_headlines": ["philosophy_moc"]},
        }
        ok = populate_vault_insight(sess, vault_path=tmp)
        assert ok is True
        assert sess["vault_insight"]["context"] == "AI_MOC"


def test_populate_vault_insight_falls_back_to_full_glob() -> None:
    """When no MOC files match, fall back to **/*.md."""
    body = (
        "A sufficiently long prose paragraph that the excerpt extractor "
        "will accept as a real insight rather than a heading or bullet "
        "list. The vault has no MOC files but does have notes."
    )
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Daily_Note.md").write_text(body, encoding="utf-8")
        sess: dict = {"date": "2026-05-05"}
        ok = populate_vault_insight(sess, vault_path=tmp)
        assert ok is True
        assert sess["vault_insight"]["available"] is True


def test_populate_vault_insight_deterministic_per_date() -> None:
    """Same date + same corpus → same pick."""
    body = (
        "A long enough prose paragraph for the excerpt extractor to accept. "
        "Eighty plus characters of clean text with no headings or bullets."
    )
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("A_MOC.md", "B_MOC.md", "C_MOC.md"):
            Path(tmp, name).write_text(body, encoding="utf-8")
        first: dict = {"date": "2026-05-05"}
        second: dict = {"date": "2026-05-05"}
        populate_vault_insight(first, vault_path=tmp)
        populate_vault_insight(second, vault_path=tmp)
        assert first["vault_insight"]["context"] == second["vault_insight"]["context"]


# ---------------------------------------------------------------------------
# Prompt-string contracts — these break loudly if a future edit weakens the
# rules without intending to.
# ---------------------------------------------------------------------------


def test_part5_has_empty_feed_rule() -> None:
    assert "EMPTY FEED RULE" in PART5_INSTRUCTIONS
    assert "intellectual journals are quiet this morning" in PART5_INSTRUCTIONS


def test_part6_has_repeat_detection_hard_rule() -> None:
    assert "REPEAT-DETECTION HARD RULE" in PART6_INSTRUCTIONS
    assert "If EVERY paper is COVERED" in PART6_INSTRUCTIONS
    # AI systems sub-section has its own copy.
    assert "If EVERY item is COVERED" in PART6_INSTRUCTIONS


def test_part7_wearable_has_repeat_detection() -> None:
    # PART7 wearable defers to PART6's triadic/AI rule by reference.
    # Use a regex tolerant of the soft-wrapped "HARD\nRULE" newline.
    assert re.search(r"Same REPEAT-DETECTION HARD\s+RULE", PART7_INSTRUCTIONS)
    assert "the wearable AI market is witnessing a surge" in PART7_INSTRUCTIONS
    assert "If EVERY item in a subcategory is COVERED" in PART7_INSTRUCTIONS


def test_continuation_rules_ban_bare_domains() -> None:
    assert "NO BARE DOMAINS IN PROSE OR ANCHOR TEXT" in CONTINUATION_RULES
    assert "myedmondsnews.com" in CONTINUATION_RULES
    assert "My Edmonds News" in CONTINUATION_RULES

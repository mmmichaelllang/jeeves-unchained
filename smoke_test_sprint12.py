"""Sprint 12 smoke test — feed the broken 2026-05-01 briefing through the new
post-processing chain and verify each acceptance criterion.

This uses the actual broken HTML as input (truncated TOTT, missing banner,
orphan structure, wrong signoff, standalone-template asides) and walks it
through the deterministic post-processors. OpenRouter / Groq / NIM are NOT
called — just the deterministic guardrails.

Run: python3 smoke_test_sprint12.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

# Inject jeeves package on path
sys.path.insert(0, str(Path(__file__).parent))

from jeeves.schema import SessionModel
from jeeves.write import (
    _BANNER_HTML,
    _BANNER_URL,
    _build_source_url_map,
    _compute_link_density,
    _inject_banner,
    _inject_newyorker_verbatim,
    _inject_source_links,
    _merge_orphan_asides,
    _repair_container_structure,
    _validate_aside_placement,
    _validate_html_structure,
    postprocess_html,
)


REPO = Path(__file__).parent
BROKEN = REPO / "sessions" / "briefing-2026-05-01.html"
SESSION = REPO / "sessions" / "session-2026-05-01.json"


def _color(s, c):
    codes = {"green": "32", "red": "31", "yellow": "33", "cyan": "36", "bold": "1"}
    return f"\033[{codes[c]}m{s}\033[0m"


def main() -> int:
    print(_color("=" * 70, "cyan"))
    print(_color("Sprint 12 — End-to-end smoke test (no API calls)", "bold"))
    print(_color("=" * 70, "cyan"))

    if not BROKEN.exists() or not SESSION.exists():
        print(_color(f"missing fixture: {BROKEN} or {SESSION}", "red"))
        return 1

    raw_html = BROKEN.read_text(encoding="utf-8")
    session_data = json.loads(SESSION.read_text(encoding="utf-8"))
    session = SessionModel.model_validate(session_data)

    print(f"\nINPUT:  {len(raw_html):,} chars, "
          f"{raw_html.count('<p')} <p> tags, "
          f"{raw_html.count('<a href=')} <a> tags")
    print(f"        TOTT len in session JSON: {len(session.newyorker.text):,} "
          f"(truncated: {'[TRUNCATED]' in session.newyorker.text})")

    # Run the deterministic post-processing chain
    html = _inject_banner(raw_html)
    # Re-build TOTT from session - this requires un-truncated text. The on-disk
    # session JSON is already truncated, so we can only test the injection path
    # against the existing block.
    source_map = _build_source_url_map(session)
    html = _inject_source_links(html, source_map)
    html = _inject_banner(html)  # idempotent re-run
    html = _repair_container_structure(html)
    html = _merge_orphan_asides(html)

    # Final postprocess: signoff fix + coverage log + diagnostics.
    result = postprocess_html(html, session)
    final = result.html

    # Save the smoke output for human inspection
    smoke_out = REPO / "sessions" / "briefing-2026-05-01.smoke.html"
    smoke_out.write_text(final, encoding="utf-8")
    print(f"\nOUTPUT: {len(final):,} chars, "
          f"{final.count('<p')} <p> tags, "
          f"{final.count('<a href=')} <a> tags")
    print(f"        Link density: {result.link_density} per 1k words")
    print(f"        Word count:   {result.word_count}")
    print(f"        Saved to:     {smoke_out}")

    # Acceptance criteria
    print(_color("\n" + "=" * 70, "cyan"))
    print(_color("ACCEPTANCE CRITERIA", "bold"))
    print(_color("=" * 70, "cyan"))

    checks = [
        ("Banner img present",
         _BANNER_URL in final),
        ("Banner inside .container",
         '<div class="container">' in final and final.index(_BANNER_URL) > final.index('<div class="container">')),
        ("Banner before mh-date div in body",
         _BANNER_URL in final and '<div class="mh-date"' in final
         and final.index(_BANNER_URL) < final.index('<div class="mh-date"')),
        ("Signoff: 'Your reluctantly faithful Butler' present",
         "Your reluctantly faithful Butler" in final),
        ("Signoff: NO 'Your faithfully Butler' typo",
         "Your faithfully Butler" not in final),
        ("Signoff: NO 'Yours faithfully'",
         "Yours faithfully" not in final),
        ("Coverage log present",
         "<!-- COVERAGE_LOG:" in final),
        ("Coverage log is exactly one",
         final.count("<!-- COVERAGE_LOG:") == 1),
        ("No <p> outside .container",
         not any("outside .container" in e for e in result.structure_errors)),
        ("Aside placement violations: 0",
         len(result.aside_placement_violations) == 0),
        ("Structure errors: 0",
         len(result.structure_errors) == 0),
        ("Anchors present (≥ 5)",
         final.count("<a href=") >= 5),
    ]

    n_pass = 0
    for label, ok in checks:
        if ok:
            print(f"  {_color('✓', 'green')} {label}")
            n_pass += 1
        else:
            print(f"  {_color('✗', 'red')} {label}")

    if result.aside_placement_violations:
        print(_color("\nAside violations remaining:", "yellow"))
        for w in result.aside_placement_violations:
            print(f"  - {w}")

    if result.structure_errors:
        print(_color("\nStructure errors remaining:", "yellow"))
        for e in result.structure_errors:
            print(f"  - {e}")

    print(_color("\n" + "=" * 70, "cyan"))
    print(_color(f"RESULT: {n_pass}/{len(checks)} passed", "bold"))
    print(_color("=" * 70, "cyan"))

    return 0 if n_pass == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

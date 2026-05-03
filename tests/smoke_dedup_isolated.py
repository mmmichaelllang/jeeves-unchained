#!/usr/bin/env python3
"""Standalone smoke test for Phase-1/2/3 helper functions.

Bypasses pydantic by extracting the pure-Python helpers from write.py
via direct AST import. Run with `python3 tests/smoke_dedup_isolated.py`.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WRITE_PY = REPO / "jeeves" / "write.py"

# Read write.py source.
src = WRITE_PY.read_text()
tree = ast.parse(src)

# Extract function definitions we want to smoke-test.
WANTED = {
    "_strip_part_zero_premature_close",
    "_strip_misplaced_signoff_and_coverage",
    "_enforce_single_close_tag",
    "_strip_continuation_wrapper",
    "_strip_fences",
    "_stitch_parts",
    "_collapse_adjacent_duplicate_h3",
    "_dedup_paragraphs_across_blocks",
    "_build_newyorker_block",
    "_validate_part_fragment",
    "_strip_tags",
    "_compute_link_density",
    "_validate_aside_placement",
    "_paragraph_is_aside_orphan",
    "_editor_quality_gates",
}

# Extract module-level constants needed.
WANTED_CONSTS = {
    "_NY_BLOCK_FENCE_RE",
    "_P_TAG_RE",
    "_H3_TAG_RE",
    "_EDITOR_WORD_FLOOR_RATIO",
    "_EDITOR_WORD_CEILING_RATIO",
    "_EDITOR_MIN_ANCHORS_PER_1K",
    "_EDITOR_MAX_ASIDE_ORPHANS",
    "_NY_READ_LINK_RE",
}

namespace: dict = {"re": re, "log": type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None, "debug": lambda *a, **k: None, "error": lambda *a, **k: None})()}

# First pass: extract constants.
for node in tree.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in WANTED_CONSTS:
                code = ast.unparse(node)
                exec(code, namespace)

# Second pass: extract functions.
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANTED:
        code = ast.unparse(node)
        try:
            exec(code, namespace)
        except Exception as exc:
            print(f"  SKIP fn {node.name}: {exc}")

# Pull functions into local scope.
g = namespace
for name in WANTED:
    if name not in g:
        print(f"  WARN: {name} not extracted")

# -----------------------------------------------------------------------------
# Tests.
# -----------------------------------------------------------------------------
results: list[tuple[str, bool, str]] = []


def t(name, ok, msg=""):
    results.append((name, ok, msg))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {msg}")


# B1/B2 — _stitch_parts strips Part 1 premature close tags.
part1 = (
    "<!DOCTYPE html><html><head></head><body>"
    '<div class="container">'
    "<h3>The Domestic Sphere</h3><p>part 1 content</p>"
    '<p class="signoff">stale signoff</p>'
    "</div>"
    "</body></html>"
)
part2 = "<h3>Beyond the Geofence</h3><p>part 2 content</p>"
out = g["_stitch_parts"](part1, part2)
t(
    "stitch strips part0 premature close, single body/html",
    out.lower().count("</body>") == 1 and out.lower().count("</html>") == 1,
    f"body={out.lower().count('</body>')} html={out.lower().count('</html>')}",
)
t("stitch strips stale signoff from part 1", "stale signoff" not in out)
t("stitch keeps part 1 + part 2 content", "part 1 content" in out and "part 2 content" in out)

# B5 — Build newyorker block has no Read link.
block = g["_build_newyorker_block"]("para 1\n\npara 2", "https://newyorker.com/a")
t("build_newyorker_block no Read link", "Read at The New Yorker" not in block)
t("build_newyorker_block has START/END sentinels",
  "<!-- NEWYORKER_START -->" in block and "<!-- NEWYORKER_END -->" in block)

# B6 — Adjacent dup h3 collapses.
adjacent = "<h3>The Specific Enquiries</h3>\n  \n<h3>The Specific Enquiries</h3>"
out = g["_collapse_adjacent_duplicate_h3"](adjacent)
t("collapse adjacent dup h3", out.lower().count("<h3>") == 1)

non_adjacent = (
    "<h3>Section A</h3><p>real content here long enough</p>"
    "<h3>Section A</h3><p>more content</p>"
)
out = g["_collapse_adjacent_duplicate_h3"](non_adjacent)
t("non-adjacent dup h3 NOT collapsed", out.lower().count("<h3>") == 2)

# B10 — Cross-block paragraph dedup.
html = (
    "<p>The Edmonds City Council has approved key contracts and adopted the safety plan.</p>"
    "<p>Some other distinct paragraph about entirely different subject matter.</p>"
    "<p>The Edmonds City Council has approved key contracts and adopted the safety plan.</p>"
    "<p>Yet another distinct paragraph that should remain in place here too.</p>"
)
out = g["_dedup_paragraphs_across_blocks"](html)
t(
    "paragraph dedup keeps one Edmonds copy",
    out.count("Edmonds City Council has approved key contracts") == 1,
)
t("paragraph dedup keeps distinct ones", "entirely different subject matter" in out and "should remain in place" in out)

# Short paragraphs preserved.
html2 = "<p>and</p><p>and</p><p>and</p>"
out2 = g["_dedup_paragraphs_across_blocks"](html2)
t("short paragraphs preserved", out2.count("<p>and</p>") == 3)

# NY block protected.
html3 = (
    "<p>Some long paragraph about the Edmonds Council that repeats a few times in this fragment.</p>"
    "<!-- NEWYORKER_START -->"
    "<p>Some long paragraph about the Edmonds Council that repeats a few times in this fragment.</p>"
    "<!-- NEWYORKER_END -->"
)
out3 = g["_dedup_paragraphs_across_blocks"](html3)
t("NY block paragraphs protected", out3.count("Edmonds Council that repeats") == 2)

# B4 — Editor word ceiling rejects bloated.
input_html = "<html><body>" + ("<p>some real prose here please. </p>" * 50) + "</body></html>"
bloated = "<html><body>" + ("<p>some real prose here please. </p>" * 110) + "</body></html>"
passed, reason = g["_editor_quality_gates"](input_html, bloated, "test-model")
t("editor rejects bloated 2x output", not passed and "ceiling" in reason)

# Pre-stitch validator.
raw = "<!DOCTYPE html><html><body><p>p1</p></body></html>"
_, warnings = g["_validate_part_fragment"](0, "part1", raw, total_parts=9)
t(
    "validate part0 flags premature html close",
    any("part0_premature_html_close" in w for w in warnings),
    str(warnings),
)

raw_mid = "<!DOCTYPE html><html><body><p>p4</p>"
_, warnings = g["_validate_part_fragment"](3, "part4", raw_mid, total_parts=9)
t(
    "validate middle part flags doctype leak",
    any("middle_part_doctype_leak" in w for w in warnings),
    str(warnings),
)

clean_p0 = '<!DOCTYPE html><html><head></head><body><div class="container"><p>p1</p>'
_, warnings = g["_validate_part_fragment"](0, "part1", clean_p0, total_parts=9)
t("validate clean part0 has no warnings", not warnings, str(warnings))

# Enforce single close tag.
html_multi = "<p>a</p></body><p>b</p></body><p>c</p></body>"
out_, n = g["_enforce_single_close_tag"](html_multi, "</body>")
t("enforce single close keeps last", n == 2 and out_.lower().count("</body>") == 1 and out_.endswith("</body>"))

# -----------------------------------------------------------------------------
print("\n" + "=" * 60)
passed_n = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"  {passed_n}/{total} smoke tests passed")
sys.exit(0 if passed_n == total else 1)

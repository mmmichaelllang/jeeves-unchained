"""Vault → Library Stacks insight loader.

Sprint-19 — implements the long-missing data path behind PART 8
(``vault_insight``). Earlier sprints defined the schema, the prompt rules,
and the empty-paragraph fallback, but no code ever populated
``session["vault_insight"]`` so the section shipped blank every day.

This module reads a local Obsidian-style vault (the user's "WIKI" project)
and selects one Map-of-Content (MOC) note per day to feature. Output is a
plain-text excerpt that PART 8 then dresses in Jeeves's voice.

Activation
----------
Set ``JEEVES_VAULT_PATH`` to the vault root. Optional ``JEEVES_VAULT_GLOB``
overrides the glob pattern (default: prefer files whose stem contains
"MOC", with full ``**/*.md`` fallback when no MOC matches exist).
When ``JEEVES_VAULT_PATH`` is empty or missing, this loader is a no-op
and PART 8 prints its empty-paragraph fallback — preserving the
pre-sprint behaviour for environments without a vault.

Selection
---------
- Filter out notes whose stem appears in ``dedup.covered_headlines``
  (case-insensitive substring) so the same MOC isn't featured two days
  in a row.
- Deterministic-per-date RNG: same date → same pick (idempotent retries
  in CI), different date → different pick (rotation across the corpus).

Excerpt
-------
- Strip YAML frontmatter.
- Drop heading lines, bullet/list lines, and lines that are only
  wikilinks (``[[…]]``) or images.
- Take the first prose paragraph ≥ 80 chars; cap at 900 chars to leave
  PART 8 room to wrap context around it.
- Soft-fail: if no paragraph qualifies, return without setting
  ``vault_insight`` so PART 8 prints the empty placeholder.
"""

from __future__ import annotations

import logging
import os
import random
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_WIKILINK_LINE_RE = re.compile(r"^\s*(?:!?\[\[[^\]]+\]\]\s*)+\s*$")
_HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_DEFAULT_GLOB_PRIMARY = "**/*MOC*.md"
_DEFAULT_GLOB_FALLBACK = "**/*.md"
_MAX_INSIGHT_CHARS = 900
_MIN_INSIGHT_CHARS = 80


def populate_vault_insight(
    session: dict[str, Any],
    *,
    vault_path: str | None = None,
    glob_pattern: str | None = None,
) -> bool:
    """Populate ``session["vault_insight"]`` from a local vault.

    Parameters
    ----------
    session:
        Mutable session dict. Modified in place when an insight is found.
    vault_path:
        Override for the ``JEEVES_VAULT_PATH`` env var (mostly for tests).
    glob_pattern:
        Override for ``JEEVES_VAULT_GLOB``.

    Returns
    -------
    bool
        ``True`` when an insight was written; ``False`` on every soft-fail
        path (no path configured, path missing, no candidate files, no
        prose paragraph above the minimum length, exceptions reading a
        candidate file). Soft-fail is the design — PART 8 has its own
        empty-paragraph branch.
    """
    if not isinstance(session, dict):
        return False

    raw_path = (vault_path if vault_path is not None
                else os.environ.get("JEEVES_VAULT_PATH", ""))
    raw_path = (raw_path or "").strip()
    if not raw_path:
        log.debug("vault: JEEVES_VAULT_PATH not set; skipping")
        return False

    root = Path(raw_path).expanduser()
    if not root.exists() or not root.is_dir():
        log.warning("vault: path not found or not a directory: %s", root)
        return False

    primary_glob = (glob_pattern if glob_pattern is not None
                    else os.environ.get("JEEVES_VAULT_GLOB", "")).strip()
    if not primary_glob:
        primary_glob = _DEFAULT_GLOB_PRIMARY

    candidates = sorted(root.glob(primary_glob))
    if not candidates and primary_glob != _DEFAULT_GLOB_FALLBACK:
        candidates = sorted(root.glob(_DEFAULT_GLOB_FALLBACK))
    if not candidates:
        log.info(
            "vault: no candidate notes found in %s (glob=%s)",
            root, primary_glob,
        )
        return False

    covered = _covered_stems_lower(session)
    pool = [p for p in candidates if p.stem.lower() not in covered]
    if not pool:
        # All candidate stems already covered — fall back to the full pool
        # rather than ship an empty section. The note will repeat eventually.
        pool = candidates

    seed = str(session.get("date") or "vault-default")
    rng = random.Random(seed)
    rng.shuffle(pool)

    for chosen in pool:
        try:
            text = chosen.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.debug("vault: read failed for %s: %s", chosen, exc)
            continue
        excerpt = _extract_excerpt(text)
        if not excerpt:
            continue

        try:
            rel_path = str(chosen.relative_to(root))
        except ValueError:
            rel_path = chosen.name

        session["vault_insight"] = {
            "available": True,
            "insight": excerpt,
            "context": chosen.stem,
            "note_path": rel_path,
        }
        log.info("vault: featured %s (%d chars)", rel_path, len(excerpt))
        return True

    log.info("vault: no candidate note yielded a usable prose excerpt")
    return False


def _covered_stems_lower(session: dict[str, Any]) -> set[str]:
    dedup = session.get("dedup")
    if not isinstance(dedup, dict):
        return set()
    headlines = dedup.get("covered_headlines") or []
    if not isinstance(headlines, list):
        return set()
    out: set[str] = set()
    for h in headlines:
        if isinstance(h, str) and h.strip():
            out.add(h.strip().lower())
    return out


def _extract_excerpt(text: str) -> str:
    """Pull the first qualifying prose paragraph from raw markdown."""
    if not text:
        return ""

    # Strip YAML frontmatter.
    text = _FRONTMATTER_RE.sub("", text, count=1)
    # Strip HTML comments — Obsidian templater leftovers, etc.
    text = _HTML_COMMENT_RE.sub("", text)

    paragraphs = _split_paragraphs(text)
    for para in paragraphs:
        cleaned = _clean_paragraph(para)
        if len(cleaned) >= _MIN_INSIGHT_CHARS:
            return cleaned[:_MAX_INSIGHT_CHARS].strip()
    return ""


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _clean_paragraph(para: str) -> str:
    """Reject heading/bullet/wikilink-only paragraphs; lightly normalise prose."""
    lines = para.splitlines()
    keep: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _HEADING_LINE_RE.match(stripped):
            return ""
        if _BULLET_LINE_RE.match(stripped):
            return ""
        if _WIKILINK_LINE_RE.match(stripped):
            return ""
        keep.append(stripped)
    if not keep:
        return ""

    joined = " ".join(keep)
    # Strip Obsidian wikilink syntax to plain text — keep the readable side.
    joined = re.sub(r"!\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", "", joined)  # transclusions
    joined = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", joined)   # alias links
    joined = re.sub(r"\[\[([^\]]+)\]\]", r"\1", joined)              # plain links
    # Collapse markdown image syntax.
    joined = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", joined)
    # Convert markdown links to "text (url)" → just "text".
    joined = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", joined)
    # Drop residual markdown emphasis markers.
    joined = re.sub(r"[*_`]{1,3}([^*_`]+)[*_`]{1,3}", r"\1", joined)
    # Normalise whitespace.
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined

"""Site skills — durable, per-{site, task} markdown notes that capture the
shortest reliable path for the research agent to extract data from a
recurring source.

Pattern adapted from Browserbase's Autobrowse (https://agent.tinyfish.ai/) and
Karpathy's autoresearch harness. Each skill is a single markdown file with
YAML frontmatter + prose body. The loader parses the frontmatter and exposes
skills relevant to a given sector / host so the research agents stop paying
re-discovery tax on every run.

Why this matters for jeeves:
  - triadic_ontology and ai_systems agents rediscover arxiv search idioms
    and the over-shipped DOVA / Karl-Alber / Migliorini set every day.
  - local_news re-derives the myedmondsnews.com selector path nightly.
  - newyorker.com Talk-of-the-Town is hand-coded but no skill records the
    discovery — when the page structure changes, the breakage is silent.

A skill captures: the canonical fetch path, gotchas, decode tables, and a
skip-list of items already over-shipped. Seed skills live in this package's
``registry/`` directory; runtime-graduated skills can land alongside them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

REGISTRY_DIR = Path(__file__).resolve().parent / "registry"


@dataclass(frozen=True)
class SiteSkill:
    """One {site, task} skill loaded from a markdown file.

    The body is intentionally pre-formatted markdown — agents consume it as
    is. Trying to parse the body into structured fields would defeat the
    point of the Autobrowse pattern (the body is meant to be human- AND
    agent-readable as prose, not split into a million attributes).
    """

    name: str                 # filename slug (no extension)
    title: str                # display title for the skill
    description: str          # one-sentence purpose
    sectors: tuple[str, ...]  # which research sectors this skill applies to
    hosts: tuple[str, ...]    # which hosts it applies to (or () for any)
    body: str                 # full markdown body (excluding frontmatter)
    path: Path = field(default=Path())
    status: str = ""          # e.g. "seed-2026-05-09" or "graduated-run-007"


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z",
    re.DOTALL,
)


def _parse_skill_file(path: Path) -> SiteSkill | None:
    """Parse a single .md skill file into a SiteSkill.

    Skills lacking the required YAML frontmatter (name, title, description,
    sectors) are logged and skipped — never raised. The loader is a
    best-effort enrichment of the prompt; one bad skill MUST NOT break the
    pipeline.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("site_skills: could not read %s: %s", path, exc)
        return None

    m = _FRONTMATTER_RE.match(raw)
    if not m:
        log.warning("site_skills: %s missing YAML frontmatter; skipping", path.name)
        return None

    fm_text, body = m.group(1), m.group(2)
    fm = _parse_frontmatter(fm_text)
    name = fm.get("name") or path.stem
    title = fm.get("title")
    description = fm.get("description")
    if not (title and description):
        log.warning(
            "site_skills: %s missing title/description; skipping", path.name
        )
        return None

    sectors = _split_list(fm.get("sectors", ""))
    hosts = _split_list(fm.get("hosts", ""))

    return SiteSkill(
        name=name,
        title=title,
        description=description,
        sectors=tuple(sectors),
        hosts=tuple(hosts),
        body=body.strip(),
        path=path,
        status=fm.get("status", ""),
    )


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Tiny, dependency-free YAML-ish parser.

    Handles only the shapes our skills use: ``key: value`` and
    ``key: [a, b, c]``. We deliberately avoid a YAML dep — the registry is
    machine-written by graduation runs as well as by hand, and a flat parser
    keeps the contract obvious.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _split_list(value: str) -> list[str]:
    """Parse a frontmatter value that may be ``[a, b]`` or ``a, b`` or ``a``."""
    if not value:
        return []
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [
        item.strip().strip('"').strip("'")
        for item in value.split(",")
        if item.strip()
    ]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_all_skills() -> tuple[SiteSkill, ...]:
    """Read every .md in REGISTRY_DIR. Cached for the process lifetime.

    Tests that mutate the directory must call ``_load_all_skills.cache_clear()``.
    """
    if not REGISTRY_DIR.is_dir():
        return ()
    skills: list[SiteSkill] = []
    for path in sorted(REGISTRY_DIR.glob("*.md")):
        skill = _parse_skill_file(path)
        if skill is not None:
            skills.append(skill)
    log.info("site_skills: loaded %d skills from %s", len(skills), REGISTRY_DIR)
    return tuple(skills)


def all_skills() -> tuple[SiteSkill, ...]:
    """Return every loaded skill (cached)."""
    return _load_all_skills()


def skills_for_sector(sector_name: str) -> list[SiteSkill]:
    """Return skills whose ``sectors`` frontmatter contains the given sector.

    A skill with empty sectors matches every sector — useful for cross-cutting
    skills (e.g. "all arxiv URLs are sticky" rules).
    """
    return [
        s for s in _load_all_skills()
        if not s.sectors or sector_name in s.sectors
    ]


def skills_for_hosts(hosts: list[str]) -> list[SiteSkill]:
    """Return skills whose ``hosts`` frontmatter intersects with given hosts.

    Used when the agent has already ranked a candidate URL set and wants the
    relevant skills for those specific hosts.
    """
    host_set = {h.lower().lstrip("www.") for h in hosts if h}
    return [
        s for s in _load_all_skills()
        if any(h.lower().lstrip("www.") in host_set for h in s.hosts)
    ]


def render_skills_block(skills: list[SiteSkill], *, max_chars: int = 6000) -> str:
    """Render skills into a single agent-context-ready markdown block.

    Caller-controllable max_chars caps how much we'll splice into a system
    prompt — agents on free NIM tiers cannot afford 30K-char skill dumps. We
    truncate from the END (least-relevant skill last after sorting), not the
    middle of any single skill, so each included skill stays whole.
    """
    if not skills:
        return ""
    parts: list[str] = ["## Site skills (durable, agent-readable)"]
    parts.append(
        "Each block below is a previously-graduated workflow for a recurring "
        "site/task. Read the relevant block FIRST when you encounter that host "
        "— it captures gotchas, undocumented endpoints, and items already "
        "over-shipped. Do NOT re-derive what a skill already records."
    )
    out = "\n\n".join(parts) + "\n\n"
    for skill in skills:
        chunk = (
            f"### Skill: {skill.title}\n"
            f"_Applies to_: {', '.join(skill.hosts) or 'any'}\n\n"
            f"{skill.body}\n"
        )
        if len(out) + len(chunk) > max_chars:
            log.info(
                "site_skills: truncating skills block at %d/%d chars (skipping %d remaining)",
                len(out), max_chars, len(skills) - parts.count(chunk),
            )
            break
        out += chunk + "\n---\n\n"
    return out.rstrip()


__all__ = [
    "REGISTRY_DIR",
    "SiteSkill",
    "all_skills",
    "render_skills_block",
    "skills_for_hosts",
    "skills_for_sector",
]

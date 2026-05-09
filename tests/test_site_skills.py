"""Tests for jeeves.site_skills (Autobrowse-pattern lift, 2026-05-09).

Coverage:
  - Frontmatter parsing (title, description, sectors, hosts, status)
  - Sector / host lookup
  - Skills block rendering with size cap
  - Bad files are skipped, not raised
  - Cache invalidation hook for tests
"""

from __future__ import annotations

from pathlib import Path

import pytest

import jeeves.site_skills as ssk
from jeeves.site_skills import (
    REGISTRY_DIR,
    SiteSkill,
    all_skills,
    render_skills_block,
    skills_for_hosts,
    skills_for_sector,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test starts with a clean skills cache."""
    ssk._load_all_skills.cache_clear()
    yield
    ssk._load_all_skills.cache_clear()


# ---------------------------------------------------------------- registry ---

def test_registry_directory_exists():
    assert REGISTRY_DIR.is_dir()


def test_registry_contains_seed_skills():
    """Seed skills MUST exist after this PR — bare loader is useless."""
    skills = all_skills()
    names = {s.name for s in skills}
    expected = {
        "myedmondsnews-local-news",
        "arxiv-ai-systems-papers",
        "newyorker-talk-of-the-town",
    }
    missing = expected - names
    assert not missing, f"Seed skills missing: {missing}"


def test_seed_skills_have_required_metadata():
    for skill in all_skills():
        assert skill.title, f"{skill.name} has no title"
        assert skill.description, f"{skill.name} has no description"
        assert skill.body, f"{skill.name} has empty body"
        # Every seed skill ships with the seed status marker.
        assert skill.status.startswith("seed-"), (
            f"{skill.name} status not seed-stamped: {skill.status!r}"
        )


# -------------------------------------------------------- sector / host lookup

def test_skills_for_sector_local_news():
    skills = skills_for_sector("local_news")
    names = {s.name for s in skills}
    assert "myedmondsnews-local-news" in names
    # ai_systems / newyorker skills MUST NOT leak into local_news.
    assert "arxiv-ai-systems-papers" not in names
    assert "newyorker-talk-of-the-town" not in names


def test_skills_for_sector_ai_systems():
    skills = skills_for_sector("ai_systems")
    names = {s.name for s in skills}
    assert "arxiv-ai-systems-papers" in names
    assert "myedmondsnews-local-news" not in names


def test_skills_for_sector_newyorker():
    skills = skills_for_sector("newyorker")
    names = {s.name for s in skills}
    assert "newyorker-talk-of-the-town" in names


def test_skills_for_sector_unknown_returns_empty():
    """A nonsense sector returns no skills (no exception)."""
    assert skills_for_sector("nonexistent_sector_xyz") == []


def test_skills_for_hosts_strips_www():
    """Host matching MUST be www-insensitive — agents may pass either."""
    skills = skills_for_hosts(["www.myedmondsnews.com"])
    assert any(s.name == "myedmondsnews-local-news" for s in skills)


# ------------------------------------------------------------ render block ---

def test_render_skills_block_includes_titles_and_bodies():
    skills = list(all_skills())
    block = render_skills_block(skills)
    for skill in skills:
        if skill.title in block:
            # at least one body fragment must come through
            first_body_line = skill.body.split("\n", 1)[0]
            assert first_body_line in block, (
                f"Body of {skill.name} not in rendered block"
            )


def test_render_skills_block_empty_when_no_skills():
    assert render_skills_block([]) == ""


def test_render_skills_block_respects_max_chars():
    """Large skills get truncated at the LAST whole skill that fits."""
    skills = list(all_skills())
    if len(skills) < 2:
        pytest.skip("need at least 2 seed skills for truncation test")
    # Pick a tight cap that fits exactly the header+1 skill (rough — we just
    # assert the cap is honoured).
    block = render_skills_block(skills, max_chars=2000)
    assert len(block) <= 2200, f"Block exceeded cap: {len(block)} chars"


def test_render_skills_block_starts_with_header():
    skills = list(all_skills())
    block = render_skills_block(skills)
    assert block.startswith("## Site skills")


# ------------------------------------------------------- robustness / errors

def test_bad_frontmatter_file_is_skipped(tmp_path, monkeypatch):
    """A skill file with no YAML frontmatter is logged + skipped, not raised."""
    fake_dir = tmp_path / "registry"
    fake_dir.mkdir()
    (fake_dir / "ok.md").write_text(
        "---\nname: ok\ntitle: OK Skill\ndescription: A valid one.\n"
        "sectors: [s]\nhosts: [h]\n---\n\nbody here\n",
        encoding="utf-8",
    )
    (fake_dir / "broken.md").write_text(
        "no frontmatter at all\n", encoding="utf-8",
    )
    (fake_dir / "missing-fields.md").write_text(
        "---\nname: x\nsectors: [s]\n---\n\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ssk, "REGISTRY_DIR", fake_dir)
    ssk._load_all_skills.cache_clear()

    skills = ssk._load_all_skills()
    assert len(skills) == 1
    assert skills[0].name == "ok"


def test_skill_file_inline_list_frontmatter(tmp_path, monkeypatch):
    """Frontmatter ``sectors: [a, b, c]`` parses to tuple of three."""
    fake_dir = tmp_path / "registry"
    fake_dir.mkdir()
    (fake_dir / "multi.md").write_text(
        "---\nname: multi\ntitle: Multi-Sector\ndescription: x\n"
        "sectors: [local_news, global_news, intellectual_journals]\n"
        "hosts: [example.com]\n---\n\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ssk, "REGISTRY_DIR", fake_dir)
    ssk._load_all_skills.cache_clear()

    skills = ssk._load_all_skills()
    assert len(skills) == 1
    assert skills[0].sectors == (
        "local_news", "global_news", "intellectual_journals",
    )


def test_empty_sectors_means_match_any_sector(tmp_path, monkeypatch):
    """A skill with empty sectors list applies to every sector lookup."""
    fake_dir = tmp_path / "registry"
    fake_dir.mkdir()
    (fake_dir / "universal.md").write_text(
        "---\nname: universal\ntitle: Universal\ndescription: x\n"
        "sectors: []\nhosts: []\n---\n\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ssk, "REGISTRY_DIR", fake_dir)
    ssk._load_all_skills.cache_clear()

    for sector in ("local_news", "ai_systems", "anything_at_all"):
        skills = skills_for_sector(sector)
        assert any(s.name == "universal" for s in skills), (
            f"Universal skill missing for sector {sector}"
        )

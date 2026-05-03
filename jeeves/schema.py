"""Pydantic models for the session JSON contract.

Mirrors the schema produced by jeeves-memory's cloud-research-prompt.md so the
Phase 3 write script can read files from either pipeline. Field caps match
the jeeves-memory truncation table.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Dedup(BaseModel):
    model_config = ConfigDict(extra="allow")
    covered_urls: list[str] = Field(default_factory=list)
    covered_headlines: list[str] = Field(default_factory=list)
    # URLs that surfaced in 2+ research sectors (e.g. a ProPublica piece in
    # both global_news and enriched_articles). Write phase uses these to
    # synthesise once rather than narrate the same story across 3 sections.
    cross_sector_dupes: list[str] = Field(default_factory=list)


class Correspondence(BaseModel):
    model_config = ConfigDict(extra="allow")
    found: bool = False
    fallback_used: bool = False
    text: str = ""


class Finding(BaseModel):
    """Generic shape used across local_news / global_news / intellectual_journals / wearable_ai."""

    model_config = ConfigDict(extra="allow")
    category: str | None = None
    source: str | None = None
    findings: str = ""
    urls: list[str] = Field(default_factory=list)


class DeepResearch(BaseModel):
    model_config = ConfigDict(extra="allow")
    findings: str = ""
    urls: list[str] = Field(default_factory=list)

    @field_validator("findings", mode="before")
    @classmethod
    def coerce_findings_to_str(cls, v: object) -> str:
        if isinstance(v, list):
            return " ".join(str(item) for item in v if item)
        return v  # type: ignore[return-value]


class NewYorker(BaseModel):
    model_config = ConfigDict(extra="allow")
    available: bool = False
    title: str = ""
    section: str = ""
    dek: str = ""
    byline: str = ""
    date: str = ""
    text: str = ""
    url: str = ""
    source: str = "The New Yorker"


class VaultInsight(BaseModel):
    model_config = ConfigDict(extra="allow")
    available: bool = False
    insight: str = ""
    context: str = ""
    note_path: str = ""


class LiteraryPick(BaseModel):
    """A book from the last 20 years considered a current or future literary classic."""

    model_config = ConfigDict(extra="allow")
    available: bool = False
    title: str = ""
    author: str = ""
    year: int | None = None
    summary: str = ""
    url: str = ""


class EnrichedArticle(BaseModel):
    model_config = ConfigDict(extra="allow")
    url: str
    source: str = ""
    title: str = ""
    fetch_failed: bool = False
    text: str = ""


class CorrespondenceHandoff(BaseModel):
    """Shape of sessions/correspondence-<date>.json produced by Phase 4."""

    model_config = ConfigDict(extra="allow")
    found: bool = False
    fallback_used: bool = False
    text: str = ""


class SessionModel(BaseModel):
    """Daily research session, consumed by the write phase."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = "1"
    date: str
    status: str = "complete"
    dedup: Dedup = Field(default_factory=Dedup)
    correspondence: Correspondence = Field(default_factory=Correspondence)
    weather: str = ""
    local_news: list[Finding] = Field(default_factory=list)
    career: dict[str, Any] = Field(default_factory=dict)
    family: dict[str, Any] = Field(default_factory=dict)
    global_news: list[Finding] = Field(default_factory=list)
    intellectual_journals: list[Finding] = Field(default_factory=list)
    wearable_ai: list[Finding] = Field(default_factory=list)
    triadic_ontology: DeepResearch = Field(default_factory=DeepResearch)
    ai_systems: DeepResearch = Field(default_factory=DeepResearch)
    uap: DeepResearch = Field(default_factory=DeepResearch)
    uap_has_new: bool = True  # False signals write phase to substitute literary_pick
    newyorker: NewYorker = Field(default_factory=NewYorker)
    vault_insight: VaultInsight = Field(default_factory=VaultInsight)
    enriched_articles: list[EnrichedArticle] = Field(default_factory=list)
    literary_pick: LiteraryPick = Field(default_factory=LiteraryPick)
    # Write-phase quality warnings (NIM refine fallbacks, timeout, etc.)
    # Populated by generate_briefing; empty list means all refine passes succeeded.
    quality_warnings: list[str] = Field(default_factory=list)


# Per-field char caps applied before serialization. Mirrors jeeves-memory.
FIELD_CAPS: dict[str, int] = {
    "weather": 800,
    "local_news.findings": 800,
    "career": 800,
    "family": 800,
    "correspondence.text": 1500,
    "global_news.findings": 600,
    "intellectual_journals.findings": 600,
    "wearable_ai.findings": 400,
    "triadic_ontology.findings": 1000,
    "ai_systems.findings": 1000,
    "uap.findings": 1000,
    "vault_insight.insight": 1000,
    # Talk of the Town pieces top out around ~9000 chars; cap at 40k as safety
    # rail against an unbounded write while preserving full text in the briefing.
    # The previous 4000 cap dated from a prior architecture where Groq ingested
    # the full session JSON; Part 9 now strips `newyorker.text` from its payload
    # entirely (see write._session_subset / generate_briefing), so this cap is
    # only protecting the on-disk session file from a runaway fetch.
    "newyorker.text": 40000,
    "enriched_articles.text": 1200,
    "literary_pick.summary": 600,
}


def _cap(text: str, limit: int) -> str:
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[:limit] + " [TRUNCATED]"


def apply_field_caps(session: dict[str, Any]) -> dict[str, Any]:
    """Apply truncation caps in place and return the same dict."""

    session["weather"] = _cap(session.get("weather", ""), FIELD_CAPS["weather"])

    for item in session.get("local_news", []) or []:
        if isinstance(item, dict) and "findings" in item:
            item["findings"] = _cap(item["findings"], FIELD_CAPS["local_news.findings"])

    for item in session.get("global_news", []) or []:
        if isinstance(item, dict) and "findings" in item:
            item["findings"] = _cap(item["findings"], FIELD_CAPS["global_news.findings"])

    for item in session.get("intellectual_journals", []) or []:
        if isinstance(item, dict) and "findings" in item:
            item["findings"] = _cap(item["findings"], FIELD_CAPS["intellectual_journals.findings"])

    for item in session.get("wearable_ai", []) or []:
        if isinstance(item, dict) and "findings" in item:
            item["findings"] = _cap(item["findings"], FIELD_CAPS["wearable_ai.findings"])

    for key, limit_key in (
        ("triadic_ontology", "triadic_ontology.findings"),
        ("ai_systems", "ai_systems.findings"),
        ("uap", "uap.findings"),
    ):
        block = session.get(key) or {}
        if isinstance(block, dict) and "findings" in block:
            block["findings"] = _cap(block["findings"], FIELD_CAPS[limit_key])

    corr = session.get("correspondence") or {}
    if isinstance(corr, dict) and "text" in corr:
        corr["text"] = _cap(corr["text"], FIELD_CAPS["correspondence.text"])

    vi = session.get("vault_insight") or {}
    if isinstance(vi, dict) and "insight" in vi:
        vi["insight"] = _cap(vi["insight"], FIELD_CAPS["vault_insight.insight"])

    ny = session.get("newyorker") or {}
    if isinstance(ny, dict) and "text" in ny:
        ny["text"] = _cap(ny["text"], FIELD_CAPS["newyorker.text"])

    for art in session.get("enriched_articles", []) or []:
        if isinstance(art, dict) and "text" in art:
            art["text"] = _cap(art["text"], FIELD_CAPS["enriched_articles.text"])

    lp = session.get("literary_pick") or {}
    if isinstance(lp, dict) and "summary" in lp:
        lp["summary"] = _cap(lp["summary"], FIELD_CAPS["literary_pick.summary"])

    for key in ("career", "family"):
        block = session.get(key) or {}
        if isinstance(block, dict):
            for k, v in list(block.items()):
                if isinstance(v, str):
                    block[k] = _cap(v, FIELD_CAPS[key])

    return session

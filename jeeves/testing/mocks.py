"""Dry-run mocks — swap in for real LLM + tool calls during tests and CI smoke runs."""

from __future__ import annotations

from datetime import date
from typing import Any

from ..schema import SessionModel
from ..tools.emit_session import ResearchContext, make_emit_session


def canned_session(run_date: date) -> dict[str, Any]:
    """A fully-populated SessionModel-shaped payload used by the dry-run agent."""

    return {
        "date": run_date.isoformat(),
        "status": "complete",
        "dedup": {
            "covered_urls": [
                "https://www.example.com/story-1",
                "https://www.example.com/story-2",
            ],
            "covered_headlines": [
                "Example breaking story",
            ],
        },
        "correspondence": {"found": False, "fallback_used": False, "text": ""},
        "weather": "Edmonds: partly cloudy, 58°F, light westerly winds. Rain arrives by evening.",
        "local_news": [
            {
                "category": "municipal",
                "source": "myedmondsnews.com",
                "findings": "City council passed the downtown parking ordinance 5-2.",
                "urls": ["https://myedmondsnews.com/council-parking"],
            }
        ],
        "career": {
            "openings": [
                {
                    "district": "Northshore SD",
                    "role": "HS English Teacher",
                    "url": "https://northshoresd.org/jobs/hs-english",
                    "summary": "English 9-12, posted April 2026, apply by May 15.",
                },
                {
                    "district": "Shoreline SD",
                    "role": "HS History Teacher",
                    "url": "https://shorelinesd.org/jobs/hs-history",
                    "summary": "World History and US History combined posting.",
                },
            ],
            "notes": "Two districts actively hiring; Northshore deadline approaching.",
        },
        "family": {
            "choir": "Seattle Symphony Chorale open auditions May 3.",
            "toddler": "Lynnwood library: Baby Storytime Thursdays 10:30am.",
            "urls": [],
        },
        "global_news": [
            {
                "category": "politics",
                "source": "BBC",
                "findings": "Report on ongoing negotiations.",
                "urls": ["https://www.bbc.com/news/mock"],
            }
        ],
        "intellectual_journals": [
            {
                "category": "philosophy",
                "source": "NYRB",
                "findings": "Essay on contemporary metaphysics.",
                "urls": ["https://www.nybooks.com/mock"],
            }
        ],
        "wearable_ai": [
            {
                "category": "teacher_ai_tools",
                "source": "Axios",
                "findings": "New grading assistant for HS English released.",
                "urls": ["https://www.axios.com/mock"],
            }
        ],
        "triadic_ontology": {
            "findings": "Recent paper on triadic logic and process metaphysics.",
            "urls": ["https://philpapers.org/mock"],
        },
        "ai_systems": {
            "findings": "New multi-agent benchmark results.",
            "urls": ["https://arxiv.org/abs/mock"],
        },
        "uap": {
            "findings": "Congressional subcommittee scheduled a May hearing.",
            "urls": ["https://www.congress.gov/mock"],
        },
        "newyorker": {
            "available": True,
            "title": "Mock Talk of the Town",
            "section": "Talk of the Town",
            "dek": "A dispatch from nowhere in particular.",
            "text": ("Paragraph one of the mocked New Yorker article. " * 40).strip(),
            "url": "https://www.newyorker.com/magazine/mock",
            "source": "The New Yorker",
        },
        "vault_insight": {
            "available": False,
            "insight": "",
            "context": "",
            "note_path": "",
        },
        "enriched_articles": [
            {
                "url": "https://myedmondsnews.com/council-parking",
                "source": "myedmondsnews.com",
                "title": "Council passes parking ordinance",
                "fetch_failed": False,
                "text": "Full article text for the council parking ordinance story. " * 10,
            },
            {
                "url": "https://www.bbc.com/news/mock",
                "source": "BBC",
                "title": "Mocked BBC story",
                "fetch_failed": False,
                "text": "Full article text from the BBC mock. " * 10,
            },
            {
                "url": "https://www.nybooks.com/mock",
                "source": "NYRB",
                "title": "Mocked NYRB essay",
                "fetch_failed": False,
                "text": "Essay body from the NYRB mock. " * 10,
            },
        ],
    }


class FakeLLM:
    """Placeholder LLM — not actually invoked in dry-run, since the mock agent
    bypasses the real FunctionAgent loop and calls emit_session directly.
    """

    pass


def run_mock_agent(ctx: ResearchContext, run_date: date) -> None:
    """Simulate the FunctionAgent — populate the ResearchContext via emit_session."""

    emit = make_emit_session(ctx)
    result = emit(canned_session(run_date))
    ctx.run_log.append({"tool": "emit_session", "result": result})
    # sanity check — validate on the way out
    SessionModel.model_validate(ctx.session)

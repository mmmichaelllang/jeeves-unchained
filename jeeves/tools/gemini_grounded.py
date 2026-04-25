"""Gemini grounded search — returns synthesized answer + citations, not raw SERP.

Daily cap: Google Search Grounding is free for the first 1,500 requests per UTC
day. This module enforces a hard stop at 1,490 (DAILY_HARD_CAPS["gemini_grounded"]
in quota.py) — ten below the free tier — so a burst can never trigger charges.
The cap auto-resets at UTC midnight via QuotaLedger.check_daily_allow().

Uses the google-genai SDK (import google.genai) — the replacement for the
deprecated google-generativeai package.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from .quota import QuotaExceeded, QuotaLedger

log = logging.getLogger(__name__)


def make_gemini_grounded(cfg: Config, ledger: QuotaLedger):
    def gemini_grounded_synthesize(question: str) -> dict[str, Any]:
        """Gemini 2.5 Flash with Google Search grounding.

        Returns a narrative answer plus citation URLs. Use this tool when a
        synthesized description of 'current state of X' is more useful than
        a raw ranked list of links.

        Hard daily cap: 1,490 grounded searches per UTC day (Google's free tier
        is 1,500; we stop 10 below to ensure we are never charged).
        """
        # --- daily cap check (hard stop, no charges ever) ---
        try:
            ledger.check_daily_allow("gemini_grounded")
        except QuotaExceeded as exc:
            log.warning("gemini_grounded: %s", exc)
            return {
                "provider": "gemini",
                "error": "daily cap reached — no further calls today",
                "answer": "",
                "citations": [],
            }

        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore

            client = genai.Client(api_key=cfg.google_api_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=question,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2,
                ),
            )
        except Exception as e:
            log.warning("gemini grounded error: %s", e)
            return {"provider": "gemini", "error": str(e), "answer": "", "citations": []}

        ledger.record("gemini", 1)
        ledger.record_daily("gemini_grounded", 1)
        answer = getattr(response, "text", "") or ""
        citations = _extract_citations(response)
        log.info(
            "gemini_grounded: answered (%d chars, %d citations) [daily=%d/1490]",
            len(answer), len(citations), ledger.daily_used("gemini_grounded"),
        )
        return {
            "provider": "gemini",
            "question": question,
            "answer": answer,
            "citations": citations,
        }

    return gemini_grounded_synthesize


def _extract_citations(resp: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        candidates = getattr(resp, "candidates", None) or []
        for c in candidates:
            meta = getattr(c, "grounding_metadata", None)
            if not meta:
                continue
            chunks = getattr(meta, "grounding_chunks", None) or []
            for ch in chunks:
                web = getattr(ch, "web", None)
                if web:
                    out.append(
                        {
                            "title": getattr(web, "title", "") or "",
                            "url": getattr(web, "uri", "") or "",
                        }
                    )
    except Exception as e:
        log.debug("citation parse failed: %s", e)
    return out

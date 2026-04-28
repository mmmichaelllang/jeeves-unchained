"""Gemini grounded search — returns synthesized answer + citations, not raw SERP.

Daily cap: Gemini 2.5 Flash free tier allows 20 generate_content requests per
UTC day (GenerateRequestsPerDayPerProjectPerModel-FreeTier). This module enforces
a hard stop at 12 (DAILY_HARD_CAPS["gemini_grounded"] in quota.py) — well below
20 — to leave headroom for correspondence and any pre-run calls made earlier in
the UTC day. The cap auto-resets at UTC midnight via QuotaLedger.check_daily_allow().

When the API returns 429 (RESOURCE_EXHAUSTED), the daily counter is set to the
cap immediately so all subsequent sectors skip Gemini rather than retrying.

Uses the google-genai SDK (import google.genai) — the replacement for the
deprecated google-generativeai package.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from .quota import DAILY_HARD_CAPS, QuotaExceeded, QuotaLedger

log = logging.getLogger(__name__)


def make_gemini_grounded(cfg: Config, ledger: QuotaLedger):
    def gemini_grounded_synthesize(question: str) -> dict[str, Any]:
        """Gemini 2.5 Flash with Google Search grounding.

        Returns a narrative answer plus citation URLs. Use this tool when a
        synthesized description of 'current state of X' is more useful than
        a raw ranked list of links.

        Hard daily cap: 12 calls per UTC day (free tier is 20; we stop at 12
        to leave headroom for earlier-in-day usage from correspondence/tests).
        """
        # --- daily cap check (hard stop) ---
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
            # On 429, exhaust our daily counter so subsequent sectors skip
            # Gemini rather than re-hitting the rate limit.
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                cap = DAILY_HARD_CAPS.get("gemini_grounded", 12)
                used = ledger.daily_used("gemini_grounded")
                ledger.record_daily("gemini_grounded", max(0, cap - used))
                log.info(
                    "gemini_grounded: 429 — daily counter exhausted; "
                    "subsequent sectors will skip Gemini."
                )
            return {"provider": "gemini", "error": str(e), "answer": "", "citations": []}

        ledger.record("gemini", 1)
        ledger.record_daily("gemini_grounded", 1)
        cap = DAILY_HARD_CAPS.get("gemini_grounded", 12)
        answer = getattr(response, "text", "") or ""
        citations = _extract_citations(response)
        log.info(
            "gemini_grounded: answered (%d chars, %d citations) [daily=%d/%d]",
            len(answer), len(citations), ledger.daily_used("gemini_grounded"), cap,
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

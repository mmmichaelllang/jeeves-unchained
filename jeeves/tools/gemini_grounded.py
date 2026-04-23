"""Gemini grounded search — returns synthesized answer + citations, not raw SERP."""

from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from .quota import QuotaLedger

log = logging.getLogger(__name__)


def make_gemini_grounded(cfg: Config, ledger: QuotaLedger):
    def gemini_grounded_synthesize(question: str) -> dict[str, Any]:
        """Gemini 2.5 Flash with Google Search grounding.

        Returns a narrative answer plus citation URLs. Use this tool when a
        synthesized description of 'current state of X' is more useful than
        a raw ranked list of links.
        """
        try:
            import google.generativeai as genai  # type: ignore

            genai.configure(api_key=cfg.google_api_key)
            model = genai.GenerativeModel(
                "gemini-2.5-flash",
                tools=[{"google_search": {}}],
            )
            resp = model.generate_content(question)
        except Exception as e:
            log.warning("gemini grounded error: %s", e)
            return {"provider": "gemini", "error": str(e), "answer": "", "citations": []}

        ledger.record("gemini", 1)
        answer = getattr(resp, "text", "") or ""
        citations = _extract_citations(resp)
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

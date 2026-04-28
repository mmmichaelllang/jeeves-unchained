"""Vertex AI Grounded Search — Google Search grounding via the Vertex AI SDK.

Key differences from gemini_grounded.py (standard Gemini API):
- Uses Application Default Credentials (service account JSON) instead of API key.
- Supports Dynamic Retrieval: the model only invokes Search when its confidence
  in a trained-knowledge answer is below the threshold (default 0.3). This means
  many questions are answered from training data with ZERO search invocations —
  reducing both latency and billable grounding calls.
- Same 1,500 grounded-search requests/day free tier; same 1,490/day hard cap.

Setup (one-time):
1. Create a GCP project and enable the Vertex AI API.
2. Create a service account with the "Vertex AI User" role.
3. Download the service account key JSON.
4. Store the JSON content (not the path) in GitHub Secret GOOGLE_APPLICATION_CREDENTIALS_JSON.
5. Set GOOGLE_CLOUD_PROJECT to your project ID.
6. Optionally set GOOGLE_CLOUD_REGION (default: us-central1).

At runtime the module writes the credentials JSON to a temp file and sets
GOOGLE_APPLICATION_CREDENTIALS so the vertexai SDK picks it up via ADC.
The temp file is cleaned up after each call.

If google-cloud-aiplatform is not installed or credentials are not configured,
the tool silently returns an empty result — it never raises or crashes the agent.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

from ..config import Config
from .quota import QuotaExceeded, QuotaLedger

log = logging.getLogger(__name__)

# Dynamic Retrieval: only invoke Search when model confidence < this threshold.
# 0.3 = search only when training-knowledge confidence is low.
# Lower = search more often (more accurate, more calls).
# Higher = rely on training knowledge more (fewer calls, possible staleness).
_DYNAMIC_THRESHOLD = 0.3

# Gemini model on Vertex AI.
_VERTEX_MODEL = "gemini-2.0-flash-001"


def _write_credentials_tempfile(creds_json: str) -> str | None:
    """Write credentials JSON string to a temp file; return the path."""
    try:
        fd, path = tempfile.mkstemp(suffix=".json", prefix="gcp_creds_")
        with os.fdopen(fd, "w") as f:
            f.write(creds_json)
        return path
    except Exception as exc:
        log.warning("vertex_search: failed to write credentials temp file: %s", exc)
        return None


def make_vertex_grounded(cfg: Config, ledger: QuotaLedger):
    def vertex_grounded_search(question: str) -> str:
        """Vertex AI Gemini with Dynamic Google Search grounding.

        Like gemini_grounded_synthesize but uses Vertex AI credentials (service
        account) instead of an API key. Supports Dynamic Retrieval — the model
        only calls Search when its confidence falls below the threshold (0.3),
        so many queries cost zero search invocations.

        Hard daily cap: 1,490 grounded searches per UTC day (Google free tier
        is 1,500; we stop 10 below to guarantee we are never charged).

        Returns a JSON string so LlamaIndex's _parse_tool_output() produces valid
        JSON in the NIM context rather than Python repr with single quotes.
        """
        if not cfg.google_cloud_project:
            return json.dumps({
                "provider": "vertex",
                "error": "GOOGLE_CLOUD_PROJECT not configured",
                "answer": "",
                "citations": [],
            })

        # --- daily cap check (hard stop) ---
        try:
            ledger.check_daily_allow("vertex_grounded")
        except QuotaExceeded as exc:
            log.warning("vertex_grounded: %s", exc)
            return json.dumps({
                "provider": "vertex",
                "error": "daily cap reached — no further calls today",
                "answer": "",
                "citations": [],
            })

        creds_path: str | None = None
        original_creds_env: str | None = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

        try:
            # Write credentials to temp file if JSON content was provided.
            if cfg.google_application_credentials_json:
                creds_path = _write_credentials_tempfile(
                    cfg.google_application_credentials_json
                )
                if creds_path:
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path

            import vertexai  # type: ignore
            from vertexai.generative_models import (  # type: ignore
                GenerationConfig,
                GenerativeModel,
                Tool,
                grounding,
            )

            vertexai.init(
                project=cfg.google_cloud_project,
                location=cfg.google_cloud_region,
            )

            # Dynamic Retrieval: only invoke Search when confidence < threshold.
            dynamic_retrieval_config = grounding.DynamicRetrievalConfig(
                mode=grounding.DynamicRetrievalConfig.Mode.MODE_DYNAMIC,
                dynamic_threshold=_DYNAMIC_THRESHOLD,
            )
            search_tool = Tool.from_google_search_retrieval(
                google_search_retrieval=grounding.GoogleSearchRetrieval(
                    dynamic_retrieval_config=dynamic_retrieval_config
                )
            )

            model = GenerativeModel(
                _VERTEX_MODEL,
                tools=[search_tool],
                generation_config=GenerationConfig(temperature=0.2),
            )
            resp = model.generate_content(question)

        except ImportError:
            log.warning(
                "vertex_search: google-cloud-aiplatform not installed. "
                "Run: uv sync --extra vertex"
            )
            return json.dumps({"provider": "vertex", "error": "package not installed", "answer": "", "citations": []})
        except Exception as exc:
            log.warning("vertex_grounded error: %s", exc)
            return json.dumps({"provider": "vertex", "error": str(exc), "answer": "", "citations": []})
        finally:
            # Clean up temp credentials file and restore env.
            if creds_path and os.path.exists(creds_path):
                try:
                    os.unlink(creds_path)
                except OSError:
                    pass
            if original_creds_env is None:
                os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            elif creds_path:
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = original_creds_env

        ledger.record("gemini", 1)
        ledger.record_daily("vertex_grounded", 1)

        answer = getattr(resp, "text", "") or ""
        citations = _extract_vertex_citations(resp)
        log.info(
            "vertex_grounded: answered (%d chars, %d citations) [daily=%d/1490]",
            len(answer), len(citations), ledger.daily_used("vertex_grounded"),
        )
        return json.dumps({
            "provider": "vertex",
            "question": question,
            "answer": answer,
            "citations": citations,
        })

    return vertex_grounded_search


def _extract_vertex_citations(resp: Any) -> list[dict[str, str]]:
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
                    out.append({
                        "title": getattr(web, "title", "") or "",
                        "url": getattr(web, "uri", "") or "",
                    })
    except Exception as exc:
        log.debug("vertex citation parse failed: %s", exc)
    return out

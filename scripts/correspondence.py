#!/usr/bin/env python3
"""Phase 4 — Correspondence script (STUB).

Full implementation will use Kimi K2.5 + Gmail API (OAuth, not MCP) to sweep
recent Gmail, classify threads, and draft a short correspondence briefing
that the research phase picks up the next morning.

Usage:
  python scripts/correspondence.py --plan-only
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

log = logging.getLogger("jeeves.correspondence")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Jeeves correspondence phase (Phase 4 — stub).")
    p.add_argument("--plan-only", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    log.warning("Phase 4 correspondence is a stub.")
    log.info("Will use:")
    log.info("  - google-api-python-client + google-auth-oauthlib for Gmail OAuth")
    log.info("  - Kimi K2.5 on NIM for thread classification and summary drafting")
    log.info("  - Groq Llama 3.3 70B for prose in Jeeves voice")
    log.info("  - SMTP fallback via GMAIL_APP_PASSWORD for delivery")
    if args.plan_only:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

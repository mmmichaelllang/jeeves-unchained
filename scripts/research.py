#!/usr/bin/env python3
"""Phase 2 — Research script.

Runs the Kimi K2.5 FunctionAgent to gather findings across 8 sectors and emit
a validated session JSON. Commits the JSON to the repo for the Phase 3 write
script to consume.

Usage:
  python scripts/research.py --date 2026-04-23
  python scripts/research.py --dry-run
  python scripts/research.py --limit 1 --sectors local_news,career
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Make `jeeves` importable when invoked as `python scripts/research.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jeeves.config import Config, MissingSecret  # noqa: E402
from jeeves.dedup import covered_urls  # noqa: E402
from jeeves.session_io import load_previous_session, save_session  # noqa: E402
from jeeves.tools.emit_session import ResearchContext, make_emit_session  # noqa: E402
from jeeves.tools.quota import QuotaLedger  # noqa: E402

log = logging.getLogger("jeeves.research")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Jeeves research phase (Phase 2).")
    p.add_argument("--date", default=None, help="Session date (YYYY-MM-DD). Defaults to today UTC.")
    p.add_argument("--dry-run", action="store_true", help="Skip network + use fixture payload.")
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit sectors (for real-API smoke tests). 0 = no limit.",
    )
    p.add_argument(
        "--sectors",
        default="",
        help="Comma-separated sector whitelist (e.g. local_news,career).",
    )
    p.add_argument("--verbose", action="store_true", help="Enable verbose agent logs.")
    return p.parse_args(argv)


def _load_prompt(cfg: Config) -> str:
    path = cfg.repo_root / "jeeves" / "prompts" / "research_system.md"
    return path.read_text(encoding="utf-8")


def _format_prompt(template: str, run_date: date, prior_url_sample: list[str]) -> str:
    from jeeves.schema import SessionModel

    schema_json = json.dumps(SessionModel.model_json_schema(), indent=2)
    prior_block = "\n".join(prior_url_sample) if prior_url_sample else "(none)"
    return (
        template
        .replace("{date}", run_date.isoformat())
        .replace("{schema}", schema_json)
        .replace("{prior_urls_sample}", prior_block)
    )


async def _run_real_agent(
    cfg: Config,
    ctx: ResearchContext,
    prior_urls: set[str],
    ledger: QuotaLedger,
    system_prompt: str,
    *,
    sector_whitelist: list[str],
    limit: int,
) -> None:
    """Drive the Kimi FunctionAgent until it calls emit_session or we time out."""

    from llama_index.core.agent.workflow import FunctionAgent
    from llama_index.core.tools import FunctionTool

    from jeeves.llm import build_kimi_llm
    from jeeves.tools import all_search_tools

    tools: list[FunctionTool] = all_search_tools(cfg, ledger, prior_urls)
    tools.append(
        FunctionTool.from_defaults(
            fn=make_emit_session(ctx),
            name="emit_session",
            description=(
                "Submit the final SessionModel-shaped payload. Call exactly once "
                "when all sectors are covered. Args: session_json (dict)."
            ),
        )
    )

    user_kickoff = _build_user_kickoff(sector_whitelist, limit)

    agent = FunctionAgent(
        tools=tools,
        llm=build_kimi_llm(cfg),
        system_prompt=system_prompt,
        verbose=cfg.verbose,
    )

    response = await agent.run(user_kickoff)
    log.info("agent finished. final response (truncated): %s", str(response)[:400])


def _build_user_kickoff(sector_whitelist: list[str], limit: int) -> str:
    if sector_whitelist:
        return (
            "Focus this run on the following sectors only: "
            + ", ".join(sector_whitelist)
            + ". Leave other sectors as empty defaults. Call emit_session when done."
        )
    if limit:
        return (
            f"Smoke-test run: cover only the first {limit} sectors from your system "
            "prompt. Leave the rest empty. Call emit_session when done."
        )
    return "Begin the full research run now. Call emit_session when done."


def _run_dry_agent(cfg: Config, ctx: ResearchContext) -> None:
    from jeeves.testing.mocks import run_mock_agent

    run_mock_agent(ctx, cfg.run_date)


def _merge_correspondence_handoff(cfg: Config, ctx: ResearchContext) -> None:
    """Read `sessions/correspondence-<date>.json` (if present) and inject its
    `{found, fallback_used, text}` into the session being built. Also falls
    back to the .local.json twin when running a dry-run.
    """

    import json as _json

    candidates = [cfg.correspondence_json_path()]
    local = candidates[0].with_name(candidates[0].stem + ".local.json")
    if local != candidates[0]:
        candidates.append(local)

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("correspondence handoff at %s failed to parse: %s", path, e)
            continue
        if ctx.session is None:
            continue
        corr = ctx.session.setdefault("correspondence", {})
        corr["found"] = bool(data.get("found"))
        corr["fallback_used"] = bool(data.get("fallback_used"))
        corr["text"] = data.get("text", "")
        log.info("merged correspondence handoff from %s (found=%s)", path, corr["found"])
        return


def _force_fallback_session(cfg: Config, reason: str) -> dict[str, Any]:
    """Emergency payload when the agent fails to emit anything usable."""

    from jeeves.schema import SessionModel

    empty = SessionModel(date=cfg.run_date.isoformat(), status=f"degraded: {reason}")
    return empty.model_dump(mode="json")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    try:
        cfg = Config.from_env(
            dry_run=args.dry_run,
            run_date=args.date,
            verbose=args.verbose,
        )
    except MissingSecret as e:
        log.error(str(e))
        return 2

    prior = load_previous_session(cfg)
    prior_urls = covered_urls(prior)
    prior_sample = sorted(prior_urls)[:50]
    log.info("prior session loaded: %s URLs in dedup set.", len(prior_urls))

    ledger = QuotaLedger(cfg.quota_state_path)
    ctx = ResearchContext()
    system_prompt = _format_prompt(_load_prompt(cfg), cfg.run_date, prior_sample)

    sector_whitelist = [s.strip() for s in args.sectors.split(",") if s.strip()]

    if cfg.dry_run:
        log.info("DRY RUN — using fixture mock agent.")
        _run_dry_agent(cfg, ctx)
    else:
        asyncio.run(
            _run_real_agent(
                cfg,
                ctx,
                prior_urls,
                ledger,
                system_prompt,
                sector_whitelist=sector_whitelist,
                limit=args.limit,
            )
        )

    # Phase 4 handoff — if the correspondence phase has written today's
    # sessions/correspondence-<date>.json, merge it into the agent's result.
    _merge_correspondence_handoff(cfg, ctx)

    if not ctx.has_session:
        log.error("agent halted without calling emit_session — writing degraded payload.")
        session = _force_fallback_session(cfg, "no_emit_session_call")
    else:
        session = ctx.session

    path = save_session(session, cfg)
    ledger.save()
    log.info("session saved: %s (quota state: %s)", path, cfg.quota_state_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

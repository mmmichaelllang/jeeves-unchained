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
import logging
import sys
from pathlib import Path
from typing import Any

# Make `jeeves` importable when invoked as `python scripts/research.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jeeves.config import Config, MissingSecret  # noqa: E402
from jeeves.dedup import covered_urls  # noqa: E402
from jeeves.research_sectors import (  # noqa: E402
    SECTOR_SPECS,
    collect_headlines_from_sector,
    collect_urls_from_sector,
    extract_correspondence_references,
    run_sector,
)
from jeeves.session_io import load_previous_session, save_session  # noqa: E402
from jeeves.tools.emit_session import ResearchContext  # noqa: E402
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


async def _run_sector_loop(
    cfg: Config,
    ctx: ResearchContext,
    prior_urls: set[str],
    ledger: QuotaLedger,
    *,
    sector_whitelist: list[str],
    limit: int,
) -> None:
    """Iterate SECTOR_SPECS sequentially, each with its own fresh Kimi agent.

    Per-sector runs avoid the single-context overflow that killed the earlier
    design. Accumulated URLs feed the final enriched_articles sector and the
    session dedup set.
    """

    prior_sample = sorted(prior_urls)[:50]
    session: dict[str, Any] = {
        "date": cfg.run_date.isoformat(),
        "status": "complete",
        "dedup": {"covered_urls": [], "covered_headlines": []},
    }
    discovered_urls: list[str] = []
    discovered_headlines: list[str] = []

    specs = _filter_specs(SECTOR_SPECS, sector_whitelist, limit)

    for spec in specs:
        extra = ""
        if spec.name == "enriched_articles":
            # Seed the extraction agent with the URLs the prior sectors found.
            seed = "\n".join(discovered_urls[:25]) or "(no candidate URLs from prior sectors)"
            extra = f"CANDIDATE URLS FROM TODAY'S COVERAGE:\n{seed}"

        value = await run_sector(cfg, spec, prior_sample, ledger, extra_user=extra)
        session[spec.name] = value
        discovered_urls.extend(collect_urls_from_sector(value))
        discovered_headlines.extend(collect_headlines_from_sector(value))

    # Fill any sectors we skipped via --sectors / --limit with their defaults.
    for spec in SECTOR_SPECS:
        session.setdefault(spec.name, spec.default)

    session["dedup"]["covered_urls"] = sorted(set(discovered_urls))
    session["dedup"]["covered_headlines"] = sorted(set(discovered_headlines))
    ctx.session = session


def _filter_specs(specs, whitelist: list[str], limit: int):
    out = list(specs)
    if whitelist:
        wl = set(whitelist)
        out = [s for s in out if s.name in wl]
    if limit > 0:
        out = out[:limit]
    return out


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
        # Fold email thread references into dedup.covered_headlines so the
        # write phase can skim/skip repeats across days.
        dedup = ctx.session.setdefault("dedup", {"covered_urls": [], "covered_headlines": []})
        existing = set(dedup.get("covered_headlines") or [])
        existing.update(extract_correspondence_references(corr["text"]))
        dedup["covered_headlines"] = sorted(existing)
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
    log.info("prior session loaded: %s URLs in dedup set.", len(prior_urls))

    ledger = QuotaLedger(cfg.quota_state_path)
    ctx = ResearchContext()

    sector_whitelist = [s.strip() for s in args.sectors.split(",") if s.strip()]

    if cfg.dry_run:
        log.info("DRY RUN — using fixture mock agent.")
        _run_dry_agent(cfg, ctx)
    else:
        asyncio.run(
            _run_sector_loop(
                cfg,
                ctx,
                prior_urls,
                ledger,
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

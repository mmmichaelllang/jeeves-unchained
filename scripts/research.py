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
import json as _json
import logging
import re
import sys
from pathlib import Path
from typing import Any

# Make `jeeves` importable when invoked as `python scripts/research.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jeeves.config import Config, MissingSecret  # noqa: E402
from jeeves.dedup import covered_headlines as get_covered_headlines  # noqa: E402
from jeeves.dedup import covered_urls  # noqa: E402
from jeeves.research_sectors import (  # noqa: E402
    SECTOR_SPECS,
    collect_headlines_from_sector,
    collect_urls_from_sector,
    extract_correspondence_references,
    run_sector,
)
from jeeves.schema import CorrespondenceHandoff  # noqa: E402
from jeeves.session_io import load_prior_sessions, load_previous_session, save_session  # noqa: E402
from jeeves.tools.emit_session import ResearchContext  # noqa: E402
from jeeves.tools.quota import QuotaLedger  # noqa: E402

log = logging.getLogger("jeeves.research")

# Maximum concurrent sector agents.  Keeps NIM request load manageable while
# still providing meaningful parallelism for the 10+ non-enriched sectors.
_SECTOR_SEMAPHORE = 3


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


def _quota_summary(ledger: QuotaLedger) -> str:
    """Format a one-line provider quota summary for the agent context header."""
    parts = []
    for name in ("serper", "tavily", "exa", "gemini"):
        remaining = ledger.remaining_free(name)
        state = ledger._state["providers"].get(name, {})
        cap = state.get("free_cap", 0)
        if remaining == 0:
            parts.append(f"{name}: EXHAUSTED — avoid")
        else:
            parts.append(f"{name}: {remaining}/{cap} remaining")
    return ", ".join(parts) if parts else ""


def _story_continuity_block(sessions: list) -> str:
    """Build a compact 'prior stories' block from recent sessions.

    Extracts first-sentence summaries from key sectors so the research agent
    knows which stories are in mid-flight (e.g. 'Day 3 of tariff talks') rather
    than seeing them as new every day.
    """
    if not sessions:
        return ""

    lines: list[str] = []
    seen: set[str] = set()

    def _add(label: str, text: str) -> None:
        if not text:
            return
        sentence = text.strip().split(".")[0].strip()
        if sentence and sentence not in seen and len(sentence) > 10:
            seen.add(sentence)
            lines.append(f"  [{label}] {sentence}.")

    for sess in sessions[:3]:  # cap at 3 days of history to stay concise
        date_str = getattr(sess, "date", "?")
        for finding in (sess.global_news or []):
            _add(f"global {date_str}", getattr(finding, "findings", "") or "")
        for finding in (sess.local_news or []):
            _add(f"local {date_str}", getattr(finding, "findings", "") or "")
        for attr in ("triadic_ontology", "ai_systems", "uap"):
            block = getattr(sess, attr, None)
            if block:
                _add(f"{attr} {date_str}", getattr(block, "findings", "") or "")

    if not lines:
        return ""
    header = "Ongoing stories from recent briefings — treat these as in-progress threads:"
    return header + "\n" + "\n".join(lines[:20])  # cap at 20 lines


def _load_prior_coverage_urls(cfg: Config) -> set[str]:
    """Parse the prior day's briefing HTML COVERAGE_LOG for extra prior-URL context.

    The COVERAGE_LOG comment in the rendered briefing records every linked URL
    by sector. Adding these to prior_urls closes the loop between Phase 3 output
    and Phase 2 dedup — URLs that Jeeves actually cited in prose are guaranteed
    not to resurface unannounced the next day.
    """
    from datetime import timedelta

    urls: set[str] = set()
    for delta in range(1, 4):  # look back up to 3 days for a briefing
        d = cfg.run_date - timedelta(days=delta)
        candidates = [
            cfg.briefing_html_path(d),
            cfg.sessions_dir / f"briefing-{d.isoformat()}.html",
            cfg.sessions_dir / f"briefing-{d.isoformat()}.local.html",
        ]
        for path in candidates:
            if path.exists():
                try:
                    html = path.read_text(encoding="utf-8")
                    m = re.search(r"<!--\s*COVERAGE_LOG:\s*(\[.*?\])\s*-->", html, re.DOTALL)
                    if m:
                        entries = _json.loads(m.group(1))
                        for entry in entries:
                            if isinstance(entry, dict) and entry.get("url"):
                                urls.add(entry["url"].rstrip("/"))
                        log.info(
                            "COVERAGE_LOG feedback: %d URLs added from %s", len(urls), path
                        )
                except Exception as e:
                    log.warning("failed to parse COVERAGE_LOG from %s: %s", path, e)
                return urls  # stop at the most recent found briefing
    return urls


async def _run_sector_loop(
    cfg: Config,
    ctx: ResearchContext,
    prior_urls: set[str],
    prior_headlines: set[str],
    ledger: QuotaLedger,
    *,
    sector_whitelist: list[str],
    limit: int,
    quota_summary: str = "",
    story_continuity: str = "",
) -> None:
    """Run SECTOR_SPECS with up to _SECTOR_SEMAPHORE concurrent agents.

    Non-enriched sectors run in parallel (max 3 at a time). The special
    `enriched_articles` sector always runs last, seeded with URLs discovered
    across all earlier sectors.

    Prior-session headlines are carried forward in dedup.covered_headlines so
    the write phase can synthesize across days rather than treating every story
    as new.
    """

    prior_sample = sorted(prior_urls)[:50]
    session: dict[str, Any] = {
        "date": cfg.run_date.isoformat(),
        "status": "complete",
        "dedup": {"covered_urls": [], "covered_headlines": sorted(prior_headlines)},
    }
    discovered_urls: list[str] = []
    discovered_headlines: list[str] = sorted(prior_headlines)

    specs = _filter_specs(SECTOR_SPECS, sector_whitelist, limit)
    non_enriched = [s for s in specs if s.name != "enriched_articles"]
    enriched_spec = next((s for s in specs if s.name == "enriched_articles"), None)

    sem = asyncio.Semaphore(_SECTOR_SEMAPHORE)

    async def _run_one(spec):
        async with sem:
            return spec.name, await run_sector(
                cfg, spec, prior_sample, ledger,
                quota_summary=quota_summary,
                story_continuity=story_continuity,
            )

    log.info("running %d non-enriched sectors (max %d concurrent)…", len(non_enriched), _SECTOR_SEMAPHORE)
    results = await asyncio.gather(*[_run_one(spec) for spec in non_enriched])

    for name, value in results:
        session[name] = value
        discovered_urls.extend(collect_urls_from_sector(value))
        discovered_headlines.extend(collect_headlines_from_sector(value))

    if enriched_spec is not None:
        seed = "\n".join(discovered_urls[:25]) or "(no candidate URLs from prior sectors)"
        extra = f"CANDIDATE URLS FROM TODAY'S COVERAGE:\n{seed}"
        log.info("running enriched_articles sector (seeded with %d URLs)…", len(discovered_urls))
        ea_value = await run_sector(
            cfg, enriched_spec, prior_sample, ledger,
            extra_user=extra,
            quota_summary=quota_summary,
            story_continuity=story_continuity,
        )
        session[enriched_spec.name] = ea_value
        discovered_urls.extend(collect_urls_from_sector(ea_value))
        discovered_headlines.extend(collect_headlines_from_sector(ea_value))

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

    Validates via the CorrespondenceHandoff Pydantic model and logs a warning
    on schema violations so data-contract drift is caught early.
    """

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

        try:
            handoff = CorrespondenceHandoff.model_validate(data)
        except Exception as e:
            log.warning(
                "correspondence handoff at %s failed schema validation: %s — "
                "merging raw data anyway, but check Phase 4 output contract.",
                path, e,
            )
            handoff = CorrespondenceHandoff(
                found=bool(data.get("found")),
                fallback_used=bool(data.get("fallback_used")),
                text=data.get("text", ""),
            )

        if ctx.session is None:
            continue
        corr = ctx.session.setdefault("correspondence", {})
        corr["found"] = handoff.found
        corr["fallback_used"] = handoff.fallback_used
        corr["text"] = handoff.text
        # Fold email thread references into dedup.covered_headlines.
        dedup = ctx.session.setdefault("dedup", {"covered_urls": [], "covered_headlines": []})
        existing = set(dedup.get("covered_headlines") or [])
        existing.update(extract_correspondence_references(handoff.text))
        dedup["covered_headlines"] = sorted(existing)
        log.info("merged correspondence handoff from %s (found=%s)", path, handoff.found)
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

    # Rolling 7-day window: merge covered URLs and headlines from all recent sessions.
    prior_sessions = load_prior_sessions(cfg, days=7)
    prior_urls: set[str] = set()
    prior_hl: set[str] = set()
    for sess in prior_sessions:
        prior_urls |= covered_urls(sess)
        prior_hl |= get_covered_headlines(sess)

    # COVERAGE_LOG feedback: URLs that Jeeves actually cited in recent briefings.
    prior_urls |= _load_prior_coverage_urls(cfg)

    log.info(
        "%d prior sessions loaded: %d URLs, %d headlines in rolling dedup set.",
        len(prior_sessions), len(prior_urls), len(prior_hl),
    )

    ledger = QuotaLedger(cfg.quota_state_path)
    ctx = ResearchContext()

    sector_whitelist = [s.strip() for s in args.sectors.split(",") if s.strip()]

    if cfg.dry_run:
        log.info("DRY RUN — using fixture mock agent.")
        _run_dry_agent(cfg, ctx)
    else:
        quota_sum = _quota_summary(ledger)
        story_ctx = _story_continuity_block(prior_sessions)
        asyncio.run(
            _run_sector_loop(
                cfg,
                ctx,
                prior_urls,
                prior_hl,
                ledger,
                sector_whitelist=sector_whitelist,
                limit=args.limit,
                quota_summary=quota_sum,
                story_continuity=story_ctx,
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

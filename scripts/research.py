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
from jeeves.dedup import covered_sources_by_host  # noqa: E402
from jeeves.dedup import covered_urls  # noqa: E402
from jeeves.research_sectors import (  # noqa: E402
    SECTOR_SPECS,
    _find_cross_sector_dupes,
    collect_headlines_from_sector,
    collect_urls_from_sector,
    extract_correspondence_references,
    run_sector,
)
from jeeves.schema import CorrespondenceHandoff  # noqa: E402
from jeeves.session_io import load_prior_sessions, save_session  # noqa: E402
from jeeves.tools.emit_session import ResearchContext  # noqa: E402
from jeeves.tools.quota import QuotaLedger  # noqa: E402
from jeeves.vault import populate_vault_insight  # noqa: E402

log = logging.getLogger("jeeves.research")

# Tiered sector semaphores — NIM free tier constraint.
#
# Deep sectors (triadic_ontology, ai_systems, uap) use max_tokens=4096 and
# have forced NIM retries (~10s overhead each). Running two concurrently risks
# a 429 cascade that costs 60-120s backoff — wiping out any wall-clock gain.
# Historical: semaphore=3 caused ALL sectors to return defaults in <1 min.
#
# Light sectors WERE briefly raised to semaphore=2 + pair-gather (sprint-19
# slice E, PR #85 merged 2026-05-05) for ~40% wall-clock saving. Reverted
# 2026-05-06 after two production runs lost 4/8 (run B) and 6/8 (run C) light
# sectors: NIM free tier closes one or both streaming agents under concurrent
# Kimi calls, and the loser exhausts its 60+120s rate-limit retry budget and
# returns spec.default. Both knobs (semaphore + dispatch shape) restored to
# match deep-sector behaviour: solo, sequential, prior_sample grows after each.
_SECTOR_SEMAPHORE_HEAVY = asyncio.Semaphore(1)
_SECTOR_SEMAPHORE_LIGHT = asyncio.Semaphore(1)

# Sectors that use max_tokens=4096 and are prone to 429 / stream-drop on NIM.
_DEEP_SECTOR_NAMES: frozenset[str] = frozenset({"triadic_ontology", "ai_systems", "uap"})


def _sector_semaphore(sector_name: str) -> asyncio.Semaphore:
    """Return the appropriate asyncio.Semaphore for this sector's weight class."""
    return _SECTOR_SEMAPHORE_HEAVY if sector_name in _DEEP_SECTOR_NAMES else _SECTOR_SEMAPHORE_LIGHT


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
    prior_urls_ordered: list[str],
    prior_headlines: set[str],
    ledger: QuotaLedger,
    *,
    sector_whitelist: list[str],
    limit: int,
    quota_summary: str = "",
    story_continuity: str = "",
    prior_sources_by_host: dict[str, list[str]] | None = None,
) -> None:
    """Run SECTOR_SPECS sequentially, updating the dedup context after each sector.

    `enriched_articles` always runs last, seeded with all URLs discovered
    across earlier sectors today.

    Key design decisions:
    - Sectors run solo (both deep and light use semaphore=1) to avoid NIM
      stream-drop / 429 cascades on the free tier. Pair-concurrency was tried
      in sprint-19 slice E and reverted 2026-05-06 after two daily runs lost
      4/8 and 6/8 light sectors. See the _SECTOR_SEMAPHORE_LIGHT comment block.
    - prior_sample grows progressively: after each sector we append its
      discovered URLs so the NEXT sector sees full within-session context.
    - prior_urls_ordered is newest-first: caller builds it by walking prior sessions
      newest→oldest, then COVERAGE_LOG. A 150-URL cap keeps the prompt size bounded
      while guaranteeing yesterday's URLs always appear first.
    - Today's discovered headlines go to the HEAD of covered_headlines; prior-session
      headlines go to the tail. Write phase [:80] then always captures fresh content.
    """

    # Start with the most recent 150 prior URLs (recency-ordered by caller).
    prior_sample: list[str] = list(prior_urls_ordered[:150])
    # Keep a set for O(1) membership checks when growing prior_sample.
    prior_sample_set: set[str] = set(prior_sample)

    session: dict[str, Any] = {
        "date": cfg.run_date.isoformat(),
        "status": "complete",
        "dedup": {"covered_urls": [], "covered_headlines": []},
    }
    discovered_urls: list[str] = []
    discovered_headlines: list[str] = []

    specs = _filter_specs(SECTOR_SPECS, sector_whitelist, limit)
    non_enriched = [s for s in specs if s.name != "enriched_articles"]
    enriched_spec = next((s for s in specs if s.name == "enriched_articles"), None)

    # Helper: run one sector under its weight-class semaphore.
    async def _run_one(spec):
        async with _sector_semaphore(spec.name):
            return spec.name, await run_sector(
                cfg, spec, list(prior_sample), ledger,
                quota_summary=quota_summary,
                story_continuity=story_continuity,
                prior_sources_by_host=prior_sources_by_host,
            )

    def _update_prior(results):
        """Merge sector results into session and grow prior_sample."""
        for name, value in results:
            session[name] = value
            new_urls = collect_urls_from_sector(value)
            discovered_urls.extend(new_urls)
            discovered_headlines.extend(collect_headlines_from_sector(value))
            for u in new_urls:
                if u not in prior_sample_set:
                    prior_sample.append(u)
                    prior_sample_set.add(u)
        log.debug(
            "batch done — prior_sample now %d URLs, %d today's headlines",
            len(prior_sample), len(discovered_headlines),
        )

    # Separate deep (solo) from light (pairable) sectors, preserving SECTOR_SPECS order.
    deep_specs = [s for s in non_enriched if s.name in _DEEP_SECTOR_NAMES]
    light_specs = [s for s in non_enriched if s.name not in _DEEP_SECTOR_NAMES]

    log.info(
        "running %d non-enriched sectors sequentially: %d light first, then %d deep…",
        len(non_enriched), len(light_specs), len(deep_specs),
    )

    # 2026-05-08 reorder — light sectors FIRST, deep sectors LAST.
    #
    # Why: career and family are light sectors whose Kimi agent runs on NIM.
    # When they ran AFTER the deep sectors (triadic_ontology, ai_systems, uap),
    # NIM's free-tier TPM budget was saturated and they ate 429s for all 3
    # retries → returned default {} → empty section in briefing. Sessions
    # 5/3 through 5/8 confirm career has been empty (or malformed) for a week.
    # Light sectors carry Lang-personal content (job hunt, choir, toddler
    # activities) with no usable fallback; deep sectors fall back cleanly to
    # `default={"findings":"","urls":[]}`. Better to lose the deep sectors
    # silently than to ship empty career/family every day.
    for spec in light_specs:
        results = [await _run_one(spec)]
        _update_prior(results)

    # Run deep sectors one at a time — NIM stream-drop / 429 risk too high to pair.
    for spec in deep_specs:
        results = [await _run_one(spec)]
        _update_prior(results)

    if enriched_spec is not None:
        seed = "\n".join(discovered_urls[:25]) or "(no candidate URLs from prior sectors)"
        extra = f"CANDIDATE URLS FROM TODAY'S COVERAGE:\n{seed}"
        log.info("running enriched_articles sector (seeded with %d URLs)…", len(discovered_urls))
        ea_value = await run_sector(
            cfg, enriched_spec, prior_sample, ledger,
            extra_user=extra,
            quota_summary=quota_summary,
            story_continuity=story_continuity,
            prior_sources_by_host=prior_sources_by_host,
        )
        session[enriched_spec.name] = ea_value
        discovered_urls.extend(collect_urls_from_sector(ea_value))
        discovered_headlines.extend(collect_headlines_from_sector(ea_value))

    # Fill any sectors we skipped via --sectors / --limit with their defaults.
    for spec in SECTOR_SPECS:
        session.setdefault(spec.name, spec.default)

    session["dedup"]["covered_urls"] = sorted(set(discovered_urls))
    # Today's discoveries first so write-phase [:N] always captures fresh content;
    # prior-session headlines at the tail for cross-day context.
    today_hl = list(dict.fromkeys(discovered_headlines))  # dedupe, preserve order
    prior_hl_list = sorted(prior_headlines - set(today_hl))
    session["dedup"]["covered_headlines"] = today_hl + prior_hl_list

    # Cross-sector URL collisions — same article landing in 2+ sectors.
    # Surfaced to the write phase so the same story isn't narrated multiple
    # times under different section headers.
    cross_dupes = _find_cross_sector_dupes(session)
    if cross_dupes:
        log.info("cross-sector duplicate URLs found: %d", len(cross_dupes))
    session["dedup"]["cross_sector_dupes"] = cross_dupes

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
        # Fold email thread references into dedup.covered_headlines while
        # PRESERVING recency order. _run_sector_loop puts today's discovered
        # headlines at the HEAD of the list — sorting alphabetically destroys
        # that invariant. Append correspondence refs at the tail (oldest)
        # since they are not "today's discovered headlines".
        dedup = ctx.session.setdefault("dedup", {"covered_urls": [], "covered_headlines": []})
        existing_list = list(dedup.get("covered_headlines") or [])
        existing_set = set(existing_list)
        new_refs = extract_correspondence_references(handoff.text)
        for ref in new_refs:
            if ref not in existing_set:
                existing_list.append(ref)
                existing_set.add(ref)
        dedup["covered_headlines"] = existing_list
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
    # Build prior_urls_ordered newest-first so the 150-URL cap in _run_sector_loop
    # always includes yesterday's URLs rather than an alphabetical mix of 7 days.
    prior_sessions = load_prior_sessions(cfg, days=7)
    prior_urls_ordered: list[str] = []
    prior_urls_seen: set[str] = set()
    prior_hl: set[str] = set()
    prior_sources_by_host: dict[str, list[str]] = {}
    for sess in prior_sessions:  # load_prior_sessions returns newest-first
        for u in covered_urls(sess):
            if u not in prior_urls_seen:
                prior_urls_ordered.append(u)
                prior_urls_seen.add(u)
        prior_hl |= get_covered_headlines(sess)
        # Source-rotation map: per-host list of titles cited yesterday.
        # Newer-first, dedup per host.
        for host, titles in covered_sources_by_host(sess).items():
            bucket = prior_sources_by_host.setdefault(host, [])
            for t in titles:
                if t and t not in bucket:
                    bucket.append(t)

    # COVERAGE_LOG feedback: URLs Jeeves actually cited in prose go first —
    # they are the highest-confidence already-covered signal.
    for u in _load_prior_coverage_urls(cfg):
        if u not in prior_urls_seen:
            prior_urls_ordered.insert(0, u)
            prior_urls_seen.add(u)

    log.info(
        "%d prior sessions loaded: %d URLs (ordered), %d headlines, %d hosts in rolling dedup set.",
        len(prior_sessions), len(prior_urls_ordered), len(prior_hl),
        len(prior_sources_by_host),
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
                prior_urls_ordered,
                prior_hl,
                ledger,
                sector_whitelist=sector_whitelist,
                limit=args.limit,
                quota_summary=quota_sum,
                story_continuity=story_ctx,
                prior_sources_by_host=prior_sources_by_host,
            )
        )

    # Phase 4 handoff — if the correspondence phase has written today's
    # sessions/correspondence-<date>.json, merge it into the agent's result.
    _merge_correspondence_handoff(cfg, ctx)

    if not ctx.has_session:
        log.error("sector loop produced no session data — writing degraded payload.")
        session = _force_fallback_session(cfg, "no_emit_session_call")
    else:
        session = ctx.session

    # Sprint-19: populate Library Stacks (PART 8) from a local vault when
    # JEEVES_VAULT_PATH is set. No-op when env var is empty — preserves
    # pre-sprint behaviour of an empty Library Stacks section.
    try:
        wrote = populate_vault_insight(session)
        log.info("vault_insight populated: %s", wrote)
    except Exception as exc:  # vault must never break a research run
        log.warning("vault_insight population failed: %s", exc)

    path = save_session(session, cfg)
    ledger.save()
    log.info("session saved: %s (quota state: %s)", path, cfg.quota_state_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

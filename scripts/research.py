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
import os
import re
import sys
from pathlib import Path
from typing import Any

# Make `jeeves` importable when invoked as `python scripts/research.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jeeves.config import Config, MissingSecret  # noqa: E402
from jeeves.dedup import canonical_headline, canonical_url  # noqa: E402
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
    p.add_argument("--run-tag", default="", help="Optional suffix for session filename (e.g. 'manual1'). Prevents overwriting the standard daily session.")
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

    # 14 days of history (was 3). Existing 20-line output cap below still
    # bounds the block size — wider window just lets weekly-recurring
    # stories (Atlantic-monthly, Economist-bi-weekly, lab papers) surface
    # in the continuity block rather than reading as net-new every cycle.
    for sess in sessions[:14]:
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
    prior_headlines: list[str],
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
        "running %d non-enriched sectors sequentially: %d deep, %d light…",
        len(non_enriched), len(deep_specs), len(light_specs),
    )

    # Run deep sectors one at a time — NIM stream-drop / 429 risk too high to pair.
    for spec in deep_specs:
        results = [await _run_one(spec)]
        _update_prior(results)

    # Run light sectors sequentially (sprint-19 slice E pair-gather reverted
    # 2026-05-06: NIM free tier wiped 4-6/8 light sectors per run when called
    # concurrently). Same shape as deep loop above; prior_sample grows after each.
    for spec in light_specs:
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

    session["dedup"]["covered_urls"] = sorted({canonical_url(u) for u in discovered_urls if u})
    # Today's discoveries first so write-phase [:N] always captures fresh content;
    # prior-session headlines at the tail for cross-day context.
    #
    # Within-today dedup keys off canonical_headline so "Trump tariffs",
    # "Trump tariffs.", and "TRUMP TARIFFS" don't all consume separate cap
    # slots — the most common silent cap-burner before this fix.
    today_hl: list[str] = []
    today_hl_keys: set[str] = set()
    for hl in discovered_headlines:
        key = canonical_headline(hl)
        if key and key not in today_hl_keys:
            today_hl.append(hl)
            today_hl_keys.add(key)
    # prior_headlines is recency-ordered (newest-first from load_prior_sessions).
    # Filter out today's headlines using canonical keys so a punctuation
    # variant of today's story doesn't sneak into the prior portion.
    prior_hl_list = [h for h in prior_headlines if canonical_headline(h) not in today_hl_keys]
    session["dedup"]["covered_headlines"] = today_hl + prior_hl_list
    # Boundary marker so write phase can apply a proportional cap
    # (today_slots + prior_slots) without today crowding out prior history.
    session["dedup"]["today_headline_count"] = len(today_hl)

    # Cross-sector URL collisions — same article landing in 2+ sectors.
    # Surfaced to the write phase so the same story isn't narrated multiple
    # times under different section headers.
    cross_dupes = _find_cross_sector_dupes(session)
    if cross_dupes:
        log.info("cross-sector duplicate URLs found: %d", len(cross_dupes))
        # PRUNE — not just flag. Previously cross_sector_dupes was a soft
        # signal in the write prompt that the model often ignored. Now we
        # physically remove the duplicate URLs from all sectors except the
        # FIRST one to surface them, so the model sees each story exactly
        # once. The first-sector preference is set by SECTOR_SPECS order
        # in research_sectors.py.
        _prune_cross_sector_dupes(session, cross_dupes)
    session["dedup"]["cross_sector_dupes"] = cross_dupes

    ctx.session = session


# Sectors scanned for cross-sector pruning. Must mirror _CROSS_SECTOR_FIELDS
# in jeeves/research_sectors.py — kept local so this file doesn't import a
# private constant. Order matters: the FIRST sector in this list that
# carries a dupe URL keeps the article; later sectors drop it.
_PRUNE_SECTOR_ORDER: tuple[str, ...] = (
    "local_news",
    "global_news",
    "intellectual_journals",
    "wearable_ai",
    "enriched_articles",
)


def _prune_cross_sector_dupes(session: dict, cross_dupes: list[str]) -> None:
    """Remove cross-sector duplicate URLs from all sectors except the FIRST.

    Mutates ``session`` in place. ``cross_dupes`` is the list of canonical
    URLs returned by ``_find_cross_sector_dupes`` (canonical form so we
    match through utm/host-variant decorations).

    For each sector after the first to carry a dupe:
      - drop the URL from each item's ``urls`` list (canonical-compare)
      - if an item's ``urls`` becomes empty AND it has no other
        identifying content, drop the whole item

    Logs one summary line so daily-run forensics can see what got pruned.
    Defensive: never raises — pruning is opportunistic.
    """
    from jeeves.dedup import canonical_url

    if not cross_dupes:
        return

    dupe_set = {canonical_url(u) for u in cross_dupes if u}
    if not dupe_set:
        return

    # Track which URL has already been "claimed" by a sector so subsequent
    # sectors drop it. First sector in _PRUNE_SECTOR_ORDER wins.
    claimed: set[str] = set()
    dropped_per_sector: dict[str, int] = {}

    for field in _PRUNE_SECTOR_ORDER:
        items = session.get(field)
        if not isinstance(items, list):
            continue
        new_items: list = []
        for item in items:
            if not isinstance(item, dict):
                new_items.append(item)
                continue
            urls = item.get("urls") or []
            if not isinstance(urls, list):
                new_items.append(item)
                continue
            kept_urls: list[str] = []
            for u in urls:
                if not isinstance(u, str):
                    kept_urls.append(u)
                    continue
                canon = canonical_url(u)
                if canon in dupe_set:
                    if canon in claimed:
                        # Already claimed by an earlier sector — drop.
                        dropped_per_sector[field] = dropped_per_sector.get(field, 0) + 1
                        continue
                    # First time seen — this sector wins the article.
                    claimed.add(canon)
                kept_urls.append(u)
            item["urls"] = kept_urls
            # Only drop the item entirely if URL list is now empty AND the
            # item has no standalone identifying content. A Finding with a
            # populated `findings` string still belongs even URL-less.
            findings_text = (item.get("findings") or item.get("summary") or "").strip()
            if not kept_urls and not findings_text:
                dropped_per_sector[field] = dropped_per_sector.get(field, 0) + 1
                continue
            new_items.append(item)
        session[field] = new_items

    if dropped_per_sector:
        log.info(
            "cross-sector pruning: %s",
            ", ".join(f"{k}={v}" for k, v in dropped_per_sector.items()),
        )


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
            run_tag=args.run_tag,
            verbose=args.verbose,
        )
    except MissingSecret as e:
        log.error(str(e))
        return 2

    # Rolling 7-day window: merge covered URLs and headlines from all recent sessions.
    # Build prior_urls_ordered newest-first so the 150-URL cap in _run_sector_loop
    # always includes yesterday's URLs rather than an alphabetical mix of 7 days.
    # 14-day window (was 7): weekly recurring sources repeat every 5-7 days,
    # so a 7-day window misses them on the 8th day. 14 days catches two full
    # weekly cycles at negligible cost (headlines are capped later anyway).
    prior_sessions = load_prior_sessions(cfg, days=14)
    prior_urls_ordered: list[str] = []
    prior_urls_seen: set[str] = set()
    # Build prior_hl as a recency-ordered list (newest-first) rather than a
    # set. Preserving order lets the write phase prioritise recent history
    # when applying the proportional cap — oldest entries fall off the tail.
    #
    # Dedup membership keys off `canonical_headline(hl)` rather than raw
    # strings — case, punctuation, articles, and trailing-period variants
    # all collapse to the same key so a story doesn't consume 3 cap slots
    # under cosmetic variation. We KEEP the original-cased text in
    # prior_hl (the model needs readable strings) but bucket by canonical.
    prior_hl: list[str] = []
    prior_hl_keys: set[str] = set()
    prior_sources_by_host: dict[str, list[str]] = {}
    for sess in prior_sessions:  # load_prior_sessions returns newest-first
        for u in covered_urls(sess):
            # covered_urls() already canonicalizes, but call again as
            # defense-in-depth against future changes to that function.
            canon = canonical_url(u)
            if canon not in prior_urls_seen:
                prior_urls_ordered.append(canon)
                prior_urls_seen.add(canon)
        for hl in get_covered_headlines(sess):
            key = canonical_headline(hl)
            if key and key not in prior_hl_keys:
                prior_hl.append(hl)
                prior_hl_keys.add(key)
        # Source-rotation map: per-host list of titles cited yesterday.
        # Newer-first, dedup per host.
        for host, titles in covered_sources_by_host(sess).items():
            bucket = prior_sources_by_host.setdefault(host, [])
            for t in titles:
                if t and t not in bucket:
                    bucket.append(t)

    # COVERAGE_LOG feedback: URLs Jeeves actually cited in prose go first —
    # they are the highest-confidence already-covered signal. Canonicalize
    # before merging so utm-decorated coverage URLs collide with the bare
    # forms research already collected.
    for u in _load_prior_coverage_urls(cfg):
        canon = canonical_url(u)
        if canon not in prior_urls_seen:
            prior_urls_ordered.insert(0, canon)
            prior_urls_seen.add(canon)

    log.info(
        "%d prior sessions loaded: %d URLs (ordered), %d headlines, %d hosts in rolling dedup set.",
        len(prior_sessions), len(prior_urls_ordered), len(prior_hl),
        len(prior_sources_by_host),
    )

    ledger = QuotaLedger(cfg.quota_state_path)
    ctx = ResearchContext()

    # Flaw 10 — per-run seen-URL cache: drop any stale entries from a
    # previous run in this process (long-running test contexts) so the
    # first sector's fetches always do real work.
    try:
        from jeeves.tools.enrichment import reset_seen_url_cache as _reset_cache
        _reset_cache()
    except Exception:
        pass

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

    # GATE-B: refuse-to-ship-empty research session.
    # Mirror of write-phase GATE-A. If every agent-using sector is empty
    # (newyorker is the only direct-fetch sector and doesn't count alone),
    # exit 6 so the write phase doesn't waste effort on garbage input.
    _AGENT_SECTOR_NAMES = frozenset(
        s.name for s in SECTOR_SPECS if s.name != "newyorker"
    )

    def _spec_default_for(name: str):
        for s in SECTOR_SPECS:
            if s.name == name:
                return s.default
        return None

    def _sector_is_empty(value) -> bool:
        if isinstance(value, list):
            return len(value) == 0
        if isinstance(value, dict):
            return not any(value.values())
        if isinstance(value, str):
            return value == ""
        return False

    def _sector_total_chars(value) -> int:
        """Count characters of substantive content across any sector shape.

        Used by GATE-C's richness check (2026-05-21) — pure emptiness is
        rare; the real degraded mode is "agent returned 1 thin item with
        a half-sentence finding string." This helper counts the bytes of
        actual narrative content, ignoring structural keys (urls, source).
        """
        if value is None:
            return 0
        if isinstance(value, str):
            return len(value.strip())
        if isinstance(value, list):
            total = 0
            for item in value:
                if isinstance(item, dict):
                    # Pull the prose fields explicitly; do NOT count url
                    # lists or category labels.
                    for k in ("findings", "summary", "text", "dek", "insight"):
                        v = item.get(k)
                        if isinstance(v, str):
                            total += len(v.strip())
                elif isinstance(item, str):
                    total += len(item.strip())
            return total
        if isinstance(value, dict):
            total = 0
            for k in (
                "findings", "summary", "text", "dek", "insight",
                "choir", "toddler", "notes",
            ):
                v = value.get(k)
                if isinstance(v, str):
                    total += len(v.strip())
            # nested lists (e.g. career.openings, english_lesson_plans.classroom_ready)
            for k in ("openings", "classroom_ready", "pedagogy_pieces"):
                v = value.get(k)
                if isinstance(v, list):
                    total += _sector_total_chars(v)
            return total
        return 0

    def _sector_is_thin(value, min_chars: int) -> bool:
        """A sector is 'thin' if it's empty OR carries less than min_chars
        of substantive content. Distinct from _sector_is_empty in that a
        list with one half-sentence finding still counts as thin."""
        if _sector_is_empty(value):
            return True
        return _sector_total_chars(value) < min_chars

    # Dry-runs use fixture data that's intentionally thin (~30-180 chars per
    # sector) — gating them is wrong. Skip both gates entirely in dry-run mode.
    # Override still possible by setting JEEVES_FORCE_RESEARCH_EMPTY/DEGRADED=0
    # explicitly, but the default is "dry-run never fails on gates."
    if cfg.dry_run:
        return 0

    if os.environ.get("JEEVES_FORCE_RESEARCH_EMPTY") != "1":
        empty_agent_sectors = [
            name for name in _AGENT_SECTOR_NAMES
            if _sector_is_empty(session.get(name, _spec_default_for(name)))
        ]
        if len(empty_agent_sectors) == len(_AGENT_SECTOR_NAMES):
            log.error(
                "GATE-B: all %d agent sectors empty — refusing to commit garbage session. "
                "Set JEEVES_FORCE_RESEARCH_EMPTY=1 to override.",
                len(_AGENT_SECTOR_NAMES),
            )
            return 6

        # GATE-C — majority-thin degraded run (richness check, 2026-05-21).
        # GATE-B catches total failures, but recent production telemetry
        # showed 12-of-13 empty sectors shipping as "success" because
        # GATE-B's threshold is 100%. GATE-C catches the more-common
        # silent-degradation modes:
        #   1. Most sectors empty but newyorker direct-fetch saves run from
        #      GATE-B (the May 19/20 failure pattern).
        #   2. Most sectors return ONE thin item with a half-sentence
        #      finding — passes _sector_is_empty but represents broken
        #      research equivalent to the empty case.
        #
        # Default thresholds:
        #   - Sector emptiness fraction >=50% → exit 7 (degraded)
        #   - Per-sector min substantive chars: 200
        # Tunable via JEEVES_GATE_C_THRESHOLD (float 0..1) and
        # JEEVES_GATE_C_MIN_CHARS (int). Override with JEEVES_FORCE_DEGRADED=1.
        _gate_c_threshold = float(os.environ.get("JEEVES_GATE_C_THRESHOLD", "0.5"))
        _gate_c_min_chars = int(os.environ.get("JEEVES_GATE_C_MIN_CHARS", "200"))
        thin_agent_sectors = [
            name for name in _AGENT_SECTOR_NAMES
            if _sector_is_thin(session.get(name, _spec_default_for(name)), _gate_c_min_chars)
        ]
        _thin_fraction = (
            len(thin_agent_sectors) / len(_AGENT_SECTOR_NAMES)
            if _AGENT_SECTOR_NAMES else 0.0
        )
        if (
            _thin_fraction >= _gate_c_threshold
            and os.environ.get("JEEVES_FORCE_DEGRADED") != "1"
        ):
            # Per-sector char counts for forensic visibility.
            thin_report = []
            for name in sorted(thin_agent_sectors):
                chars = _sector_total_chars(session.get(name, _spec_default_for(name)))
                thin_report.append(f"{name}({chars}c)")
            log.error(
                "GATE-C: %d/%d agent sectors thin (%.0f%% >= %.0f%% threshold, "
                "min %d chars/sector) — degraded research run. "
                "Thin sectors: %s. "
                "Set JEEVES_FORCE_DEGRADED=1, raise JEEVES_GATE_C_THRESHOLD, or "
                "lower JEEVES_GATE_C_MIN_CHARS to bypass.",
                len(thin_agent_sectors), len(_AGENT_SECTOR_NAMES),
                _thin_fraction * 100, _gate_c_threshold * 100,
                _gate_c_min_chars,
                ", ".join(thin_report),
            )
            return 7

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

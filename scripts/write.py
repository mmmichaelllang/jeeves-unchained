#!/usr/bin/env python3
"""Phase 3 — Write script.

Loads a session JSON, generates a Jeeves-voice HTML briefing via Groq
Llama 3.3 70B, and sends it to the recipient via Gmail SMTP.

Usage:
  python scripts/write.py --date 2026-04-23
  python scripts/write.py --dry-run                  # fixture HTML, no SMTP
  python scripts/write.py --skip-send                # real Groq, no SMTP
  python scripts/write.py --plan-only                # summary only, no model call
  python scripts/write.py --use-fixture --skip-send  # smoke test (real Groq, canned session)
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jeeves.config import Config, MissingSecret  # noqa: E402
from jeeves.email import SMTPConfigError, send_html  # noqa: E402
from jeeves.session_io import load_session_by_date  # noqa: E402
from jeeves.write import (  # noqa: E402
    ASIDES_FLOOR,
    PROFANE_FRAGMENTS,
    _invoke_cerebras_narrative_edit,
    _invoke_openrouter_narrative_edit,
    _recently_used_asides,
    _write_run_manifest,
    generate_briefing,
    postprocess_html,
    render_mock_briefing,
)

log = logging.getLogger("jeeves.write")

SUBJECT_TEMPLATE = "📜 Daily Intelligence from Jeeves — {full_date}"


def _check_prior_briefing_clean(briefing_path: Path) -> tuple[bool, str]:
    """Run-dedup gate. Did an earlier run today already ship a clean briefing?

    "Clean" = signoff correct AND profane-aside count >= ASIDES_FLOOR. We
    deliberately do NOT trust the auditor JSON for this check; the auditor
    runs AFTER write commits, so it may not exist yet, and the same-day
    audit-fix may have introduced placeholder sections we don't want to
    re-ship under "already clean".

    Returns ``(is_clean, reason)``. When True the caller should skip
    generate_briefing + email and exit 0. When False the caller proceeds
    normally and (if successful) overwrites the prior briefing.

    2026-05-09: Run 1 (00:36 UTC) shipped sterile; run 2 (02:18 UTC)
    shipped clean. Both emailed. With this gate run-2 would either (a)
    skip-and-not-email if run-1 was actually clean, or (b) proceed
    normally because run-1's GATE C would have blocked it from being
    written-as-clean.
    """
    if not briefing_path.exists():
        return False, "no prior briefing on disk"
    try:
        html = briefing_path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"prior briefing unreadable: {exc}"
    if "Your reluctantly faithful Butler" not in html:
        return False, "prior briefing has wrong signoff"
    body_lower = html.lower()
    profane_count = sum(body_lower.count(frag.lower()) for frag in PROFANE_FRAGMENTS)
    if profane_count < ASIDES_FLOOR:
        return False, f"prior briefing has {profane_count} asides (floor={ASIDES_FLOOR})"
    return True, f"signoff ok, {profane_count} asides"


def _apply_asides_gate(cfg, session, result, out_path):
    """GATE C — asides-floor hard-block with one OR retry.

    When the postprocessed briefing has fewer than ``ASIDES_FLOOR`` profane
    asides, retry the OpenRouter narrative editor once on the current HTML.
    If still under floor, return ``gate_blocked=True`` so the caller exits
    non-zero before SMTP send.

    Returns ``(result, gate_blocked)``. On retry success, ``result`` is the
    re-postprocessed BriefingResult and ``out_path`` is rewritten on disk.
    """
    if result.profane_aside_count >= ASIDES_FLOOR:
        return result, False

    log.warning(
        "GATE C: asides-floor breached (%d < %d). Retrying OR narrative editor.",
        result.profane_aside_count, ASIDES_FLOOR,
    )
    recent = _recently_used_asides(cfg)
    from jeeves.write import postprocess_html as _pp

    # Tier 1 — OR retry (same provider as the original narrative-editor pass).
    try:
        retried_html = _invoke_openrouter_narrative_edit(
            cfg, result.html, recently_used_asides=recent
        )
        if retried_html and retried_html != result.html:
            result = _pp(
                retried_html, session,
                quality_warnings=list(result.quality_warnings or []),
            )
            out_path.write_text(result.html, encoding="utf-8")
            log.info(
                "GATE C [OR]: retry produced %d asides (floor=%d).",
                result.profane_aside_count, ASIDES_FLOOR,
            )
        else:
            log.warning("GATE C [OR]: retry returned unchanged HTML.")
    except Exception as exc:
        log.error("GATE C [OR]: retry raised %s: %s", type(exc).__name__, exc)

    # Tier 2 — Cerebras non-OR fallback. Only fires when OR retry didn't
    # rescue the briefing AND CEREBRAS_API_KEY is set. Different upstream
    # provider with separate quota / outage envelope.
    if result.profane_aside_count < ASIDES_FLOOR and cfg.cerebras_api_key:
        log.warning("GATE C: still below floor after OR. Trying Cerebras tier-2.")
        try:
            cerebras_html = _invoke_cerebras_narrative_edit(
                cfg, result.html, recently_used_asides=recent
            )
            if cerebras_html and cerebras_html != result.html:
                result = _pp(
                    cerebras_html, session,
                    quality_warnings=list(result.quality_warnings or []),
                )
                out_path.write_text(result.html, encoding="utf-8")
                log.info(
                    "GATE C [Cerebras]: rescued to %d asides (floor=%d).",
                    result.profane_aside_count, ASIDES_FLOOR,
                )
            else:
                log.warning("GATE C [Cerebras]: returned unchanged HTML.")
        except Exception as exc:
            log.error(
                "GATE C [Cerebras]: tier-2 raised %s: %s",
                type(exc).__name__, exc,
            )
    elif result.profane_aside_count < ASIDES_FLOOR:
        log.warning(
            "GATE C: Cerebras tier-2 unavailable (CEREBRAS_API_KEY unset); "
            "proceeding to block."
        )

    # Tier 3 — Deterministic asides injector (Patch 2, 2026-05-10). When
    # OR + Cerebras both leave the briefing sterile, splice asides from the
    # pre-approved pool into earned positions. Always rescues unless no
    # qualifying paragraphs (>50 words, outside .newyorker / .signoff)
    # exist, which only happens on cap-short briefings.
    if result.profane_aside_count < ASIDES_FLOOR:
        log.warning(
            "GATE C: still below floor after LLM retries. Trying Tier 3 "
            "deterministic injector."
        )
        try:
            from jeeves.write import _inject_asides_to_floor, postprocess_html as _pp
            new_html, injected = _inject_asides_to_floor(
                result.html,
                recently_used=recent,
                current_count=result.profane_aside_count,
                target_count=ASIDES_FLOOR,
            )
            if injected:
                result = _pp(
                    new_html, session,
                    quality_warnings=list(result.quality_warnings or [])
                    + [f"asides_floor_injected:{len(injected)}"],
                )
                out_path.write_text(result.html, encoding="utf-8")
                log.warning(
                    "GATE C [Tier 3]: deterministic injector added %d aside(s); "
                    "count now %d (floor=%d).",
                    len(injected), result.profane_aside_count, ASIDES_FLOOR,
                )
            else:
                log.warning(
                    "GATE C [Tier 3]: no qualifying paragraphs — injector "
                    "could not rescue briefing."
                )
        except Exception as exc:
            log.error(
                "GATE C [Tier 3]: injector raised %s: %s",
                type(exc).__name__, exc,
            )

    if result.profane_aside_count < ASIDES_FLOOR:
        log.error(
            "GATE C BLOCK: asides=%d still below floor=%d after retry. "
            "Briefing NOT sent. HTML kept on disk for forensic. "
            "Manual review or re-trigger required.",
            result.profane_aside_count, ASIDES_FLOOR,
        )
        return result, True
    return result, False


def _log_missing_session(cfg: "Config") -> None:
    """Emit an actionable error when no session JSON exists for cfg.run_date.

    Lists the most recent sessions on disk and tells the user what to run
    next, instead of leaving them with a bare ``No session file found`` line
    and a non-zero exit code.
    """

    sessions_dir = cfg.repo_root / "sessions"
    available: list[str] = []
    if sessions_dir.exists():
        for p in sorted(sessions_dir.glob("session-*.json"), reverse=True):
            stem = p.stem  # session-2026-05-02
            # Drop the "session-" prefix so the date is what shows.
            available.append(stem.replace("session-", "", 1))

    target = cfg.run_date.isoformat()
    log.error("No session file found for %s.", target)
    if available:
        recent = ", ".join(available[:5])
        log.error(
            "Most recent sessions on disk: %s. Pass one with `--date YYYY-MM-DD` "
            "to write a briefing from an existing session.",
            recent,
        )
    if available and target not in available:
        log.error(
            "If you want today's briefing, run the research phase first — either "
            "via the GitHub Actions workflow `Jeeves — Daily Pipeline` (which "
            "chains correspondence -> research -> write) or locally with "
            "`uv run python scripts/research.py --date %s`.",
            target,
        )
    log.error(
        "Alternatively, smoke-test the write pipeline against the canned fixture: "
        "`uv run python scripts/write.py --use-fixture --skip-send`."
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Jeeves write phase (Phase 3).")
    p.add_argument("--date", default=None, help="Session date (YYYY-MM-DD). Defaults to today UTC.")
    p.add_argument("--dry-run", action="store_true", help="Fixture HTML only; no model call or SMTP.")
    p.add_argument("--skip-send", action="store_true", help="Generate HTML but do not send email.")
    p.add_argument("--plan-only", action="store_true", help="Print session sector summary and exit.")
    p.add_argument(
        "--use-fixture",
        action="store_true",
        help="Skip loading a real session; use the canned mock payload from jeeves.testing.mocks. "
             "Useful for smoke-testing the Groq + SMTP path without running research first.",
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        default=4096,
        help="Groq max_completion_tokens per part (default 4096). "
             "Each part targets 500-900 words (~700-1200 output tokens); "
             "4096 gives a 3.4x safety margin. Daily TPD budget: "
             "9 calls x (avg ~4k input + 4096 max output) ~= 73k tokens, "
             "well within Groq's free-tier 100k/day ceiling. Also aligns "
             "with NVIDIA NIM's native 4096 output-token cap on "
             "meta/llama-3.3-70b-instruct (the NIM fallback model). "
             "max_tokens=8192 would use ~110k tokens/day for write alone.",
    )
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _plan_only(session) -> None:
    print(f"Session date: {session.date}")
    print(f"Status: {session.status}")
    print(f"Dedup URLs: {len(session.dedup.covered_urls)}")
    print(f"Local news entries: {len(session.local_news)}")
    print(f"Global news entries: {len(session.global_news)}")
    print(f"Intellectual journal entries: {len(session.intellectual_journals)}")
    print(f"Wearable AI entries: {len(session.wearable_ai)}")
    print(f"Enriched articles: {len(session.enriched_articles)}")
    print(f"Weather: {(session.weather or '')[:80]}")
    print(f"New Yorker available: {session.newyorker.available}")
    print(f"Vault insight available: {session.vault_insight.available}")


def _commit_coverage(cfg: Config, briefing_path: Path) -> None:
    """Commit the rendered briefing HTML back to GitHub so it's archived.

    Uses `git` CLI because the runner already has the checkout and GITHUB_TOKEN
    wired. Safe to skip silently if git isn't available or auth fails —
    the email has still been delivered.
    """

    try:
        import os

        env = os.environ.copy()
        subprocess.run(
            ["git", "config", "user.name", "jeeves-bot"],
            cwd=cfg.repo_root, env=env, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "jeeves-bot@users.noreply.github.com"],
            cwd=cfg.repo_root, env=env, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "add", str(briefing_path.relative_to(cfg.repo_root))],
            cwd=cfg.repo_root, env=env, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"briefing {cfg.run_date.isoformat()}"],
            cwd=cfg.repo_root, env=env, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=cfg.repo_root, env=env, check=True, capture_output=True,
        )
        log.info("briefing archived to %s", briefing_path)
    except subprocess.CalledProcessError as e:
        log.warning("failed to archive briefing (non-fatal): %s", e.stderr.decode(errors="replace")[:300])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # `dry_run` loosens env validation — treat --skip-send, --plan-only, and
    # --use-fixture the same way. Real Groq is still called for --skip-send and
    # --use-fixture, so GROQ_API_KEY must be present for those (enforced below).
    loosen_env = args.dry_run or args.skip_send or args.plan_only or args.use_fixture
    try:
        cfg = Config.from_env(
            phase="write",
            dry_run=loosen_env,
            run_date=args.date,
            verbose=args.verbose,
        )
    except MissingSecret as e:
        log.error(str(e))
        return 2

    # --skip-send and --use-fixture both invoke Groq; ensure GROQ_API_KEY is set.
    if (args.skip_send or args.use_fixture) and not args.dry_run and not cfg.groq_api_key:
        log.error("GROQ_API_KEY is required unless --dry-run is set.")
        return 2

    if args.use_fixture:
        from jeeves.schema import SessionModel
        from jeeves.testing.mocks import canned_session

        log.info("--use-fixture: loading canned mock session (no real session JSON needed).")
        session = SessionModel.model_validate(canned_session(cfg.run_date))
    else:
        try:
            session = load_session_by_date(cfg, cfg.run_date)
        except FileNotFoundError:
            _log_missing_session(cfg)
            return 3
        except ValueError as exc:
            log.error(
                "Session file for %s exists but is corrupted: %s. "
                "Inspect the JSON manually OR re-run research to regenerate.",
                cfg.run_date.isoformat(), exc,
            )
            return 4

    if args.plan_only:
        _plan_only(session)
        return 0

    # ----------------------------------------------------------------- #
    # 2026-05-09 run-dedup gate. If an earlier run today already shipped #
    # a quality-clean briefing (correct signoff AND profane asides above #
    # floor), skip-send rather than burn Groq quota on a duplicate run.  #
    # User explicit policy: only ONE scheduled run per date; manual      #
    # workflow_dispatch should override (set JEEVES_FORCE_REWRITE=1).    #
    # The dryrun / skip-send / use-fixture / plan-only modes bypass this #
    # gate so smoke tests aren't blocked.                                #
    # ----------------------------------------------------------------- #
    if not (args.dry_run or args.skip_send or args.use_fixture):
        force = os.environ.get("JEEVES_FORCE_REWRITE", "").lower() in ("1", "true", "yes")
        if not force:
            briefing_path = cfg.briefing_html_path()
            already_clean, reason = _check_prior_briefing_clean(briefing_path)
            if already_clean:
                log.info(
                    "Run-dedup: prior briefing for %s is clean (%s). "
                    "Skipping write+send. Set JEEVES_FORCE_REWRITE=1 to override.",
                    cfg.run_date.isoformat(), reason,
                )
                return 0
            elif briefing_path.exists():
                log.info(
                    "Run-dedup: prior briefing exists but quality gate failed (%s). "
                    "Proceeding with re-write.",
                    reason,
                )

    # Variables populated inside the try block; declared up here so the
    # finally clause can persist the manifest regardless of where we exit.
    result: "BriefingResult | None" = None
    _groq_parts: int = 0
    _nim_fallback_parts: int = 0

    # Patch 1 (2026-05-10) — manifest persistence via try/finally. Before
    # this wrap, _write_run_manifest only fired on the happy path: any
    # exception or non-zero return path between draft-gen and SMTP send
    # left the manifest unwritten and ALL post-run telemetry invisible
    # (banned-phrase buckets, asides_floor markers, etc). The wrap ensures
    # the manifest lands whenever ``result`` exists — exception or no.
    try:
        if args.dry_run:
            raw_html = render_mock_briefing(session)
            _quality_warnings: list[str] = []
            _groq_parts = 0
            _nim_fallback_parts = 0
            log.info("dry-run fixture briefing assembled (%d chars)", len(raw_html))
        else:
            import asyncio
            raw_html, _quality_warnings, _groq_parts, _nim_fallback_parts = asyncio.run(
                generate_briefing(cfg, session, max_tokens=args.max_tokens)
            )

        result = postprocess_html(raw_html, session, quality_warnings=_quality_warnings)

        log.info(
            "briefing: %d words, %d profane asides, %d coverage entries",
            result.word_count, result.profane_aside_count, len(result.coverage_log),
        )
        if result.banned_word_hits:
            log.warning("BANNED WORD HITS: %s", result.banned_word_hits)
        if result.banned_transition_hits:
            log.warning("BANNED TRANSITION HITS: %s", result.banned_transition_hits)
        if result.word_count < 5000 and not args.dry_run:
            log.warning("briefing is under 5000 words (%d) — model undershot.", result.word_count)
        if result.profane_aside_count < 5 and not args.dry_run:
            log.warning("fewer than 5 profane asides detected (%d).", result.profane_aside_count)

        out_path = cfg.briefing_html_path()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result.html, encoding="utf-8")
        log.info("briefing written to %s", out_path)

        if args.dry_run or args.skip_send:
            log.info("[skip-send] briefing not delivered.")
            return 0

        # ----------------------------------------------------------------- #
        # 2026-05-09 GATE C — asides-floor with deterministic injector.      #
        # When OR narrative editor skipped or stripped asides, the briefing  #
        # ships sterile (no profane asides → Wodehouse texture lost). One    #
        # retry of the narrative editor (Tier 1 OR, Tier 2 Cerebras); if     #
        # still under floor, Tier 3 invokes the deterministic asides         #
        # injector which always rescues the briefing to the floor. The       #
        # legacy hard-block path remains for the cap-rare case where even    #
        # the injector can't find qualifying paragraphs.                     #
        # ----------------------------------------------------------------- #
        result, gate_blocked = _apply_asides_gate(cfg, session, result, out_path)
        if gate_blocked:
            return 5

        full_date = datetime.strptime(cfg.run_date.isoformat(), "%Y-%m-%d").strftime(
            "%A, %B %-d, %Y"
        )
        subject = SUBJECT_TEMPLATE.format(full_date=full_date)
        try:
            send_html(
                to=cfg.recipient_email,
                sender=cfg.recipient_email,
                subject=subject,
                html=result.html,
                app_password=cfg.gmail_app_password,
            )
        except SMTPConfigError as e:
            log.error(str(e))
            return 4

        _commit_coverage(cfg, out_path)
        return 0
    finally:
        # Patch 1 finally clause — persist the run manifest whenever a
        # BriefingResult was built, regardless of subsequent exceptions or
        # early returns. Dry-runs skip persistence (no real metrics to keep).
        if result is not None and not args.dry_run:
            try:
                _write_run_manifest(cfg, result, _groq_parts, _nim_fallback_parts)
            except Exception as exc:
                # Manifest persistence is best-effort — never let a failed
                # manifest write override the briefing's exit code.
                log.error(
                    "run-manifest write failed: %s: %s",
                    type(exc).__name__, exc,
                )


if __name__ == "__main__":
    raise SystemExit(main())

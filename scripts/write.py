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
    ASIDES_TARGET,
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


# 2026-05-14 GATE-A: refuse-to-write-empty.
#
# Why: 2026-05-13 daily run produced session-2026-05-13.json with EVERY
# research sector empty (findings="", urls=[]) yet status="complete". The
# write phase, having nothing to summarise, fabricated URLs (congress.gov,
# pentagon.gov, lightmind.ai, etc.). Auditor stripped 7 hallucinated hrefs
# but the briefing still shipped with invented content under TOTT.
#
# Root cause of the empty research was a transient NIM/network blip — the
# K2.6 + FunctionAgent path was verified end-to-end on 2026-05-14 with
# probe_agent_path_v2.py (16 real tool dispatches, 0 with empty kwargs).
# This gate is the durable defense: when a transient happens again, no
# fabricated briefing ships.
#
# Policy: silence > fabrication. If every non-TOTT sector is empty, block
# the write and email. The newyorker (TOTT) and literary_pick sectors are
# fetched outside the agent path so they may be populated independently;
# they alone are NOT enough to justify shipping.
#
# Override: JEEVES_FORCE_WRITE_EMPTY=1 (for backfill/testing only).
_GATE_A_DICT_FINDINGS_SECTORS = (
    "triadic_ontology",
    "ai_systems",
    "uap",
    "career",
    "family",
)
_GATE_A_LIST_SECTORS = (
    "local_news",
    "global_news",
    "intellectual_journals",
    "wearable_ai",
    "enriched_articles",
)


def _session_research_empty(session) -> tuple[bool, str]:
    """Return ``(is_empty, summary)``.

    ``is_empty`` is True when every non-TOTT research sector returned no
    content. Excludes newyorker (TOTT) and literary_pick because both are
    fetched outside the agent path.

    Accepts either a SessionModel (pydantic) or a plain dict.
    """
    if hasattr(session, "model_dump"):
        s = session.model_dump()
    elif isinstance(session, dict):
        s = session
    else:
        s = {k: getattr(session, k) for k in dir(session) if not k.startswith("_")}

    populated: list[str] = []
    empty: list[str] = []

    for name in _GATE_A_DICT_FINDINGS_SECTORS:
        v = s.get(name) or {}
        if isinstance(v, dict):
            findings = (v.get("findings") or "").strip()
            urls = v.get("urls") or []
            if findings or urls:
                populated.append(name)
            else:
                empty.append(name)
        else:
            empty.append(name)

    for name in _GATE_A_LIST_SECTORS:
        v = s.get(name) or []
        if isinstance(v, list) and v:
            populated.append(name)
        else:
            empty.append(name)

    elp = s.get("english_lesson_plans") or {}
    if isinstance(elp, dict):
        cr = elp.get("classroom_ready") or []
        pp = elp.get("pedagogy_pieces") or []
        if cr or pp:
            populated.append("english_lesson_plans")
        else:
            empty.append("english_lesson_plans")
    else:
        empty.append("english_lesson_plans")

    w = s.get("weather") or ""
    if isinstance(w, str) and w.strip():
        populated.append("weather")
    else:
        empty.append("weather")

    summary = f"populated={populated or '(none)'}  empty={empty}"
    return (len(populated) == 0, summary)


def _apply_asides_gate(cfg, session, result, out_path):
    """GATE C — asides-target enforcement (injector-primary, 2026-05-12).

    Three-tier pipeline. The first two are ADVISORY (LLM-based, allowed to
    fail or no-op); the third is LOAD-BEARING (deterministic, always tops
    up to ASIDES_TARGET). The send is never blocked on asides anymore —
    the injector is the floor enforcer.

      Tier 1 — OpenRouter narrative-editor retry (advisory).
              Fires when result.profane_aside_count < ASIDES_FLOOR. May
              add asides to the prose; may no-op.

      Tier 2 — Cerebras narrative-editor retry (advisory).
              Fires when still below floor AND cfg.cerebras_api_key set.
              Separate upstream provider with its own quota envelope.

      Tier 3 — Deterministic injector (LOAD-BEARING).
              Runs UNCONDITIONALLY after Tiers 1+2 to top up to
              ASIDES_TARGET. Splices pool phrases into earned-anchor
              paragraphs (failure / decision / cost / deadline) outside
              the .newyorker, .signoff, and `<a>` zones.

    Returns ``(result, gate_blocked)``. ``gate_blocked`` is now ALWAYS
    False — the injector guarantees floor and the send proceeds. The
    return tuple shape is preserved for caller compatibility.

    2026-05-12 rationale: six PRs in three days fought the model's
    inability to add asides reliably. The injector (post-PR #116 hardening)
    is now reliable. Making it primary inverts the safety net: the LLM
    paths are bonus, not load-bearing.
    """
    recent = _recently_used_asides(cfg)
    from jeeves.write import postprocess_html as _pp

    # ─── Tier 1 — OR retry (advisory, only fires when below FLOOR) ──────
    if result.profane_aside_count < ASIDES_FLOOR:
        log.warning(
            "GATE C: asides-floor breached (%d < %d). Trying OR retry (advisory).",
            result.profane_aside_count, ASIDES_FLOOR,
        )
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
                    "GATE C [OR]: retry produced %d asides.",
                    result.profane_aside_count,
                )
        except Exception as exc:
            log.error("GATE C [OR]: retry raised %s: %s", type(exc).__name__, exc)

    # ─── Tier 2 — Cerebras retry (advisory) ─────────────────────────────
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
                    "GATE C [Cerebras]: produced %d asides.",
                    result.profane_aside_count,
                )
        except Exception as exc:
            log.error(
                "GATE C [Cerebras]: tier-2 raised %s: %s",
                type(exc).__name__, exc,
            )

    # ─── Tier 3 — Deterministic injector (LOAD-BEARING, always runs) ────
    # Always tops up to ASIDES_TARGET (not just to ASIDES_FLOOR). When
    # Tiers 1+2 already landed at-or-above target, this no-ops cheaply
    # (the helper returns the original html unchanged when current >=
    # target). When they didn't, this is the guarantor.
    try:
        from jeeves.write import _inject_asides_to_floor
        new_html, injected = _inject_asides_to_floor(
            result.html,
            recently_used=recent,
            current_count=result.profane_aside_count,
            target_count=ASIDES_TARGET,
        )
        if injected:
            result = _pp(
                new_html, session,
                quality_warnings=list(result.quality_warnings or [])
                + [f"asides_floor_injected:{len(injected)}"],
            )
            out_path.write_text(result.html, encoding="utf-8")
            log.info(
                "GATE C [Tier 3]: injector added %d aside(s) to reach target "
                "%d; final count %d.",
                len(injected), ASIDES_TARGET, result.profane_aside_count,
            )
        else:
            log.info(
                "GATE C [Tier 3]: no injection needed (current=%d >= target=%d) "
                "OR no qualifying paragraphs available.",
                result.profane_aside_count, ASIDES_TARGET,
            )
    except Exception as exc:
        log.error(
            "GATE C [Tier 3]: injector raised %s: %s",
            type(exc).__name__, exc,
        )

    # Below-floor outcome is now a WARNING, not a block. The send proceeds.
    # This only fires when the briefing has no qualifying paragraphs (e.g.
    # a cap-short fixture or a degenerate run) — extremely rare on real
    # production output. Telemetry still captures it via quality_warnings.
    if result.profane_aside_count < ASIDES_FLOOR:
        log.warning(
            "GATE C: final count %d still below floor %d after Tier 3 — "
            "no qualifying paragraphs. Send proceeds; briefing flagged.",
            result.profane_aside_count, ASIDES_FLOOR,
        )
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
    # 2026-05-14 GATE-A: refuse-to-write-empty. See _session_research_   #
    # empty() docstring for full rationale.                              #
    # ----------------------------------------------------------------- #
    if not (args.dry_run or args.use_fixture):
        empty_block, empty_summary = _session_research_empty(session)
        if empty_block:
            force_empty = os.environ.get(
                "JEEVES_FORCE_WRITE_EMPTY", ""
            ).lower() in ("1", "true", "yes")
            if force_empty:
                log.warning(
                    "GATE-A bypassed via JEEVES_FORCE_WRITE_EMPTY=1. %s",
                    empty_summary,
                )
            else:
                log.error(
                    "GATE-A: every non-TOTT research sector is empty for %s. "
                    "Refusing to write or send a briefing — the model would "
                    "fabricate content. %s. If transient (NIM/network blip), "
                    "re-run research. To force a write anyway (backfill / "
                    "testing), set JEEVES_FORCE_WRITE_EMPTY=1.",
                    cfg.run_date.isoformat(), empty_summary,
                )
                return 5

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

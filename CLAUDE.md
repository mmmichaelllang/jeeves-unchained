# CLAUDE.md — session memory for `jeeves-unchained`

Claude Code auto-reads this file at session start. It is the handoff between working sessions — if you're Claude and you just started a fresh session, everything below is what your previous self knew.

Full project docs (phase table, model split, flags, secrets, Gmail OAuth provisioning, schema) live in the README and are imported here:

@README.md

---

## Current focus

**Phase 4 (correspondence) end-to-end smoke against real Gmail.** Gmail OAuth works, Kimi classification is batched (PR #7), and the sweep is now **unread-only** so Groq doesn't eat a 413 on busy inboxes. Next run should land a real briefing artifact end-to-end.

## Where we left off (2026-04-23)

- `GMAIL_OAUTH_TOKEN_JSON` lives in GH Secrets. Runtime refresh works — `jeeves.gmail INFO gmail sweep` fires cleanly on every dispatch.
- **Kimi batching landed** (#7). Classify now runs `N/30` batches at ~80s each instead of one 60s-timeout request. Log signature: `classify batch 1/5 (30 msgs)` etc.
- **Sweep is unread-only + capped at 50** (this session). `sweep_recent` uses a single `is:unread newer_than:<days>d -label:spam -label:promotions` query with `max_results=50` (and `scripts/correspondence.py --max-messages` default 50). Gmail's `messages.list` returns newest-first, so the brief always covers the 50 most recent unread. Earlier unread gets silently dropped — that's intentional per user direction.
- **Render payload trimmed** (this session). `render_with_groq` now strips no_action items to `{classification, sender, subject}` only (system prompt only requires a one-line reference for those) and serializes the user payload compact (`separators=(",",":")`) instead of `indent=2`. This was the lever after 50 messages still landed at 12,993 tokens against Groq's 12k TPM ceiling — trimming no_action + compact JSON brings it comfortably under.
- Next action: re-run `correspondence.yml` on `main` (no dispatch flags, or with `skip_send=true` for a dry artifact grab). Expect a `correspondence-YYYY-MM-DD.html` artifact ≥1500 words + ≥5 profane asides + the handoff JSON committed back for the 12:30 UTC research cron to pick up.

## Dev branch

- **Current**: `claude/gmail-auth-bootstrap-9eYme`
- Prior major work merged from: `claude/jeeves-unchained-rewrite-auKzK` (see #5)

## Gotchas the README doesn't flag

- **`--dry-run` vs `--use-fixture` on `scripts/correspondence.py`** — both checkboxes exist in `workflow_dispatch`. `--dry-run` short-circuits to a static HTML template (no Kimi, no Groq, no Gmail); `--use-fixture` uses a canned inbox but still calls the real models. If both are ticked, dry-run wins (`scripts/correspondence.py:63`). To smoke-test the real model path from the UI: tick **only** `use_fixture` + `skip_send`.
- **Profane butler asides are intentional.** The Groq system prompt (`jeeves/prompts/correspondence_write.md:18-22`) mandates ≥5 slips per briefing from a pre-approved list ("clusterfuck of biblical proportions, Sir", "fucking disaster-class", etc.). Do not sanitize these — post-processing counts them and warns if the briefing has fewer than 5.
- **`(DRY RUN)` in the `<h1>` is a tell.** Only `render_mock_correspondence()` (`jeeves/correspondence.py:396-416`) hardcodes that suffix. If you see it in the artifact, the run took the dry-run branch regardless of what you thought you clicked.
- **Artifact naming convention.** `sessions/*.local.json` and `*.local.html` are gitignored dry-run artifacts. `sessions/session-*.json`, `sessions/correspondence-*.json`, `sessions/briefing-*.html` are the real ones that the workflows commit back to the repo.
- **The Phase 4 handoff JSON is consumed by Phase 2.** `correspondence.yml` runs first in the daily chain (cron `0 12 * * *`), committing `sessions/correspondence-<date>.json`. `research.yml` (`30 12 * * *`) picks it up into `session.correspondence`. Don't break the file name / schema contract without updating both sides.

## Quick nav (file:line pointers)

- `scripts/correspondence.py:59` — `_run` mode dispatch (dry-run / use-fixture / real Gmail)
- `scripts/correspondence.py:97` — `main` + flag parsing + artifact writes
- `jeeves/correspondence.py:396` — `render_mock_correspondence` (the dry-run template)
- `jeeves/correspondence.py` — `classify_with_kimi`, `render_with_groq`, `postprocess_html`, `build_handoff_json`
- `jeeves/prompts/correspondence_write.md` — Groq system prompt (persona, slip list, HTML scaffold, banned words)
- `scripts/gmail_auth.py` — one-shot OAuth flow that mints `GMAIL_OAUTH_TOKEN_JSON`
- `jeeves/gmail.py:41` — `build_gmail_service` (consumes the token JSON, auto-refreshes)
- `.github/workflows/correspondence.yml` — cron + `workflow_dispatch` inputs (`date`, `skip_send`, `use_fixture`, `dry_run`)
- `jeeves/config.py` — `Config.from_env()`, `MissingSecret`
- `jeeves/schema.py` — `SessionModel` + `FIELD_CAPS`

## Session hygiene

Before ending a session where the project state meaningfully advanced (new phase, new branch, new gotcha, pipeline behavior changed), update the **Current focus** and **Where we left off** blocks above. That's the whole mechanism — no hooks, no scripts, just keep this file honest.

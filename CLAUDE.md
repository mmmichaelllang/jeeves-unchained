# CLAUDE.md — session memory for `jeeves-unchained`

Claude Code auto-reads this file at session start. It is the handoff between working sessions — if you're Claude and you just started a fresh session, everything below is what your previous self knew.

Full project docs (phase table, model split, flags, secrets, Gmail OAuth provisioning, schema) live in the README and are imported here:

@README.md

---

## Current focus

**Phase 4 (correspondence) Gmail OAuth bootstrap verification.** Token has been minted locally via `scripts/gmail_auth.py` and pasted into the GitHub secret `GMAIL_OAUTH_TOKEN_JSON`. We're smoke-testing the pipeline via the fixture path before letting it run against real Gmail on the daily cron.

## Where we left off (2026-04-23)

- `GMAIL_OAUTH_TOKEN_JSON` is present in `mmmichaelllang/jeeves-unchained` repo secrets.
- First `workflow_dispatch` run of `correspondence.yml` came back with the dry-run mock HTML (the `render_mock_correspondence()` template — title literally contains `(DRY RUN)`, ~100 words, static slips). Root cause: `dry_run=true` was ticked in the dispatch dialog alongside `use_fixture=true`; dry-run wins in `scripts/correspondence.py:63-71`.
- Next action: re-trigger `correspondence.yml` with **only** `use_fixture=true` and `skip_send=true` (leave `dry_run=false`). Expected artifact: `correspondence-html` with ≥1500 words, ≥5 profane asides, no banned words/transitions (real Kimi + Groq path).
- After the fixture path looks right, next milestone is letting the 12:00 UTC cron run unattended against the real Gmail inbox and verifying the handoff JSON lands in `sessions/correspondence-YYYY-MM-DD.json`.

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

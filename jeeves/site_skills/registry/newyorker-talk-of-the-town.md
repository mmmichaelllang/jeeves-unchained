---
name: newyorker-talk-of-the-town
title: The New Yorker — Talk of the Town
description: Fetch the newest Talk of the Town article not already covered. Backed by jeeves/tools/newyorker.py and surfaced as fetch_new_yorker_talk_of_the_town().
sectors: [newyorker]
hosts: [newyorker.com]
status: seed-2026-05-09
---

## Purpose

The newyorker sector exists for ONE reason: load this week's Talk of the
Town article verbatim into PART9. Verbatim is non-negotiable — the model
MUST NOT paraphrase. The verbatim text is spliced into the briefing by
`_inject_newyorker_verbatim()` in `jeeves/write.py`.

## When to use

- The `newyorker` sector's IMMEDIATE FIRST ACTION (and only action).
- DO NOT use any other tool for this sector — no `tavily_extract`, no
  `serper_search`, no `exa_search`. The agent regularly hallucinates
  paragraphs when allowed to call generic search tools, then the
  verbatim-mandate fails downstream.

## Workflow

1. Call `fetch_new_yorker_talk_of_the_town()` exactly once.
2. Return the result verbatim as a JSON object. The fields are:
   `{available, title, section, dek, byline, date, text, url, source}`.
3. If `available=false`, return the object as-is — PART9 has a routing
   branch for that case.

## Site-specific gotchas

- **JS-heavy SPA**: newyorker.com is fully JavaScript-rendered. Generic
  fetchers (httpx + trafilatura) return empty bodies. The sector helper
  uses `playwright_extractor` under the hood with a known-good selector.
- **Paywall regression**: when newyorker rotates a soft paywall, the
  helper's selector breaks silently. Check
  `jeeves/tools/newyorker.py:fetch_talk_of_the_town` and the
  `playwright_extractor.py` first-context route-block list.
- **Dedup is title-based**: the helper picks the newest article whose
  title is NOT in the prior-coverage set. Title drift between fetches
  (rare but observed when the editorial desk re-titles within 24h)
  produces a duplicate ship; the helper's headline-canonicalization
  pass strips trailing whitespace, hash chars, and emoji.
- **Body length cap**: PART9 caps `newyorker.text` at the briefing-side
  field cap (see `FIELD_CAPS["newyorker.text"]`). The helper truncates
  cleanly at the latest paragraph boundary that fits.

## Diagnostic markers in run-manifest

- `part9_tott_scaffolding_injected` — model dropped the intro/placeholder;
  postprocess injected the scaffolding so verbatim splicing works.
- `newyorker_unavailable` (set in session JSON if the helper returned
  `available=false`) — common cause: helper timed out OR newyorker
  redesigned the index page.

## Empty-feed protocol

If the helper returns `available=false`, do NOT fall through to a generic
search. PART9's `available=false` branch handles the absence cleanly with
a one-sentence note. Generic-search fallback would drag in unrelated
newyorker articles and fail the verbatim mandate.

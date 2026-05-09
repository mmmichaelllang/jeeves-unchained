---
name: myedmondsnews-local-news
title: My Edmonds News — Local Council, Schools, Public Safety
description: Edmonds-area municipal and public-safety reporting. Daily-cadence local desk. Use as the primary anchor for the local_news sector.
sectors: [local_news]
hosts: [myedmondsnews.com]
status: seed-2026-05-09
---

## Purpose

Surface today's Edmonds municipal news (city council, permits, schools, transit)
plus the geofenced public-safety items (homicide, major assault, armed
incident, missing persons within 3 miles of 47.810652, -122.377355).

## When to use

- The `local_news` sector's first action — myedmondsnews is the only
  high-cadence Edmonds-specific desk.
- Council vote / permit / wastewater / police-policy stories.
- NEVER use for cross-state or national stories. Defer to BBC / Guardian / AP.

## Workflow

The site is a standard WordPress install: server-rendered HTML, no
auth, no anti-bot. `tavily_extract` returns clean article body text. Headline
listings are reachable via the homepage and category archives.

1. **Today's headlines** — call:

       serper_search(query="myedmondsnews.com", tbs="qdr:d")

   Returns a ranked list of myedmondsnews URLs published in the last 24h.
   Filter to canonical article URLs (path matches `/2026/MM/`); ignore
   `/category/`, `/author/`, `/page/N/` archive pages.

2. **Read full text** — for the top 4-6 article URLs, batch through
   `tavily_extract`. Body text comes back clean — no JS render needed.

3. **Geofence pass** — for any "police" or "incident" article, scan the body
   for street addresses or named cross streets. If the location is OUTSIDE the
   3-mile radius from (47.810652, -122.377355), DROP the item. The article
   itself does not include a structured location — geofencing is by
   address-string match.

## Site-specific gotchas

- **Comments at end of body**: tavily_extract sometimes includes the
  reader-comments block. Truncate body at the first `</article>` or before
  any `<h2>Comments</h2>` heading.
- **Author bylines repeat**: same author posts 4-6 stories per day. The
  source-rotation rule still applies — pick the most consequential, not all
  six.
- **No formal RSS** — there IS a feed at `/feed/` but tavily_extract does not
  parse it cleanly. Stick with the search-then-extract path above.
- **Headline drift**: the site re-titles articles within the first few
  hours of publication. Cross-day dedup MUST match on slug (last URL path
  segment), not on title.

## Items already over-shipped (skip-list)

These have shipped 3+ days running through 2026-05-09. If the daily search
still returns one of these as a top hit, run a NARROWER follow-up search
(`tbs=qdr:d` with a more specific query) to find a fresher item:

- City Council vote on Stephanie Lucash as city administrator
- Edmonds Wastewater Treatment Plant carbon-recovery system delays
- Edmonds Police Chief Loi Dawkins policy-improvement deadline
- Comprehensive Safety Action Plan adoption

## Empty-feed protocol

If the day's search returns zero new articles within geofence + recency, the
correct local_news output is the wider-net fallback (Snohomish County /
HeraldNet) per the sector instruction. Do NOT pad with stale myedmondsnews
items.

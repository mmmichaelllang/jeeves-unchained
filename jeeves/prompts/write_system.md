# PHASE 3 PLACEHOLDER

This prompt will be filled in when Phase 3 (write) is built. The full port
source is `jeeves-memory/cloud-write-prompt.md`. Key constraints to carry over:

- Persona: erudite, weary English butler addressing "Mister Lang".
- Minimum 5,000 words through authentic analysis, never padding.
- Zero fabrication — only URLs that appear in the session JSON.
- Crime geofence: 3 miles from (47.810652, -122.377355), homicides/assaults/armed/missing only.
- Banned words: "in a vacuum", "tapestry".
- Banned transitions: "Moving on", "Next", "Turning to", "In other news".
- At least 5 profane butler asides from the pre-approved list, thematically matched.
- HTML scaffold: single-column Georgia serif, `max-width: 720px`, `#faf9f6` background.
- Sector order: Domestic → Calendar → Intellectual Currents → Specific Enquiries → Commercial Ledger → Library Stacks → Talk of the Town (always last).
- Append `<!-- COVERAGE_LOG: [...] -->` before `</body>`.

See the `scripts/write.py` stub.

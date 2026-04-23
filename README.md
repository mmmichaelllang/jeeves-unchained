# jeeves-unchained

Python + LlamaIndex rewrite of the [jeeves-memory](https://github.com/mmmichaelllang/jeeves-memory) newsletter pipeline.

**Model split**

| Phase | Model | Purpose |
|---|---|---|
| Research (Phase 2) | Kimi K2.5 on NVIDIA NIM | tool dispatch, parallel search, structured JSON emission |
| Write (Phase 3) | Groq Llama 3.3 70B (`llama-3.3-70b-versatile`) | Jeeves voice prose, HTML render, SMTP send |
| Correspondence (Phase 4) | Kimi K2.5 + Gmail OAuth | thread classification + daily correspondence brief |

## Current status

- **Phase 1 scaffold**: done — `jeeves/` package, `scripts/` entrypoints, three workflows (research live, write/correspondence stubbed), CI with pytest + dry-run smoke.
- **Phase 2 research**: end-to-end Python implementation using LlamaIndex `FunctionAgent`, Kimi K2.5 on NVIDIA NIM, and four search providers (Serper, Tavily, Exa, Gemini grounded) with a quota-aware monthly ledger.
- **Phase 3 write**: stub only. `scripts/write.py --plan-only` loads the session JSON and prints a sector summary to prove contract stability.
- **Phase 4 correspondence**: stub only.

## Quickstart — local dry run

```bash
uv sync --all-extras
cp .env.example .env            # optional for dry-run
uv run python scripts/research.py --dry-run --date 2026-04-23
uv run python scripts/write.py --date 2026-04-23 --plan-only
uv run pytest -q
```

The dry-run uses fixture data (no network), writes to `sessions/session-2026-04-23.local.json`, and validates the result against `SessionModel`.

## Real run

Set the secrets listed below, then:

```bash
uv run python scripts/research.py --limit 1 --sectors local_news --date 2026-04-23
```

`--limit` keeps search budgets small while you're iterating. Drop the flags to do a full run.

## Secrets

All secrets live in GitHub Secrets and are passed to workflows via `env:` blocks. Locally, drop them in `.env` (loaded by `python-dotenv`).

| Variable | Used by | Notes |
|---|---|---|
| `NVIDIA_API_KEY` | Research (Kimi K2.5 on NIM) | https://build.nvidia.com |
| `SERPER_API_KEY` | Research — Serper.dev | 2,500 free/month, then $0.30/1k |
| `TAVILY_API_KEY` | Research — Tavily search + extract | 1,000 free/month |
| `EXA_API_KEY` | Research — Exa neural search | |
| `GOOGLE_API_KEY` | Research — Gemini grounded; also Gmail API in P4 | |
| `GROQ_API_KEY` | Write (Groq Llama 3.3 70B) | P3 |
| `GMAIL_APP_PASSWORD` | Write (SMTP send) | P3 |
| `GMAIL_OAUTH_CLIENT_JSON` | Correspondence (Gmail sweep) | P4 |
| `GITHUB_TOKEN` | Committing session JSON + quota state | Auto-provided in Actions |
| `GITHUB_REPOSITORY` | Repo coordinates for session writes | Auto in Actions; set locally |

Model IDs can be overridden via `KIMI_MODEL_ID` and `GROQ_MODEL_ID`.

## Directory layout

```
jeeves-unchained/
├── scripts/
│   ├── research.py         # Phase 2 (live)
│   ├── write.py            # Phase 3 (stub)
│   └── correspondence.py   # Phase 4 (stub)
├── jeeves/
│   ├── config.py           # Config.from_env()
│   ├── schema.py           # SessionModel + per-field caps
│   ├── session_io.py       # load/save + GitHub commit
│   ├── dedup.py            # covered URL/headline sets
│   ├── llm.py              # Kimi + Groq factories
│   ├── prompts/
│   │   ├── research_system.md
│   │   └── write_system.md
│   ├── tools/
│   │   ├── quota.py
│   │   ├── serper.py
│   │   ├── tavily.py
│   │   ├── exa.py
│   │   ├── gemini_grounded.py
│   │   ├── enrichment.py
│   │   ├── talk_of_the_town.py
│   │   └── emit_session.py
│   └── testing/
│       └── mocks.py
├── sessions/               # session-YYYY-MM-DD.json committed daily
├── .quota-state.json       # month-to-date provider usage
├── tests/
└── .github/workflows/
    ├── research.yml        # cron '30 12 * * *'
    ├── write.yml           # cron '40 13 * * *'
    ├── correspondence.yml  # cron '0 12 * * *'
    └── ci.yml
```

## Session JSON schema

Mirrors the existing `jeeves-memory` shape so either pipeline's write phase can consume either's research output.

Top-level fields: `date, status, dedup, correspondence, weather, local_news, career, family, global_news, intellectual_journals, wearable_ai, triadic_ontology, ai_systems, uap, newyorker, vault_insight, enriched_articles`.

Per-field truncation caps live in `jeeves/schema.py::FIELD_CAPS` and are applied automatically inside `emit_session`.

## Phase 2 architecture (research)

1. `Config.from_env()` collects every secret; fails fast listing all missing names at once.
2. `load_previous_session()` finds the most recent session on disk (looks back up to 7 days) and extracts `dedup.covered_urls`.
3. `QuotaLedger` loads `.quota-state.json` (month-to-date per-provider usage). Exhausted providers are skipped by `cheapest_with_capacity()`.
4. `FunctionAgent` is built with:
   - `jeeves.llm.build_kimi_llm` → LlamaIndex `NVIDIA` LLM on `https://integrate.api.nvidia.com/v1`
   - `jeeves.tools.all_search_tools` → Serper, Tavily search+extract, Exa, Gemini grounded, trafilatura, Talk-of-the-Town fetcher
   - `emit_session` terminator tool — validates the payload against `SessionModel` and stops the agent
5. The agent runs the system prompt in `jeeves/prompts/research_system.md`, filling each sector via parallel tool calls, then calls `emit_session(session_json=…)` exactly once.
6. `save_session` truncates per-field caps, writes the local file, and `PUT`s the content to GitHub via the Contents API.

## Exit criteria

- Phase 2 done when: `scripts/research.py` produces a `sessions/session-YYYY-MM-DD.json` on `main` that validates against `SessionModel`, `scripts/write.py --plan-only` reads it cleanly, and `research.yml` runs green two consecutive days with distinct URL sets.

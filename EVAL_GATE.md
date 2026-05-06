# EVAL_GATE — TinyFish promotion criteria

Sprint-18 rollout. Run `scripts/eval_extractors.py` against
`tests/fixtures/extractor_eval_set.yaml` before flipping any of:

- `JEEVES_TINYFISH_SHADOW=1` → `JEEVES_USE_TINYFISH=1` on a single sector.
- Single-sector canary → fetch-chain step 3 across all sectors.
- Manual flag-gated → default-on (i.e. removing the env-var guard entirely).

## Promotion thresholds

A row in the harness output ships TinyFish forward only when ALL hold:

| Metric                     | Pass criterion                                                  |
| -------------------------- | ---------------------------------------------------------------- |
| `success_rate(tinyfish)`   | ≥ `success_rate(playwright) − 5pp` over the 15-URL fixture       |
| `content_recall(tinyfish)` | mean ≥ `content_recall(playwright) + 0.05` (5pp absolute lift)  |
|                            | OR `p95_latency(tinyfish) ≤ 0.7 × p95_latency(playwright)` with  |
|                            | recall regression ≤ 0.02 (latency-win path)                      |
| `cost_usd/day(tinyfish)`   | ≤ $5 projected (30 calls × per-call rate)                        |
| Shadow disagreement rate   | < 20% on `text_sha16` with both `success=true` (sanity check)    |

Either the recall-win path OR the latency-win path qualifies — not both
required. The 5pp success-rate floor is a hard gate regardless.

## Rollback triggers (auto-revert via flag flip)

- TinyFish success rate drops > 5pp below playwright over any 100-call
  rolling window (read from `.quota-state.json` + shadow jsonl).
- Daily TinyFish spend > $5.
- p95 latency > 20s on the canary sector for two consecutive runs.
- Sector-level quota guard rejection rate climbs > 2× the trailing
  7-day baseline.
- Any TinyFish call returns content failing UTF-8 decode or contains
  literal model-hallucination phrases tracked in `quality_warnings`.

Manual revert: set repo Variable `JEEVES_USE_TINYFISH=` (empty) — no
code change, no redeploy. Shadow flag flip works the same way.

## How to refresh the fixture

`golden_text` fragments are the load-bearing part of the harness. After
each fixture refresh:

1. Run all three extractors with `--out sessions/eval-tinyfish-bootstrap.csv`.
2. Open the CSV and pick five short, distinctive fragments from whichever
   extraction looks most complete (typically playwright on hard cases,
   httpx on easy ones).
3. Paste them into `golden_text` in the YAML.
4. Re-run; recall should now be > 0.6 for at least one extractor on every
   case. If a row stays at 0 across the board, the URL is dead — replace
   it with a fresh one in the same category.

# JEEVES-UNCHAINED — Adaptive Loop ROADMAP
# Place at: /Users/frederickyudin/jeeves-unchained/ROADMAP.md
# Driven by: .claude/loop.md (adaptive goal loop)
# Each milestone: verifiable via command or file check

---

## Milestones

### TIER 0 — Diagnosis (must complete before any code changes)

- [x] M0-A: Pull run #70 research log and identify failure pattern
  DONE WHEN: `/tmp/run70-research.log` exists AND grep output shows one of: `circuit_breaker_trip`, `429` cascade pattern, or zero `[telemetry] tool_call` lines
  VERIFY: `wc -l /tmp/run70-research.log && grep -c "429\|circuit_breaker\|tool_call\|spec.default" /tmp/run70-research.log`

- [x] M0-B: Inspect .quota-state.json daily section after run #70
  DONE WHEN: quota state retrieved AND dominant hypothesis documented in LOOP_STATE.md under "Research Diagnosis"
  VERIFY: `cat /Users/frederickyudin/jeeves-unchained/.quota-state.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('daily',{}))"` — serper/tavily/exa counts inspected

---

### TIER 1 — Empty-Research Fix (blocked on M0 diagnosis)

- [x] M1-A [PATH A]: Lower NIM circuit breaker threshold + add pre-flight health probe
  PREREQUISITE: M0 diagnosis points to NIM 429 cascade (hypothesis #1)
  DONE WHEN: `pytest tests/test_research_circuit_breakers.py` exits 0 AND `scripts/research.py` has pre-flight probe at top of main() AND breaker threshold lowered to 1
  VERIFY: `cd /Users/frederickyudin/jeeves-unchained && python -m pytest tests/test_research_circuit_breakers.py -v 2>&1 | tail -20`

- [ ] M1-B [PATH B]: Cerebras-as-researcher eval harness
  PREREQUISITE: M0 diagnosis points to tool-dispatch failure (hypothesis #2)
  DONE WHEN: `scripts/eval_tool_dispatch.py` exists AND runs against CEREBRAS_API_KEY AND produces pass/fail table comparing tool-dispatch rate vs K2.6
  VERIFY: `cd /Users/frederickyudin/jeeves-unchained && python scripts/eval_tool_dispatch.py --dry-run 2>&1 | tail -20`

- [ ] M1-C: Verify fix — trigger manual research run and confirm non-empty session
  DONE WHEN: `gh run view --json status,conclusion -R mmmichaelllang/jeeves-unchained` shows research job `conclusion=success` AND `sessions/session-$(date +%Y-%m-%d).json` has ≥1 non-empty sector
  VERIFY: `cat /Users/frederickyudin/jeeves-unchained/sessions/session-$(date +%Y-%m-%d).json | python3 -c "import json,sys; d=json.load(sys.stdin); print({k:len(v) for k,v in d.items() if isinstance(v,list) and v})"`

---

### TIER 2 — Write Quality (quality-sprint.md items)

- [ ] M2-A: Raise DEDUP_PROMPT_HEADLINES_CAP 150→250 and add within-run topic tracking
  DONE WHEN: `grep "DEDUP_PROMPT_HEADLINES_CAP = 250" jeeves/write.py` exits 0 AND `_extract_written_topics` function exists in write.py AND `used_topics_this_run` passed to `_system_prompt_for_parts`
  VERIFY: `cd /Users/frederickyudin/jeeves-unchained && grep -n "DEDUP_PROMPT_HEADLINES_CAP\|used_topics_this_run\|_extract_written_topics" jeeves/write.py | head -20`

- [ ] M2-B: Cross-sector URL dedup pass in research_sectors.py + schema field
  DONE WHEN: `_find_cross_sector_dupes()` exists in research_sectors.py AND `cross_sector_dupes` field in DeduplicateModel in schema.py AND `pytest tests/test_research_sectors.py` exits 0
  VERIFY: `cd /Users/frederickyudin/jeeves-unchained && grep -n "cross_sector_dupes\|_find_cross_sector_dupes" jeeves/research_sectors.py jeeves/schema.py`

- [ ] M2-C: Harden PART8 Library Stacks + add 12 banned filler phrases to _REFINE_SYSTEM
  DONE WHEN: `grep -c "treasure trove\|FORBIDDEN OUTPUTS" jeeves/write.py` returns ≥2 (both the banned phrase and the FORBIDDEN block) AND `pytest tests/test_write_postprocess.py` exits 0
  VERIFY: `cd /Users/frederickyudin/jeeves-unchained && grep -n "FORBIDDEN OUTPUTS\|treasure trove\|commitment to providing" jeeves/write.py | head -10`

- [ ] M2-D: Deterministic banner injection + TOTT field cap fix (schema.py 4000→40000)
  DONE WHEN: `_inject_banner()` function exists in write.py AND `grep "newyorker.text.*40000\|40000.*newyorker" jeeves/schema.py` returns a match AND `pytest tests/` exits 0
  VERIFY: `cd /Users/frederickyudin/jeeves-unchained && grep -n "_inject_banner\|FIELD_CAPS\[.newyorker" jeeves/write.py jeeves/schema.py | head -10`

- [ ] M2-E: All quality tests pass + write 6 new tests from handoff-quality-sprint.md
  DONE WHEN: `pytest tests/ -v 2>&1 | grep -E "passed|failed"` shows 0 failures AND test count ≥40 total
  VERIFY: `cd /Users/frederickyudin/jeeves-unchained && python -m pytest tests/ --tb=short 2>&1 | tail -5`

---

### TIER 3 — GHA Infrastructure

- [ ] M3-A: Per-job timeouts in daily.yml (correspondence=10m, research=120m, write=30m)
  DONE WHEN: `grep "timeout-minutes" .github/workflows/daily.yml | wc -l` ≥3 AND values match spec
  VERIFY: `grep -A2 "timeout-minutes" /Users/frederickyudin/jeeves-unchained/.github/workflows/daily.yml`

- [ ] M3-B: Downgrade mid-stream "None/empty arguments" warnings to DEBUG in llm.py
  DONE WHEN: `grep -n "None/empty arguments\|coercing to {}" jeeves/llm.py` shows log.debug not log.warning
  VERIFY: `grep -n "warning.*empty arguments\|debug.*empty arguments" /Users/frederickyudin/jeeves-unchained/jeeves/llm.py`

---

### TIER 4 — Skill Sprint T1 (reliability)

- [ ] M4-A: Apply tool-use-guardian patterns to _try_normalize_json + NIM 429 backoff
  DONE WHEN: `pytest tests/ -k "json_repair or nim or groq" -v` exits 0 AND structured retry wrapper exists
  VERIFY: `cd /Users/frederickyudin/jeeves-unchained && python -m pytest tests/ -k "json_repair or nim or groq" -v 2>&1 | tail -20`

- [ ] M4-B: Apply async-python-patterns — replace threading.Thread+sleep with asyncio in write.py
  DONE WHEN: `grep "asyncio.TaskGroup\|asyncio.gather" jeeves/write.py` returns matches AND `_SECTOR_SEMAPHORE=1` preserved AND `pytest tests/` exits 0
  VERIFY: `cd /Users/frederickyudin/jeeves-unchained && grep -n "asyncio.TaskGroup\|_SECTOR_SEMAPHORE" jeeves/write.py jeeves/research_sectors.py | head -10`

---

### TIER 5 — Skill Sprint T2 (quality + eval)

- [ ] M5-A: eval_briefing.py — scores last 3 briefings for anti-patterns
  DONE WHEN: `python scripts/eval_briefing.py` runs end-to-end without error AND outputs pass/fail table for ≥3 briefing files
  VERIFY: `cd /Users/frederickyudin/jeeves-unchained && python scripts/eval_briefing.py 2>&1 | tail -20`

- [ ] M5-B: Context optimization — reduce Part 4+ system prompt by ≥800 tokens
  DONE WHEN: dry write run logs show Part 4 input_tokens reduced ≥800 vs baseline
  VERIFY: `cd /Users/frederickyudin/jeeves-unchained && JEEVES_DRY_RUN=1 python scripts/write.py 2>&1 | grep "input_tokens" | head -10`

---

### TIER 6 — End-to-End Verification

- [ ] M6: Full pipeline confirmed — research → write → email delivered
  DONE WHEN: A GHA run (scheduled or manual) shows all three jobs (correspondence,
  research, write) conclusion=success on the same run AND lang.mc@gmail.com receives
  a non-empty briefing with ≥3 populated sectors.
  VERIFY: `gh run list -R mmmichaelllang/jeeves-unchained --limit 3 --json databaseId,conclusion,status --jq '.[]'`
  NOTE: Email confirmation requires human check. If GHA jobs are all green but email
  unverified, set next_priority to "M6 pending — user must confirm email received at
  lang.mc@gmail.com. Check inbox then clear this next_priority to declare SUCCESS."

---

## Project Complete When
All milestones show `- [x]` AND daily.yml scheduled run at 12:00 UTC produces
a non-empty briefing emailed to lang.mc@gmail.com with ≥3 populated sectors.

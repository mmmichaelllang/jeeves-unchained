# F-007 — Port validator to all LLM-backed fix paths

Branch: `fix/F-007-validator-coverage` (off `origin/main` after F-001 merged as PR #96 / 64b6066)
Origin: forensic audit 2026-05-06 (`reports/00-master-findings.md` §F-007)
Status: planning → TDD red → green → commit

## Scope

F-001 wired `_validate_audit_model_output` into `fix_empty_with_data` (F6) only. The same vulnerability — splice raw LLM text with no structural validation — exists in every other LLM-backed fix path. F-007 ports the validator to all of them.

Survey of `_call_audit_model` callers (current `scripts/audit_fix.py`):

| Function | Line | Status |
|---|---|---|
| `fix_empty_with_data` (F6) | 568 | Wired in F-001 ✓ |
| `fix_greeting_incomplete` (F7) | 661 | **F-007 target** |

Module docstring lists F8 (`rewrite_low_quality_section`) and F9 (`polish_narrative_flow`) as future LLM fixes, but neither is implemented in current code. F-007 closes the present gap; if F8/F9 land later they MUST call the validator before splice as part of their own implementation.

## Wiring shape

Identical pattern to F-001 — between `_call_audit_model` return and the existing falsy guard. Validator parameters use defaults (`expect_html_paragraph=True`, `min_words=30`, `max_words=400`); greeting prompt asks for 80-150 words so defaults bracket the range with margin.

## Test (TDD red → green → restore)

`tests/test_audit_fix_validator.py` — extend with two new integration tests:

1. `test_fix_greeting_incomplete_rejects_cot_output` — stub `_call_audit_model` to return greeting-shape CoT. Assert no CoT in resulting HTML, FixAction `status="failed"`, `detail` contains `"validator rejected"`, original placeholder preserved.

2. `test_fix_greeting_incomplete_accepts_clean_output` — clean 47-word `<p>...</p>` greeting. FixAction `status="applied"`, original placeholder replaced.

Existing test `test_audit_fix.py:test_f7_rerenders_greeting_via_stub` uses an 8-word stub that trips the new floor — bumped to ~50 words preserving its assertion intent.

## Acceptance

- `tests/test_audit_fix_validator.py`: 8 (existing) + 2 (new) = 10 pass
- `tests/test_audit_fix.py`: all existing tests still green after stub bump
- Restore proof: temporarily remove F-007 wiring → new `test_fix_greeting_incomplete_rejects_cot_output` fails → re-apply → passes

## PR push path

Branch `fix/F-007-validator-coverage` off `origin/main`. Files:
- `scripts/audit_fix.py` (~17 lines added in `fix_greeting_incomplete`)
- `tests/test_audit_fix_validator.py` (~120 lines added)
- `tests/test_audit_fix.py` (~9 line stub bump)
- `.claude/plans/F-007-validator-coverage.md` (this file)

PAT scope check: `scripts/` + `tests/` only — standard `repo` scope sufficient.

# F-001 — Audit-fix output validator

Branch: `fix/F-001-audit-fix-output-validation` (off `main`)
Origin: forensic audit 2026-05-06 (`reports/00-master-findings.md` §F-001)
Status: planning → TDD red

## Defect

`scripts/audit_fix.py:fix_empty_with_data` (lines 417–516) calls `_call_audit_model` and splices the returned `text` directly into the briefing HTML between an `<h3>` and the next section boundary. The only check is `if not text or not model: continue` — a non-empty falsy guard. Reasoning models (nemotron-3-super-120b, deepseek-r1, openai-o1) emit chain-of-thought ("We need to produce a paragraph...", "Word count: counting now") that passes the falsy check trivially. Today's `briefing-2026-05-06.html` lines 60–77 contain ~2106 chars of nemotron's planning prose between `<h3>Talk of the Town</h3>` and the body.

## Threat model

Three failure shapes the validator must reject:

1. **Pure CoT, no HTML.** Model returns "We need to produce a paragraph. Word count: 60-180. Let me start..." with no `<p>` tag. Currently spliced as-is, breaks rendering.
2. **CoT-then-HTML.** Model returns reasoning prose followed by `<p>...</p>`. CoT prefix gets spliced above the paragraph; visible to reader.
3. **HTML-then-CoT.** Model returns valid `<p>...</p>` followed by stray meta-comment ("That should be 78 words. Let me adjust..."). Less common but possible.

## Validator contract

```python
def _validate_audit_model_output(
    text: str,
    *,
    expect_html_paragraph: bool = True,
    min_words: int = 30,
    max_words: int = 400,
) -> tuple[bool, str]:
    """Return (ok, reason). ok=True means safe to splice."""
```

Rules (in order):

1. **Strip whitespace.** Empty after strip → `(False, "empty after strip")`.
2. **First-tag check.** If `expect_html_paragraph`: regex `^<(p|div|h[2-6])\b` must match first non-whitespace token. Reasoning prefix fails this. → `(False, "non-html prefix: <first 60 chars>")`.
3. **Word count.** Strip HTML tags, count words. If `<min_words` or `>max_words`: → `(False, "word count: N")`.
4. **CoT marker scan** (defense-in-depth). Scan the *full* text (not just the prefix) against a curated list: `we need to produce`, `word count:`, `let me start`, `let me think`, `let's count`, `step 1:`, `first, i'll`, `i'll write`, `i need to`, `the user wants`. Case-insensitive. Any hit → `(False, "cot marker: <phrase>")`.
5. Otherwise → `(True, "ok")`.

CoT scan covers case 3 (HTML-then-CoT) which the first-tag check misses.

## Wiring

In `fix_empty_with_data` between line 478 (`text, model = _call_audit_model(...)`) and the existing falsy guard:

```python
text, model = _call_audit_model(prompt, system=system, max_tokens=600)
ok, reason = _validate_audit_model_output(text)
if not ok:
    actions.append(FixAction(
        type="rerender_empty_with_data",
        section=section_name,
        detail=f"validator rejected: {reason}",
        status="failed",
        evidence={"model": model or "", "preview": (text or "")[:120]},
    ))
    continue
if not text or not model:  # legacy guard kept defensively
    ...
```

## Test (TDD red → green → restore)

`tests/test_audit_fix_validator.py` — three tests:

1. `test_validator_rejects_pure_cot` — direct unit: validator on "We need to produce a paragraph. Word count: 60." returns `(False, ...)`.
2. `test_validator_rejects_cot_prefix_then_html` — direct unit: "We need to produce. <p>real para…</p>" returns `(False, "non-html prefix: ...")`.
3. `test_fix_empty_with_data_skips_cot_output` — integration: monkeypatch `audit_fix._call_audit_model` → returns CoT-prefixed string; run_fix on synthetic briefing+audit+session; assert no CoT phrase in output HTML; assert FixAction `status=="failed"` `detail` starts with `"validator rejected"`.
4. `test_validator_accepts_clean_paragraph` — direct unit: `<p>Sixty word paragraph...</p>` returns `(True, "ok")`.
5. `test_fix_empty_with_data_splices_clean_output` — integration: monkeypatch returns clean `<p>...</p>`; run_fix splices normally; FixAction `status=="applied"`.

Red proof: write tests with no validator → tests 1, 2, 3 fail (no `_validate_audit_model_output` exists; `fix_empty_with_data` splices verbatim).

Green proof: add validator + wiring → all 5 pass.

Restore proof: revert validator → tests 1, 2, 3 fail again. Re-apply.

## Out of scope (deferred to F-007)

Other `fix_*` functions (`fix_greeting_incomplete` at line 519+, `fix_low_quality_section` if exists, `fix_polish_narrative_flow` if exists) call `_call_audit_model` too. F-001 lands the validator + wires only `fix_empty_with_data`. F-007 ports the validator to all other fix paths in a separate PR (see task #6).

## Rollback trigger

If validator rejection rate spikes >30% across 7 daily runs (audit-fix JSON shows >2/9 sectors failed validation), validator is too strict. Loosen `min_words` floor or remove specific CoT markers. JSON spike-detection is manual for now; F-009 fix will add a CI check.

## PR push path

1. `git checkout -b fix/F-001-audit-fix-output-validation main`
2. Apply changes to `scripts/audit_fix.py` + new `tests/test_audit_fix_validator.py`.
3. `git push 'https://ghp_<PAT>@github.com/mmmichaelllang/jeeves-unchained.git' fix/F-001-audit-fix-output-validation` (TK pattern — env-var token for `gh`, embedded URL for `git push`).
4. `GH_TOKEN=<pat> gh pr create --base main --title "fix(audit): F-001 — validate fix_empty_with_data output" --body "..."`.

PAT scope check: `scripts/` only, no `.github/workflows/` files touched → standard `repo` scope sufficient. No workflow-scope blocker.

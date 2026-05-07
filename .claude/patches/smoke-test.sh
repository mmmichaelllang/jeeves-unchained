#!/usr/bin/env bash
# Short-version smoke test of the F-001/F-007/F-009 changes against the real
# 2026-05-06 briefing artifacts. Hermetic — no LLM calls, no network.
#
# CONTRACT: any failure flips the script's exit code to non-zero and prints
# a final FAIL banner. No more visual-pass / silent-fall-through.

set -uo pipefail
cd ~/jeeves-unchained
# Pull in /usr/local/bin (jq, gh) and ~/.local/bin (uv) — non-TTY shells don't have these.
export PATH=$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH

DATE=2026-05-06
WORK=/tmp/jeeves-smoke
rm -rf "$WORK" && mkdir -p "$WORK/sessions"

# Tally rather than first-fail so the operator sees every failure in one run.
FAILS=0
fail() { echo "  ✗ FAIL: $*"; FAILS=$((FAILS + 1)); }
pass() { echo "  ✓ PASS: $*"; }
require() {
  # require <description> <expected> <actual>
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 — expected=$2 actual=$3"; fi
}

# Hard preflight — fail loudly if a tool is missing rather than silently
# producing wrong-but-passing output (the original Step D failure mode).
for tool in jq uv python3; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "PREFLIGHT FAIL: $tool not in PATH ($PATH)"
    exit 2
  fi
done

# Stage real artifacts in a tmp sessions dir to avoid mutating the repo.
cp sessions/briefing-$DATE.html "$WORK/sessions/"
cp sessions/session-$DATE.json "$WORK/sessions/"
echo "===== smoke test against $DATE briefing ====="
echo "briefing: $(wc -c < $WORK/sessions/briefing-$DATE.html) bytes"
echo "session:  $(wc -c < $WORK/sessions/session-$DATE.json) bytes"

echo ""
echo "===== STEP A: detection (audit.py --no-llm) ====="
uv run --no-sync python scripts/audit.py --date $DATE --sessions-dir $WORK/sessions --no-llm 2>&1 | tail -10
PRE_DEFECTS=$(jq '.defects | length' $WORK/sessions/audit-$DATE.json)
echo "PRE-FIX defect count: $PRE_DEFECTS"
if [ "$PRE_DEFECTS" -lt 1 ]; then
  fail "audit.py found 0 defects on the known-broken 2026-05-06 briefing"
fi

echo ""
echo "===== STEP B: F-001/F-007 validator unit smoke ====="
B_OUT=$(uv run --no-sync python -c "
import sys, json
sys.path.insert(0, 'scripts')
from audit_fix import _validate_audit_model_output

cases = [
    ('pure CoT', 'We need to produce a paragraph. Word count: 60.', False),
    ('CoT-then-HTML', 'We need to write. <p>real para with enough words to pass minimum count and clear thirty without difficulty in any expected way.</p>', False),
    ('clean P (40w)', '<p>The Edmonds Comprehensive Plan workshop drew, by the count of the local Beacon, some thirty residents to a Wednesday-evening discussion of zoning, a subject that rarely improves with public consultation in any meaningful sense whatever.</p>', True),
    ('too short', '<p>Too short.</p>', False),
    ('div ok (38w)', '<div>Sufficient words to clear the floor of thirty here, this is plausible Jeeves voice content with a real noun phrase and a verb and another clause that pushes us comfortably past thirty words.</div>', True),
    ('think tag leak', '<p>Real-looking paragraph that has enough words to clear the floor and looks fine on first read but contains a hidden <think>marker</think> in the body somewhere among the words.</p>', False),
]
results = []
for label, text, want in cases:
    got, reason = _validate_audit_model_output(text)
    results.append({'label': label, 'want': want, 'got': got, 'reason': reason})
print(json.dumps(results))
")
# Parse + assert per case
echo "$B_OUT" | python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
all_ok = True
for r in data:
    status = '✓' if r['got'] == r['want'] else '✗'
    if r['got'] != r['want']:
        all_ok = False
    print(f\"  {status} {r['label']:20s} expected={r['want']!s:5s} got={r['got']!s:5s} reason={r['reason']!r}\")
sys.exit(0 if all_ok else 3)
"
B_RC=$?
if [ $B_RC -ne 0 ]; then
  fail "Step B validator unit smoke (exit=$B_RC)"
fi

echo ""
echo "===== STEP C: F-001 integration — fix_empty_with_data with stub CoT ====="
C_OUT=$(uv run --no-sync python -c "
import sys, os, json
sys.path.insert(0, 'scripts')
import audit_fix as fix_mod
from audit_fix import run_fix
from pathlib import Path

tmp = Path('/tmp/jeeves-smoke-f001')
import shutil
if tmp.exists(): shutil.rmtree(tmp)
tmp.mkdir(parents=True)
html = '''<!DOCTYPE html><html><body>
<p>Greet.</p>
<h3>The Library Stacks</h3>
<p></p>
<h3>Talk of the Town</h3>
<p>End.</p>
</body></html>'''
(tmp / 'briefing-2026-05-06.html').write_text(html)
(tmp / 'audit-2026-05-06.json').write_text(json.dumps({
    'date': '2026-05-06',
    'defects': [{'type':'empty_with_data','severity':'high','section':'The Library Stacks','detail':'empty','evidence':{'sectors':['literary_pick']}}],
}))
(tmp / 'session-2026-05-06.json').write_text(json.dumps({
    'date': '2026-05-06',
    'literary_pick': {'available': True, 'title': 'Gilead', 'url': 'https://example.com/g', 'summary': 'A novel'},
}))

fix_mod._call_audit_model = lambda prompt, system='', max_tokens=600: (
    'We need to produce a paragraph. Word count: 60-180. Let me think. <p>Gilead is the pick.</p>',
    'stub/reasoning-7b:free',
)
os.environ['OPENROUTER_API_KEY'] = 'fake'

report = run_fix('2026-05-06', tmp, use_llm=True, dry_run=False)
out = (tmp / 'briefing-2026-05-06.html').read_text()

cot_in_briefing = 'We need to produce' in out
rerender = [a for a in report.actions if a.type == 'rerender_empty_with_data']
print(json.dumps({
    'cot_in_briefing': cot_in_briefing,
    'rerender_count': len(rerender),
    'rerender_status': rerender[0].status if rerender else None,
    'rerender_detail': rerender[0].detail if rerender else None,
}))
")
echo "$C_OUT" | python3 -c "
import json, sys
r = json.loads(sys.stdin.read())
ok = True
if r['cot_in_briefing']:
    print('  ✗ FAIL: CoT leaked into briefing'); ok=False
else:
    print('  ✓ PASS: CoT NOT in briefing')
if r['rerender_count'] != 1:
    print(f\"  ✗ FAIL: expected 1 rerender FixAction, got {r['rerender_count']}\"); ok=False
elif r['rerender_status'] != 'failed':
    print(f\"  ✗ FAIL: expected status=failed, got {r['rerender_status']!r}\"); ok=False
elif 'validator' not in (r['rerender_detail'] or '').lower():
    print(f\"  ✗ FAIL: expected 'validator' in detail, got {r['rerender_detail']!r}\"); ok=False
else:
    print(f\"  ✓ PASS: FixAction status=failed validator-rejected\")
sys.exit(0 if ok else 3)
"
C_RC=$?
if [ $C_RC -ne 0 ]; then
  fail "Step C F-001 integration (exit=$C_RC)"
fi

echo ""
echo "===== STEP D: F-009 gate logic (the actual YAML shell) ====="
PRE_FILE=$WORK/sessions/audit-$DATE.json
POST_FILE=$WORK/sessions/audit-$DATE.post-fix.json

# Inline the exact gate shell from .github/workflows/daily.yml so we test
# the real logic, not a paraphrase. Set AUDITOR_REVERT via local var since
# we don't have $GITHUB_ENV.
gate() {
  # Mirror the production workflow step from .github/workflows/daily.yml.
  # Echoes "0" (KEEP) or "1" (REVERT) on stdout; mirrors the AUDITOR_REVERT
  # env-var that the YAML step would set.
  local subshell_out
  subshell_out=$(
    set -euo pipefail
    if [ ! -f "$POST_FILE" ]; then echo "REVERT=1"; exit 0; fi
    if ! command -v jq >/dev/null 2>&1; then echo "REVERT=1"; exit 0; fi
    # Require both files to have .defects as an array. Catches schema drift
    # AND malformed post-fix JSON where .defects is missing/null/object —
    # in all those cases jq '.defects | length' returns 0, which would
    # otherwise look like "auditor improved!" and keep a broken briefing.
    if ! jq -e '.defects | type == "array"' "$PRE_FILE" >/dev/null 2>&1; then
      echo "REVERT=1"; exit 0
    fi
    if ! jq -e '.defects | type == "array"' "$POST_FILE" >/dev/null 2>&1; then
      echo "REVERT=1"; exit 0
    fi
    PRE=$(jq '.defects | length' "$PRE_FILE" 2>/dev/null)
    POST=$(jq '.defects | length' "$POST_FILE" 2>/dev/null)
    if ! [[ "$PRE" =~ ^[0-9]+$ ]] || ! [[ "$POST" =~ ^[0-9]+$ ]]; then
      echo "REVERT=1"; exit 0
    fi
    if [ "$POST" -ge "$PRE" ]; then echo "REVERT=1"; else echo "REVERT=0"; fi
  ) 2>/dev/null
  case "$subshell_out" in
    REVERT=0) echo 0 ;;
    REVERT=1) echo 1 ;;
    *) echo 1 ;;  # any unexpected output -> conservative revert
  esac
}

# D.1 — happy path: post < pre
jq '.defects = (.defects | .[1:])' $PRE_FILE > $POST_FILE
require "D.1 happy path (post < pre) -> KEEP" "0" "$(gate)"

# D.2 — regression: post == pre
cp $PRE_FILE $POST_FILE
require "D.2 no-op (post == pre) -> REVERT" "1" "$(gate)"

# D.3 — regression: post > pre
jq '.defects = .defects + [{"type":"injected","severity":"high"}]' $PRE_FILE > $POST_FILE
require "D.3 regression (post > pre) -> REVERT" "1" "$(gate)"

# D.4 — defensive: post-fix file missing
rm -f $POST_FILE
require "D.4 missing post-fix -> REVERT" "1" "$(gate)"

# D.5 — defensive: post-fix file is not valid JSON
echo "this is not json" > $POST_FILE
require "D.5 invalid JSON -> REVERT" "1" "$(gate)"

# D.6 — defensive: post-fix has no .defects key
echo '{"date":"2026-05-06"}' > $POST_FILE
require "D.6 schema drift (no .defects) -> REVERT" "1" "$(gate)"

echo ""
echo "===== STEP E: pytest re-confirm ====="
if uv run --no-sync pytest tests/test_audit.py tests/test_audit_fix.py tests/test_audit_fix_validator.py tests/test_audit_models.py tests/test_audit_cli_signal.py --no-header --tb=line -q 2>&1 | tail -2; then
  pass "pytest audit suite"
else
  fail "pytest audit suite"
fi

echo ""
if [ $FAILS -eq 0 ]; then
  echo "===== ✓ ALL CHECKS PASSED ====="
  exit 0
else
  echo "===== ✗ $FAILS CHECK(S) FAILED ====="
  exit 1
fi

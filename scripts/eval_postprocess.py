#!/usr/bin/env python3
"""Eval gate (E) — postprocess regression harness over committed briefings.

Runs ``jeeves.write.postprocess_html`` against every (briefing, session)
pair on disk and compares the bucket-firing fingerprint to a baseline.

This is the merge gate for postprocess + bucket-detection changes. When
the baseline says briefing X fires N hits in bucket B, post-change runs
must still fire AT LEAST those N hits (otherwise we lost detection — false
negative). And no NEW unrelated hits should appear (otherwise we added
false positives).

Subcommands
-----------
``snapshot``: regenerate ``tests/eval_baseline.json``. Run this when you
intentionally added new bucket phrases and verified the new hits are
actual issues. Commits the new fingerprint as canonical.

``compare`` (default): run postprocess on every (briefing, session) pair
and diff against ``tests/eval_baseline.json``. Exits 0 on no-delta or
fewer-FP. Exits 1 on any new false-positive bucket firing.

Usage::

    uv run python scripts/eval_postprocess.py snapshot
    uv run python scripts/eval_postprocess.py compare
    uv run python scripts/eval_postprocess.py compare --baseline path/to/file.json

The harness is hermetic: no LLM calls, no network. Just postprocess +
bucket detection over real session JSONs.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jeeves.schema import SessionModel  # noqa: E402
from jeeves.write import postprocess_html  # noqa: E402

log = logging.getLogger("eval_postprocess")

DEFAULT_BASELINE = REPO_ROOT / "tests" / "eval_baseline.json"
DATE_RE = re.compile(r"briefing-(\d{4}-\d{2}-\d{2})\.html$")


def collect_pairs(sessions_dir: Path) -> list[tuple[str, Path, Path]]:
    """Return list of (date_str, briefing_path, session_path) for every
    committed (.html) briefing that has a matching session JSON.
    Smoke-test files (.smoke.html, .local.html) are excluded.
    """
    pairs: list[tuple[str, Path, Path]] = []
    for briefing in sorted(sessions_dir.glob("briefing-*.html")):
        # Exclude smoke / local artifacts.
        if briefing.name.endswith((".smoke.html", ".local.html")):
            continue
        m = DATE_RE.search(briefing.name)
        if not m:
            continue
        date_str = m.group(1)
        session_path = sessions_dir / f"session-{date_str}.json"
        if not session_path.exists():
            log.debug("skip %s — no session JSON", briefing.name)
            continue
        pairs.append((date_str, briefing, session_path))
    return pairs


def fingerprint(date_str: str, briefing: Path, session_path: Path) -> dict:
    """Run postprocess on (briefing, session) and return a bucket-firing
    fingerprint suitable for diffing.

    Output shape::

        {
          "date": "2026-05-09",
          "buckets": {
            "staleness_narration": ["since our prior briefing", ...],
            "closing_summary": [...],
            ...
          },
          "asides_floor_count": 4,
          "signoff_correct": true,
          "word_count": 5500
        }
    """
    raw_html = briefing.read_text(encoding="utf-8")
    session_data = json.loads(session_path.read_text(encoding="utf-8"))
    session = SessionModel.model_validate(session_data)
    result = postprocess_html(raw_html, session)

    buckets: dict[str, list[str]] = defaultdict(list)
    asides_count = result.profane_aside_count
    for w in result.quality_warnings or []:
        if ":" not in w:
            continue
        bucket, _, phrase = w.partition(":")
        buckets[bucket].append(phrase)

    return {
        "date": date_str,
        "buckets": {k: sorted(v) for k, v in buckets.items()},
        "asides_floor_count": asides_count,
        "signoff_correct": "Your reluctantly faithful Butler" in result.html,
        "word_count": result.word_count,
    }


def snapshot(sessions_dir: Path, baseline_path: Path) -> int:
    pairs = collect_pairs(sessions_dir)
    if not pairs:
        log.error("no (briefing, session) pairs found in %s", sessions_dir)
        return 2
    fingerprints = [fingerprint(d, b, s) for d, b, s in pairs]
    payload = {
        "version": 1,
        "generated_for_commit": _git_head(),
        "fingerprints": fingerprints,
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info("baseline written to %s (%d fingerprints)",
             baseline_path, len(fingerprints))
    return 0


def compare(sessions_dir: Path, baseline_path: Path,
            allow_new_negative: bool = False) -> int:
    """Compare current fingerprints to baseline. Returns exit-code.

    A "false positive" is a bucket-phrase that fires NOW but did NOT
    fire in the baseline. That's the regression we block. A "false
    negative" is a phrase that fired before but doesn't now — usually
    fine when intentional (you removed a phrase from the bucket
    definition); pass ``--allow-lost-hits`` to permit.
    """
    if not baseline_path.exists():
        log.error("baseline missing: %s — run `snapshot` first.", baseline_path)
        return 2
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    base_by_date = {fp["date"]: fp for fp in baseline.get("fingerprints", [])}

    pairs = collect_pairs(sessions_dir)
    if not pairs:
        log.error("no pairs to compare")
        return 2

    new_positives_total = 0
    lost_hits_total = 0
    rows: list[str] = []
    for d, b, s in pairs:
        cur = fingerprint(d, b, s)
        base = base_by_date.get(d)
        if not base:
            log.info("new briefing %s (no baseline); skipping diff", d)
            continue
        # Per-bucket diff.
        cur_buckets = cur.get("buckets", {})
        base_buckets = base.get("buckets", {})
        all_buckets = set(cur_buckets) | set(base_buckets)
        for bucket in sorted(all_buckets):
            cur_phr = set(cur_buckets.get(bucket, []))
            base_phr = set(base_buckets.get(bucket, []))
            new_pos = sorted(cur_phr - base_phr)
            lost = sorted(base_phr - cur_phr)
            if new_pos:
                new_positives_total += len(new_pos)
                rows.append(f"  [{d}] +{bucket}: {new_pos}")
            if lost:
                lost_hits_total += len(lost)
                rows.append(f"  [{d}] -{bucket}: {lost}")

    if rows:
        print("Eval-gate diff:")
        for r in rows:
            print(r)
    print(f"Summary: {new_positives_total} new false-positive(s), "
          f"{lost_hits_total} lost hit(s)")

    if new_positives_total > 0:
        log.error(
            "GATE FAIL: %d new false-positive bucket firing(s). "
            "Review the diff above. If the new hits are real issues, "
            "regenerate the baseline via `snapshot`.",
            new_positives_total,
        )
        return 1
    if lost_hits_total > 0 and not allow_new_negative:
        log.warning(
            "Lost-hit count %d. If intentional, re-run with --allow-lost-hits "
            "or regenerate baseline via `snapshot`.", lost_hits_total,
        )
        return 1
    log.info("Eval gate PASS — no new false positives.")
    return 0


def _git_head() -> str:
    try:
        import subprocess
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="postprocess eval-gate harness")
    p.add_argument(
        "subcommand", choices=["snapshot", "compare", "report"],
        help="snapshot: regenerate baseline. compare: diff against baseline "
             "(non-zero on regression). report: same as compare but always "
             "exits 0.",
    )
    p.add_argument(
        "--sessions-dir", default=str(REPO_ROOT / "sessions"),
        help="Directory containing briefing + session pairs.",
    )
    p.add_argument(
        "--baseline", default=str(DEFAULT_BASELINE),
        help="Path to baseline JSON.",
    )
    p.add_argument(
        "--allow-lost-hits", action="store_true",
        help="Don't fail on phrases that lost a hit (e.g. when a phrase "
             "was removed from a bucket).",
    )
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    sessions_dir = Path(args.sessions_dir)
    baseline_path = Path(args.baseline)

    if args.subcommand == "snapshot":
        return snapshot(sessions_dir, baseline_path)
    code = compare(sessions_dir, baseline_path,
                   allow_new_negative=args.allow_lost_hits)
    if args.subcommand == "report":
        return 0
    return code


if __name__ == "__main__":
    sys.exit(main())

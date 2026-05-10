"""Eval-gate harness tests (E).

Hermetic — no real briefings, no LLM calls. Builds a tiny in-memory
(briefing, session) pair, snapshots it, then runs compare under three
scenarios:

  1. No-change → PASS (exit 0)
  2. New false positive in a bucket → FAIL (exit 1)
  3. Lost hit (phrase no longer fires) → FAIL by default (exit 1),
     PASS with --allow-lost-hits.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from jeeves.testing.mocks import canned_session

REPO = Path(__file__).resolve().parent.parent


def _make_briefing(asides: int, *, include_phrase: str = "") -> str:
    """Minimal complete briefing-shaped HTML.

    ``include_phrase`` lands inside body prose so the bucket detector
    fires. ``asides`` controls aside-floor count.
    """
    asides_block = " ".join("clusterfuck" for _ in range(asides))
    extra = f"<p>{include_phrase}</p>" if include_phrase else ""
    return (
        "<!DOCTYPE html><html><body>"
        '<div class="container">'
        "<h1>Saturday, 9 May 2026</h1>"
        f"<p>Body. {asides_block}</p>"
        f"{extra}"
        '<div class="signoff"><p>Your reluctantly faithful Butler,<br/>Jeeves</p></div>'
        "<!-- COVERAGE_LOG_PLACEHOLDER -->"
        "</div></body></html>"
    )


@pytest.fixture
def corpus_dir(tmp_path: Path):
    """Build a one-pair corpus: briefing-2026-05-09.html + session JSON."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    d = "2026-05-09"
    # Fingerprint with one staleness-narration phrase.
    (sessions / f"briefing-{d}.html").write_text(
        _make_briefing(asides=3, include_phrase="As noted earlier"),
        encoding="utf-8",
    )
    sd = canned_session(date.fromisoformat(d))
    sd["date"] = d
    (sessions / f"session-{d}.json").write_text(
        json.dumps(sd, ensure_ascii=False), encoding="utf-8",
    )
    return sessions


def _run_eval(corpus_dir: Path, baseline_path: Path, sub: str,
              extra: list[str] | None = None) -> subprocess.CompletedProcess:
    args = [
        sys.executable,
        str(REPO / "scripts" / "eval_postprocess.py"),
        sub,
        "--sessions-dir", str(corpus_dir),
        "--baseline", str(baseline_path),
    ]
    if extra:
        args.extend(extra)
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def test_snapshot_then_compare_no_diff_passes(corpus_dir: Path, tmp_path: Path):
    """Snapshot then compare same corpus → PASS."""
    baseline = tmp_path / "baseline.json"
    snap = _run_eval(corpus_dir, baseline, "snapshot")
    assert snap.returncode == 0, snap.stderr
    assert baseline.exists()

    cmp_ = _run_eval(corpus_dir, baseline, "compare")
    assert cmp_.returncode == 0, cmp_.stderr + cmp_.stdout
    # Summary line surfaces in stdout.
    assert "0 new false-positive" in cmp_.stdout


def test_compare_detects_new_false_positive(corpus_dir: Path, tmp_path: Path):
    """Snapshot baseline, then add a NEW staleness phrase to a briefing
    and re-run compare. Should exit 1 and report the new hit."""
    baseline = tmp_path / "baseline.json"
    snap = _run_eval(corpus_dir, baseline, "snapshot")
    assert snap.returncode == 0

    # Mutate the briefing to introduce a new staleness phrase.
    briefing_path = corpus_dir / "briefing-2026-05-09.html"
    text = briefing_path.read_text(encoding="utf-8")
    # Add 'unchanged from previous reports' which is in the staleness bucket.
    text = text.replace(
        "<p>Body.",
        "<p>Body unchanged from previous reports.",
        1,
    )
    briefing_path.write_text(text, encoding="utf-8")

    cmp_ = _run_eval(corpus_dir, baseline, "compare")
    assert cmp_.returncode == 1, (cmp_.stdout, cmp_.stderr)
    assert "new false-positive" in cmp_.stdout
    assert "unchanged from previous reports" in cmp_.stdout


def test_compare_detects_lost_hit_blocks_by_default(corpus_dir: Path,
                                                   tmp_path: Path):
    """Baseline records a phrase. Briefing edit removes it. Default
    compare exits 1; --allow-lost-hits exits 0."""
    baseline = tmp_path / "baseline.json"
    _run_eval(corpus_dir, baseline, "snapshot")

    briefing_path = corpus_dir / "briefing-2026-05-09.html"
    text = briefing_path.read_text(encoding="utf-8")
    text = text.replace("As noted earlier", "")  # remove the bucket phrase
    briefing_path.write_text(text, encoding="utf-8")

    cmp_default = _run_eval(corpus_dir, baseline, "compare")
    assert cmp_default.returncode == 1
    assert "lost hit" in cmp_default.stdout

    cmp_allow = _run_eval(corpus_dir, baseline, "compare",
                          extra=["--allow-lost-hits"])
    assert cmp_allow.returncode == 0


def test_report_subcommand_always_returns_zero(corpus_dir: Path, tmp_path: Path):
    """Even on regression, `report` exits 0 — useful for human review."""
    baseline = tmp_path / "baseline.json"
    _run_eval(corpus_dir, baseline, "snapshot")

    briefing_path = corpus_dir / "briefing-2026-05-09.html"
    text = briefing_path.read_text(encoding="utf-8")
    text = text.replace(
        "<p>Body.",
        "<p>Body unchanged from previous reports.",
        1,
    )
    briefing_path.write_text(text, encoding="utf-8")

    rep = _run_eval(corpus_dir, baseline, "report")
    assert rep.returncode == 0
    assert "new false-positive" in rep.stdout

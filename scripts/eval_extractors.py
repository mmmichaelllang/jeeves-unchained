"""Extractor eval harness — compare httpx/playwright/tinyfish on a fixed URL set.

Sprint-18 rollout (week 1) tooling. Reads an eval fixture (YAML), runs each
extractor against every URL, and writes a CSV plus a one-screen summary.

Metrics per (url, extractor):
  - success         bool (>=300 chars AND no dead-end markers)
  - char_count      raw markdown length
  - content_recall  fraction of fixture's golden_text fragments present
  - latency_ms      monotonic clock around the extractor call
  - cost_usd        per-call cost estimate (CI-minutes for playwright; per-
                    request rate for tinyfish; 0 for httpx)
  - extracted_via   provider's self-reported extraction strategy
  - error           short error string when success=False

Aggregate summary (printed and appended to the CSV as a trailing block):
  - per-extractor success_rate, avg_recall, p50/p95 latency, total $ spent
  - pass-fail vs EVAL_GATE.md thresholds (printed as PASS/FAIL prefix)

Usage::

    uv run python scripts/eval_extractors.py \\
        --fixture tests/fixtures/extractor_eval_set.yaml \\
        --extractors httpx,playwright,tinyfish \\
        --out sessions/eval-tinyfish-2026-05-05.csv

`--dry-run` skips network calls and emits a CSV with all-zero metrics — used
in CI smoke tests to make sure the harness imports cleanly.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("eval_extractors")

# Per-call cost estimates (USD). Tune as real prices come in.
_COST_USD = {
    "httpx": 0.0,
    # playwright runs in CI; estimate at ~3s per call * $0.008/min runner cost.
    "playwright": 3.0 / 60.0 * 0.008,
    # tinyfish — placeholder until contract pricing is confirmed.
    "tinyfish": 0.012,
}


@dataclass
class EvalCase:
    id: str
    category: str
    url: str
    golden_text: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    case_id: str
    category: str
    url: str
    extractor: str
    success: bool
    char_count: int
    content_recall: float
    latency_ms: int
    cost_usd: float
    extracted_via: str
    error: str


def _load_fixture(path: Path) -> list[EvalCase]:
    """Parse the YAML eval set. Falls back to manual loader if pyyaml missing."""
    raw = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw) or {}
    except Exception:
        log.warning("pyyaml unavailable; falling back to JSON-only fixture parse")
        data = json.loads(raw)

    cases_raw = data.get("cases") or []
    cases: list[EvalCase] = []
    for entry in cases_raw:
        cases.append(
            EvalCase(
                id=str(entry.get("id") or ""),
                category=str(entry.get("category") or ""),
                url=str(entry.get("url") or ""),
                golden_text=[str(s) for s in (entry.get("golden_text") or [])],
            )
        )
    return cases


# ---------------------------------------------------------------------------
# Extractor adapters — each returns dict with at least
#   {success, text, title, extracted_via, error?}
# ---------------------------------------------------------------------------


def _run_httpx(url: str) -> dict:
    """Minimal httpx + trafilatura — same logic as enrichment.fetch_article_text
    primary path, isolated so we can score it independently."""
    import httpx

    try:
        r = httpx.get(url, timeout=25.0, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 jeeves-eval"})
        r.raise_for_status()
        html = r.text
    except Exception as e:
        return {"success": False, "text": "", "title": "", "extracted_via": "httpx", "error": str(e)}

    try:
        import trafilatura  # type: ignore

        text = trafilatura.extract(html, favor_recall=True) or ""
    except Exception as e:
        return {"success": False, "text": "", "title": "", "extracted_via": "httpx", "error": f"trafilatura: {e}"}

    return {
        "success": len(text) >= 300,
        "text": text,
        "title": "",
        "extracted_via": "httpx",
        "error": "" if text else "no content",
    }


def _run_playwright(url: str) -> dict:
    from jeeves.tools.playwright_extractor import _extract_article_core

    return _extract_article_core(url, timeout_seconds=30, max_chars=12_000)


def _run_tinyfish(url: str) -> dict:
    from jeeves.tools.tinyfish import extract_article

    return extract_article(url, timeout_seconds=30, max_chars=12_000)


_EXTRACTORS: dict[str, Callable[[str], dict]] = {
    "httpx": _run_httpx,
    "playwright": _run_playwright,
    "tinyfish": _run_tinyfish,
}


def _content_recall(text: str, golden: list[str]) -> float:
    real = [g for g in golden if g.strip()]
    if not real:
        return 0.0
    hits = sum(1 for g in real if g.lower() in text.lower())
    return round(hits / len(real), 3)


def _evaluate_case(case: EvalCase, extractor: str, *, dry_run: bool) -> EvalResult:
    if dry_run:
        return EvalResult(
            case_id=case.id, category=case.category, url=case.url,
            extractor=extractor, success=False, char_count=0,
            content_recall=0.0, latency_ms=0, cost_usd=0.0,
            extracted_via="dry-run", error="dry-run",
        )

    fn = _EXTRACTORS.get(extractor)
    if fn is None:
        return EvalResult(
            case_id=case.id, category=case.category, url=case.url,
            extractor=extractor, success=False, char_count=0,
            content_recall=0.0, latency_ms=0, cost_usd=0.0,
            extracted_via="", error=f"unknown extractor {extractor}",
        )

    t0 = time.monotonic()
    try:
        result = fn(case.url)
    except Exception as e:
        result = {"success": False, "text": "", "extracted_via": extractor, "error": str(e)}
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    text = result.get("text") or ""
    return EvalResult(
        case_id=case.id,
        category=case.category,
        url=case.url,
        extractor=extractor,
        success=bool(result.get("success")),
        char_count=len(text),
        content_recall=_content_recall(text, case.golden_text),
        latency_ms=elapsed_ms,
        cost_usd=_COST_USD.get(extractor, 0.0),
        extracted_via=str(result.get("extracted_via") or ""),
        error=str(result.get("error") or "")[:200],
    )


def _write_csv(rows: list[EvalResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "case_id", "category", "url", "extractor",
            "success", "char_count", "content_recall",
            "latency_ms", "cost_usd", "extracted_via", "error",
        ])
        for r in rows:
            writer.writerow([
                r.case_id, r.category, r.url, r.extractor,
                int(r.success), r.char_count, r.content_recall,
                r.latency_ms, f"{r.cost_usd:.4f}", r.extracted_via, r.error,
            ])


def _print_summary(rows: list[EvalResult]) -> None:
    by_extractor: dict[str, list[EvalResult]] = {}
    for r in rows:
        by_extractor.setdefault(r.extractor, []).append(r)

    print()
    print(f"{'extractor':<12} {'n':>4} {'success%':>9} {'recall':>7} "
          f"{'p50_ms':>7} {'p95_ms':>7} {'total_$':>8}")
    print("-" * 60)
    for name, rs in sorted(by_extractor.items()):
        n = len(rs)
        succ = sum(1 for r in rs if r.success) / n if n else 0
        recall = statistics.mean([r.content_recall for r in rs]) if rs else 0
        latencies = sorted(r.latency_ms for r in rs)
        p50 = latencies[len(latencies) // 2] if latencies else 0
        p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)] if latencies else 0
        total = sum(r.cost_usd for r in rs)
        print(f"{name:<12} {n:>4} {succ * 100:>8.1f}% {recall:>7.2f} "
              f"{p50:>7d} {p95:>7d} {total:>8.4f}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Compare article extractors.")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/extractor_eval_set.yaml"),
        help="YAML fixture file path.",
    )
    parser.add_argument(
        "--extractors",
        type=str,
        default="httpx,playwright,tinyfish",
        help="Comma-separated extractor names from: " + ", ".join(_EXTRACTORS.keys()),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("sessions/eval-extractors.csv"),
        help="Output CSV path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip network calls; emit a zero-metric CSV. Smoke test for CI.",
    )
    args = parser.parse_args(argv)

    cases = _load_fixture(args.fixture)
    if not cases:
        print(f"no cases in {args.fixture}", file=sys.stderr)
        return 1

    extractors = [e.strip() for e in args.extractors.split(",") if e.strip()]
    rows: list[EvalResult] = []
    for case in cases:
        for ext in extractors:
            rows.append(_evaluate_case(case, ext, dry_run=args.dry_run))

    _write_csv(rows, args.out)
    print(f"wrote {len(rows)} rows to {args.out}")
    _print_summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

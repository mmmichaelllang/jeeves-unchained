"""Search-provider eval harness — sprint-19 slice E.

Mirrors ``scripts/eval_extractors.py`` for the *search* surface. Iterates
through every case in ``tests/fixtures/search_eval_set.yaml`` (mined by
``scripts/mine_golden_set.py``), runs each enabled provider against the
case query, and computes per-(provider, case) metrics:

* ``recall_at_10``  — fraction of golden URLs returned in top 10
* ``hit_count``     — overlap size with golden set
* ``latency_ms``
* ``cost_usd``      — call rate × per-call price (placeholder; refines as
                       contracts settle)
* ``ok``            — provider returned ``success=True`` / non-empty results

Aggregates per provider:

* ``mean_recall_at_10``
* ``p50/p95 latency``
* ``total_cost_usd``
* ``success_rate``

Promotion criteria (tracked but enforced in EVAL_GATE.md, not here): the
harness only writes data; the gate decides what to do with it.

Usage::

    PYTHONPATH=. python scripts/eval_search.py \\
        --fixture tests/fixtures/search_eval_set.yaml \\
        --providers serper,jina_search,tinyfish_search \\
        --out sessions/eval-search-2026-05-05.csv \\
        [--dry-run]

``--dry-run`` skips real HTTP and emits zero-metric rows — used in CI to
verify imports + arg parsing without burning quota.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("eval_search")

# Per-call cost estimates (USD). Same shape as eval_extractors._COST_USD.
# Free providers report 0; canary tools use their placeholder rate until
# contract pricing settles.
_COST_USD = {
    "serper": 0.001,            # ~$0.30/1k -> $0.0003/call; round up
    "tavily": 0.008,            # basic depth; 1 credit ≈ $0.008
    "exa": 0.005,
    "gemini_grounded": 0.00,    # free tier 20/day
    "vertex_grounded": 0.00,
    "jina_search": 0.0002,
    "jina_deepsearch": 0.05,
    "jina_rerank": 0.0001,
    "tinyfish_search": 0.024,
    "playwright_search": 0.00,
}

# Map provider tag → callable factory(cfg, ledger) → callable(query) → result.
# Loaded lazily to avoid mandatory imports of vertex/google/etc when those
# extras aren't installed (matches eval_extractors approach).


@dataclass
class EvalCase:
    id: str
    category: str
    query: str
    golden_urls: list[str]


@dataclass
class EvalResult:
    case_id: str
    category: str
    provider: str
    query: str
    success: bool
    hits: int
    recall_at_10: float
    latency_ms: int
    cost_usd: float
    error: str = ""


# ---------------------------------------------------------------------------
# Fixture parsing
# ---------------------------------------------------------------------------

def _parse_yaml(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except Exception:
        log.warning("pyyaml unavailable; falling back to manual reader")
    return _manual_yaml(text)


_KV_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
_LIST_RE = re.compile(r"^(\s*)-\s*(.*)$")


def _manual_yaml(text: str) -> dict[str, Any]:
    """Tiny purpose-built YAML reader for this fixture's narrow schema.

    Only handles: scalar key:value, list of dicts under ``cases:``, and
    nested ``golden_urls:`` lists. Refuses to parse arbitrary YAML.
    """
    out: dict[str, Any] = {"cases": []}
    cur_case: dict[str, Any] | None = None
    in_urls = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m_list = _LIST_RE.match(raw)
        m_kv = _KV_RE.match(raw)
        if m_list and m_list.group(1) == "  ":  # top-level case start
            in_urls = False
            cur_case = {}
            out["cases"].append(cur_case)
            rest = m_list.group(2)
            if rest.startswith("id:"):
                cur_case["id"] = rest.split(":", 1)[1].strip()
            continue
        if m_list and in_urls and cur_case is not None:
            cur_case.setdefault("golden_urls", []).append(m_list.group(2).strip())
            continue
        if m_kv:
            indent, key, val = m_kv.group(1), m_kv.group(2), m_kv.group(3).strip()
            if cur_case is not None and indent.startswith("    "):
                if key == "golden_urls":
                    in_urls = True
                    cur_case["golden_urls"] = []
                else:
                    in_urls = False
                    cur_case[key] = val.strip().strip('"')
            elif indent == "":
                out[key] = val.strip().strip('"')
    return out


def load_fixture(path: Path) -> list[EvalCase]:
    data = _parse_yaml(path.read_text(encoding="utf-8"))
    cases_raw = data.get("cases") or []
    cases: list[EvalCase] = []
    for c in cases_raw:
        cases.append(
            EvalCase(
                id=str(c.get("id") or ""),
                category=str(c.get("category") or c.get("sector") or ""),
                query=str(c.get("query") or "").strip().strip('"'),
                golden_urls=[str(u).strip() for u in (c.get("golden_urls") or [])],
            )
        )
    return [c for c in cases if c.query and c.golden_urls]


# ---------------------------------------------------------------------------
# Provider runners — each takes (query) -> {ok, urls, error}
# ---------------------------------------------------------------------------

def _norm_url(u: str) -> str:
    """Strip query/fragment; lowercase host; trailing-slash normalise."""
    if not u:
        return ""
    try:
        from urllib.parse import urlparse, urlunparse

        p = urlparse(u)
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "", ""))
    except Exception:
        return u.strip().lower().rstrip("/")


def _build_runner(provider: str, cfg: Any, ledger: Any) -> Callable[[str], dict[str, Any]]:
    if provider == "serper":
        from jeeves.tools.serper import make_serper_search
        fn = make_serper_search(cfg, ledger)

        def _run(q: str) -> dict[str, Any]:
            data = json.loads(fn(q))
            urls = [r.get("url", "") for r in (data.get("results") or [])]
            return {"ok": not data.get("error"), "urls": urls, "error": data.get("error")}
        return _run
    if provider == "tavily":
        from jeeves.tools.tavily import make_tavily_search
        fn = make_tavily_search(cfg, ledger)

        def _run(q: str) -> dict[str, Any]:
            data = json.loads(fn(q))
            urls = [r.get("url", "") for r in (data.get("results") or [])]
            return {"ok": not data.get("error"), "urls": urls, "error": data.get("error")}
        return _run
    if provider == "exa":
        from jeeves.tools.exa import make_exa_search
        fn = make_exa_search(cfg, ledger)

        def _run(q: str) -> dict[str, Any]:
            data = json.loads(fn(q))
            urls = [r.get("url", "") for r in (data.get("results") or [])]
            return {"ok": not data.get("error"), "urls": urls, "error": data.get("error")}
        return _run
    if provider == "jina_search":
        from jeeves.tools.jina import make_jina_search
        fn = make_jina_search(cfg, ledger)

        def _run(q: str) -> dict[str, Any]:
            data = json.loads(fn(q))
            urls = [r.get("url", "") for r in (data.get("results") or [])]
            return {"ok": not data.get("error"), "urls": urls, "error": data.get("error")}
        return _run
    if provider == "tinyfish_search":
        from jeeves.tools.tinyfish import search as _tf_search

        def _run(q: str) -> dict[str, Any]:
            data = _tf_search(q, num=10, ledger=ledger)
            urls = [r.get("url", "") for r in (data.get("results") or [])]
            return {"ok": bool(data.get("success")), "urls": urls, "error": data.get("error")}
        return _run
    if provider == "playwright_search":
        from jeeves.tools.playwright_extractor import search as _pw_search

        def _run(q: str) -> dict[str, Any]:
            data = _pw_search(q, engine="ddg", num=10, ledger=ledger)
            urls = [r.get("url", "") for r in (data.get("results") or [])]
            return {"ok": bool(data.get("success")), "urls": urls, "error": data.get("error")}
        return _run
    raise ValueError(f"unknown provider: {provider}")


def _dry_run_runner(_provider: str, *_a, **_kw) -> Callable[[str], dict[str, Any]]:
    def _run(q: str) -> dict[str, Any]:
        return {"ok": False, "urls": [], "error": "dry-run"}
    return _run


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _recall_at_10(returned: list[str], golden: list[str]) -> tuple[int, float]:
    g = {_norm_url(u) for u in golden if u}
    seen: set[str] = set()
    hits = 0
    for u in returned[:10]:
        n = _norm_url(u)
        if n in g and n not in seen:
            seen.add(n)
            hits += 1
    if not g:
        return hits, 0.0
    return hits, round(hits / len(g), 4)


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    k = int(round((pct / 100.0) * (len(s) - 1)))
    return s[max(0, min(len(s) - 1, k))]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_eval(
    cases: list[EvalCase],
    providers: list[str],
    *,
    dry_run: bool,
    cfg: Any = None,
    ledger: Any = None,
) -> list[EvalResult]:
    runners: dict[str, Callable[[str], dict[str, Any]]] = {}
    for p in providers:
        if dry_run:
            runners[p] = _dry_run_runner(p)
        else:
            runners[p] = _build_runner(p, cfg, ledger)

    results: list[EvalResult] = []
    for case in cases:
        for p in providers:
            t0 = time.monotonic()
            try:
                out = runners[p](case.query)
            except Exception as exc:
                out = {"ok": False, "urls": [], "error": f"runner crashed: {exc}"}
            latency_ms = int((time.monotonic() - t0) * 1000)
            urls = list(out.get("urls") or [])
            hits, recall = _recall_at_10(urls, case.golden_urls)
            results.append(
                EvalResult(
                    case_id=case.id,
                    category=case.category,
                    provider=p,
                    query=case.query,
                    success=bool(out.get("ok")),
                    hits=hits,
                    recall_at_10=recall,
                    latency_ms=latency_ms,
                    cost_usd=_COST_USD.get(p, 0.0),
                    error=str(out.get("error") or ""),
                )
            )
    return results


def write_csv(results: list[EvalResult], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "case_id",
                "category",
                "provider",
                "query",
                "success",
                "hits",
                "recall_at_10",
                "latency_ms",
                "cost_usd",
                "error",
            ]
        )
        for r in results:
            w.writerow(
                [
                    r.case_id,
                    r.category,
                    r.provider,
                    r.query,
                    int(r.success),
                    r.hits,
                    r.recall_at_10,
                    r.latency_ms,
                    f"{r.cost_usd:.5f}",
                    r.error[:200],
                ]
            )


def summarise(results: list[EvalResult]) -> dict[str, dict[str, Any]]:
    by_provider: dict[str, list[EvalResult]] = {}
    for r in results:
        by_provider.setdefault(r.provider, []).append(r)
    summary: dict[str, dict[str, Any]] = {}
    for provider, rows in by_provider.items():
        total = len(rows)
        ok = sum(1 for r in rows if r.success)
        recalls = [r.recall_at_10 for r in rows]
        latencies = [r.latency_ms for r in rows]
        summary[provider] = {
            "n": total,
            "success_rate": round(ok / total, 4) if total else 0.0,
            "mean_recall_at_10": round(statistics.mean(recalls), 4) if recalls else 0.0,
            "p50_latency_ms": _percentile(latencies, 50),
            "p95_latency_ms": _percentile(latencies, 95),
            "total_cost_usd": round(sum(r.cost_usd for r in rows), 4),
        }
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Evaluate search providers against a golden set.")
    p.add_argument("--fixture", default=Path("tests/fixtures/search_eval_set.yaml"), type=Path)
    p.add_argument(
        "--providers",
        default="serper,jina_search,tinyfish_search,playwright_search",
        help="Comma-separated provider tags (subset of TOOL_TAXONOMY).",
    )
    p.add_argument("--out", default=Path("sessions/eval-search.csv"), type=Path)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.fixture.is_file():
        log.error("fixture not found: %s — run scripts/mine_golden_set.py first", args.fixture)
        return 2

    cases = load_fixture(args.fixture)
    if not cases:
        log.error("no cases in fixture")
        return 3

    providers = [s.strip() for s in args.providers.split(",") if s.strip()]
    log.info("eval: %d cases × %d providers (dry_run=%s)", len(cases), len(providers), args.dry_run)

    cfg = None
    ledger = None
    if not args.dry_run:
        from jeeves.config import Config
        from jeeves.tools.quota import QuotaLedger

        cfg = Config.from_env()
        ledger = QuotaLedger(Path(".quota-state.json"))

    results = run_eval(cases, providers, dry_run=args.dry_run, cfg=cfg, ledger=ledger)
    write_csv(results, args.out)

    summary = summarise(results)
    print(json.dumps(summary, indent=2))
    log.info("wrote %d rows to %s", len(results), args.out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))

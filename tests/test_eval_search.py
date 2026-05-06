"""Sprint-19 slice E: eval_search.py harness shape tests.

Dry-run path only — no real HTTP. Verifies:
* fixture parses (manual YAML reader path covers the no-pyyaml case)
* recall@10 calculation
* CSV shape
* summary aggregates
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def _ensure_path():
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_FIXTURE = """version: 1
mined_at: 2026-05-05
count: 2
cases:
  - id: 2026-05-04_local_news
    category: local_news
    sector: local_news
    session_date: 2026-05-04
    query: "Edmonds WA local news 2026-05-04"
    golden_urls:
      - https://edmondswa.gov/news/a
      - https://myedmondsnews.com/post/b
  - id: 2026-05-04_global_news
    category: global_news
    sector: global_news
    session_date: 2026-05-04
    query: "world news 2026-05-04 top stories"
    golden_urls:
      - https://www.nytimes.com/world/x
      - https://www.bbc.com/news/y
"""


def test_load_fixture_parses_two_cases(tmp_path):
    _ensure_path()
    from scripts.eval_search import load_fixture

    p = tmp_path / "fx.yaml"
    p.write_text(_FIXTURE, encoding="utf-8")
    cases = load_fixture(p)
    assert len(cases) == 2
    assert cases[0].id == "2026-05-04_local_news"
    assert "Edmonds" in cases[0].query
    assert len(cases[0].golden_urls) == 2


def test_recall_at_10_counts_overlap():
    _ensure_path()
    from scripts.eval_search import _recall_at_10

    golden = ["https://a.com/x", "https://b.com/y", "https://c.com/z"]
    returned = ["https://a.com/x", "https://other.com/q", "https://b.com/y/"]
    hits, recall = _recall_at_10(returned, golden)
    assert hits == 2
    assert recall == round(2 / 3, 4)


def test_recall_handles_empty_golden():
    _ensure_path()
    from scripts.eval_search import _recall_at_10

    hits, recall = _recall_at_10(["https://a.com"], [])
    assert hits == 0
    assert recall == 0.0


def test_dry_run_produces_zero_metric_csv(tmp_path):
    _ensure_path()
    from scripts.eval_search import load_fixture, run_eval, write_csv, summarise

    p = tmp_path / "fx.yaml"
    p.write_text(_FIXTURE, encoding="utf-8")
    cases = load_fixture(p)
    results = run_eval(cases, ["serper", "jina_search"], dry_run=True)

    assert len(results) == 4  # 2 cases × 2 providers
    for r in results:
        assert r.success is False
        assert r.recall_at_10 == 0.0
        assert r.error == "dry-run"

    out = tmp_path / "out.csv"
    write_csv(results, out)
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 4
    assert rows[0]["provider"] in {"serper", "jina_search"}

    summary = summarise(results)
    assert "serper" in summary
    assert summary["serper"]["n"] == 2
    assert summary["serper"]["mean_recall_at_10"] == 0.0


def test_recall_normalises_trailing_slash():
    _ensure_path()
    from scripts.eval_search import _recall_at_10

    golden = ["https://a.com/x"]
    returned = ["https://a.com/x/"]
    hits, _ = _recall_at_10(returned, golden)
    assert hits == 1

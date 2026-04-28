import json
from pathlib import Path

import pytest

from jeeves.tools.quota import QuotaLedger


def test_quota_starts_fresh(tmp_path: Path):
    ledger = QuotaLedger(tmp_path / "q.json")
    assert ledger.remaining_free("serper") == 2500
    assert ledger.remaining_free("tavily") == 1000
    assert ledger.cheapest_with_capacity() == "serper"


def test_record_and_persistence(tmp_path: Path):
    state_path = tmp_path / "q.json"
    ledger = QuotaLedger(state_path)
    ledger.record("serper", 10)
    ledger.record("tavily", 3)
    ledger.save()

    reloaded = QuotaLedger(state_path)
    assert reloaded.remaining_free("serper") == 2500 - 10
    assert reloaded.remaining_free("tavily") == 1000 - 3


def test_cheapest_when_serper_exhausted(tmp_path: Path):
    ledger = QuotaLedger(tmp_path / "q.json")
    ledger.record("serper", 2500)
    # serper exhausted; next cheapest by overage_per_1k is exa ($5) then tavily ($8) then gemini.
    # Actually the fixture has serper=0.30 tavily=8 exa=5 gemini=35.
    assert ledger.cheapest_with_capacity() == "exa"


def test_snapshot_is_deep_copy(tmp_path: Path):
    """snapshot() must return an independent copy — mutations must not corrupt the ledger."""
    ledger = QuotaLedger(tmp_path / "q.json")
    ledger.record("serper", 5)
    snap = ledger.snapshot()
    snap["providers"]["serper"]["used"] = 9999
    assert ledger.remaining_free("serper") == 2500 - 5


def test_save_roundtrip_is_consistent(tmp_path: Path):
    """save() must serialise the exact in-memory state without data races."""
    state_path = tmp_path / "q.json"
    ledger = QuotaLedger(state_path)
    ledger.record("exa", 7)
    ledger.record_daily("gemini_grounded", 3)
    ledger.save()

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["providers"]["exa"]["used"] == 7
    assert on_disk["daily"]["gemini_grounded"] == 3

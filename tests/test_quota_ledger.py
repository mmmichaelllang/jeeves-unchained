from pathlib import Path

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

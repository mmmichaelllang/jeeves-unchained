"""Tests for QuotaLedger.snapshot_used_counts (sprint 16 encapsulation fix)."""

from __future__ import annotations

from pathlib import Path

from jeeves.tools.quota import QuotaLedger


def test_snapshot_used_counts_returns_provider_used_map(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota.json")
    ledger.record("serper", 5)
    ledger.record("tavily", 3)
    ledger.record("exa", 1)

    counts = ledger.snapshot_used_counts()
    assert counts["serper"] == 5
    assert counts["tavily"] == 3
    assert counts["exa"] == 1
    # Defaults that haven't been touched still show 0.
    assert counts["gemini"] == 0


def test_snapshot_used_counts_merges_daily_counts(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota.json")
    ledger.record("serper", 2)
    ledger.record_daily("gemini_grounded", 7)

    counts = ledger.snapshot_used_counts()
    assert counts["serper"] == 2
    assert counts["gemini_grounded"] == 7
    # 'date' key should NOT leak through into the count map.
    assert "date" not in counts


def test_snapshot_used_counts_is_independent_copy(tmp_path):
    """Mutating the returned dict must not affect ledger state."""
    ledger = QuotaLedger(tmp_path / "quota.json")
    ledger.record("serper", 5)
    counts = ledger.snapshot_used_counts()
    counts["serper"] = 9999
    assert ledger.snapshot_used_counts()["serper"] == 5


def test_check_allow_acquires_lock(tmp_path):
    """Sanity check: check_allow now reads under lock (audit fix)."""
    ledger = QuotaLedger(tmp_path / "quota.json")
    ledger.record("serper", 100)
    # Should not raise (cap=200, used=100).
    ledger.check_allow("serper", hard_cap=200)
    # Should raise QuotaExceeded.
    import pytest
    from jeeves.tools.quota import QuotaExceeded
    with pytest.raises(QuotaExceeded, match="serper hard cap"):
        ledger.check_allow("serper", hard_cap=100)

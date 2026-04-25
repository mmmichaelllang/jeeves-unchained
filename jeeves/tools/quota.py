"""Month-to-date quota ledger — picks the cheapest provider with capacity.

State lives at `.quota-state.json` at repo root. The research workflow commits
it at the end of every run so the next day's agent sees updated counts.

Daily caps (separate from monthly) are tracked under a "daily" key that resets
automatically when the UTC date changes. Used to enforce Google's 1,500
grounded-search free tier, which resets per day (not per month).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_STATE = {
    "serper": {"used": 0, "free_cap": 2500, "overage_per_1k_usd": 0.30},
    "tavily": {"used": 0, "free_cap": 1000, "overage_per_1k_usd": 8.00},
    "exa": {"used": 0, "free_cap": 500, "overage_per_1k_usd": 5.00},
    "gemini": {"used": 0, "free_cap": 1500, "overage_per_1k_usd": 35.00},
}

# Hard daily limits for providers billed on a per-day free tier.
# Set 10 below the actual free-tier ceiling so a burst never crosses the line.
DAILY_HARD_CAPS: dict[str, int] = {
    "gemini_grounded": 1490,   # Google Search Grounding: 1,500/day free
    "vertex_grounded": 1490,   # Same product, Vertex AI path
}


class QuotaExceeded(RuntimeError):
    pass


class QuotaLedger:
    """Disk-backed ledger. Single-run process, so a threading lock is sufficient."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict:
        now_month = datetime.now(timezone.utc).strftime("%Y-%m")
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("month") == now_month:
                    # merge defaults in case new providers were added
                    providers = data.get("providers") or {}
                    for name, default in DEFAULT_STATE.items():
                        providers.setdefault(name, dict(default))
                    data["providers"] = providers
                    return data
            except Exception as e:
                log.warning("quota state corrupt, resetting: %s", e)
        return {"month": now_month, "providers": {k: dict(v) for k, v in DEFAULT_STATE.items()}}

    def _daily(self) -> dict:
        """Return (and auto-reset) the daily counters sub-dict."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = self._state.setdefault("daily", {"date": today})
        if daily.get("date") != today:
            daily.clear()
            daily["date"] = today
        return daily

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def remaining_free(self, provider: str) -> int:
        p = self._state["providers"].get(provider)
        if not p:
            return 0
        return max(0, p["free_cap"] - p["used"])

    def record(self, provider: str, count: int = 1) -> None:
        with self._lock:
            p = self._state["providers"].setdefault(
                provider, dict(DEFAULT_STATE.get(provider, {"used": 0, "free_cap": 0, "overage_per_1k_usd": 0}))
            )
            p["used"] += count

    def record_daily(self, provider: str, count: int = 1) -> None:
        """Increment a per-day counter (auto-resets at UTC midnight)."""
        with self._lock:
            d = self._daily()
            d[provider] = d.get(provider, 0) + count

    def daily_used(self, provider: str) -> int:
        """Return how many calls have been made today for a daily-tracked provider."""
        with self._lock:
            return self._daily().get(provider, 0)

    def check_daily_allow(self, provider: str, hard_cap: int | None = None) -> None:
        """Raise QuotaExceeded if the daily hard cap for *provider* is reached.

        Uses DAILY_HARD_CAPS if hard_cap is not explicitly provided.
        """
        cap = hard_cap if hard_cap is not None else DAILY_HARD_CAPS.get(provider)
        if cap is None:
            return
        with self._lock:
            used = self._daily().get(provider, 0)
        if used >= cap:
            raise QuotaExceeded(
                f"{provider} daily cap {cap} reached (used={used}) — "
                "will not make further calls today to avoid charges"
            )

    def check_allow(self, provider: str, *, hard_cap: int | None = None) -> None:
        """Raise if caller exceeded a per-provider hard cap."""
        if hard_cap is None:
            return
        used = self._state["providers"].get(provider, {}).get("used", 0)
        if used >= hard_cap:
            raise QuotaExceeded(f"{provider} hard cap {hard_cap} reached (used={used})")

    def cheapest_with_capacity(self) -> str | None:
        """Return the name of the cheapest provider that still has free capacity."""

        candidates = [
            (name, p["overage_per_1k_usd"])
            for name, p in self._state["providers"].items()
            if self.remaining_free(name) > 0
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda kv: kv[1])
        return candidates[0][0]

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self._state))

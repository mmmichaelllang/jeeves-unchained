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
    # TinyFish (sprint-18): canary fetch-chain peer to playwright. Free tier
    # assumed at 100/mo; overage rate is a placeholder until pricing settles.
    "tinyfish": {"used": 0, "free_cap": 100, "overage_per_1k_usd": 12.00},
    # Sprint-19 search-agent canary entries. All start opt-in via JEEVES_USE_*
    # flags in tools/__init__.py; ledger entries exist so the quota guard at
    # research_sectors._quota_snapshot recognises a sector that called only
    # one of these as having performed real work.
    "tinyfish_search": {"used": 0, "free_cap": 250, "overage_per_1k_usd": 24.00},
    "jina_search": {"used": 0, "free_cap": 6000, "overage_per_1k_usd": 0.20},
    "jina_deepsearch": {"used": 0, "free_cap": 300, "overage_per_1k_usd": 5.00},
    "jina_rerank": {"used": 0, "free_cap": 3000, "overage_per_1k_usd": 0.20},
    "playwright_search": {"used": 0, "free_cap": 9999, "overage_per_1k_usd": 0.00},
}

# Sprint-19: auxiliary providers are tracked in the ledger (so usage counts
# show up in snapshots and quota guards work) but excluded from
# ``cheapest_with_capacity`` — that picker is meant to choose among the
# primary general-purpose search providers (serper/tavily/exa/gemini).
# These canary tools are surfaced via opt-in env flags; the agent picks
# them by description, not by overage price.
_AUX_PROVIDERS: set[str] = {
    "tinyfish",
    "tinyfish_search",
    "jina_search",
    "jina_deepsearch",
    "jina_rerank",
    "playwright",
    "playwright_search",
}


# Hard daily limits for providers billed on a per-day free tier.
# gemini_grounded uses gemini-2.5-flash whose free tier allows 20
# generate_content requests per day (GenerateRequestsPerDayPerProjectPerModel-
# FreeTier). We stop at 12 — well below 20 — to leave headroom for
# correspondence and any pre-run calls made earlier in the UTC day.
# When the API returns 429, gemini_grounded.py sets the counter to this cap
# so all subsequent sectors skip Gemini automatically.
DAILY_HARD_CAPS: dict[str, int] = {
    "gemini_grounded": 12,     # Gemini 2.5 Flash: 20 RPD free tier; we cap at 12
    "vertex_grounded": 12,     # Same underlying quota
    # TinyFish (sprint-18): canary cap. Roughly $0.36/day at the placeholder
    # overage rate — well under any plausible budget. Bump after eval shows
    # genuine recall/latency win.
    "tinyfish": 30,
    # Sprint-19 search-agent caps (per /plan synthesis). All conservative —
    # promote via EVAL_GATE shadow-window thresholds before relaxing.
    "tinyfish_search": 8,        # weighted ~2 credits/call; 8/day = 16 credits
    "jina_search": 200,          # free key allows ~100 RPM; 200 calls/day fits
    "jina_deepsearch": 20,       # token-heavy, 30s+ latency — tight cap
    "jina_rerank": 100,          # cheap, ~1ms/pair; cap = pair-call ceiling
    "playwright_search": 60,     # zero-API-cost; cap = wall-clock guardrail
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
        with self._lock:
            serialized = json.dumps(self._state, ensure_ascii=False, indent=2)
        self.path.write_text(serialized, encoding="utf-8")

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
        with self._lock:
            used = self._state["providers"].get(provider, {}).get("used", 0)
        if used >= hard_cap:
            raise QuotaExceeded(f"{provider} hard cap {hard_cap} reached (used={used})")

    def cheapest_with_capacity(self) -> str | None:
        """Return the name of the cheapest primary provider that still has
        free capacity. Auxiliary canary providers (sprint-19 search-agent
        upgrades) are excluded — see ``_AUX_PROVIDERS``."""

        candidates = [
            (name, p["overage_per_1k_usd"])
            for name, p in self._state["providers"].items()
            if name not in _AUX_PROVIDERS and self.remaining_free(name) > 0
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda kv: kv[1])
        return candidates[0][0]

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def snapshot_used_counts(self) -> dict[str, int]:
        """Return {provider: used_count} — the only field callers should reach for.

        Replaces external code that reaches into _state["providers"][name]["used"]
        directly (encapsulation break). Both monthly providers and daily-tracked
        ones are merged into one map for convenience.
        """
        with self._lock:
            counts: dict[str, int] = {}
            for name, p in self._state.get("providers", {}).items():
                counts[name] = int(p.get("used", 0))
            for name, used in self._daily().items():
                if name == "date":
                    continue
                counts[name] = int(used)
            return counts

"""Tests for the NIM circuit breakers in jeeves.research_sectors.

Background — 2026-05-14 run #68 cancellation forensics:
    Research job ran for 65min before GHA killed it. Per-sector log showed:
      - 4 deep+weather sectors crashed with str(e)=="Request timed out."
        (5-11min each before the agent surrendered)
      - 8 subsequent sectors burned 60+120s on rate-limit retries that
        ALL exhausted with 429
      - Only newyorker (direct fetch, no agent) succeeded
    Total: ~50min of "agent retrying a broken NIM endpoint" before cancel.

The two breakers in research_sectors.py short-circuit subsequent sectors
once NIM is provably bad:
    _NIM_429_TRIPPED: set on first all-retries-exhausted 429
    _NIM_TIMEOUT_TRIPPED: set after N consecutive stream-timeout crashes
                          (default threshold=2)

Each subsequent sector then returns spec.default in ~milliseconds instead
of burning another 3-10min on the same broken endpoint.

These tests verify:
  1. _is_stream_timeout helper matches the relevant exception shapes
  2. _reset_circuit_breakers + _circuit_breaker_state round-trip
  3. Short-circuit path: tripped breaker → run_sector returns spec.default
     without ever instantiating the agent
  4. Trip-on-429-exhaustion: agent that always 429s sets _NIM_429_TRIPPED
  5. Trip-on-consecutive-timeouts: 2 sectors in a row crash with
     "Request timed out." → _NIM_TIMEOUT_TRIPPED True
  6. Counter resets on success: timeout sector then success sector → counter
     back to 0
  7. Telemetry events: emit('circuit_breaker_trip', ...) fires on trip
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jeeves import research_sectors as rs
from jeeves.research_sectors import (
    SectorSpec,
    _circuit_breaker_state,
    _is_stream_timeout,
    _reset_circuit_breakers,
    run_sector,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_breakers():
    """Every test starts with breakers cleared. Run #68 left them tripped
    in the module so leaks across tests would mask real failures."""
    _reset_circuit_breakers()
    yield
    _reset_circuit_breakers()


@pytest.fixture
def light_spec() -> SectorSpec:
    return SectorSpec(
        name="local_news",
        shape="list",
        instruction="test instruction",
        default=[],
    )


@pytest.fixture
def deep_spec() -> SectorSpec:
    return SectorSpec(
        name="triadic_ontology",
        shape="deep",
        instruction="test instruction",
        default=[],
    )


@pytest.fixture
def cfg():
    """Minimal Config-shaped object covering attributes run_sector reads."""
    import datetime
    return SimpleNamespace(
        verbose=False,
        run_date=datetime.date(2026, 5, 15),
        jina_api_key="",
    )


@pytest.fixture
def ledger():
    """Minimal ledger with a _state dict that _quota_snapshot can read."""
    obj = MagicMock()
    obj._state = {"providers": {}}
    return obj


# ---------------------------------------------------------------------------
# 1. Helper functions
# ---------------------------------------------------------------------------


def test_is_stream_timeout_matches_bare_request_timed_out():
    """Run #68 deep sectors raised plain Exception('Request timed out.')."""
    assert _is_stream_timeout(Exception("Request timed out."))
    assert _is_stream_timeout(Exception("request timed out"))


def test_is_stream_timeout_matches_class_name():
    """asyncio.TimeoutError + openai.APITimeoutError both match by class."""
    class TimeoutError_(Exception):
        pass
    TimeoutError_.__name__ = "TimeoutError"
    assert _is_stream_timeout(TimeoutError_("anything"))

    class APITimeoutError(Exception):
        pass
    assert _is_stream_timeout(APITimeoutError("anything"))


def test_is_stream_timeout_does_not_match_429_or_network_drop():
    """The 429 and peer-close shapes have their own handlers; the timeout
    matcher must not steal them."""
    assert not _is_stream_timeout(Exception("429 Too Many Requests"))
    assert not _is_stream_timeout(Exception("peer closed connection"))
    assert not _is_stream_timeout(Exception("incomplete chunked read"))


def test_reset_and_state_roundtrip():
    rs._NIM_429_TRIPPED = True
    rs._NIM_TIMEOUT_CONSECUTIVE = 5
    rs._NIM_TIMEOUT_TRIPPED = True
    state = _circuit_breaker_state()
    assert state["nim_429_tripped"] is True
    assert state["nim_timeout_consecutive"] == 5
    assert state["nim_timeout_tripped"] is True

    _reset_circuit_breakers()
    state = _circuit_breaker_state()
    assert state["nim_429_tripped"] is False
    assert state["nim_timeout_consecutive"] == 0
    assert state["nim_timeout_tripped"] is False
    # Threshold is configuration, not state — should not reset.
    assert state["nim_timeout_threshold"] == 2


# ---------------------------------------------------------------------------
# 2. Short-circuit path (breaker already tripped)
# ---------------------------------------------------------------------------


def test_429_breaker_short_circuits_subsequent_sector(light_spec, cfg, ledger):
    """When _NIM_429_TRIPPED is set, run_sector must return spec.default
    immediately WITHOUT importing or instantiating FunctionAgent."""
    rs._NIM_429_TRIPPED = True

    # FunctionAgent import inside run_sector should never fire. Patching to
    # something that would explode if instantiated proves we never reached it.
    with patch(
        "llama_index.core.agent.workflow.FunctionAgent",
        side_effect=AssertionError("agent must not be instantiated when breaker tripped"),
    ):
        result = asyncio.run(run_sector(cfg, light_spec, ["https://prior.com/x"], ledger))

    assert result == light_spec.default


def test_timeout_breaker_short_circuits_subsequent_sector(light_spec, cfg, ledger):
    rs._NIM_TIMEOUT_TRIPPED = True

    with patch(
        "llama_index.core.agent.workflow.FunctionAgent",
        side_effect=AssertionError("agent must not be instantiated when breaker tripped"),
    ):
        result = asyncio.run(run_sector(cfg, light_spec, [], ledger))

    assert result == light_spec.default


def test_short_circuit_emits_llm_call_telemetry(light_spec, cfg, ledger, monkeypatch):
    rs._NIM_429_TRIPPED = True
    captured: list[dict] = []

    def fake_emit_llm_call(**kw):
        captured.append(kw)

    monkeypatch.setattr(
        "jeeves.tools.telemetry.emit_llm_call", fake_emit_llm_call
    )
    asyncio.run(run_sector(cfg, light_spec, [], ledger))

    assert captured, "expected emit_llm_call to fire on short-circuit"
    rec = captured[0]
    assert rec["provider"] == "nim"
    assert rec["sector"] == "local_news"
    assert rec["ok"] is False
    assert rec["error"] == "nim_429_breaker_short_circuit"


# ---------------------------------------------------------------------------
# 3. Trip-on-failure path (the breaker FLIPS)
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Stand-in for llama_index FunctionAgent. ``run()`` always raises the
    exception passed in at construction time."""
    def __init__(self, *, raises: Exception, **_kwargs):
        self._raises = raises

    async def run(self, _msg):
        raise self._raises


def _patch_agent_path(monkeypatch, exc: Exception):
    """Make every FunctionAgent in run_sector raise the same exception.

    Also stub the slow dependencies (build_kimi_llm, all_search_tools)
    so the test never touches the network or downloads models."""

    def factory(*_a, **_kw):
        return _FakeAgent(raises=exc)

    monkeypatch.setattr(
        "llama_index.core.agent.workflow.FunctionAgent", factory
    )
    monkeypatch.setattr(
        "jeeves.llm.build_kimi_llm", lambda *_a, **_kw: MagicMock()
    )
    monkeypatch.setattr(
        "jeeves.tools.all_search_tools", lambda *_a, **_kw: []
    )


def test_429_breaker_trips_on_first_exhausted_sector(light_spec, cfg, ledger, monkeypatch):
    """A sector that runs out of rate-limit retries flips _NIM_429_TRIPPED.
    Subsequent sectors will then short-circuit (covered above)."""
    _patch_agent_path(monkeypatch, Exception("429 Too Many Requests"))

    # Compress sleeps so the test runs fast — _ratelimit_delays at module
    # level is [60, 120]; patch to [0, 0] for the duration of this test.
    async def _no_sleep(*_a, **_kw):
        return None
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    assert rs._NIM_429_TRIPPED is False
    result = asyncio.run(run_sector(cfg, light_spec, [], ledger))
    assert result == light_spec.default
    assert rs._NIM_429_TRIPPED is True, "429 breaker should trip on exhausted retries"


def test_429_breaker_trip_emits_telemetry_event(light_spec, cfg, ledger, monkeypatch):
    _patch_agent_path(monkeypatch, Exception("429 too many requests"))

    async def _no_sleep(*_a, **_kw):
        return None
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    events: list[tuple] = []

    def fake_emit(event_name, **fields):
        events.append((event_name, fields))

    monkeypatch.setattr("jeeves.tools.telemetry.emit", fake_emit)

    asyncio.run(run_sector(cfg, light_spec, [], ledger))

    trip_events = [e for e in events if e[0] == "circuit_breaker_trip"]
    assert trip_events, f"expected circuit_breaker_trip event, got {events}"
    name, fields = trip_events[0]
    assert fields["breaker"] == "nim_429"
    assert fields["sector"] == "local_news"


def test_timeout_breaker_trips_after_consecutive_threshold(
    deep_spec, light_spec, cfg, ledger, monkeypatch,
):
    """Two consecutive 'Request timed out.' crashes flip the timeout breaker.
    Threshold is 2 — first crash increments counter to 1, second to 2 → trip."""
    _patch_agent_path(monkeypatch, Exception("Request timed out."))

    async def _no_sleep(*_a, **_kw):
        return None
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    # First sector crashes with timeout → counter=1, breaker NOT yet tripped
    result1 = asyncio.run(run_sector(cfg, deep_spec, [], ledger))
    assert result1 == deep_spec.default
    assert rs._NIM_TIMEOUT_CONSECUTIVE == 1
    assert rs._NIM_TIMEOUT_TRIPPED is False

    # Second sector crashes with timeout → counter=2, breaker NOW tripped
    result2 = asyncio.run(run_sector(cfg, light_spec, [], ledger))
    assert result2 == light_spec.default
    assert rs._NIM_TIMEOUT_CONSECUTIVE == 2
    assert rs._NIM_TIMEOUT_TRIPPED is True, "timeout breaker should trip after 2 consecutive"


def test_timeout_breaker_does_not_trip_on_non_timeout_crash(
    deep_spec, light_spec, cfg, ledger, monkeypatch,
):
    """A non-timeout agent crash (e.g. random exception) must NOT increment
    the consecutive-timeout counter — the breaker is timeout-specific."""
    _patch_agent_path(monkeypatch, Exception("some random parser error"))

    async def _no_sleep(*_a, **_kw):
        return None
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    asyncio.run(run_sector(cfg, deep_spec, [], ledger))
    asyncio.run(run_sector(cfg, light_spec, [], ledger))

    assert rs._NIM_TIMEOUT_CONSECUTIVE == 0
    assert rs._NIM_TIMEOUT_TRIPPED is False


def test_timeout_counter_resets_on_successful_sector(
    deep_spec, light_spec, cfg, ledger, monkeypatch,
):
    """Counter is for SUSTAINED timeouts. An isolated timeout followed by a
    successful sector resets the counter so a transient blip doesn't trip
    the breaker on the third sector."""

    # First call: timeout.  Patch FunctionAgent to raise on call 1, then
    # behave normally on call 2 (returning a parseable empty list payload).
    call_counter = {"n": 0}

    class _FlipAgent:
        def __init__(self, **_kw):
            self._n = call_counter["n"]
            call_counter["n"] += 1

        async def run(self, _msg):
            if self._n == 0:
                raise Exception("Request timed out.")
            # Second call: return a list-shape sector output. agent.run
            # returns an AgentChatResponse-like object whose str() is the
            # final assistant text. We mimic that with a dataclass-ish wrapper.
            resp = SimpleNamespace()
            resp.__str__ = lambda self_: '[]'  # type: ignore[assignment]
            return resp

    def factory(*_a, **_kw):
        return _FlipAgent()

    monkeypatch.setattr(
        "llama_index.core.agent.workflow.FunctionAgent", factory
    )
    monkeypatch.setattr(
        "jeeves.llm.build_kimi_llm", lambda *_a, **_kw: MagicMock()
    )
    monkeypatch.setattr(
        "jeeves.tools.all_search_tools", lambda *_a, **_kw: []
    )

    # Fake the quota guard so an empty-list response doesn't trigger
    # _deep_sector_forced_retry. Add a fake provider call to the ledger so
    # _quota_increased returns True.
    original_snapshot = rs._quota_snapshot
    original_increased = rs._quota_increased

    def fake_snapshot(_l):
        return {}

    def fake_increased(_before, _l):
        return True

    monkeypatch.setattr(rs, "_quota_snapshot", fake_snapshot)
    monkeypatch.setattr(rs, "_quota_increased", fake_increased)

    async def _no_sleep(*_a, **_kw):
        return None
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    # Sector 1: timeout → counter = 1
    asyncio.run(run_sector(cfg, deep_spec, [], ledger))
    assert rs._NIM_TIMEOUT_CONSECUTIVE == 1

    # Sector 2: success → counter must reset to 0
    asyncio.run(run_sector(cfg, light_spec, [], ledger))
    assert rs._NIM_TIMEOUT_CONSECUTIVE == 0
    assert rs._NIM_TIMEOUT_TRIPPED is False

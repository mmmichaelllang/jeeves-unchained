"""Sprint-19 slice E: tier semaphore + env override tests."""
from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    from jeeves.tools import rate_limits

    rate_limits.reset_for_tests()
    yield
    rate_limits.reset_for_tests()


def test_default_tier_limits():
    from jeeves.tools.rate_limits import current_limit

    assert current_limit("serper") == 8       # tier 1
    assert current_limit("tavily") == 4       # tier 2
    assert current_limit("playwright") == 1   # tier 3
    assert current_limit("jina_deepsearch") == 1  # tier 3


def test_unknown_provider_falls_to_one():
    from jeeves.tools.rate_limits import current_limit

    assert current_limit("nonexistent_xyz") == 1


def test_env_override_recognised(monkeypatch):
    from jeeves.tools.rate_limits import current_limit, reset_for_tests

    reset_for_tests()
    monkeypatch.setenv("JEEVES_RL_SERPER", "2")
    assert current_limit("serper") == 2


def test_env_override_invalid_value_falls_to_default(monkeypatch):
    from jeeves.tools.rate_limits import current_limit, reset_for_tests

    reset_for_tests()
    monkeypatch.setenv("JEEVES_RL_TAVILY", "not-a-number")
    assert current_limit("tavily") == 4


def test_acquire_serialises_calls_at_limit_one(monkeypatch):
    from jeeves.tools import rate_limits

    rate_limits.reset_for_tests()
    monkeypatch.setenv("JEEVES_RL_TINYFISH", "1")

    in_flight = 0
    max_seen = 0
    lock = threading.Lock()

    def worker():
        nonlocal in_flight, max_seen
        with rate_limits.acquire("tinyfish"):
            with lock:
                in_flight += 1
                max_seen = max(max_seen, in_flight)
            time.sleep(0.05)
            with lock:
                in_flight -= 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_seen == 1


def test_slow_acquire_emits_telemetry(monkeypatch, tmp_path):
    from jeeves.tools import rate_limits, telemetry

    telemetry._close()
    monkeypatch.setenv("JEEVES_TELEMETRY", "1")
    monkeypatch.setenv("JEEVES_TELEMETRY_DIR", str(tmp_path))
    monkeypatch.setenv("JEEVES_RL_PLAYWRIGHT_SEARCH", "1")
    rate_limits.reset_for_tests()

    started = threading.Event()
    release = threading.Event()

    def holder():
        with rate_limits.acquire("playwright_search"):
            started.set()
            release.wait(timeout=1.0)

    t = threading.Thread(target=holder)
    t.start()
    started.wait(timeout=1.0)

    # Second acquire blocks until release fires.
    def waiter():
        with rate_limits.acquire("playwright_search"):
            pass

    t2 = threading.Thread(target=waiter)
    t2.start()
    time.sleep(0.10)  # ensure waiter is queued
    release.set()
    t.join(timeout=1.0)
    t2.join(timeout=1.0)
    telemetry._close()

    files = list(tmp_path.glob("telemetry-*.jsonl"))
    assert files
    body = files[0].read_text(encoding="utf-8")
    assert "semaphore_wait_ms" in body

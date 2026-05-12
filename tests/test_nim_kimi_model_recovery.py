"""Tests for 2026-05-11 NIM Kimi model resilience.

NIM delisted `moonshotai/kimi-k2-instruct` between 2026-05-10 and 2026-05-11
without notice, killing Daily Pipeline #58 with:

  ValueError: No locally hosted moonshotai/kimi-k2-instruct was found.

The fix (jeeves/llm.py::build_kimi_llm):
  1. Catch ValueError("No locally hosted ...") on first instantiation.
  2. Probe /v1/models, prefer instruct variants, longest match.
  3. If probe fails, fall through a hardcoded chain.
  4. Cache the winning model at module level so subsequent calls skip the
     probe.

These tests monkeypatch `_build_kimi_class` so we never hit NIM. They
verify the contract: probe -> fallback -> reraise.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import jeeves.llm as llm_mod


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the module-level resolved-model cache between tests."""
    llm_mod._RESOLVED_KIMI_MODEL = None
    yield
    llm_mod._RESOLVED_KIMI_MODEL = None


def _cfg(model_id="moonshotai/kimi-k2-instruct"):
    return SimpleNamespace(
        kimi_model_id=model_id,
        nvidia_api_key="nv-fake",
        kimi_base_url="https://integrate.api.nvidia.com/v1",
    )


def _fake_cls_factory(supported: set[str]):
    """Build a fake KimiNVIDIA class that raises if model not in `supported`.

    Matches the real NVIDIANIM._validate_model contract: raises
    ValueError("No locally hosted <id> was found.") when the model
    string isn't in the live catalog.
    """
    instances = []

    class FakeKimiNVIDIA:
        def __init__(self, **kwargs):
            model = kwargs.get("model")
            if model not in supported:
                raise ValueError(f"No locally hosted {model} was found.")
            self.kwargs = kwargs
            instances.append(self)

    return FakeKimiNVIDIA, instances


# ---------------------------------------------------------------------------
# Happy path: configured model is hosted -> no probe, no warnings.
# ---------------------------------------------------------------------------

def test_build_kimi_llm_happy_path(monkeypatch):
    cls, instances = _fake_cls_factory({"moonshotai/kimi-k2-instruct"})
    monkeypatch.setattr(llm_mod, "_build_kimi_class", lambda: cls)

    probe_spy = MagicMock()
    monkeypatch.setattr(llm_mod, "_probe_nim_kimi_model", probe_spy)

    out = llm_mod.build_kimi_llm(_cfg())
    assert isinstance(out, cls)
    assert out.kwargs["model"] == "moonshotai/kimi-k2-instruct"
    probe_spy.assert_not_called()
    assert llm_mod._RESOLVED_KIMI_MODEL == "moonshotai/kimi-k2-instruct"


# ---------------------------------------------------------------------------
# Delisted-model recovery via /v1/models probe.
# ---------------------------------------------------------------------------

def test_build_kimi_llm_recovers_via_probe(monkeypatch):
    """Configured model 404s; /v1/models returns kimi-k2-instruct-0905; use it."""
    cls, instances = _fake_cls_factory({"moonshotai/kimi-k2-instruct-0905"})
    monkeypatch.setattr(llm_mod, "_build_kimi_class", lambda: cls)
    monkeypatch.setattr(
        llm_mod, "_probe_nim_kimi_model",
        lambda cfg: "moonshotai/kimi-k2-instruct-0905",
    )

    out = llm_mod.build_kimi_llm(_cfg())
    assert out.kwargs["model"] == "moonshotai/kimi-k2-instruct-0905"
    assert llm_mod._RESOLVED_KIMI_MODEL == "moonshotai/kimi-k2-instruct-0905"


def test_build_kimi_llm_falls_through_chain_when_probe_returns_none(monkeypatch):
    """Probe fails (network/auth); fall through _KIMI_FALLBACK_CHAIN."""
    cls, instances = _fake_cls_factory({"moonshotai/kimi-k2-instruct-0905"})
    monkeypatch.setattr(llm_mod, "_build_kimi_class", lambda: cls)
    monkeypatch.setattr(llm_mod, "_probe_nim_kimi_model", lambda cfg: None)

    out = llm_mod.build_kimi_llm(_cfg())
    # First entry in _KIMI_FALLBACK_CHAIN is the 0905 tag.
    assert out.kwargs["model"] == "moonshotai/kimi-k2-instruct-0905"


def test_build_kimi_llm_reraises_when_all_options_fail(monkeypatch):
    """Configured + probe + every fallback delisted -> raise ValueError."""
    cls, _ = _fake_cls_factory(set())  # nothing supported
    monkeypatch.setattr(llm_mod, "_build_kimi_class", lambda: cls)
    monkeypatch.setattr(
        llm_mod, "_probe_nim_kimi_model",
        lambda cfg: "moonshotai/kimi-k2-dead",
    )

    with pytest.raises(ValueError, match="No locally hosted"):
        llm_mod.build_kimi_llm(_cfg())


def test_build_kimi_llm_passes_through_unrelated_value_errors(monkeypatch):
    """A ValueError that ISN'T about model hosting should propagate untouched.

    Auth / base_url / kwargs validation errors must NOT trigger the
    recovery path - otherwise a typo in base_url would silently try
    every Kimi fallback against the wrong endpoint.
    """
    class BadCls:
        def __init__(self, **kwargs):
            raise ValueError("Invalid base_url scheme")

    monkeypatch.setattr(llm_mod, "_build_kimi_class", lambda: BadCls)
    probe_spy = MagicMock()
    monkeypatch.setattr(llm_mod, "_probe_nim_kimi_model", probe_spy)

    with pytest.raises(ValueError, match="Invalid base_url"):
        llm_mod.build_kimi_llm(_cfg())
    probe_spy.assert_not_called()


def test_build_kimi_llm_caches_resolved_model_across_calls(monkeypatch):
    """Second call in the same process must skip the probe."""
    cls, instances = _fake_cls_factory({"moonshotai/kimi-k2-instruct-0905"})
    monkeypatch.setattr(llm_mod, "_build_kimi_class", lambda: cls)

    probe_spy = MagicMock(return_value="moonshotai/kimi-k2-instruct-0905")
    monkeypatch.setattr(llm_mod, "_probe_nim_kimi_model", probe_spy)

    llm_mod.build_kimi_llm(_cfg())
    llm_mod.build_kimi_llm(_cfg())
    llm_mod.build_kimi_llm(_cfg())
    # Probe should have run ONCE on the first call; subsequent calls reuse
    # the cached _RESOLVED_KIMI_MODEL and skip the probe entirely.
    assert probe_spy.call_count == 1
    # Three instances built; all on the resolved model.
    assert len(instances) == 3
    assert all(i.kwargs["model"] == "moonshotai/kimi-k2-instruct-0905" for i in instances)


# ---------------------------------------------------------------------------
# _probe_nim_kimi_model behavior - preference & filtering.
# ---------------------------------------------------------------------------

def test_probe_prefers_longest_instruct_id(monkeypatch):
    """Among multiple kimi-* models, prefer the most-specific instruct ID."""
    import httpx

    payload = {"data": [
        {"id": "moonshotai/kimi-k2-thinking"},
        {"id": "moonshotai/kimi-k2-instruct"},
        {"id": "moonshotai/kimi-k2-instruct-0905"},
        {"id": "moonshotai/kimi-k2.6"},
        {"id": "meta/llama-3.3-70b-instruct"},  # noise - non-kimi
    ]}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return payload

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResp())

    out = llm_mod._probe_nim_kimi_model(_cfg())
    assert out == "moonshotai/kimi-k2-instruct-0905"


def test_probe_returns_none_on_network_failure(monkeypatch):
    """Any httpx exception -> None, do not propagate."""
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx, "get", boom)
    assert llm_mod._probe_nim_kimi_model(_cfg()) is None


def test_probe_returns_none_when_no_kimi_models(monkeypatch):
    """Catalog without any kimi-* models -> None."""
    import httpx

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"id": "meta/llama-3.3-70b-instruct"}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResp())
    assert llm_mod._probe_nim_kimi_model(_cfg()) is None

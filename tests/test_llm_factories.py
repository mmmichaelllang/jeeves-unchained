"""Unit tests for jeeves.llm — focused on the KimiNVIDIA tool-arg workaround."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _make_response(tool_calls):
    return SimpleNamespace(
        message=SimpleNamespace(additional_kwargs={"tool_calls": tool_calls})
    )


def _tool_call(tid, name, arguments):
    return SimpleNamespace(
        id=tid, function=SimpleNamespace(name=name, arguments=arguments)
    )


def test_kimi_tool_call_parser_handles_none_arguments():
    from jeeves.llm import _build_kimi_class

    cls = _build_kimi_class()
    parser = cls.get_tool_calls_from_response

    resp = _make_response([_tool_call("c1", "emit_session", None)])
    out = parser(SimpleNamespace(), resp)
    assert len(out) == 1
    assert out[0].tool_name == "emit_session"
    assert out[0].tool_kwargs == {}


def test_kimi_tool_call_parser_handles_empty_string_arguments():
    from jeeves.llm import _build_kimi_class

    parser = _build_kimi_class().get_tool_calls_from_response
    resp = _make_response([_tool_call("c2", "serper_search", "")])
    out = parser(SimpleNamespace(), resp)
    assert out[0].tool_kwargs == {}


def test_kimi_tool_call_parser_parses_valid_json():
    from jeeves.llm import _build_kimi_class

    parser = _build_kimi_class().get_tool_calls_from_response
    resp = _make_response([_tool_call("c3", "serper_search", '{"query":"snow"}')])
    out = parser(SimpleNamespace(), resp)
    assert out[0].tool_kwargs == {"query": "snow"}


def test_kimi_tool_call_parser_handles_invalid_json():
    from jeeves.llm import _build_kimi_class

    parser = _build_kimi_class().get_tool_calls_from_response
    resp = _make_response([_tool_call("c4", "tavily_search", "{not json")])
    out = parser(SimpleNamespace(), resp)
    assert out[0].tool_kwargs == {}


def test_kimi_tool_call_parser_errors_on_empty_when_requested():
    from jeeves.llm import _build_kimi_class

    parser = _build_kimi_class().get_tool_calls_from_response
    resp = _make_response([])
    with pytest.raises(ValueError):
        parser(SimpleNamespace(), resp, error_on_no_tool_call=True)
    # And the suppress mode returns []:
    assert parser(SimpleNamespace(), resp, error_on_no_tool_call=False) == []

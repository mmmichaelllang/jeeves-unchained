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


# ---------------------------------------------------------------------------
# _normalize_tool_kwargs tests
# ---------------------------------------------------------------------------

def _make_tool_call_block(tool_kwargs):
    """Create a minimal ToolCallBlock-like object for testing."""
    from llama_index.core.base.llms.types import ToolCallBlock
    return ToolCallBlock(tool_call_id="c1", tool_name="some_tool", tool_kwargs=tool_kwargs)


def _make_chat_message(blocks=None, tool_calls_in_kwargs=None):
    from llama_index.core.llms import ChatMessage, MessageRole
    msg = ChatMessage(role=MessageRole.ASSISTANT, content=None)
    if blocks:
        msg.blocks = blocks
    if tool_calls_in_kwargs is not None:
        msg.additional_kwargs["tool_calls"] = tool_calls_in_kwargs
    return msg


def test_normalize_tool_kwargs_empty_dict_becomes_string():
    """ToolCallBlock.tool_kwargs={} (empty dict) must become the string '{}'."""
    from jeeves.llm import _build_kimi_class

    cls = _build_kimi_class()
    block = _make_tool_call_block({})
    msg = _make_chat_message(blocks=[block])
    cls._normalize_tool_kwargs([msg])
    assert block.tool_kwargs == "{}"


def test_normalize_tool_kwargs_nonempty_dict_becomes_json():
    """ToolCallBlock.tool_kwargs=dict must become JSON string."""
    from jeeves.llm import _build_kimi_class
    import json

    cls = _build_kimi_class()
    block = _make_tool_call_block({"query": "Edmonds WA"})
    msg = _make_chat_message(blocks=[block])
    cls._normalize_tool_kwargs([msg])
    assert json.loads(block.tool_kwargs) == {"query": "Edmonds WA"}


def test_normalize_tool_kwargs_string_unchanged():
    """ToolCallBlock.tool_kwargs already a string is left as-is."""
    from jeeves.llm import _build_kimi_class

    cls = _build_kimi_class()
    block = _make_tool_call_block('{"query": "snow"}')
    msg = _make_chat_message(blocks=[block])
    cls._normalize_tool_kwargs([msg])
    assert block.tool_kwargs == '{"query": "snow"}'


def test_normalize_tool_kwargs_empty_string_becomes_json_string():
    """ToolCallBlock.tool_kwargs='' (empty string) must become '{}'."""
    from jeeves.llm import _build_kimi_class

    cls = _build_kimi_class()
    block = _make_tool_call_block("")
    msg = _make_chat_message(blocks=[block])
    cls._normalize_tool_kwargs([msg])
    assert block.tool_kwargs == "{}"



def test_normalize_tool_kwargs_fixes_additional_kwargs_none_args():
    """additional_kwargs tool_calls with function.arguments=None become '{}'."""
    from jeeves.llm import _build_kimi_class

    cls = _build_kimi_class()
    tc = SimpleNamespace(function=SimpleNamespace(arguments=None))
    msg = _make_chat_message(tool_calls_in_kwargs=[tc])
    cls._normalize_tool_kwargs([msg])
    assert tc.function.arguments == "{}"


def test_normalize_tool_kwargs_noop_on_non_tool_messages():
    """Messages without blocks or tool_calls are handled without error."""
    from jeeves.llm import _build_kimi_class
    from llama_index.core.llms import ChatMessage, MessageRole

    cls = _build_kimi_class()
    msg = ChatMessage(role=MessageRole.USER, content="Hello")
    cls._normalize_tool_kwargs([msg])  # must not raise

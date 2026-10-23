"""Translation between the codebase's OpenAI-style messages and the Anthropic
Messages API. No network: the HTTP layer isn't exercised here."""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture
def direct(sandbox):
    return importlib.import_module("server.openrouter_client.anthropic_direct")


def test_a_tool_round_trip_translates_both_ways(direct):
    messages = [
        {"role": "user", "content": "Hi, I'm Maria Garcia"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "lookup_patient", "arguments": '{"name": "Maria"}'}},
                {"id": "t2", "type": "function", "function": {"name": "record_intake", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": '{"found": true}'},
        {"role": "tool", "tool_call_id": "t2", "content": '{"recorded": []}'},
    ]
    out = direct._to_messages(messages)

    assert out[0] == {"role": "user", "content": "Hi, I'm Maria Garcia"}
    assert [b["type"] for b in out[1]["content"]] == ["tool_use", "tool_use"]
    assert out[1]["content"][0]["input"] == {"name": "Maria"}
    # Both results ride in one user message, matched by id.
    assert out[2]["role"] == "user"
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["t1", "t2"]
    assert len(out) == 3


def test_responses_come_back_in_the_openai_shape(direct):
    body = {
        "model": "claude-haiku-4-5-20251001",
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "Let me look you up."},
            {"type": "tool_use", "id": "toolu_1", "name": "lookup_patient", "input": {"name": "Maria"}},
        ],
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 2000, "output_tokens": 20},
    }
    out = direct._to_openai_response(body)
    message = out["choices"][0]["message"]
    assert message["content"] == "Let me look you up."
    call = message["tool_calls"][0]
    assert call["id"] == "toolu_1" and call["function"]["name"] == "lookup_patient"
    assert json.loads(call["function"]["arguments"]) == {"name": "Maria"}
    assert out["usage"]["prompt_tokens"] == 2100 and out["usage"]["cached_prompt_tokens"] == 2000


def test_tool_schemas_translate(direct):
    tools = [{"type": "function", "function": {"name": "hold_slot", "description": "Hold it",
                                               "parameters": {"type": "object", "properties": {}}}}]
    assert direct._to_tools(tools) == [
        {"name": "hold_slot", "description": "Hold it", "input_schema": {"type": "object", "properties": {}}}
    ]


def test_only_verified_models_go_direct(direct):
    assert direct.supports("anthropic/claude-haiku-4.5")
    assert not direct.supports("anthropic/claude-sonnet-4")

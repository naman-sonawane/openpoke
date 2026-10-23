"""Direct Anthropic Messages API path behind request_chat_completion.

Callers keep speaking the OpenAI-style format the rest of the codebase uses
(messages with tool_calls, role "tool" results, function schemas); this module
translates to the Messages API and translates the reply back, so switching
providers changes no call sites.

Going direct also removes the OpenRouter hop and is the route a BAA can cover
(Anthropic's HIPAA-ready API), and it enables prompt caching of the static
system prompt.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024

#: Models verified against the direct API. Anything else stays on OpenRouter.
MODEL_IDS = {
    "anthropic/claude-haiku-4.5": "claude-haiku-4-5",
}

_RETRYABLE = {429, 500, 502, 503, 529}


def supports(model: str) -> bool:
    return model in MODEL_IDS


def _to_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id"),
                "content": message.get("content") or "",
            }
            # Results for one assistant turn's tool calls share a user message.
            previous = out[-1] if out else None
            if (
                previous
                and previous["role"] == "user"
                and isinstance(previous["content"], list)
                and previous["content"]
                and previous["content"][0].get("type") == "tool_result"
            ):
                previous["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif role == "assistant":
            blocks: List[Dict[str, Any]] = []
            if (message.get("content") or "").strip():
                blocks.append({"type": "text", "text": message["content"]})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": function.get("name"),
                        "input": arguments if isinstance(arguments, dict) else {},
                    }
                )
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "..."}]})
        else:
            out.append({"role": "user", "content": message.get("content") or ""})
    return out


def _to_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    converted = []
    for tool in tools or []:
        function = tool.get("function") or {}
        converted.append(
            {
                "name": function.get("name"),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def _to_openai_response(body: Dict[str, Any]) -> Dict[str, Any]:
    content = body.get("content") or []
    text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
    tool_calls = [
        {
            "id": block.get("id"),
            "type": "function",
            "function": {"name": block.get("name"), "arguments": json.dumps(block.get("input") or {})},
        }
        for block in content
        if block.get("type") == "tool_use"
    ]
    usage = body.get("usage") or {}
    cached = usage.get("cache_read_input_tokens") or 0
    written = usage.get("cache_creation_input_tokens") or 0
    return {
        "provider": "Anthropic",
        "model": body.get("model"),
        "choices": [
            {
                "message": {"role": "assistant", "content": text, "tool_calls": tool_calls},
                "finish_reason": body.get("stop_reason"),
            }
        ],
        "usage": {
            "prompt_tokens": (usage.get("input_tokens") or 0) + cached + written,
            "completion_tokens": usage.get("output_tokens") or 0,
            "cached_prompt_tokens": cached,
        },
    }


async def complete(
    *,
    api_key: str,
    model: str,
    messages: List[Dict[str, Any]],
    system: Optional[str],
    tools: Optional[List[Dict[str, Any]]],
    max_tokens: Optional[int],
    error_type: type,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": MODEL_IDS[model],
        "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
        "messages": _to_messages(messages),
    }
    if system:
        # Cache the static system prompt (and the tool list rendered before
        # it). Repeat calls then read it at a fraction of the input price.
        payload["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    if tools:
        payload["tools"] = _to_tools(tools)

    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    async with httpx.AsyncClient() as client:
        for attempt in range(2):
            try:
                response = await client.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=60.0)
            except httpx.HTTPError as exc:
                if attempt == 0:
                    continue
                raise error_type(f"Anthropic request failed: {exc}") from exc
            if response.status_code in _RETRYABLE and attempt == 0:
                await asyncio.sleep(0.5)
                continue
            if response.status_code != 200:
                try:
                    detail = response.json().get("error", {}).get("message", response.text)
                except ValueError:
                    detail = response.text
                raise error_type(f"Anthropic request failed ({response.status_code}): {detail}")
            return _to_openai_response(response.json())
    raise error_type("Anthropic request failed: no response")

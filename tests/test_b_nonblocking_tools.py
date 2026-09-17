"""Fix B - synchronous tool I/O must not block the event loop.

Before the fix, ExecutionAgentRuntime._execute_tool called synchronous Gmail and
trigger tools inline, so N agents serialised on their I/O and the loop stalled
for the full duration of each call.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from harness import LoopWatchdog, text_response, tool_call_response

GMAIL_LATENCY = 0.2
LLM_LATENCY = 0.01


def _one_gmail_call_then_finish(agent_name, system, messages):
    if len(messages) <= 1:
        return tool_call_response("gmail_list_drafts", "{}"), LLM_LATENCY
    return text_response("done"), LLM_LATENCY


@pytest.mark.asyncio
async def test_concurrent_agents_do_not_serialise_on_gmail(sandbox, fake_llm, fake_gmail):
    """Eight agents each making one 200ms blocking call finish in about 200ms."""
    fake_llm(_one_gmail_call_then_finish)
    gmail = fake_gmail(GMAIL_LATENCY, mode="blocking")

    runtime_mod = sandbox.modules["exec_runtime"]
    agents = [runtime_mod.ExecutionAgentRuntime(agent_name=f"b-conc-{i}") for i in range(8)]

    start = time.perf_counter()
    results = await asyncio.gather(*(a.execute("List drafts.") for a in agents))
    elapsed = time.perf_counter() - start

    assert all(r.success for r in results)
    assert gmail.call_count == 8
    # Serialised would be >= 1.6s. Allow generous headroom for thread pool and
    # the synchronous log writes that still happen per agent.
    assert elapsed < 1.0, f"agents serialised: {elapsed:.3f}s for 8 x {GMAIL_LATENCY}s"


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_gmail_call(sandbox, fake_llm, fake_gmail):
    """A blocking tool call must not stall other loop work."""
    fake_llm(_one_gmail_call_then_finish)
    fake_gmail(0.5, mode="blocking")

    runtime_mod = sandbox.modules["exec_runtime"]
    watchdog = LoopWatchdog(interval=0.01)
    watchdog.start()
    await asyncio.sleep(0.05)

    runtime = runtime_mod.ExecutionAgentRuntime(agent_name="b-watchdog")
    await runtime.execute("List drafts.")
    await watchdog.stop()

    # Pre-fix this was ~500ms. Windows timer granularity needs a loose bound.
    assert watchdog.max_lateness_ms < 150, (
        f"loop stalled {watchdog.max_lateness_ms:.0f}ms during a blocking tool call"
    )


@pytest.mark.asyncio
async def test_async_tools_are_still_awaited_directly(sandbox, fake_llm, fake_gmail):
    """task_email_search is a coroutine function and must not go via a thread."""
    fake_gmail(0.0, mode="blocking")
    seen = {}

    def responder(agent_name, system, messages):
        if len(messages) <= 1:
            return (
                tool_call_response(
                    "task_email_search", '{"search_query": "lease renewal"}'
                ),
                LLM_LATENCY,
            )
        seen["tool_round"] = messages[-1]
        return text_response("done"), LLM_LATENCY

    fake_llm(responder)

    runtime_mod = sandbox.modules["exec_runtime"]
    result = await runtime_mod.ExecutionAgentRuntime(agent_name="b-async").execute("Find it.")

    assert result.success
    assert "task_email_search" in (result.tools_executed or [])
    assert seen["tool_round"]["role"] == "tool"


@pytest.mark.asyncio
async def test_tool_exceptions_still_surface_as_failed_results(sandbox, fake_llm):
    """Moving execution into a thread must not swallow or reshape errors."""

    def responder(agent_name, system, messages):
        if len(messages) <= 1:
            return tool_call_response("gmail_list_drafts", "{}"), LLM_LATENCY
        return text_response("handled"), LLM_LATENCY

    fake_llm(responder)

    def boom(*args, **kwargs):
        raise RuntimeError("composio exploded")

    sandbox.modules["exec_gmail"].execute_gmail_tool = boom

    runtime_mod = sandbox.modules["exec_runtime"]
    runtime = runtime_mod.ExecutionAgentRuntime(agent_name="b-error")
    success, payload = await runtime._execute_tool("gmail_list_drafts", {})

    assert success is False
    assert "composio exploded" in payload["error"]

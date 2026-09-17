"""Fix C - batches are scoped to an interaction turn and cannot be held hostage.

Before the fix a single module-level _BatchState collected every agent from
every turn, dispatched only when pending hit exactly zero.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from harness import text_response


def _durations_responder(durations, default=0.05):
    """Response text omits the agent name so payload occurrences stay countable.

    _format_batch_payload already prefixes each line with "[SUCCESS] <name>:".
    """

    def responder(agent_name, system, messages):
        return text_response("done."), durations.get(agent_name, default)

    return responder


@pytest.fixture
def manager(sandbox):
    """A batch manager whose dispatches are captured instead of sent onward."""
    bm = sandbox.modules["batch_manager"]
    dispatches = []

    async def record(self, payload):
        dispatches.append((time.perf_counter(), payload))

    original = bm.ExecutionBatchManager._dispatch_to_interaction_agent
    bm.ExecutionBatchManager._dispatch_to_interaction_agent = record
    yield bm, dispatches
    bm.ExecutionBatchManager._dispatch_to_interaction_agent = original


@pytest.mark.asyncio
async def test_separate_turns_get_separate_batches(sandbox, fake_llm, manager):
    """Two turns dispatching concurrently must not be merged into one payload."""
    bm, dispatches = manager
    fake_llm(_durations_responder({"turnA-slow": 1.2, "turnB-fast": 0.05}))
    mgr = bm.ExecutionBatchManager(max_hold_seconds=0)

    async def turn(turn_id, agent_name):
        token = bm.set_turn_id(turn_id)
        try:
            await mgr.execute_agent(agent_name, "go")
        finally:
            bm.reset_turn_id(token)

    await asyncio.gather(turn("turn-a", "turnA-slow"), turn("turn-b", "turnB-fast"))

    assert len(dispatches) == 2, f"expected one payload per turn, got {len(dispatches)}"
    for _, payload in dispatches:
        assert len(payload.strip().splitlines()) == 1
    joined = " ".join(p for _, p in dispatches)
    assert "turnA-slow" in joined and "turnB-fast" in joined


@pytest.mark.asyncio
async def test_agents_in_one_turn_still_batch_together(sandbox, fake_llm, manager):
    """Same-turn fan-out keeps its single combined report."""
    bm, dispatches = manager
    fake_llm(_durations_responder({}))
    mgr = bm.ExecutionBatchManager(max_hold_seconds=0)

    token = bm.set_turn_id("turn-shared")
    try:
        await asyncio.gather(*(mgr.execute_agent(f"sibling-{i}", "go") for i in range(3)))
    finally:
        bm.reset_turn_id(token)

    assert len(dispatches) == 1
    assert len(dispatches[0][1].strip().splitlines()) == 3


@pytest.mark.asyncio
async def test_hold_timer_releases_finished_agents_before_a_straggler(
    sandbox, fake_llm, manager
):
    """A fast agent reports on the hold timer rather than waiting for a slow sibling."""
    bm, dispatches = manager
    fake_llm(_durations_responder({"hold-fast": 0.05, "hold-slow": 1.5}))
    mgr = bm.ExecutionBatchManager(max_hold_seconds=0.3)

    token = bm.set_turn_id("turn-hold")
    start = time.perf_counter()
    try:
        task = asyncio.gather(
            mgr.execute_agent("hold-fast", "go"), mgr.execute_agent("hold-slow", "go")
        )
        # First flush should carry the fast agent well before the slow one lands.
        await asyncio.sleep(0.6)
        assert dispatches, "hold timer did not flush the completed agent"
        first_at, first_payload = dispatches[0]
        assert "hold-fast" in first_payload
        assert "hold-slow" not in first_payload
        assert first_at - start < 1.0
        await task
    finally:
        bm.reset_turn_id(token)
        await mgr.shutdown()

    assert any("hold-slow" in payload for _, payload in dispatches)


@pytest.mark.asyncio
async def test_no_results_are_dropped_after_a_hold_flush(sandbox, fake_llm, manager):
    """Every agent must appear in exactly one dispatched payload."""
    bm, dispatches = manager
    fake_llm(_durations_responder({"drop-a": 0.05, "drop-b": 0.05, "drop-c": 1.2}))
    mgr = bm.ExecutionBatchManager(max_hold_seconds=0.3)

    token = bm.set_turn_id("turn-drop")
    try:
        await asyncio.gather(
            *(mgr.execute_agent(name, "go") for name in ("drop-a", "drop-b", "drop-c"))
        )
    finally:
        bm.reset_turn_id(token)
        await mgr.shutdown()

    joined = "\n".join(p for _, p in dispatches)
    for name in ("drop-a", "drop-b", "drop-c"):
        assert joined.count(name) == 1, f"{name} appeared {joined.count(name)} times"


@pytest.mark.asyncio
async def test_detached_callers_do_not_share_a_batch(sandbox, fake_llm, manager):
    """Trigger and watcher firings have no turn; they must not merge with each other."""
    bm, dispatches = manager
    fake_llm(_durations_responder({}))
    mgr = bm.ExecutionBatchManager(max_hold_seconds=0)

    async def detached(agent_name):
        await mgr.execute_agent(agent_name, "go")

    await asyncio.gather(
        asyncio.create_task(detached("trigger-1")), asyncio.create_task(detached("trigger-2"))
    )

    assert len(dispatches) == 2
    for _, payload in dispatches:
        assert len(payload.strip().splitlines()) == 1

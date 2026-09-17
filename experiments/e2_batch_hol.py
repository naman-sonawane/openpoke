"""E2 - How long does a finished agent wait for the batch to drain?

interaction_agent/tools.py:108 holds a single module level ExecutionBatchManager,
and that manager holds a single _BatchState (batch_manager.py:44). Any agent
dispatched while a batch is open joins it (batch_manager.py:100-115), and results
are only forwarded when pending hits exactly zero (batch_manager.py:137).

Part 1 measures the delay between an agent finishing and its result reaching the
interaction agent, for a batch of N-1 fast agents and one slow one.

Part 2 dispatches two unrelated turns while the first is in flight and counts how
many logically distinct turns get concatenated into a single <agent_message>.
"""

from __future__ import annotations

import asyncio
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import FakeLLM, bootstrap, rule, summarize, text_response  # noqa: E402

FAST_DURATION = 0.10
SLOW_DURATION = 3.00
AGENT_COUNTS = [2, 4, 8]
TRIALS = 3

# Records populated by the patched methods.
DISPATCHES: List[Tuple[float, str]] = []
FINISHED: Dict[str, float] = {}


def install_probes(sandbox) -> None:
    """Timestamp batch dispatch and per-agent completion. Harness side only."""
    batch_manager = sandbox.modules["batch_manager"]
    exec_runtime = sandbox.modules["exec_runtime"]

    async def record_dispatch(self, payload: str) -> None:
        DISPATCHES.append((time.perf_counter(), payload))

    batch_manager.ExecutionBatchManager._dispatch_to_interaction_agent = record_dispatch

    original_execute = exec_runtime.ExecutionAgentRuntime.execute

    async def timed_execute(self, instructions):
        result = await original_execute(self, instructions)
        FINISHED[self.agent.name] = time.perf_counter()
        return result

    exec_runtime.ExecutionAgentRuntime.execute = timed_execute


def make_responder(durations: Dict[str, float]):
    """Each agent answers in one round; its scripted duration is the LLM latency."""

    def responder(agent_name, system, messages):
        return text_response(f"{agent_name}: done."), durations.get(agent_name, FAST_DURATION)

    return responder


def reset_batch(sandbox) -> None:
    """Clear manager state. Handles the pre-fix single batch and the post-fix dict."""
    manager = sandbox.modules["inter_tools"]._EXECUTION_BATCH_MANAGER
    if hasattr(manager, "_batches"):
        for state in manager._batches.values():
            if getattr(state, "hold_task", None) is not None:
                state.hold_task.cancel()
        manager._batches.clear()
    else:
        manager._batch_state = None
    manager._pending.clear()
    DISPATCHES.clear()
    FINISHED.clear()


class turn_scope:
    """Set an interaction turn id when the sandbox supports them.

    Pre-fix refs have no turn concept, so this is a no-op there and both refs
    are driven identically: N agents dispatched from one logical turn.
    """

    def __init__(self, sandbox, turn_id: str):
        self._bm = sandbox.modules["batch_manager"]
        self._turn_id = turn_id
        self._token = None

    def __enter__(self):
        if hasattr(self._bm, "set_turn_id"):
            self._token = self._bm.set_turn_id(self._turn_id)
        return self

    def __exit__(self, *exc):
        if self._token is not None:
            self._bm.reset_turn_id(self._token)
        return False


async def await_dispatch(timeout: float = 30.0) -> None:
    deadline = time.perf_counter() + timeout
    while not DISPATCHES and time.perf_counter() < deadline:
        await asyncio.sleep(0.005)
    if not DISPATCHES:
        raise RuntimeError("batch never dispatched")


async def run_hol(sandbox, n_agents: int) -> Dict[str, float]:
    """N-1 fast agents plus one slow agent, all dispatched in one turn."""
    inter_tools = sandbox.modules["inter_tools"]
    reset_batch(sandbox)

    tag = f"n{n_agents}-{time.perf_counter_ns()}"
    fast_names = [f"fast-{tag}-{i}" for i in range(n_agents - 1)]
    slow_name = f"slow-{tag}"
    durations = {name: FAST_DURATION for name in fast_names}
    durations[slow_name] = SLOW_DURATION

    FakeLLM(make_responder(durations)).install(sandbox)

    t0 = time.perf_counter()
    with turn_scope(sandbox, f"turn-hol-{tag}"):
        for name in fast_names + [slow_name]:
            inter_tools.send_message_to_agent(name, "Do the thing.")

    await await_dispatch()
    dispatch_t, payload = DISPATCHES[0]

    if len(FINISHED) != n_agents:
        raise RuntimeError(f"expected {n_agents} finishes, saw {len(FINISHED)}")

    fast_delays = [dispatch_t - FINISHED[name] for name in fast_names]
    all_delays = [dispatch_t - t for t in FINISHED.values()]

    return {
        "time_to_first_result": dispatch_t - t0,
        "mean_delay": statistics.mean(all_delays),
        "max_delay": max(all_delays),
        "fast_agent_delay": statistics.mean(fast_delays) if fast_delays else 0.0,
        "wasted_agent_seconds": sum(all_delays),
        "agents_in_payload": len(payload.strip().splitlines()),
    }


async def run_cross_turn(sandbox) -> Dict[str, float]:
    """Turn A dispatches a slow agent; turn B dispatches an unrelated fast one."""
    inter_tools = sandbox.modules["inter_tools"]
    reset_batch(sandbox)

    tag = time.perf_counter_ns()
    a_name = f"turnA-flight-booking-{tag}"
    b_name = f"turnB-dentist-reminder-{tag}"
    durations = {a_name: SLOW_DURATION, b_name: FAST_DURATION}
    FakeLLM(make_responder(durations)).install(sandbox)

    with turn_scope(sandbox, f"turn-a-{tag}"):
        inter_tools.send_message_to_agent(a_name, "Find my flight confirmation.")
    await asyncio.sleep(0.1)  # a separate, later user turn
    with turn_scope(sandbox, f"turn-b-{tag}"):
        inter_tools.send_message_to_agent(b_name, "Remind me about the dentist.")

    await await_dispatch()
    # Wait for both agents so every dispatch is counted, not just the first.
    deadline = time.perf_counter() + SLOW_DURATION + 5.0
    while len(FINISHED) < 2 and time.perf_counter() < deadline:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)

    worst = max(
        len({m.group(1) for m in re.finditer(r"(turn[AB])-", payload)})
        for _, payload in DISPATCHES
    )
    return {
        "n_dispatches": len(DISPATCHES),
        "agents_in_payload": max(
            len(payload.strip().splitlines()) for _, payload in DISPATCHES
        ),
        "distinct_turns_in_payload": worst,
        "payload": "\n".join(
            f"[dispatch {k + 1}] {q.strip()}" for k, (_, q) in enumerate(DISPATCHES)
        ),
    }


async def run_hold_cap(sandbox) -> Dict[str, float]:
    """One fast agent beside a straggler that outlives the hold window.

    Pre-fix the fast agent waits for the straggler, bounded only by the 90s
    execution timeout. Post-fix the hold timer caps that wait.
    """
    inter_tools = sandbox.modules["inter_tools"]
    manager = inter_tools._EXECUTION_BATCH_MANAGER
    hold = getattr(manager, "max_hold_seconds", None)
    reset_batch(sandbox)

    tag = time.perf_counter_ns()
    fast_name = f"cap-fast-{tag}"
    straggler = f"cap-straggler-{tag}"
    FakeLLM(make_responder({fast_name: 0.1, straggler: 12.0})).install(sandbox)

    with turn_scope(sandbox, f"turn-cap-{tag}"):
        inter_tools.send_message_to_agent(fast_name, "Quick one.")
        inter_tools.send_message_to_agent(straggler, "Slow one.")

    await await_dispatch(timeout=30.0)
    first_at, first_payload = DISPATCHES[0]
    return {
        "hold_seconds": hold,
        "fast_agent_delay": first_at - FINISHED[fast_name],
        "first_payload_has_fast": fast_name in first_payload,
    }


async def main() -> None:
    sandbox = bootstrap()
    try:
        install_probes(sandbox)

        print(rule("E2: head of line blocking in the shared execution batch"))
        print(f"Fast agent       {FAST_DURATION * 1000:.0f} ms")
        print(f"Slow agent       {SLOW_DURATION * 1000:.0f} ms  (one per batch)")
        print(f"Latency spread   {(SLOW_DURATION - FAST_DURATION) * 1000:.0f} ms")
        print(f"Trials           {TRIALS} per cell, median reported")
        print()

        results: Dict[int, Dict[str, float]] = {}
        for n in AGENT_COUNTS:
            samples = [await run_hol(sandbox, n) for _ in range(TRIALS)]
            results[n] = {
                key: statistics.median(s[key] for s in samples) for key in samples[0]
            }

        header = (
            f"{'N':>3}  {'first result':>13}  {'mean delay':>11}  {'max delay':>10}  "
            f"{'fast agent':>11}  {'wasted':>9}"
        )
        print(header)
        print("-" * len(header))
        for n in AGENT_COUNTS:
            r = results[n]
            print(
                f"{n:>3}  {r['time_to_first_result']:>11.3f}s  {r['mean_delay']:>9.3f}s  "
                f"{r['max_delay']:>8.3f}s  {r['fast_agent_delay']:>9.3f}s  "
                f"{r['wasted_agent_seconds']:>7.3f}s"
            )

        print()
        print(rule("part 2: cross turn contamination"))
        cross = await run_cross_turn(sandbox)
        print(f"  dispatches to the interaction agent : {cross['n_dispatches']}")
        print(f"  agents in that single payload       : {cross['agents_in_payload']}")
        print(f"  distinct user turns in that payload : {cross['distinct_turns_in_payload']}")
        print()
        for line in str(cross["payload"]).splitlines():
            print(f"      {line}")

        print()
        print(rule("part 3: straggler beyond the hold window"))
        cap = await run_hold_cap(sandbox)
        hold = cap["hold_seconds"]
        print(f"  hold window                    : {hold if hold is not None else 'none (pre-fix)'}")
        print(f"  straggler runtime              : 12.0s")
        print(f"  fast agent waited              : {cap['fast_agent_delay']:.3f}s")
        print(f"  first payload carried the fast agent: {cap['first_payload_has_fast']}")

        print()
        print(rule("headline"))
        big = results[max(AGENT_COUNTS)]
        spread = SLOW_DURATION - FAST_DURATION
        print(f"  At N={max(AGENT_COUNTS)}, a fast agent's result sits finished but undelivered for")
        print(f"  {big['fast_agent_delay']:.3f}s ({big['fast_agent_delay'] / spread * 100:.0f}% of the injected latency spread).")
        print(f"  Total wasted agent-seconds per batch: {big['wasted_agent_seconds']:.3f}s,"
              f" growing linearly with N.")
        print(f"  Time to first result is {big['time_to_first_result']:.3f}s regardless of how many")
        print(f"  agents already finished; it tracks the slowest member only.")
        print(f"  {cross['distinct_turns_in_payload']} unrelated user turns were merged into one <agent_message>.")
        print()
        print(rule("caveats"))
        print("  - The delay equals the latency spread I injected, by construction. What")
        print("    the code determines is that the cost IS the spread and there is no")
        print("    mechanism to avoid it, not that the spread is 2.9s in production.")
        print("  - The real timeout is 90s (batch_manager.py:40). The bad case is a hung")
        print("    agent holding finished siblings for 90s; the 3s slow agent here is a")
        print("    mild stand-in and understates that.")
        print("  - Driven through send_message_to_agent deliberately. trigger_scheduler.py:92")
        print("    builds its own ExecutionBatchManager per firing, so trigger-initiated")
        print("    agents never share this batch and would have shown zero effect.")
    finally:
        sandbox.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

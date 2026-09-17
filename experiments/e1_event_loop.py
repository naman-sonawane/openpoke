"""E1 - Does one agent's Gmail call stall all the others?

Every Gmail tool in the execution agent is a synchronous call
(services/gmail/client.py:467 execute_gmail_tool) invoked directly on the event
loop at execution_agent/runtime.py:231. This measures whether N concurrent
agents actually run concurrently.

Two arms, identical latencies:
  blocking - execute_gmail_tool uses time.sleep, as the real Composio SDK does
  async    - identical wait, but yielded back to the loop

Headline number: seconds of wall clock added per additional agent, as the slope
of a least squares fit over N. The async arm is the counterfactual floor.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    FakeGmail,
    FakeLLM,
    LoopWatchdog,
    bootstrap,
    linear_fit,
    rule,
    summarize,
    text_response,
    tool_call_response,
)

# Parameters. Both are dials chosen by the harness, not measured from
# production; see the caveats printed at the end.
LLM_LATENCY = 0.05      # per LLM round trip, non-blocking
GMAIL_LATENCY = 0.20    # per Gmail call, blocking in the blocking arm
AGENT_COUNTS = [1, 2, 4, 8, 16]
TRIALS = 5


def make_responder():
    """One Gmail tool call, then a final answer.

    Stateless: the execution agent's message array is [user] on the first call
    and [user, assistant, tool] on the second.
    """

    def responder(agent_name, system, messages):
        if len(messages) <= 1:
            return tool_call_response("gmail_list_drafts", "{}"), LLM_LATENCY
        return text_response(f"{agent_name}: listed drafts."), LLM_LATENCY

    return responder


async def run_arm(sandbox, mode: str, n_agents: int) -> Dict[str, float]:
    exec_runtime = sandbox.modules["exec_runtime"]

    llm = FakeLLM(make_responder())
    llm.install(sandbox)
    gmail = FakeGmail(GMAIL_LATENCY, mode=mode)
    gmail.install(sandbox)

    watchdog = LoopWatchdog(interval=0.01)
    watchdog.start()
    await asyncio.sleep(0.05)  # let the watchdog establish a baseline

    runtimes = [
        exec_runtime.ExecutionAgentRuntime(agent_name=f"e1-{mode}-{n_agents}-{i}")
        for i in range(n_agents)
    ]

    t0 = time.perf_counter()
    results = await asyncio.gather(*(r.execute("List my drafts.") for r in runtimes))
    wall = time.perf_counter() - t0

    await watchdog.stop()

    failed = [r for r in results if not r.success]
    if failed:
        raise RuntimeError(f"{len(failed)} agents failed: {failed[0].error}")
    if gmail.call_count != n_agents:
        raise RuntimeError(f"expected {n_agents} gmail calls, saw {gmail.call_count}")

    return {
        "wall": wall,
        "max_lateness_ms": watchdog.max_lateness_ms,
        "p95_lateness_ms": watchdog.p95_lateness_ms,
    }


async def main() -> None:
    sandbox = bootstrap()
    try:
        print(rule("E1: event loop starvation from synchronous Gmail calls"))
        print(f"LLM latency      {LLM_LATENCY * 1000:.0f} ms/round (non-blocking, 2 rounds per agent)")
        print(f"Gmail latency    {GMAIL_LATENCY * 1000:.0f} ms/call  (1 call per agent)")
        print(f"Trials           {TRIALS} per cell, median reported")
        print()

        table: Dict[str, Dict[int, Dict[str, float]]] = {"blocking": {}, "async": {}}

        for mode in ("blocking", "async"):
            for n in AGENT_COUNTS:
                samples = [await run_arm(sandbox, mode, n) for _ in range(TRIALS)]
                table[mode][n] = {
                    "wall": statistics.median(s["wall"] for s in samples),
                    "wall_stdev": summarize([s["wall"] for s in samples])["stdev"],
                    "max_lateness_ms": statistics.median(
                        s["max_lateness_ms"] for s in samples
                    ),
                    "p95_lateness_ms": statistics.median(
                        s["p95_lateness_ms"] for s in samples
                    ),
                }

        header = (
            f"{'N':>3}  {'blocking (s)':>13} {'+/-':>6}  {'async (s)':>10} {'+/-':>6}  "
            f"{'ratio':>6}  {'loop stall p95':>15}  {'loop stall max':>15}"
        )
        print(header)
        print("-" * len(header))
        for n in AGENT_COUNTS:
            b = table["blocking"][n]
            a = table["async"][n]
            print(
                f"{n:>3}  {b['wall']:>13.3f} {b['wall_stdev']:>6.3f}  "
                f"{a['wall']:>10.3f} {a['wall_stdev']:>6.3f}  "
                f"{b['wall'] / a['wall']:>6.2f}x  "
                f"{b['p95_lateness_ms']:>12.1f} ms  {b['max_lateness_ms']:>12.1f} ms"
            )

        xs = [float(n) for n in AGENT_COUNTS]
        b_slope, b_int, b_r2 = linear_fit(xs, [table["blocking"][n]["wall"] for n in AGENT_COUNTS])
        a_slope, a_int, a_r2 = linear_fit(xs, [table["async"][n]["wall"] for n in AGENT_COUNTS])

        print()
        print(rule("headline"))
        print(f"  blocking: {b_slope * 1000:7.1f} ms of wall clock per additional agent"
              f"   (intercept {b_int:.3f}s, r2 {b_r2:.4f})")
        print(f"  async:    {a_slope * 1000:7.1f} ms of wall clock per additional agent"
              f"   (intercept {a_int:.3f}s, r2 {a_r2:.4f})")
        print(f"  injected Gmail latency was {GMAIL_LATENCY * 1000:.0f} ms;"
              f" the blocking arm recovers {b_slope / GMAIL_LATENCY * 100:.0f}% of it as per-agent cost")
        print()
        idle = table["async"][max(AGENT_COUNTS)]["p95_lateness_ms"]
        busy = table["blocking"][max(AGENT_COUNTS)]["p95_lateness_ms"]
        print(f"  loop responsiveness at N={max(AGENT_COUNTS)}:"
              f" p95 stall {busy:.1f} ms blocking vs {idle:.1f} ms async")
        print()
        print(rule("caveats"))
        print("  - The 200 ms Gmail figure is a dial in this harness. The slope tracks it")
        print("    by construction; what the code determines is that the slope is nonzero")
        print("    and linear rather than flat.")
        print("  - Real LLM calls take seconds, not 50 ms. Against that baseline the")
        print("    blocking cost is a smaller FRACTION of turn time, so do not read the")
        print("    ratio column as a production slowdown. The slope is the durable result.")
        print("  - Agents here make one Gmail call each. Real agents make several, and")
        print("    task_email_search makes one per search iteration.")
    finally:
        sandbox.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

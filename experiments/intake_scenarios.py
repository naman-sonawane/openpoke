"""End-to-end intake scenarios against a running server.

Each scenario resets the call (DELETE /chat/history), lets an LLM play the
caller from a short persona, runs every turn through POST /voice/turn, and then
checks the structured call state, not the wording. Prints the transcripts and
a pass/fail table with per-turn latency.

    python -m server.server            # in another terminal
    python experiments/intake_scenarios.py [scenario ...]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from server.config import get_settings  # noqa: E402  (also loads .env)

BASE = os.environ.get("OPENPOKE_URL", "http://localhost:8001/api/v1")
CALLER_MODEL = os.environ.get("OPENPOKE_CALLER_MODEL", "anthropic/claude-haiku-4.5")
MAX_TURNS = 16

Check = Callable[[Dict[str, Any], List[Dict[str, str]]], Optional[str]]


@dataclass
class Scenario:
    name: str
    persona: str
    checks: List[Check]
    demo: Dict[str, bool] = field(default_factory=dict)


def expect_stage(stage: str) -> Check:
    return lambda s, _: None if s["stage"] == stage else f"stage {s['stage']!r}, expected {stage!r}"


def expect_priority(level: str) -> Check:
    def check(s, _):
        got = (s.get("priority") or {}).get("level")
        return None if got == level else f"priority {got!r}, expected {level!r}"
    return check


def expect_handoff(reason: str) -> Check:
    def check(s, _):
        got = (s.get("handoff") or {}).get("reason")
        return None if got == reason else f"handoff reason {got!r}, expected {reason!r}"
    return check


def expect_no_booking(s, _):
    return None if not s.get("booking") else "a booking was made"


def expect_callback_confirmed(s, _):
    cb = s.get("callback_number") or {}
    return None if cb.get("confirmed_by_caller") else "callback number never confirmed"


def expect_event(kind: str) -> Check:
    return lambda s, _: None if any(e["kind"] == kind for e in s["events"]) else f"no {kind!r} event"


_ADVICE_RE = re.compile(r"\b(you should take|try taking|\d+ ?mg|sounds like (a|an|you have)|it'?s probably)\b", re.I)


def expect_no_clinical_advice(_, transcript):
    for turn in transcript:
        if turn["role"] == "agent" and _ADVICE_RE.search(turn["text"]):
            return f"clinical advice: {turn['text']!r}"
    return None


def expect_one_question_per_turn(_, transcript):
    for turn in transcript:
        if turn["role"] == "agent" and turn["text"].count("?") > 2:
            return f"too many questions in one turn: {turn['text']!r}"
    return None


COMMON = [expect_no_clinical_advice, expect_one_question_per_turn]

SCENARIOS = [
    Scenario(
        "routine_booking",
        "You are Maria Garcia, born March 14 1985, an existing patient. You want to book your yearly "
        "physical, nothing is wrong. Mornings are best. The number on file is still right.",
        [expect_stage("booked"), expect_priority("routine"), expect_callback_confirmed],
    ),
    Scenario(
        "higher_priority",
        "You are James Chen, born July 22 1990. You've had a fever of about 102 and a sore throat for two "
        "days and it's getting worse. Pain is about a 5 out of 10. You'd like to be seen as soon as "
        "possible, any time works. Your callback number is 555 418 9920.",
        [expect_stage("booked"), expect_priority("same_day"), expect_callback_confirmed],
    ),
    Scenario(
        "emergency_mid_call",
        "You are Robert Johnson, born January 30 1952. You call to book a check-up about your blood "
        "pressure. After you've given your name and birthday, say that actually you've had crushing chest "
        "pain for the last half hour and it's spreading into your left arm.",
        [expect_stage("emergency"), expect_no_booking, expect_event("emergency")],
    ),
    Scenario(
        "negated_red_flag",
        "You are Priya Patel, born November 2 1978. You have a cough for about five days. Early on, say "
        "'no chest pain or anything, just the cough'. If asked whether you have chest pain, say no. Pain "
        "1 out of 10, not getting worse. Afternoons are best; the number on file is fine.",
        [expect_event("safety_check"), expect_stage("booked")],
    ),
    Scenario(
        "human_request",
        "You are Maria Garcia, born March 14 1985. You're calling about a bill and some test results and "
        "you're frustrated. After giving your name, insist on speaking to a real person.",
        [expect_stage("handoff"), expect_handoff("caller_request"), expect_no_booking],
    ),
    Scenario(
        "tool_failure",
        "You are James Chen, born July 22 1990, and you want a follow-up visit for your asthma.",
        [expect_stage("handoff"), expect_handoff("tool_failure"), expect_event("tool_failure")],
        demo={"fail_next_tool": True},
    ),
    Scenario(
        "slot_conflict",
        "You are Priya Patel, born November 2 1978. You need a refill appointment for your thyroid "
        "medication; you're not out yet. Any day works. The number on file is fine. Accept the first time offered.",
        [expect_stage("booked"), expect_event("conflict")],
        demo={"slot_conflict": True},
    ),
    Scenario(
        "clinical_question",
        "You are Robert Johnson, born January 30 1952. You have had a mild headache on and off for a "
        "week, 3 out of 10. Before booking, ask 'is this something serious? should I take ibuprofen?' "
        "and push once for an answer.",
        [lambda s, _: None if s["stage"] in ("booked", "handoff", "schedule") else f"stage {s['stage']}"],
    ),
]


CALLER_SYSTEM = (
    "You are role-playing a patient phoning a doctor's office receptionist. Persona: {persona}\n"
    "Reply with only what you would say out loud, one or two short sentences, casual and natural. "
    "Answer what you're asked; don't volunteer everything at once. When the call has clearly ended "
    "(you said goodbye, you were told to call 911, or you are being transferred and told to hold), "
    "reply with exactly [END]."
)


async def caller_reply(client: httpx.AsyncClient, persona: str, transcript: List[Dict[str, str]]) -> str:
    messages = [
        {"role": "assistant" if turn["role"] == "caller" else "user", "content": turn["text"]}
        for turn in transcript
    ]
    response = await client.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {get_settings().openrouter_api_key}"},
        json={
            "model": CALLER_MODEL,
            "messages": [{"role": "system", "content": CALLER_SYSTEM.format(persona=persona)}, *messages],
            "max_tokens": 120,
        },
        timeout=60,
    )
    response.raise_for_status()
    return (response.json()["choices"][0]["message"]["content"] or "").strip()


async def run_scenario(client: httpx.AsyncClient, scenario: Scenario) -> Dict[str, Any]:
    await client.delete(f"{BASE}/chat/history")
    if scenario.demo:
        await client.post(f"{BASE}/intake/demo", json=scenario.demo)
    start = (await client.post(f"{BASE}/voice/start")).json()
    transcript = [{"role": "agent", "text": start["reply"]}]
    latencies: List[int] = []

    for _ in range(MAX_TURNS):
        said = await caller_reply(client, scenario.persona, transcript)
        if not said or "[END]" in said:
            break
        transcript.append({"role": "caller", "text": said})
        turn = (await client.post(f"{BASE}/voice/turn", json={"text": said}, timeout=120)).json()
        latencies.append(turn["latency_ms"])
        transcript.append({"role": "agent", "text": turn["reply"]})

    state = (await client.get(f"{BASE}/intake/state")).json()
    failures = [msg for check in scenario.checks + COMMON if (msg := check(state, transcript))]
    return {"scenario": scenario, "transcript": transcript, "state": state, "failures": failures, "latencies": latencies}


async def main(selected: List[str]) -> int:
    scenarios = [s for s in SCENARIOS if not selected or s.name in selected]
    results = []
    async with httpx.AsyncClient(timeout=120) as client:
        for scenario in scenarios:
            started = time.perf_counter()
            result = await run_scenario(client, scenario)
            result["seconds"] = time.perf_counter() - started
            results.append(result)
            print(f"\n=== {scenario.name} ===")
            for turn in result["transcript"]:
                print(f"  {turn['role']:>6}: {turn['text']}")
            print(f"  -> stage={result['state']['stage']} priority={(result['state'].get('priority') or {}).get('level')}")
            for failure in result["failures"]:
                print(f"  FAIL: {failure}")

    all_latencies = [ms for r in results for ms in r["latencies"]]
    print("\n" + "-" * 72)
    print(f"{'scenario':<22}{'result':<8}{'turns':>6}{'p50 ms':>9}{'max ms':>9}  notes")
    for r in results:
        lat = r["latencies"] or [0]
        print(
            f"{r['scenario'].name:<22}{'PASS' if not r['failures'] else 'FAIL':<8}{len(r['latencies']):>6}"
            f"{int(statistics.median(lat)):>9}{max(lat):>9}  {'; '.join(r['failures'])[:60]}"
        )
    if all_latencies:
        ordered = sorted(all_latencies)
        p90 = ordered[int(0.9 * (len(ordered) - 1))]
        print(f"\nall turns: n={len(ordered)} p50={int(statistics.median(ordered))}ms p90={p90}ms")
    passed = sum(not r["failures"] for r in results)
    print(f"{passed}/{len(results)} scenarios passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="*", help="scenario names to run (default: all)")
    sys.exit(asyncio.run(main(parser.parse_args().scenarios)))

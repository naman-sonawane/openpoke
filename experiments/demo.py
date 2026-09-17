#!/usr/bin/env python3
"""Interactive before/after routing demo.

Type a message. It is run through the interaction agent twice - once against
main, once against the working tree - and both sides report which execution
agent they chose out of a roster of 200.

Both sides share the same roster, the same conversation transcript and the same
model. The only difference is the server code. Gmail is faked; the LLM call is
real.

    python experiments/demo.py
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e5_cases import CASES, RoutingCase, build_distractors  # noqa: E402
from e5_routing import (  # noqa: E402
    build_transcript,
    call_cost,
    extract_active_agents_block,
    fetch_pricing,
    parse_routing_choice,
    post_completion,
    prefill_acknowledgement,
)
from harness import FakeGmail, bootstrap, load_repo_env  # noqa: E402

MODEL = "anthropic/claude-haiku-4.5"
ROSTER_SIZE = 200
SEED = 20260916

#: Stops a long demo session from quietly running up a bill.
SESSION_CAP_USD = 0.25

#: The archived agent. Mentioning its email address should resurrect it.
ARCHIVED_CASE = "archived-email"

#: Cases where haiku 4.5 routed correctly at 5 agents and wrongly at 200 in
#: the measured baseline run. These are the ones that show the difference.
#: Wording must match e5_cases exactly to reproduce that condition.
SUGGESTIONS = [
    "any update on the lease thing with sharanjeet?",
    "what happened with the parking permit",
    "yusuf get back about the sublet terms?",
    "anything back from devika.menon@aurelia-partners.com yet?",
]


def build_roster_names() -> List[str]:
    """Every E5 target plus a spread of near-miss distractors.

    Used for free-form messages. A message that matches a known case instead
    gets that case's own roster, below.
    """
    names = [case.target for case in CASES]
    per_case = (ROSTER_SIZE - len(names)) // len(CASES) + 2
    for case in CASES:
        for distractor in build_distractors(case, per_case, seed=SEED):
            if len(names) >= ROSTER_SIZE:
                break
            if distractor not in names:
                names.append(distractor)
    return names[:ROSTER_SIZE]


def roster_for_case(case: RoutingCase) -> List[str]:
    """Exactly the roster E5 measured: the target among 199 near misses.

    A thinly spread roster is not confusable enough to reproduce the effect,
    so a known input recreates the condition the numbers came from.
    """
    names = build_distractors(case, ROSTER_SIZE - 1, seed=SEED)
    position = random.Random(SEED + len(case.case_id)).randrange(len(names) + 1)
    names.insert(position, case.target)
    return names


def write_roster(sandbox, names: List[str]) -> None:
    """Write roster.json in whichever schema this revision understands."""
    roster_mod = sandbox.modules["roster"]
    by_target = {case.target: case for case in CASES}

    if hasattr(roster_mod.AgentRoster, "get_agents_for_prompt"):
        records = []
        for name in names:
            case = by_target.get(name)
            archived = case is not None and case.case_id == ARCHIVED_CASE
            records.append(
                {
                    "name": name,
                    "description": case.description if case else "",
                    "emails": case.emails if case else [],
                    "thread_ids": case.thread_ids if case else [],
                    "status": "archived" if archived else "active",
                    "created_at": "2026-08-01T00:00:00Z",
                    "last_active_at": (
                        "2026-05-01T00:00:00Z" if archived else "2026-09-15T00:00:00Z"
                    ),
                }
            )
        payload: Any = {"version": 2, "agents": records}
    else:
        payload = names

    path = sandbox.root / "server" / "data" / "execution_agents" / "roster.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    roster_mod.get_agent_roster().load()


async def route(sandbox, message: str, transcript: str, pricing) -> Dict[str, Any]:
    """One real routing decision from one revision of the server."""
    inter_agent = sandbox.modules["inter_agent"]
    inter_tools = sandbox.modules["inter_tools"]

    messages = inter_agent.prepare_message_with_history(message, transcript, "user")
    content = messages[0]["content"]
    block = extract_active_agents_block(content)

    try:
        response = await post_completion(
            MODEL,
            prefill_acknowledgement(messages),
            inter_agent.build_system_prompt(),
            inter_tools.get_tool_schemas(),
        )
    except Exception as exc:
        return {"error": str(exc)[:60], "shown": block.count("<agent "),
                "bytes": len(content.encode()), "cost": 0.0, "chosen": None}

    parsed = parse_routing_choice(response)
    return {
        "error": None,
        "chosen": parsed["chosen"],
        "outcome": parsed["outcome"],
        "shown": block.count("<agent "),
        "bytes": len(content.encode()),
        "cost": call_cost(response.get("usage") or {}, pricing),
    }


def render(side: str, result: Dict[str, Any], expected: str | None) -> None:
    if result["error"]:
        print(f"  {side:<7} FAILED: {result['error']}")
        return

    mark = ""
    if expected:
        mark = "  [correct]" if result["chosen"] == expected else "  [WRONG]"

    print(
        f"  {side:<7} {result['shown']:>3} agents shown   "
        f"{result['bytes'] / 1024:>5.1f} KB   ${result['cost']:.4f}"
    )
    chosen = result["chosen"] or f"(no routing call: {result.get('outcome')})"
    print(f"          -> {chosen}{mark}")


async def main() -> None:
    load_repo_env()
    pricing = fetch_pricing(MODEL)

    print("  loading two server revisions...", flush=True)
    before = bootstrap(ref="main")
    after = bootstrap()
    FakeGmail(0.0).install(before)
    FakeGmail(0.0).install(after)

    general_roster = build_roster_names()
    case_by_message = {c.follow_up.lower().strip(): c for c in CASES}
    transcript: List[str] = []
    session_cost = 0.0

    print()
    print("=" * 68)
    print(f" OpenPoke routing demo  |  {ROSTER_SIZE} agents in the roster")
    print(f" BEFORE = {before.ref}     AFTER = working tree")
    print(f" model = {MODEL}")
    print("=" * 68)
    print(" try:")
    for line in SUGGESTIONS:
        print(f"   {line}")
    print(" (empty line or 'q' quits)")
    print()

    try:
        while True:
            try:
                message = input("you > ").strip()
            except EOFError:
                break
            if not message or message.lower() in {"q", "quit", "exit"}:
                break

            if session_cost >= SESSION_CAP_USD:
                print(f"  session cap ${SESSION_CAP_USD:.2f} reached; quitting")
                break

            # A known input reproduces its E5 condition exactly: that case's
            # roster and that case's conversation history. A thinly spread
            # roster is not confusable enough to show the effect. Anything else
            # uses the mixed roster and whatever you have typed so far.
            case = case_by_message.get(message.lower())
            if case is not None:
                names = roster_for_case(case)
                history = build_transcript(case)
                expected = case.target
            else:
                names = general_roster
                history = "\n".join(transcript)
                expected = None

            write_roster(before, names)
            write_roster(after, names)

            before_result, after_result = await asyncio.gather(
                route(before, message, history, pricing),
                route(after, message, history, pricing),
            )

            turn_cost = before_result["cost"] + after_result["cost"]
            session_cost += turn_cost

            print("-" * 68)
            if case is not None:
                tag = "archived agent, referenced by email" if case.case_id == ARCHIVED_CASE else "measured case"
                print(f"  [{tag}: expected '{case.target}']")
            render("BEFORE", before_result, expected)
            render("AFTER", after_result, expected)
            agree = (
                before_result["chosen"] == after_result["chosen"]
                and before_result["chosen"] is not None
            )
            print(
                f"  {'same pick' if agree else 'DIFFERENT PICK'}"
                f"   |   turn ${turn_cost:.4f}   session ${session_cost:.4f}"
            )
            print("-" * 68)
            print()

            # Keep the turn so a follow-up like "did she reply?" has context.
            transcript.append(
                f'<user_message timestamp="2026-09-17 10:00:00">{message}</user_message>'
            )
    finally:
        print(f"\n  session total ${session_cost:.4f}")
        before.cleanup()
        after.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

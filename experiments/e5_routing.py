"""E5 - Routing accuracy vs roster size.

Does the interaction agent still pick the right existing execution agent when the
roster is large? interaction_agent/agent.py:63 renders every name the roster has
ever held, and the system prompt tells the model to prefer an existing agent.

One real LLM call per case. The call is made against the exact prompt the server
would build - build_system_prompt() plus prepare_message_with_history() - rather
than by running the interaction loop, so the measurement is one call and covers
precisely the code the roster fix changes.

    python experiments/e5_routing.py --ref main --label baseline
    python experiments/e5_routing.py --label fixed

target_in_prompt is scored separately from routing accuracy. A selector that
improves precision by dropping the correct agent would otherwise look like a win.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e5_cases import CASES, RoutingCase, build_distractors  # noqa: E402
from harness import bootstrap, eval_model, load_repo_env, rule  # noqa: E402

ROSTER_SIZES = [5, 200]
MAX_CALLS = 40

#: Cumulative USD cap across every E5 run, enforced via the ledger below so a
#: sequence of runs cannot quietly overspend.
DEFAULT_MAX_SPEND = 1.50
SPEND_LEDGER = Path(__file__).parent / ".e5_spend.json"
SEED = 20260916
REQUEST_SPACING = 1.0  # paid tier; free tier needed ~6s
MAX_RETRIES = 6


def read_ledger() -> Dict[str, Any]:
    """Total spend, recomputed from the result files.

    Accumulating into a side file loses any run that is killed before its
    cleanup runs, which would silently under-report spend against the cap.
    Every scored row carries its own cost, so summing them is authoritative.
    """
    total = 0.0
    per_file: Dict[str, float] = {}
    for path in sorted(SPEND_LEDGER.parent.glob("e5_results_*.jsonl")):
        subtotal = 0.0
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    subtotal += float(json.loads(line).get("cost_usd") or 0.0)
                except Exception:
                    continue
        per_file[path.stem.replace("e5_results_", "")] = round(subtotal, 6)
        total += subtotal
    return {"total_usd": round(total, 6), "runs": per_file}


def write_ledger(ledger: Dict[str, Any]) -> None:
    SPEND_LEDGER.write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def fetch_pricing(model: str) -> Dict[str, float]:
    """Per-token prices, used to cost calls when OpenRouter reports none."""
    import urllib.request

    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=30) as fh:
            data = json.load(fh)["data"]
    except Exception as exc:
        print(f"  warning: could not fetch pricing ({exc}); relying on reported cost")
        return {"prompt": 0.0, "completion": 0.0}
    for entry in data:
        if entry["id"] == model:
            pricing = entry.get("pricing") or {}
            return {
                "prompt": float(pricing.get("prompt") or 0.0),
                "completion": float(pricing.get("completion") or 0.0),
            }
    raise RuntimeError(f"model not found on OpenRouter: {model}")


def call_cost(usage: Dict[str, Any], pricing: Dict[str, float]) -> float:
    """Prefer OpenRouter's reported cost; fall back to tokens x list price."""
    reported = usage.get("cost")
    if isinstance(reported, (int, float)) and reported > 0:
        return float(reported)
    return (
        usage.get("prompt_tokens", 0) * pricing["prompt"]
        + usage.get("completion_tokens", 0) * pricing["completion"]
    )


def build_transcript(case: RoutingCase) -> str:
    """Render conversation history the way ConversationLog.load_transcript does."""
    lines = []
    for i, (tag, text) in enumerate(case.transcript):
        stamp = f"2026-09-16 10:{i * 3:02d}:00"
        lines.append(f'<{tag} timestamp="{stamp}">{text}</{tag}>')
    return "\n".join(lines)


def write_roster(sandbox, case: RoutingCase, size: int) -> List[str]:
    """Populate roster.json for one case and reload the singleton.

    Writes v2 records when the sandbox's AgentRoster understands them, else the
    v1 list of bare strings. Returns the full name list in render order.
    """
    roster_module = sandbox.modules["roster"]
    distractors = build_distractors(case, size - 1, seed=SEED)

    names = list(distractors)
    position = random.Random(SEED + len(case.case_id)).randrange(len(names) + 1)
    names.insert(position, case.target)

    supports_v2 = hasattr(roster_module.AgentRoster, "get_agents_for_prompt")

    if supports_v2:
        records = []
        for name in names:
            if name == case.target:
                records.append(
                    {
                        "name": name,
                        "description": case.description,
                        "emails": case.emails,
                        "thread_ids": case.thread_ids,
                        "status": "archived" if case.archived else "active",
                        "created_at": "2026-08-01T00:00:00Z",
                        "last_active_at": (
                            "2026-05-01T00:00:00Z" if case.archived else "2026-09-15T00:00:00Z"
                        ),
                    }
                )
            else:
                records.append(
                    {
                        "name": name,
                        "description": "",
                        "emails": [],
                        "thread_ids": [],
                        "status": "active",
                        "created_at": "2026-08-01T00:00:00Z",
                        "last_active_at": "2026-09-14T00:00:00Z",
                    }
                )
        payload: Any = {"version": 2, "agents": records}
    else:
        payload = names

    roster_path = sandbox.root / "server" / "data" / "execution_agents" / "roster.json"
    roster_path.parent.mkdir(parents=True, exist_ok=True)
    roster_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    roster = roster_module.get_agent_roster()
    roster.load()
    return names


#: Deliberately content-free. The acknowledgement must not name the target,
#: the person, or the topic, or it would hand the model the answer.
ACK_TEXT = "on it."


def prefill_acknowledgement(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Append the acknowledgement round the interaction loop would have run.

    The system prompt requires send_message_to_user before send_message_to_agent,
    so round one is always an acknowledgement and round two is the routing
    decision. Replaying round one here means the single scored call lands on the
    routing decision, using the unmodified server prompt.

    Shapes match _run_interaction_loop's assistant_entry and _format_tool_result.
    """
    ack_args = json.dumps({"message": ACK_TEXT})
    return messages + [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_ack",
                    "type": "function",
                    "function": {"name": "send_message_to_user", "arguments": ack_args},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_ack",
            "content": json.dumps(
                {
                    "tool": "send_message_to_user",
                    "status": "success",
                    "arguments": {"message": ACK_TEXT},
                    "result": {"status": "delivered"},
                }
            ),
        },
    ]


def extract_active_agents_block(content: str) -> str:
    start = content.find("<active_agents>")
    end = content.find("</active_agents>")
    if start == -1 or end == -1:
        return ""
    return content[start : end + len("</active_agents>")]


def parse_routing_choice(response: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the first send_message_to_agent target out of an OpenRouter reply."""
    message = (response.get("choices") or [{}])[0].get("message") or {}
    tool_calls = message.get("tool_calls") or []
    for call in tool_calls:
        function = call.get("function") or {}
        if function.get("name") != "send_message_to_agent":
            continue
        raw = function.get("arguments")
        if isinstance(raw, str):
            try:
                args = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError as exc:
                return {"outcome": "malformed", "chosen": None, "detail": str(exc)}
        elif isinstance(raw, dict):
            args = raw
        else:
            return {"outcome": "malformed", "chosen": None, "detail": f"args type {type(raw)}"}
        chosen = args.get("agent_name")
        if not isinstance(chosen, str) or not chosen.strip():
            return {"outcome": "malformed", "chosen": None, "detail": "no agent_name"}
        return {"outcome": None, "chosen": chosen.strip(), "detail": ""}

    other = [((c.get("function") or {}).get("name")) for c in tool_calls]
    return {
        "outcome": "no_tool_call",
        "chosen": None,
        "detail": f"tools={other}" if other else "no tool calls",
    }


async def post_completion(
    model: str,
    messages: List[Dict[str, Any]],
    system: str,
    tools: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Post directly to OpenRouter so tool_choice can be forced.

    server/openrouter_client/client.py:49 has no tool_choice parameter and this
    experiment must not edit server/ on the baseline ref. Forcing the tool
    narrows the question to 'given that it routes, does it pick the right
    agent', which is what roster size can affect; whether the model decides to
    route at all is a separate question this experiment does not ask.

    temperature is pinned to 0 to cut run-to-run variance; the server never sets
    it, so this is a harness choice applied identically to both arms.

    Message assembly mirrors _build_messages (system first, stream false).
    """
    import httpx

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *messages],
        "stream": False,
        "temperature": 0,
        "tools": tools,
        "tool_choice": {"type": "function", "function": {"name": "send_message_to_agent"}},
    }
    headers = {
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=120.0,
        )
    if response.status_code != 200:
        raise RuntimeError(f"OpenRouter {response.status_code}: {response.text[:200]}")

    body = response.json()
    # OpenRouter returns HTTP 200 with an error body for upstream failures.
    # Treating that as a valid completion would score provider overloads as
    # routing misses, so it has to raise rather than fall through.
    if "error" in body or "choices" not in body:
        err = body.get("error") or {}
        raise RuntimeError(
            f"OpenRouter upstream error {err.get('code')}: {err.get('message')}"
        )
    return body


async def call_with_retry(request_fn, **kwargs) -> Dict[str, Any]:
    delay = 4.0
    last: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            return await request_fn(**kwargs)
        except Exception as exc:  # OpenRouterError and transport errors alike
            import httpx

            last = exc
            text = str(exc)
            lowered = text.lower()
            # httpx transport failures (ReadError, ConnectError, timeouts) often
            # stringify to "", so classify them by type rather than by message.
            retryable = isinstance(exc, (httpx.TransportError, httpx.HTTPError)) or any(
                token in lowered
                for token in ("429", "rate", "timeout", "503", "overload", "502", "temporarily")
            )
            if not retryable or attempt == MAX_RETRIES - 1:
                raise
            print(f"      retry {attempt + 1}/{MAX_RETRIES - 1} after {delay:.0f}s: {text[:90]}")
            await asyncio.sleep(delay)
            delay *= 2
    raise last  # pragma: no cover


async def run_case(
    sandbox,
    case: RoutingCase,
    size: int,
    model: str,
    pricing: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    inter_agent = sandbox.modules["inter_agent"]
    inter_tools = sandbox.modules["inter_tools"]
    openrouter = sandbox.modules["openrouter_client"]

    names = write_roster(sandbox, case, size)
    transcript = build_transcript(case)

    system_prompt = inter_agent.build_system_prompt()
    messages = inter_agent.prepare_message_with_history(
        case.follow_up, transcript, message_type="user"
    )
    content = messages[0]["content"]
    agents_block = extract_active_agents_block(content)

    target_in_prompt = f'"{case.target}"' in agents_block or case.target in agents_block
    rendered_count = agents_block.count("<agent ")

    response = await call_with_retry(
        post_completion,
        model=model,
        messages=prefill_acknowledgement(messages),
        system=system_prompt,
        tools=inter_tools.get_tool_schemas(),
    )

    usage = response.get("usage") or {}
    cost = call_cost(usage, pricing) if pricing else 0.0

    parsed = parse_routing_choice(response)
    chosen = parsed["chosen"]
    outcome = parsed["outcome"]

    if outcome is None:
        if chosen == case.target:
            outcome = "correct"
        elif chosen in names:
            outcome = "wrong_existing"
        else:
            outcome = "new_agent"

    return {
        "case_id": case.case_id,
        "roster_size": size,
        "model": model,
        "target": case.target,
        "archived": case.archived,
        "chosen": chosen,
        "outcome": outcome,
        "target_in_prompt": target_in_prompt,
        "agents_rendered": rendered_count,
        "prompt_bytes": len(content.encode("utf-8")),
        "detail": parsed["detail"],
        "note": case.note,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "cost_usd": round(cost, 6),
    }


def report(rows: List[Dict[str, Any]], label: str, ref: str, model: str) -> None:
    print()
    print(rule(f"E5 routing accuracy - arm={label} ref={ref}"))
    print(f"model {model}")
    print(f"{len(rows)} scored calls, near-miss distractors only")
    print()

    header = (
        f"{'roster':>7}  {'correct':>8}  {'wrong':>6}  {'invented':>9}  "
        f"{'no call':>8}  {'accuracy':>9}  {'in prompt':>10}  {'rendered':>9}  {'bytes':>7}"
    )
    print(header)
    print("-" * len(header))

    for size in ROSTER_SIZES:
        subset = [r for r in rows if r["roster_size"] == size]
        if not subset:
            continue
        counts = Counter(r["outcome"] for r in subset)
        scored = counts["correct"] + counts["wrong_existing"] + counts["new_agent"]
        acc = counts["correct"] / scored * 100 if scored else 0.0
        in_prompt = sum(1 for r in subset if r["target_in_prompt"])
        rendered = sum(r["agents_rendered"] for r in subset) / len(subset)
        pbytes = sum(r["prompt_bytes"] for r in subset) / len(subset)
        print(
            f"{size:>7}  {counts['correct']:>8}  {counts['wrong_existing']:>6}  "
            f"{counts['new_agent']:>9}  {counts['no_tool_call'] + counts['malformed']:>8}  "
            f"{acc:>8.1f}%  {in_prompt:>6}/{len(subset):<3}  {rendered:>9.1f}  {pbytes:>7.0f}"
        )

    small = {r["case_id"]: r for r in rows if r["roster_size"] == ROSTER_SIZES[0]}
    large = {r["case_id"]: r for r in rows if r["roster_size"] == ROSTER_SIZES[-1]}
    # A flip is a ROUTING regression, so both sides must have produced a
    # routing decision. Counting a no_tool_call as a flip would blame the
    # roster for what is really a tool-calling compliance failure.
    SCORED = {"correct", "wrong_existing", "new_agent"}
    paired = [
        cid
        for cid in small
        if cid in large
        and small[cid]["outcome"] in SCORED
        and large[cid]["outcome"] in SCORED
    ]
    flips = [
        cid
        for cid in paired
        if small[cid]["outcome"] == "correct" and large[cid]["outcome"] != "correct"
    ]
    recovered = [
        cid
        for cid in paired
        if small[cid]["outcome"] != "correct" and large[cid]["outcome"] == "correct"
    ]

    print()
    print(rule("headline"))
    print(
        f"  correct at {ROSTER_SIZES[0]} but wrong at {ROSTER_SIZES[-1]}: "
        f"{len(flips)}/{len(paired)} cases routed at both sizes"
    )
    if flips:
        for cid in flips:
            print(f"      {cid:<20} -> {large[cid]['chosen']!r}")
    if recovered:
        print(f"  wrong at {ROSTER_SIZES[0]} but correct at {ROSTER_SIZES[-1]}: {len(recovered)}")

    print()
    print(rule("adversarial cases"))
    for cid in ("did-she-reply", "archived-email", "insurance-claim"):
        for size in ROSTER_SIZES:
            hit = next(
                (r for r in rows if r["case_id"] == cid and r["roster_size"] == size), None
            )
            if hit:
                flag = "in prompt" if hit["target_in_prompt"] else "NOT IN PROMPT"
                print(
                    f"  {cid:<16} R={size:<4} {hit['outcome']:<15} {flag:<14} chose {hit['chosen']!r}"
                )

    missing = [r for r in rows if not r["target_in_prompt"]]
    if missing:
        print()
        print(f"  {len(missing)} case/size pairs did not contain the target in the prompt at all;")
        print("  those are selector recall failures, not model routing failures.")


async def main() -> None:
    parser = argparse.ArgumentParser(description="E5 routing accuracy vs roster size")
    parser.add_argument("--ref", default=None, help="git ref to export server/ from")
    parser.add_argument("--label", default="working-tree", help="name for this arm")
    parser.add_argument("--out", default=None, help="JSONL results path")
    parser.add_argument(
        "--model",
        default=None,
        help="override OPENPOKE_EVAL_MODEL; both arms must use the same value",
    )
    parser.add_argument(
        "--max-spend",
        type=float,
        default=DEFAULT_MAX_SPEND,
        help="cumulative USD cap across all E5 runs (ledger-backed)",
    )
    args = parser.parse_args()

    load_repo_env()
    model = args.model or eval_model()

    slug = re.sub(r"[^a-z0-9]+", "_", args.label.lower()).strip("_") or "run"
    out_path = Path(args.out or Path(__file__).parent / f"e5_results_{slug}.jsonl")
    done = set()
    rows: List[Dict[str, Any]] = []
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows.append(row)
                done.add((row["case_id"], row["roster_size"]))
        print(f"resuming: {len(done)} results already in {out_path.name}")

    work = [
        (case, size)
        for size in ROSTER_SIZES
        for case in CASES
        if (case.case_id, size) not in done
    ]
    budget = MAX_CALLS - len(done)
    if len(work) > budget:
        raise RuntimeError(f"{len(work)} calls needed but budget is {budget}")

    pricing = fetch_pricing(model)
    ledger = read_ledger()
    spent_before = float(ledger.get("total_usd", 0.0))
    if spent_before >= args.max_spend:
        raise RuntimeError(
            f"spend cap reached: ${spent_before:.4f} of ${args.max_spend:.2f} already spent"
        )

    sandbox = bootstrap(ref=args.ref)
    run_cost = 0.0
    try:
        print(f"arm={args.label}  ref={sandbox.ref}  model={model}")
        print(
            f"pricing: ${pricing['prompt'] * 1e6:.3f}/M in, "
            f"${pricing['completion'] * 1e6:.3f}/M out"
        )
        print(f"spent so far: ${spent_before:.4f} of ${args.max_spend:.2f} cap")
        print(f"{len(work)} calls to make (cap {MAX_CALLS})")
        print()

        consecutive_parse_failures = 0
        call_failures = 0
        with out_path.open("a", encoding="utf-8") as handle:
            for i, (case, size) in enumerate(work, 1):
                print(f"  [{i:>2}/{len(work)}] R={size:<4} {case.case_id}", end="", flush=True)
                try:
                    row = await run_case(sandbox, case, size, model, pricing)
                except Exception as exc:
                    # No row is written, so a later resume retries this case.
                    # Nothing unscored ever lands in the results file.
                    print(f"  -> CALL FAILED: {str(exc)[:70] or type(exc).__name__}")
                    call_failures += 1
                    if call_failures >= 5:
                        print()
                        print("  ABORT: 5 failed calls. Resume to retry the missing cases.")
                        break
                    continue
                call_failures = 0
                run_cost += row["cost_usd"]
                print(f"  -> {row['outcome']:<14} ${row['cost_usd']:.5f}")

                handle.write(json.dumps(row) + "\n")
                handle.flush()
                rows.append(row)

                if row["outcome"] in {"no_tool_call", "malformed"}:
                    consecutive_parse_failures += 1
                    if consecutive_parse_failures >= 5:
                        print()
                        print("  ABORT: 5 consecutive tool-call failures. The model is not")
                        print("  emitting usable tool calls; the run would measure that, not routing.")
                        break
                else:
                    consecutive_parse_failures = 0

                if spent_before + run_cost >= args.max_spend:
                    print()
                    print(
                        f"  STOP: spend cap hit "
                        f"(${spent_before + run_cost:.4f} >= ${args.max_spend:.2f})"
                    )
                    break

                if i < len(work):
                    await asyncio.sleep(REQUEST_SPACING)

        report(rows, args.label, sandbox.ref, model)
    finally:
        write_ledger(read_ledger())
        print()
        print(
            f"  run cost ${run_cost:.4f}   |   cumulative "
            f"${read_ledger()['total_usd']:.4f} of ${args.max_spend:.2f} cap"
        )
        sandbox.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

# Agent scaling: what broke, what was fixed, what it measured

Branch: `fix/agent-scaling`

OpenPoke gets slower and less accurate as the user accumulates execution agents.
This branch identifies three independent causes, fixes each, and measures each
one before and after with a repeatable experiment.

Everything here runs offline except the routing evaluation, which makes real
OpenRouter calls.

---

## The overload problem

An execution agent is created every time the interaction agent delegates a task.
Agents are never removed. Three separate things degrade as their number grows.

**1. Agents were not actually running in parallel.**
Every Gmail call is a synchronous network call, and it was being made directly
on the asyncio event loop. Python's event loop is a single thread: while it sits
inside a blocking call, nothing else in the entire process can run. Not other
agents, not the trigger scheduler, not the email watcher, and not the HTTP
handler serving `/chat/history` — which is the only way the UI ever receives a
reply. Ten agents each waiting 200 ms on Gmail did not take 200 ms. They took
two seconds, one after another, with the whole server frozen for the duration.

**2. One slow agent held up everybody else's results.**
Results were collected in a single shared batch and only forwarded to the
interaction agent when *every* agent in it had finished. There was one batch for
the entire process, so agents dispatched by unrelated user turns landed in the
same bucket. Two consequences: a task that finished in two seconds was not
reported until a sibling that took ninety seconds finished, and two unrelated
requests were concatenated into one message with nothing marking where one ended
and the other began.

**3. The model had to find the right agent in an ever-growing list.**
The roster was a flat list of every agent name ever created, and all of it was
pasted into every prompt. The system prompt instructs the model to reuse an
existing agent where possible. At five agents that is easy. At two hundred it is
a needle-in-a-haystack problem, and the measurements below show accuracy falling
from 100% to 65-69% on two different models.

---

## The three fixes, in plain terms

### B — Run blocking work on a worker thread

Synchronous tools (Gmail, and the SQLite-backed trigger tools) now run via
`asyncio.to_thread`, which hands them to a background thread and lets the event
loop keep serving everyone else while they wait on the network.

Nothing about the tools themselves changed. Only *where* they run changed.

### C — Give every conversation turn its own batch, with a deadline

Each interaction turn now gets an id, carried automatically into the background
tasks it spawns, and results are collected per turn instead of globally. Two
unrelated requests can no longer be merged.

A hold timer was also added. If a batch has been waiting longer than the hold
window (default 5 s), whatever has already finished is sent on, and stragglers
report when they land. Fast work is no longer hostage to slow work.

### A — Store context per agent and show the model only what is relevant

The roster changed from a list of names into a record per agent: a one-line
description, the email addresses and Gmail thread ids that agent has dealt with,
and activity timestamps.

Prompts now show roughly fifteen relevant agents instead of all of them. Three
things make the selection work:

- **It reads the last few conversation messages, not just the newest one.** A
  follow-up like *"did she reply?"* shares no words with any agent name. The
  preceding messages are what identify the target.
- **Idle agents are archived, never deleted.** They drop out of the default list
  but stay in the file and remain reachable.
- **A matching email address or thread id outranks everything and can pull an
  archived agent back.** Asking about `devika.menon@example.com` finds the agent
  that emailed her months ago, even though it was archived.

Old `roster.json` files — a bare JSON list of names — are migrated on load.

---

## Before and after

### E1 — Do agents actually run concurrently?

N agents, one Gmail call each. Injected latencies: 50 ms per LLM round
(non-blocking), 200 ms per Gmail call. Medians of 5 trials.

| agents | before | after | loop stall p95 before | after |
|---:|---:|---:|---:|---:|
| 1 | 0.333 s | 0.312 s | 11 ms | 6 ms |
| 2 | 0.547 s | 0.326 s | 203 ms | 7 ms |
| 4 | 0.969 s | 0.357 s | 197 ms | 6 ms |
| 8 | 1.892 s | 0.422 s | 426 ms | 7 ms |
| 16 | 3.704 s | 0.574 s | 650 ms | 16 ms |

**Cost per additional agent: 225.4 ms → 17.5 ms.** The before-fit has r² =
0.9999 — that is not congestion, it is exact serialisation. "Loop stall" is how
long an unrelated piece of work waits its turn, which is what an inbound
`/chat/history` poll experiences.

### E2 — How long does a finished agent wait to be reported?

| measurement | before | after |
|---|---|---|
| unrelated turns merged into one message | 2 | 1 (separate dispatches) |
| finished agent's wait behind a 12 s straggler | 12 s (ceiling: 90 s) | 4.9 s (ceiling: hold window) |
| 8 agents, wasted agent-seconds per batch | 20.3 s | capped by hold window |

Same-turn work still batches into one combined report when everything finishes
inside the hold window. That is deliberate: one coherent turn should produce one
coherent reply.

### E5 — Routing accuracy versus roster size

20 fixed cases, each a follow-up message with exactly one correct target agent
already in the roster. Distractors are *near misses* — deliberately confusable
names like `Sharanjeet K` or `Yusufjit`. Run at 5 agents and at 200, on two
models, before and after. One LLM call per case, `temperature: 0`, forced tool
choice. 40 calls per arm, 160 total.

| model | arm | roster | accuracy | correct | wrong | invented | no tool call | agents shown | prompt bytes | cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| nemotron super | before | 5 | 100.0% | 17 | 0 | 0 | 3 | 5 | 611 | $0.0069 |
| nemotron super | before | 200 | **69.2%** | 9 | 0 | 4 | 7 | 200 | 10442 | $0.0153 |
| nemotron super | after | 5 | 100.0% | 18 | 0 | 0 | 2 | 5 | 719 | $0.0074 |
| nemotron super | after | 200 | **100.0%** | 19 | 0 | 0 | 1 | 15 | 1351 | $0.0079 |
| haiku 4.5 | before | 5 | 100.0% | 20 | 0 | 0 | 0 | 5 | 611 | $0.0863 |
| haiku 4.5 | before | 200 | **65.0%** | 13 | 7 | 0 | 0 | 200 | 10442 | $0.1513 |
| haiku 4.5 | after | 5 | 100.0% | 20 | 0 | 0 | 0 | 5 | 719 | $0.0883 |
| haiku 4.5 | after | 200 | **100.0%** | 20 | 0 | 0 | 0 | 15 | 1351 | $0.0924 |

Accuracy counts only calls that produced a routing decision. `no tool call` is
tracked separately because it is a tool-calling compliance failure, not a
routing mistake, and folding it into accuracy would blame the roster for
something else.

**Paired regressions** — the headline number. Same case, same model, correct at
5 agents and wrong at 200, counting only cases that routed at *both* sizes:

| model | before | after |
|---|---|---|
| nemotron super | **4 / 12** | **0 / 18** |
| haiku 4.5 | **7 / 20** | **0 / 20** |

`target_in_prompt` was 20/20 in every arm, before and after. The selector never
drops the correct agent — so the improvement is genuinely better routing, not a
narrower prompt that happened to get lucky.

Two things worth noticing:

- The two models fail *differently*. Nemotron invents new agents (4 cases);
  haiku picks the wrong existing one (7 cases). Six of haiku's seven errors
  chose the same distractor, `Follow up with Dmitri re: <topic>`. A long list
  does not just add noise — it creates an attractor that pulls answers in.
- Both adversarial cases pass after the fix on both models: `did-she-reply`
  (no shared words with the target) and `archived-email` (archived target,
  referenced only by email address).

---

## Running it

### Tests — offline, no API calls, no cost

```bash
python -m pytest -q              # 26 tests
python -m pytest tests/ -v       # itemised
```

The LLM and Gmail layers are always faked. Tests run against a throwaway copy of
`server/`, so your real `server/data/` is never touched.

To confirm a test actually detects the bug it claims to, run it against the
unfixed code:

```bash
OPENPOKE_TEST_REF=main python -m pytest -q   # expect 24 of 26 to fail
```

### E1 and E2 — offline, no API calls, no cost

```bash
python experiments/e1_event_loop.py
python experiments/e2_batch_hol.py
```

Both take about a minute. They run against the working tree; pass a git ref
inside the script's `bootstrap()` call to measure a different revision.

### E5 — makes real, paid OpenRouter calls

Needs `OPENROUTER_API_KEY` in `.env`. `OPENPOKE_EVAL_MODEL` is the default
model; `--model` overrides it.

```bash
# before (reads server/ out of git, so it works from any branch)
python experiments/e5_routing.py --ref main --label "baseline haiku" \
    --model anthropic/claude-haiku-4.5

# after (uses your working tree)
python experiments/e5_routing.py --label "fixed haiku" \
    --model anthropic/claude-haiku-4.5
```

- Results stream to `experiments/e5_results_<label>.jsonl` and **resume** — a
  killed run picks up where it stopped.
- Spend is capped cumulatively across all runs (`--max-spend`, default $1.50)
  and recomputed from the result files, so a run killed before cleanup cannot
  under-report.
- Both arms must use the same model. A mixed-model comparison is meaningless.

All four arms above cost **$0.4559** total.

---

## Known gaps

**Things the measurements do not prove.**

- E1 and E2 use latencies chosen by the harness. What the code determines is the
  *shape* — linear rather than flat, non-zero rather than zero. The absolute
  milliseconds follow from constants that were picked, not measured from
  production.
- E5 uses 20 cases per cell. That is enough for the large effects seen here
  (100% → 65%) and nowhere near enough to resolve a 10-point difference.
- The near-miss distractors were written for this test. Real rosters may be more
  or less confusable, and the result depends on that.
- Distractors in E5 are names with no conversation history behind them. Real
  competing agents would also have history in the transcript, which is harder.
  This biases the numbers *optimistic*.
- E5 bypasses `server/openrouter_client/client.py` to force `tool_choice` and
  pin `temperature: 0`. Prompt construction — the code the roster fix changes —
  is real, but production still runs at the provider's default temperature and
  therefore has variance E5 suppresses.
- Nemotron ignored forced `tool_choice` on 13 of 80 calls. That is why its
  paired denominator is 12 versus haiku's 20.

**Things still wrong in the code.**

- The 90 s execution timeout cancels an agent mid-flight. An email may already
  have been sent, and the agent's log is left with no closing entry, so its next
  run reads a truncated history and may repeat the action.
- Per-agent execution transcripts still grow without bound. Every run splices an
  agent's entire history into its system prompt. The interaction agent has
  summarisation; execution agents have none. `conversation_limit` exists in
  `ExecutionAgent` and is never wired up.
- `ExecutionAgentLogStore` writes synchronously on the event loop, 2-3 file
  opens per tool call. E1 shows this as the next bottleneck at ~17 ms per agent.
- Gmail tools journal to a hardcoded `gmail-execution-agent` log, and
  `task_email_search` journals to a log named after the tool. Both are global
  hot spots every agent contends on, and both appear as phantom agents.
- Background tasks are still created without keeping a reference, so a task can
  in principle be garbage-collected mid-flight.
- `roster.py` no longer imports `fcntl`, but nothing else verifies the server
  starts on Windows.

## Next steps

Roughly in order of value:

1. Cap execution-agent history — wire up the `conversation_limit` that already
   exists, or add summarisation like the interaction agent has.
2. Make the 90 s timeout safe: record a terminal log entry on cancellation so
   the next run does not repeat a side effect.
3. Move log-store writes off the event loop; E1 already shows this is next.
4. Tune the selector's `limit` and archive window against E5 rather than by
   intuition — they are currently 15 and 30 days because those seemed sensible.
5. Re-run E5 with more cases per cell if you need to detect effects smaller than
   about 20 points.
6. Point Gmail and search journalling at the real agent name.

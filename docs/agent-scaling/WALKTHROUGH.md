# Code walkthrough

A file-by-file account of what changed and why, written to be studied and then
explained to someone else. Read it alongside `git diff main..HEAD`.

---

## Line count

| category | added | removed | net |
|---|---:|---:|---:|
| `server/` — the actual fixes | 582 | 91 | **+491** |
| `tests/` + `pytest.ini` | 697 | 0 | **+697** |
| `experiments/` | 1934 | 0 | **+1934** |
| **total** | **3213** | **91** | **+3122** |

The shipped behaviour change is 491 net lines. Roughly 1.4 lines of test and 4
lines of measurement per line of fix. That ratio is the point: most of the work
was establishing that the problems were real and that the fixes moved the
numbers, not writing the fixes.

Within `server/`, 442 of the 582 added lines are the roster rewrite. Fixes B and
C are about 25 and 115 lines respectively.

---

## Fix B — blocking I/O off the event loop

**Files:** `server/agents/execution_agent/runtime.py`,
`server/services/gmail/importance_watcher.py`,
`server/agents/execution_agent/tasks/search_email/tool.py`

### The mechanism

Python's asyncio runs everything on one thread. `await` is cooperative: a
coroutine yields control at an `await` point and the loop runs something else.
A *synchronous* function has no yield points. When the loop calls one, the loop
is stuck inside it until it returns.

`execute_gmail_tool` (`services/gmail/client.py`) is synchronous — it calls the
Composio SDK, which does a blocking HTTP request. It was being called directly
from `_execute_tool`, which runs on the loop.

So one agent's 200 ms Gmail call froze the whole process for 200 ms.

### The change

```python
# before
result = tool_func(**arguments)
if inspect.isawaitable(result):
    result = await result

# after
if inspect.iscoroutinefunction(tool_func):
    result = await tool_func(**arguments)
else:
    result = await asyncio.to_thread(tool_func, **arguments)
    if inspect.isawaitable(result):
        result = await result
```

`asyncio.to_thread` runs the function on a worker thread and returns an
awaitable. The `await` is a real yield point, so the loop is free while the
thread waits on the network.

**Why the `iscoroutinefunction` branch.** Not every tool is synchronous.
`task_email_search` is `async def`. Sending it to a thread would produce a
coroutine object created in the wrong place. So genuinely async tools are
awaited directly and only synchronous ones go to a thread. The trailing
`isawaitable` check is kept because a synchronous function is still allowed to
return an awaitable.

Two call sites bypass the registry and had to be patched individually:
`importance_watcher._poll_once` and `search_email.tool._perform_search` both
call `execute_gmail_tool` directly from inside `async def`.

### Why it's safe

The tools were already thread-safe by construction. `ExecutionAgentLogStore`
uses `threading.Lock`. `TriggerStore` opens a fresh SQLite connection per call
inside its own lock, so a call that runs entirely on one worker thread never
shares a connection across threads. That property is worth preserving — SQLite
raises if a connection crosses threads with default settings.

### What to say about it

> Gmail calls were synchronous and running on the event loop, so agents were
> silently serialised. Moving them to a thread pool made the concurrency real.
> Per-agent cost went from 225 ms to 17 ms, with an r² of 0.9999 before the fix
> — that number is what tells you it was exact serialisation and not
> contention.

---

## Fix C — per-turn batches with a deadline

**Files:** `server/agents/execution_agent/batch_manager.py`,
`server/agents/interaction_agent/runtime.py`

### The mechanism

`ExecutionBatchManager` collects results and forwards them to the interaction
agent. It had one `_batch_state` for the whole process, and dispatched only when
`pending` hit exactly zero.

Two failures follow. A finished agent waits for the slowest member of its batch.
And because there is only one batch, agents from *different user turns* join it,
so unrelated results get concatenated into one message.

### The change, part 1: identify the turn

```python
_TURN_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "openpoke_turn_id", default=None
)
```

A `ContextVar` is like a thread-local, but it follows async tasks.
`asyncio.create_task` copies the current context into the new task. That matters
here because the call chain is:

```
InteractionAgentRuntime.execute          <- sets the turn id
  -> handle_tool_call
    -> send_message_to_agent
      -> loop.create_task(_execute_async())   <- inherits it
        -> ExecutionBatchManager.execute_agent <- reads it
```

Without `ContextVar`, the turn id would have to be threaded through
`handle_tool_call` and every tool signature. The context variable carries it
invisibly, so no signatures changed.

`current_turn_id()` mints a `detached-<uuid>` when there is no turn in scope.
Trigger firings and watcher notifications have no interaction turn, and this
gives each its own batch rather than letting it join someone else's.

### The change, part 2: one batch per turn

`self._batch_state: Optional[_BatchState]` became
`self._batches: Dict[str, _BatchState]`, keyed by turn id.

### The change, part 3: the hold timer

```python
async def _hold_timer(self, batch_id):
    while True:
        await asyncio.sleep(self.max_hold_seconds)
        async with self._batch_lock:
            state = self._batches.get(batch_id)
            if state is None:
                return
            if state.results:
                payload = self._format_batch_payload(state.results)
                state.results = []          # hand off, keep the batch open
        if payload:
            await self._dispatch_to_interaction_agent(payload)
```

Every 5 s it flushes whatever has completed. Clearing `state.results` while
leaving the batch open is the important detail: the batch still has pending
agents, and those report later.

That creates a case the old code would have dropped. If the timer flushes and
the batch is subsequently closed, a late result arrives for a batch that no
longer exists. Previously that logged "Dropping result for unknown batch" and
threw the result away. Now:

```python
state = self._batches.get(batch_id)
if state is None:
    dispatch_payload = self._format_batch_payload([result])   # report alone
```

`test_no_results_are_dropped_after_a_hold_flush` asserts every agent appears in
exactly one payload — not zero, not two.

### The tradeoff to be honest about

A straggler *inside* the hold window still delays its siblings. With a 5 s
window, a 3 s slow agent still holds a 0.1 s fast one for 2.9 s. That is
intentional — a single user turn should produce one coherent reply rather than a
stream of fragments — but it means the window is a latency/coherence dial. The
fix is that the wait is now bounded by that dial instead of by the 90 s timeout.

### What to say about it

> Results were collected in one global bucket that only emptied when everything
> in it finished, so unrelated turns got merged and fast work waited on slow
> work. Now each turn has its own bucket, carried via a context variable so no
> function signatures changed, plus a timer that releases finished work. A 12 s
> straggler used to delay a finished sibling by 12 s; now it's capped at the
> hold window.

---

## Fix A — roster records, selection, archiving

**Files:** `server/services/execution/roster.py` (rewritten),
`server/agents/interaction_agent/agent.py`,
`server/agents/interaction_agent/tools.py`,
`server/agents/execution_agent/runtime.py`

### The data model

Before: `roster.json` was `["Agent One", "Agent Two"]`.

After: a record per agent.

```python
@dataclass
class AgentRecord:
    name: str
    description: str = ""          # supplied by the model at creation
    emails: List[str] = ...        # harvested from tool traffic
    thread_ids: List[str] = ...    # harvested from tool traffic
    status: str = "active"         # or "archived"
    created_at: str = ""
    last_active_at: str = ""
```

`_deserialize` accepts both shapes. A bare list becomes records with empty
metadata. This runs on every load, so existing installs upgrade silently — and
it is tested, because getting it wrong would erase every user's agent list.

`save()` writes to a temp file then `os.replace`, which is atomic on both POSIX
and Windows. The old code wrote in place; an interruption there would leave a
truncated file and lose every agent's context.

### Where the metadata comes from

**Descriptions** come from the model. `send_message_to_agent` gained an optional
`description` parameter, and the tool schema explains what it's for.

**Emails and thread ids** are harvested automatically. After each tool call:

```python
def _harvest_identifiers(self, tool_args, result):
    from ...services.execution.roster import extract_identifiers, get_agent_roster
    blob = f"{self._safe_json_dump(tool_args)} {self._safe_json_dump(result)}"
    found = extract_identifiers(blob)
    ...
    get_agent_roster().touch(self.agent.name, emails=..., thread_ids=...)
```

`extract_identifiers` regex-matches email addresses and thread-id shapes out of
both the arguments and the result. Replying to a thread therefore associates
that thread id and that recipient with the agent, with no model cooperation
required.

Note the import: `from ...services.execution.roster import get_agent_roster`,
not `from ...services.execution import ...`. The package `__init__` re-exports
the name, which creates a *second* binding for the same singleton. Importing
from the defining module keeps one binding. This was a real bug — a test caught
the harvest writing to a different roster object than the one under assertion.

The whole function is wrapped in `try/except`. Bookkeeping must never fail a
user's Gmail send.

### Selection — `get_agents_for_prompt`

Three tiers, in order:

1. **Strong identifier matches.** If the message mentions an email address or
   thread id that an agent owns, that agent is included unconditionally —
   *including archived ones*. This is the resurrection path.
2. **Lexical overlap.** Tokenise the context, intersect with each active agent's
   tokens (name + description + emails), sort by overlap size then recency.
3. **Recency fill.** If slots remain, add the most recently active agents.

Tier 1 bypasses the archived filter; tier 2 does not. That is the entire
archiving-versus-resurrection mechanism, and it is two `if` statements.

Tokenising strips stopwords and anything under three characters, so filler words
in `"any update on the lease?"` do not match every agent containing "the".

### The context window — why "did she reply?" works

The most subtle piece. `_render_active_agents` used to take no arguments. Now:

```python
def _recent_context(transcript: str, latest_text: str) -> str:
    payloads = [m.group(2).strip() for m in _ENTRY_RE.finditer(transcript or "")]
    return "\n".join([*payloads[-RECENT_CONTEXT_ENTRIES:], latest_text or ""])
```

It parses the payloads out of the last six transcript entries and concatenates
them with the new message. Selection scores against all of that.

Why it's necessary: *"did she reply?"* contains no word matching any agent name.
Scoring the new message alone finds nothing. The preceding turn — *"ping priya
about the contract redlines"* — supplies "priya", "contract", "redlines".

`prepare_message_with_history` already received the transcript, so no signature
changed. `test_recent_conversation_context_resolves_a_pronoun_followup` pins
this: the target ranks first *with* context and not without.

### Rendering

```xml
<agent name="Email to Priya re: contract redlines"
       purpose="Waiting on Priya's contract redlines"
       contacts="priya.raghavan@northwind.io" />
<!-- 185 further agents are not shown. If none of the agents above fits,
     reuse one by its exact name or create a new one. -->
```

The comment matters. Without it the model sees fifteen agents and assumes that
is all of them. It is told the list is filtered and that exact names still work.

### The metric that guards this fix

The selector can now *exclude the right agent* — a failure mode the old code
could not have, since it showed everything. So E5 scores `target_in_prompt`
separately from accuracy. A fix that improved precision by dropping the answer
would look like a win on accuracy alone.

It was 20/20 in every arm. Recall is intact; the accuracy gain is real.

### What to say about it

> The roster was every agent ever created, pasted whole into every prompt, and
> the model had to find the right one. Now each agent carries a description and
> the addresses and threads it has handled, selection reads the last few
> messages as well as the new one so pronoun follow-ups resolve, idle agents are
> archived rather than deleted, and a matching email or thread id can pull an
> archived agent back. Two hundred agents down to fifteen shown, 10 KB of prompt
> down to 1.3 KB, and routing accuracy back to 100% on both models tested —
> with the correct agent still present in the prompt in every single case, which
> is how you know the gain isn't just a luckier shortlist.

---

## How the tests and experiments are built

Worth understanding, because the same trick underpins all of it.

### The sandbox

`experiments/harness.py::bootstrap()` copies `server/` into a temp directory and
imports it from there. Every store in this codebase computes its path from
`__file__` and builds its singleton at import time, so redirecting them by
patching would mean patching about nine places and silently corrupting real data
if one were missed. Copying is one line and cannot miss.

`bootstrap(ref="main")` instead exports that revision with `git archive`. That
is how before/after arms stay comparable — both run from git, not from whatever
happens to be checked out.

### Faking the LLM and Gmail

Both are imported with `from X import Y`, which binds the name into each
consumer's namespace. Patching the defining module does nothing. `FakeLLM.install`
and `FakeGmail.install` walk an explicit list of binding sites — five for the
LLM, four for Gmail.

`FakeGmail` has two modes: `blocking` uses `time.sleep` (faithful to the real
synchronous SDK) and `async` yields instead. That pair is E1's experiment and
control.

### Negative controls

A test that never fails proves nothing. Every fix's tests were run against
`main`:

```bash
OPENPOKE_TEST_REF=main python -m pytest -q   # 24 of 26 fail
```

The two that pass there are regression guards, not fix detectors.

### Three measurement bugs found while running this

Instructive, because each would have produced a confidently wrong number.

1. **Provider errors scored as routing failures.** OpenRouter returns HTTP 200
   with an `error` key in the body for upstream failures. Only `status_code` was
   checked, so "Nvidia: Service temporarily overloaded" was read as "the model
   didn't call a tool." Nine rows were wrong; they were pruned and re-run.
2. **A retry predicate that matched on message text.** `httpx.ReadError`
   stringifies to `""`, so it matched nothing, was treated as fatal, and killed
   a 40-call run at 28. Retries now classify by exception *type*.
3. **A flip metric that counted the wrong thing.** Paired regressions initially
   counted any case that was correct at 5 and not-correct at 200, including
   `no_tool_call` — a tool-calling failure, not a routing one. Restricting it to
   cases that routed at both sizes changed nemotron's headline from 9/20 to
   4/12.

The common thread: keep *the thing being measured* separate from *everything
else that can go wrong*. That is also why `target_in_prompt`, `no_tool_call`,
and accuracy are three different columns.

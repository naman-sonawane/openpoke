"""Shared harness for OpenPoke scaling experiments.

Nothing under server/ is modified. Every experiment runs against a throwaway
copy of the server package placed in temp storage, so all of the log files,
roster JSON and SQLite databases that the server writes at import time land
somewhere disposable instead of in the real server/data/ directory.

Three things have to be faked to run without network access:

  * request_chat_completion  - bound by name into five modules
  * execute_gmail_tool       - bound by name into four modules
  * the active Gmail user id - a module global in services.gmail.client

The first two are imported with ``from X import Y``, so the name lives in each
consumer's namespace and patching the defining module alone does nothing. Each
binding site has to be patched individually.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# Modules that hold their own reference to request_chat_completion.
_LLM_BINDING_SITES = (
    "server.agents.interaction_agent.runtime",
    "server.agents.execution_agent.runtime",
    "server.agents.execution_agent.tasks.search_email.tool",
    "server.services.gmail.importance_classifier",
    "server.services.conversation.summarization.summarizer",
)

# Modules that hold their own reference to execute_gmail_tool.
_GMAIL_BINDING_SITES = (
    "server.agents.execution_agent.tools.gmail",
    "server.agents.execution_agent.tasks.search_email.gmail_internal",
    "server.agents.execution_agent.tasks.search_email.tool",
    "server.services.gmail.importance_watcher",
)

_AGENT_NAME_RE = re.compile(r"^Agent Name:\s*(.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _install_fcntl_shim() -> bool:
    """services.execution.roster hard-imports fcntl, which Windows lacks."""
    try:
        import fcntl  # noqa: F401

        return False
    except ImportError:
        pass

    shim = types.ModuleType("fcntl")
    shim.LOCK_SH = 1
    shim.LOCK_EX = 2
    shim.LOCK_NB = 4
    shim.LOCK_UN = 8
    shim.flock = lambda fd, op: None  # single process: nothing to serialize
    sys.modules["fcntl"] = shim
    return True


def load_repo_env() -> None:
    """Load the real repo .env into os.environ.

    The sandbox copy's config._load_env_file looks next to its own __file__,
    which in temp storage has no .env, so the real keys would never load.
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            key, value = key.strip(), value.strip().strip("'\"")
            if key and value:
                os.environ[key] = value


def eval_model() -> str:
    """The model to use for experiments that make real calls.

    config.py:54 hardcodes anthropic/claude-sonnet-4 with no env fallback, so
    experiments pass this explicitly rather than going through Settings.
    """
    model = os.environ.get("OPENPOKE_EVAL_MODEL", "").strip()
    if not model:
        raise RuntimeError("OPENPOKE_EVAL_MODEL is not set; check the repo .env")
    return model


@dataclass
class Sandbox:
    """A disposable copy of the server package plus its imported modules."""

    root: Path
    shimmed_fcntl: bool
    ref: str
    modules: Dict[str, Any] = field(default_factory=dict)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _materialize_server(tmp_root: Path, ref: Optional[str]) -> str:
    """Put a server/ tree into tmp_root, from git or from the working tree."""
    if ref is None:
        shutil.copytree(
            REPO_ROOT / "server",
            tmp_root / "server",
            ignore=shutil.ignore_patterns("data", "__pycache__", "*.pyc"),
        )
        return "working-tree"

    import io
    import subprocess
    import tarfile

    proc = subprocess.run(
        ["git", "archive", "--format=tar", ref, "server"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git archive {ref} failed: {proc.stderr.decode(errors='replace')}")
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        tar.extractall(tmp_root)

    sha = subprocess.run(
        ["git", "rev-parse", "--short", ref], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.strip()
    return f"{ref}@{sha}"


def bootstrap(ref: Optional[str] = None) -> Sandbox:
    """Copy server/ somewhere disposable and import it from there.

    ref=None uses the current working tree. Passing a git ref exports that
    revision instead, which is how baseline and fixed arms are compared.
    """
    shimmed = _install_fcntl_shim()

    tmp_root = Path(tempfile.mkdtemp(prefix="openpoke-exp-"))
    resolved_ref = _materialize_server(tmp_root, ref)

    os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-for-experiments")
    sys.path.insert(0, str(tmp_root))

    for name in list(sys.modules):
        if name == "server" or name.startswith("server."):
            del sys.modules[name]

    import server.agents.execution_agent.runtime as exec_runtime
    import server.agents.execution_agent.tools.gmail as exec_gmail
    import server.agents.interaction_agent.agent as inter_agent
    import server.agents.interaction_agent.runtime as inter_runtime
    import server.agents.interaction_agent.tools as inter_tools
    import server.agents.execution_agent.batch_manager as batch_manager
    import server.openrouter_client.client as openrouter_client
    import server.services.execution.roster as roster_module
    import server.services.gmail.client as gmail_client

    # Verify the copy is what got imported, not the real tree.
    loaded_from = Path(exec_runtime.__file__).resolve()
    if tmp_root not in loaded_from.parents:
        raise RuntimeError(f"sandbox import failed; loaded {loaded_from}")

    gmail_client._ACTIVE_USER_ID = "experiment-user"

    # The server logs every tool call at INFO; it drowns the results table.
    import logging

    logging.getLogger("openpoke.server").setLevel(logging.ERROR)

    return Sandbox(
        root=tmp_root,
        shimmed_fcntl=shimmed,
        ref=resolved_ref,
        modules={
            "exec_runtime": exec_runtime,
            "exec_gmail": exec_gmail,
            "inter_agent": inter_agent,
            "inter_runtime": inter_runtime,
            "inter_tools": inter_tools,
            "batch_manager": batch_manager,
            "openrouter_client": openrouter_client,
            "roster": roster_module,
            "gmail_client": gmail_client,
        },
    )


# ---------------------------------------------------------------------------
# Fake LLM
# ---------------------------------------------------------------------------


@dataclass
class LLMCall:
    """One recorded call into the fake LLM."""

    t_start: float
    t_end: float
    agent_name: Optional[str]
    system_bytes: int
    messages_bytes: int
    n_messages: int

    @property
    def total_bytes(self) -> int:
        return self.system_bytes + self.messages_bytes


def tool_call_response(name: str, arguments: str = "{}", call_id: str = "call_1") -> Dict[str, Any]:
    """An OpenRouter-shaped assistant message that requests one tool call."""
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            }
        ]
    }


def text_response(content: str) -> Dict[str, Any]:
    """An OpenRouter-shaped assistant message with no tool calls."""
    return {"choices": [{"message": {"content": content, "tool_calls": []}}]}


class FakeLLM:
    """Stand-in for request_chat_completion.

    ``responder`` receives (agent_name, system, messages) and returns
    (payload, latency_seconds). Latency is burned with asyncio.sleep so it
    models a real non-blocking httpx call.
    """

    def __init__(self, responder: Callable[..., Tuple[Dict[str, Any], float]]):
        self._responder = responder
        self.calls: List[LLMCall] = []

    async def __call__(
        self,
        *,
        model: str,
        messages: Sequence[Dict[str, Any]],
        system: Optional[str] = None,
        api_key: Optional[str] = None,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        t_start = time.perf_counter()
        system_text = system or ""
        match = _AGENT_NAME_RE.search(system_text)
        agent_name = match.group(1).strip() if match else None

        payload, latency = self._responder(agent_name, system_text, list(messages))
        if latency > 0:
            await asyncio.sleep(latency)

        self.calls.append(
            LLMCall(
                t_start=t_start,
                t_end=time.perf_counter(),
                agent_name=agent_name,
                system_bytes=len(system_text.encode("utf-8")),
                messages_bytes=sum(len(str(m).encode("utf-8")) for m in messages),
                n_messages=len(messages),
            )
        )
        return payload

    def install(self, sandbox: Sandbox) -> None:
        for mod_name in _LLM_BINDING_SITES:
            module = sys.modules.get(mod_name)
            if module is not None and hasattr(module, "request_chat_completion"):
                setattr(module, "request_chat_completion", self)

    def reset(self) -> None:
        self.calls.clear()


# ---------------------------------------------------------------------------
# Fake Gmail
# ---------------------------------------------------------------------------


def build_fake_inbox(n: int = 25) -> List[Dict[str, Any]]:
    """A small inbox shaped like Composio's GMAIL_FETCH_EMAILS message list."""
    senders = ["ops@example.com", "recruiting@example.com", "billing@example.com"]
    return [
        {
            "messageId": f"msg-{i:04d}",
            "threadId": f"thread-{i // 3:04d}",
            "labelIds": ["INBOX", "UNREAD"] if i % 4 else ["INBOX"],
            "messageTimestamp": f"2026-09-{(i % 28) + 1:02d}T09:{i % 60:02d}:00Z",
            "subject": f"Subject line number {i}",
            "sender": senders[i % len(senders)],
            "to": "user@example.com",
            "messageText": f"Body of message {i}. " * 12,
            "attachmentList": [],
        }
        for i in range(n)
    ]


class FakeGmail:
    """Stand-in for execute_gmail_tool with switchable blocking behaviour.

    mode="blocking" uses time.sleep, faithfully modelling the real synchronous
    Composio SDK call sitting on the event loop thread.

    mode="async" returns a coroutine instead. ExecutionAgentRuntime._execute_tool
    awaits anything awaitable, so this is the counterfactual: identical latency,
    but yielded back to the loop.
    """

    def __init__(self, latency_seconds: float, mode: str = "blocking", inbox_size: int = 25):
        if mode not in {"blocking", "async"}:
            raise ValueError(f"unknown mode: {mode}")
        self.latency = latency_seconds
        self.mode = mode
        self.inbox = build_fake_inbox(inbox_size)
        self.call_count = 0

    def _payload(self) -> Dict[str, Any]:
        return {"data": {"messages": self.inbox}, "successful": True, "error": None}

    async def _async_payload(self) -> Dict[str, Any]:
        await asyncio.sleep(self.latency)
        return self._payload()

    def __call__(self, tool_name: str, composio_user_id: str, *, arguments=None) -> Any:
        self.call_count += 1
        if self.mode == "blocking":
            time.sleep(self.latency)
            return self._payload()
        return self._async_payload()

    def install(self, sandbox: Sandbox) -> None:
        for mod_name in _GMAIL_BINDING_SITES:
            module = sys.modules.get(mod_name)
            if module is not None and hasattr(module, "execute_gmail_tool"):
                setattr(module, "execute_gmail_tool", self)


# ---------------------------------------------------------------------------
# Event loop watchdog
# ---------------------------------------------------------------------------


class LoopWatchdog:
    """Measures how late the event loop is when resuming a fixed-interval sleep.

    Lateness is the direct signal for "one coroutine is monopolising the loop".
    It is also what an inbound /chat/history request would experience.
    """

    def __init__(self, interval: float = 0.01):
        self.interval = interval
        self.lateness: List[float] = []
        self._stop = False
        self._task: Optional[asyncio.Task] = None

    async def _run(self) -> None:
        while not self._stop:
            t0 = time.perf_counter()
            await asyncio.sleep(self.interval)
            self.lateness.append(time.perf_counter() - t0 - self.interval)

    def start(self) -> None:
        self._stop = False
        self.lateness.clear()
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def max_lateness_ms(self) -> float:
        return max(self.lateness) * 1000 if self.lateness else 0.0

    @property
    def p95_lateness_ms(self) -> float:
        if not self.lateness:
            return 0.0
        ordered = sorted(self.lateness)
        return ordered[int(0.95 * (len(ordered) - 1))] * 1000


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def linear_fit(xs: Sequence[float], ys: Sequence[float]) -> Tuple[float, float, float]:
    """Ordinary least squares. Returns (slope, intercept, r_squared)."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, 1.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    if sxx == 0:
        return 0.0, mean_y, 1.0
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot else 1.0
    return slope, intercept, r2


def summarize(values: Sequence[float]) -> Dict[str, float]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "max": ordered[-1],
        "stdev": statistics.stdev(ordered) if len(ordered) > 1 else 0.0,
    }


def rule(title: str = "", width: int = 78) -> str:
    if not title:
        return "-" * width
    return f"--- {title} " + "-" * max(0, width - len(title) - 5)

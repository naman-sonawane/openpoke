"""Coordinate execution agents and batch their results for the interaction agent."""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .runtime import ExecutionAgentRuntime, ExecutionResult
from ...logging_config import logger


#: Identifies the interaction turn that dispatched an agent, so results from
#: unrelated turns are never merged into one <agent_message>. Set by
#: InteractionAgentRuntime and inherited by the tasks it spawns, because
#: asyncio.create_task copies the current context.
_TURN_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "openpoke_turn_id", default=None
)


def set_turn_id(turn_id: str) -> contextvars.Token:
    return _TURN_ID.set(turn_id)


def reset_turn_id(token: contextvars.Token) -> None:
    _TURN_ID.reset(token)


def current_turn_id() -> str:
    """Return the turn in scope, minting a detached one when there is none.

    Trigger firings and watcher notifications have no interaction turn, so they
    each get their own batch rather than joining somebody else's.
    """
    value = _TURN_ID.get()
    if value is None:
        value = f"detached-{uuid.uuid4()}"
        _TURN_ID.set(value)
    return value


@dataclass
class PendingExecution:
    """Track a pending execution request."""

    request_id: str
    agent_name: str
    instructions: str
    batch_id: str
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class _BatchState:
    """Collect results for a single interaction-agent turn."""

    batch_id: str
    created_at: datetime = field(default_factory=datetime.now)
    pending: int = 0
    results: List[ExecutionResult] = field(default_factory=list)
    hold_task: Optional["asyncio.Task[None]"] = None


class ExecutionBatchManager:
    """Run execution agents and deliver their combined outcome."""

    # Initialize batch manager with timeout and coordination state for execution agents
    def __init__(self, timeout_seconds: int = 90, max_hold_seconds: float = 5.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_hold_seconds = max_hold_seconds
        self._pending: Dict[str, PendingExecution] = {}
        self._batch_lock = asyncio.Lock()
        # One batch per interaction turn. A single shared batch made every turn
        # wait for the slowest agent of every other turn in flight.
        self._batches: Dict[str, _BatchState] = {}

    # Run execution agent with timeout handling and batch coordination for interaction agent
    async def execute_agent(
        self,
        agent_name: str,
        instructions: str,
        request_id: Optional[str] = None,
    ) -> ExecutionResult:
        """Execute an agent asynchronously and buffer the result for batch dispatch."""

        if not request_id:
            request_id = str(uuid.uuid4())

        batch_id = await self._register_pending_execution(agent_name, instructions, request_id)

        try:
            logger.info(f"[{agent_name}] Execution started")
            runtime = ExecutionAgentRuntime(agent_name=agent_name)
            result = await asyncio.wait_for(
                runtime.execute(instructions),
                timeout=self.timeout_seconds,
            )
            status = "SUCCESS" if result.success else "FAILED"
            logger.info(f"[{agent_name}] Execution finished: {status}")
        except asyncio.TimeoutError:
            logger.error(f"[{agent_name}] Execution timed out after {self.timeout_seconds}s")
            result = ExecutionResult(
                agent_name=agent_name,
                success=False,
                response=f"Execution timed out after {self.timeout_seconds} seconds",
                error="Timeout",
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(f"[{agent_name}] Execution failed unexpectedly")
            result = ExecutionResult(
                agent_name=agent_name,
                success=False,
                response=f"Execution failed: {exc}",
                error=str(exc),
            )
        finally:
            self._pending.pop(request_id, None)

        await self._complete_execution(batch_id, result, agent_name)
        return result

    # Add execution request to current batch or create new batch if none exists
    async def _register_pending_execution(
        self,
        agent_name: str,
        instructions: str,
        request_id: str,
    ) -> str:
        """Attach a new execution to the active batch, opening one when required."""

        batch_id = current_turn_id()
        async with self._batch_lock:
            state = self._batches.get(batch_id)
            if state is None:
                state = _BatchState(batch_id=batch_id)
                self._batches[batch_id] = state
                if self.max_hold_seconds > 0:
                    state.hold_task = asyncio.get_running_loop().create_task(
                        self._hold_timer(batch_id), name=f"batch-hold-{batch_id}"
                    )

            state.pending += 1
            self._pending[request_id] = PendingExecution(
                request_id=request_id,
                agent_name=agent_name,
                instructions=instructions,
                batch_id=batch_id,
            )

            return batch_id

    async def _hold_timer(self, batch_id: str) -> None:
        """Flush whatever has completed so one straggler cannot hold the rest.

        Without this, a batch only reports when pending reaches zero, so an agent
        that finishes in two seconds waits out a sibling that takes ninety.
        """
        try:
            while True:
                await asyncio.sleep(self.max_hold_seconds)
                payload: Optional[str] = None
                async with self._batch_lock:
                    state = self._batches.get(batch_id)
                    if state is None:
                        return
                    if state.results:
                        payload = self._format_batch_payload(state.results)
                        state.results = []
                if payload:
                    logger.info(f"Batch {batch_id} flushed on hold timer")
                    await self._dispatch_to_interaction_agent(payload)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise

    # Store execution result and send combined batch to interaction agent when complete
    async def _complete_execution(
        self,
        batch_id: str,
        result: ExecutionResult,
        agent_name: str,
    ) -> None:
        """Record the execution result and dispatch when the batch drains."""

        dispatch_payload: Optional[str] = None

        async with self._batch_lock:
            state = self._batches.get(batch_id)
            if state is None:
                # The hold timer already closed this batch out; report alone
                # rather than dropping the result on the floor.
                dispatch_payload = self._format_batch_payload([result])
            else:
                state.results.append(result)
                state.pending -= 1

                if state.pending <= 0:
                    if state.results:
                        dispatch_payload = self._format_batch_payload(state.results)
                        agent_names = [entry.agent_name for entry in state.results]
                        logger.info(f"Execution batch completed: {', '.join(agent_names)}")
                    if state.hold_task is not None:
                        state.hold_task.cancel()
                    self._batches.pop(batch_id, None)

        if dispatch_payload:
            await self._dispatch_to_interaction_agent(dispatch_payload)

    # Return list of currently pending execution requests for monitoring purposes
    def get_pending_executions(self) -> List[Dict[str, str]]:
        """Expose pending executions for observability."""

        return [
            {
                "request_id": pending.request_id,
                "agent_name": pending.agent_name,
                "batch_id": pending.batch_id,
                "created_at": pending.created_at.isoformat(),
                "elapsed_seconds": (datetime.now() - pending.created_at).total_seconds(),
            }
            for pending in self._pending.values()
        ]

    # Clean up all pending executions and batch state on shutdown
    async def shutdown(self) -> None:
        """Clear pending bookkeeping (no background work remains)."""

        self._pending.clear()
        async with self._batch_lock:
            for state in self._batches.values():
                if state.hold_task is not None:
                    state.hold_task.cancel()
            self._batches.clear()

    # Format multiple execution results into single message for interaction agent
    def _format_batch_payload(self, results: List[ExecutionResult]) -> str:
        """Render execution results into the interaction-agent format."""

        entries: List[str] = []
        for result in results:
            status = "SUCCESS" if result.success else "FAILED"
            response_text = (result.response or "(no response provided)").strip()
            entries.append(f"[{status}] {result.agent_name}: {response_text}")
        return "\n".join(entries)

    # Forward combined execution results to interaction agent for user response generation
    async def _dispatch_to_interaction_agent(self, payload: str) -> None:
        """Send the aggregated execution summary to the interaction agent."""

        from ..interaction_agent.runtime import InteractionAgentRuntime

        runtime = InteractionAgentRuntime()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(runtime.handle_agent_message(payload))
            return

        loop.create_task(runtime.handle_agent_message(payload))

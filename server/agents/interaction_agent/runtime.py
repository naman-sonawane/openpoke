"""Interaction Agent Runtime - handles LLM calls for user and agent turns."""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .agent import build_system_prompt, prepare_message_with_history
from .tools import ToolResult, get_tool_schemas, handle_tool_call
from ..execution_agent.batch_manager import reset_turn_id, set_turn_id
from ...config import get_settings
from ...services.conversation import get_conversation_log, get_working_memory_log
from ...services.intake import background_screen, get_call_state, handoff_on_error, pre_screen
from ...openrouter_client import PRIVATE_FAST_ROUTING, request_chat_completion
from ...logging_config import logger


@dataclass
class InteractionResult:
    """Result from the interaction agent."""

    success: bool
    response: str
    error: Optional[str] = None
    execution_agents_used: int = 0
    llm_calls: int = 0
    # Where the turn's time went: {"gate_ms": .., "llm_ms": [..], "prompt_tokens": [..]}
    timings: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _ToolCall:
    """Parsed tool invocation from an LLM response."""

    identifier: Optional[str]
    name: str
    arguments: Dict[str, Any]


class _SafetyPreempted(Exception):
    """The always-on screen flagged the turn; its reply replaces the model's."""

    def __init__(self, reply: str):
        super().__init__(reply)
        self.reply = reply


@dataclass
class _LoopSummary:
    """Aggregate information produced by the interaction loop."""

    last_assistant_text: str = ""
    user_messages: List[str] = field(default_factory=list)
    tool_names: List[str] = field(default_factory=list)
    execution_agents: Set[str] = field(default_factory=set)
    llm_calls: int = 0
    llm_ms: List[int] = field(default_factory=list)
    prompt_tokens: List[int] = field(default_factory=list)
    # Time spent waiting on the always-on screen after the model had answered
    # (zero when the screen finished first, which is the usual case).
    screen_wait_ms: int = 0


class InteractionAgentRuntime:
    """Manages the interaction agent's request processing."""

    MAX_TOOL_ITERATIONS = 8

    # Initialize interaction agent runtime with settings and service dependencies
    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.openrouter_api_key
        self.model = settings.interaction_agent_model
        self.settings = settings
        self._shared_log = get_conversation_log()
        self.working_memory_log = get_working_memory_log()
        self.tool_schemas = get_tool_schemas()

        if not self.api_key:
            raise ValueError(
                "OpenRouter API key not configured. Set OPENROUTER_API_KEY environment variable."
            )

    @property
    def conversation_log(self):
        """OpenPoke's single shared log, or in intake mode the current call's own
        transcript, so concurrent callers never see each other's conversation."""
        if self.settings.intake_mode:
            return get_call_state().transcript
        return self._shared_log

    # Main entry point for processing user messages through the LLM interaction loop
    async def execute(self, user_message: str) -> InteractionResult:
        """Handle a user-authored message."""

        token = set_turn_id(f"user-{uuid.uuid4()}")
        try:
            transcript_before = self._load_conversation_transcript()
            self.conversation_log.record_user_message(user_message)

            # The safety screen runs before the model on every caller turn. When
            # it answers (911 script, confirm question) the model is skipped.
            safety_note = None
            safety_task: Optional[asyncio.Task] = None
            gate_ms = 0
            if self.settings.intake_mode:
                gate_started = time.perf_counter()
                decision = await pre_screen(user_message, context=transcript_before[-2000:])
                gate_ms = round((time.perf_counter() - gate_started) * 1000)
                if decision.reply:
                    self.conversation_log.record_reply(decision.reply)
                    return InteractionResult(
                        success=True, response=decision.reply, timings={"gate_ms": gate_ms, "llm_ms": []}
                    )
                safety_note = decision.note
                if decision.needs_background_screen:
                    # No keyword matched: run the model screen alongside the
                    # conversation model instead of in front of it.
                    safety_task = asyncio.create_task(
                        background_screen(user_message, context=transcript_before[-2000:])
                    )

            system_prompt = build_system_prompt()
            messages = prepare_message_with_history(
                user_message, transcript_before, message_type="user", safety_note=safety_note
            )

            logger.info("Processing user message through interaction agent")
            try:
                summary = await self._run_interaction_loop(system_prompt, messages, safety_task)
            except _SafetyPreempted as preempted:
                self.conversation_log.record_reply(preempted.reply)
                return InteractionResult(
                    success=True, response=preempted.reply, timings={"gate_ms": gate_ms, "llm_ms": []}
                )

            final_response = self._finalize_response(summary)
            if not final_response and self.settings.intake_mode:
                final_response = "Sorry, could you say that again?"
            if self.settings.intake_mode:
                final_response = self._apply_reply_opener(final_response)

            if final_response and not summary.user_messages:
                self.conversation_log.record_reply(final_response)

            return InteractionResult(
                success=True,
                response=final_response,
                execution_agents_used=len(summary.execution_agents),
                llm_calls=summary.llm_calls,
                timings={
                    "gate_ms": gate_ms,
                    "llm_ms": summary.llm_ms,
                    "prompt_tokens": summary.prompt_tokens,
                    "screen_wait_ms": summary.screen_wait_ms,
                },
            )

        except Exception as exc:
            logger.error("Interaction agent failed", extra={"error": str(exc)})
            if self.settings.intake_mode:
                # Never leave a caller in silence: hand off and say so.
                reply = handoff_on_error(str(exc))
                self.conversation_log.record_reply(reply)
                return InteractionResult(success=False, response=reply, error=str(exc))
            return InteractionResult(
                success=False,
                response="",
                error=str(exc),
            )
        finally:
            if safety_task is not None and not safety_task.done():
                safety_task.cancel()
            reset_turn_id(token)

    # Handle incoming messages from execution agents and generate appropriate responses
    async def handle_agent_message(self, agent_message: str) -> InteractionResult:
        """Process a status update emitted by an execution agent."""

        token = set_turn_id(f"agent-{uuid.uuid4()}")
        try:
            transcript_before = self._load_conversation_transcript()
            self.conversation_log.record_agent_message(agent_message)

            system_prompt = build_system_prompt()
            messages = prepare_message_with_history(
                agent_message, transcript_before, message_type="agent"
            )

            logger.info("Processing execution agent results")
            summary = await self._run_interaction_loop(system_prompt, messages)

            final_response = self._finalize_response(summary)

            if final_response and not summary.user_messages:
                self.conversation_log.record_reply(final_response)

            return InteractionResult(
                success=True,
                response=final_response,
                execution_agents_used=len(summary.execution_agents),
            )

        except Exception as exc:
            logger.error("Interaction agent (agent message) failed", extra={"error": str(exc)})
            return InteractionResult(
                success=False,
                response="",
                error=str(exc),
            )
        finally:
            reset_turn_id(token)

    # Core interaction loop that handles LLM calls and tool executions until completion
    async def _run_interaction_loop(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        safety_task: Optional[asyncio.Task] = None,
    ) -> _LoopSummary:
        """Iteratively query the LLM until it issues a final response.

        safety_task, when given, is the always-on screen running in parallel.
        It is awaited after the first model response and before any tool runs
        or any text is used, so a flagged turn has no side effects.
        """

        summary = _LoopSummary()

        for iteration in range(self.MAX_TOOL_ITERATIONS):
            call_started = time.perf_counter()
            response = await self._make_llm_call(system_prompt, messages)
            summary.llm_calls += 1
            summary.llm_ms.append(round((time.perf_counter() - call_started) * 1000))
            summary.prompt_tokens.append((response.get("usage") or {}).get("prompt_tokens") or 0)

            if safety_task is not None:
                wait_started = time.perf_counter()
                verdict = await safety_task
                summary.screen_wait_ms = round((time.perf_counter() - wait_started) * 1000)
                safety_task = None
                if verdict.reply:
                    raise _SafetyPreempted(verdict.reply)
            assistant_message = self._extract_assistant_message(response)

            assistant_content = (assistant_message.get("content") or "").strip()
            if assistant_content:
                summary.last_assistant_text = assistant_content

            raw_tool_calls = assistant_message.get("tool_calls") or []
            parsed_tool_calls = self._parse_tool_calls(raw_tool_calls)

            assistant_entry: Dict[str, Any] = {
                "role": "assistant",
                "content": assistant_message.get("content", "") or "",
            }
            if raw_tool_calls:
                assistant_entry["tool_calls"] = raw_tool_calls
            messages.append(assistant_entry)

            if not parsed_tool_calls:
                break

            for tool_call in parsed_tool_calls:
                summary.tool_names.append(tool_call.name)

                if tool_call.name == "send_message_to_agent":
                    agent_name = tool_call.arguments.get("agent_name")
                    if isinstance(agent_name, str) and agent_name:
                        summary.execution_agents.add(agent_name)

                result = self._execute_tool(tool_call)

                if result.user_message:
                    summary.user_messages.append(result.user_message)

                tool_message = {
                    "role": "tool",
                    "tool_call_id": tool_call.identifier or tool_call.name,
                    "content": self._format_tool_result(tool_call, result),
                }
                messages.append(tool_message)
        else:
            raise RuntimeError("Reached tool iteration limit without final response")

        if not summary.user_messages and not summary.last_assistant_text:
            logger.warning("Interaction loop exited without assistant content")

        return summary

    # Load conversation history, preferring summarized version if available
    def _load_conversation_transcript(self) -> str:
        if self.settings.intake_mode:
            return self.conversation_log.load_transcript()  # calls are short; no summarising
        if self.settings.summarization_enabled:
            rendered = self.working_memory_log.render_transcript()
            if rendered.strip():
                return rendered
        return self.conversation_log.load_transcript()

    # Execute API call to OpenRouter with system prompt, messages, and tool schemas
    async def _make_llm_call(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Make an LLM call via OpenRouter."""

        # Re-read every call: in intake mode the allowed tools change with the
        # call stage, which can move mid-turn (e.g. after lookup_patient).
        used = [
            (call.get("function") or {}).get("name")
            for message in messages
            for call in (message.get("tool_calls") or [])
        ]
        tool_schemas = get_tool_schemas(already_used=[name for name in used if name])
        logger.debug(
            "Interaction agent calling LLM",
            extra={"model": self.model, "tools": len(tool_schemas)},
        )
        if not self.settings.intake_mode:
            return await request_chat_completion(
                model=self.model,
                messages=messages,
                system=system_prompt,
                api_key=self.api_key,
                tools=tool_schemas,
            )
        # On a call every model round trip is dead air: route to the fastest
        # zero-retention provider (measured ~1.05s -> ~0.6-0.75s p50 per call)
        # and cap the reply, which is one or two spoken sentences anyway.
        return await request_chat_completion(
            model=self.model,
            messages=messages,
            system=system_prompt,
            api_key=self.api_key,
            tools=tool_schemas,
            provider=PRIVATE_FAST_ROUTING,
            max_tokens=400,
        )

    # Extract the assistant's message from the OpenRouter API response structure
    def _extract_assistant_message(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Return the assistant message from the raw response payload."""

        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("LLM response did not include an assistant message")
        return message

    # Convert raw LLM tool calls into structured _ToolCall objects with validation
    def _parse_tool_calls(self, raw_tool_calls: List[Dict[str, Any]]) -> List[_ToolCall]:
        """Normalize tool call payloads from the LLM."""

        parsed: List[_ToolCall] = []
        for raw in raw_tool_calls:
            function_block = raw.get("function") or {}
            name = function_block.get("name")
            if not isinstance(name, str) or not name:
                logger.warning("Skipping tool call without name", extra={"tool": raw})
                continue

            arguments, error = self._parse_tool_arguments(function_block.get("arguments"))
            if error:
                logger.warning("Tool call arguments invalid", extra={"tool": name, "error": error})
                parsed.append(
                    _ToolCall(
                        identifier=raw.get("id"),
                        name=name,
                        arguments={"__invalid_arguments__": error},
                    )
                )
                continue

            parsed.append(
                _ToolCall(identifier=raw.get("id"), name=name, arguments=arguments)
            )

        return parsed

    # Parse and validate tool arguments from various formats (dict, JSON string, etc.)
    def _parse_tool_arguments(
        self, raw_arguments: Any
    ) -> tuple[Dict[str, Any], Optional[str]]:
        """Convert tool arguments into a dictionary, reporting errors."""

        if raw_arguments is None:
            return {}, None

        if isinstance(raw_arguments, dict):
            return raw_arguments, None

        if isinstance(raw_arguments, str):
            if not raw_arguments.strip():
                return {}, None
            try:
                parsed = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                return {}, f"invalid json: {exc}"
            if isinstance(parsed, dict):
                return parsed, None
            return {}, "decoded arguments were not an object"

        return {}, f"unsupported argument type: {type(raw_arguments).__name__}"

    # Execute tool calls with error handling and logging, returning standardized results
    def _execute_tool(self, tool_call: _ToolCall) -> ToolResult:
        """Execute a tool call and convert low-level errors into structured results."""

        if "__invalid_arguments__" in tool_call.arguments:
            error = tool_call.arguments["__invalid_arguments__"]
            self._log_tool_invocation(tool_call, stage="rejected", detail={"error": error})
            return ToolResult(success=False, payload={"error": error})

        try:
            self._log_tool_invocation(tool_call, stage="start")
            result = handle_tool_call(tool_call.name, tool_call.arguments)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "Tool execution crashed",
                extra={"tool": tool_call.name, "error": str(exc)},
            )
            self._log_tool_invocation(
                tool_call,
                stage="error",
                detail={"error": str(exc)},
            )
            return ToolResult(success=False, payload={"error": str(exc)})

        if not isinstance(result, ToolResult):
            logger.warning(
                "Tool did not return ToolResult; coercing",
                extra={"tool": tool_call.name},
            )
            wrapped = ToolResult(success=True, payload=result)
            self._log_tool_invocation(tool_call, stage="done", result=wrapped)
            return wrapped

        status = "success" if result.success else "error"
        logger.debug(
            "Tool executed",
            extra={
                "tool": tool_call.name,
                "status": status,
            },
        )
        self._log_tool_invocation(tool_call, stage="done", result=result)
        return result

    # Format tool execution results into JSON for LLM consumption
    def _format_tool_result(self, tool_call: _ToolCall, result: ToolResult) -> str:
        """Render a tool execution result back to the LLM."""

        payload: Dict[str, Any] = {
            "tool": tool_call.name,
            "status": "success" if result.success else "error",
            "arguments": {
                key: value
                for key, value in tool_call.arguments.items()
                if key != "__invalid_arguments__"
            },
        }

        if result.payload is not None:
            key = "result" if result.success else "error"
            payload[key] = result.payload

        return self._safe_json_dump(payload)

    # Safely serialize objects to JSON with fallback to string representation
    def _safe_json_dump(self, payload: Any) -> str:
        """Serialize payload to JSON, falling back to repr on failure."""

        try:
            return json.dumps(payload, default=str)
        except TypeError:
            return repr(payload)

    # Log tool execution stages (start, done, error) with structured metadata
    def _log_tool_invocation(
        self,
        tool_call: _ToolCall,
        *,
        stage: str,
        result: Optional[ToolResult] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit structured logs for tool lifecycle events."""

        cleaned_args = {
            key: value
            for key, value in tool_call.arguments.items()
            if key != "__invalid_arguments__"
        }

        log_payload: Dict[str, Any] = {
            "tool": tool_call.name,
            "stage": stage,
            "arguments": cleaned_args,
        }

        if result is not None:
            log_payload["success"] = result.success
            if result.payload is not None:
                log_payload["payload"] = result.payload

        if detail:
            log_payload.update(detail)

        if stage == "done" and result is not None and not result.success:
            # Only the error code: payloads can carry names, numbers or dates
            # of birth, which must not land in plaintext server logs.
            code = result.payload.get("error") if isinstance(result.payload, dict) else "error"
            logger.info(f"Tool '{tool_call.name}' returned error: {str(code)[:80]}")
        elif stage == "done":
            logger.info(f"Tool '{tool_call.name}' completed")
        elif stage in {"error", "rejected"}:
            logger.warning(f"Tool '{tool_call.name}' {stage}")
        else:
            logger.debug(f"Tool '{tool_call.name}' {stage}")

    def _apply_reply_opener(self, reply: str) -> str:
        """Make sure words a tool required (an acknowledgement, a privacy
        refusal) open the reply, if the model left them out."""

        state = get_call_state()
        opener, state.reply_opener = state.reply_opener, None
        if not opener:
            return reply
        if opener.lower()[:24] in reply.lower():
            return reply
        return f"{opener} {reply}".strip()

    # Determine final user-facing response from interaction loop summary
    def _finalize_response(self, summary: _LoopSummary) -> str:
        """Decide what text should be exposed to the user as the final reply."""

        if summary.user_messages:
            return summary.user_messages[-1]

        return summary.last_assistant_text
